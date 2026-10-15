"""Deterministic reader-facing weekly stories and monthly albums."""

from __future__ import annotations

import json
from calendar import monthrange
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from math import isfinite
from pathlib import Path
from statistics import median
from xml.sax.saxutils import escape

from .coachledger import load_coach_ledger
from .memory import MemoryEntry
from .vault import atomic_write_text


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if isfinite(value) else None


def _nonnegative_number(value: object) -> float | None:
    value = _number(value)
    return value if value is not None and value >= 0 else None


def _loop_tax(stat: Mapping[str, object]) -> float | None:
    ai = stat.get("ai")
    loop = ai.get("loop_tax") if isinstance(ai, Mapping) else None
    value = _number(loop.get("total_wasted_tokens")) if isinstance(loop, Mapping) else None
    return value if value is not None and value >= 0 else None


def _dated_stats(
    stats_list: Sequence[Mapping[str, object]], start: date, end: date
) -> list[Mapping[str, object]]:
    """Accept each canonical day once, and never let an out-of-range row count."""
    by_day: dict[date, Mapping[str, object]] = {}
    for stat in stats_list:
        raw_day = stat.get("day")
        if not isinstance(raw_day, str):
            continue
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            continue
        if start <= day <= end:
            by_day.setdefault(day, stat)
    return [by_day[day] for day in sorted(by_day)]


def _markdown_text(value: object, redactor: Callable[[str], str] | None) -> str:
    text = " ".join(str(value or "").split())
    if redactor:
        text = " ".join(str(redactor(text) or "").split())
    for token in ("\\", "`", "*", "_", "[", "]", "(", ")", "!", "<", ">", "|", "#"):
        text = text.replace(token, "\\" + token)
    return text


def _private_text(value: object, patterns: Sequence[str]) -> bool:
    from .privacy_filter import is_private_block

    return is_private_block(title=str(value or ""), private_title_patterns=patterns)


def render_weekly_story(
    stats_list: Sequence[Mapping[str, object]],
    memory_dir: Path,
    week_start: date,
    *,
    baseline_stats: Sequence[Mapping[str, object]] = (),
) -> str:
    """A factual weekly narrative. Missing observations are never zero-filled."""
    week_end = week_start + timedelta(days=6)
    stats_list = _dated_stats(stats_list, week_start, week_end)
    baseline_stats = _dated_stats(
        baseline_stats, week_start - timedelta(days=7), week_start - timedelta(days=1)
    )
    tax_values = [v for stat in stats_list if (v := _loop_tax(stat)) is not None]
    tax_sentence = (
        f"ループ税は{sum(tax_values):g} tokens（{len(tax_values)}日分）"
        if tax_values else "ループ税は未判定（記録なし）"
    )
    current_total = [v for stat in stats_list if (v := _nonnegative_number(stat.get("total_minutes"))) is not None]
    base_total = [v for stat in baseline_stats if (v := _nonnegative_number(stat.get("total_minutes"))) is not None]
    if len(current_total) >= 3 and len(base_total) >= 3:
        delta = median(current_total) - median(base_total)
        baseline_sentence = f"日次合計の7日中央値差は{delta:+g}分"
    else:
        baseline_sentence = "日次合計の7日中央値差は未判定（比較可能な記録不足）"

    entries = [
        entry for entry in load_coach_ledger(memory_dir)
        if week_start.isoformat() <= str(entry.verdict_date or entry.applied_on or "")[:10] <= week_end.isoformat()
    ]
    wins = sum(entry.status == "pass" for entry in entries)
    losses = sum(entry.status == "fail" for entry in entries)
    watching = sum(entry.status == "watching" for entry in entries)
    unmeasurable = sum(entry.status == "unmeasurable" for entry in entries)
    rolled_back = sum(entry.status == "rolled_back" for entry in entries)
    judged = wins + losses
    coach_sentence = (
        f"コーチは{wins}勝{losses}敗（{round(wins * 100 / judged)}% / 判定{judged}件）"
        if judged else "コーチは未判定（判定済み記録なし）"
    )
    lines = [
        "## 今週の物語",
        "",
        f"- {week_start.isoformat()} の週は、{tax_sentence}。",
        f"- {baseline_sentence}。",
        f"- {coach_sentence}（対象週の判定日／現在の台帳状態）。監視中 {watching}件、測定不能 {unmeasurable}件、ロールバック済み {rolled_back}件。",
        "- 欠測は未判定として扱い、点数や称号には変換しません。",
    ]
    return "\n".join(lines) + "\n"


def _raw_prompt_promotions(memory_dir: Path, year: int, month: int) -> int:
    """Count recorded promotion events, retaining history rather than latest status."""
    path = Path(memory_dir) / "prompt_clusters.jsonl"
    if not path.is_file():
        return 0
    seen: set[str] = set()
    prefix = f"{year:04d}-{month:02d}-"
    try:
        rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    for row in rows:
        try:
            item = json.loads(row)
        except (TypeError, ValueError):
            continue
        if not isinstance(item, dict) or item.get("status") != "skilled":
            continue
        marked = str(item.get("marked_on") or "")
        identifier = str(item.get("id") or "")
        if identifier and marked.startswith(prefix):
            seen.add(identifier)
    return len(seen)


def _confirmed_month(entries: Sequence[MemoryEntry], year: int, month: int) -> list[MemoryEntry]:
    prefix = f"{year:04d}-{month:02d}-"
    latest: dict[str, MemoryEntry] = {}
    for entry in entries:
        latest[entry.id] = entry
    selected = []
    for entry in latest.values():
        if (
            str(entry.date or "").startswith(prefix)
            and entry.verdict_stage == "confirmed"
            and entry.verdict in ("pass", "fail")
        ):
            selected.append(entry)
    return sorted(selected, key=lambda entry: entry.id)


def render_monthly_album(
    stats_list: Sequence[Mapping[str, object]],
    entries: Sequence[MemoryEntry],
    memory_dir: Path,
    *,
    year: int,
    month: int,
    asset_paths: Sequence[str] = (),
    redactor: Callable[[str], str] | None = None,
    private_title_patterns: Sequence[str] = (),
) -> str:
    """Render a factual album; assets are referenced only when the caller wrote them."""
    lines = ["## 月刊アルバム", "", f"### {year:04d}-{month:02d} 提案の確定KZN（現在の台帳状態）", ""]
    confirmed = _confirmed_month(entries, year, month)
    if confirmed:
        for entry in confirmed:
            if _private_text(entry.action, private_title_patterns):
                continue
            lines.append(
                f"- {entry.id}: {_markdown_text(entry.action, redactor)}"
                f"（現在: {entry.status} / 判定: {entry.verdict}）"
            )
    else:
        lines.append("- 該当なし")
    lines.extend(["", f"- プロンプト昇格: {_raw_prompt_promotions(memory_dir, year, month)}件"])
    month_start = date(year, month, 1).isoformat()
    month_end = date(year, month, monthrange(year, month)[1]).isoformat()
    coach = [
        entry for entry in load_coach_ledger(memory_dir)
        if month_start <= str(entry.verdict_date or entry.applied_on or "")[:10] <= month_end
    ]
    wins = sum(entry.status == "pass" for entry in coach)
    losses = sum(entry.status == "fail" for entry in coach)
    judged = wins + losses
    if judged:
        lines.append(f"- コーチ勝率: {wins}勝{losses}敗（{round(100 * wins / judged)}% / 判定{judged}件、対象月の判定日／現在の台帳状態）")
    else:
        lines.append("- コーチ勝率: 未判定（判定済み記録なし）")
    unmeasurable = sum(entry.status == "unmeasurable" for entry in coach)
    rolled_back = sum(entry.status == "rolled_back" for entry in coach)
    lines.append(f"- コーチ補足: 測定不能 {unmeasurable}件 / ロールバック済み {rolled_back}件（対象月の台帳日付）")
    if asset_paths:
        lines.extend(["", "### 推移カード", ""])
        lines.extend(f"![[{path}]]" for path in asset_paths)
    return "\n".join(lines) + "\n"


def render_sparkline_svg(label: str, points: Sequence[tuple[str, float | None]]) -> str:
    """Render dated values as separate polylines so missing days remain visual gaps."""
    finite = [(day, _number(value)) for day, value in points]
    values = [value for _, value in finite if value is not None]
    title = escape(label)
    period = escape(f"{points[0][0]} 〜 {points[-1][0]}") if points else "記録なし"
    if not values:
        body = '<text x="40" y="130" font-size="16" fill="#555">記録なし</text>'
    else:
        low, high = min(values), max(values)
        span = high - low
        runs: list[list[str]] = []
        current: list[str] = []
        count = max(1, len(points) - 1)
        for index, (_, value) in enumerate(finite):
            if value is None:
                if current:
                    runs.append(current)
                    current = []
                continue
            x = 40 + 400 * index / count
            y = 190 if span == 0 else 190 - 110 * (value - low) / span
            current.append(f"{x:.1f},{y:.1f}")
        if current:
            runs.append(current)
        polylines = "\n".join(
            f'<polyline points="{" ".join(run)}" fill="none" stroke="#4C8BF5" stroke-width="3"/>'
            for run in runs
        )
        dots = "\n".join(
            f'<circle cx="{point.split(",")[0]}" cy="{point.split(",")[1]}" r="3" fill="#4C8BF5"/>'
            for run in runs for point in run
        )
        body = (
            f'<text x="40" y="225" font-size="12" fill="#555">min {low:g} / max {high:g}</text>\n'
            + polylines + "\n" + dots
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="260" viewBox="0 0 480 260">\n'
        '<rect width="480" height="260" fill="#fafafa"/>\n'
        f'<text x="40" y="36" font-size="18" font-weight="bold" fill="#111">{title}</text>\n'
        f'<text x="40" y="60" font-size="13" fill="#555">{period}</text>\n'
        f"{body}\n</svg>\n"
    )


def write_monthly_sparklines(
    assets_dir: Path,
    year: int,
    month: int,
    stats_list: Sequence[Mapping[str, object]],
    *,
    write: bool,
) -> list[Path]:
    """Write cards only for an explicit write request; previews remain side-effect free."""
    if not write:
        return []
    month_start = date(year, month, 1)
    month_end = date(year, month, monthrange(year, month)[1])
    accepted = _dated_stats(stats_list, month_start, month_end)
    by_day = {str(stat.get("day") or ""): stat for stat in accepted}
    last = monthrange(year, month)[1]
    paths: list[Path] = []
    metrics = (
        ("loop-tax", lambda stat: _loop_tax(stat)),
        ("est-cost-usd", lambda stat: _nonnegative_number((stat.get("ai") or {}).get("est_cost_usd")) if isinstance(stat.get("ai"), Mapping) else None),
    )
    for slug, get_value in metrics:
        points = []
        for day_n in range(1, last + 1):
            day_s = date(year, month, day_n).isoformat()
            stat = by_day.get(day_s)
            points.append((day_s, get_value(stat) if isinstance(stat, Mapping) else None))
        path = Path(assets_dir) / f"{slug}-{year:04d}-{month:02d}.svg"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, render_sparkline_svg(slug, points))
        paths.append(path)
    return paths
