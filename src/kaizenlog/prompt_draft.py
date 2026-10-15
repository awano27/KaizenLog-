"""Create deterministic, unverified prompt drafts from the prompt ledger.

This module deliberately reads the ledger as bytes instead of delegating to
``load_prompt_ledger``.  The latter is a convenient display API and
normalizes malformed statuses; a draft's input must retain the exact ledger
line so a write can detect a changed source.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from .config import Config
from .file_lock import file_lock, lock_path_for, locked_read_modify_write
from .privacy import make_redactor
from .promptledger import LEDGER_FILE, STATUSES
from .vault import atomic_write_bytes


_ID_RE = re.compile(r"PRM-\d{8}-\d{3,}")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{n}" for n in range(1, 10)),
    *(f"LPT{n}" for n in range(1, 10)),
}
PromptDraftStatus = Literal[
    "preview", "written", "unchanged", "conflict", "invalid", "missing", "stale"
]


@dataclass(frozen=True, slots=True)
class PromptLedgerSnapshot:
    """The validated latest target line in the append-only prompt ledger."""

    fingerprint: str
    status: str
    representative: str
    source_path: Path


@dataclass(frozen=True, slots=True)
class PromptDraftResult:
    status: PromptDraftStatus
    prm_id: str
    path: Path | None
    content: str
    fingerprint: str | None
    backup: Path | None
    reason: str | None


class _SnapshotError(ValueError):
    pass


class _UnsafePath(ValueError):
    pass


def _result(
    status: str,
    prm_id: str,
    *,
    path: Path | None = None,
    content: str = "",
    fingerprint: str | None = None,
    backup: Path | None = None,
    reason: str | None = None,
) -> PromptDraftResult:
    return PromptDraftResult(status, prm_id, path, content, fingerprint, backup, reason)


def _valid_prm_id(prm_id: object) -> bool:
    return isinstance(prm_id, str) and _ID_RE.fullmatch(prm_id) is not None


def _is_reparse_point(path: Path) -> bool:
    """Return true for links and Windows reparse points without following them."""

    path = Path(path)
    try:
        if path.is_symlink():
            return True
        info = path.stat(follow_symlinks=False)
    except OSError:
        return False
    attrs = int(getattr(info, "st_file_attributes", 0) or 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _check_component_chain(path: Path) -> None:
    """Reject links/reparse points in every existing component of ``path``."""

    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _is_reparse_point(current):
            raise _UnsafePath(f"出力先にsymlink/junction/reparse pointがあります: {current}")


def _safe_draft_paths(project: Path, prm_id: str) -> tuple[Path, Path]:
    """Validate and return the root and target before any write-side effects."""

    raw_root = Path(project).expanduser()
    if not raw_root.is_absolute():
        raw_root = Path.cwd() / raw_root
    _check_component_chain(raw_root)
    if not raw_root.is_dir():
        raise _UnsafePath("project は存在するディレクトリである必要があります")
    root = raw_root.resolve(strict=True)

    target = root / ".kaizenlog" / "drafts" / f"{prm_id}.md"
    backup = target.with_name(target.name + ".bak")
    _check_component_chain(target)
    _check_component_chain(backup)
    for candidate in (target, backup):
        try:
            resolved = candidate.resolve(strict=False)
            if os.path.commonpath((str(root), str(resolved))) != str(root):
                raise _UnsafePath("出力先がprojectの外を指しています")
        except ValueError as exc:
            raise _UnsafePath("出力先のパスを検証できません") from exc
    return root, target


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _validate_entry(data: Any, prm_id: str) -> tuple[str, str]:
    if not isinstance(data, dict):
        raise _SnapshotError("対象行がJSON objectではありません")
    if data.get("id") != prm_id:
        raise _SnapshotError("対象IDが一致しません")
    representative = data.get("representative")
    if not isinstance(representative, str) or not representative.strip():
        raise _SnapshotError("代表文が空です")
    status = data.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        raise _SnapshotError("statusが不正です")
    for key in ("count_total", "days_seen"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _SnapshotError(f"{key}が不正です")
    for key in ("first_seen", "last_seen"):
        if not _valid_date(data.get(key)):
            raise _SnapshotError(f"{key}が不正です")
    if status == "skilled":
        skill = data.get("skill_name")
        if skill is not None and (not isinstance(skill, str) or not skill.strip()):
            raise _SnapshotError("skill_nameが不正です")
    marked_on = data.get("marked_on")
    if marked_on is not None and not _valid_date(marked_on):
        raise _SnapshotError("marked_onが不正です")
    return status, representative.strip()


def _line_mentions_target(line: bytes, prm_id: str) -> bool:
    try:
        pattern = rb'"id"\s*:\s*"' + re.escape(prm_id.encode("ascii")) + rb'"'
        return re.search(pattern, line) is not None
    except (UnicodeEncodeError, re.error):
        return False


def _read_snapshot(path: Path, prm_id: str) -> PromptLedgerSnapshot | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _SnapshotError(f"台帳を読めません: {exc}") from exc
    found: PromptLedgerSnapshot | None = None
    for line in raw.splitlines(keepends=True):
        if not line.strip():
            continue
        try:
            text = line.decode("utf-8")
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if _line_mentions_target(line, prm_id):
                # A later append may repair an older malformed target line;
                # keep the sentinel and let the final target line win.
                found = PromptLedgerSnapshot("", "__invalid__", "", path)
            continue
        if not isinstance(data, dict) or data.get("id") != prm_id:
            continue
        try:
            status, representative = _validate_entry(data, prm_id)
        except _SnapshotError:
            # Continue scanning only to allow a later target line to become
            # the actual latest target line; the final target line wins.
            found = PromptLedgerSnapshot("", "__invalid__", "", path)
            continue
        found = PromptLedgerSnapshot(
            hashlib.sha256(line).hexdigest(), status, representative, path
        )
    if found is not None and found.status == "__invalid__":
        raise _SnapshotError("対象IDの最新台帳行が不正です")
    return found


def _redacted_representative(cfg: Config, representative: str) -> str:
    patterns = list(cfg.privacy.redact_patterns or [])
    replacement = str(cfg.privacy.replacement or "[REDACTED]")
    redactor = make_redactor(patterns, replacement)
    return (redactor(representative) if redactor else representative).strip()


def _render_content(cfg: Config, prm_id: str, snapshot: PromptLedgerSnapshot) -> str:
    representative = _redacted_representative(cfg, snapshot.representative)
    return (
        "# UNVERIFIED_DRAFT / 効果未検証・編集用\n\n"
        f"PRM-ID: {prm_id}\n"
        f"入力fingerprint: {snapshot.fingerprint}\n"
        f"台帳status: {snapshot.status}\n\n"
        "元の依頼例（レダクト済み）:\n\n"
        f"> {representative or '未記入'}\n\n"
        "## 目的\n\n未記入\n\n"
        "## 前提\n\n未記入\n\n"
        "## 依頼\n\n未記入\n\n"
        "## 確認条件\n\n未記入\n"
    )


def _snapshot_result(cfg: Config, prm_id: str) -> tuple[PromptLedgerSnapshot | None, PromptDraftResult | None]:
    ledger = Path(cfg.memory_path) / LEDGER_FILE
    try:
        snapshot = _read_snapshot(ledger, prm_id)
    except _SnapshotError as exc:
        return None, _result("invalid", prm_id, reason=str(exc))
    if snapshot is None:
        return None, _result("missing", prm_id, reason="対象IDが台帳にありません")
    if snapshot.status == "dismissed":
        return None, _result("invalid", prm_id, fingerprint=snapshot.fingerprint, reason="dismissedの依頼は草案化できません")
    return snapshot, None


def _ensure_exclusive_backup(backup: Path, old: bytes) -> tuple[Path, bool]:
    if backup.exists():
        try:
            if backup.read_bytes() == old:
                return backup, False
        except OSError as exc:
            raise _UnsafePath(f"既存backupを読めません: {exc}") from exc
        raise _SnapshotError("既存のbackupが異なるため上書きしません")
    try:
        with backup.open("xb") as stream:
            stream.write(old)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        try:
            if backup.read_bytes() == old:
                return backup, False
        except OSError as exc:
            raise _UnsafePath(f"競合したbackupを読めません: {exc}") from exc
        raise _SnapshotError("既存のbackupが異なるため上書きしません")
    return backup, True


def _read_existing_target(target: Path) -> bytes | None:
    """Read a regular target, preserving the distinction between missing and empty."""

    if not target.exists():
        return None
    if not target.is_file():
        raise _UnsafePath(f"草案対象が通常ファイルではありません: {target}")
    return target.read_bytes()


def draft_prompt(
    cfg: Config,
    prm_id: str,
    *,
    project: Path | None = None,
    write: bool = False,
    force: bool = False,
    expected_fingerprint: str | None = None,
) -> PromptDraftResult:
    """Preview or explicitly save a deterministic unverified prompt draft."""

    if not _valid_prm_id(prm_id) or prm_id.upper() in _RESERVED_NAMES:
        return _result("invalid", str(prm_id), reason="PRM-IDが不正です")
    if write and project is None:
        return _result("invalid", prm_id, reason="保存時はprojectを明示してください")

    snapshot, early = _snapshot_result(cfg, prm_id)
    if early is not None:
        return early
    assert snapshot is not None
    content = _render_content(cfg, prm_id, snapshot)
    if not write:
        return _result("preview", prm_id, content=content, fingerprint=snapshot.fingerprint)

    try:
        _, target = _safe_draft_paths(Path(project), prm_id)
    except (OSError, _UnsafePath, ValueError) as exc:
        return _result("invalid", prm_id, fingerprint=snapshot.fingerprint, reason=str(exc))
    ledger = Path(cfg.memory_path) / LEDGER_FILE
    try:
        with locked_read_modify_write(ledger):
            latest, early = _snapshot_result(cfg, prm_id)
            if early is not None:
                return early
            assert latest is not None
            if expected_fingerprint is not None and latest.fingerprint != expected_fingerprint:
                return _result("stale", prm_id, fingerprint=latest.fingerprint, reason="台帳がプレビュー後に変更されています")
            # Revalidate the complete parent chain before deriving the lock
            # path. file_lock creates its parent/sidecar as a side effect.
            try:
                locked_root, locked_target = _safe_draft_paths(Path(project), prm_id)
            except (OSError, _UnsafePath, ValueError) as exc:
                return _result("invalid", prm_id, reason=str(exc))
            with file_lock(lock_path_for(locked_target)):
                try:
                    canonical_root, canonical_target = _safe_draft_paths(Path(project), prm_id)
                    if canonical_root != locked_root or canonical_target != locked_target:
                        return _result("invalid", prm_id, reason="lock取得後に出力先が変化しました")
                    target = canonical_target
                    latest, early = _snapshot_result(cfg, prm_id)
                except (OSError, _UnsafePath, ValueError) as exc:
                    return _result("invalid", prm_id, reason=str(exc))
                if early is not None:
                    return early
                assert latest is not None
                if expected_fingerprint is not None and latest.fingerprint != expected_fingerprint:
                    return _result("stale", prm_id, fingerprint=latest.fingerprint, reason="保存直前に台帳が変更されています")
                content = _render_content(cfg, prm_id, latest)
                payload = content.encode("utf-8")
                try:
                    old = _read_existing_target(target)
                except (OSError, _UnsafePath) as exc:
                    return _result("invalid", prm_id, path=target, reason=str(exc))
                if old == payload:
                    return _result("unchanged", prm_id, path=target, content=content, fingerprint=latest.fingerprint)
                if old is not None and not force:
                    return _result("conflict", prm_id, path=target, content=content, fingerprint=latest.fingerprint, reason="既存の草案が異なります。forceが必要です")
                backup = None
                if old is not None:
                    try:
                        backup_root, backup_target = _safe_draft_paths(Path(project), prm_id)
                        if backup_root != locked_root or backup_target != locked_target:
                            return _result("invalid", prm_id, path=target, content=content, fingerprint=latest.fingerprint, reason="backup前に出力先が変化しました")
                        target = backup_target
                        current = _read_existing_target(target)
                        if current != old:
                            return _result("conflict", prm_id, path=target, content=content, fingerprint=latest.fingerprint, reason="草案がバックアップ前に変更されました")
                        backup, _ = _ensure_exclusive_backup(target.with_name(target.name + ".bak"), old)
                    except _SnapshotError as exc:
                        return _result("conflict", prm_id, path=target, content=content, fingerprint=latest.fingerprint, reason=str(exc))
                    except (OSError, _UnsafePath, ValueError) as exc:
                        return _result("invalid", prm_id, path=target, content=content, fingerprint=latest.fingerprint, reason=str(exc))
                # Revalidate source and target immediately before replacement.
                try:
                    canonical_root, canonical_target = _safe_draft_paths(Path(project), prm_id)
                    if canonical_root != locked_root or canonical_target != locked_target:
                        return _result("invalid", prm_id, path=target, content=content, fingerprint=latest.fingerprint, backup=backup, reason="replace前に出力先が変化しました")
                    current = _read_existing_target(canonical_target)
                    if current != old:
                        return _result("conflict", prm_id, path=target, content=content, fingerprint=latest.fingerprint, backup=backup, reason="草案がreplace前に変更されました")
                    final_snapshot, early = _snapshot_result(cfg, prm_id)
                    if early is not None:
                        return early
                    assert final_snapshot is not None
                    if expected_fingerprint is not None and final_snapshot.fingerprint != expected_fingerprint:
                        return _result("stale", prm_id, fingerprint=final_snapshot.fingerprint, reason="replace前に台帳が変更されました", backup=backup)
                    if final_snapshot.fingerprint != latest.fingerprint:
                        latest = final_snapshot
                        content = _render_content(cfg, prm_id, latest)
                        payload = content.encode("utf-8")
                    atomic_write_bytes(target, payload)
                except (OSError, _UnsafePath, ValueError) as exc:
                    return _result("invalid", prm_id, path=target, content=content, fingerprint=latest.fingerprint, backup=backup, reason=str(exc))
                return _result("written", prm_id, path=target, content=content, fingerprint=latest.fingerprint, backup=backup)
    except (OSError, TimeoutError, ValueError) as exc:
        return _result("invalid", prm_id, reason=str(exc))


__all__ = ["PromptDraftResult", "PromptDraftStatus", "PromptLedgerSnapshot", "draft_prompt"]
