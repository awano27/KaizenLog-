from __future__ import annotations

import sqlite3
from pathlib import Path

from kaizenlog.ops_ledger import OpsLedger


def _entry(run_id: str) -> dict:
    return {
        "run_id": run_id, "ts": "2026-01-15T00:00:00+00:00",
        "command": "journal", "ok": True, "duration_seconds": 0.1,
    }


def test_readonly_snapshot_missing_does_not_create_tree(tmp_path: Path):
    path = tmp_path / "nested" / "ops.sqlite3"
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    result = OpsLedger(path).readonly_snapshot()
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert result.available is False
    assert result.rows == ()
    assert before == after


def test_readonly_snapshot_does_not_create_wal_or_shm(tmp_path: Path):
    path = tmp_path / "ops.sqlite3"
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, ts TEXT, payload_json TEXT)"
        )
        con.execute(
            "INSERT INTO runs VALUES (?, ?, ?)",
            ("one", "2026-01-15T00:00:00+00:00", '{"run_id":"one"}'),
        )
    before = {p.name: p.stat().st_size for p in tmp_path.iterdir()}
    result = OpsLedger(path).readonly_snapshot()
    after = {p.name: p.stat().st_size for p in tmp_path.iterdir()}
    assert result.available is True
    assert result.rows[0]["run_id"] == "one"
    assert after == before


def test_readonly_snapshot_rejects_active_rollback_journal(tmp_path: Path):
    path = tmp_path / "ops.sqlite3"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, ts TEXT, payload_json TEXT)")
        con.execute("INSERT INTO runs VALUES (?, ?, ?)", ("one", "2026-01-15", '{"run_id":"one"}'))
    rollback = Path(f"{path}-journal")
    rollback.write_bytes(b"active rollback journal")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = OpsLedger(path).readonly_snapshot()
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert result.available is False
    assert "rollback journal" in (result.reason or "")
    assert after == before


def test_conditional_append_distinguishes_missing_and_changed(tmp_path: Path):
    path = tmp_path / "ops.sqlite3"
    ledger = OpsLedger(path)
    assert ledger.append_if_payload_digest(_entry("one"), expected_digest=None, expected_exists=False)
    with sqlite3.connect(path) as con:
        import hashlib
        raw = con.execute("SELECT payload_json FROM runs WHERE run_id='one'").fetchone()[0]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert not ledger.append_if_payload_digest(_entry("one"), expected_digest="bad", expected_exists=True)
    assert ledger.append_if_payload_digest(_entry("two"), expected_digest=None, expected_exists=False)
    with sqlite3.connect(path) as con:
        raw_after = con.execute("SELECT payload_json FROM runs WHERE run_id='one'").fetchone()[0]
    assert digest == hashlib.sha256(raw_after.encode("utf-8")).hexdigest()
