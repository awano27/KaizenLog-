"""Bounded, incremental live retry detection for configured local AI buffers.

This module deliberately has no stats writes.  Nightly ``detect_retry_chains``
remains the accounting source of truth; ``live_episodes`` below is only a
record of notifications that were actually requested and delivered.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .guard import LIVE_EPISODES_FILE
from .file_lock import file_lock

_MAX_READ_BYTES = 512 * 1024
_MAX_FILES_PER_SOURCE = 1024
_MAX_DISCARD_BYTES = 1024 * 1024
_STATE_FILE = "guard-watch.json"


def _state_file(state_root: Path) -> Path:
    return Path(state_root) / _STATE_FILE


def _default_state() -> dict[str, Any]:
    return {"files": {}, "sessions": {}}


def _load_state(state_root: Path) -> dict[str, Any]:
    try:
        raw = json.loads(_state_file(state_root).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _default_state()
    if not isinstance(raw, dict):
        return _default_state()
    state = _default_state()
    state.update(raw)
    if not isinstance(state["files"], dict):
        state["files"] = {}
    if not isinstance(state["sessions"], dict):
        state["sessions"] = {}
    return state


def _save_state(state_root: Path, state: dict[str, Any]) -> bool:
    path = _state_file(state_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _file_identity(stat_result: os.stat_result) -> list[int]:
    return [int(stat_result.st_dev), int(stat_result.st_ino)]


def _line_spans(data: bytes, base_offset: int) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    pos = base_offset
    for raw in data.splitlines(keepends=True):
        end = pos + len(raw)
        text = raw.decode("utf-8", errors="replace").strip()
        if text:
            out.append((text, pos, end))
        pos = end
    return out


def _read_incremental(path: Path, previous: dict[str, Any]) -> tuple[list[tuple[str, int, int]], dict[str, Any]]:
    """Read only complete new lines, resetting safely after truncate/rotation."""
    try:
        st = path.stat()
    except OSError:
        return [], previous
    identity = _file_identity(st)
    offset = int(previous.get("offset") or 0)
    previous_size = int(previous.get("size") or 0)
    try:
        prefix_len = min(int(st.st_size), 256)
        with path.open("rb") as f:
            prefix = f.read(prefix_len)
            previous_prefix_len = min(max(0, int(previous.get("prefix_len") or 0)), int(st.st_size))
            f.seek(0)
            previous_prefix = f.read(previous_prefix_len)
    except OSError:
        return [], previous
    prefix_hash = hashlib.sha256(prefix).hexdigest()
    prior_prefix_hash = hashlib.sha256(previous_prefix).hexdigest()
    if (
        previous.get("identity") != identity
        or st.st_size < previous_size
        or (previous_prefix_len and previous.get("prefix_hash") not in (None, prior_prefix_hash))
        or offset > st.st_size
    ):
        offset = 0
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read(_MAX_READ_BYTES)
    except OSError:
        return [], previous
    next_state = {"offset": offset, "identity": identity, "prefix_hash": prefix_hash, "prefix_len": prefix_len, "size": st.st_size}
    if not data:
        return [], next_state
    end = data.rfind(b"\n")
    if end < 0:
        # A writer may leave a short partial JSON line.  A giant corrupt line,
        # however, must not block all later rows forever: scan a bounded amount
        # to its newline and discard only that line.
        scanned = len(data)
        while scanned < _MAX_DISCARD_BYTES:
            try:
                with path.open("rb") as f:
                    f.seek(offset + scanned)
                    more = f.read(min(_MAX_READ_BYTES, _MAX_DISCARD_BYTES - scanned))
            except OSError:
                return [], next_state
            if not more:
                return [], next_state
            nl = more.find(b"\n")
            if nl >= 0:
                tail = more[nl + 1 :]
                tail_end = tail.rfind(b"\n")
                if tail_end < 0:
                    next_state["offset"] = offset + scanned + nl + 1
                    return [], next_state
                complete = tail[: tail_end + 1]
                next_state["offset"] = offset + scanned + nl + 1 + len(complete)
                return _line_spans(complete, offset + scanned + nl + 1), next_state
            scanned += len(more)
        # Bounded discard progress.  The remaining fragment will be ignored as
        # malformed JSON until the next newline, but cannot pin the cursor.
        next_state["offset"] = offset + scanned
        return [], next_state
    complete = data[: end + 1]
    next_state["offset"] = offset + len(complete)
    return (
        _line_spans(complete, offset),
        next_state,
    )


def _parse_timestamp(value: object) -> float | None:
    from math import isfinite
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except ValueError:
            return None
    return None


def _codex_events(records: list[dict[str, Any]], fallback_session: str) -> tuple[list[tuple[str, float, str, dict[str, int]]], str]:
    events: list[tuple[str, float, str, dict[str, int]]] = []
    session_id = fallback_session
    for record in records:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if record.get("type") == "session_meta":
            candidate = payload.get("session_id") or payload.get("id")
            if isinstance(candidate, str) and candidate.strip():
                session_id = candidate.strip()
            continue
        if record.get("type") != "event_msg" or payload.get("type") != "user_message":
            continue
        candidate = payload.get("session_id") or payload.get("sessionId")
        if isinstance(candidate, str) and candidate.strip():
            session_id = candidate.strip()
        text = payload.get("message")
        ts = _parse_timestamp(record.get("timestamp"))
        if isinstance(text, str) and ts is not None and len(text.strip()) >= 8:
            from .aiwork import _is_system_wrapper, is_kaizenlog_internal_text
            if _is_system_wrapper(text.strip()) or is_kaizenlog_internal_text(text.strip()):
                continue
            events.append((f"codex:{session_id}", ts, text.strip(), dict(record.get("__guard_pointer") or {})))
    return events, session_id


def _browser_events(records: list[dict[str, Any]]) -> list[tuple[str, float, str, dict[str, int]]]:
    events: list[tuple[str, float, str, dict[str, int]]] = []
    for record in records:
        if str(record.get("role") or "").lower() not in ("user", "human"):
            continue
        text = record.get("text")
        ts = _parse_timestamp(record.get('timestamp'))
        if ts is None:
            ts = _parse_timestamp(record.get('ts'))
        site = str(record.get("site") or "").lower()
        conversation = str(record.get("conversation_id") or record.get('conversationId') or 'unknown').strip()
        if not conversation or not isinstance(text, str) or ts is None or len(text.strip()) < 8:
            continue
        from .aiwork import is_kaizenlog_internal_text
        if is_kaizenlog_internal_text(text.strip()):
            continue
        events.append((f"browser:{site}:{conversation}", ts, text.strip(), dict(record.get("__guard_pointer") or {})))
    return events


def _json_records(lines: list[tuple[str, int, int]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line, start, end in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            value["__guard_pointer"] = {"start": start, "end": end}
            records.append(value)
    return records


def _source_files(root: Path, *, codex: bool) -> list[Path]:
    if not root.is_dir():
        return []
    pattern = "rollout-*.jsonl" if codex else "*.jsonl"
    try:
        candidates = list(root.rglob(pattern))
        candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        return candidates[:_MAX_FILES_PER_SOURCE]
    except OSError:
        return []


def _notify_message(chain_len: int, excerpt: str) -> str:
    msg = f"⚠️ KaizenLog空転ブレーカー: 同趣旨の依頼が{chain_len}回目です。方針変更・手動介入を検討してください。"
    return f"{msg} 抜粋: {excerpt[:40]}" if excerpt else msg


def _append_episode(memory_dir: Path | None, event: dict[str, Any]) -> None:
    if memory_dir is None:
        return
    try:
        memory = Path(memory_dir)
        with file_lock(memory / ".live-episodes.lock", timeout=2.0):
            memory.mkdir(parents=True, exist_ok=True)
            with (memory / LIVE_EPISODES_FILE).open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except (OSError, TimeoutError):
        pass


def _recover_previous_text(
    pointer: object,
    files: dict[str, Any],
    normalize: Callable[[str], str],
) -> str | None:
    """Re-read a verified local JSONL span; state itself never holds prompt text."""
    if not isinstance(pointer, dict):
        return None
    key = pointer.get("file_key")
    start, end = pointer.get("start"), pointer.get("end")
    identity = pointer.get("identity")
    fingerprint = pointer.get("fingerprint")
    source = pointer.get("source")
    if not isinstance(key, str) or not isinstance(start, int) or not isinstance(end, int) or end <= start:
        return None
    file_state = files.get(key)
    if not isinstance(file_state, dict) or file_state.get("identity") != identity:
        return None
    try:
        path = Path(key.split(":", 1)[1])
        with path.open("rb") as f:
            f.seek(start)
            raw = f.read(min(end - start, _MAX_READ_BYTES))
        record = json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    text: object = None
    if source == "codex":
        payload = record.get("payload") if isinstance(record, dict) and isinstance(record.get("payload"), dict) else {}
        if record.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = payload.get("message")
    elif source == "browser" and isinstance(record, dict) and str(record.get("role") or "").lower() in ("user", "human"):
        text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    if hashlib.sha256(normalize(text).encode("utf-8")).hexdigest() != fingerprint:
        return None
    return text


def watch_once(
    *,
    codex_sessions_dir: Path,
    browser_export_dir: Path,
    state_root: Path,
    memory_dir: Path | None,
    retry_threshold: int,
    notify_enabled: bool,
    cooldown_seconds: int = 300,
    redactor: Callable[[str], str] | None = None,
    notifier: Callable[[str, str], object] | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Process new local rows once and return only delivered notification events.

    Costs remain ``None`` because these tail formats cannot establish an
    incremental attributable cost without inventing one.
    """
    try:
        with file_lock(Path(state_root) / ".guard-watch.lock", timeout=2.0):
            return _watch_once_locked(
                codex_sessions_dir=codex_sessions_dir,
                browser_export_dir=browser_export_dir,
                state_root=state_root,
                memory_dir=memory_dir,
                retry_threshold=retry_threshold,
                notify_enabled=notify_enabled,
                cooldown_seconds=cooldown_seconds,
                redactor=redactor,
                notifier=notifier,
                now=now,
            )
    except (OSError, TimeoutError):
        return []


def _watch_once_locked(
    *,
    codex_sessions_dir: Path,
    browser_export_dir: Path,
    state_root: Path,
    memory_dir: Path | None,
    retry_threshold: int,
    notify_enabled: bool,
    cooldown_seconds: int,
    redactor: Callable[[str], str] | None,
    notifier: Callable[[str, str], object] | None,
    now: float | None,
) -> list[dict[str, Any]]:
    if retry_threshold < 2:
        retry_threshold = 2
    now = time.time() if now is None else float(now)
    state = _load_state(Path(state_root))
    events: list[tuple[str, float, str, dict[str, Any]]] = []
    files = state["files"]

    for path in _source_files(Path(codex_sessions_dir), codex=True):
        key = f"codex:{path.resolve()}"
        previous = files.get(key) if isinstance(files.get(key), dict) else {}
        lines, file_state = _read_incremental(path, previous)
        records = _json_records(lines)
        extracted, session_id = _codex_events(records, str(previous.get("session_id") or path.stem))
        file_state["session_id"] = session_id
        files[key] = file_state
        events.extend(
            (sid, ts, text, {**pointer, "file_key": key, "identity": file_state.get("identity"), "source": "codex"})
            for sid, ts, text, pointer in extracted
        )

    for path in _source_files(Path(browser_export_dir), codex=False):
        key = f"browser:{path.resolve()}"
        previous = files.get(key) if isinstance(files.get(key), dict) else {}
        lines, file_state = _read_incremental(path, previous)
        files[key] = file_state
        events.extend(
            (sid, ts, text, {**pointer, "file_key": key, "identity": file_state.get("identity"), "source": "browser"})
            for sid, ts, text, pointer in _browser_events(_json_records(lines))
        )

    try:
        from .aiwork import _RETRY_SIMILARITY, _RETRY_WINDOW_MINUTES
        from .promptmine import normalize
    except Exception:
        return []

    delivered: list[dict[str, Any]] = []
    # A failed toast is retried on the next once run.  No raw text is retained.
    pending = [sid for sid, value in state["sessions"].items() if isinstance(value, dict) and value.get("pending_chain_len")]
    for session_id in pending:
        session = state["sessions"][session_id]
        if float(session.get('last_ts') or 0) < now - _RETRY_WINDOW_MINUTES * 60:
            session.pop('pending_chain_len', None)
            continue
        if not notify_enabled:
            continue
        try:
            message = _notify_message(int(session["pending_chain_len"]), "")
            if notifier is not None:
                sent = notifier("KaizenLog 空転ブレーカー", message)
            else:
                from .notify import notify
                sent = notify("KaizenLog 空転ブレーカー", message[:200], icon="Warning")
            if sent is not True:
                continue
        except Exception:
            continue
        session["last_notified_chain"] = int(session["pending_chain_len"])
        session["last_fire_ts"] = now
        session.pop("pending_chain_len", None)
        event = {
            "ts": datetime.now().astimezone().isoformat(),
            "session_id": session_id,
            "kind": "retry",
            "chain_len": int(session["last_notified_chain"]),
            "est_tokens": None,
            "est_usd": None,
            "excerpt": "",
        }
        _append_episode(memory_dir, event)
        delivered.append(event)
    for session_id, ts, text, pointer in sorted(events, key=lambda item: (item[0], item[1])):
        # A periodic watcher must consume old buffers quietly. A retry chain is
        # inherently short-lived, so the shared retry window is also the safe
        # live-notification freshness bound.
        if ts is None or ts < now - _RETRY_WINDOW_MINUTES * 60 or ts > now + 300:
            continue
        session = state["sessions"].get(session_id)
        if not isinstance(session, dict):
            session = {}
        # Re-read the prior byte span and use the nightly algorithm exactly.
        # Raw text only exists transiently in this invocation.
        norm = normalize(text)
        fingerprint = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        previous_text = _recover_previous_text(session.get("previous_pointer"), files, normalize)
        last_ts = float(session.get("last_ts") or 0.0)
        from difflib import SequenceMatcher
        similar = (
            previous_text is not None
            and 0 <= (ts - last_ts) <= _RETRY_WINDOW_MINUTES * 60
            and SequenceMatcher(None, normalize(previous_text), norm).ratio() >= _RETRY_SIMILARITY
        )
        chain_len = int(session.get("chain_len") or 0) + 1 if similar else 1
        session.update({
            "last_fingerprint": fingerprint,
            "previous_pointer": {**pointer, "fingerprint": fingerprint},
            "last_ts": ts,
            "chain_len": chain_len,
            "last_seen_ts": now,
        })
        if chain_len < retry_threshold:
            session["last_notified_chain"] = 0
            state["sessions"][session_id] = session
            continue
        already = int(session.get("last_notified_chain") or 0)
        last_fire = float(session.get("last_fire_ts") or 0.0)
        if not notify_enabled or chain_len <= already or now - last_fire < max(0, cooldown_seconds):
            state["sessions"][session_id] = session
            continue
        excerpt = redactor(text) if redactor is not None else text
        message = _notify_message(chain_len, excerpt)
        try:
            if notifier is not None:
                sent = notifier("KaizenLog 空転ブレーカー", message)
            else:
                from .notify import notify

                sent = notify("KaizenLog 空転ブレーカー", message[:200], icon="Warning")
            if sent is not True:
                session['pending_chain_len'] = chain_len
                state['sessions'][session_id] = session
                continue
        except Exception:
            # A failed delivery is not a history entry or cooldown; retry it
            # later without retaining its prompt excerpt.
            session["pending_chain_len"] = chain_len
            state["sessions"][session_id] = session
            continue
        event = {
            "ts": datetime.now().astimezone().isoformat(),
            "session_id": session_id,
            "kind": "retry",
            "chain_len": chain_len,
            "est_tokens": None,
            "est_usd": None,
            "excerpt": excerpt[:80],
        }
        _append_episode(memory_dir, event)
        session["last_notified_chain"] = chain_len
        session["last_fire_ts"] = now
        state["sessions"][session_id] = session
        delivered.append(event)

    # Keep this small and bounded even when input paths rotate frequently.
    state["sessions"] = dict(sorted(state["sessions"].items(), key=lambda item: float(item[1].get("last_seen_ts", 0)) if isinstance(item[1], dict) else 0, reverse=True)[:2048])
    _save_state(Path(state_root), state)
    return delivered
