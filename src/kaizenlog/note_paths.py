"""Resolve one daily Markdown note without scanning or mutating the vault."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .config import Config

DailyFormat = Literal["YYYY-MM-DD", "YYYYMMDD", "YYYY/MM/DD"]
_FORMATS: tuple[DailyFormat, ...] = ("YYYY-MM-DD", "YYYYMMDD", "YYYY/MM/DD")
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class DailyNoteCandidate:
    path: Path
    format_name: str
    exists: bool
    source: Literal["override", "configured", "known"]


@dataclass(frozen=True, slots=True)
class DailyNoteResolution:
    day: date
    path: Path | None
    status: Literal["selected", "missing", "conflict", "unsupported"]
    selected_by: Literal["override", "configured", "default", "none"]
    candidates: tuple[DailyNoteCandidate, ...]
    reason: str | None


class DailyNoteResolutionError(ValueError):
    def __init__(self, resolution: DailyNoteResolution):
        self.resolution = resolution
        super().__init__(resolution.reason or f"日次ノートを解決できません: {resolution.status}")


def _format_day(day: date, format_name: str) -> str:
    if format_name == "YYYY-MM-DD":
        return day.isoformat() + ".md"
    if format_name == "YYYYMMDD":
        return day.strftime("%Y%m%d") + ".md"
    if format_name == "YYYY/MM/DD":
        return day.strftime("%Y/%m/%d") + ".md"
    raise ValueError(format_name)


def _normalized(path: Path) -> Path:
    return Path(os.path.normcase(str(path.resolve(strict=False))))


def _reserved_or_dot(segment: str) -> bool:
    if (not segment or segment in {".", ".."} or
            segment != segment.rstrip(" .") or
            any(ord(char) < 32 for char in segment) or
            any(char in '<>:"|?*' for char in segment)):
        return True
    stem = segment.rstrip(" .").split(".", 1)[0].rstrip(" .").upper()
    return stem in _RESERVED


def validate_daily_override(base: Path, relative_path: str) -> Path:
    """Validate and return an override path under *base* without creating it."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("日次overrideは空にできません")
    if relative_path != relative_path.strip():
        raise ValueError("日次overrideの前後に空白を指定できません")
    value = relative_path
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value.replace("\\", "/"))
    if windows.is_absolute() or bool(windows.drive) or posix.is_absolute():
        raise ValueError("日次overrideに絶対パスは指定できません")
    if Path(value).suffix.lower() != ".md":
        raise ValueError("日次overrideは.mdの完成パスで指定してください")
    parts = tuple(re.split(r"[\\/]", value))
    if any(not part for part in parts):
        raise ValueError("日次overrideに空のパス要素があります")
    if any(_reserved_or_dot(part) for part in parts) or ".." in parts:
        raise ValueError("日次overrideに安全でないパス要素があります")
    candidate = base.joinpath(*parts)
    base_real = base.resolve(strict=False)
    candidate_real = candidate.resolve(strict=False)
    try:
        candidate_real.relative_to(base_real)
    except ValueError as exc:
        raise ValueError("日次overrideが日次フォルダの外に出ます") from exc
    return candidate


def validate_daily_notes_dir(vault_root: Path, relative_dir: str) -> Path:
    """Validate the configured daily-note directory without touching disk."""
    if not isinstance(relative_dir, str):
        raise ValueError("日次フォルダは文字列で指定してください")
    if not relative_dir:
        return Path(vault_root).expanduser()
    windows = PureWindowsPath(relative_dir)
    posix = PurePosixPath(relative_dir.replace("\\", "/"))
    if windows.is_absolute() or bool(windows.drive) or posix.is_absolute():
        raise ValueError("日次フォルダに絶対パスは指定できません")
    parts = tuple(re.split(r"[\\/]", relative_dir))
    if any(not part for part in parts) or any(_reserved_or_dot(part) for part in parts):
        raise ValueError("日次フォルダに安全でないパス要素があります")
    candidate = Path(vault_root).expanduser().joinpath(*parts)
    try:
        candidate.resolve(strict=False).relative_to(Path(vault_root).expanduser().resolve(strict=False))
    except ValueError as exc:
        raise ValueError("日次フォルダがVaultの外に出ます") from exc
    return candidate


def _base(cfg: Config) -> Path:
    return validate_daily_notes_dir(Path(cfg.vault_dir).expanduser(), cfg.daily_notes_dir)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _unsupported(cfg: Config, day: date, reason: str) -> DailyNoteResolution:
    return DailyNoteResolution(day, None, "unsupported", "none", (), reason)


def resolve_daily_note(cfg: Config, day: date) -> DailyNoteResolution:
    format_name = getattr(cfg, "daily_note_format", "YYYY-MM-DD")
    if format_name not in _FORMATS:
        return _unsupported(cfg, day, f"未対応の日次ノート形式です: {format_name!r}")
    vault_root = Path(cfg.vault_dir).expanduser()
    try:
        base = _base(cfg)
    except ValueError as exc:
        return _unsupported(cfg, day, str(exc))
    if not _inside(base, vault_root):
        return _unsupported(cfg, day, "日次フォルダがVaultの外にあります")
    candidates: list[DailyNoteCandidate] = []
    seen: set[str] = set()
    override = None
    overrides = getattr(cfg, "daily_note_overrides", {}) or {}
    if isinstance(overrides, dict):
        override = overrides.get(day.isoformat())
    if override is not None:
        try:
            override_path = validate_daily_override(base, override)
        except ValueError as exc:
            return _unsupported(cfg, day, str(exc))
        candidates.append(
            DailyNoteCandidate(override_path, "override", override_path.is_file(), "override")
        )
        seen.add(str(_normalized(override_path)).casefold())
    for name in (format_name, *[item for item in _FORMATS if item != format_name]):
        path = base / _format_day(day, name)
        if not _inside(path, base):
            return _unsupported(cfg, day, "日次ノート候補がVaultの外を指しています")
        key = str(_normalized(path)).casefold()
        if key in seen:
            continue
        candidates.append(DailyNoteCandidate(path, name, path.is_file(), "configured" if name == format_name else "known"))
        seen.add(key)
    candidate_tuple = tuple(candidates)
    if override is not None:
        chosen = candidate_tuple[0]
        return DailyNoteResolution(
            day, chosen.path, "selected" if chosen.exists else "missing", "override", candidate_tuple, None
        )
    existing = [candidate for candidate in candidate_tuple if candidate.exists]
    if len(existing) > 1:
        return DailyNoteResolution(day, None, "conflict", "none", candidate_tuple, "複数の日次ノート候補が存在します")
    if existing:
        selected = existing[0]
        if selected.source == "known":
            return DailyNoteResolution(day, None, "conflict", "none", candidate_tuple, "設定された形式以外の日次ノートだけが存在します")
        by = "configured" if selected.source == "configured" else "default"
        return DailyNoteResolution(day, selected.path, "selected", by, candidate_tuple, None)
    configured = next(candidate for candidate in candidate_tuple if candidate.source == "configured")
    return DailyNoteResolution(day, configured.path, "missing", "configured", candidate_tuple, None)


__all__ = [
    "DailyNoteCandidate", "DailyNoteResolution", "DailyNoteResolutionError",
    "resolve_daily_note", "validate_daily_override", "validate_daily_notes_dir",
]
