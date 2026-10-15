"""Marker-owned integrations for private-safe history and narrative views.

The renderers in :mod:`history_recall` and :mod:`narrative` are intentionally
read-only.  This module is their small Vault-facing boundary: each writer owns
only its named marker and leaves all surrounding user text untouched.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping

from .config import Config
from .file_lock import locked_read_modify_write
from .history_recall import render_anniversaries
from .human_input import HumanDay, human_overlay_lines, load_human_day
from .memory import load_entries
from .monthly import load_month_stats
from .narrative import (
    render_monthly_album,
    render_weekly_story as _render_weekly_story,
    write_monthly_sparklines,
)
from .privacy import make_redactor
from .stats import load_stats
from .vault import (
    atomic_write_text,
    read_text_preserve_newlines,
    upsert_section,
)
from .weekly_context import iso_week_label, weekly_review_path


DAILY_ANNIVERSARY_MARKER = "kaizenlog:anniversaries"
WEEKLY_STORY_MARKER = "kaizenlog:weekly-story"
MONTHLY_ALBUM_MARKER = "kaizenlog:monthly-album"


def render_daily_human_overlay(
    cfg: Config,
    as_of: date,
    *,
    source_stats: Mapping[str, object] | None = None,
) -> str:
    """Return the read-only daily human overlay for callers that render views."""
    return "\n".join(
        human_overlay_lines(load_human_day(cfg, as_of), source_stats=source_stats)
    )


def load_monthly_human_overlays(cfg: Config, year: int, month: int) -> dict[str, HumanDay]:
    """Load human inputs for a month without creating files or locks."""
    return {
        f"{year:04d}-{month:02d}-{day_n:02d}": human
        for day_n in range(1, monthrange(year, month)[1] + 1)
        if (human := load_human_day(cfg, date(year, month, day_n))).status == "loaded"
    }


def _privacy_options(cfg: Config):
    privacy = cfg.privacy
    return (
        make_redactor(privacy.redact_patterns, privacy.replacement),
        tuple(privacy.private_title_patterns),
    )


def render_daily_anniversaries(cfg: Config, as_of: date) -> str:
    """Build the daily history section from local stats and ledgers only."""
    redactor, private_title_patterns = _privacy_options(cfg)
    return render_anniversaries(
        cfg.stats_path,
        cfg.memory_path,
        as_of,
        redactor=redactor,
        private_title_patterns=private_title_patterns,
    )


def write_daily_anniversaries(cfg: Config, as_of: date) -> Path:
    """Upsert the anniversary marker in that day's note without touching other text."""
    from .vault import DailyNoteStore

    store = DailyNoteStore(cfg)
    with store.locked_note(as_of) as (path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            content = f"# {as_of.isoformat()}\n\n"
        updated = upsert_section(
            content,
            DAILY_ANNIVERSARY_MARKER,
            render_daily_anniversaries(cfg, as_of),
            position="bottom",
        )
        atomic_write_text(path, updated)
    return path


def render_weekly_story(cfg: Config, week_start: date) -> str:
    """Build a weekly story with the prior seven calendar days as its baseline."""
    week_end = week_start + timedelta(days=6)
    stats = load_stats(cfg.stats_path, days=14, end_day=week_end)
    current = [
        stat
        for stat in stats
        if week_start.isoformat() <= str(stat.get("day") or "") <= week_end.isoformat()
    ]
    baseline_end = week_start - timedelta(days=1)
    baseline_start = week_start - timedelta(days=7)
    baseline = [
        stat
        for stat in stats
        if baseline_start.isoformat() <= str(stat.get("day") or "") <= baseline_end.isoformat()
    ]
    return _render_weekly_story(current, cfg.memory_path, week_start, baseline_stats=baseline)


def _default_weekly_note(week_start: date) -> str:
    label = iso_week_label(week_start)
    return (
        "---\n"
        f'title: "{label} Weekly Review"\n'
        f"date: {week_start.isoformat()}\n"
        "tags: [type/weekly-review]\n"
        "---\n\n"
    )


def write_weekly_story(cfg: Config, week_start: date) -> Path:
    """Upsert the story beside, never inside, weekly-context's managed marker."""
    path = weekly_review_path(cfg.daily_notes_path, week_start)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = render_weekly_story(cfg, week_start)
    with locked_read_modify_write(path):
        content = (
            read_text_preserve_newlines(path)
            if path.is_file()
            else _default_weekly_note(week_start)
        )
        updated = upsert_section(
            content,
            WEEKLY_STORY_MARKER,
            body,
            position="bottom",
        )
        atomic_write_text(path, updated)
    return path


def preview_monthly_album(cfg: Config, year: int, month: int) -> str:
    """Return the album preview without creating a note or SVG asset."""
    redactor, private_title_patterns = _privacy_options(cfg)
    return render_monthly_album(
        load_month_stats(cfg.stats_path, year, month),
        load_entries(cfg.memory_path),
        cfg.memory_path,
        year=year,
        month=month,
        redactor=redactor,
        private_title_patterns=private_title_patterns,
    )


def write_monthly_album(cfg: Config, year: int, month: int) -> Path:
    """Write album and explicitly requested SVG cards to the monthly note area."""
    stats = load_month_stats(cfg.stats_path, year, month)
    assets_dir = cfg.monthly_path / "assets"
    asset_paths = write_monthly_sparklines(
        assets_dir, year, month, stats, write=True
    )
    redactor, private_title_patterns = _privacy_options(cfg)
    body = render_monthly_album(
        stats,
        load_entries(cfg.memory_path),
        cfg.memory_path,
        year=year,
        month=month,
        asset_paths=[f"assets/{path.name}" for path in asset_paths],
        redactor=redactor,
        private_title_patterns=private_title_patterns,
    )
    path = cfg.monthly_path / f"{year:04d}-{month:02d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_read_modify_write(path):
        content = (
            read_text_preserve_newlines(path)
            if path.is_file()
            else f"# {year:04d}-{month:02d}\n\n"
        )
        atomic_write_text(
            path,
            upsert_section(content, MONTHLY_ALBUM_MARKER, body, position="bottom"),
        )
    return path
