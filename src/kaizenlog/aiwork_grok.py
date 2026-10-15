"""Schema-gated telemetry reader for Grok CLI's local ``updates.jsonl`` files.

This module deliberately reads only the on-disk event envelope implemented by
``xai-org/grok-build``.  It does not inspect ``summary.json`` because its
schema is not part of this adapter's evidence contract, and it does not infer
dates from paths or filesystem metadata.
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


_ACP_METHOD = "session/update"
_XAI_METHOD = "_x.ai/session/update"


def _unix_seconds(value: object) -> datetime | None:
    """Accept only the upstream envelope's positive integral Unix seconds."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _storage_session_id(root: Path, updates_path: Path) -> str:
    """Use the local storage identity, never a potentially borrowed payload ID."""
    try:
        rel = updates_path.parent.relative_to(root).as_posix()
    except ValueError:
        rel = updates_path.parent.name
    return f"grok:{rel or updates_path.parent.name}"


@dataclass
class _PromptEntry:
    timestamp: datetime | None
    text: str


@dataclass
class _PromptCollector:
    """Port of Grok's documented prompt-run and rewind semantics.

    ``promptIndex`` is the producer's counted prompt sequence.  After an
    indexed turn appears, unindexed chunks are deliberately not counted, just
    like the upstream collector, because they can be mid-turn phantoms.
    """

    prompts: list[_PromptEntry] = field(default_factory=list)
    _parts: list[str] = field(default_factory=list)
    _timestamp: datetime | None = None
    _in_user: bool = False
    _current_prompt_index: int | None = None
    _current_counts: bool = False
    _seen_marker: bool = False

    def _flush(self) -> None:
        if self._in_user and self._current_counts:
            text = "".join(self._parts).strip()
            if text:
                self.prompts.append(_PromptEntry(self._timestamp, text))
        self._parts.clear()
        self._timestamp = None
        self._in_user = False
        self._current_prompt_index = None
        self._current_counts = False

    def user_text(
        self, timestamp: datetime | None, text: str, prompt_index: int | None
    ) -> None:
        if prompt_index is not None:
            self._seen_marker = True
        counts = prompt_index is not None if self._seen_marker else True
        new_run = (
            not self._in_user
            or self._seen_marker
            or prompt_index is not None
        ) and prompt_index != self._current_prompt_index
        if new_run:
            self._flush()
            self._in_user = True
            self._current_prompt_index = prompt_index
            self._current_counts = counts
            self._timestamp = timestamp
        elif not self._in_user:
            self._in_user = True
            self._current_prompt_index = prompt_index
            self._current_counts = counts
            self._timestamp = timestamp
        self._parts.append(text)

    def not_user(self) -> None:
        self._flush()

    def rewind_to(self, target_index: int) -> None:
        self._flush()
        del self.prompts[target_index:]

    def finish(self) -> list[_PromptEntry]:
        self._flush()
        return self.prompts


@dataclass
class _GrokFile:
    session_id: str
    prompts: list[_PromptEntry]
    event_times: list[datetime]


def _parse_event(record: object) -> tuple[datetime | None, str, dict] | None:
    """Validate only Grok's persisted SessionUpdateEnvelope shape."""
    if not isinstance(record, dict):
        return None
    method = record.get("method")
    params = record.get("params")
    if method not in (_ACP_METHOD, _XAI_METHOD) or not isinstance(params, dict):
        return None
    # ``SessionNotification`` contains sessionId.  Its value is intentionally
    # not used as this adapter's identity; another session can be replayed in a
    # file whose storage directory owns the local transcript.
    if not isinstance(params.get("sessionId"), str) or not params["sessionId"]:
        return None
    update = params.get("update")
    if not isinstance(update, dict) or not isinstance(update.get("sessionUpdate"), str):
        return None
    return _unix_seconds(record.get("timestamp")), method, update


def _user_chunk(update: dict) -> tuple[str, int | None] | None:
    if update.get("sessionUpdate") != "user_message_chunk":
        return None
    content = update.get("content")
    if not isinstance(content, dict) or content.get("type") != "text":
        return None
    text = content.get("text")
    if not isinstance(text, str):
        return None
    content_meta = content.get("_meta")
    if isinstance(content_meta, dict) and content_meta.get("bash_command") is not None:
        return None
    meta = update.get("_meta")
    if isinstance(meta, dict) and meta.get("host_turn") is True:
        return None
    index = meta.get("promptIndex") if isinstance(meta, dict) else None
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        index = None
    return text, index


def _rewind_target(method: str, update: dict) -> int | None:
    if method != _XAI_METHOD or update.get("sessionUpdate") != "rewind_marker":
        return None
    target = update.get("target_prompt_index")
    if isinstance(target, bool) or not isinstance(target, int) or target < 0:
        return None
    return target


class GrokAdapter:
    """Read Grok CLI's local ``~/.grok/sessions/**/updates.jsonl`` event logs."""

    name = "grok"

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = Path(sessions_dir)

    def _files(self) -> list[Path]:
        if not self.sessions_dir.is_dir():
            return []
        try:
            return sorted(
                (path for path in self.sessions_dir.rglob("updates.jsonl") if path.is_file()),
                key=lambda path: path.as_posix(),
            )
        except OSError:
            return []

    def _read_file(self, path: Path) -> _GrokFile | None:
        collector = _PromptCollector()
        event_times: list[datetime] = []
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        event = _parse_event(json.loads(line))
                    except json.JSONDecodeError:
                        # Upstream treats a torn/unparseable line as a turn boundary.
                        collector.not_user()
                        continue
                    if event is None:
                        collector.not_user()
                        continue
                    timestamp, method, update = event
                    if timestamp is not None:
                        event_times.append(timestamp)
                    chunk = _user_chunk(update) if method == _ACP_METHOD else None
                    if chunk is not None:
                        text, prompt_index = chunk
                        collector.user_text(timestamp, text, prompt_index)
                        continue
                    target = _rewind_target(method, update)
                    if target is not None:
                        collector.rewind_to(target)
                    else:
                        collector.not_user()
        except OSError:
            return None
        return _GrokFile(
            session_id=_storage_session_id(self.sessions_dir, path),
            prompts=collector.finish(),
            event_times=event_times,
        )

    @staticmethod
    def _in_range(timestamp: datetime | None, start: datetime, end: datetime) -> bool:
        return timestamp is not None and start <= timestamp < end

    def scan_sessions(
        self, day_start: datetime, day_end: datetime
    ) -> list[AISession]:
        sessions: list[AISession] = []
        for path in self._files():
            parsed = self._read_file(path)
            if parsed is None:
                continue
            prompts = [
                item
                for item in parsed.prompts
                if self._in_range(item.timestamp, day_start, day_end)
            ]
            if not prompts:
                continue
            timestamps = [item.timestamp for item in prompts if item.timestamp is not None]
            timestamps.extend(
                timestamp
                for timestamp in parsed.event_times
                if day_start <= timestamp < day_end
            )
            if not timestamps:
                continue
            first = prompts[0].text
            extracted = extract_session_title(first)
            title, first_len = extracted if extracted is not None else (None, 0)
            session = AISession(
                session_id=parsed.session_id,
                project="grok",
                start=min(timestamps),
                end=max(timestamps),
                user_turns=len(prompts),
                source="grok",
                title=title,
                first_prompt_len=first_len,
                is_internal=is_kaizenlog_internal_text(first),
                tools_measurable=False,
                tool_metrics_measurable=False,
                output_tokens_measurable=False,
                output_tokens=0,
                assistant_chars_measurable=False,
                _user_prompts_raw=[item.text for item in prompts],
            )
            finalize_session_io_digest(session)
            sessions.append(session)
        sessions.sort(key=lambda session: (session.start, session.session_id))
        return sessions

    def scan_user_prompts(
        self, start: datetime, end: datetime, min_chars: int = 8
    ) -> list[UserPrompt]:
        prompts: list[UserPrompt] = []
        for path in self._files():
            parsed = self._read_file(path)
            if parsed is None:
                continue
            for item in parsed.prompts:
                if not self._in_range(item.timestamp, start, end):
                    continue
                text = item.text.strip()
                if len(text) < min_chars or is_kaizenlog_internal_text(text):
                    continue
                prompts.append(
                    UserPrompt(
                        timestamp=item.timestamp,
                        project="grok",
                        text=text,
                        source="grok",
                        session_id=parsed.session_id,
                    )
                )
        prompts.sort(key=lambda prompt: (prompt.timestamp, prompt.text))
        return prompts
