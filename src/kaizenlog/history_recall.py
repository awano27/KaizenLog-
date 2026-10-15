"""Private-safe, deterministic views over the local stats history.

These functions only read stats and ledgers.  They deliberately do not use a
``latest`` state to recreate a past state: KZN annotations are labelled as the
current ledger state.
"""

from __future__ import annotations

import json
from calendar import monthrange
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from math import isfinite
from pathlib import Path
from typing import Any

from .memory import MemoryEntry, load_entries
from .privacy_filter import is_private_block


@dataclass(frozen=True)
class RecallHit:
    day: str
    source: str
    text: str


def _safe_text(value: object, redactor: Callable[[str], str] | None) -> str:
    text = " ".join(str(value or "").split())
    if redactor is not None:
        text = " ".join(str(redactor(text) or "").split())
    return text


def _markdown_text(value: object, redactor: Callable[[str], str] | None) -> str:
    """Make redacted free text inert before embedding it in Markdown."""
    text = _safe_text(value, redactor)
    for token in ("\\", "`", "*", "_", "[", "]", "(", ")", "!", "<", ">", "|", "#"):
        text = text.replace(token, "\\" + token)
    return text


def _private(category: object, title: object, app: object, patterns: Sequence[str]) -> bool:
    """Use the shared privacy gate, including configured title patterns."""
    return is_private_block(
        category=str(category or ""),
        title=str(title or ""),
        app=str(app or ""),
        private_title_patterns=patterns,
    )


def _load_stats(stats_dir: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield valid JSON-object stats in date/filename order; bad files are ignored."""
    try:
        paths = sorted(Path(stats_dir).glob("*.json"))
    except OSError:
        return
    for path in paths:
        try:
            date.fromisoformat(path.stem)
        except ValueError:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        day = str(raw.get("day") or "")
        if day != path.stem:
            continue
        yield day, raw


def _load_day(stats_dir: Path, target: date) -> dict[str, Any] | None:
    """Read exactly one canonical date path for anniversary rendering."""
    day = target.isoformat()
    path = Path(stats_dir) / f"{day}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("day") != day:
        return None
    return raw


def _stat_lines(
    stat: dict[str, Any],
    *,
    redactor: Callable[[str], str] | None,
    private_title_patterns: Sequence[str],
) -> Iterator[tuple[str, str, str]]:
    blocks = stat.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            category = block.get("category")
            title = block.get("title")
            app = block.get("app")
            if _private(category, title, app, private_title_patterns):
                continue
            raw_text = " ".join(" / ".join(str(v) for v in (category, title, app) if v).split())
            text = _markdown_text(raw_text, redactor)
            if text:
                yield "block", text, raw_text

    ai = stat.get("ai")
    if isinstance(ai, dict):
        digests = ai.get("session_digests")
        if isinstance(digests, list):
            for digest in digests:
                if not isinstance(digest, dict):
                    continue
                title = digest.get("title")
                project = digest.get("project")
                if _private("", title, project, private_title_patterns):
                    continue
                raw_text = " ".join(" / ".join(str(v) for v in (project, title) if v).split())
                text = _markdown_text(raw_text, redactor)
                if text:
                    yield "session", text, raw_text

    effort = stat.get("effort")
    minutes = effort.get("minutes") if isinstance(effort, dict) else None
    if isinstance(minutes, dict):
        for name, value in sorted(minutes.items(), key=lambda item: str(item[0])):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
                continue
            if str(name) == "（私的）" or _private("", name, "", private_title_patterns):
                continue
            raw_text = f"工数 {name}: {float(value):g}分"
            text = _safe_text(raw_text, redactor)
            if text:
                yield "effort", text, raw_text


def search_history(
    stats_dir: Path,
    keyword: str,
    *,
    redactor: Callable[[str], str] | None = None,
    private_title_patterns: Sequence[str] = (),
) -> list[RecallHit]:
    """Search every local stats file without writing or exposing private text."""
    needle = " ".join((keyword or "").casefold().split())
    if not needle:
        return []
    hits: list[RecallHit] = []
    for day, stat in _load_stats(stats_dir):
        for source, text, raw_text in _stat_lines(
            stat, redactor=redactor, private_title_patterns=private_title_patterns
        ):
            # The query is user-supplied and is checked before display
            # truncation.  Only the redacted value is ever returned.
            if needle in raw_text.casefold():
                hits.append(RecallHit(day, source, text))
    return sorted(hits, key=lambda hit: (hit.day, hit.source, hit.text))


def _months_before(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 - months
    year, month0 = divmod(index, 12)
    month = month0 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _confirmed_adopted(entries: Sequence[MemoryEntry], target: date) -> list[MemoryEntry]:
    found: list[MemoryEntry] = []
    for entry in entries:
        if entry.date != target.isoformat() or entry.verdict_stage != "confirmed":
            continue
        decision = entry.decision if isinstance(entry.decision, dict) else {}
        if decision.get("choice") == "adopt" or entry.status == "adopted":
            found.append(entry)
    return sorted(found, key=lambda entry: entry.id)


def render_anniversaries(
    stats_dir: Path,
    memory_dir: Path,
    as_of: date,
    *,
    redactor: Callable[[str], str] | None = None,
    private_title_patterns: Sequence[str] = (),
) -> str:
    """Render 1/3-calendar-month memories; missing days stay explicitly missing."""
    entries = load_entries(memory_dir)
    lines = ["## 過去の今日", "", "※ KZN は現在の台帳状態であり、当時の状態は復元しません。"]
    for months in (1, 3):
        target = _months_before(as_of, months)
        label = f"{months}か月前（{target.isoformat()}）"
        lines.extend(["", f"### {label}", ""])
        stat = _load_day(stats_dir, target)
        if stat is None:
            lines.append("- 記録なし")
            continue
        history = list(
            (source, text) for source, text, _raw in _stat_lines(
                stat, redactor=redactor, private_title_patterns=private_title_patterns
            )
        )
        if history:
            for source, text in history[:3]:
                lines.append(f"- {source}: {text}")
        else:
            lines.append("- 表示できる記録なし")
        adopted = _confirmed_adopted(entries, target)
        for entry in adopted:
            if _private("", entry.action, "", private_title_patterns):
                continue
            action = _markdown_text(entry.action, redactor)
            lines.append(f"- {entry.id}: {action}（現在の台帳状態: confirmed / adopted）")
    return "\n".join(lines) + "\n"
