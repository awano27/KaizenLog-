"""Schema-gated reader for caller-captured Cursor CLI ``stream-json`` NDJSON.

Cursor's public output-format reference does not document a native transcript
location.  This adapter therefore has no default discovery path.  It reads an
explicitly configured directory of raw captures and requires a timestamp on
the event itself; file names and mtimes are never treated as event time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .aiwork import (
    AISession,
    UserPrompt,
    extract_session_title,
    finalize_session_io_digest,
    is_kaizenlog_internal_text,
)


def _timestamp_ms(value: object) -> datetime | None:
    """Accept only a positive integral epoch-millisecond event timestamp."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _text_content(record: dict, role: str) -> str | None:
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != role:
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    joined = "".join(parts).strip()
    return joined or None


def _project_name(cwd: object) -> str:
    if not isinstance(cwd, str) or not cwd.strip():
        return "cursor"
    cleaned = cwd.rstrip("/\\")
    if not cleaned:
        return "cursor"
    # Raw captures can originate on a different OS than the reader.
    return cleaned.replace("\\", "/").rsplit("/", 1)[-1] or "cursor"


@dataclass
class _CursorAccum:
    session_id: str
    project: str = "cursor"
    models: set[str] = field(default_factory=set)
    prompts: list[tuple[datetime, str]] = field(default_factory=list)
    assistant_events: list[tuple[datetime, int]] = field(default_factory=list)
    event_times: list[datetime] = field(default_factory=list)
    _seen_rows: set[str] = field(default_factory=set, repr=False)

    def note_row(self, record: dict) -> bool:
        """Return False for byte-equivalent duplicate captured events."""
        try:
            key = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return False
        if key in self._seen_rows:
            return False
        self._seen_rows.add(key)
        return True


class CursorAdapter:
    """Read raw, explicitly captured Cursor CLI ``--output-format stream-json``."""

    name = "cursor"

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = Path(sessions_dir)

    def _files(self) -> list[Path]:
        if not self.sessions_dir.is_dir():
            return []
        try:
            return sorted(
                (
                    path
                    for path in self.sessions_dir.rglob("*.jsonl")
                    if path.is_file()
                ),
                key=lambda path: path.as_posix(),
            )
        except OSError:
            return []

    def _read(self) -> dict[str, _CursorAccum]:
        accums: dict[str, _CursorAccum] = {}
        for path in self._files():
            try:
                with path.open(encoding="utf-8", errors="replace") as handle:
                    for raw_line in handle:
                        try:
                            record = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(record, dict):
                            continue
                        session_id = record.get("session_id")
                        if not isinstance(session_id, str) or not session_id:
                            continue
                        accum = accums.setdefault(session_id, _CursorAccum(session_id))
                        if not accum.note_row(record):
                            continue
                        if record.get("type") == "system" and record.get("subtype") == "init":
                            accum.project = _project_name(record.get("cwd"))
                            model = record.get("model")
                            if isinstance(model, str) and model.strip():
                                accum.models.add(model.strip())
                            continue
                        event_type = record.get("type")
                        if event_type not in ("user", "assistant"):
                            continue
                        timestamp = _timestamp_ms(record.get("timestamp_ms"))
                        if timestamp is None:
                            continue
                        accum.event_times.append(timestamp)
                        if event_type == "user":
                            text = _text_content(record, "user")
                            if text is not None:
                                accum.prompts.append((timestamp, text))
                        elif not isinstance(record.get("model_call_id"), str):
                            # The official reference marks this form as the
                            # pre-tool-call duplicate; skip it rather than
                            # inflating character counts.
                            text = _text_content(record, "assistant")
                            if text is not None:
                                accum.assistant_events.append((timestamp, len(text)))
            except OSError:
                continue
        return accums

    @staticmethod
    def _in_range(timestamp: datetime, start: datetime, end: datetime) -> bool:
        return start <= timestamp < end

    def scan_sessions(
        self, day_start: datetime, day_end: datetime
    ) -> list[AISession]:
        sessions: list[AISession] = []
        for accum in self._read().values():
            prompts = [
                item
                for item in accum.prompts
                if self._in_range(item[0], day_start, day_end)
            ]
            if not prompts:
                continue
            timestamps = [timestamp for timestamp, _ in prompts]
            timestamps.extend(
                timestamp
                for timestamp in accum.event_times
                if self._in_range(timestamp, day_start, day_end)
            )
            assistant_chars = sum(
                chars
                for timestamp, chars in accum.assistant_events
                if self._in_range(timestamp, day_start, day_end)
            )
            first = prompts[0][1]
            extracted = extract_session_title(first)
            title, first_len = extracted if extracted is not None else (None, 0)
            session = AISession(
                session_id=accum.session_id,
                project=accum.project,
                start=min(timestamps),
                end=max(timestamps),
                user_turns=len(prompts),
                assistant_chars=assistant_chars,
                models=set(accum.models),
                source="cursor",
                title=title,
                first_prompt_len=first_len,
                is_internal=is_kaizenlog_internal_text(first),
                tools_measurable=False,
                tool_metrics_measurable=False,
                output_tokens_measurable=False,
                output_tokens=0,
                assistant_chars_measurable=bool(
                    any(
                        self._in_range(timestamp, day_start, day_end)
                        for timestamp, _chars in accum.assistant_events
                    )
                ),
                _user_prompts_raw=[text for _, text in prompts],
            )
            finalize_session_io_digest(session)
            sessions.append(session)
        sessions.sort(key=lambda session: (session.start, session.session_id))
        return sessions

    def scan_user_prompts(
        self, start: datetime, end: datetime, min_chars: int = 8
    ) -> list[UserPrompt]:
        prompts: list[UserPrompt] = []
        for accum in self._read().values():
            for timestamp, raw_text in accum.prompts:
                if not self._in_range(timestamp, start, end):
                    continue
                text = raw_text.strip()
                if len(text) < min_chars or is_kaizenlog_internal_text(text):
                    continue
                prompts.append(
                    UserPrompt(
                        timestamp=timestamp,
                        project=accum.project,
                        text=text,
                        source="cursor",
                        session_id=accum.session_id,
                    )
                )
        prompts.sort(key=lambda prompt: (prompt.timestamp, prompt.source, prompt.text))
        return prompts
