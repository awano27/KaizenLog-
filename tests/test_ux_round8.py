"""第8弾 UX: actions top/stats、morning、catch-up、可読レンダ。"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advice_format import _assert_render_shape, render_advice_markdown, validate_advice
from kaizenlog.config import Config, ConfigError, load_config
from kaizenlog.memory import (
    MemoryEntry,
    append_entries,
    assign_action_ids,
    load_entries,
    render_actions_section,
)
from kaizenlog.vault import (
    ACTIONS_MARKER,
    ACTIVITY_MARKER,
    ADVICE_MARKER,
    DailyNoteStore,
    extract_section,
    upsert_section,
)
from kaizenlog.verdict import (
    apply_verdicts_to_advice_note,
    judge_entries,
    parse_pass_condition,
)
from tests.test_advice_evidence import CURRENT, HISTORY
from tests.test_advice_format import _valid_data


# ---- U2 ----

def test_upsert_section_top_after_frontmatter():
    from kaizenlog.vault import upsert_section

    content = "---\ndate: 2026-07-26\n---\n\n# Note\nhandwritten\n"
    out = upsert_section(content, ACTIONS_MARKER, "## 📌 今日のアクション\n- [ ] a", position="top")
    # frontmatter の直後に actions
    assert out.index("kaizenlog:actions:start") < out.index("# Note")
    assert out.index("---\n\n") < out.index("kaizenlog:actions:start") or "---" in out[:40]


def test_upsert_section_top_without_frontmatter():
    from kaizenlog.vault import upsert_section

    content = "# Note\nbody\n"
    out = upsert_section(content, ACTIONS_MARKER, "SEC", position="top")
    assert out.index("kaizenlog:actions:start") < out.index("# Note")


def test_upsert_existing_keeps_position():
    from kaizenlog.vault import upsert_section

    content = "# Top\n\n" + upsert_section("", ACTIONS_MARKER, "OLD", position="bottom") + "\n# Bottom\n"
    # force actions in the middle by construction
    mid = "# Top\n\n<!-- kaizenlog:actions:start -->\nOLD\n<!-- kaizenlog:actions:end -->\n\n# Bottom\n"
    out = upsert_section(mid, ACTIONS_MARKER, "NEW", position="top")
    assert out.index("NEW") > out.index("# Top")
    assert out.index("NEW") < out.index("# Bottom")
    assert out.count("kaizenlog:actions:start") == 1


def test_actions_position_config(tmp_path):
    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text(
        f'[general]\nvault_dir = "{tmp_path.as_posix()}"\nactions_position = "bottom"\n',
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))
    assert cfg.actions_position == "bottom"

    bad = tmp_path / "bad.toml"
    bad.write_text(
        f'[general]\nvault_dir = "{tmp_path.as_posix()}"\nactions_position = "side"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="actions_position"):
        load_config(str(bad))


# ---- U5 ----

def test_render_actions_includes_stats_line():
    today = date(2026, 7, 26)
    entries = [
        MemoryEntry(
            id="KZN-20260725-001", date="2026-07-25", action="a", status="proposed"
        ),
        MemoryEntry(
            id="KZN-20260720-001",
            date="2026-07-20",
            action="b",
            status="done",
            done_date="2026-07-21",
            verdict="pass",
            verdict_value=1.0,
            verdict_date="2026-07-21",
        ),
    ]
    md = render_actions_section(entries, today)
    assert md is not None
    assert "消化率" in md
    assert "実行済みPASS" in md or "PASS" in md

    # proposed 0 の窓内アクションのみ → セクション None（アクション0）
    assert render_actions_section([], today) is None


# ---- U1 / U4 ----

def test_build_morning_notification():
    from kaizenlog.cli import build_morning_notification

    today = date(2026, 7, 26)
    entries = [
        MemoryEntry(id="KZN-20260725-001", date="2026-07-25", action="secret", status="proposed"),
        MemoryEntry(
            id="KZN-20260724-001",
            date="2026-07-24",
            action="x",
            status="proposed",
            verdict="pass",
            verdict_value=1.0,
            verdict_date="2026-07-25",
        ),
        MemoryEntry(
            id="KZN-20260724-002",
            date="2026-07-24",
            action="y",
            status="proposed",
            verdict="fail",
            verdict_value=9.0,
            verdict_date="2026-07-25",
        ),
    ]
    msg = build_morning_notification(entries, today)
    assert msg is not None
    assert "今日の候補" in msg
    assert "保留" in msg
    assert "昨日の判定" in msg
    # 未実行 PASS/FAIL は層別: 未実行達成1件
    assert "未実行での達成 1件" in msg or "✅" in msg
    assert "secret" not in msg
    assert build_morning_notification([], today) is None


def test_cmd_morning_rewrites_actions_and_notifies(tmp_path, monkeypatch):
    from kaizenlog import cli as cli_mod

    vault = tmp_path / "vault"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    mem = vault / "Kaizen" / "Memory"
    mem.mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / ".kaizenlog" / "logs").mkdir(parents=True)

    today = date(2026, 7, 26)
    append_entries(
        mem,
        [
            MemoryEntry(
                id="KZN-20260725-001",
                date="2026-07-25",
                action="do it",
                status="proposed",
            )
        ],
    )
    # existing note with checked box
    note = upsert_section(
        f"---\ndate: {today.isoformat()}\n---\n\n# Daily\n",
        ACTIONS_MARKER,
        "## 📌 今日のアクション\n- [x] KZN-20260725-001: do it（7/25提案）\n",
        position="top",
    )
    (daily / f"{today.isoformat()}.md").write_text(note, encoding="utf-8")

    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        memory_dir="Kaizen/Memory",
        stats_dir=".kaizenlog/stats",
        logs_dir=".kaizenlog/logs",
        actions_position="top",
    )
    notified = []
    monkeypatch.setattr(
        cli_mod, "catch_up_yesterday", lambda *a, **k: cli_mod.CatchUpResult()
    )
    monkeypatch.setattr(
        cli_mod, "notify", lambda title, msg, **kw: notified.append((title, msg)) or True
    )

    assert cli_mod.cmd_morning(cfg, today) == 0
    text = (daily / f"{today.isoformat()}.md").read_text(encoding="utf-8")
    assert "- [x] KZN-20260725-001:" in text
    assert text.index("kaizenlog:actions") < text.index("# Daily")
    assert notified and "今日の候補" in notified[0][1]


def test_catch_up_yesterday_calls_generate_and_advise(tmp_path, monkeypatch):
    from kaizenlog import cli as cli_mod

    vault = tmp_path / "vault"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / ".kaizenlog" / "logs").mkdir(parents=True)
    today = date(2026, 7, 26)
    yday = today - __import__("datetime").timedelta(days=1)
    # ACTIVITY only
    content = upsert_section(
        f"---\ndate: {yday.isoformat()}\n---\n",
        ACTIVITY_MARKER,
        "### カテゴリ別\n|a|b|\n",
    )
    (daily / f"{yday.isoformat()}.md").write_text(content, encoding="utf-8")

    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        stats_dir=".kaizenlog/stats",
        logs_dir=".kaizenlog/logs",
        memory_dir="Kaizen/Memory",
    )
    calls = []
    monkeypatch.setattr(
        cli_mod, "cmd_generate", lambda c, d: calls.append(("generate", d))
    )
    monkeypatch.setattr(
        cli_mod, "cmd_advise", lambda c, d, dry_run=False: calls.append(("advise", d))
    )
    monkeypatch.setattr(cli_mod, "log_run", lambda *a, **k: None)
    # force missing stats for yesterday
    monkeypatch.setattr(cli_mod, "missing_days", lambda *a, **k: [yday])

    cli_mod.catch_up_yesterday(cfg, today)
    assert ("generate", yday) in calls
    assert ("advise", yday) in calls

    # ADVICE already present → no second advise
    content2 = upsert_section(content, ADVICE_MARKER, "### 今日の改善提案\n1. x\n")
    (daily / f"{yday.isoformat()}.md").write_text(content2, encoding="utf-8")
    calls.clear()
    monkeypatch.setattr(cli_mod, "missing_days", lambda *a, **k: [])  # stats ok
    cli_mod.catch_up_yesterday(cfg, today)
    assert calls == []


def test_catch_up_swallows_advise_errors(tmp_path, monkeypatch):
    from kaizenlog import cli as cli_mod

    vault = tmp_path / "vault"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / ".kaizenlog" / "logs").mkdir(parents=True)
    today = date(2026, 7, 26)
    yday = today - __import__("datetime").timedelta(days=1)
    content = upsert_section(
        f"---\ndate: {yday.isoformat()}\n---\n",
        ACTIVITY_MARKER,
        "act",
    )
    (daily / f"{yday.isoformat()}.md").write_text(content, encoding="utf-8")
    # create empty stats for yesterday so generate not needed
    (vault / ".kaizenlog" / "stats" / f"{yday.isoformat()}.json").write_text(
        "{}", encoding="utf-8"
    )
    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        stats_dir=".kaizenlog/stats",
        logs_dir=".kaizenlog/logs",
        memory_dir="Kaizen/Memory",
    )
    monkeypatch.setattr(
        cli_mod, "cmd_advise", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("llm down"))
    )
    monkeypatch.setattr(cli_mod, "log_run", lambda *a, **k: None)
    # should not raise
    cli_mod.catch_up_yesterday(cfg, today)


# ---- U3 ----

def test_readable_render_and_roundtrip():
    data = _valid_data()
    evidence = build_advice_evidence(CURRENT, HISTORY)
    md = render_advice_markdown(data, evidence)
    assert "[F3]" not in md and "[F5]" not in md
    assert "コンテキストスイッチ回数" in md or "focus_blocks" in md
    # 形状検査（旧 Markdown 契約の形状相当）
    _assert_render_shape(md, n_actions=len(data["actions"]))

    full = f"## 🚀 Kaizen（AIからの改善提案）\n\n{md}"
    with_ids, entries = assign_action_ids(full, date(2026, 7, 21), [])
    assert entries
    # note-annotated PASS still parses
    line = [ln for ln in with_ids.splitlines() if "KZN-" in ln and "PASS:" in ln][0]
    parsed = parse_pass_condition(line)
    assert parsed is not None

    # freeform + annotated machine
    assert parse_pass_condition(
        "x｜PASS: context_switches <= 40（コンテキストスイッチ回数）｜FAIL: 41"
    ) == ("context_switches", "<=", 40.0)
    assert parse_pass_condition("x｜PASS: 集中ブロック2回以上｜FAIL: 0") is None


def test_legacy_line_verdict_writeback_still_works():
    entry = MemoryEntry(
        id="KZN-20260724-001",
        date="2026-07-24",
        action="[F3] 旧形式｜PASS: context_switches <= 40｜FAIL: x",
        verdict="pass",
        verdict_value=12.0,
        verdict_date="2026-07-25",
    )
    content = upsert_section(
        "---\ndate: x\n---\n",
        ADVICE_MARKER,
        "- [ ] KZN-20260724-001: [F3] 旧形式｜PASS: context_switches <= 40｜FAIL: x\n",
    )
    updated = apply_verdicts_to_advice_note(content, [entry])
    assert updated and "｜判定:" in updated and "実測12" in updated


def test_category_minutes_label_has_no_nested_parens():
    """注記自体が（…）で囲まれるため、ラベル内に括弧を入れない。"""
    from kaizenlog.experiments import metric_display_label

    cat = metric_display_label("category_minutes:エンタメ")
    site = metric_display_label("site_minutes:youtube.com")
    assert cat is not None and "（" not in cat and "(" not in cat
    assert site is not None and "（" not in site and "(" not in site
    assert "分" in cat and "エンタメ" in cat


def test_strip_pass_annotation_handles_nested_parens():
    """既にノートに残ったネスト注記も strip で機械構文に戻す。"""
    from kaizenlog.verdict import strip_pass_annotation

    nested = "category_minutes:エンタメ <= 30（エンタメの時間（分））"
    assert strip_pass_annotation(nested) == "category_minutes:エンタメ <= 30"
    half = "context_switches <= 40(ctx (count))"
    assert strip_pass_annotation(half) == "context_switches <= 40"
    plain = "focus_blocks >= 2（集中ブロック数）"
    assert strip_pass_annotation(plain) == "focus_blocks >= 2"


def test_category_minutes_annotated_pass_roundtrip():
    """render 注記 → Memory 保存文 → parse → 機械判定可能な metric。"""
    from kaizenlog.experiments import metric_display_label
    from kaizenlog.verdict import strip_pass_annotation

    metric = "category_minutes:エンタメ"
    label = metric_display_label(metric)
    assert label
    # レンダラと同形: PASS 値 + （ラベル）
    annotated = f"{metric} <= 30（{label}）"
    assert "（" not in label  # ネストしない
    assert strip_pass_annotation(annotated) == f"{metric} <= 30"
    line = f"- [ ] 娯楽を減らす｜PASS: {annotated}｜FAIL: 31分以上"
    assert parse_pass_condition(line) == (metric, "<=", 30.0)

    # 過去に書き込まれたネスト注記の互換
    legacy = (
        "- [ ] KZN-20260725-001: 娯楽を減らす"
        "｜PASS: category_minutes:エンタメ <= 30（エンタメの時間（分））"
        "｜FAIL: 31分以上"
    )
    assert parse_pass_condition(legacy) == ("category_minutes:エンタメ", "<=", 30.0)
