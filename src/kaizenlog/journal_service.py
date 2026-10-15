"""Typed, side-effect-bounded journal and report services.

The service deliberately separates collection from persistence.  A preview
owns one immutable source snapshot; execution only consumes that snapshot and
rechecks the file/database preconditions captured with it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .classifier import Classifier
from .collector import ActivityEvent, ActivityWatchClient, collect_day, collect_input_observation
from .config import Config
from .aiwork import available_adapters, collect_ai_telemetry, compute_loop_tax, detect_retry_chains
from .focus import compute_input_stats
from .focus import InputStats
from .human_input import HumanDay, load_human_day
from .memory import ID_PATTERN, humanize_action_body, load_entries, memory_lock, open_actions_in_window, render_actions_section
from .ops_ledger import OpsLedger
from .report import build_session_spans, render_markdown, summarize
from .runlog import load_runs
from .stats import build_stats, load_stats
from .experiments import load_experiments
from .resume import build_resume_section
from .privacy import make_redactor
from .nippou import generate_nippou_deterministic
from .vault import ACTIVITY_MARKER, NIPPOU_MARKER, RESUME_MARKER, DailyNoteStore, atomic_write_text, default_frontmatter, extract_heading_section, extract_section, upsert_section
from .file_lock import locked_read_modify_write
from .note_paths import DailyFormat as DailyNoteFormat, resolve_daily_note
from .setup import SetupOptions


def _digest(value: object) -> str:
    def normalize(item: object) -> object:
        if is_dataclass(item):
            return normalize(asdict(item))
        if isinstance(item, Mapping):
            return {str(k): normalize(v) for k, v in sorted(item.items(), key=lambda x: str(x[0]))}
        if isinstance(item, (list, tuple)):
            return [normalize(v) for v in item]
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        return str(item)

    raw = json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _bytes_digest(value: bytes | None) -> str | None:
    return hashlib.sha256(value).hexdigest() if value is not None else None


@dataclass(frozen=True, slots=True)
class SampleJournal:
    status: Literal["sample"]
    fixture_id: str
    target_day: date
    next_day: date
    timezone: str
    markdown: str
    planned_updates: tuple["PlannedUpdate", ...]
    provider_probe_count: int
    writes: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class SourceState:
    status: Literal["observed", "no_records", "unavailable"]
    reason: str | None
    source_digest: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    target_day: date
    next_day: date
    timezone: str
    activity: Any
    input: Any
    ai_sessions: tuple[Any, ...]
    current_stats: Mapping[str, Any]
    stats_history: tuple[Mapping[str, Any], ...]
    memory_entries: tuple[Any, ...]
    runlog_entries: tuple[Mapping[str, Any], ...]
    experiments: tuple[Any, ...]
    note_revisions: tuple[tuple[str, int], ...]
    source_digest: str
    provider_probe_count: int = 0
    rendered_markdown: str = ""
    next_markdown: str = ""
    human_input_digests: tuple[tuple[str, Path, str | None], ...] = ()
    source_files: tuple[tuple[Path, str | None], ...] = ()
    stats_payload: str | None = None
    input_quality: Mapping[str, Any] | None = None
    afk_watcher_available: bool | None = None
    ledger_run_id: str | None = None
    human_days: tuple[tuple[str, HumanDay], ...] = ()
    effective_config_digest: str = ""
    prepared_intent: str | None = None
    prepared_open_actions: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class PlannedUpdate:
    target: Literal["target_day", "next_day", "ledger", "stats", "runlog", "experiments", "sqlite"]
    path: Path | None
    action: Literal["upsert_section", "replace_json", "append_jsonl", "sqlite_transaction", "delete"]
    managed_region: str | None
    expected_revision: int | None
    expected_digest: str | None
    new_digest: str | None
    reason: str
    explicit_confirmation_required: bool = False
    row_id: str | None = None
    expected_bytes: bytes | None = None
    new_bytes: bytes | None = None


@dataclass(frozen=True, slots=True)
class Precondition:
    target: str
    path: Path | None
    kind: Literal["file_bytes", "json_revision", "sqlite_row_digest", "directory_entry"]
    exists: bool
    expected_digest: str | None
    expected_revision: int | None
    observed_digest: str | None
    observed_revision: int | None
    row_id: str | None = None
    expected_bytes: bytes | None = None


@dataclass(frozen=True, slots=True)
class JournalPreview:
    status: Literal["ready", "no_records", "unavailable", "conflict"]
    target_day: date
    next_day: date
    timezone: str
    snapshot: SourceSnapshot
    snapshot_digest: str
    planned_updates: tuple[PlannedUpdate, ...]
    preconditions: tuple[Precondition, ...]
    source_status: SourceState
    provider_probe_count: int = 0
    catch_up: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class JournalExecutionResult:
    status: Literal["generated", "already_generated", "no_records", "unavailable", "conflict", "failed", "partial"]
    target_day: date
    snapshot_digest: str
    planned_updates: tuple[PlannedUpdate, ...]
    applied_updates: tuple[PlannedUpdate, ...]
    pending_updates: tuple[PlannedUpdate, ...]
    precondition_failures: tuple[Precondition, ...]
    source_status: SourceState
    provider_probe_count: int
    error: str | None
    retryable: bool
    reopen_required: bool


@dataclass(frozen=True, slots=True)
class OpenResult:
    status: Literal["opened", "missing", "conflict", "unavailable", "failed"]
    path: Path | None
    error: str | None
    retryable: bool


@dataclass(frozen=True, slots=True)
class SetupPreview:
    status: Literal["ready", "invalid", "unavailable", "conflict"]
    config_path: Path
    requested_vault: Path | None
    detected_vaults: tuple[Path, ...]
    daily_notes_dir: str
    daily_note_format: DailyNoteFormat
    timezone: str
    proposed_config_digest: str
    diagnostics: tuple[str, ...]
    writes: tuple[Path, ...]
    external_actions: tuple[str, ...]
    reason: str | None
    _config_digest: str | None = None
    _request_digest: str | None = None
    _config_bytes: bytes | None = None


@dataclass(frozen=True, slots=True)
class SetupSaveResult:
    status: Literal["saved", "already_saved", "conflict", "failed", "partial"]
    config_path: Path
    config_digest: str | None
    written_paths: tuple[Path, ...]
    diagnostics: tuple[str, ...]
    error: str | None
    retryable: bool


@dataclass(frozen=True, slots=True)
class ReportPreview:
    status: Literal["ready", "no_records", "unavailable", "conflict"]
    target_day: date
    snapshot: SourceSnapshot
    planned_updates: tuple[PlannedUpdate, ...]
    preconditions: tuple[Precondition, ...]
    provider_probe_count: int = 0
    reason: str | None = None
    _snapshot_digest: str = ""


@dataclass(frozen=True, slots=True)
class ReportExecutionResult:
    status: Literal["generated", "no_records", "unavailable", "conflict", "failed", "partial"]
    target_day: date
    written_path: Path | None
    applied_updates: tuple[PlannedUpdate, ...]
    pending_updates: tuple[PlannedUpdate, ...]
    precondition_failures: tuple[Precondition, ...]
    provider_probe_count: int
    error: str | None
    retryable: bool


@dataclass(frozen=True, slots=True)
class KznNoteSyncResult:
    status: Literal["not_requested", "synced", "missing", "conflict", "failed"]
    reason: str | None


@dataclass(frozen=True, slots=True)
class _Collected:
    state: SourceState
    snapshot: SourceSnapshot


def _target_day(cfg: Config, day: date | None, now: datetime | None) -> date:
    if day is not None:
        return day
    try:
        tz = ZoneInfo(cfg.timezone)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    point = now or datetime.now(tz)
    if point.tzinfo is None:
        point = point.replace(tzinfo=tz)
    return point.astimezone(tz).date()


def _empty_snapshot(cfg: Config, target: date, status: SourceState, *, rendered: str = "") -> SourceSnapshot:
    next_day = target.fromordinal(target.toordinal() + 1)
    return SourceSnapshot(
        target, next_day, cfg.timezone, (), None, (), {}, (), (), (), (), (),
        status.source_digest, 0, rendered, "",
    )


def _collect_source_snapshot(cfg: Config, target: date) -> _Collected:
    """Collect and freeze every input used by journal rendering and stats."""
    try:
        tz = ZoneInfo(cfg.timezone)
    except ZoneInfoNotFoundError:
        state = SourceState("unavailable", f"unknown timezone: {cfg.timezone}", "", datetime.now(timezone.utc))
        return _Collected(state, _empty_snapshot(cfg, target, state))
    start = datetime.combine(target, day_time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    try:
        client = ActivityWatchClient(cfg.aw_base_url)
        events, afk_available = collect_day(client, start, end)
        input_observation = collect_input_observation(client, start, end)
    except Exception as exc:
        state = SourceState("unavailable", type(exc).__name__, _digest((target.isoformat(), type(exc).__name__)), datetime.now(timezone.utc))
        return _Collected(state, _empty_snapshot(cfg, target, state))
    input_quality = {
        "state": input_observation.state,
        "reason": input_observation.reason,
        "bucket_id": input_observation.bucket_id,
        "last_event_at": input_observation.last_event_at,
    }
    try:
        history = tuple(load_stats(cfg.stats_path, days=8, end_day=target))
        prior = tuple(x for x in history if x.get("day") != target.isoformat())
        memory = tuple(load_entries(cfg.memory_path))
        runs = tuple(load_runs(cfg.logs_path))
        experiments = tuple(load_experiments(cfg.experiments_path))
        source_files: list[tuple[Path, str | None]] = []
        stats_dir = Path(cfg.stats_path)
        stats_candidates = sorted(stats_dir.glob("*.json")) if stats_dir.is_dir() else [stats_dir / f"{target.isoformat()}.json"]
        for path in (*stats_candidates, Path(cfg.logs_path) / "runs.jsonl", Path(cfg.memory_path) / "suggestions.jsonl"):
            source_files.append((path, _bytes_digest(path.read_bytes()) if path.is_file() else None))
        for exp in experiments:
            source_files.append((exp.path, _bytes_digest(exp.path.read_bytes()) if exp.path.is_file() else None))
    except Exception as exc:
        state = SourceState("unavailable", type(exc).__name__, _digest((target.isoformat(), type(exc).__name__)), datetime.now(timezone.utc))
        return _Collected(state, _empty_snapshot(cfg, target, state))
    note_revisions: list[tuple[str, int]] = []
    human_input_digests: list[tuple[str, Path, str | None]] = []
    human_days: list[tuple[str, HumanDay]] = []
    for candidate in (target, target + timedelta(days=1)):
        try:
            note = load_human_day(cfg, candidate)
            note_revisions.append((candidate.isoformat(), note.revision))
            human_raw = note.path.read_bytes() if note.path.is_file() else None
            human_input_digests.append((candidate.isoformat(), note.path, _bytes_digest(human_raw)))
            human_days.append((candidate.isoformat(), note))
        except Exception:
            planned_human = Path(cfg.vault_dir) / ".kaizenlog" / "human" / f"{candidate.isoformat()}.json"
            note_revisions.append((candidate.isoformat(), 0))
            human_input_digests.append((candidate.isoformat(), planned_human, None))
    if not events:
        state = SourceState("no_records", "no activity records for target day", _digest((target.isoformat(), input_quality, source_files)), datetime.now(timezone.utc))
        return _Collected(state, _empty_snapshot(cfg, target, state))
    try:
        classified = Classifier(cfg.rules).classify_all(events)
        summary = summarize(target, classified, gap_minutes=cfg.session_gap_minutes)
        input_stats = compute_input_stats(input_observation.events, day_start=start, day_end=end) if str(getattr(input_observation.state, "value", input_observation.state)) == "observed" else None
        ai_sessions: list[Any] = []
        day_prompts: list[Any] = []
        internal_ai_n = 0
        if cfg.aiwork.enabled:
            adapters = available_adapters(cfg)
            ai_sessions, day_prompts, internal_ai_n = collect_ai_telemetry(adapters, start, end)
        retry_chains = detect_retry_chains(day_prompts) if cfg.aiwork.enabled else []
        loop_tax = compute_loop_tax(retry_chains, ai_sessions, pricing=cfg.aiwork.pricing or None) if cfg.aiwork.enabled else None
        redactor = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)
        session_spans = build_session_spans(ai_sessions, redactor=redactor) if ai_sessions else []
        markdown = render_markdown(
            summary, tz,
            min_block_minutes=cfg.min_block_minutes,
            input_stats=input_stats,
            session_spans=session_spans,
            hide_private_titles=bool(getattr(cfg.privacy, "hide_private_titles", True)),
            private_title_patterns=tuple(getattr(cfg.privacy, "private_title_patterns", ())),
            stats_history=prior,
        )
        today_stats = build_stats(
            target, summary, ai_sessions, input_stats, activity_md=markdown,
            retry_chains=retry_chains, afk_watcher_available=afk_available,
            pricing=cfg.aiwork.pricing or None, title_redactor=redactor,
            internal_ai_sessions=internal_ai_n, loop_tax_summary=loop_tax,
            input_quality=input_quality,
        )
        stats_payload = json.dumps(today_stats, ensure_ascii=False, indent=1)
    except Exception as exc:
        state = SourceState("unavailable", type(exc).__name__, _digest((target.isoformat(), type(exc).__name__)), datetime.now(timezone.utc))
        return _Collected(state, _empty_snapshot(cfg, target, state))
    # The handoff written into target+1 is based on the activity just
    # collected for target, while the existing target-day actions remain the
    # human continuation input.
    prev_stats = today_stats
    target_path, target_raw, _ = _note_bytes(cfg, target)
    next_path, next_raw, _ = _note_bytes(cfg, target + timedelta(days=1))
    prev_actions = extract_section(target_raw.decode("utf-8") if target_raw else "", "kaizenlog:actions")
    existing_resume = extract_section(next_raw.decode("utf-8") if next_raw else "", RESUME_MARKER)
    resume = build_resume_section(prev_stats, prev_actions_content=prev_actions, existing_resume=existing_resume, tz=tz) or ""
    next_day = target + timedelta(days=1)
    source_digest = _digest((events, input_quality, ai_sessions, today_stats, history, memory, runs, experiments, note_revisions, human_input_digests, source_files, markdown, resume, cfg.timezone))
    state = SourceState("observed", None, source_digest, datetime.now(timezone.utc))
    snapshot = SourceSnapshot(
        target, next_day, cfg.timezone, summary, input_observation, tuple(ai_sessions), today_stats, prior,
        memory, runs, experiments, tuple(note_revisions), source_digest, 0, markdown, resume,
        tuple(human_input_digests), tuple(source_files), stats_payload, input_quality, afk_available,
        f"journal-{target.isoformat()}-{source_digest[:12]}", tuple(human_days), _digest(cfg),
    )
    return _Collected(state, snapshot)


def _note_bytes(cfg: Config, day: date) -> tuple[Path | None, bytes | None, str | None]:
    try:
        resolution = resolve_daily_note(cfg, day)
    except Exception as exc:
        return None, None, type(exc).__name__
    if resolution.status in {"conflict", "unsupported"} or resolution.path is None:
        return None, None, resolution.reason
    path = resolution.path
    try:
        return path, path.read_bytes() if path.is_file() else None, None
    except OSError as exc:
        return path, None, type(exc).__name__


def _planned(cfg: Config, snapshot: SourceSnapshot) -> tuple[tuple[PlannedUpdate, ...], tuple[Precondition, ...]]:
    updates: list[PlannedUpdate] = []
    preconditions: list[Precondition] = []
    for target, day, region, body in (
        ("target_day", snapshot.target_day, ACTIVITY_MARKER, getattr(snapshot, "rendered_markdown", "")),
        ("next_day", snapshot.next_day, RESUME_MARKER, getattr(snapshot, "next_markdown", "")),
    ):
        path, raw, error = _note_bytes(cfg, day)
        digest = _bytes_digest(raw)
        if error:
            path = None
        base = raw.decode("utf-8") if raw is not None else default_frontmatter(day) + "\n"
        new_text = upsert_section(base, region, body, position="bottom")
        new_bytes = new_text.encode("utf-8")
        updates.append(PlannedUpdate(target, path, "upsert_section", region, None, digest, _bytes_digest(new_bytes), "journal managed section", False, None, raw, new_bytes))
        preconditions.append(Precondition(target, path, "file_bytes", raw is not None, digest, None, digest, None, None, raw))
    stats_path = Path(cfg.stats_path) / f"{snapshot.target_day.isoformat()}.json"
    stats_raw = stats_path.read_bytes() if stats_path.is_file() else None
    stats_payload = getattr(snapshot, "stats_payload", None)
    if stats_payload is not None:
        stats_bytes = stats_payload.encode("utf-8")
        updates.append(PlannedUpdate("stats", stats_path, "replace_json", None, None, _bytes_digest(stats_raw), _bytes_digest(stats_bytes), "frozen daily stats", False, None, stats_raw, stats_bytes))
        preconditions.append(Precondition("stats", stats_path, "file_bytes", stats_raw is not None, _bytes_digest(stats_raw), None, _bytes_digest(stats_raw), None, None, stats_raw))
    ledger_path = cfg.operational_db_path
    if ledger_path is not None:
        row_id = snapshot.ledger_run_id or f"journal-{snapshot.target_day.isoformat()}-{snapshot.source_digest[:12]}"
        ledger = OpsLedger(ledger_path)
        ledger_snapshot = ledger.readonly_snapshot()
        existing_digest = ledger_snapshot.row_digests.get(row_id) if ledger_snapshot.available else None
        updates.append(PlannedUpdate("ledger", ledger_path, "sqlite_transaction", None, None, existing_digest, None, "operational receipt", False, row_id))
        preconditions.append(Precondition("ledger", ledger_path, "sqlite_row_digest", row_id in ledger_snapshot.row_digests if ledger_snapshot.available else False, existing_digest, None, existing_digest, None, row_id, None))
    else:
        log_path = Path(cfg.logs_path) / "runs.jsonl"
        log_raw = log_path.read_bytes() if log_path.is_file() else None
        updates.append(PlannedUpdate("runlog", log_path, "append_jsonl", None, None, _bytes_digest(log_raw), None, "operational receipt JSONL", False, None, log_raw, None))
        preconditions.append(Precondition("runlog", log_path, "file_bytes", log_raw is not None, _bytes_digest(log_raw), None, _bytes_digest(log_raw), None, None, log_raw))
    for target, path, digest in getattr(snapshot, "human_input_digests", ()):
        raw = path.read_bytes() if path.is_file() else None
        preconditions.append(Precondition("human", path, "file_bytes", digest is not None, digest, None, _bytes_digest(raw), None, None, raw))
    for path, digest in getattr(snapshot, "source_files", ()):
        raw = path.read_bytes() if path.is_file() else None
        preconditions.append(Precondition("source", path, "file_bytes", digest is not None, digest, None, _bytes_digest(raw), None, None, raw))
    for directory in (Path(cfg.stats_path), Path(cfg.logs_path), Path(cfg.memory_path), Path(cfg.experiments_path)):
        preconditions.append(Precondition("source_directory", directory, "directory_entry", directory.is_dir(), None, None, None, None))
    preconditions.append(Precondition("config", None, "json_revision", True, snapshot.effective_config_digest, None, _digest(cfg), None))
    return tuple(updates), tuple(preconditions)


def sample_journal(day: date | None = None) -> SampleJournal:
    target = day or date(2026, 1, 15)
    next_day = target.fromordinal(target.toordinal() + 1)
    tz = ZoneInfo("Asia/Tokyo")
    event = ActivityEvent(datetime.combine(target, day_time(9, 0), tzinfo=tz), datetime.combine(target, day_time(10, 15), tzinfo=tz), "Code.exe", "journal_service.py")
    summary = summarize(target, Classifier(Config().rules).classify_all([event]), gap_minutes=5.0)
    input_stats = InputStats(keypresses=248, clicks=31, active_input_minutes=52.0)
    markdown = render_markdown(summary, tz, min_block_minutes=3.0, input_stats=input_stats)
    stats = build_stats(
        target, summary, (), input_stats, activity_md=markdown,
        input_quality={"state": "observed", "reason": None, "bucket_id": "sample-input"},
    )
    markdown = (
        "## 📓 Sample Journal\n\n"
        "これは固定fixtureです。実測データやMemoryには保存されません。\n\n"
        + markdown
        + "\n### 📈 Sample Stats\n\n"
        + f"- アクティブ時間: {stats['total_minutes']:.1f}分\n"
        + f"- キー入力: {stats['source_quality']['input']['state']} / {input_stats.keypresses}回\n"
    )
    return SampleJournal("sample", f"fixed-{target.isoformat()}", target, next_day, "Asia/Tokyo", markdown, (), 0, ())


def preview_journal(cfg: Config, day: date | None = None, *, now: datetime | None = None) -> JournalPreview:
    target = _target_day(cfg, day, now)
    collected = _collect_source_snapshot(cfg, target)
    try:
        updates, preconditions = _planned(cfg, collected.snapshot)
    except Exception as exc:
        return JournalPreview(
            "unavailable", target, collected.snapshot.next_day, cfg.timezone,
            collected.snapshot, _digest(collected.snapshot), (), (),
            SourceState("unavailable", type(exc).__name__, collected.state.source_digest, datetime.now(timezone.utc)),
            0, False, type(exc).__name__,
        )
    status = "ready" if collected.state.status == "observed" else collected.state.status
    reason = collected.state.reason
    if any(update.target in {"target_day", "next_day"} and update.path is None for update in updates):
        status = "conflict"
        reason = reason or "daily note resolver could not select a path"
    ledger_path = cfg.operational_db_path
    if ledger_path is not None:
        wal = Path(f"{ledger_path}-wal")
        rollback = Path(f"{ledger_path}-journal")
        if wal.is_file() and wal.stat().st_size > 0:
            status = "unavailable"
            reason = "ledger WAL is active; retry after it is quiescent"
        elif rollback.is_file() and rollback.stat().st_size > 0:
            status = "unavailable"
            reason = "ledger rollback journal is active; retry after it is quiescent"
        elif ledger_path.is_file():
            ledger_state = OpsLedger(ledger_path).readonly_snapshot()
            if not ledger_state.available:
                status = "unavailable"
                reason = ledger_state.reason
    return JournalPreview(
        status, target, collected.snapshot.next_day, cfg.timezone, collected.snapshot,
        _digest(collected.snapshot), updates, preconditions, collected.state, 0, False,
        reason,
    )


def _check_preconditions(
    cfg: Config,
    preview: JournalPreview,
    *,
    output_states: Mapping[str, OutputState] | None = None,
) -> tuple[Precondition, ...]:
    failures: list[Precondition] = []
    for expected in preview.preconditions:
        if expected.target in {"target_day", "next_day", "stats", "ledger", "runlog"}:
            continue
        if expected.target == "source" and output_states is not None:
            stats_update = next((item for item in preview.planned_updates if item.target == "stats"), None)
            if stats_update is not None and expected.path == stats_update.path and output_states.get("stats") == "after":
                continue
            runlog_update = next((item for item in preview.planned_updates if item.target == "runlog"), None)
            if runlog_update is not None and expected.path == runlog_update.path and output_states.get("runlog") == "after":
                continue
        if expected.target == "source_directory" and output_states is not None:
            if expected.path == Path(cfg.stats_path) and output_states.get("stats") == "after":
                continue
            if expected.path == Path(cfg.logs_path) and output_states.get("runlog") == "after":
                continue
        if expected.kind == "file_bytes":
            actual = None
            if expected.path is not None and expected.path.is_file():
                try:
                    actual = expected.path.read_bytes()
                except OSError:
                    actual = None
            observed = _bytes_digest(actual)
            if observed != expected.expected_digest or (actual is not None) != expected.exists:
                failures.append(Precondition(expected.target, expected.path, expected.kind, expected.exists, expected.expected_digest, expected.expected_revision, observed, None, expected.row_id, expected.expected_bytes))
        elif expected.kind == "sqlite_row_digest" and expected.path is not None:
            current = OpsLedger(expected.path).readonly_snapshot()
            observed = current.row_digests.get(expected.row_id or "") if current.available else None
            if (not current.available and expected.path.is_file()) or observed != expected.expected_digest or (observed is not None) != expected.exists:
                failures.append(Precondition(expected.target, expected.path, expected.kind, expected.exists, expected.expected_digest, expected.expected_revision, observed, None, expected.row_id, expected.expected_bytes))
        elif expected.kind == "directory_entry" and expected.path is not None:
            observed_exists = expected.path.is_dir()
            if observed_exists != expected.exists:
                failures.append(Precondition(expected.target, expected.path, expected.kind, expected.exists, expected.expected_digest, expected.expected_revision, None, None, expected.row_id, expected.expected_bytes))
        elif expected.kind == "json_revision" and expected.target == "config":
            observed = _digest(cfg)
            if observed != expected.expected_digest:
                failures.append(Precondition(expected.target, expected.path, expected.kind, expected.exists, expected.expected_digest, expected.expected_revision, observed, None, expected.row_id, expected.expected_bytes))
    return tuple(failures)


def _write_file_cas(path: Path, expected: bytes | None, new: bytes) -> None:
    with locked_read_modify_write(path):
        current = path.read_bytes() if path.is_file() else None
        if current != expected:
            raise RuntimeError(f"file changed during write: {path}")
        atomic_write_text(path, new.decode("utf-8"), newline="")


def _write_note_cas(cfg: Config, update: PlannedUpdate, day: date, body: str) -> None:
    if update.path is None:
        raise RuntimeError(update.reason)
    store = DailyNoteStore(cfg)
    with store.locked_note(day, expected_bytes=update.expected_bytes) as (path, content):
        if path.resolve(strict=False) != update.path.resolve(strict=False):
            raise RuntimeError("daily note resolver changed after preview")
        current = content if content is not None else default_frontmatter(day) + "\n"
        atomic_write_text(path, upsert_section(current, update.managed_region or ACTIVITY_MARKER, body, position="bottom"))


OutputState = Literal["before", "after", "changed"]


def _runtime_receipt_valid(payload: Mapping[str, Any], preview: JournalPreview) -> bool:
    """Validate receipt fields that are fixed by the execute operation."""
    if payload.get("run_id") != preview.snapshot.ledger_run_id:
        return False
    if payload.get("command") != "journal" or payload.get("snapshot_digest") != preview.snapshot_digest:
        return False
    if payload.get("ok") is not True or payload.get("outcome") != "generated":
        return False
    timestamp = payload.get("ts")
    if not isinstance(timestamp, str):
        return False
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return False
    try:
        duration = float(payload.get("duration_seconds"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(duration) and duration >= 0.0


_SQLITE_RECEIPT_KEYS = frozenset({
    "schema_version", "run_id", "parent_run_id", "ts", "command", "ok",
    "partial", "duration_seconds", "configured_backend", "actual_backend",
    "outcome", "reason_code", "notify_failed", "snapshot_digest",
})


def _canonical_sqlite_receipt_valid(payload: Mapping[str, Any], preview: JournalPreview) -> bool:
    """Recognize only the complete receipt shape written by this service."""
    if set(payload) != _SQLITE_RECEIPT_KEYS:
        return False
    if payload.get("schema_version") != 2:
        return False
    if payload.get("parent_run_id") is not None:
        return False
    if payload.get("partial") is not False or payload.get("notify_failed") is not False:
        return False
    if payload.get("configured_backend") is not None or payload.get("actual_backend") is not None:
        return False
    if payload.get("reason_code") is not None:
        return False
    return _runtime_receipt_valid(payload, preview)


def _classify_jsonl_receipt(update: PlannedUpdate, preview: JournalPreview) -> OutputState:
    if update.path is None:
        return "changed"
    expected = update.expected_bytes or b""
    try:
        current = update.path.read_bytes() if update.path.is_file() else b""
    except OSError:
        return "changed"
    if current == expected:
        return "before"
    if current is None or not current.startswith(expected):
        return "changed"
    remainder = current[len(expected):]
    if expected and not expected.endswith((b"\n", b"\r")):
        if not remainder.startswith(b"\n"):
            return "changed"
        remainder = remainder[1:]
    if not remainder.endswith(b"\n"):
        return "changed"
    lines = remainder.splitlines(keepends=True)
    if len(lines) != 1:
        return "changed"
    try:
        payload = json.loads(lines[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "changed"
    return "after" if isinstance(payload, dict) and _runtime_receipt_valid(payload, preview) else "changed"


def _classify_sqlite_receipt(update: PlannedUpdate, preview: JournalPreview) -> tuple[OutputState, str | None]:
    if update.path is None or update.row_id is None:
        return "changed", "ledger receipt has no path or row identity"
    if not update.path.is_file():
        return ("before", None) if not update.expected_digest else ("changed", "ledger database disappeared")
    snapshot = OpsLedger(update.path).readonly_snapshot()
    if not snapshot.available:
        return "changed", snapshot.reason or "ledger readonly snapshot unavailable"
    row_digest = snapshot.row_digests.get(update.row_id)
    row = next((item for item in snapshot.rows if item.get("run_id") == update.row_id), None)
    if row is not None and _canonical_sqlite_receipt_valid(row, preview):
        return "after", None
    if update.expected_digest is None and row is None:
        return "before", None
    if row_digest == update.expected_digest and row is not None:
        return "before", None
    return "changed", "ledger receipt changed"


def _classify_outputs(preview: JournalPreview) -> tuple[dict[str, OutputState], str | None]:
    states: dict[str, OutputState] = {}
    reason: str | None = None
    for update in preview.planned_updates:
        if update.action in {"upsert_section", "replace_json"}:
            if update.path is None:
                states[update.target] = "changed"
                reason = reason or f"{update.target} output has no resolved path"
                continue
            try:
                current = update.path.read_bytes() if update.path.is_file() else None
            except OSError:
                current = None
            if current == update.expected_bytes:
                states[update.target] = "before"
            elif current == update.new_bytes:
                states[update.target] = "after"
            else:
                states[update.target] = "changed"
                reason = reason or f"{update.target} output changed"
        elif update.action == "append_jsonl":
            states[update.target] = _classify_jsonl_receipt(update, preview)
            if states[update.target] == "changed":
                reason = reason or "JSONL receipt changed"
        elif update.action == "sqlite_transaction":
            states[update.target], receipt_reason = _classify_sqlite_receipt(update, preview)
            reason = reason or receipt_reason
    return states, reason


def _result_for_preview(preview: JournalPreview, status: str, *, error: str | None = None, failures: tuple[Precondition, ...] = ()) -> JournalExecutionResult:
    return JournalExecutionResult(
        status, preview.target_day, preview.snapshot_digest, preview.planned_updates, (), preview.planned_updates,
        failures, preview.source_status, preview.provider_probe_count, error, bool(failures), False,
    )


def execute_journal(cfg: Config, preview: JournalPreview) -> JournalExecutionResult:
    if preview.status == "unavailable":
        return _result_for_preview(preview, "unavailable", error=preview.reason)
    if preview.status == "no_records":
        return _result_for_preview(preview, "no_records", error=preview.reason)
    if preview.status == "conflict":
        return _result_for_preview(preview, "conflict", error=preview.reason)
    if _digest(preview.snapshot) != preview.snapshot_digest:
        return _result_for_preview(preview, "conflict", error="preview snapshot changed")
    output_states, state_reason = _classify_outputs(preview)
    if any(state == "changed" for state in output_states.values()):
        return _result_for_preview(preview, "conflict", error=state_reason or "planned output changed")
    failures = _check_preconditions(cfg, preview, output_states=output_states)
    if failures:
        return _result_for_preview(preview, "conflict", error="precondition changed", failures=failures)
    if all(state == "after" for state in output_states.values()):
        return JournalExecutionResult(
            "already_generated", preview.target_day, preview.snapshot_digest,
            preview.planned_updates, preview.planned_updates, (), (), preview.source_status,
            preview.provider_probe_count, None, False, False,
        )
    applied: list[PlannedUpdate] = []
    pending = list(preview.planned_updates)
    execution_started = time.monotonic()
    try:
        for update in preview.planned_updates:
            if output_states.get(update.target) == "after":
                applied.append(update)
                pending.remove(update)
                continue
            if update.action == "upsert_section":
                body = getattr(preview.snapshot, "rendered_markdown", "") if update.target == "target_day" else getattr(preview.snapshot, "next_markdown", "")
                _write_note_cas(cfg, update, preview.target_day if update.target == "target_day" else preview.next_day, body)
            elif update.action == "replace_json":
                assert update.path is not None and update.new_bytes is not None
                _write_file_cas(update.path, update.expected_bytes, update.new_bytes)
            elif update.action == "sqlite_transaction":
                assert update.path is not None
                entry = {
                    "schema_version": 2,
                    "run_id": update.row_id or f"journal-{preview.target_day.isoformat()}-{preview.snapshot_digest[:12]}",
                    "parent_run_id": None,
                    "ts": datetime.now(timezone.utc).isoformat(), "command": "journal",
                    "ok": True, "partial": False,
                    "duration_seconds": time.monotonic() - execution_started,
                    "configured_backend": None, "actual_backend": None,
                    "outcome": "generated", "reason_code": None,
                    "notify_failed": False, "snapshot_digest": preview.snapshot_digest,
                }
                condition = next((p for p in preview.preconditions if p.target == "ledger"), None)
                if not OpsLedger(update.path).append_if_payload_digest(entry, expected_digest=condition.expected_digest if condition else None, expected_exists=condition.exists if condition else False):
                    raise RuntimeError("operational receipt changed")
            elif update.action == "append_jsonl":
                assert update.path is not None
                entry = {
                    "schema_version": 2, "run_id": preview.snapshot.ledger_run_id,
                    "ts": datetime.now(timezone.utc).isoformat(), "command": "journal",
                    "ok": True, "duration_seconds": time.monotonic() - execution_started,
                    "outcome": "generated", "snapshot_digest": preview.snapshot_digest,
                }
                current = update.expected_bytes or b""
                payload = current + (b"" if not current or current.endswith((b"\n", b"\r")) else b"\n") + (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
                _write_file_cas(update.path, update.expected_bytes, payload)
            applied.append(update)
            pending.remove(update)
    except Exception as exc:
        return JournalExecutionResult(
            "partial" if applied else "failed", preview.target_day, preview.snapshot_digest,
            preview.planned_updates, tuple(applied), tuple(pending), (), preview.source_status,
            preview.provider_probe_count, type(exc).__name__, True, any(u.target == "target_day" for u in applied),
        )
    return JournalExecutionResult(
        "generated", preview.target_day, preview.snapshot_digest, preview.planned_updates,
        tuple(applied), tuple(pending), (), preview.source_status, preview.provider_probe_count,
        None, False, False,
    )


def open_daily_note(cfg: Config, day: date) -> OpenResult:
    path, raw, error = _note_bytes(cfg, day)
    if error:
        return OpenResult("conflict", path, error, False)
    if path is None or raw is None:
        return OpenResult("missing", path, "daily note is missing", False)
    try:
        adapter = getattr(cfg, "open_adapter", None)
        if callable(adapter):
            adapter(path)
        elif hasattr(os, "startfile"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            return OpenResult("unavailable", path, "no open adapter", True)
    except Exception as exc:
        return OpenResult("failed", path, type(exc).__name__, True)
    return OpenResult("opened", path, None, False)


def preview_initial_setup(opts: SetupOptions, *, daily_notes_dir: str | None, daily_note_format: DailyNoteFormat, backend: str, model: str) -> SetupPreview:
    from . import setup_detect
    from .config import default_config_path
    from .note_paths import validate_daily_notes_dir
    config_path = Path(opts.config_path).expanduser() if getattr(opts, "config_path", None) else default_config_path()
    requested = Path(opts.vault).expanduser() if getattr(opts, "vault", None) is not None else None
    detected = tuple(setup_detect.detect_vault_candidates(existing=requested))
    configured_dir = daily_notes_dir if daily_notes_dir is not None else "01 Daily Notes"
    try:
        current_config_bytes = config_path.read_bytes() if config_path.is_file() else None
    except OSError as exc:
        return SetupPreview("unavailable", config_path, requested, detected, configured_dir, daily_note_format, "Asia/Tokyo", "", (), (), (), type(exc).__name__, None, None, None)
    current_config_digest = _bytes_digest(current_config_bytes)
    diagnostics: list[str] = []
    if config_path.exists() and not config_path.is_file():
        diagnostics.append("config_path is not a file")
    if daily_note_format not in {"YYYY-MM-DD", "YYYYMMDD", "YYYY/MM/DD"}:
        diagnostics.append("unsupported daily_note_format")
    if backend not in {"none", "claude-code-cli", "copilot-cli", "openai-compatible"}:
        diagnostics.append("unsupported backend")
    if requested is None or not requested.is_dir():
        diagnostics.append("vault is required")
    elif not config_path.exists():
        diagnostics.append("config will be created")
    try:
        if requested is not None and requested.is_dir():
            validate_daily_notes_dir(requested, configured_dir)
    except ValueError as exc:
        diagnostics.append(str(exc))
    if diagnostics and any(item != "config will be created" for item in diagnostics):
        return SetupPreview("invalid", config_path, requested, detected, configured_dir, daily_note_format, "Asia/Tokyo", "", tuple(diagnostics), (), (), "; ".join(diagnostics), current_config_digest, None, current_config_bytes)
    payload = {"vault": str(requested.resolve()), "daily_notes_dir": configured_dir, "daily_note_format": daily_note_format, "backend": backend, "model": model}
    request_digest = _digest({"config_path": str(config_path.resolve()), **payload})
    detected_dir = setup_detect.detect_daily_notes_dir(requested)
    if detected_dir is not None and daily_notes_dir is None:
        diagnostics.append(f"detected daily notes directory: {detected_dir}")
    return SetupPreview("ready", config_path, requested, detected, str(payload["daily_notes_dir"]), daily_note_format, "Asia/Tokyo", _digest(payload), tuple(diagnostics), (config_path,), (), None, current_config_digest, request_digest, current_config_bytes)


def save_initial_setup(opts: SetupOptions, preview: SetupPreview, *, daily_notes_dir: str | None, daily_note_format: DailyNoteFormat, backend: str, model: str) -> SetupSaveResult:
    if preview.status != "ready" or preview.requested_vault is None:
        return SetupSaveResult("failed", preview.config_path, None, (), preview.diagnostics, preview.reason, False)
    from .config import write_config_file
    try:
        configured_path = Path(opts.config_path).expanduser() if opts.config_path is not None else preview.config_path
        if configured_path.resolve() != preview.config_path.resolve():
            return SetupSaveResult("conflict", preview.config_path, None, (), (), "config path changed after preview", True)
        requested = Path(opts.vault).expanduser() if getattr(opts, "vault", None) is not None else None
        if requested is None or requested.resolve() != preview.requested_vault.resolve():
            return SetupSaveResult("conflict", preview.config_path, None, (), (), "setup selection changed", True)
        configured_dir = daily_notes_dir if daily_notes_dir is not None else "01 Daily Notes"
        request_digest = _digest({
            "config_path": str(preview.config_path.resolve()), "vault": str(requested.resolve()),
            "daily_notes_dir": configured_dir, "daily_note_format": daily_note_format,
            "backend": backend, "model": model,
        })
        if preview._request_digest is not None and request_digest != preview._request_digest:
            return SetupSaveResult("conflict", preview.config_path, None, (), (), "setup options changed", True)
        current_bytes = preview.config_path.read_bytes() if preview.config_path.is_file() else None
        current_digest = _bytes_digest(current_bytes)
        if current_digest != preview._config_digest:
            return SetupSaveResult("conflict", preview.config_path, None, (), (), "config changed after preview", True)
        if current_bytes != preview._config_bytes:
            return SetupSaveResult("conflict", preview.config_path, None, (), (), "config changed after preview", True)
        expected_bytes = current_bytes
        write_config_file(preview.config_path, preview.requested_vault, backend, model, merge=preview.config_path.is_file(), daily_notes_dir=daily_notes_dir, daily_note_format=daily_note_format, expected_bytes=expected_bytes)
        digest = _bytes_digest(preview.config_path.read_bytes()) if preview.config_path.is_file() else None
        return SetupSaveResult("saved", preview.config_path, digest, (preview.config_path,), (), None, False)
    except ValueError as exc:
        return SetupSaveResult("conflict", preview.config_path, None, (), (), str(exc), True)
    except Exception as exc:
        return SetupSaveResult("failed", preview.config_path, None, (), (), type(exc).__name__, True)


def _extract_prepared_intent(content: str) -> str | None:
    parts: list[str] = []
    for heading in ("Today's Focus", "Tasks"):
        section = extract_heading_section(content, heading)
        if section:
            parts.append(f"## {heading}\n{section}")
    return "\n\n".join(parts) or None


def _prepared_open_actions(entries: Sequence[Any], target: date, note_content: str) -> tuple[tuple[str, str], ...]:
    checked: set[str] = set()
    action_section = extract_section(note_content, "kaizenlog:actions") or ""
    for line in action_section.splitlines():
        if line.lstrip().startswith(("- [x]", "- [X]")):
            match = ID_PATTERN.search(line)
            if match:
                checked.add(match.group(0))
    result: list[tuple[str, str]] = []
    for entry in open_actions_in_window(list(entries), target, window_days=7):
        if entry.id in checked:
            continue
        body = humanize_action_body(entry.action)
        if body:
            result.append((entry.id, body))
        if len(result) >= 2:
            break
    return tuple(result)


def preview_report_no_llm(cfg: Config, day: date | None = None) -> ReportPreview:
    target = _target_day(cfg, day, None)
    path, raw, error = _note_bytes(cfg, target)
    if error:
        state = SourceState("unavailable", error, "", datetime.now(timezone.utc))
        snapshot = _empty_snapshot(cfg, target, state)
        return ReportPreview("unavailable", target, snapshot, (), (), 0, error)
    try:
        history = tuple(load_stats(cfg.stats_path, days=8, end_day=target))
    except Exception as exc:
        state = SourceState("unavailable", type(exc).__name__, "", datetime.now(timezone.utc))
        snapshot = _empty_snapshot(cfg, target, state)
        return ReportPreview("unavailable", target, snapshot, (), (), 0, state.reason)
    stats = tuple(item for item in history if item.get("day") == target.isoformat())
    if not stats:
        state = SourceState("no_records", "daily stats are missing", _digest((target.isoformat(), "stats-missing")), datetime.now(timezone.utc))
        snapshot = _empty_snapshot(cfg, target, state)
        return ReportPreview("no_records", target, snapshot, (), (), 0, state.reason)
    try:
        tz = ZoneInfo(cfg.timezone)
        content = raw.decode("utf-8") if raw is not None else ""
        activity = extract_section(content, ACTIVITY_MARKER)
        intent = _extract_prepared_intent(content)
        memory = tuple(load_entries(cfg.memory_path))
        open_actions = _prepared_open_actions(memory, target, content)
        human_day = load_human_day(cfg, target)
        section = generate_nippou_deterministic(
            stats[0], tz, intent, private_title_patterns=tuple(getattr(cfg.privacy, "private_title_patterns", ())),
            min_block_minutes=cfg.min_block_minutes, open_kzn_actions=open_actions, activity_md=activity,
            human_day=human_day,
        )
    except Exception as exc:
        state = SourceState("unavailable", type(exc).__name__, _digest((target.isoformat(), type(exc).__name__)), datetime.now(timezone.utc))
        snapshot = _empty_snapshot(cfg, target, state)
        return ReportPreview("unavailable", target, snapshot, (), (), 0, state.reason)
    next_day = target.fromordinal(target.toordinal() + 1)
    human_path = human_day.path
    try:
        human_raw = human_path.read_bytes() if human_path.is_file() else None
        stats_path = Path(cfg.stats_path) / f"{target.isoformat()}.json"
        memory_path = Path(cfg.memory_path) / "suggestions.jsonl"
        source_files = (
            (stats_path, _bytes_digest(stats_path.read_bytes()) if stats_path.is_file() else None),
            (memory_path, _bytes_digest(memory_path.read_bytes()) if memory_path.is_file() else None),
        )
    except OSError as exc:
        state = SourceState("unavailable", type(exc).__name__, "", datetime.now(timezone.utc))
        snapshot = _empty_snapshot(cfg, target, state)
        return ReportPreview("unavailable", target, snapshot, (), (), 0, state.reason)
    try:
        base = raw.decode("utf-8") if raw is not None else default_frontmatter(target) + "\n"
        new_bytes = upsert_section(base, NIPPOU_MARKER, section, position="bottom").encode("utf-8")
        stats_raw = stats_path.read_bytes() if stats_path.is_file() else None
    except (OSError, UnicodeError, ValueError) as exc:
        state = SourceState("unavailable", type(exc).__name__, "", datetime.now(timezone.utc))
        snapshot = _empty_snapshot(cfg, target, state)
        return ReportPreview("unavailable", target, snapshot, (), (), 0, state.reason)
    if path is None:
        state = SourceState("unavailable", "daily note resolver could not select a path", "", datetime.now(timezone.utc))
        snapshot = _empty_snapshot(cfg, target, state)
        return ReportPreview("conflict", target, snapshot, (), (), 0, state.reason)
    snapshot = SourceSnapshot(
        target_day=target,
        next_day=next_day,
        timezone=cfg.timezone,
        activity=activity or "",
        input=None,
        ai_sessions=(),
        current_stats=stats[0],
        stats_history=history,
        memory_entries=memory,
        runlog_entries=(),
        experiments=(),
        note_revisions=(),
        source_digest=_digest((stats[0], history, activity, intent, open_actions, raw, human_raw, source_files, _digest(cfg))),
        rendered_markdown=section,
        human_input_digests=((target.isoformat(), human_path, _bytes_digest(human_raw)),),
        source_files=source_files,
        human_days=((target.isoformat(), human_day),),
        effective_config_digest=_digest(cfg),
        prepared_intent=intent,
        prepared_open_actions=open_actions,
    )
    update = PlannedUpdate("target_day", path, "upsert_section", NIPPOU_MARKER, None, _bytes_digest(raw), _bytes_digest(new_bytes), "deterministic no-LLM report", False, None, raw, new_bytes)
    precondition = Precondition("target_day", path, "file_bytes", raw is not None, _bytes_digest(raw), None, _bytes_digest(raw), None, None, raw)
    stats_pre = Precondition("stats", stats_path, "file_bytes", stats_raw is not None, _bytes_digest(stats_raw), None, _bytes_digest(stats_raw), None, None, stats_raw)
    human_pre = Precondition("human", human_path, "file_bytes", human_raw is not None, _bytes_digest(human_raw), None, _bytes_digest(human_raw), None, None, human_raw)
    source_pre = tuple(
        # The digest was captured with the frozen report snapshot.  Do not
        # reread a source file here: a concurrent delete/read failure must be
        # handled by execute's typed precondition result, not escape preview.
        Precondition("source", source_path, "file_bytes", source_digest is not None, source_digest, None, source_digest, None)
        for source_path, source_digest in source_files
    )
    config_pre = Precondition("config", None, "json_revision", True, snapshot.effective_config_digest, None, _digest(cfg), None)
    return ReportPreview("ready", target, snapshot, (update,), (precondition, stats_pre, human_pre, *source_pre, config_pre), 0, None, _digest(snapshot))


def execute_report_no_llm(cfg: Config, preview: ReportPreview) -> ReportExecutionResult:
    if preview.status in {"unavailable", "no_records"}:
        return ReportExecutionResult(preview.status, preview.target_day, None, (), preview.planned_updates, (), 0, preview.reason, True)
    if _digest(preview.snapshot) != preview._snapshot_digest:
        return ReportExecutionResult("conflict", preview.target_day, None, (), preview.planned_updates, (), 0, "preview snapshot changed", True)
    failures = _check_preconditions(cfg, JournalPreview(
        preview.status, preview.target_day, preview.target_day, preview.snapshot.timezone,
        preview.snapshot, preview._snapshot_digest, preview.planned_updates,
        preview.preconditions, SourceState("observed", None, preview.snapshot.source_digest, datetime.now(timezone.utc)),
    ))
    if failures:
        return ReportExecutionResult("conflict", preview.target_day, None, (), preview.planned_updates, failures, 0, "precondition changed", True)
    update = preview.planned_updates[0] if preview.planned_updates else None
    if update is None:
        return ReportExecutionResult("failed", preview.target_day, None, (), (), (), 0, "no planned report update", False)
    try:
        update = preview.planned_updates[0]
        _write_note_cas(cfg, update, preview.target_day, preview.snapshot.rendered_markdown)
        path = update.path
    except Exception as exc:
        return ReportExecutionResult("failed", preview.target_day, None, (), preview.planned_updates, (), 0, type(exc).__name__, True)
    return ReportExecutionResult("generated", preview.target_day, path, (update,), (), (), 0, None, False)


def sync_kzn_completion_note(cfg: Config, day: date) -> KznNoteSyncResult:
    """Refresh the existing actions section from a read-only Memory snapshot."""
    path, raw, error = _note_bytes(cfg, day)
    if error:
        return KznNoteSyncResult("conflict", error)
    if path is None or raw is None:
        return KznNoteSyncResult("missing", "daily note is missing")
    try:
        with memory_lock(cfg.memory_path):
            entries = load_entries(cfg.memory_path)
            content = raw.decode("utf-8")
            body = render_actions_section(entries, day, note_content=content, stats_history=load_stats(cfg.stats_path, days=8, end_day=day))
            if body is None:
                return KznNoteSyncResult("not_requested", "no open actions")
            with DailyNoteStore(cfg).locked_note(day, expected_bytes=raw) as (current_path, current_content):
                if current_path.resolve(strict=False) != path.resolve(strict=False):
                    return KznNoteSyncResult("conflict", "daily note resolver changed")
                if current_content is None:
                    return KznNoteSyncResult("missing", "daily note is missing")
                if extract_section(current_content, "kaizenlog:actions") == body.strip():
                    return KznNoteSyncResult("synced", None)
                atomic_write_text(current_path, upsert_section(current_content, "kaizenlog:actions", body, position="bottom"))
        return KznNoteSyncResult("synced", None)
    except ValueError as exc:
        return KznNoteSyncResult("conflict", str(exc))
    except Exception as exc:
        return KznNoteSyncResult("failed", type(exc).__name__)


__all__ = [
    "SampleJournal", "SourceState", "SourceSnapshot", "PlannedUpdate", "Precondition",
    "JournalPreview", "JournalExecutionResult", "OpenResult", "SetupPreview", "SetupSaveResult",
    "ReportPreview", "ReportExecutionResult", "KznNoteSyncResult", "sample_journal",
    "preview_initial_setup", "save_initial_setup", "preview_journal", "execute_journal",
    "open_daily_note", "preview_report_no_llm", "execute_report_no_llm", "sync_kzn_completion_note",
]
