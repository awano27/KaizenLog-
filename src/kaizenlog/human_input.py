"""Append-only human input and crash-safe projection into daily notes."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping

from .file_lock import file_lock, lock_path_for
from .memory import MemoryEntry, load_entries, resolve_action_id
from .vault import HUMAN_INPUT_MARKER, atomic_write_text

if TYPE_CHECKING:
    from .config import Config


HumanKind = Literal["outcome", "next_action", "classification_correction", "cancel"]
HumanStatus = Literal["missing", "loaded", "invalid"]
ProjectionState = Literal["none", "pending", "applied", "conflict"]

_KINDS = frozenset(("outcome", "next_action", "classification_correction", "cancel"))
_OPERATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$", re.ASCII)
_HEX64 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_TOP_KEYS = frozenset(
    ("schema_version", "day", "updated_at_utc", "revision", "history", "projection")
)
_PROJECTION_KEYS = frozenset(("state", "projected_revision", "digest"))
_OP_KEYS = frozenset(
    ("operation_id", "target_day", "kind", "payload", "target_key", "cancel_operation_id")
)
_RECORD_KEYS = frozenset(("operation", "recorded_at_utc"))


@dataclass(frozen=True, slots=True)
class HumanOperation:
    operation_id: str
    target_day: date
    kind: HumanKind
    payload: Mapping[str, object]
    target_key: str | None
    cancel_operation_id: str | None


@dataclass(frozen=True, slots=True)
class HumanOperationRecord:
    operation: HumanOperation
    recorded_at_utc: str
    active: bool


@dataclass(frozen=True, slots=True)
class HumanDay:
    day: date
    path: Path
    status: HumanStatus
    revision: int
    history: tuple[HumanOperationRecord, ...]
    projection_state: ProjectionState
    projected_revision: int | None
    projection_digest: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class HumanSaveResult:
    status: Literal["saved", "idempotent", "conflict", "stale", "invalid"]
    day: date
    operation_id: str
    revision: int | None
    path: Path
    projection_state: Literal["pending", "applied", "conflict"] | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    status: Literal[
        "applied", "already_applied", "pending", "conflict", "stale", "missing", "partial_failure"
    ]
    day: date
    revision: int | None
    human_path: Path
    note_path: Path | None
    projection_digest: str | None
    reason: str | None


def _planned_human_path(cfg: Config, day: date) -> Path:
    return Path(cfg.vault_dir).expanduser() / ".kaizenlog" / "human" / f"{day.isoformat()}.json"


def _human_path(cfg: Config, day: date) -> Path:
    vault = Path(cfg.vault_dir).expanduser()
    root = vault.resolve(strict=False)
    path = _planned_human_path(cfg, day)
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValueError("human JSONの保存先がVaultの外にあります") from exc
    return path


def stable_target_key(
    start: datetime, end: datetime, app: str, title: str
) -> str:
    """Return the stable key for one saved measurement block.

    The four canonical values are UTC start, UTC end, the exact app string,
    and the SHA-256 of the exact title available in the saved stats row.
    """
    start_iso = _utc_iso(start)
    end_iso = _utc_iso(end)
    if end_iso <= start_iso:
        raise ValueError("stable keyの終了時刻は開始時刻より後である必要があります")
    if not isinstance(app, str) or not isinstance(title, str):
        raise ValueError("stable keyのapp/titleは文字列が必要です")
    title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
    canonical = json.dumps(
        [start_iso, end_iso, app, title_hash],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("stable keyの時刻はtimezone-awareである必要があります")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_aware(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("時刻は文字列が必要です")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("時刻の形式が不正です") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("naive時刻は受け付けません")
    return parsed


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _OPERATION_ID.fullmatch(value) is not None and value == value.strip()


def _valid_single_line(value: object, max_bytes: int) -> bool:
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value or "\x00" in value:
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return 1 <= size <= max_bytes


def _valid_text(value: object, max_bytes: int) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return 1 <= size <= max_bytes


def _operation_from_mapping(raw: object) -> HumanOperation:
    if not isinstance(raw, dict) or set(raw) != _OP_KEYS:
        raise ValueError("operationのキーが不正です")
    operation_id = raw["operation_id"]
    if not _valid_id(operation_id):
        raise ValueError("operation_idが不正です")
    target_day_raw = raw["target_day"]
    if not isinstance(target_day_raw, str):
        raise ValueError("target_dayが不正です")
    try:
        target_day = date.fromisoformat(target_day_raw)
    except ValueError as exc:
        raise ValueError("target_dayが不正です") from exc
    if target_day.isoformat() != target_day_raw:
        raise ValueError("target_dayはYYYY-MM-DDで指定してください")
    kind = raw["kind"]
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError("kindが不正です")
    payload = raw["payload"]
    if not isinstance(payload, dict):
        raise ValueError("payloadがobjectではありません")
    target_key = raw["target_key"]
    if target_key is not None and (not isinstance(target_key, str) or _HEX64.fullmatch(target_key) is None):
        raise ValueError("target_keyが不正です")
    cancel_id = raw["cancel_operation_id"]
    if cancel_id is not None and not _valid_id(cancel_id):
        raise ValueError("cancel_operation_idが不正です")
    operation = HumanOperation(operation_id, target_day, kind, dict(payload), target_key, cancel_id)
    _validate_payload_shape(operation)
    return operation


def _validate_payload_shape(operation: HumanOperation) -> None:
    payload = operation.payload
    if operation.kind == "outcome":
        if set(payload) != {"text"} or not _valid_single_line(payload.get("text"), 1000):
            raise ValueError("outcome payloadが不正です")
        if operation.target_key is not None or operation.cancel_operation_id is not None:
            raise ValueError("outcomeの対象指定が不正です")
    elif operation.kind == "next_action":
        if set(payload) != {"text", "source", "source_id"}:
            raise ValueError("next_action payloadのキーが不正です")
        if not _valid_single_line(payload.get("text"), 500):
            raise ValueError("next_action textが不正です")
        source = payload.get("source")
        source_id = payload.get("source_id")
        if source not in ("free_text", "kzn"):
            raise ValueError("next_action sourceが不正です")
        if source == "free_text":
            if source_id is not None:
                raise ValueError("free_textのsource_idはnullである必要があります")
        elif not _valid_id(source_id):
            raise ValueError("kznのsource_idが不正です")
        if operation.target_key is not None or operation.cancel_operation_id is not None:
            raise ValueError("next_actionの対象指定が不正です")
    elif operation.kind == "classification_correction":
        if not set(payload).issubset({"category", "project"}) or not payload:
            raise ValueError("classification_correction payloadが不正です")
        if "category" in payload and not _valid_text(payload["category"], 80):
            raise ValueError("categoryが不正です")
        if "project" in payload and not _valid_text(payload["project"], 160):
            raise ValueError("projectが不正です")
        if operation.target_key is None or operation.cancel_operation_id is not None:
            raise ValueError("classification_correctionのtarget_keyが不正です")
    else:
        if payload or operation.target_key is not None or operation.cancel_operation_id is None:
            raise ValueError("cancel payloadが不正です")


def _validate_operation(operation: object, day: date) -> HumanOperation:
    if not isinstance(operation, HumanOperation):
        raise ValueError("operation型が不正です")
    if operation.target_day != day:
        raise ValueError("operationの対象日が一致しません")
    # Re-run through the wire shape to reject Mapping implementations and
    # values that are not JSON-safe before any parent directory is created.
    raw = {
        "operation_id": operation.operation_id,
        "target_day": operation.target_day.isoformat() if isinstance(operation.target_day, date) and not isinstance(operation.target_day, datetime) else None,
        "kind": operation.kind,
        "payload": dict(operation.payload) if isinstance(operation.payload, Mapping) else None,
        "target_key": operation.target_key,
        "cancel_operation_id": operation.cancel_operation_id,
    }
    normalized = _operation_from_mapping(raw)
    try:
        json.dumps(normalized.payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("payloadがJSON値ではありません") from exc
    return normalized


def _record_from_mapping(raw: object) -> tuple[HumanOperation, str]:
    if not isinstance(raw, dict) or set(raw) != _RECORD_KEYS:
        raise ValueError("history recordのキーが不正です")
    operation = _operation_from_mapping(raw["operation"])
    recorded = raw["recorded_at_utc"]
    if not isinstance(recorded, str):
        raise ValueError("recorded_at_utcが不正です")
    parsed = _parse_aware(recorded)
    if parsed.utcoffset() != timezone.utc.utcoffset(None) or not recorded.endswith("Z"):
        raise ValueError("recorded_at_utcはUTCのZ表記が必要です")
    if _utc_iso(parsed) != recorded:
        raise ValueError("recorded_at_utcがcanonicalではありません")
    return operation, recorded


def _derive_records(operations: list[tuple[HumanOperation, str]]) -> tuple[HumanOperationRecord, ...]:
    cancelled: set[str] = set()
    for operation, _ in operations:
        if operation.kind == "cancel":
            cancelled.add(operation.cancel_operation_id or "")
    return tuple(
        HumanOperationRecord(operation, recorded, operation.operation_id not in cancelled)
        for operation, recorded in operations
    )


def _invalid_day(cfg: Config | None, day: date, path: Path, reason: str) -> HumanDay:
    return HumanDay(day, path, "invalid", 0, (), "none", None, None, reason)


def _decode_day(cfg: Config | None, day: date, path: Path, raw: object) -> HumanDay:
    if not isinstance(raw, dict) or set(raw) != _TOP_KEYS:
        return _invalid_day(cfg, day, path, "human JSONのトップレベルキーが不正です")
    if not _is_int(raw.get("schema_version")) or raw.get("schema_version") != 1:
        return _invalid_day(cfg, day, path, "schema_versionは1だけを受け付けます")
    if raw.get("day") != day.isoformat():
        return _invalid_day(cfg, day, path, "human JSONの日付が一致しません")
    revision = raw.get("revision")
    if not _is_int(revision) or revision < 0:
        return _invalid_day(cfg, day, path, "revisionが不正です")
    try:
        updated = raw.get("updated_at_utc")
        if not isinstance(updated, str) or not updated.endswith("Z") or _utc_iso(_parse_aware(updated)) != updated:
            raise ValueError("updated_at_utcが不正です")
        projection = raw["projection"]
        if not isinstance(projection, dict) or set(projection) != _PROJECTION_KEYS:
            raise ValueError("projectionのキーが不正です")
        state = projection["state"]
        if state not in ("none", "pending", "applied", "conflict"):
            raise ValueError("projection stateが不正です")
        projected = projection["projected_revision"]
        if projected is not None and (not _is_int(projected) or projected < 0):
            raise ValueError("projected_revisionが不正です")
        digest = projection["digest"]
        if digest is not None and (not isinstance(digest, str) or _HEX64.fullmatch(digest) is None):
            raise ValueError("projection digestが不正です")
        if state == "none":
            if revision != 0 or projected is not None or digest is not None:
                raise ValueError("projection state noneのmetadataが不整合です")
        elif state == "applied":
            if projected != revision or digest is None:
                raise ValueError("projection state appliedのmetadataが不整合です")
        elif projected is not None:
            if projected <= 0 or projected > revision or digest is None:
                raise ValueError("projection metadataが現在revisionと不整合です")
        elif digest is not None:
            raise ValueError("projection metadataのrevision/digestが不整合です")
        history = raw["history"]
        if not isinstance(history, list):
            raise ValueError("historyが配列ではありません")
        operations = [_record_from_mapping(item) for item in history]
        if len(operations) != revision:
            raise ValueError("revisionとhistory件数が一致しません")
        ids: set[str] = set()
        for operation, _ in operations:
            if operation.target_day != day or operation.operation_id in ids:
                raise ValueError("historyのoperationが不正です")
            ids.add(operation.operation_id)
        records = _derive_records(operations)
        active_ids: set[str] = set()
        operation_indexes = {operation.operation_id: index for index, (operation, _) in enumerate(operations)}
        for index, (operation, _) in enumerate(operations):
            if operation.kind == "cancel":
                target = operation.cancel_operation_id
                target_index = operation_indexes.get(target or "")
                target_record = records[target_index] if target_index is not None else None
                if (
                    target_record is None
                    or target_index is None
                    or target_index >= index
                    or target_record.operation.kind == "cancel"
                    or target not in active_ids
                ):
                    raise ValueError("cancelの対象が不正です")
                active_ids.remove(target)
            else:
                active_ids.add(operation.operation_id)
        return HumanDay(day, path, "loaded", revision, records, state, projected, digest, None)
    except (TypeError, ValueError, UnicodeError) as exc:
        return _invalid_day(cfg, day, path, str(exc))


def load_human_day(cfg: Config, day: date) -> HumanDay:
    """Read a day without creating its directory or lock sidecar."""
    try:
        path = _human_path(cfg, day)
    except (OSError, TypeError, ValueError) as exc:
        return _invalid_day(cfg, day, _planned_human_path(cfg, day), str(exc))
    if not path.is_file():
        return HumanDay(day, path, "missing", 0, (), "none", None, None, None)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _invalid_day(cfg, day, path, f"human JSONを読めません: {exc}")
    return _decode_day(cfg, day, path, raw)


def _wire_operation(operation: HumanOperation) -> dict[str, object]:
    return {
        "operation_id": operation.operation_id,
        "target_day": operation.target_day.isoformat(),
        "kind": operation.kind,
        "payload": dict(operation.payload),
        "target_key": operation.target_key,
        "cancel_operation_id": operation.cancel_operation_id,
    }


def _operation_equal(left: HumanOperation, right: HumanOperation) -> bool:
    return json.dumps(_wire_operation(left), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
        _wire_operation(right), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _read_existing(path: Path, day: date) -> HumanDay:
    if not path.is_file():
        return HumanDay(day, path, "missing", 0, (), "none", None, None, None)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _invalid_day(None, day, path, f"human JSONを読めません: {exc}")
    return _decode_day(None, day, path, raw)


def _stats_path(cfg: Config, day: date) -> Path:
    return Path(cfg.stats_path) / f"{day.isoformat()}.json"


def _stats_block_keys(cfg: Config, day: date) -> tuple[set[str], bool]:
    path = _stats_path(cfg, day)
    if not path.is_file():
        return set(), False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set(), False
    if not isinstance(data, dict) or data.get("day") != day.isoformat() or not isinstance(data.get("blocks"), list):
        return set(), False
    keys: list[str] = []
    try:
        for block in data["blocks"]:
            if not isinstance(block, dict):
                return set(), False
            start = _parse_aware(block.get("start"))
            end = _parse_aware(block.get("end"))
            app = block.get("app")
            title = block.get("title")
            if not isinstance(app, str) or not isinstance(title, str):
                return set(), False
            keys.append(stable_target_key(start, end, app, title))
    except (TypeError, ValueError, UnicodeError):
        return set(), False
    if len(set(keys)) != len(keys):
        return set(), False
    return set(keys), True


def _validate_external_source(cfg: Config, operation: HumanOperation, day: date) -> str | None:
    if operation.kind == "next_action" and operation.payload.get("source") == "kzn":
        source_id = operation.payload.get("source_id")
        entries = load_entries(Path(cfg.memory_path))
        resolved = resolve_action_id(str(source_id), entries)
        if not isinstance(resolved, MemoryEntry) or resolved.id != source_id:
            return "source_idが既存Memoryの一意IDではありません"
    if operation.kind == "classification_correction":
        keys, valid = _stats_block_keys(cfg, day)
        if not valid or operation.target_key not in keys:
            return "classification_correctionのtarget_keyが現在のstats原本と一致しません"
    return None


@contextmanager
def _source_lock(cfg: Config, operation: HumanOperation, day: date):
    """Hold the source store lock before acquiring the human lock.

    Read-only ``load_human_day`` never enters this context.  KZN reads use the
    existing Memory lock; stats corrections use the completed stats file lock.
    """
    if operation.kind == "next_action" and operation.payload.get("source") == "kzn":
        from .memory import memory_lock

        with memory_lock(Path(cfg.memory_path)):
            yield
        return
    if operation.kind == "classification_correction":
        with file_lock(lock_path_for(_stats_path(cfg, day))):
            yield
        return
    yield


def _save_payload(day: date, revision: int, history: list[tuple[HumanOperation, str]], state: str = "pending", projected: int | None = None, digest: str | None = None) -> str:
    payload = {
        "schema_version": 1,
        "day": day.isoformat(),
        "updated_at_utc": _utc_iso(datetime.now(timezone.utc)),
        "revision": revision,
        "history": [
            {"operation": _wire_operation(operation), "recorded_at_utc": recorded}
            for operation, recorded in history
        ],
        "projection": {"state": state, "projected_revision": projected, "digest": digest},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def apply_human_operation(cfg: Config, day: date, operation: HumanOperation, *, expected_revision: int) -> HumanSaveResult:
    try:
        path = _human_path(cfg, day)
    except (OSError, TypeError, ValueError) as exc:
        path = _planned_human_path(cfg, day)
        return HumanSaveResult("invalid", day, getattr(operation, "operation_id", ""), None, path, None, str(exc))
    try:
        normalized = _validate_operation(operation, day)
        if not _is_int(expected_revision) or expected_revision < 0:
            raise ValueError("expected_revisionが不正です")
    except (TypeError, ValueError, UnicodeError) as exc:
        return HumanSaveResult("invalid", day, getattr(operation, "operation_id", ""), None, path, None, str(exc))

    # Keep invalid source operations read-only.  This preliminary snapshot is
    # only an optimization for duplicate detection; the locked check below is
    # authoritative and closes the race with another writer.
    preliminary = load_human_day(cfg, day)
    if preliminary.status == "invalid":
        return HumanSaveResult("invalid", day, normalized.operation_id, None, path, None, preliminary.reason)
    preliminary_existing = next(
        (record for record in preliminary.history if record.operation.operation_id == normalized.operation_id),
        None,
    )
    if preliminary_existing is not None:
        same = _operation_equal(preliminary_existing.operation, normalized)
        return HumanSaveResult(
            "idempotent" if same else "conflict",
            day,
            normalized.operation_id,
            preliminary.revision,
            path,
            preliminary.projection_state if preliminary.projection_state != "none" else None,
            None if same else "operation_idが既存payloadと異なります",
        )

    with _source_lock(cfg, normalized, day):
        # Another identical writer may have committed while this call was
        # waiting for the source lock.  Re-read the human primary first so
        # idempotency still wins over a source that changed in the meantime.
        locked_snapshot = load_human_day(cfg, day)
        if locked_snapshot.status == "invalid":
            return HumanSaveResult("invalid", day, normalized.operation_id, None, path, None, locked_snapshot.reason)
        locked_existing = next(
            (record for record in locked_snapshot.history if record.operation.operation_id == normalized.operation_id),
            None,
        )
        if locked_existing is not None:
            same = _operation_equal(locked_existing.operation, normalized)
            return HumanSaveResult(
                "idempotent" if same else "conflict",
                day,
                normalized.operation_id,
                locked_snapshot.revision,
                path,
                locked_snapshot.projection_state if locked_snapshot.projection_state != "none" else None,
                None if same else "operation_idが既存payloadと異なります",
            )
        # The first source read is protected by its store lock.  It can return
        # before taking the human lock, leaving no human artifact for stale
        # source input.
        source_reason = _validate_external_source(cfg, normalized, day)
        if source_reason is not None:
            return HumanSaveResult(
                "stale" if normalized.kind == "classification_correction" else "invalid",
                day,
                normalized.operation_id,
                preliminary.revision,
                path,
                preliminary.projection_state if preliminary.projection_state != "none" else None,
                source_reason,
            )

        with file_lock(lock_path_for(path)):
            current = _read_existing(path, day)
            if current.status == "invalid":
                return HumanSaveResult("invalid", day, normalized.operation_id, None, path, None, current.reason)
            existing = next((record for record in current.history if record.operation.operation_id == normalized.operation_id), None)
            if existing is not None:
                status = "idempotent" if _operation_equal(existing.operation, normalized) else "conflict"
                return HumanSaveResult(status, day, normalized.operation_id, current.revision, path, current.projection_state if current.projection_state != "none" else None, None if status == "idempotent" else "operation_idが既存payloadと異なります")
            if current.revision != expected_revision:
                return HumanSaveResult("stale", day, normalized.operation_id, current.revision, path, current.projection_state if current.projection_state != "none" else None, "expected_revisionが現在値と一致しません")
            if normalized.kind == "cancel":
                target = next((record for record in current.history if record.operation.operation_id == normalized.cancel_operation_id), None)
                if target is None or not target.active or target.operation.kind == "cancel":
                    return HumanSaveResult("conflict", day, normalized.operation_id, current.revision, path, current.projection_state if current.projection_state != "none" else None, "cancel対象が存在しないか既に無効です")
            # Recheck while the source lock is still held.  Never acquire a
            # source lock from inside the human lock (store -> human only).
            source_reason = _validate_external_source(cfg, normalized, day)
            if source_reason is not None:
                return HumanSaveResult("stale" if normalized.kind == "classification_correction" else "invalid", day, normalized.operation_id, current.revision, path, current.projection_state if current.projection_state != "none" else None, source_reason)
            recorded = _utc_iso(datetime.now(timezone.utc))
            history = [(record.operation, record.recorded_at_utc) for record in current.history]
            history.append((normalized, recorded))
            revision = current.revision + 1
            next_state = "conflict" if current.projection_state == "conflict" else "pending"
            atomic_write_text(
                path,
                _save_payload(
                    day,
                    revision,
                    history,
                    state=next_state,
                    projected=current.projected_revision,
                    digest=current.projection_digest,
                ),
            )
            return HumanSaveResult("saved", day, normalized.operation_id, revision, path, next_state, None)


def _escape_projection_text(value: object) -> str:
    """Keep user text visible while preventing managed marker recognition."""
    text = str(value)
    return text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def _stats_index(
    source_stats: Mapping[str, object] | None,
    *,
    expected_day: date,
) -> tuple[str, dict[str, Mapping[str, object]] | None]:
    if source_stats is None:
        return "missing", None
    if source_stats.get("day") != expected_day.isoformat():
        return "invalid", None
    blocks = source_stats.get("blocks") if isinstance(source_stats, Mapping) else None
    if not isinstance(blocks, list):
        return "invalid", None
    index: dict[str, Mapping[str, object]] = {}
    try:
        for block in blocks:
            if not isinstance(block, Mapping):
                return "invalid", None
            start = _parse_aware(block.get("start"))
            end = _parse_aware(block.get("end"))
            app = block.get("app")
            title = block.get("title")
            if not isinstance(app, str) or not isinstance(title, str):
                return "invalid", None
            key = stable_target_key(start, end, app, title)
            if key in index:
                return "duplicate", None
            index[key] = block
    except (TypeError, ValueError, UnicodeError):
        return "invalid", None
    return "valid", index


def _correction_line(
    target_key: str,
    field: str,
    value: object,
    source_stats: Mapping[str, object] | None,
    expected_day: date,
) -> str:
    status, index = _stats_index(source_stats, expected_day=expected_day)
    requested = _escape_projection_text(value)
    state_label = "未確認 / 未適用" if status in {"missing", "invalid"} else "stale / 未適用"
    if status != "valid" or index is None or target_key not in index:
        return (
            f"- 手動訂正（対象 {target_key}; {state_label} / "
            f"requested {field}={requested}; source={status}）"
        )
    block = index[target_key]
    original = _escape_projection_text(block.get(field) or "未記録")
    start = _escape_projection_text(block.get("start") or "未記録")
    end = _escape_projection_text(block.get("end") or "未記録")
    app = _escape_projection_text(block.get("app") or "未記録")
    return (
        f"- 手動訂正（{field}; {start}–{end}; app={app}; "
        f"original={original} -> manual={requested}; 出自=手動訂正 / 原本保持）"
    )


def human_overlay_lines(
    human: HumanDay,
    *,
    source_stats: Mapping[str, object] | None = None,
) -> tuple[str, ...]:
    """Return reader-facing human lines without changing measured values."""
    if human.status != "loaded":
        return ()
    latest: dict[str, HumanOperationRecord] = {}
    corrections: dict[tuple[str, str], HumanOperationRecord] = {}
    for record in human.history:
        operation = record.operation
        if not record.active or operation.kind == "cancel":
            continue
        if operation.kind in {"outcome", "next_action"}:
            latest[operation.kind] = record
        elif operation.kind == "classification_correction":
            assert operation.target_key is not None
            for field in operation.payload:
                corrections[(operation.target_key, field)] = record
    lines: list[str] = []
    if "outcome" in latest:
        lines.append(f"- 成果（本人入力）: {_escape_projection_text(latest['outcome'].operation.payload['text'])}")
    if "next_action" in latest:
        operation = latest["next_action"].operation
        source = operation.payload.get("source")
        source_id = operation.payload.get("source_id")
        provenance = "自由記述" if source == "free_text" else f"KZN {source_id}"
        lines.append(
            f"- 次の1件（本人入力・出自: {provenance}）: "
            f"{_escape_projection_text(operation.payload['text'])}"
        )
    for (target_key, field), record in sorted(corrections.items()):
        lines.append(
            _correction_line(
                target_key,
                field,
                record.operation.payload[field],
                source_stats,
                human.day,
            )
        )
    return tuple(lines)


def render_human_overlay(
    human: HumanDay,
    *,
    source_stats: Mapping[str, object] | None = None,
) -> str:
    """Render the managed Markdown body for a human day."""
    lines = [f"## 本人入力・表示用訂正 (human revision: {human.revision})", ""]
    lines.extend(human_overlay_lines(human, source_stats=source_stats) or ("- （有効な本人入力はありません）",))
    return "\n".join(lines)


def _section_bytes(content: str) -> bytes | None:
    from .vault import _find_section_span

    span = _find_section_span(content, HUMAN_INPUT_MARKER)
    if span is None:
        return None
    return content[span[0]:span[1]].encode("utf-8")


def _human_marker_issue(content: str) -> str | None:
    from .vault import _end_tag, _start_tag

    start_tag, end_tag = _start_tag(HUMAN_INPUT_MARKER), _end_tag(HUMAN_INPUT_MARKER)
    starts, ends = content.count(start_tag), content.count(end_tag)
    if starts == 0 and ends == 0:
        return None
    if starts != 1 or ends != 1:
        return "本人入力の管理markerが重複または入れ子になっています"
    start = content.find(start_tag)
    end = content.find(end_tag, start + len(start_tag))
    if start < 0 or end < 0 or end <= start:
        return "本人入力の管理markerが壊れています"
    return None


def _source_stats_snapshot(cfg: Config, day: date) -> tuple[bytes | None, Mapping[str, object] | None]:
    path = _stats_path(cfg, day)
    try:
        raw = path.read_bytes() if path.is_file() else None
    except OSError:
        return None, None
    if raw is None:
        return None, None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return raw, None
    return raw, decoded if isinstance(decoded, Mapping) else None


def project_human_day(cfg: Config, day: date, *, expected_revision: int) -> ProjectionResult:
    """Project one saved HumanDay into its existing daily note.

    The human JSON lock is acquired before the Markdown lock.  Note creation is
    intentionally excluded: a missing note leaves the primary input pending.
    """
    try:
        path = _human_path(cfg, day)
    except (OSError, TypeError, ValueError) as exc:
        return ProjectionResult("conflict", day, None, _planned_human_path(cfg, day), None, None, str(exc))
    if not _is_int(expected_revision) or expected_revision < 0:
        return ProjectionResult("stale", day, None, path, None, None, "expected_revisionが不正です")
    try:
        from .note_paths import resolve_daily_note
        from .vault import upsert_section
        from .vault import DailyNoteStore
        from . import vault as vault_module

        with file_lock(lock_path_for(path)):
            human = _read_existing(path, day)
            if human.status == "missing":
                return ProjectionResult("missing", day, None, path, None, None, "本人入力がありません")
            if human.status != "loaded":
                return ProjectionResult("conflict", day, None, path, None, None, human.reason or "human JSONが不正です")
            if human.revision != expected_revision:
                return ProjectionResult("stale", day, human.revision, path, None, human.projection_digest, "expected_revisionが現在値と一致しません")
            if human.revision == 0:
                return ProjectionResult("missing", day, 0, path, None, None, "投影対象の本人入力がありません")
            if human.projection_state == "conflict":
                return ProjectionResult("conflict", day, human.revision, path, None, human.projection_digest, "外部編集競合を解消してから再投影してください")

            source_stats_bytes, source_stats = _source_stats_snapshot(cfg, day)
            resolution = resolve_daily_note(cfg, day)
            if resolution.status == "missing":
                return ProjectionResult("pending", day, human.revision, path, resolution.path, human.projection_digest, "日次ノートがないため本人入力を反映していません")
            if resolution.status != "selected" or resolution.path is None:
                return ProjectionResult("conflict", day, human.revision, path, None, human.projection_digest, resolution.reason or "日次ノートを一意に解決できません")
            note_path = resolution.path

            # Keep the store->human->note order and use DailyNoteStore's
            # resolver re-check while waiting for the note lock.
            store = DailyNoteStore(cfg)
            with store.locked_note(day, expected_resolution=resolution) as (locked_path, content):
                if locked_path != note_path:
                    return ProjectionResult("conflict", day, human.revision, path, locked_path, human.projection_digest, "日次ノートの解決先が保存待ちの間に変わりました")
                if content is None:
                    return ProjectionResult("pending", day, human.revision, path, note_path, human.projection_digest, "日次ノートがないため本人入力を反映していません")
                latest_source_bytes, _ = _source_stats_snapshot(cfg, day)
                if latest_source_bytes != source_stats_bytes:
                    return ProjectionResult("stale", day, human.revision, path, note_path, human.projection_digest, "stats原本が投影中に変更されました")
                marker_issue = _human_marker_issue(content)
                if marker_issue is not None:
                    return ProjectionResult("conflict", day, human.revision, path, note_path, human.projection_digest, marker_issue)
                current_section = _section_bytes(content)
                desired = upsert_section(content, HUMAN_INPUT_MARKER, render_human_overlay(human, source_stats=source_stats), position="bottom")
                desired_section = _section_bytes(desired)
                if desired_section is None:
                    return ProjectionResult("partial_failure", day, human.revision, path, note_path, None, "管理区間を生成できません")
                # Recovery must compare the complete desired bytes first. A
                # newer revision may legitimately differ from retained old
                # metadata digest after a metadata-only crash.
                if current_section == desired_section:
                    pass
                elif human.projection_digest is not None:
                    if current_section is None or hashlib.sha256(current_section).hexdigest() != human.projection_digest:
                        return ProjectionResult("conflict", day, human.revision, path, note_path, human.projection_digest, "管理区間が外部編集されています")
                elif current_section is not None:
                    # A pending projection may have crashed after the note
                    # replace and before metadata.  An exact match with the
                    # desired complete section is therefore a safe recovery.
                    # Any other pre-existing marker lacks a trusted baseline.
                    if current_section != desired_section:
                        return ProjectionResult("conflict", day, human.revision, path, note_path, human.projection_digest, "管理区間の基準がありません")

                digest = hashlib.sha256(desired_section).hexdigest()
                changed = desired != content
                if changed:
                    latest_source_bytes, _ = _source_stats_snapshot(cfg, day)
                    if latest_source_bytes != source_stats_bytes:
                        return ProjectionResult("stale", day, human.revision, path, note_path, human.projection_digest, "stats原本が投影直前に変更されました")
                    vault_module.atomic_write_text(note_path, desired)

                try:
                    history = [(record.operation, record.recorded_at_utc) for record in human.history]
                    atomic_write_text(
                        path,
                        _save_payload(human.day, human.revision, history, state="applied", projected=human.revision, digest=digest),
                    )
                except (OSError, UnicodeError, ValueError) as exc:
                    return ProjectionResult("partial_failure", day, human.revision, path, note_path, digest, f"投影後の管理情報保存に失敗しました: {exc}")
                status = "already_applied" if not changed else "applied"
                return ProjectionResult(status, day, human.revision, path, note_path, digest, None)
    except TimeoutError as exc:
        return ProjectionResult("partial_failure", day, expected_revision, path, None, None, f"投影lock取得がtimeoutしました: {exc}")
    except RuntimeError as exc:
        return ProjectionResult("conflict", day, expected_revision, path, None, None, str(exc))
    except ValueError as exc:
        return ProjectionResult("conflict", day, expected_revision, path, None, None, str(exc))
    except OSError as exc:
        return ProjectionResult("partial_failure", day, expected_revision, path, None, None, str(exc))


__all__ = [
    "HumanOperation", "HumanOperationRecord", "HumanDay", "HumanSaveResult", "ProjectionResult",
    "stable_target_key", "load_human_day", "apply_human_operation", "project_human_day",
    "human_overlay_lines", "render_human_overlay",
]
