"""直近N日の中央値ベースライン（決定論）。"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _nested(stats: Mapping[str, Any], key: str) -> float | None:
    """key は 'total_minutes' または 'ai.tool_errors' / 'by_category.AI作業' 形式。"""
    if "." not in key:
        return _as_float(stats.get(key))
    cur: object = stats
    for part in key.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return _as_float(cur)


def baseline(
    stats_history: Sequence[Mapping[str, Any]],
    key: str,
    *,
    days: int = 7,
    today_value: float | None = None,
) -> tuple[float | None, str]:
    """直近 days 日（当日を除く）の中央値と当日比較ラベルを返す。

    Returns:
        (median, label)
        - 測定日が3日未満: (None, "")
        - today_value が無い / median が 0 で倍率不能: (median, "") または
          中央値だけ使う呼び出し側向けに (median, "中央値なみ" 等は today がある時のみ)
        label 例: "1.4倍" / "0.6倍" / "中央値なみ"
    """
    values: list[float] = []
    # history は古い→新しい想定。末尾から当日を除いて最大 days 日
    items = list(stats_history)
    # 当日を除外するため today_value が渡されても history の最終日は呼び出し側で除く
    for item in items:
        if not isinstance(item, Mapping):
            continue
        v = _nested(item, key)
        if v is not None:
            values.append(v)
    # 直近 days 件だけ（history が end_day 前日までならそのまま末尾）
    if len(values) > days:
        values = values[-days:]
    if len(values) < 3:
        return None, ""
    med = float(statistics.median(values))
    if today_value is None:
        return med, ""
    if med <= 0:
        # 中央値0のとき倍率は出さない（ゼロ除算回避）
        if today_value == 0:
            return med, "中央値なみ"
        return med, ""
    ratio = today_value / med
    if 0.85 <= ratio <= 1.15:
        return med, "中央値なみ"
    # 表示は小数1桁（1.0倍は整数っぽく）
    if abs(ratio - round(ratio)) < 0.05:
        label = f"{int(round(ratio))}倍"
    else:
        label = f"{ratio:.1f}倍"
    return med, label


def format_with_baseline(
    value_text: str,
    median: float | None,
    label: str,
    *,
    median_fmt: str | None = None,
) -> str:
    """`863回（7日中央値 512 の 1.7倍）` 形式。中央値無しなら value_text のみ。"""
    if median is None or not label:
        return value_text
    med_s = median_fmt if median_fmt is not None else (
        str(int(median)) if float(median).is_integer() else f"{median:g}"
    )
    return f"{value_text}（7日中央値 {med_s} の {label}）"
