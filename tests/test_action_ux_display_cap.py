"""ACTION-UX-DISPLAY-CAP-1: display_cap と still_open 表示順。"""
from __future__ import annotations

from datetime import date, timedelta

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.memory import (
    ActionStats,
    MemoryEntry,
    backlog_generation_cap,
    format_today_action_line,
    order_still_open_for_display,
    render_actions_section,
    resolve_display_cap,
    summarize_for_prompt,
)
from tests.test_advice_evidence import CURRENT, HISTORY


def test_resolve_display_cap_default_one():
    # done>0 and healthy rate → base 1 without max_candidates
    s = ActionStats(
        window_days=14,
        proposed=10,
        done=8,
        judged=5,
        passed=4,
        done_judged=4,
        done_passed=3,
        undone_judged=1,
        undone_passed=1,
        skipped=0,
    )
    # done_rate = 8/10 = 0.8 if property exists
    assert resolve_display_cap(s) == 1
    assert resolve_display_cap(s, max_candidates=2) == 2
    assert resolve_display_cap(s, max_candidates=9) == 3


def test_resolve_display_cap_force_one_when_done_zero():
    s = ActionStats(
        window_days=14,
        proposed=5,
        done=0,
        judged=0,
        passed=0,
        done_judged=0,
        done_passed=0,
        undone_judged=0,
        undone_passed=0,
        skipped=0,
    )
    assert resolve_display_cap(s) == 1
    # max_candidates を大きくしても低消化なら強制1
    assert resolve_display_cap(s, max_candidates=3) == 1


def test_resolve_display_cap_force_one_low_done_rate():
    s = ActionStats(
        window_days=14,
        proposed=5,
        done=1,
        judged=2,
        passed=1,
        done_judged=1,
        done_passed=0,
        undone_judged=1,
        undone_passed=1,
        skipped=0,
    )
    # done_rate = 1/5 = 0.2 < 0.4 and proposed >= 3
    assert s.done_rate is not None and s.done_rate < 0.4
    assert resolve_display_cap(s, max_candidates=3) == 1


def test_order_still_open_provisional_last():
    entries = [
        MemoryEntry(
            id="KZN-20260731-003",
            date="2026-07-31",
            action="prov｜PASS: context_switches <= 10｜FAIL: 11",
            status="proposed",
            verdict_stage="provisional",
        ),
        MemoryEntry(
            id="KZN-20260731-002",
            date="2026-07-31",
            action="ok｜PASS: context_switches <= 10｜FAIL: 11",
            status="proposed",
            verdict_stage="confirmed",
        ),
        MemoryEntry(
            id="KZN-20260731-001",
            date="2026-07-30",
            action="fail｜PASS: context_switches <= 10｜FAIL: 11",
            status="proposed",
            verdict="fail",
            verdict_stage="confirmed",
            verdict_value=20.0,
            verdict_date="2026-07-31",
        ),
    ]
    ordered = order_still_open_for_display(entries)
    assert [e.id for e in ordered] == [
        "KZN-20260731-002",
        "KZN-20260731-001",
        "KZN-20260731-003",
    ]


def test_render_actions_prefers_executable_over_provisional():
    """display_cap=1 のとき provisional より実行可能案をフォーカスに出す。"""
    target = date(2026, 8, 2)
    # recent window for 📌: target-7 .. target-1 → 7/26..8/1
    entries = [
        MemoryEntry(
            id="KZN-20260801-001",
            date="2026-08-01",
            action="朝の最初のセッション前→目標を1行書く｜PASS: category_minutes:執筆・ノート >= 7｜FAIL: 7未満",
            status="proposed",
            verdict_stage="provisional",
        ),
        MemoryEntry(
            id="KZN-20260731-002",
            date="2026-07-31",
            action="タイマー開始前→30分だけ作業する｜PASS: focus_minutes >= 30｜FAIL: 30未満",
            status="proposed",
            verdict_stage="confirmed",
        ),
        MemoryEntry(
            id="KZN-20260730-003",
            date="2026-07-30",
            action="古い案｜PASS: context_switches <= 10｜FAIL: 11",
            status="proposed",
            verdict_stage="confirmed",
        ),
        MemoryEntry(
            id="KZN-20260729-004",
            date="2026-07-29",
            action="さらに古い｜PASS: context_switches <= 10｜FAIL: 11",
            status="proposed",
            verdict_stage="confirmed",
        ),
        MemoryEntry(
            id="KZN-20260728-005",
            date="2026-07-28",
            action="五件目｜PASS: context_switches <= 10｜FAIL: 11",
            status="proposed",
            verdict_stage="confirmed",
        ),
    ]
    out = render_actions_section(entries, target)
    assert out is not None
    open_boxes = [ln for ln in out.splitlines() if ln.startswith("- [ ]")]
    assert len(open_boxes) == 1, open_boxes
    # provisional の 20260801 ではなく実行可能側が選ばれる
    assert "KZN-20260731-002" in open_boxes[0]
    assert "KZN-20260801-001" not in open_boxes[0]
    assert "完了条件: 今日の予定分を実施して `kaizenlog done KZN-20260731-002`" in out


def test_render_actions_single_checkbox_when_backlog():
    target = date(2026, 8, 2)
    entries = []
    for i in range(5):
        d = date(2026, 7, 27) + timedelta(days=i)
        entries.append(
            MemoryEntry(
                id=f"KZN-{d.strftime('%Y%m%d')}-00{i+1}",
                date=d.isoformat(),
                action=f"案{i}｜PASS: context_switches <= 10｜FAIL: 11",
                status="proposed",
            )
        )
    out = render_actions_section(entries, target)
    assert out is not None
    open_boxes = [ln for ln in out.splitlines() if ln.startswith("- [ ]")]
    assert len(open_boxes) == 1
    assert "今週の提案は" in out or "提案" in out


def test_backlog_generation_cap_and_evidence_max_actions():
    zero_done = ActionStats(
        window_days=14,
        proposed=5,
        done=0,
        judged=0,
        passed=0,
        done_judged=0,
        done_passed=0,
        undone_judged=0,
        undone_passed=0,
        skipped=0,
    )
    assert backlog_generation_cap(zero_done) == 1
    # dosing は proposed≥6 未満でも backlog で1
    ev = build_advice_evidence(
        {**CURRENT, "total_minutes": 400.0},
        HISTORY,
        action_stats=zero_done,
    )
    assert ev.max_actions == 1

    healthy = ActionStats(
        window_days=14,
        proposed=10,
        done=8,
        judged=5,
        passed=4,
        done_judged=4,
        done_passed=3,
        undone_judged=1,
        undone_passed=1,
        skipped=0,
    )
    assert backlog_generation_cap(healthy) == 3


def test_summarize_prompt_mentions_backlog_cap():
    today = date(2026, 8, 2)
    entries = [
        MemoryEntry(
            id=f"KZN-2026072{i}-001",
            date=f"2026-07-2{i}",
            action="x｜PASS: context_switches <= 10｜FAIL: 11",
            status="proposed",
        )
        for i in range(5, 9)
    ]
    text = summarize_for_prompt(entries, today)
    assert "未チェックの提案が溜まっている" in text or "件数を1件に制限" in text
    assert "どう確認するか" in text or "1件" in text


def test_format_today_action_line_hides_pass_machine_syntax():
    e = MemoryEntry(
        id="KZN-20260801-001",
        date="2026-08-01",
        action="朝のセッション前→目標を1行書く｜PASS: category_minutes:執筆・ノート >= 7｜FAIL: 7未満",
        status="proposed",
    )
    line = format_today_action_line(e)
    assert "｜PASS:" not in line
    assert "目標を1行書く" in line
    raw = format_today_action_line(e, reader_friendly=False)
    assert "｜PASS:" in raw
