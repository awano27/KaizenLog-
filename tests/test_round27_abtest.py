"""第27弾 §E: パーソナル METR abtest。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from kaizenlog.cardgen import AbtestCardData, render_abtest_svg, write_abtest_card
from kaizenlog.experiments import (
    compute_abtest_effect,
    create_abtest,
    finish_abtest,
    format_abtest_journal_line,
    is_ai_day,
    load_abtests,
    parse_predict_pct,
)


def _stat(day: date, *, api_calls: int, dev_min: float) -> dict:
    return {
        "version": 2,
        "day": day.isoformat(),
        "total_minutes": dev_min + 10,
        "by_category": {"開発": dev_min, "その他": 10},
        "ai": {
            "sessions": 1 if api_calls else 0,
            "api_calls": api_calls,
            "internal_ai_sessions": 0,
        },
    }


def test_e5_ai_day_split_and_internal_excluded():
    # api_calls=0 → 非AI日
    assert is_ai_day(_stat(date(2026, 7, 1), api_calls=0, dev_min=60)) is False
    assert is_ai_day(_stat(date(2026, 7, 1), api_calls=3, dev_min=60)) is True
    # internal は stats 側で既に除外済みの前提（api_calls が user 分のみ）
    s = _stat(date(2026, 7, 1), api_calls=0, dev_min=60)
    s["ai"]["internal_ai_sessions"] = 5
    assert is_ai_day(s) is False


def _pre_stats_for(start: date, days: int = 28, dev_min: float = 80.0) -> list[dict]:
    """開始前の同曜日 baseline 用。"""
    return [
        _stat(start - timedelta(days=i + 1), api_calls=0, dev_min=dev_min)
        for i in range(days)
    ]


def test_e5_sample_shortage_invalid():
    start = date(2026, 7, 1)
    pre = _pre_stats_for(start)
    stats = [
        _stat(start + timedelta(days=i), api_calls=1, dev_min=100)
        for i in range(5)
    ]
    # 非AI日ゼロ
    measured, ai_n, non_n, reason = compute_abtest_effect(
        stats, start=start, end=start + timedelta(days=4), pre_stats=pre
    )
    assert measured is None
    assert non_n < 3
    assert reason and "サンプル不足" in reason


def test_e5_effect_with_enough_samples():
    start = date(2026, 7, 1)
    pre = _pre_stats_for(start, dev_min=80.0)
    stats = []
    # AI日: 高開発時間
    for i in range(5):
        stats.append(
            _stat(start + timedelta(days=i), api_calls=2, dev_min=120.0)
        )
    # 非AI日: 低
    for i in range(5, 10):
        stats.append(
            _stat(start + timedelta(days=i), api_calls=0, dev_min=60.0)
        )
    measured, ai_n, non_n, reason = compute_abtest_effect(
        stats, start=start, end=start + timedelta(days=9), pre_stats=pre
    )
    assert reason is None
    assert ai_n >= 1 and non_n >= 3
    assert measured is not None
    assert measured > 0  # AI日 index の方が高い → 正の効果


def test_e5_svg_well_formed_and_invalid_design(tmp_path: Path):
    ok = render_abtest_svg(
        AbtestCardData(
            experiment_id="ABT-1",
            period_label="2026-07-01 〜 2026-07-28",
            sample_ai_days=5,
            sample_non_ai_days=5,
            predict_pct=30,
            felt_pct=20,
            measured_pct=-12,
        )
    )
    ET.fromstring(ok)
    bad = render_abtest_svg(
        AbtestCardData(
            experiment_id="ABT-2",
            period_label="p",
            sample_ai_days=1,
            sample_non_ai_days=1,
            predict_pct=30,
            felt_pct=10,
            measured_pct=None,
            invalid_reason="実測不成立(サンプル不足: 1日)",
        )
    )
    root = ET.fromstring(bad)
    assert "不成立" in ET.tostring(root, encoding="unicode")
    p = write_abtest_card(tmp_path / "c.svg", AbtestCardData(
        experiment_id="ABT-3",
        period_label="p",
        sample_ai_days=0,
        sample_non_ai_days=0,
        predict_pct=0,
        felt_pct=0,
        measured_pct=None,
        invalid_reason="x",
    ))
    ET.fromstring(p.read_text(encoding="utf-8"))


def test_e5_frontmatter_roundtrip(tmp_path: Path):
    exp_dir = tmp_path / "exp"
    path = create_abtest(exp_dir, today=date(2026, 7, 1), predict_pct=30, days=28)
    items = load_abtests(exp_dir)
    assert len(items) == 1
    assert items[0].predict_pct == 30
    assert items[0].status == "running"
    exp = items[0]
    finish_abtest(
        exp,
        felt_pct=20,
        card_rel_path="cards/abtest.svg",
        measured_pct=-12,
        invalid_reason=None,
        sample_ai=5,
        sample_non=5,
        as_of=date(2026, 7, 29),
    )
    items2 = load_abtests(exp_dir)
    assert items2[0].status == "finished"
    assert items2[0].felt_pct == 20
    assert items2[0].measured_pct == -12
    line = format_abtest_journal_line(items2[0])
    assert "予測+30%" in line
    assert "体感+20%" in line
    assert "実測-12%" in line


def test_e5_parse_predict():
    assert parse_predict_pct("+30") == 30
    assert parse_predict_pct("30%") == 30
    assert parse_predict_pct("-12") == -12
