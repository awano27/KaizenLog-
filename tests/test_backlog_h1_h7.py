"""残バックログ H1–H7 の回帰テスト。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from kaizenlog.aiwork import (
    AISession,
    UserPrompt,
    estimate_sessions_cost,
    render_aiwork_markdown,
    resolve_output_price,
)
from kaizenlog.advisor import _semantic_contract_errors
from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.config import Config
from kaizenlog.experiments import Experiment, load_experiments, record_measurement
from kaizenlog.memory import MemoryEntry, append_entries, load_entries
from kaizenlog.notify import notify
from kaizenlog.promptmine import count_cluster_matches, normalize, render_prompt_report
from kaizenlog.report import DailySummary
from kaizenlog.stats import build_stats, write_stats
from kaizenlog.weekly_context import monday_of, parse_iso_week, render_weekly_context
from tests.test_advice_evidence import CURRENT, HISTORY


UTC = timezone.utc


def _cfg(vault: Path) -> Config:
    return Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        experiments_dir="03 Areas/Kaizen Experiments",
        stats_dir=".kaizenlog/stats",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
    )


def _summary(day: date = date(2026, 7, 20)) -> DailySummary:
    return DailySummary(
        day=day,
        total_minutes=100.0,
        by_category={"開発": 60.0},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=10,
        by_site={},
    )


# ---- H1 ----

def test_count_cluster_matches_similarity_boundary():
    rep = normalize("please fix the unit test for vault")
    prompts = [
        UserPrompt(
            timestamp=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            project="p",
            text="Please fix the unit test for vault module",
        ),
        UserPrompt(
            timestamp=datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
            project="p",
            text="totally unrelated cooking recipe",
        ),
    ]
    n = count_cluster_matches(prompts, rep, similarity=0.6)
    assert n == 1
    assert count_cluster_matches(prompts, "", similarity=0.6) == 0


def test_prompt_cluster_measurement_and_skip(tmp_path):
    vault = tmp_path / "v"
    exp_dir = vault / "03 Areas" / "Kaizen Experiments"
    exp_dir.mkdir(parents=True)
    rep = normalize("summarize the daily news")
    exp_path = exp_dir / "EXP cluster.md"
    exp_path.write_text(
        f"""---
title: スキル化効果
status: running
metric: prompt_cluster:news
target: "<= 0"
deadline: 2026-08-01
cluster_rep: "{rep}"
---
""",
        encoding="utf-8",
    )
    exps = load_experiments(exp_dir)
    assert len(exps) == 1 and exps[0].cluster_rep == rep

    prompts = [
        UserPrompt(
            timestamp=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
            project="news",
            text="Summarize the daily news please",
        ),
        UserPrompt(
            timestamp=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            project="news",
            text="summarize the daily news",
        ),
    ]
    n = count_cluster_matches(prompts, rep)
    assert n >= 1
    record_measurement(exps[0], date(2026, 7, 20), float(n))
    reloaded = load_experiments(exp_dir)[0]
    assert reloaded.measurements[date(2026, 7, 20)] == float(n)

    # cluster_rep 欠落 → generate ループはスキップ（単位テストで None 相当）
    bare = exp_dir / "EXP bare.md"
    bare.write_text(
        """---
title: bare
status: running
metric: prompt_cluster:x
target: "<= 0"
---
""",
        encoding="utf-8",
    )
    bare_exp = [e for e in load_experiments(exp_dir) if e.title == "bare"][0]
    assert bare_exp.cluster_rep is None


def test_prompts_report_tracking_annotation():
    from kaizenlog.promptmine import cluster_prompts

    prompts = [
        UserPrompt(
            timestamp=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            project="p",
            text="fix the failing unit test again",
        ),
        UserPrompt(
            timestamp=datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
            project="p",
            text="fix the failing unit test please",
        ),
        UserPrompt(
            timestamp=datetime(2026, 7, 22, 10, 0, tzinfo=UTC),
            project="p",
            text="fix the failing unit test now",
        ),
    ]
    clusters = cluster_prompts(prompts)
    assert clusters
    rep = clusters[0].representative
    report = render_prompt_report(
        prompts, days=7, min_count=3, tracking=[(rep, "ユニットテスト依頼をスキル化")]
    )
    assert "📉 追跡中: ユニットテスト依頼をスキル化" in report


# ---- H2 ----

def test_pricing_match_and_override():
    assert resolve_output_price("claude-sonnet-4-20250514") == 3.0
    assert resolve_output_price("mystery-model-xyz") is None
    assert resolve_output_price("claude-sonnet-4", {"claude-sonnet": 9.9}) == 9.9


def test_cost_uncosted_and_markdown_stats_f5():
    s_known = AISession(
        session_id="1",
        project="p",
        start=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
        end=datetime(2026, 7, 20, 10, 30, tzinfo=UTC),
        user_turns=2,
        output_tokens=1_000_000,
        models={"claude-sonnet-4"},
        source="claude-code",
    )
    s_unknown = AISession(
        session_id="2",
        project="p",
        start=datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
        end=datetime(2026, 7, 20, 11, 10, tzinfo=UTC),
        user_turns=1,
        output_tokens=5000,
        models={"unknown-local"},
        source="codex",
    )
    cost, uncosted, by_src = estimate_sessions_cost([s_known, s_unknown])
    assert cost == 3.0  # 1M * $3 / M
    assert uncosted == 5000
    assert "claude-code" in by_src

    md = render_aiwork_markdown(
        [s_known, s_unknown], ZoneInfo("UTC"), retry_chain_count=0
    )
    assert "推定コスト(下限): $3.00" in md
    assert "対象外 5,000 tok" in md

    stats = build_stats(date(2026, 7, 20), _summary(), [s_known, s_unknown])
    assert stats["ai"]["est_cost_usd"] == 3.0
    assert stats["ai"]["uncosted_tokens"] == 5000

    stats["ai"]["sessions"] = 2
    stats["ai"]["fragmented"] = 0
    stats["ai"]["tool_errors"] = 0
    stats["ai"]["interruptions"] = 0
    ev = build_advice_evidence(stats, HISTORY)
    assert "推定コスト $3.00" in ev.markdown
    assert "output のみ" in ev.markdown

    # 旧形式 stats
    legacy = dict(CURRENT)
    legacy["ai"] = {
        "sessions": 1,
        "fragmented": 0,
        "tool_errors": 0,
        "interruptions": 0,
    }
    ev2 = build_advice_evidence(legacy, HISTORY)
    assert "推定コスト" not in ev2.markdown or "F5" in ev2.markdown


# ---- H3 ----

def test_weekly_context_week_boundary_and_missing_and_superseded(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    stats = vault / "stats"
    mem = vault / "mem"
    exp = vault / "exp"
    stats.mkdir()
    mem.mkdir()
    exp.mkdir()
    # 2026-07-20 は月曜
    assert monday_of(date(2026, 7, 22)) == date(2026, 7, 20)
    assert parse_iso_week("2026-W30") == date(2026, 7, 20)

    write_stats(stats, date(2026, 7, 20), _summary(date(2026, 7, 20)), [])
    # 火〜日は欠損
    append_entries(
        mem,
        [
            MemoryEntry(
                id="KZN-20260720-001",
                date="2026-07-20",
                action="a",
                status="proposed",
            ),
            MemoryEntry(
                id="KZN-20260720-002",
                date="2026-07-20",
                action="b",
                status="superseded",
            ),
            MemoryEntry(
                id="KZN-20260720-003",
                date="2026-07-20",
                action="c",
                status="done",
                done_date="2026-07-21",
                verdict="pass",
                verdict_value=1.0,
                verdict_date="2026-07-21",
            ),
        ],
    )
    exp_path = exp / "e.md"
    exp_path.write_text(
        """---
title: 期限切れ実験
status: expired
metric: context_switches
target: "<= 40"
---
<!-- kaizenlog:measurements:start -->
## Measurements
| 日付 | 値 | 目標達成 |
| --- | ---: | :-: |
| 2026-07-18 | 10 | ✅ |
| 2026-07-19 | 50 | ❌ |
<!-- kaizenlog:measurements:end -->
""",
        encoding="utf-8",
    )
    out = render_weekly_context(stats, mem, exp, date(2026, 7, 20))
    assert "2026-W30" in out or "2026-07-20" in out
    assert "記録なし" in out
    assert "superseded" not in out.lower() or "KZN-20260720-002" not in out
    assert "達成率 1/2" in out
    # weekly-kaizen が期待する見出し
    assert "## 日別カテゴリと合計" in out
    assert "## AIテレメトリ週次推移" in out
    assert "## アクション実績" in out
    assert "## 実験サマリー" in out


# ---- H4 ----

def test_semantic_guard_japanese_canaries():
    from kaizenlog.advisor import AdviceEvidence

    def _ev(**kw):
        base = dict(
            markdown="x",
            fact_ids=frozenset(),
            ai_conversation_metrics_available=False,
            entertainment_observed=False,
            reader_summary="s",
            reader_notes=("n",),
            max_actions=1,
            previous_day_available=True,
            browser_sample_sufficient=True,
        )
        base.update(kw)
        return AdviceEvidence(**base)

    # 娯楽断定
    errs = _semantic_contract_errors(
        "今日はエンタメ利用が多く浪費が発生した。",
        _ev(entertainment_observed=False),
    )
    assert any("娯楽" in e or "私用" in e for e in errs)

    # AI 品質断定（テレメトリ無し）
    errs2 = _semantic_contract_errors(
        "AI会話が細切れで往復が多い。",
        _ev(ai_conversation_metrics_available=False),
    )
    assert any("AI会話" in e for e in errs2)

    # 通知帰属（F1）
    errs4 = _semantic_contract_errors(
        "[F1] 通知が多すぎて集中力が低下した。",
        _ev(fact_ids=frozenset({"[F1]"}), markdown="[F1]"),
    )
    assert any("通知" in e or "割り込み" in e for e in errs4)


# ---- H5 ----

def test_notify_truncates_before_escape(monkeypatch):
    monkeypatch.setattr("kaizenlog.notify.sys.platform", "win32")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("kaizenlog.notify.subprocess.run", fake_run)
    # 境界付近に ' を置く
    msg = "x" * 198 + "'" + "y" * 10
    assert notify("t't", msg) is True
    script = captured["cmd"][-1]
    # エスケープ後も閉じクォートが壊れていない（奇数個の連続 ' で終わらない）
    assert "ShowBalloonTip" in script


def test_notify_returncode_nonzero(monkeypatch):
    monkeypatch.setattr("kaizenlog.notify.sys.platform", "win32")

    class R:
        returncode = 1

    monkeypatch.setattr(
        "kaizenlog.notify.subprocess.run", lambda *a, **k: R()
    )
    assert notify("t", "m") is False


# ---- H7 ----

def test_skip_verdict_ids_for_retro_advise_same_run(tmp_path, monkeypatch):
    import kaizenlog.cli as cli_mod
    from kaizenlog.verdict import judge_entries

    day = date(2026, 7, 21)
    entries = [
        MemoryEntry(
            id="KZN-20260720-001",
            date="2026-07-20",
            action="x｜PASS: context_switches <= 40｜FAIL: 41",
            status="proposed",
        ),
        MemoryEntry(
            id="KZN-20260720-NEW",
            date="2026-07-20",
            action="y｜PASS: context_switches <= 40｜FAIL: 41",
            status="proposed",
        ),
    ]
    summary = _summary(day)
    judged = judge_entries(
        entries, date(2026, 7, 20), summary, [], None, day
    )
    assert {j.id for j in judged} == {"KZN-20260720-001", "KZN-20260720-NEW"}
    skip = {"KZN-20260720-NEW"}
    filtered = [e for e in judged if e.id not in skip]
    assert {j.id for j in filtered} == {"KZN-20260720-001"}
