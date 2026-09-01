"""Machine-local, append-safe operational run ledger."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping


@dataclass
class OperationalRunContext:
    """Scoped correlation data for one command invocation."""

    run_id: str
    source_quality: dict | None = None


_RUN_CONTEXT: ContextVar[OperationalRunContext | None] = ContextVar(
    "kaizenlog_run_context", default=None
)
_DROP_PAYLOAD_KEYS = frozenset({"events", "raw_events", "input_events"})


def default_ops_db_path() -> Path:
    """Return the local ledger path without creating a profile directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "kaizenlog" / "ops.sqlite3"
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / "kaizenlog" / "ops.sqlite3"


def new_run_id() -> str:
    return uuid.uuid4().hex


def new_run_context(run_id: str | None = None) -> OperationalRunContext:
    return OperationalRunContext(run_id=run_id or new_run_id())


def current_run_id() -> str | None:
    context = _RUN_CONTEXT.get()
    return context.run_id if context is not None else None


def _quality_code(value: object) -> str:
    return str(getattr(value, "value", value))


def _canonical_source_quality(source_quality: Mapping[str, object]) -> dict:
    """Keep the Task 3 input contract without raw watcher events."""
    input_quality = source_quality.get("input")
    if not isinstance(input_quality, Mapping):
        return {}
    last_event_at = input_quality.get("last_event_at")
    return {
        "input": {
            "state": _quality_code(input_quality.get("state", "unknown")),
            "reason": _quality_code(input_quality.get("reason", "unknown")),
            "bucket_id": str(input_quality.get("bucket_id") or ""),
            "last_event_at": str(last_event_at) if last_event_at is not None else None,
        }
    }


def record_run_source_quality(source_quality: Mapping[str, object]) -> None:
    """Attach canonical source quality to the active context, if one exists."""
    context = _RUN_CONTEXT.get()
    if context is not None:
        context.source_quality = _canonical_source_quality(source_quality)


def current_run_source_quality() -> dict | None:
    context = _RUN_CONTEXT.get()
    if context is None or context.source_quality is None:
        return None
    return json.loads(json.dumps(context.source_quality))


@contextmanager
def bind_run(run: str | OperationalRunContext) -> Iterator[None]:
    """Bind a top-level run id for correlated nested operational rows."""
    context = run if isinstance(run, OperationalRunContext) else new_run_context(str(run))
    token = _RUN_CONTEXT.set(context)
    try:
        yield
    finally:
        _RUN_CONTEXT.reset(token)


def _payload(entry: dict) -> dict:
    """Keep only JSON-safe operational values; never store raw input events."""
    def clean(value):
        if isinstance(value, dict):
            return {
                str(key): clean(item)
                for key, item in value.items()
                if str(key) not in _DROP_PAYLOAD_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    return clean(dict(entry))


class OpsLedger:
    """SQLite operational ledger with a durable normalized payload."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 5.0
        while True:
            con = sqlite3.connect(self.path, timeout=5.0)
            try:
                con.execute("PRAGMA busy_timeout=5000")
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA user_version=1")
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY,
                        parent_run_id TEXT,
                        ts TEXT NOT NULL,
                        command TEXT NOT NULL,
                        ok INTEGER NOT NULL,
                        partial INTEGER NOT NULL DEFAULT 0,
                        duration_seconds REAL NOT NULL,
                        configured_backend TEXT,
                        actual_backend TEXT,
                        outcome TEXT,
                        reason_code TEXT,
                        notify_failed INTEGER NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                con.execute("CREATE INDEX IF NOT EXISTS idx_runs_command_ts ON runs(command, ts)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id)")
                return con
            except sqlite3.OperationalError:
                con.close()
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def append(self, entry: dict) -> None:
        payload = _payload(entry)
        run_id = str(payload.get("run_id") or new_run_id())
        payload["run_id"] = run_id
        reason_codes = payload.get("reason_codes") or []
        reason_code = str(reason_codes[0]) if reason_codes else payload.get("reason_code")
        with self._connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO runs (
                    run_id, parent_run_id, ts, command, ok, partial, duration_seconds,
                    configured_backend, actual_backend, outcome, reason_code,
                    notify_failed, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    payload.get("parent_run_id"),
                    str(payload["ts"]),
                    str(payload["command"]),
                    int(bool(payload.get("ok", False))),
                    int(bool(payload.get("partial", False))),
                    float(payload.get("duration_seconds", 0.0)),
                    payload.get("configured_backend"),
                    payload.get("actual_backend"),
                    payload.get("outcome"),
                    reason_code,
                    int(bool(payload.get("notify_failed", False))),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def load_runs(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            with self._connect() as con:
                rows = con.execute("SELECT payload_json FROM runs ORDER BY ts ASC").fetchall()
        except sqlite3.Error:
            return []
        runs: list[dict] = []
        for (payload_json,) in rows:
            try:
                entry = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(entry, dict) and "ts" in entry:
                runs.append(entry)
        return runs
