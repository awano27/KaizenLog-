"""日次統計のJSON永続化。

Markdownの日誌とは別に、機械可読な統計を `.kaizenlog/stats/YYYY-MM-DD.json` に
蓄積する。パターン検出（patterns.py）や外部ツールがここから履歴を読む。
ドットフォルダなのでObsidianのファイルツリーには表示されない。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from .aiwork import AISession
from .focus import InputStats
from .report import DailySummary
from .vault import atomic_write_text


def _round_minutes(d: dict[str, float]) -> dict[str, float]:
    return {k: round(v, 1) for k, v in d.items()}


def build_stats(
    day: date,
    summary: DailySummary,
    cc_sessions: list[AISession],
    input_stats: InputStats | None = None,
) -> dict:
    projects: dict[str, dict] = {}
    for s in cc_sessions:
        p = projects.setdefault(
            s.project, {"sessions": 0, "turns": 0, "errors": 0, "fragmented": 0}
        )
        p["sessions"] += 1
        p["turns"] += s.user_turns
        p["errors"] += s.tool_errors
        p["fragmented"] += 1 if s.is_fragmented else 0

    stats = {
        "version": 1,
        "day": day.isoformat(),
        "total_minutes": round(summary.total_minutes, 1),
        "context_switches": summary.context_switches,
        "by_category": _round_minutes(summary.by_category),
        "by_app": _round_minutes(summary.by_app),
        "by_site": _round_minutes(summary.by_site),
        "blocks": [
            {
                "start": b.start.isoformat(),
                "end": b.end.isoformat(),
                "category": b.category,
                "app": b.app,
                "minutes": round(b.minutes, 1),
                "title": (b.titles[0][:60] if b.titles else ""),
            }
            for b in summary.blocks
        ],
        "ai": {
            "sessions": len(cc_sessions),
            "fragmented": sum(1 for s in cc_sessions if s.is_fragmented),
            "tool_errors": sum(s.tool_errors for s in cc_sessions),
            "interruptions": sum(s.interruptions for s in cc_sessions),
            "projects": projects,
        },
    }
    if input_stats is not None:
        stats["input"] = {
            "keypresses": input_stats.keypresses,
            "clicks": input_stats.clicks,
            "active_input_minutes": input_stats.active_input_minutes,
            "focus_blocks": len(input_stats.focus_blocks),
            "focus_minutes": round(input_stats.focus_minutes, 1),
        }
    return stats


def write_stats(
    stats_dir: Path,
    day: date,
    summary: DailySummary,
    cc_sessions: list[AISession],
    input_stats: InputStats | None = None,
) -> Path:
    stats_dir.mkdir(parents=True, exist_ok=True)
    path = stats_dir / f"{day.isoformat()}.json"
    atomic_write_text(
        path,
        json.dumps(build_stats(day, summary, cc_sessions, input_stats),
                   ensure_ascii=False, indent=1),
    )
    return path


def missing_days(stats_dir: Path, end_day: date, lookback: int) -> list[date]:
    """end_dayより前のlookback日間で、統計が存在しない日を古い順に返す（欠損日の補完用）。"""
    stats_dir = Path(stats_dir)
    return [
        d
        for i in range(lookback, 0, -1)
        for d in [end_day - timedelta(days=i)]
        if not (stats_dir / f"{d.isoformat()}.json").is_file()
    ]


def load_stats(stats_dir: Path, days: int, end_day: date) -> list[dict]:
    """end_dayを含む直近days日分の統計を古い順に返す（存在する日のみ）。"""
    out = []
    for i in range(days - 1, -1, -1):
        path = stats_dir / f"{(end_day - timedelta(days=i)).isoformat()}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # ValueError は JSONDecodeError と UnicodeDecodeError（部分書き込みで
            # 生じる不正UTF-8）の両方を覆う。1日分の破損で patterns/block を落とさない
            continue
        if isinstance(data, dict) and "day" in data:
            out.append(data)
    return out
