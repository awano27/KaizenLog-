from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from kaizenlog.config import Config
from kaizenlog.human_input import HumanOperation, apply_human_operation
from kaizenlog.memory import MemoryEntry, load_entries
import kaizenlog.home_service as home_service


DAY = date(2026, 10, 15)


def cfg(tmp_path: Path) -> Config:
    return Config(vault_dir=tmp_path, memory_dir="Memory")


def tree_digest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def tree_state(root: Path) -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        stat = path.stat()
        result[str(path.relative_to(root))] = (
            stat.st_mtime_ns,
            stat.st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return result


def test_load_missing_is_read_only_and_keeps_unknown_states(tmp_path: Path):
    config = cfg(tmp_path)
    before = tree_state(tmp_path)
    snapshot = home_service.load_home_snapshot(config, DAY)
    assert snapshot.saved.status == "missing"
    assert snapshot.saved.content is None
    assert snapshot.stats_status == "missing"
    assert snapshot.human.revision == 0
    assert snapshot.measurements.status == "unknown"
    assert snapshot.recovery.source_status is None
    assert tree_state(tmp_path) == before


def test_conflict_resolution_does_not_fallback(monkeypatch, tmp_path: Path):
    config = cfg(tmp_path)
    resolution = home_service.DailyNoteResolution(
        DAY, None, "conflict", "none", (), "multiple daily notes"
    )
    monkeypatch.setattr(home_service, "resolve_daily_note", lambda _cfg, _day: resolution)
    snapshot = home_service.load_home_snapshot(config, DAY)
    assert snapshot.saved.status == "conflict"
    assert snapshot.saved.path is None
    assert snapshot.saved.content is None


def test_invalid_stats_are_unavailable_and_not_zero(tmp_path: Path):
    config = cfg(tmp_path)
    path = config.stats_path / f"{DAY.isoformat()}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    snapshot = home_service.load_home_snapshot(config, DAY)
    assert snapshot.stats_status == "unavailable"


def test_next_action_free_text_and_unique_kzn_are_distinct(tmp_path: Path):
    config = cfg(tmp_path)
    config.memory_path.mkdir(parents=True)
    entry = MemoryEntry("KZN-20261015-001", DAY.isoformat(), "既存アクション")
    (config.memory_path / "suggestions.jsonl").write_text(
        json.dumps(asdict(entry), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    apply_human_operation(
        config, DAY,
        HumanOperation("free", DAY, "next_action", {"text": "自由文", "source": "free_text", "source_id": None}, None, None),
        expected_revision=0,
    )
    apply_human_operation(
        config, DAY,
        HumanOperation("kzn", DAY, "next_action", {"text": "KZN", "source": "kzn", "source_id": entry.id}, None, None),
        expected_revision=1,
    )
    snapshot = home_service.load_home_snapshot(config, DAY)
    assert len(snapshot.next_actions) == 1
    action = snapshot.next_actions[0]
    assert action.source == "kzn"
    assert action.completable is True
    assert action.selectable is True
    assert action.entry_digest == home_service.memory_entry_digest(entry)


def test_missing_human_still_lists_memory_candidates_without_writing(tmp_path: Path):
    config = cfg(tmp_path)
    config.memory_path.mkdir(parents=True)
    entry = MemoryEntry("KZN-20261015-001", DAY.isoformat(), "候補アクション")
    (config.memory_path / "suggestions.jsonl").write_text(
        json.dumps(asdict(entry), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    before = tree_state(tmp_path)
    snapshot = home_service.load_home_snapshot(config, DAY)
    assert [(a.source_id, a.selectable, a.completable) for a in snapshot.next_actions] == [
        (entry.id, True, False)
    ]
    assert tree_state(tmp_path) == before


def test_selected_free_text_keeps_proposed_candidates_visible(tmp_path: Path):
    config = cfg(tmp_path)
    config.memory_path.mkdir(parents=True)
    entry = MemoryEntry("KZN-20261015-001", DAY.isoformat(), "候補アクション")
    (config.memory_path / "suggestions.jsonl").write_text(
        json.dumps(asdict(entry), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    free = HumanOperation(
        "free", DAY, "next_action",
        {"text": "自由文", "source": "free_text", "source_id": None}, None, None,
    )
    assert apply_human_operation(config, DAY, free, expected_revision=0).status == "saved"
    snapshot = home_service.load_home_snapshot(config, DAY)
    assert snapshot.next_actions[0].source == "free_text"
    assert snapshot.next_actions[0].completable is False
    assert snapshot.next_actions[1].source_id == entry.id
    assert snapshot.next_actions[1].completable is False


def test_human_delegates_preserve_status(monkeypatch, tmp_path: Path):
    config = cfg(tmp_path)
    operation = HumanOperation("op", DAY, "outcome", {"text": "結果"}, None, None)
    expected = home_service.HumanSaveResult("stale", DAY, "op", 3, tmp_path / "x", None, "stale")
    monkeypatch.setattr(home_service._human_input, "apply_human_operation", lambda *a, **k: expected)
    assert home_service.apply_human_operation(config, DAY, operation, expected_revision=2) is expected

    projection = home_service.ProjectionResult("pending", DAY, 3, tmp_path / "x", None, None, "pending")
    monkeypatch.setattr(home_service._human_input, "project_human_day", lambda *a, **k: projection)
    assert home_service.project_human_day(config, DAY, expected_revision=3) is projection


def test_refresh_clips_today_and_keeps_missing_input_separate(monkeypatch, tmp_path: Path):
    config = cfg(tmp_path)
    now = datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc)

    class FakeClient:
        pass

    event = home_service.ActivityEvent(
        datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 15, 11, 0, tzinfo=timezone.utc),
        "Code.exe", "main.py"
    )
    monkeypatch.setattr(home_service, "collect_day", lambda *_: ([event], True))
    monkeypatch.setattr(
        home_service, "collect_input_observation",
        lambda *_: home_service.InputObservation(
            home_service.QualityState.MISSING, [], None,
            home_service.FailureReason.INPUT_BUCKET_MISSING
        ),
    )
    snapshot = home_service.refresh_aw_snapshot(config, DAY, now=now, client=FakeClient())
    assert snapshot.status == "observed"
    assert snapshot.observed["active_minutes"] == 60.0
    assert snapshot.observed["input_status"] == "missing"
    assert "keypresses" not in snapshot.observed
    assert snapshot.refreshed_at is not None and snapshot.refreshed_at.tzinfo is not None


@pytest.mark.parametrize("afk_available", [True, False])
def test_empty_aw_events_never_become_activity_zero(monkeypatch, tmp_path: Path, afk_available: bool):
    config = cfg(tmp_path)
    monkeypatch.setattr(home_service, "collect_day", lambda *_: ([], afk_available))
    monkeypatch.setattr(
        home_service, "collect_input_observation",
        lambda *_: home_service.InputObservation(
            home_service.QualityState.MISSING, [], None,
            home_service.FailureReason.INPUT_BUCKET_MISSING
        ),
    )
    snapshot = home_service.refresh_aw_snapshot(
        config, DAY, now=datetime(2026, 10, 15, 13, tzinfo=timezone.utc), client=object()
    )
    assert snapshot.status == "missing"
    assert snapshot.observed["activity_status"] == "missing"
    assert "active_minutes" not in snapshot.observed
    assert "context_switches" not in snapshot.observed


def test_observed_input_can_stand_without_window_events(monkeypatch, tmp_path: Path):
    config = cfg(tmp_path)
    monkeypatch.setattr(home_service, "collect_day", lambda *_: ([], True))
    raw = [{
        "timestamp": "2026-10-15T10:00:00+00:00", "duration": 60,
        "data": {"presses": 2, "clicks": 1},
    }]
    monkeypatch.setattr(
        home_service, "collect_input_observation",
        lambda *_: home_service.InputObservation(
            home_service.QualityState.OBSERVED, raw, "input", home_service.FailureReason.NONE,
        ),
    )
    snapshot = home_service.refresh_aw_snapshot(
        config, DAY, now=datetime(2026, 10, 15, 13, tzinfo=timezone.utc), client=object()
    )
    assert snapshot.status == "observed"
    assert snapshot.observed["activity_status"] == "missing"
    assert snapshot.observed["keypresses"] == 2


def test_malformed_observed_input_is_unavailable_without_losing_activity(monkeypatch, tmp_path: Path):
    config = cfg(tmp_path)
    window = home_service.ActivityEvent(
        datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 15, 11, 0, tzinfo=timezone.utc),
        "Code.exe", "main.py",
    )
    malformed = [{
        "timestamp": "not-a-timestamp", "duration": 60,
        "data": {"presses": 2, "clicks": 1},
    }]
    monkeypatch.setattr(home_service, "collect_day", lambda *_: ([window], True))
    monkeypatch.setattr(
        home_service, "collect_input_observation",
        lambda *_: home_service.InputObservation(
            home_service.QualityState.OBSERVED, malformed, "input", home_service.FailureReason.NONE,
        ),
    )
    before = tree_state(tmp_path)
    snapshot = home_service.refresh_aw_snapshot(
        config, DAY, now=datetime(2026, 10, 15, 13, tzinfo=timezone.utc), client=object()
    )
    assert snapshot.status == "observed"
    assert snapshot.observed["activity_status"] == "observed"
    assert snapshot.observed["active_minutes"] == 60.0
    assert snapshot.observed["input_status"] == "unavailable"
    assert snapshot.reason == "ValueError"
    assert "keypresses" not in snapshot.observed
    assert tree_state(tmp_path) == before


def test_correction_targets_reject_duplicates_and_keep_source_labels(tmp_path: Path):
    config = cfg(tmp_path)
    path = config.stats_path / f"{DAY.isoformat()}.json"
    path.parent.mkdir(parents=True)
    block = {
        "start": "2026-10-15T00:00:00+00:00", "end": "2026-10-15T01:00:00+00:00",
        "app": "Code.exe", "title": "main.py", "category": "開発", "project": "P1",
    }
    path.write_text(json.dumps({"day": DAY.isoformat(), "blocks": [block]}), encoding="utf-8")
    targets = home_service.load_correction_targets(config, DAY)
    assert len(targets) == 1
    assert targets[0].source_category == "開発"
    assert targets[0].original_project == "P1"
    assert targets[0].privacy is not None


def test_correction_targets_reject_duplicate_stable_keys(tmp_path: Path):
    config = cfg(tmp_path)
    path = config.stats_path / f"{DAY.isoformat()}.json"
    path.parent.mkdir(parents=True)
    block = {
        "start": "2026-10-15T00:00:00+00:00", "end": "2026-10-15T01:00:00+00:00",
        "app": "Code.exe", "title": "main.py", "category": "開発",
    }
    path.write_text(json.dumps({"day": DAY.isoformat(), "blocks": [block, block]}), encoding="utf-8")
    assert home_service.load_correction_targets(config, DAY) == ()


def test_correction_targets_reject_non_positive_interval(tmp_path: Path):
    config = cfg(tmp_path)
    path = config.stats_path / f"{DAY.isoformat()}.json"
    path.parent.mkdir(parents=True)
    block = {
        "start": "2026-10-15T01:00:00+00:00", "end": "2026-10-15T01:00:00+00:00",
        "app": "Code.exe", "title": "main.py", "category": "開発",
    }
    path.write_text(json.dumps({"day": DAY.isoformat(), "blocks": [block]}), encoding="utf-8")
    assert home_service.load_correction_targets(config, DAY) == ()
    assert home_service._stats_status(config, DAY, None) == "unavailable"


def test_future_aw_interval_has_no_fake_zero(monkeypatch, tmp_path: Path):
    called = False

    def should_not_collect(*_args):
        nonlocal called
        called = True
        raise AssertionError("future interval must not be collected")

    monkeypatch.setattr(home_service, "collect_day", should_not_collect)
    snapshot = home_service.refresh_aw_snapshot(
        cfg(tmp_path), DAY, now=datetime(2026, 10, 14, 12, tzinfo=timezone.utc), client=object()
    )
    assert snapshot.status == "unknown"
    assert snapshot.observed == {}
    assert called is False


def test_explicit_runlog_recovery_status_is_not_aliased(monkeypatch, tmp_path: Path):
    config = cfg(tmp_path)
    monkeypatch.setattr(
        home_service, "load_runs",
        lambda _path: [{"date": DAY.isoformat(), "command": "recovery", "recovery_status": "partial", "source_status": "pending"}],
    )
    snapshot = home_service.load_home_snapshot(config, DAY)
    assert snapshot.recovery.status == "partial"
    assert snapshot.recovery.source_status == "pending"


def test_saved_human_pending_recovery_keeps_source_status_and_message(tmp_path: Path):
    result = home_service.HumanSaveResult(
        "saved", DAY, "op-1", 1, tmp_path / "human.json", "pending", None
    )
    recovery = home_service.recovery_snapshot_from_result(result)
    assert recovery.status == "pending"
    assert recovery.source_status == "saved"
    assert recovery.message == "本人入力は保存済み／日誌反映は未完了"


def _kzn_fixture(tmp_path: Path, *, selected: str = "KZN-20261015-001", free_text: bool = False):
    config = cfg(tmp_path)
    config.memory_path.mkdir(parents=True)
    entries = [
        MemoryEntry("KZN-20261015-001", DAY.isoformat(), "選択アクション"),
        MemoryEntry("KZN-20261015-002", DAY.isoformat(), "未選択アクション"),
    ]
    (config.memory_path / "suggestions.jsonl").write_text(
        "".join(json.dumps(asdict(entry), ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )
    payload = (
        {"text": "自由文", "source": "free_text", "source_id": None}
        if free_text else
        {"text": "選択アクション", "source": "kzn", "source_id": selected}
    )
    operation = HumanOperation("select-1", DAY, "next_action", payload, None, None)
    assert apply_human_operation(config, DAY, operation, expected_revision=0).status == "saved"
    return config, entries


def test_complete_kzn_action_commits_selected_entry_and_syncs_note(monkeypatch, tmp_path: Path):
    config, entries = _kzn_fixture(tmp_path)
    sync = home_service.KznNoteSyncResult("synced", None)
    monkeypatch.setattr(home_service, "sync_kzn_completion_note", lambda *_: sync)
    result = home_service.complete_kzn_action(
        config, entries[0].id, DAY, expected_revision=1,
        expected_action_digest=home_service.memory_entry_digest(entries[0]),
    )
    assert result.status == "saved"
    assert result.entry_id == entries[0].id
    assert result.memory_status == "done"
    assert result.note_status == "synced"
    assert load_entries(config.memory_path)[0].status == "done"


def test_complete_kzn_uses_actual_journal_renderer_with_existing_note(tmp_path: Path):
    config, entries = _kzn_fixture(tmp_path)
    prior = MemoryEntry(entries[1].id, "2026-10-14", entries[1].action)
    (config.memory_path / "suggestions.jsonl").write_text(
        json.dumps(asdict(entries[0]), ensure_ascii=False) + "\n" +
        json.dumps(asdict(prior), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    config.daily_notes_path.mkdir(parents=True)
    note = config.daily_notes_path / f"{DAY.isoformat()}.md"
    note.write_text(f"# {DAY.isoformat()}\n\n本文\n", encoding="utf-8")
    result = home_service.complete_kzn_action(
        config, entries[0].id, DAY, expected_revision=1,
        expected_action_digest=home_service.memory_entry_digest(entries[0]),
    )
    assert result.status == "saved"
    assert result.note_status == "synced"
    assert "今日やること" in note.read_text(encoding="utf-8")


@pytest.mark.parametrize("action_id", ["001", "", "KZN-20261015-999"])
def test_complete_kzn_rejects_non_full_missing_or_unselected_ids(tmp_path: Path, action_id: str):
    config, entries = _kzn_fixture(tmp_path)
    result = home_service.complete_kzn_action(
        config, action_id, DAY, expected_revision=1,
        expected_action_digest=home_service.memory_entry_digest(entries[0]),
    )
    assert result.status == "invalid"
    assert load_entries(config.memory_path)[0].status == "proposed"


def test_complete_kzn_rejects_free_text_and_unselected_candidate(tmp_path: Path):
    config, entries = _kzn_fixture(tmp_path, free_text=True)
    result = home_service.complete_kzn_action(
        config, entries[0].id, DAY, expected_revision=1,
        expected_action_digest=home_service.memory_entry_digest(entries[0]),
    )
    assert result.status == "invalid"
    config2, entries2 = _kzn_fixture(tmp_path / "other")
    result2 = home_service.complete_kzn_action(
        config2, entries2[1].id, DAY, expected_revision=1,
        expected_action_digest=home_service.memory_entry_digest(entries2[1]),
    )
    assert result2.status == "invalid"


def test_complete_kzn_rejects_stale_revision_and_digest(tmp_path: Path):
    config, entries = _kzn_fixture(tmp_path)
    stale = home_service.complete_kzn_action(
        config, entries[0].id, DAY, expected_revision=0,
        expected_action_digest=home_service.memory_entry_digest(entries[0]),
    )
    assert stale.status == "stale"
    conflict = home_service.complete_kzn_action(
        config, entries[0].id, DAY, expected_revision=1,
        expected_action_digest="0" * 64,
    )
    assert conflict.status == "conflict"


def test_complete_kzn_done_retry_preserves_date_and_does_not_append(monkeypatch, tmp_path: Path):
    config, entries = _kzn_fixture(tmp_path)
    monkeypatch.setattr(home_service, "sync_kzn_completion_note", lambda *_: home_service.KznNoteSyncResult("not_requested", "no open actions"))
    digest = home_service.memory_entry_digest(entries[0])
    first = home_service.complete_kzn_action(config, entries[0].id, DAY, expected_revision=1, expected_action_digest=digest)
    done_date = load_entries(config.memory_path)[0].done_date
    raw_lines = (config.memory_path / "suggestions.jsonl").read_text(encoding="utf-8").splitlines()
    second = home_service.complete_kzn_action(config, entries[0].id, DAY, expected_revision=1, expected_action_digest=digest)
    assert first.status == "saved"
    assert second.status == "idempotent"
    assert load_entries(config.memory_path)[0].done_date == done_date
    assert len((config.memory_path / "suggestions.jsonl").read_text(encoding="utf-8").splitlines()) == len(raw_lines)


def test_complete_kzn_note_failure_is_partial_after_memory_commit(monkeypatch, tmp_path: Path):
    config, entries = _kzn_fixture(tmp_path)
    monkeypatch.setattr(home_service, "sync_kzn_completion_note", lambda *_: home_service.KznNoteSyncResult("failed", "note write failed"))
    result = home_service.complete_kzn_action(
        config, entries[0].id, DAY, expected_revision=1,
        expected_action_digest=home_service.memory_entry_digest(entries[0]),
    )
    assert result.status == "partial_failure"
    assert result.memory_status == "done"
    assert result.note_status == "failed"
    assert load_entries(config.memory_path)[0].status == "done"
