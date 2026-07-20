"""aw-watcher-input のイベントから入力量と「集中ブロック」を算出する。

集中ブロック = キーボード/マウス入力がほぼ途切れずに続いた一定時間以上の区間。
「画面は開いているが手が止まっている」時間と「実際に手を動かしている」時間を
区別し、フロー状態の時間を実験の指標（focus_blocks / focus_minutes）として
追跡できるようにする。

aw-watcher-input は5秒ごとのハートビートで presses / clicks / deltaX / deltaY を
送り、無入力期間は全ゼロのイベントに統合される。全ゼロのイベントは集中の
連続とはみなさない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .collector import _parse_events

FOCUS_MIN_MINUTES = 25.0    # この長さ以上入力が続いたら集中ブロックとみなす
FOCUS_MAX_GAP_MINUTES = 3.0  # この間隔までの入力の途切れは同一ブロックとして扱う


@dataclass
class FocusBlock:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass
class InputStats:
    keypresses: int = 0
    clicks: int = 0
    active_input_minutes: float = 0.0  # 何らかの入力があった時間の合計
    focus_blocks: list[FocusBlock] = field(default_factory=list)

    @property
    def focus_minutes(self) -> float:
        return sum(b.minutes for b in self.focus_blocks)


def compute_input_stats(
    raw: list[dict],
    min_block_minutes: float = FOCUS_MIN_MINUTES,
    max_gap_minutes: float = FOCUS_MAX_GAP_MINUTES,
    day_start: datetime | None = None,
    day_end: datetime | None = None,
) -> InputStats:
    """入力イベントを集計する。rawが空でもゼロ値のInputStatsを返す。

    day_start/day_end を渡すとイベントを日の範囲にクリップする
    （ActivityWatchは範囲に重なるイベントを返すため、深夜跨ぎの
    イベントが前日と二重計上されるのを防ぐ）。
    """
    presses = clicks = 0
    active_seconds = 0.0
    active_cursor: datetime | None = None  # 重複区間を二重計上しないための走査位置
    runs: list[list[datetime]] = []  # [start, end] 入力が続いた区間（gap統合済み）
    gap = timedelta(minutes=max_gap_minutes)

    for start, end, data in _parse_events(raw):
        if day_start is not None and start < day_start:
            start = day_start
        if day_end is not None and end > day_end:
            end = day_end
        if start >= end:
            continue
        p = int(data.get("presses", 0) or 0)
        c = int(data.get("clicks", 0) or 0)
        moved = abs(float(data.get("deltaX", 0) or 0)) + abs(float(data.get("deltaY", 0) or 0))
        if p == 0 and c == 0 and moved == 0:
            continue  # 無入力ハートビート（アイドル区間）は連続にカウントしない
        presses += p
        clicks += c
        # 重複するハートビートがあっても実時間を二重計上しない（区間の和集合）
        eff_start = start if active_cursor is None else max(start, active_cursor)
        if eff_start < end:
            active_seconds += (end - eff_start).total_seconds()
        active_cursor = end if active_cursor is None else max(active_cursor, end)
        if runs and start - runs[-1][1] <= gap:
            runs[-1][1] = max(runs[-1][1], end)
        else:
            runs.append([start, end])

    blocks = [
        FocusBlock(s, e)
        for s, e in runs
        if (e - s).total_seconds() / 60 >= min_block_minutes
    ]
    return InputStats(
        keypresses=presses,
        clicks=clicks,
        active_input_minutes=round(active_seconds / 60, 1),
        focus_blocks=blocks,
    )
