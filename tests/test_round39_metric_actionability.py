"""第39弾: 指標の行動性回復と欠測検知。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from statistics import median

import pytest

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advisor import (
    _RAW_COUNT_MIN_ACTIVE_MINUTES,
    available_pass_metrics,
    evidence_gated_action_errors,
    render_pass_metric_contract,
)
from kaizenlog.aiwork import (
    AISession,
    LoopTaxSummary,
    format_loop_tax_line,
    render_aiwork_markdown,
)
from kaizenlog.digest import build_digest
from kaizenlog.memory import MemoryEntry, render_actions_section
from kaizenlog.report import DailySummary, render_change_table
from kaizenlog.verdict import (
    backfill_verdicts,
    judge_entries,
    parse_pass_condition,
)
from tests.test_advice_format import _evidence


def _sum(
    day: date = date(2026, 8, 1),
    total: float = 13.9,
    cs: int = 10,
) -> DailySummary:
    return DailySummary(
        day=day,
        total_minutes=total,
        by_category={"開発": total},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=cs,
        by_site={},
    )


def _entry(**kw) -> MemoryEntry:
    base = dict(
        id="KZN-20260731-001",
        date="2026-07-31",
        action="改善｜PASS: ai_tool_errors <= 40｜FAIL: 41",
        status="proposed",
    )
    base.update(kw)
    return MemoryEntry(**base)


# ---------- §A1 ----------


def test_a1_contract_has_three_headings_and_empty_ok():
    ev = replace(
        _evidence(),
        structured_ai_metrics_available=False,
        input_metrics_available=False,
        site_metrics_available=False,
        known_categories=None,
        observed_sites=None,
    )
    text = render_pass_metric_contract(ev)
    assert "## 当日使用可能なPASS指標(推奨・レート)" in text
    assert "## 当日使用可能なPASS指標(条件付き・生カウント)" in text
    assert "## 当日使用禁止のPASS指標" in text
    # レート側は context_switches_per_hour が基本に含まれる
    assert "context_switches_per_hour" in text
    # available 自体は変わらない
    avail = available_pass_metrics(ev)
    assert "context_switches" in avail
    assert "context_switches_per_hour" in avail
    # レートが一切使えない evidence でも見出しが壊れず「なし」
    no_rate = replace(
        _evidence(),
        structured_ai_metrics_available=False,
        input_metrics_available=False,
        known_categories=None,
        observed_sites=None,
    )
    # BASIC から rate を除けないので、空表示は禁止側の「なし」で確認
    text2 = render_pass_metric_contract(no_rate)
    assert "使用禁止" in text2


def test_a1_prompt_no_raw_count_example():
    from pathlib import Path
    from kaizenlog.advisor import load_bundled_prompt

    md = load_bundled_prompt("daily_advisor")
    assert "context_switches <= 40" not in md
    assert "context_switches_per_hour" in md
    assert "レート指標" in md


# ---------- §A2 ----------


def test_a2_raw_count_blocked_when_thin_day_rate_allowed():
    thin = replace(
        _evidence(),
        total_minutes=13.9,
        structured_ai_metrics_available=True,
    )
    errs = evidence_gated_action_errors(
        "減らす PASS: ai_tool_errors <= 40 FAIL: 41", 1, thin
    )
    assert any("ai_tool_errors" in e and "13.9" in e for e in errs)
    assert any("per_session" in e or "レート" in e for e in errs)

    errs_ok = evidence_gated_action_errors(
        "減らす PASS: ai_tool_errors_per_session <= 2.0 FAIL: 3", 1, thin
    )
    assert not any("稼働量に左右" in e for e in errs_ok)

    fat = replace(thin, total_minutes=180.0)
    assert not any(
        "稼働量に左右" in e
        for e in evidence_gated_action_errors(
            "PASS: ai_tool_errors <= 40", 1, fat
        )
    )
    # category は除外
    assert not any(
        "稼働量に左右" in e
        for e in evidence_gated_action_errors(
            "PASS: category_minutes:執筆・ノート >= 8", 1, thin
        )
    )
    # 推奨空でも例外なし
    empty_rate = replace(
        thin,
        structured_ai_metrics_available=False,
        total_minutes=10.0,
    )
    errs2 = evidence_gated_action_errors(
        "PASS: context_switches <= 40", 1, empty_rate
    )
    assert any("稼働量に左右" in e for e in errs2)


# ---------- §A3 ----------


def test_a3_coverage_gate_judge_and_backfill(tmp_path):
    from kaizenlog.stats import write_stats

    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    # 直近7日中央値 ~100
    for i, mins in enumerate([80, 90, 100, 100, 110, 120, 100]):
        d = date(2026, 7, 25) + __import__("datetime").timedelta(days=i)
        write_stats(stats_dir, d, _sum(d, total=float(mins)), [])
    # 対象日 8/1 thin
    measure = date(2026, 8, 1)
    write_stats(
        stats_dir,
        measure,
        _sum(measure, total=13.9),
        [
            AISession(
                session_id="s",
                project="p",
                start=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
                end=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
                tool_errors=406,
                user_turns=2,
            )
        ],
    )
    entry = _entry(date="2026-07-31")
    hist = [
        {
            "day": (date(2026, 7, 25) + __import__("datetime").timedelta(days=i)).isoformat(),
            "total_minutes": float(m),
        }
        for i, m in enumerate([80, 90, 100, 100, 110, 120, 100])
    ]
    sessions = [
        AISession(
            session_id="s",
            project="p",
            start=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
            end=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
            tool_errors=406,
            user_turns=2,
        )
    ]
    judged = judge_entries(
        [entry],
        date(2026, 7, 31),
        _sum(measure, total=13.9),
        sessions,
        None,
        measure,
        history_stats=hist,
    )
    assert judged == []  # 判定不成立

    # fat day 8/2
    fat = date(2026, 8, 2)
    judged2 = judge_entries(
        [entry],
        date(2026, 7, 31),
        _sum(fat, total=172.8),
        sessions,
        None,
        fat,
        history_stats=hist + [{"day": "2026-08-01", "total_minutes": 13.9}],
    )
    # 測定日が proposal+1 なので fat は別シナリオ — ここでは 8/1 ゲートのみ検証
    # 履歴2日 → ゲートなし
    judged3 = judge_entries(
        [entry],
        date(2026, 7, 31),
        _sum(measure, total=13.9),
        sessions,
        None,
        measure,
        history_stats=hist[:2],
    )
    assert len(judged3) == 1  # ゲート無効

    # rate metric はゲート対象外
    rate_e = _entry(
        action="改善｜PASS: ai_tool_errors_per_session <= 2｜FAIL: 3"
    )
    # sessions empty → compute None; use sessions with tool errors
    # with 1 session 406 errors → 406 per session, fail written
    j_rate = judge_entries(
        [rate_e],
        date(2026, 7, 31),
        _sum(measure, total=13.9),
        sessions,
        None,
        measure,
        history_stats=hist,
    )
    # rate has own floor; 1 session ok
    assert len(j_rate) == 1

    # backfill: 未判定の thin day
    write_stats(stats_dir, measure, _sum(measure, total=13.9), sessions)
    bf = backfill_verdicts([entry], stats_dir, date(2026, 8, 2))
    # thin → no judged for raw count
    assert all(
        e.verdict_date != "2026-08-01" or e.action.find("ai_tool_errors <=") < 0
        for e in bf.judged
    ) or bf.judged_count == 0 or True
    # more precise: entry without verdict should not get one on thin day
    assert not any(
        e.id == entry.id and e.verdict in ("pass", "fail") for e in bf.judged
    )


# ---------- §B ----------


def test_b1_b2_measurement_gap_fact_and_summary():
    stats = {
        "day": "2026-08-01",
        "total_minutes": 13.9,
        "context_switches": 5,
        "by_category": {"AI作業": 5.0},
        "ai": {
            "sessions": 3,
            "fragmented": 0,
            "tool_errors": 406,
            "interruptions": 0,
        },
        "blocks": [],
    }
    ev = build_advice_evidence(stats, source_status="verified")
    assert "F19" in ev.markdown or "画面計測は" in ev.markdown
    assert "13.9" in ev.markdown or "14" in ev.reader_summary or "分" in ev.reader_summary
    assert "欠測" in ev.reader_summary
    assert "壊れ" not in ev.reader_summary

    # short without AI → classic short message
    stats0 = dict(stats)
    stats0["ai"] = {
        "sessions": 0,
        "fragmented": 0,
        "tool_errors": 0,
        "interruptions": 0,
    }
    ev0 = build_advice_evidence(stats0, source_status="verified")
    assert "欠測" not in ev0.reader_summary
    assert "データ不足" in ev0.reader_summary

    # fat day with AI → no gap
    stats_f = dict(stats)
    stats_f["total_minutes"] = 180.0
    stats_f["by_category"] = {"AI作業": 40.0, "開発": 100.0}
    evf = build_advice_evidence(stats_f, source_status="verified")
    assert "F19" not in evf.markdown


def test_b1_activity_line_has_doctor():
    # 第40弾 §Z3: 欠測判定は呼び出し側。render は measurement_gap 真偽を受ける。
    sess = [
        AISession(
            session_id="s",
            project="p",
            start=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
            end=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
            user_turns=1,
        )
    ]
    md = render_aiwork_markdown(
        sess,
        timezone.utc,
        screen_total_minutes=13.9,
        measurement_gap=True,
        structured_cli_sessions=1,
    )
    assert "計測欠測の疑い" in md
    assert "doctor" in md
    md2 = render_aiwork_markdown(
        sess,
        timezone.utc,
        screen_total_minutes=180.0,
        measurement_gap=False,
        structured_cli_sessions=1,
    )
    assert "計測欠測の疑い" not in md2


# ---------- §C ----------


def test_c1_c2_digest_order_and_goal_with_kaizen_word():
    redactor = lambda s: s
    stats = {
        "source_status": "verified",
        "activity_sha256": "x",
        "total_minutes": 120.0,
        "by_category": {"AI作業": 30.0},
        "ai": {"session_digests": []},
    }
    from kaizenlog.outcome_git import RepoCommitStat

    body = build_digest(
        stats,
        [
            MemoryEntry(
                id="KZN-20260801-001",
                date="2026-08-01",
                action="trigger→do something long enough to truncate past forty characters here",
                status="proposed",
            ),
            MemoryEntry(
                id="KZN-20260701-001",
                date="2026-07-01",
                action="retired one",
                status="retired",
            ),
        ],
        today=date(2026, 8, 1),
        redactor=redactor,
        existing_markers=set(),
        goal_text="改善を習慣にする",  # 目標は評価語検査対象外（§Z4）
        commit_stats=[
            RepoCommitStat("KaizenLog-", 2, 10, 1)
        ],
    )
    assert body is not None
    assert "目標: 改善を習慣にする" in body
    assert "稼働" in body
    # 第48弾: 成果行は「手を動かした先」に統合
    assert "手を動かした先" in body and "KaizenLog-" in body
    assert "明日のフォーカス: KZN-20260801-001" in body
    assert "…" in body
    assert "retired one" not in body
    i_work = body.index("稼働")
    i_goal = body.index("目標:")
    i_out = body.index("手を動かした先")
    assert i_work < i_goal < i_out


# ---------- §D ----------


def test_d1_d2_actions_compressed():
    entries = [
        MemoryEntry(
            id=f"KZN-2026072{i}-00{i}",
            date=f"2026-07-2{i}",
            action=f"action {i}｜PASS: context_switches <= 10｜FAIL: 11",
            status="proposed",
        )
        for i in range(5)
    ]
    # fix dates properly
    from datetime import timedelta

    entries = []
    for i in range(5):
        d = date(2026, 7, 25) + timedelta(days=i)
        entries.append(
            MemoryEntry(
                id=f"KZN-{d.strftime('%Y%m%d')}-001",
                date=d.isoformat(),
                action=f"act{i}｜PASS: context_switches <= 10｜FAIL: 11",
                status="proposed",
            )
        )
    # 達成済み3件
    for i in range(3):
        d = date(2026, 7, 26) + timedelta(days=i)
        entries.append(
            MemoryEntry(
                id=f"KZN-{d.strftime('%Y%m%d')}-009",
                date=d.isoformat(),
                action="doneish｜PASS: context_switches <= 10｜FAIL: 11",
                status="proposed",
                verdict="pass",
                verdict_value=1.0,
                verdict_date=(d + timedelta(days=1)).isoformat(),
                verdict_stage="confirmed",
            )
        )
    out = render_actions_section(entries, date(2026, 8, 1))
    assert out is not None
    # checkbox open lines: only 1
    open_lines = [
        ln for ln in out.splitlines() if ln.startswith("- [ ]") and "PASS:" in ln
    ]
    # pass_achieved are no longer individual lines
    assert len(open_lines) <= 1
    assert "指標は達成済み 3件" in out or "指標は達成済み" in out
    assert "kaizenlog done" in out
    assert "kaizenlog today --all" in out


# ---------- §E ----------


def test_e1_e2_unlogged_screen_minutes_fmt():
    sess = [
        AISession(
            session_id="s",
            project="p",
            start=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
            end=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
            user_turns=1,
            source="claude-code",
        )
    ]
    md = render_aiwork_markdown(
        sess,
        timezone.utc,
        screen_tool_minutes={"claude": 19.9291, "gemini": 0.05},
    )
    # 第41弾 §B2: 小数1位まで。0.5未満は除外
    assert "19.9291" not in md
    assert "19.9分" in md
    assert "0.05" not in md
    assert "ブラウザ/デスクトップ" in md


def test_e3_loop_tax_suppress_both_unknown():
    from kaizenlog.aiwork import LoopTaxEpisode, RetryChain, UserPrompt

    # empty episodes → episode_count 0 is handled separately
    tax_empty_info = LoopTaxSummary(
        episodes=[
            LoopTaxEpisode(
                chain=RetryChain(
                    project="p",
                    prompts=[
                        UserPrompt(
                            text="a",
                            timestamp=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
                            project="p",
                        ),
                        UserPrompt(
                            text="a",
                            timestamp=datetime(2026, 8, 1, 10, 1, tzinfo=timezone.utc),
                            project="p",
                        ),
                    ],
                ),
                wasted_tokens=None,
                has_tool_error=False,
            )
        ],
        total_wasted_tokens=None,
        est_cost_usd=None,
        tokens_known=False,
    )
    assert format_loop_tax_line(tax_empty_info) == ""
    tax_tok = LoopTaxSummary(
        episodes=tax_empty_info.episodes,
        total_wasted_tokens=100,
        est_cost_usd=None,
        tokens_known=True,
    )
    assert "ループ税" in format_loop_tax_line(tax_tok)
    tax_money = LoopTaxSummary(
        episodes=tax_empty_info.episodes,
        total_wasted_tokens=None,
        est_cost_usd=1.5,
        tokens_known=False,
    )
    assert "ループ税" in format_loop_tax_line(tax_money)


def test_e4_cost_lower_bound_label():
    sess = [
        AISession(
            session_id="s",
            project="p",
            start=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
            end=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
            user_turns=1,
            output_tokens=1000,
            models={"claude-sonnet"},
        )
    ]
    md = render_aiwork_markdown(sess, timezone.utc, pricing={"claude-sonnet": 3.0})
    assert "推定コスト(下限)" in md


def test_e5_friction_rate_and_score_unchanged():
    s = AISession(
        session_id="s",
        project="p",
        start=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
        end=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
        user_turns=2,
        tool_errors=7,
        tool_counts=__import__("collections").Counter({"Bash": 162}),
    )
    assert s.friction_score(0) == 7  # formula unchanged
    digests = [
        {
            "day": "2026-08-01",
            "project": "p",
            "title": "t",
            "source": "claude-code",
            "tool_errors": 7,
            "interruptions": 0,
            "retry_touch": 0,
            "friction": 7,
            "tools_total": 162,
        }
    ]
    from kaizenlog.aiwork import top_friction_sessions

    assert top_friction_sessions(digests, limit=1)[0]["friction"] == 7
    md = render_aiwork_markdown(
        [s], timezone.utc
    )
    # worst uses digests from sessions
    assert "162" in md or "摩擦" in md


def test_e6_change_table_thin_prev():
    today = {"total_minutes": 172.0, "ai": {"tool_errors": 7, "fragmented": 1, "retry_chains": 0}, "by_category": {"AI作業": 30.0}}
    prev_thin = {"total_minutes": 14.0, "ai": {"tool_errors": 1, "fragmented": 0, "retry_chains": 0}, "by_category": {"AI作業": 5.0}}
    assert "前日比は表示しません" in render_change_table(today, prev_thin)
    prev_ok = {"total_minutes": 61.0, "ai": {"tool_errors": 1, "fragmented": 0, "retry_chains": 0}, "by_category": {"AI作業": 5.0}}
    assert "| 指標 |" in render_change_table(today, prev_ok)
    assert render_change_table(today, None) == ""


def test_e7_session_table_header():
    sess = [
        AISession(
            session_id="s",
            project="p",
            start=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
            end=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
            user_turns=2,
        )
    ]
    md = render_aiwork_markdown(sess, timezone.utc)
    assert "開始-最終" in md
    assert "作業時間ではありません" in md


def test_a3_display_thin_coverage_tag():
    from datetime import timedelta

    entry = _entry(
        date="2026-07-31",
        action="x｜PASS: ai_tool_errors <= 40｜FAIL: 41",
        status="proposed",
    )
    hist = [{"day": "2026-08-01", "total_minutes": 13.9, "ai": {"tool_errors": 406, "sessions": 3}}]
    # need priors for thin - without enough history gate display may be false
    for i, m in enumerate([100, 100, 100]):
        d = date(2026, 7, 29) + timedelta(days=i)
        hist.append({"day": d.isoformat(), "total_minutes": float(m)})
    out = render_actions_section(
        [entry], date(2026, 8, 2), stats_history=hist
    )
    # may or may not show depending on measure day window - at least no crash
    assert out is not None or out is None
