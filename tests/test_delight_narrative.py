"""Synthetic contracts for deterministic weekly and monthly reader views."""
from __future__ import annotations

import json
from datetime import date

from kaizenlog.memory import MemoryEntry
from kaizenlog.narrative import (
    render_monthly_album,
    render_sparkline_svg,
    render_weekly_story,
    write_monthly_sparklines,
)


def test_weekly_story_uses_only_measured_values_and_coach_denominator(tmp_path):
    stats = [
        {"day": "2026-08-03", "ai": {"loop_tax": {"total_wasted_tokens": 12}}},
        {"day": "2026-08-04", "ai": {"loop_tax": {"total_wasted_tokens": 8}}},
        {"day": "2026-08-05", "ai": {}},
    ]
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "coach_ledger.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"id": "CCH-1", "status": "pass", "verdict_date": "2026-08-04"}),
                json.dumps({"id": "CCH-2", "status": "fail", "verdict_date": "2026-08-05"}),
                json.dumps({"id": "CCH-3", "status": "watching", "applied_on": "2026-08-06"}),
            ]
        ),
        encoding="utf-8",
    )

    text = render_weekly_story(stats, memory, date(2026, 8, 3))

    assert "今週の物語" in text
    assert "ループ税は20 tokens（2日分）" in text
    assert "1勝1敗（50% / 判定2件）" in text
    assert "未判定" in text


def test_monthly_album_only_lists_confirmed_adopted_kzn_and_actual_promotions(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "prompt_clusters.jsonl").write_text(
        json.dumps({"id": "PRM-1", "status": "skilled", "marked_on": "2026-08-10"}) + "\n"
        + json.dumps({"id": "PRM-2", "status": "skilled", "marked_on": "2026-09-01"}) + "\n",
        encoding="utf-8",
    )
    entries = [
        MemoryEntry("KZN-1", "2026-08-02", "adopted action", verdict="pass", verdict_stage="confirmed", decision={"choice": "adopt"}),
        MemoryEntry("KZN-2", "2026-08-03", "confirmed but not adopted", verdict="pass", verdict_stage="confirmed"),
        MemoryEntry("KZN-3", "2026-08-03", "private <script> ![image](x)", verdict="pass", verdict_stage="confirmed"),
        MemoryEntry("KZN-1", "2026-08-02", "latest confirmed", verdict="pass", verdict_stage="confirmed", status="done"),
    ]
    stats = [{"day": "2026-08-02", "ai": {"loop_tax": {"total_wasted_tokens": 2, "est_cost_usd": 0.1}}}]

    text = render_monthly_album(
        stats, entries, memory, year=2026, month=8,
        redactor=lambda text: text.replace("private", "[REDACTED]"),
        private_title_patterns=("script",),
    )

    assert "月刊アルバム" in text
    assert "KZN-1" in text
    assert "KZN-2" in text
    assert "latest confirmed" in text
    assert "KZN-3" not in text
    assert "プロンプト昇格: 1件" in text
    assert "assets" not in text  # preview does not imply asset creation


def test_sparkline_escapes_xml_and_keeps_missing_days_as_gaps(tmp_path):
    svg = render_sparkline_svg("loop & <tax>", [("2026-08-01", 1.0), ("2026-08-02", None), ("2026-08-03", 3.0)])
    assert "loop &amp; &lt;tax&gt;" in svg
    assert svg.count("<polyline") == 2
    assert svg.count("<circle") == 2
    assert "min 1" in svg and "max 3" in svg
    assert "nan" not in svg.lower()

    paths = write_monthly_sparklines(tmp_path / "assets", 2026, 8, [], write=False)
    assert paths == []
    assert not (tmp_path / "assets").exists()


def test_weekly_story_filters_dates_dedupes_and_needs_three_valid_values(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    current = [
        {"day": "2026-08-03", "total_minutes": 10, "ai": {"loop_tax": {"total_wasted_tokens": 1}}},
        {"day": "2026-08-03", "total_minutes": 999, "ai": {"loop_tax": {"total_wasted_tokens": 999}}},
        {"day": "2026-08-04", "total_minutes": 20, "ai": {"loop_tax": {"total_wasted_tokens": -2}}},
        {"day": "2026-08-05", "total_minutes": 30, "ai": {}},
        {"day": "2026-08-11", "total_minutes": 77, "ai": {"loop_tax": {"total_wasted_tokens": 77}}},
    ]
    baseline = [
        {"day": "2026-07-27", "total_minutes": 1},
        {"day": "2026-07-28", "total_minutes": 2},
        {"day": "2026-07-29", "total_minutes": 3},
        {"day": "2026-07-20", "total_minutes": 800},
    ]

    text = render_weekly_story(current, memory, date(2026, 8, 3), baseline_stats=baseline)

    assert "ループ税は1 tokens（1日分）" in text
    assert "7日中央値差は+18分" in text
    assert "999" not in text and "77" not in text
