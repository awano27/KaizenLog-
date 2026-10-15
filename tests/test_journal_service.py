from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
import pytest

from kaizenlog.config import Config
from kaizenlog.collector import ActivityEvent, InputObservation
from kaizenlog.reliability import FailureReason, QualityState
from kaizenlog.vault import DailyNoteStore
from kaizenlog.setup import SetupOptions
from kaizenlog.journal_service import (
    _Collected,
    SourceSnapshot,
    SourceState,
    PlannedUpdate,
    Precondition,
    JournalPreview,
    JournalExecutionResult,
    execute_journal,
    execute_report_no_llm,
    preview_report_no_llm,
    preview_journal,
    preview_initial_setup,
    save_initial_setup,
    _write_note_cas,
    open_daily_note,
    sample_journal,
)


def _cfg(tmp_path: Path) -> Config:
    return Config(
        vault_dir=tmp_path,
        daily_notes_dir="notes",
        stats_dir="stats",
        logs_dir="logs",
        memory_dir="memory",
        experiments_dir="experiments",
        ops_db_path=tmp_path / "ops.sqlite3",
        timezone="Asia/Tokyo",
        aw_base_url="http://127.0.0.1:9",
    )


def test_sample_journal_is_memory_only(tmp_path: Path):
    result = sample_journal()
    assert result.status == "sample"
    assert result.writes == ()
    assert result.provider_probe_count == 0


def test_target_day_uses_config_timezone(tmp_path: Path):
    cfg = _cfg(tmp_path)
    preview = preview_journal(
        cfg,
        now=datetime.fromisoformat("2026-01-15T15:30:00+00:00"),
    )
    assert preview.target_day == date(2026, 1, 16)
    assert preview.next_day == date(2026, 1, 17)


def test_preview_has_zero_provider_probes_and_no_catch_up(tmp_path: Path):
    preview = preview_journal(_cfg(tmp_path), date(2026, 1, 15))
    assert preview.provider_probe_count == 0
    assert preview.catch_up is False
    assert {u.target for u in preview.planned_updates} >= {"target_day", "next_day", "ledger"}


def _fixture_source(monkeypatch, cfg: Config, *, count: list[int] | None = None):
    day = date(2026, 1, 15)
    event = ActivityEvent(
        datetime.fromisoformat("2026-01-15T09:00:00+09:00"),
        datetime.fromisoformat("2026-01-15T10:00:00+09:00"),
        "Code.exe", "journal_service.py",
    )
    def collect(*args, **kwargs):
        if count is not None:
            count.append(1)
        return [event], True
    monkeypatch.setattr("kaizenlog.journal_service.collect_day", collect)
    monkeypatch.setattr(
        "kaizenlog.journal_service.collect_input_observation",
        lambda *args, **kwargs: InputObservation(QualityState.MISSING, [], None, FailureReason.INPUT_BUCKET_MISSING),
    )
    monkeypatch.setattr("kaizenlog.journal_service.available_adapters", lambda cfg: [])


def _observed(cfg: Config, day: date) -> _Collected:
    next_day = day.fromordinal(day.toordinal() + 1)
    snapshot = SourceSnapshot(
        day, next_day, cfg.timezone, "activity", None, (), {}, (), (), (), (), (),
        "source", 0, "## Activity\n\nObserved", "## Resume\n\nHandoff",
    )
    return _Collected(SourceState("observed", None, "source", datetime.now().astimezone()), snapshot)


def test_source_snapshot_reuses_existing_generators(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    calls = []
    _fixture_source(monkeypatch, cfg, count=calls)
    preview = preview_journal(cfg, date(2026, 1, 15))
    result = execute_journal(cfg, preview)
    assert len(calls) == 1
    assert result.status == "generated"


def test_next_day_resume_uses_target_day_stats(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    assert preview.status == "ready"
    assert "きのうの続きから" in preview.snapshot.next_markdown
    assert "09:00–10:00" in preview.snapshot.next_markdown


def test_public_fresh_journal_stats_report_roundtrip(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    result = execute_journal(cfg, preview)
    assert result.status == "generated"
    assert (cfg.stats_path / "2026-01-15.json").is_file()
    report_preview = preview_report_no_llm(cfg, date(2026, 1, 15))
    assert report_preview.status == "ready"
    report_result = execute_report_no_llm(cfg, report_preview)
    assert report_result.status == "generated"
    assert report_result.pending_updates == ()


def test_none_ops_db_uses_jsonl_receipt(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.ops_db_path = None
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    receipt = next(update for update in preview.planned_updates if update.target == "runlog")
    assert receipt.path == cfg.logs_path / "runs.jsonl"
    result = execute_journal(cfg, preview)
    assert result.status == "generated"
    assert receipt.path.is_file()
    payload = receipt.path.read_text(encoding="utf-8")
    assert preview.snapshot.ledger_run_id in payload


def test_execute_same_preview_is_already_generated_without_rewrite(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    first = execute_journal(cfg, preview)
    assert first.status == "generated"
    paths = [u.path for u in preview.planned_updates if u.path is not None and u.path.exists()]
    mtimes = {path: path.stat().st_mtime_ns for path in paths}
    second = execute_journal(cfg, preview)
    assert second.status == "already_generated"
    assert second.pending_updates == ()
    assert mtimes == {path: path.stat().st_mtime_ns for path in paths}


def test_execute_same_preview_jsonl_receipt_is_already_generated(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.ops_db_path = None
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    assert execute_journal(cfg, preview).status == "generated"
    receipt = next(update.path for update in preview.planned_updates if update.target == "runlog")
    assert receipt is not None
    before = receipt.stat().st_mtime_ns
    result = execute_journal(cfg, preview)
    assert result.status == "already_generated"
    assert result.pending_updates == ()
    assert receipt.stat().st_mtime_ns == before


def test_completed_jsonl_receipt_with_false_substring_is_conflict(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.ops_db_path = None
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    first = execute_journal(cfg, preview)
    assert first.status == "generated"
    receipt = next(update.path for update in preview.planned_updates if update.target == "runlog")
    assert receipt is not None
    with receipt.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"run_id": preview.snapshot.ledger_run_id, "snapshot_digest": "wrong"}) + "\n")
    result = execute_journal(cfg, preview)
    assert result.status == "conflict"
    assert result.applied_updates == ()


def test_completed_sqlite_receipt_mutation_is_conflict_without_writes(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    first = execute_journal(cfg, preview)
    assert first.status == "generated"
    paths = [u.path for u in preview.planned_updates if u.path is not None and u.path.exists()]
    mtimes = {path: path.stat().st_mtime_ns for path in paths}
    assert cfg.ops_db_path is not None
    with sqlite3.connect(cfg.ops_db_path) as con:
        con.execute("UPDATE runs SET payload_json = ? WHERE run_id = ?", ('{"run_id":"mutated"}', preview.snapshot.ledger_run_id))
    result = execute_journal(cfg, preview)
    assert result.status == "conflict"
    assert result.applied_updates == ()
    assert mtimes == {path: path.stat().st_mtime_ns for path in paths}


def test_completed_sqlite_receipt_delete_is_conflict_without_writes(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fixture_source(monkeypatch, cfg)
    seed_preview = preview_journal(cfg, date(2026, 1, 15))
    assert cfg.ops_db_path is not None
    from kaizenlog.ops_ledger import OpsLedger
    OpsLedger(cfg.ops_db_path).append({
        "run_id": seed_preview.snapshot.ledger_run_id,
        "ts": "2026-01-15T00:00:00+00:00", "command": "journal", "ok": False,
        "duration_seconds": 0.0, "outcome": "seed",
    })
    # Quiesce the fixture's own WAL before exercising the immutable reader.
    with sqlite3.connect(cfg.ops_db_path) as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    preview = preview_journal(cfg, date(2026, 1, 15))
    assert next(update for update in preview.planned_updates if update.target == "ledger").expected_digest is not None
    assert execute_journal(cfg, preview).status == "generated"
    with sqlite3.connect(cfg.ops_db_path) as con:
        con.execute("DELETE FROM runs WHERE run_id = ?", (preview.snapshot.ledger_run_id,))
    result = execute_journal(cfg, preview)
    assert result.status == "conflict"
    assert result.applied_updates == ()


def test_preexisting_sqlite_receipt_replaced_then_same_preview_is_already_generated(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fixture_source(monkeypatch, cfg)
    seed_preview = preview_journal(cfg, date(2026, 1, 15))
    assert cfg.ops_db_path is not None
    from kaizenlog.ops_ledger import OpsLedger
    OpsLedger(cfg.ops_db_path).append({
        "run_id": seed_preview.snapshot.ledger_run_id,
        "ts": "2026-01-15T00:00:00+00:00", "command": "journal", "ok": False,
        "duration_seconds": 0.0, "outcome": "seed",
    })
    with sqlite3.connect(cfg.ops_db_path) as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    preview = preview_journal(cfg, date(2026, 1, 15))
    first = execute_journal(cfg, preview)
    assert first.status == "generated"
    paths = [item.path for item in preview.planned_updates if item.path is not None and item.path.is_file()]
    mtimes = {path: path.stat().st_mtime_ns for path in paths}
    second = execute_journal(cfg, preview)
    assert second.status == "already_generated"
    assert second.pending_updates == ()
    assert mtimes == {path: path.stat().st_mtime_ns for path in paths}


def test_completed_sqlite_receipt_with_extra_field_is_conflict(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    assert execute_journal(cfg, preview).status == "generated"
    assert cfg.ops_db_path is not None
    with sqlite3.connect(cfg.ops_db_path) as con:
        row = con.execute("SELECT payload_json FROM runs WHERE run_id = ?", (preview.snapshot.ledger_run_id,)).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["unexpected"] = True
        con.execute("UPDATE runs SET payload_json = ? WHERE run_id = ?", (json.dumps(payload), preview.snapshot.ledger_run_id))
    with sqlite3.connect(cfg.ops_db_path) as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    result = execute_journal(cfg, preview)
    assert result.status == "conflict"
    assert result.applied_updates == ()


def test_receipt_write_failure_after_sqlite_outputs_is_retryable(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    from kaizenlog.ops_ledger import OpsLedger
    original = OpsLedger.append_if_payload_digest
    monkeypatch.setattr(OpsLedger, "append_if_payload_digest", lambda *args, **kwargs: False)
    first = execute_journal(cfg, preview)
    assert first.status == "partial"
    assert {item.target for item in first.applied_updates} >= {"target_day", "next_day", "stats"}
    assert {item.target for item in first.pending_updates} == {"ledger"}
    output_paths = [item.path for item in preview.planned_updates if item.target in {"target_day", "next_day", "stats"}]
    mtimes = {path: path.stat().st_mtime_ns for path in output_paths if path is not None and path.is_file()}
    monkeypatch.setattr(OpsLedger, "append_if_payload_digest", original)
    second = execute_journal(cfg, preview)
    assert second.status == "generated"
    assert second.pending_updates == ()
    assert mtimes == {path: path.stat().st_mtime_ns for path in output_paths if path is not None and path.is_file()}
    assert execute_journal(cfg, preview).status == "already_generated"


def test_receipt_write_failure_after_jsonl_outputs_is_retryable(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.ops_db_path = None
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    receipt = next(item.path for item in preview.planned_updates if item.target == "runlog")
    assert receipt is not None
    from kaizenlog.journal_service import _write_file_cas
    original = _write_file_cas
    monkeypatch.setattr(
        "kaizenlog.journal_service._write_file_cas",
        lambda path, expected, new: (_ for _ in ()).throw(OSError("receipt fixture failure")) if path == receipt else original(path, expected, new),
    )
    first = execute_journal(cfg, preview)
    assert first.status == "partial"
    assert {item.target for item in first.applied_updates} >= {"target_day", "next_day", "stats"}
    assert {item.target for item in first.pending_updates} == {"runlog"}
    output_paths = [item.path for item in preview.planned_updates if item.target in {"target_day", "next_day", "stats"}]
    mtimes = {path: path.stat().st_mtime_ns for path in output_paths if path is not None and path.is_file()}
    monkeypatch.setattr("kaizenlog.journal_service._write_file_cas", original)
    second = execute_journal(cfg, preview)
    assert second.status == "generated"
    assert second.pending_updates == ()
    assert mtimes == {path: path.stat().st_mtime_ns for path in output_paths if path is not None and path.is_file()}
    assert execute_journal(cfg, preview).status == "already_generated"


def test_completed_preview_human_input_mutation_is_conflict_without_writes(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    assert execute_journal(cfg, preview).status == "generated"
    human_path = cfg.vault_dir / ".kaizenlog" / "human" / "2026-01-15.json"
    human_path.parent.mkdir(parents=True, exist_ok=True)
    human_path.write_text('{"revision":1}', encoding="utf-8")
    result = execute_journal(cfg, preview)
    assert result.status == "conflict"
    assert result.applied_updates == ()


def test_completed_preview_config_mutation_is_conflict_without_writes(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    assert execute_journal(cfg, preview).status == "generated"
    cfg.session_gap_minutes += 1.0
    result = execute_journal(cfg, preview)
    assert result.status == "conflict"
    assert result.applied_updates == ()


def test_setup_save_rejects_config_path_change_without_writes(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    opts = SetupOptions(config_path=tmp_path / "config.toml", vault=vault)
    preview = preview_initial_setup(
        opts, daily_notes_dir="", daily_note_format="YYYY-MM-DD", backend="none", model="fixture"
    )
    assert preview.status == "ready"
    assert not list(tmp_path.iterdir()) or list(tmp_path.iterdir()) == [vault]
    opts.config_path = tmp_path / "other.toml"
    result = save_initial_setup(
        opts, preview, daily_notes_dir="", daily_note_format="YYYY-MM-DD", backend="none", model="fixture"
    )
    assert result.status == "conflict"
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "other.toml").exists()


def test_setup_public_preview_save_is_root_only_and_preserves_empty_directory(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    config_path = tmp_path / "config.toml"
    opts = SetupOptions(config_path=config_path, vault=vault)
    preview = preview_initial_setup(
        opts, daily_notes_dir="", daily_note_format="YYYY-MM-DD", backend="none", model="fixture"
    )
    assert preview.status == "ready"
    assert sorted(tmp_path.iterdir()) == [vault]
    result = save_initial_setup(
        opts, preview, daily_notes_dir="", daily_note_format="YYYY-MM-DD", backend="none", model="fixture"
    )
    assert result.status == "saved"
    assert config_path.is_file()
    assert not (vault / "01 Daily Notes").exists()
    assert not (vault / ".kaizenlog").exists()
    assert 'daily_notes_dir = ""' in config_path.read_text(encoding="utf-8")


def test_open_existing_note_uses_adapter_without_provider_or_generation(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    note = cfg.daily_notes_path / "2026-01-15.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Existing", encoding="utf-8")
    opened: list[Path] = []
    cfg.open_adapter = opened.append
    monkeypatch.setattr("kaizenlog.journal_service.ActivityWatchClient", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider called")))
    result = open_daily_note(cfg, date(2026, 1, 15))
    assert result.status == "opened"
    assert opened == [note]


def test_report_read_failure_is_typed_unavailable(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        "kaizenlog.journal_service._note_bytes",
        lambda *args, **kwargs: (None, None, "PermissionError"),
    )
    preview = preview_report_no_llm(cfg, date(2026, 1, 15))
    assert preview.status == "unavailable"
    assert preview.reason == "PermissionError"


def test_report_snapshot_freezes_handwritten_intent_and_integrity(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.daily_notes_path.mkdir(parents=True)
    cfg.stats_path.mkdir(parents=True)
    (cfg.daily_notes_path / "2026-01-15.md").write_text(
        "---\ndate: 2026-01-15\n---\n\n## Today's Focus\n- finish the fixture\n",
        encoding="utf-8",
    )
    (cfg.stats_path / "2026-01-15.json").write_text(
        json.dumps({"day": "2026-01-15", "total_minutes": 10.0, "blocks": []}),
        encoding="utf-8",
    )
    preview = preview_report_no_llm(cfg, date(2026, 1, 15))
    assert preview.status == "ready"
    assert preview.snapshot.prepared_intent == "## Today's Focus\n- finish the fixture"
    cfg.session_gap_minutes += 1.0
    result = execute_report_no_llm(cfg, preview)
    assert result.status == "conflict"


def test_execute_returns_partial_on_second_write_failure(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    calls = []

    def fail_second(self, day, marker, body, **kwargs):
        calls.append(marker)
        if len(calls) == 2:
            raise OSError("fixture second write failure")
        path = Path(self.cfg.vault_dir) / "notes" / f"{day.isoformat()}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    monkeypatch.setattr("kaizenlog.journal_service._write_note_cas", lambda cfg, update, day, body: fail_second(DailyNoteStore(cfg), day, update.managed_region, body))
    result = execute_journal(cfg, preview)
    assert result.status == "partial"
    assert len(result.applied_updates) == 1
    assert len(result.pending_updates) >= 1


def test_execute_retries_partial_preview_by_skipping_exact_after_units(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    original = _write_note_cas
    calls = []

    def fail_once(cfg_arg, update, day, body):
        calls.append(update.target)
        if len(calls) == 2:
            raise OSError("fixture second write failure")
        return original(cfg_arg, update, day, body)

    monkeypatch.setattr("kaizenlog.journal_service._write_note_cas", fail_once)
    first = execute_journal(cfg, preview)
    assert first.status == "partial"
    monkeypatch.setattr("kaizenlog.journal_service._write_note_cas", original)
    second = execute_journal(cfg, preview)
    assert second.status == "generated"
    assert second.pending_updates == ()


def test_unavailable_and_no_records_are_not_generated(tmp_path: Path):
    preview = preview_journal(_cfg(tmp_path), date(2026, 1, 15))
    assert preview.status in {"unavailable", "no_records"}
    if preview.status == "unavailable":
        result = execute_journal(_cfg(tmp_path), preview)
        assert result.status == "unavailable"
    assert not (tmp_path / "notes").exists()


def test_execute_rejects_changed_preconditions_without_writes(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    note = tmp_path / "notes" / "2026-01-15.md"
    note.parent.mkdir()
    note.write_text("before", encoding="utf-8")
    _fixture_source(monkeypatch, cfg)
    preview = preview_journal(cfg, date(2026, 1, 15))
    note.write_text("changed", encoding="utf-8")
    result = execute_journal(cfg, preview)
    assert result.status == "conflict"
    assert result.applied_updates == ()
    assert note.read_text(encoding="utf-8") == "changed"


def test_retention_delete_is_explicit(tmp_path: Path):
    preview = preview_journal(_cfg(tmp_path), date(2026, 1, 15))
    assert not [u for u in preview.planned_updates if u.action == "delete"]
