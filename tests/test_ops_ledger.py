"""Operational SQLite ledger contracts."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

from kaizenlog.config import Config, load_config
from kaizenlog.ops_ledger import OpsLedger, bind_run, default_ops_db_path
from kaizenlog.reliability import FailureReason
from kaizenlog.runlog import load_operational_runs, log_advise_health, log_run


def _entry(run_id: str, *, ts: str = "2026-09-01T00:00:00+00:00") -> dict:
    return {
        "schema_version": 2,
        "run_id": run_id,
        "ts": ts,
        "command": "run",
        "ok": True,
        "duration_seconds": 1.0,
    }


def test_ops_ledger_initializes_v1_schema_and_returns_payload(tmp_path):
    """Dropping WAL/schema setup or payload decoding must break this contract."""
    path = tmp_path / "ops.sqlite3"
    ledger = OpsLedger(path)

    ledger.append(_entry("run-1"))

    assert ledger.load_runs() == [_entry("run-1")]
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_concurrent_appends_do_not_drop_rows(tmp_path):
    """Replacing append-safe writes with unlocked writes must lose or fail rows."""
    path = tmp_path / "ops.sqlite3"

    def write(index: int) -> None:
        OpsLedger(path).append(
            _entry(f"run-{index}", ts=f"2026-09-01T00:00:{index:02d}+00:00")
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))

    assert len(OpsLedger(path).load_runs()) == 40


def test_load_operational_runs_falls_back_to_legacy_jsonl(tmp_path):
    """A missing or empty ledger must not hide the compatibility JSONL history."""
    log_run(tmp_path, "run", ok=True, duration_seconds=1.0)
    cfg = Config(vault_dir=tmp_path, logs_dir=".", ops_db_path=tmp_path / "missing.sqlite3")

    assert load_operational_runs(cfg)[-1]["command"] == "run"


def test_log_run_keeps_jsonl_when_ledger_write_fails(tmp_path, monkeypatch):
    """A SQLite outage must not replace the command's JSONL outcome."""
    path = tmp_path / "ops.sqlite3"

    def fail_append(self, entry):
        raise OSError("disk full")

    monkeypatch.setattr(OpsLedger, "append", fail_append)

    failure = log_run(
        tmp_path,
        "run",
        ok=True,
        duration_seconds=1.0,
        ops_db_path=path,
    )

    assert failure is FailureReason.LEDGER_WRITE_FAILED
    assert load_operational_runs(
        Config(vault_dir=tmp_path, logs_dir=".", ops_db_path=path)
    )[-1]["command"] == "run"


def test_advice_health_inherits_bound_parent_run_id(tmp_path):
    """Removing context correlation must leave a health phase orphaned."""
    path = tmp_path / "ops.sqlite3"
    with bind_run("top-level-run"):
        log_advise_health(
            tmp_path,
            day="2026-09-01",
            backend="claude-code-cli",
            configured_backend="claude-code-cli",
            actual_backend=None,
            outcome="ok",
            duration_seconds=0.1,
            ops_db_path=path,
        )

    row = OpsLedger(path).load_runs()[0]
    assert row["parent_run_id"] == "top-level-run"
    assert row["actual_backend"] is None


def test_load_operational_runs_prefers_nonempty_ledger(tmp_path):
    """A populated local ledger must win over stale compatibility JSONL."""
    log_run(tmp_path, "run", ok=True, duration_seconds=1.0)
    path = tmp_path / "ops.sqlite3"
    OpsLedger(path).append(_entry("ledger-run"))
    cfg = Config(vault_dir=tmp_path, logs_dir=".", ops_db_path=path)

    assert [row["run_id"] for row in load_operational_runs(cfg)] == ["ledger-run"]


def test_ops_ledger_drops_raw_input_events_from_payload(tmp_path):
    """A privacy regression that persists raw input events must be caught."""
    path = tmp_path / "ops.sqlite3"
    entry = _entry("run-with-quality")
    entry["source_quality"] = {
        "input": {"state": "observed", "last_event_at": "2026-09-01T00:00:00+00:00"},
        "events": [{"text": "private keystroke"}],
    }

    OpsLedger(path).append(entry)

    stored = OpsLedger(path).load_runs()[0]
    assert stored["source_quality"]["input"]["state"] == "observed"
    assert "events" not in stored["source_quality"]


def test_config_only_enables_default_ledger_after_file_backed_load(tmp_path, monkeypatch):
    """A bare Config/import must not create or opt into a machine profile ledger."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAIZENLOG_CONFIG", raising=False)
    monkeypatch.setattr("kaizenlog.config.sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    assert Config().operational_db_path is None
    assert not (tmp_path / "local").exists()
    (tmp_path / "kaizenlog.toml").write_text("[general]\n", encoding="utf-8")

    cfg = load_config()

    assert cfg.operational_db_path == default_ops_db_path()
    assert not (tmp_path / "local").exists()
