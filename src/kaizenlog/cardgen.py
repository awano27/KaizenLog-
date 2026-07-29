"""パーソナルMETR実験カード: 決定論 SVG（外部依存なし）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .vault import atomic_write_text


@dataclass
class AbtestCardData:
    experiment_id: str
    period_label: str  # e.g. 2026-07-01 〜 2026-07-28
    sample_ai_days: int
    sample_non_ai_days: int
    predict_pct: float | None
    felt_pct: float | None
    measured_pct: float | None
    invalid_reason: str | None = None  # 不成立時


def _bar(
    x: int, y: int, width: int, max_w: int, label: str, value_s: str, color: str
) -> str:
    w = max(0, min(int(width), max_w))
    return (
        f'<text x="{x}" y="{y}" font-size="14" fill="#222">{escape(label)}</text>'
        f'<rect x="{x + 80}" y="{y - 12}" width="{w}" height="16" fill="{color}" rx="2"/>'
        f'<text x="{x + 90 + w}" y="{y}" font-size="13" fill="#333">{escape(value_s)}</text>'
    )


def render_abtest_svg(data: AbtestCardData) -> str:
    """well-formed SVG 文字列を返す。"""
    title = f"abtest {escape(data.experiment_id)}"
    period = escape(data.period_label)
    samples = (
        f"AI日 {data.sample_ai_days} / 非AI日 {data.sample_non_ai_days}"
    )
    if data.invalid_reason:
        reason = escape(data.invalid_reason)
        body = (
            f'<text x="40" y="100" font-size="18" fill="#a00">不成立</text>'
            f'<text x="40" y="130" font-size="14" fill="#444">{reason}</text>'
            f'<text x="40" y="160" font-size="13" fill="#666">{escape(samples)}</text>'
        )
    else:
        # バー幅: 絶対値 0-100% を 200px にマップ
        def w(v: float | None) -> int:
            if v is None:
                return 0
            return int(min(200, abs(v) * 2))

        def fmt(v: float | None) -> str:
            if v is None:
                return "—"
            sign = "+" if v > 0 else ""
            return f"{sign}{v:g}%"

        body = (
            _bar(40, 100, w(data.predict_pct), 200, "予測", fmt(data.predict_pct), "#4C8BF5")
            + _bar(40, 140, w(data.felt_pct), 200, "体感", fmt(data.felt_pct), "#34A853")
            + _bar(40, 180, w(data.measured_pct), 200, "実測", fmt(data.measured_pct), "#EA4335")
            + f'<text x="40" y="220" font-size="12" fill="#666">{escape(samples)}</text>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="260" viewBox="0 0 480 260">\n'
        '<rect width="480" height="260" fill="#fafafa"/>\n'
        f'<text x="40" y="36" font-size="18" font-weight="bold" fill="#111">{title}</text>\n'
        f'<text x="40" y="60" font-size="13" fill="#555">{period}</text>\n'
        f"{body}\n"
        "</svg>\n"
    )


def write_abtest_card(path: Path, data: AbtestCardData) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, render_abtest_svg(data))
    return path
