"""Read-only Home snapshot and thin human-operation adapters.

The Home surface deliberately has no storage writers of its own.  Saved notes,
stats, Memory, run logs, and human input are read through their existing APIs;
the ActivityWatch refresh is an in-memory observation only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Literal, Mapping, TypeAlias
from zoneinfo import ZoneInfo

from . import human_input as _human_input
from .collector import (
    ActivityEvent,
    ActivityWatchClient,
    InputObservation,
    collect_day,
    collect_input_observation,
)
from .config import Config
from .focus import compute_input_stats
from .memory import MemoryEntry, load_entries, resolve_action_id
from .memory import ID_PATTERN, append_entries, mark_entry_done, memory_lock
from .journal_service import KznNoteSyncResult, sync_kzn_completion_note
from .note_paths import DailyNoteResolution, resolve_daily_note
from .privacy_filter import is_private_block
from .reliability import FailureReason, QualityState
from .runlog import load_runs
from .stats import activity_fingerprint
from .vault import ACTIVITY_MARKER, extract_section
from .classifier import Classifier
from .report import summarize
from .file_lock import file_lock, lock_path_for


StatsStatus: TypeAlias = Literal["observed", "missing", "stale", "unavailable", "unknown"]
MeasurementStatus: TypeAlias = Literal["observed", "missing", "stale", "unavailable", "unknown"]

# Shared result types remain importable from the Home service for callers that
# only depend on this application-layer boundary.
HumanSaveResult = _human_input.HumanSaveResult
ProjectionResult = _human_input.ProjectionResult
HumanOperation = _human_input.HumanOperation
HumanDay = _human_input.HumanDay


@dataclass(frozen=True, slots=True)
class SavedDaySnapshot:
    status: Literal["selected", "missing", "conflict", "unsupported"]
    path: Path | None
    content: str | None
    digest: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class NextActionView:
    source: Literal["free_text", "kzn"]
    source_id: str | None
    entry_digest: str | None
    text: str
    selectable: bool
    completable: bool
    status: Literal["available", "missing", "unknown"]


@dataclass(frozen=True, slots=True)
class LiveMeasurementSnapshot:
    observed: Mapping[str, float | int | str]
    status: MeasurementStatus
    refreshed_at: datetime | None
    source: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    status: Literal["none", "pending", "success", "partial", "failure", "unknown"]
    source_status: str | None
    message: str | None
    operation_id: str | None
    revision: int | None


@dataclass(frozen=True, slots=True)
class HomeSnapshot:
    day: date
    config_status: Literal["ready"]
    saved: SavedDaySnapshot
    stats_status: StatsStatus
    human: _human_input.HumanDay
    next_actions: tuple[NextActionView, ...]
    measurements: LiveMeasurementSnapshot
    recovery: RecoverySnapshot


@dataclass(frozen=True, slots=True)
class KznCompletionResult:
    """Contract for the KZN completion tail implemented by the parent task."""

    status: Literal[
        "saved", "idempotent", "conflict", "stale", "invalid", "partial_failure", "failed"
    ]
    entry_id: str
    done_date: date | None
    human_revision: int | None
    memory_status: Literal["unchanged", "done", "failed"]
    note_status: Literal["not_requested", "synced", "missing", "conflict", "failed"]
    reason: str | None


@dataclass(frozen=True, slots=True)
class CorrectionTarget:
    """A selector row derived solely from one saved stats block."""

    stable_key: str
    start: datetime
    end: datetime
    app: str
    source_category: str | None
    original_project: str | None
    display_label: str
    privacy: str

    @property
    def time(self) -> tuple[datetime, datetime]:
        return self.start, self.end


def memory_entry_digest(entry: MemoryEntry) -> str:
    """Digest only the immutable KZN identity and action text."""
    payload = json.dumps(
        {"id": entry.id, "action": entry.action},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _completion_result(
    status: Literal["saved", "idempotent", "conflict", "stale", "invalid", "partial_failure", "failed"],
    action_id: str,
    *,
    done_date: date | None = None,
    human_revision: int | None = None,
    memory_status: Literal["unchanged", "done", "failed"] = "unchanged",
    note_status: Literal["not_requested", "synced", "missing", "conflict", "failed"] = "not_requested",
    reason: str | None = None,
) -> KznCompletionResult:
    return KznCompletionResult(status, action_id, done_date, human_revision, memory_status, note_status, reason)


def _done_date(entry: MemoryEntry) -> date | None:
    if entry.done_date is None:
        return None
    try:
        return date.fromisoformat(entry.done_date)
    except (TypeError, ValueError):
        return None


def _selected_kzn_id(human: _human_input.HumanDay) -> str | None:
    latest: _human_input.HumanOperationRecord | None = None
    if human.status != "loaded":
        return None
    for record in human.history:
        if record.active and record.operation.kind == "next_action":
            latest = record
    if latest is None:
        return None
    payload = latest.operation.payload
    if payload.get("source") != "kzn":
        return None
    source_id = payload.get("source_id")
    return source_id if isinstance(source_id, str) else None


def _note_result_status(value: KznNoteSyncResult) -> tuple[
    Literal["saved", "partial_failure", "failed"],
    Literal["not_requested", "synced", "missing", "conflict", "failed"],
]:
    if value.status == "synced":
        return "saved", "synced"
    if value.status == "not_requested":
        return "saved", "not_requested"
    if value.status in {"missing", "conflict", "failed"}:
        return "partial_failure", value.status
    return "failed", "failed"


def complete_kzn_action(
    cfg: Config,
    action_id: str,
    day: date,
    *,
    expected_revision: int,
    expected_action_digest: str,
) -> KznCompletionResult:
    """Complete the currently selected KZN action with CAS-style checks.

    The lock order is Memory -> human JSON -> note Markdown.  Memory is
    committed before the existing journal synchronizer is called; a note
    failure therefore remains an explicit partial failure and is never rolled
    back by this adapter.
    """
    if not isinstance(action_id, str) or ID_PATTERN.fullmatch(action_id.strip()) is None or action_id != action_id.strip():
        return _completion_result("invalid", action_id if isinstance(action_id, str) else "", reason="action_idは完全なKZN IDが必要です")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        return _completion_result("invalid", action_id, reason="expected_revisionが不正です")
    if not isinstance(expected_action_digest, str) or len(expected_action_digest) != 64 or any(char not in "0123456789abcdef" for char in expected_action_digest):
        return _completion_result("invalid", action_id, reason="expected_action_digestが不正です")

    preview_human = _human_input.load_human_day(cfg, day)
    if preview_human.status == "missing":
        return _completion_result("invalid", action_id, human_revision=preview_human.revision, reason="本人入力がありません")
    if preview_human.status != "loaded":
        return _completion_result("invalid", action_id, human_revision=preview_human.revision, reason=preview_human.reason or "本人入力が不正です")
    if preview_human.revision != expected_revision:
        return _completion_result("stale", action_id, human_revision=preview_human.revision, reason="expected_revisionが現在値と一致しません")
    if _selected_kzn_id(preview_human) != action_id:
        return _completion_result("invalid", action_id, human_revision=preview_human.revision, reason="現在選択中のKZN actionではありません")

    human_path = preview_human.path
    try:
        with memory_lock(Path(cfg.memory_path)):
            entries = load_entries(Path(cfg.memory_path))
            resolved = resolve_action_id(action_id, entries)
            if not isinstance(resolved, MemoryEntry) or resolved.id != action_id:
                return _completion_result("invalid", action_id, human_revision=preview_human.revision, reason="KZN actionが一意に解決できません")
            actual_digest = memory_entry_digest(resolved)
            if actual_digest != expected_action_digest:
                return _completion_result("conflict", action_id, human_revision=preview_human.revision, reason="KZN actionの内容がpreview後に変更されました")
            with file_lock(lock_path_for(human_path)):
                current_human = _human_input.load_human_day(cfg, day)
                if current_human.status != "loaded":
                    return _completion_result("conflict", action_id, human_revision=current_human.revision, reason="本人入力が保存待ちの間に変更されました")
                if current_human.revision != expected_revision:
                    return _completion_result("stale", action_id, human_revision=current_human.revision, reason="expected_revisionが現在値と一致しません")
                if _selected_kzn_id(current_human) != action_id:
                    return _completion_result("conflict", action_id, human_revision=current_human.revision, reason="現在選択中のKZN actionが変更されました")

                if resolved.status == "done":
                    done_date = _done_date(resolved)
                    if done_date is None:
                        return _completion_result("invalid", action_id, human_revision=current_human.revision, reason="既存doneのdone_dateが不正です")
                    committed_status = "idempotent"
                    committed_entry = resolved
                elif resolved.status == "proposed":
                    committed_entry = mark_entry_done(resolved, day)
                    append_entries(Path(cfg.memory_path), [committed_entry])
                    committed_status = "saved"
                    done_date = day
                else:
                    return _completion_result("invalid", action_id, human_revision=current_human.revision, reason="KZN actionは完了対象のproposedではありません")

                try:
                    note_result = sync_kzn_completion_note(cfg, day)
                except Exception as exc:
                    note_result = KznNoteSyncResult("failed", type(exc).__name__)
                final_status, note_status = _note_result_status(note_result)
                if final_status == "partial_failure":
                    result_status = "partial_failure"
                else:
                    result_status = committed_status
                return _completion_result(
                    result_status,
                    action_id,
                    done_date=done_date,
                    human_revision=current_human.revision,
                    memory_status="done",
                    note_status=note_status,
                    reason=note_result.reason,
                )
    except TimeoutError as exc:
        return _completion_result("failed", action_id, human_revision=expected_revision, reason=type(exc).__name__)
    except OSError as exc:
        return _completion_result("failed", action_id, human_revision=expected_revision, reason=type(exc).__name__)


def _saved_snapshot(cfg: Config, day: date) -> tuple[SavedDaySnapshot, str | None]:
    resolution = resolve_daily_note(cfg, day)
    if resolution.status in {"conflict", "unsupported"}:
        return SavedDaySnapshot(resolution.status, None, None, None, resolution.reason), None
    path = resolution.path
    if path is None:
        return SavedDaySnapshot("unsupported", None, None, None, "日次ノートの解決先がありません"), None
    if not path.is_file():
        return SavedDaySnapshot("missing", path, None, None, resolution.reason), None
    try:
        raw = path.read_bytes()
        content = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return SavedDaySnapshot("unsupported", path, None, None, f"日次ノートを読めません: {exc}"), None
    return SavedDaySnapshot(
        "selected", path, content, hashlib.sha256(raw).hexdigest(), resolution.reason
    ), content


def _stats_status(cfg: Config, day: date, note_content: str | None) -> StatsStatus:
    path = Path(cfg.stats_path) / f"{day.isoformat()}.json"
    if not path.is_file():
        return "missing"
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unavailable"
    if not isinstance(payload, dict) or payload.get("day") != day.isoformat() or not isinstance(payload.get("blocks"), list):
        return "unavailable"
    seen_keys: set[str] = set()
    for block in payload["blocks"]:
        if not isinstance(block, Mapping):
            return "unavailable"
        try:
            start = _parse_stats_datetime(block.get("start"))
            end = _parse_stats_datetime(block.get("end"))
            app, title = block.get("app"), block.get("title")
            if end <= start or not isinstance(app, str) or not isinstance(title, str):
                return "unavailable"
            block_key = _human_input.stable_target_key(start, end, app, title)
        except (TypeError, ValueError, UnicodeError):
            return "unavailable"
        if block_key in seen_keys:
            return "unavailable"
        seen_keys.add(block_key)
    # An activity fingerprint is a relation to the saved note.  A mismatch is
    # stale data, while a malformed stats payload is unavailable.
    fingerprint = payload.get("activity_sha256")
    if fingerprint is not None:
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            return "unavailable"
        activity = extract_section(note_content or "", ACTIVITY_MARKER)
        if activity is None or activity_fingerprint(activity) != fingerprint:
            return "stale"
    return "observed"


def _next_actions(cfg: Config, human: _human_input.HumanDay) -> tuple[NextActionView, ...]:
    entries = load_entries(Path(cfg.memory_path))
    proposed = [entry for entry in entries if entry.status == "proposed"]
    represented_ids: set[str] = set()
    views: list[NextActionView] = []
    latest: _human_input.HumanOperationRecord | None = None
    if human.status == "loaded":
        for record in human.history:
            if record.active and record.operation.kind == "next_action":
                latest = record
    if latest is not None:
        operation = latest.operation
        payload = operation.payload
        source = payload.get("source")
        text = str(payload.get("text") or "")
        if source == "free_text":
            views.append(NextActionView("free_text", None, None, text, True, False, "available"))
        else:
            source_id = payload.get("source_id")
            if not isinstance(source_id, str):
                views.append(NextActionView("kzn", None, None, text, False, False, "unknown"))
            else:
                represented_ids.add(source_id)
                resolved = resolve_action_id(source_id, entries)
                if not isinstance(resolved, MemoryEntry) or resolved.id != source_id:
                    status: Literal["missing", "unknown"] = "unknown" if isinstance(resolved, list) else "missing"
                    views.append(NextActionView("kzn", source_id, None, text, False, False, status))
                elif resolved.status != "proposed":
                    # Keep a terminal selected row visible for provenance, but
                    # never present it as a completion candidate.
                    views.append(NextActionView("kzn", source_id, memory_entry_digest(resolved), text, False, False, "missing"))
                else:
                    views.append(NextActionView("kzn", source_id, memory_entry_digest(resolved), text, True, True, "available"))
    # Memory candidates remain visible even without a HumanDay or when the
    # selected view is free text.  They are selectable alternatives until the
    # existing human operation selects one.
    for entry in proposed:
        if entry.id in represented_ids:
            continue
        views.append(NextActionView("kzn", entry.id, memory_entry_digest(entry), entry.action, True, False, "available"))
    return tuple(views)


def _projection_recovery(human: _human_input.HumanDay) -> RecoverySnapshot:
    if human.status == "missing" or human.status == "loaded" and human.projection_state == "none":
        return RecoverySnapshot("none", None, None, None, human.revision)
    if human.status != "loaded":
        return RecoverySnapshot("unknown", "invalid", human.reason, None, human.revision)
    state = human.projection_state
    if state == "pending":
        return RecoverySnapshot("pending", state, "投影待ちです", None, human.revision)
    if state == "applied":
        return RecoverySnapshot("success", state, None, None, human.revision)
    if state == "conflict":
        return RecoverySnapshot("failure", state, human.reason or "投影競合", None, human.revision)
    return RecoverySnapshot("unknown", state, "投影状態を解釈できません", None, human.revision)


def _runlog_recovery(
    runs: list[dict], day: date, fallback: RecoverySnapshot
) -> RecoverySnapshot:
    """Use an explicit structured recovery record when one is available.

    Generic ``ok`` or ``render_status`` output is intentionally ignored: a
    successful unrelated run is not evidence that a recovery completed.
    """
    for row in reversed(runs):
        if not isinstance(row, dict) or row.get("date") != day.isoformat():
            continue
        raw_status = row.get("recovery_status")
        if raw_status is None and row.get("command") not in {"recovery", "recover", "human_recovery"}:
            continue
        if raw_status not in {"none", "pending", "success", "partial", "failure", "unknown"}:
            return RecoverySnapshot("unknown", str(raw_status) if raw_status is not None else None, "recovery status is invalid", None, None)
        revision = row.get("revision") if isinstance(row.get("revision"), int) else None
        return RecoverySnapshot(
            raw_status,
            str(row.get("source_status")) if row.get("source_status") is not None else str(raw_status),
            str(row.get("message")) if row.get("message") is not None else None,
            str(row.get("operation_id")) if row.get("operation_id") is not None else None,
            revision,
        )
    return fallback


def recovery_snapshot_from_result(result: object) -> RecoverySnapshot:
    """Preserve an operation's wire status while choosing display severity."""
    source_status = getattr(result, "status", None)
    if not isinstance(source_status, str):
        return RecoverySnapshot("unknown", None, "result status is unavailable", None, None)
    success = {"saved", "idempotent", "applied", "already_applied"}
    pending = {"pending"}
    partial = {"partial_failure", "partial"}
    failure = {"conflict", "stale", "invalid", "failed"}
    projection_state = getattr(result, "projection_state", None)
    if source_status in {"saved", "idempotent"} and projection_state == "pending":
        display: Literal["pending"] = "pending"
        message = "本人入力は保存済み／日誌反映は未完了"
    elif source_status in {"saved", "idempotent"} and projection_state == "conflict":
        display = "failure"
        message = "本人入力は保存済み／日誌反映で競合"
    elif source_status in success:
        display: Literal["success"] = "success"
        message = getattr(result, "reason", None)
    elif source_status in pending:
        display = "pending"
        message = getattr(result, "reason", None)
    elif source_status in partial:
        display = "partial"
        message = getattr(result, "reason", None)
    elif source_status in failure:
        display = "failure"
        message = getattr(result, "reason", None)
    else:
        display = "unknown"
        message = getattr(result, "reason", None)
    operation_id = getattr(result, "operation_id", None)
    if operation_id is None:
        operation_id = getattr(result, "entry_id", None)
    revision = getattr(result, "revision", None)
    if revision is None:
        revision = getattr(result, "human_revision", None)
    return RecoverySnapshot(
        display,
        source_status,
        message,
        operation_id if isinstance(operation_id, str) else None,
        revision if isinstance(revision, int) else None,
    )


def load_home_snapshot(cfg: Config, day: date) -> HomeSnapshot:
    """Read all persisted Home inputs without creating any filesystem artifact."""
    saved, content = _saved_snapshot(cfg, day)
    stats_status = _stats_status(cfg, day, content)
    human = _human_input.load_human_day(cfg, day)
    # Reading the JSONL API is intentional.  In particular, do not call
    # load_operational_runs: it initializes OpsLedger SQLite in some configs.
    runs = load_runs(Path(cfg.logs_path))
    return HomeSnapshot(
        day=day,
        config_status="ready",
        saved=saved,
        stats_status=stats_status,
        human=human,
        next_actions=_next_actions(cfg, human),
        measurements=LiveMeasurementSnapshot({}, "unknown", None, "activitywatch", "未更新です"),
        recovery=_runlog_recovery(runs, day, _projection_recovery(human)),
    )


def _utc_now(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(tz)


def refresh_aw_snapshot(
    cfg: Config,
    day: date,
    *,
    now: datetime | None = None,
    client: ActivityWatchClient | None = None,
) -> LiveMeasurementSnapshot:
    """Collect and summarize an AW interval in memory only."""
    try:
        tz = ZoneInfo(cfg.timezone)
    except Exception as exc:
        return LiveMeasurementSnapshot({}, "unavailable", None, "activitywatch", f"timezone: {exc}")
    current = _utc_now(now or datetime.now(tz), tz)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end_of_day = datetime.combine(day, time.max, tzinfo=tz)
    if day > current.date():
        return LiveMeasurementSnapshot({}, "unknown", None, "activitywatch", "future day has no observation interval")
    end = min(current, end_of_day)
    if end <= start:
        return LiveMeasurementSnapshot({}, "unknown", None, "activitywatch", "observation interval is empty")
    aw = client if client is not None else ActivityWatchClient(cfg.aw_base_url)
    try:
        events, afk_available = collect_day(aw, start, end)
    except Exception as exc:
        return LiveMeasurementSnapshot({}, "unavailable", current.astimezone(timezone.utc), "activitywatch", type(exc).__name__)
    observed: dict[str, float | int | str] = {}
    activity_observed = bool(events)
    try:
        classified = Classifier(cfg.rules).classify_all(events)
        summary = summarize(day, classified, gap_minutes=cfg.session_gap_minutes)
        observed.update({
            "activity_status": "observed" if activity_observed else "missing",
            "afk_status": "observed" if afk_available else "missing",
        })
        if activity_observed:
            observed.update({
                "active_minutes": round(float(summary.total_minutes), 1),
                "context_switches": int(summary.context_switches),
            })
    except Exception as exc:
        return LiveMeasurementSnapshot({}, "unavailable", current.astimezone(timezone.utc), "activitywatch", type(exc).__name__)
    input_observation: InputObservation | None = None
    input_failed = False
    try:
        input_observation = collect_input_observation(aw, start, end)
        observed["input_status"] = input_observation.state.value
        input_reason = input_observation.reason.value
        if input_observation.state is QualityState.OBSERVED:
            input_stats = compute_input_stats(input_observation.events, day_start=start, day_end=end)
            observed.update({
                "keypresses": int(input_stats.keypresses),
                "clicks": int(input_stats.clicks),
                "active_input_minutes": float(input_stats.active_input_minutes),
                "focus_minutes": round(float(input_stats.focus_minutes), 1),
            })
    except Exception as exc:
        input_observation = None
        input_failed = True
        input_reason = type(exc).__name__
        observed["input_status"] = "unavailable"
    any_observed = activity_observed or (
        input_observation is not None and input_observation.state is QualityState.OBSERVED
    )
    measurement_status: MeasurementStatus
    if any_observed:
        measurement_status = "observed"
    elif input_failed:
        measurement_status = "unavailable"
    else:
        measurement_status = "missing"
    return LiveMeasurementSnapshot(
        observed,
        measurement_status,
        current.astimezone(timezone.utc),
        "activitywatch",
        None if input_observation is not None and input_observation.state is QualityState.OBSERVED else input_reason,
    )


def apply_human_operation(
    cfg: Config,
    day: date,
    operation: _human_input.HumanOperation,
    *,
    expected_revision: int,
) -> _human_input.HumanSaveResult:
    return _human_input.apply_human_operation(cfg, day, operation, expected_revision=expected_revision)


def project_human_day(
    cfg: Config, day: date, *, expected_revision: int
) -> _human_input.ProjectionResult:
    return _human_input.project_human_day(cfg, day, expected_revision=expected_revision)


def _parse_stats_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("stats block time is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stats block time must be timezone-aware")
    return parsed


def load_correction_targets(cfg: Config, day: date) -> tuple[CorrectionTarget, ...]:
    """Build a stable, privacy-aware selector from the saved stats source only."""
    path = Path(cfg.stats_path) / f"{day.isoformat()}.json"
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict) or payload.get("day") != day.isoformat() or not isinstance(payload.get("blocks"), list):
        return ()
    targets: list[CorrectionTarget] = []
    seen: set[str] = set()
    for block in payload["blocks"]:
        if not isinstance(block, Mapping):
            return ()
        try:
            start, end = _parse_stats_datetime(block.get("start")), _parse_stats_datetime(block.get("end"))
            app, title = block.get("app"), block.get("title")
            if end <= start or not isinstance(app, str) or not isinstance(title, str) or not app.strip():
                return ()
            key = _human_input.stable_target_key(start, end, app, title)
        except (TypeError, ValueError, UnicodeError):
            return ()
        if key in seen:
            return ()
        seen.add(key)
        private = is_private_block(
            block.get("category"), title, app,
            private_title_patterns=tuple(getattr(getattr(cfg, "privacy", None), "private_title_patterns", ()) or ()),
        )
        privacy = "private" if private else "public"
        label_title = "（非表示）" if private else title
        targets.append(
            CorrectionTarget(
                stable_key=key,
                start=start,
                end=end,
                app=app,
                source_category=str(block.get("category")) if block.get("category") is not None else None,
                original_project=str(block.get("project")) if block.get("project") is not None else None,
                display_label=f"{app} / {label_title}",
                privacy=privacy,
            )
        )
    return tuple(targets)


__all__ = [
    "CorrectionTarget", "HomeSnapshot", "KznCompletionResult", "LiveMeasurementSnapshot",
    "NextActionView", "RecoverySnapshot", "SavedDaySnapshot", "HumanSaveResult", "ProjectionResult",
    "HumanOperation", "HumanDay", "KznNoteSyncResult", "apply_human_operation",
    "complete_kzn_action",
    "load_correction_targets", "load_home_snapshot", "memory_entry_digest",
    "project_human_day", "refresh_aw_snapshot",
    "recovery_snapshot_from_result",
]
