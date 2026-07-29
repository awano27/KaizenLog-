"""第26弾: 目標トレース（1行目標の計測連動）。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.goal import (
    format_goal_section,
    parse_goal_text,
    read_goal,
    upsert_goal_in_content,
    write_goal,
)
from kaizenlog.stats import build_stats
from kaizenlog.vault import (
    ACTIONS_MARKER,
    ADVICE_MARKER,
    GOAL_MARKER,
    default_frontmatter,
    extract_section,
    upsert_section,
)
from kaizenlog.weekly_context import render_weekly_context
from kaizenlog.report import DailySummary
from kaizenlog.aiwork import AISession
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


KNOWN = frozenset({"執筆・ノート", "開発", "AI作業", "エンタメ", "ブラウジング"})


def test_g1_parse_goal_with_and_without_category():
    g = parse_goal_text("🎯 今日の目標: リリースノートの下書き @執筆・ノート", KNOWN)
    assert g is not None
    assert g.text == "リリースノートの下書き"
    assert g.category == "執筆・ノート"

    g2 = parse_goal_text("今日の目標: テストを通す", KNOWN)
    assert g2 is not None
    assert g2.text == "テストを通す"
    assert g2.category is None

    # 不一致カテゴリは構造化しない（警告なし）
    g3 = parse_goal_text("今日の目標: 何かする @存在しないカテゴリ", KNOWN)
    assert g3 is not None
    assert g3.text == "何かする"
    assert g3.category is None


def test_g1_goal_section_ownership_survives_advice_upsert(tmp_path: Path):
    """advise 相当の advice 区間 upsert で goal が消えない。"""
    day = date(2026, 7, 29)
    content = default_frontmatter(day) + "\n"
    content = upsert_section(
        content, ACTIONS_MARKER, "### 今日のアクション\n- [ ] 例\n", position="top"
    )
    content = upsert_goal_in_content(
        content, format_goal_section("リリースノート下書き", "執筆・ノート")
    )
    assert extract_section(content, GOAL_MARKER)
    # advice 再実行をシミュレート
    content2 = upsert_section(
        content, ADVICE_MARKER, "## Kaizen\n\n### 今日の結論\n\nx\n", position="bottom"
    )
    goal_body = extract_section(content2, GOAL_MARKER)
    assert goal_body is not None
    assert "リリースノート下書き" in goal_body
    g = read_goal(content2, KNOWN)
    assert g is not None and g.category == "執筆・ノート"


def test_g1_write_goal_and_stats_fields(tmp_path: Path):
    day = date(2026, 7, 29)
    notes = tmp_path / "notes"
    path, g = write_goal(
        notes,
        day,
        "リリースノートの下書き @執筆・ノート",
        known_categories=KNOWN,
    )
    assert path.is_file()
    assert g.category == "執筆・ノート"
    body = path.read_text(encoding="utf-8")
    assert "kaizenlog:goal:start" in body

    summary = DailySummary(
        day=day,
        total_minutes=100.0,
        by_category={"執筆・ノート": 12.5, "開発": 50.0},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=10,
        by_site={},
    )
    stats = build_stats(
        day,
        summary,
        [],
        goal_text="リリースノートの下書き",  # redact 後想定
        goal_category="執筆・ノート",
    )
    assert stats["goal_text"] == "リリースノートの下書き"
    assert stats["goal_category"] == "執筆・ノート"


def test_g2_evidence_includes_goal_facts():
    stats = {
        "version": 2,
        "day": "2026-07-29",
        "total_minutes": 200.0,
        "context_switches": 10,
        "ai_activity_blocks": 0,
        "by_category": {"執筆・ノート": 12.5, "開発": 80.0},
        "by_app": {},
        "by_site": {},
        "blocks": [],
        "ai": {"sessions": 0, "fragmented": 0, "tool_errors": 0, "interruptions": 0},
        "goal_text": "リリースノートの下書き",
        "goal_category": "執筆・ノート",
    }
    hist = [
        {
            "day": f"2026-07-{20+i:02d}",
            "total_minutes": 180.0,
            "context_switches": 5,
            "goal_text": "何か" if i % 2 == 0 else None,
            "by_category": {},
            "blocks": [],
            "ai": {"sessions": 0, "fragmented": 0, "tool_errors": 0, "interruptions": 0},
        }
        for i in range(6)
    ]
    ev = build_advice_evidence(stats, hist)
    assert "今日の目標: リリースノートの下書き" in ev.markdown
    assert "目標カテゴリ『執筆・ノート』の実測: 12.5分" in ev.markdown
    assert "目標記入:" in ev.markdown
    # 分数は stats 由来
    assert "12.5" in ev.markdown


def test_g2_evidence_without_goal():
    stats = {
        "version": 2,
        "day": "2026-07-29",
        "total_minutes": 200.0,
        "context_switches": 10,
        "ai_activity_blocks": 0,
        "by_category": {"開発": 80.0},
        "by_app": {},
        "by_site": {},
        "blocks": [],
        "ai": {"sessions": 0, "fragmented": 0, "tool_errors": 0, "interruptions": 0},
    }
    ev = build_advice_evidence(stats)
    assert "今日の目標:" not in ev.markdown
    assert "F14" not in ev.markdown


def test_g2_goal_text_is_redacted_in_stats_path():
    """generate 経路で stats に載るのは redact 後（ここでは関数契約の確認）。"""
    day = date(2026, 7, 29)
    summary = DailySummary(
        day=day,
        total_minutes=50.0,
        by_category={"開発": 50.0},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=1,
        by_site={},
    )
    stats = build_stats(
        day,
        summary,
        [],
        goal_text="[REDACTED] の作業",
        goal_category=None,
    )
    assert "SECRET" not in stats.get("goal_text", "")
    assert stats["goal_text"] == "[REDACTED] の作業"


def test_g3_reader_summary_goal_category_sentence():
    stats = {
        "version": 2,
        "day": "2026-07-29",
        "total_minutes": 200.0,
        "context_switches": 10,
        "ai_activity_blocks": 0,
        "by_category": {"執筆・ノート": 12.5, "開発": 80.0},
        "by_app": {},
        "by_site": {},
        "blocks": [],
        "ai": {"sessions": 0, "fragmented": 0, "tool_errors": 0, "interruptions": 0},
        "goal_text": "下書き",
        "goal_category": "執筆・ノート",
    }
    ev = build_advice_evidence(stats)
    assert "目標カテゴリ『執筆・ノート』は12.5分が記録されています。" in ev.reader_summary


def test_g3_today_goal_display(tmp_path: Path, monkeypatch, capsys):
    from kaizenlog.cli import cmd_today, cmd_goal
    from kaizenlog.config import Config

    vault = tmp_path / "vault"
    notes = vault / "01 Daily Notes"
    notes.mkdir(parents=True)
    mem = vault / "Kaizen" / "Memory"
    mem.mkdir(parents=True)
    logs = vault / ".kaizenlog" / "logs"
    logs.mkdir(parents=True)
    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
        stats_dir=".kaizenlog/stats",
    )
    day = date(2026, 7, 29)
    # 未設定
    cmd_today(cfg, day, no_sync=True)
    out = capsys.readouterr().out
    assert "目標: 未設定" in out

    cmd_goal(cfg, day, "下書きを完成 @執筆・ノート")
    cmd_today(cfg, day, no_sync=True)
    out = capsys.readouterr().out
    assert "今日の目標: 下書きを完成" in out
    assert "執筆・ノート" in out


def test_g4_weekly_goal_section_empty_and_filled(tmp_path: Path):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    mem = tmp_path / "mem"
    mem.mkdir()
    exp = tmp_path / "exp"
    exp.mkdir()
    week = date(2026, 7, 27)  # Monday
    md0 = render_weekly_context(stats_dir, mem, exp, week)
    assert "## 目標" in md0
    assert "目標記入なし" in md0

    import json
    day = date(2026, 7, 28)
    (stats_dir / f"{day.isoformat()}.json").write_text(
        json.dumps(
            {
                "version": 2,
                "day": day.isoformat(),
                "total_minutes": 100.0,
                "context_switches": 1,
                "by_category": {"執筆・ノート": 12.5},
                "goal_text": "リリースノート",
                "goal_category": "執筆・ノート",
                "ai": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    md1 = render_weekly_context(stats_dir, mem, exp, week)
    assert "目標設定日数: 7日中1日" in md1
    assert "リリースノート" in md1
    assert "12.5分" in md1


def test_goal_stats_fields_applies_redactor():
    """generate 経路の redact 適用（goal_stats_fields が唯一の入口）。"""
    from kaizenlog.goal import DayGoal, goal_stats_fields

    goal = DayGoal(text="ACME-SECRET の提案書を仕上げる", category="執筆・ノート")
    text, cat = goal_stats_fields(goal, lambda t: t.replace("ACME-SECRET", "[REDACTED]"))
    assert text == "[REDACTED] の提案書を仕上げる"
    assert cat == "執筆・ノート"
    # redactor なし・目標なしの両端
    assert goal_stats_fields(goal, None) == ("ACME-SECRET の提案書を仕上げる", "執筆・ノート")
    assert goal_stats_fields(None, str.upper) == (None, None)
