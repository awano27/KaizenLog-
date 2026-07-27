"""消化率 / 実行済みPASS率の集計と適応投与・status 表示。"""
from __future__ import annotations

from datetime import date

from kaizenlog.memory import (
    ActionStats,
    MemoryEntry,
    compute_action_stats,
    dosing_max_actions,
    render_action_stats_line,
    summarize_for_prompt,
)

TODAY = date(2026, 7, 25)


def _e(
    day: str,
    *,
    status: str = "proposed",
    verdict: str | None = None,
    eid: str | None = None,
    action: str = "x",
) -> MemoryEntry:
    return MemoryEntry(
        id=eid or f"KZN-{day.replace('-', '')}-001",
        date=day,
        action=action,
        status=status,
        verdict=verdict,
        verdict_date=day if verdict else None,
        verdict_value=1.0 if verdict else None,
    )


def test_compute_action_stats_window_and_counts():
    entries = [
        _e("2026-07-11"),  # today-14 → 含む
        _e("2026-07-24", status="done", verdict="pass"),  # today-1 → 含む
        _e("2026-07-25"),  # today → 除外
        _e("2026-07-10"),  # today-15 → 除外
        _e("not-a-date"),  # 不正 → 無視
        _e("2026-07-20", status="proposed", verdict="fail", eid="KZN-20260720-002"),
        _e("2026-07-21", status="done", verdict="pass", eid="KZN-20260721-001"),
    ]
    s = compute_action_stats(entries, TODAY, window_days=14)
    # 7/11, 7/24, 7/20, 7/21 = 4
    assert s.proposed == 4
    assert s.done == 2  # 7/24, 7/21
    assert s.judged == 3  # pass, fail, pass
    assert s.passed == 2
    assert s.done_rate == 0.5
    # 実行済みPASS率: done のうち judged 2件 (7/24 pass, 7/21 pass) → 2/2 = 1.0
    assert s.done_judged == 2
    assert s.done_passed == 2
    assert s.undone_judged == 1  # 7/20 fail
    assert s.undone_passed == 0
    assert s.pass_rate == 1.0


def test_compute_action_stats_stratification_mixed():
    entries = [
        _e("2026-07-20", status="done", verdict="pass", eid="KZN-20260720-001"),
        _e("2026-07-21", status="done", verdict="fail", eid="KZN-20260721-001"),
        _e("2026-07-22", status="proposed", verdict="pass", eid="KZN-20260722-001"),
        _e("2026-07-23", status="proposed", verdict="pass", eid="KZN-20260723-001"),
    ]
    s = compute_action_stats(entries, TODAY)
    assert s.done_judged == 2 and s.done_passed == 1
    assert s.undone_judged == 2 and s.undone_passed == 2
    assert abs(s.pass_rate - 0.5) < 1e-9


def test_skipped_excluded_from_denominator():
    entries = [
        _e("2026-07-20", status="done", eid="KZN-20260720-001"),
        _e("2026-07-21", status="skipped", eid="KZN-20260721-001"),
        _e("2026-07-22", status="proposed", eid="KZN-20260722-001"),
    ]
    s = compute_action_stats(entries, TODAY)
    assert s.proposed == 2  # skip 除外
    assert s.skipped == 1
    assert s.done_rate == 0.5


def test_compute_action_stats_zero_rates_none():
    s = compute_action_stats([], TODAY)
    assert s.proposed == 0 and s.done_rate is None and s.pass_rate is None

    only_open = [_e("2026-07-20")]
    s2 = compute_action_stats(only_open, TODAY)
    assert s2.proposed == 1 and s2.done_rate == 0.0
    assert s2.judged == 0 and s2.pass_rate is None


def test_summarize_for_prompt_stats_block_and_dosing():
    five = [
        _e(f"2026-07-{d:02d}", status="proposed", eid=f"KZN-202607{d:02d}-001")
        for d in range(15, 20)
    ]
    s5 = summarize_for_prompt(five, TODAY)
    assert "## 提案の実績（直近14日）" in s5
    assert "提案5件" in s5
    assert "消化率が低いため" not in s5

    six = [
        _e(f"2026-07-{d:02d}", status="proposed", eid=f"KZN-202607{d:02d}-001")
        for d in range(15, 21)
    ]
    s6 = summarize_for_prompt(six, TODAY)
    assert "提案6件" in s6
    assert "消化率0%" in s6
    assert "消化率が低いため" in s6
    assert "1件" in s6

    # 6件中 3 done → 50% → 中程度 2件制限
    mixed = [
        _e("2026-07-15", status="done", eid="KZN-20260715-001"),
        _e("2026-07-16", status="done", eid="KZN-20260716-001"),
        _e("2026-07-17", status="done", eid="KZN-20260717-001"),
        _e("2026-07-18", status="proposed", eid="KZN-20260718-001"),
        _e("2026-07-19", status="proposed", eid="KZN-20260719-001"),
        _e("2026-07-20", status="proposed", eid="KZN-20260720-001"),
    ]
    s41 = summarize_for_prompt(mixed, TODAY)
    assert "消化率50%" in s41
    assert "最大2件" in s41
    assert "1件に制限" not in s41

    low = [
        _e("2026-07-15", status="done", eid="KZN-20260715-001"),
        _e("2026-07-16", status="done", eid="KZN-20260716-001"),
        _e("2026-07-17", eid="KZN-20260717-001"),
        _e("2026-07-18", eid="KZN-20260718-001"),
        _e("2026-07-19", eid="KZN-20260719-001"),
        _e("2026-07-20", eid="KZN-20260720-001"),
    ]
    s39 = summarize_for_prompt(low, TODAY)
    assert "消化率33%" in s39
    assert "消化率が低いため" in s39

    assert summarize_for_prompt([], TODAY) == ""


def test_summarize_undone_pass_signal():
    entries = [
        _e("2026-07-20", status="proposed", verdict="pass", eid="KZN-20260720-001"),
        _e("2026-07-21", status="proposed", verdict="pass", eid="KZN-20260721-001"),
    ]
    text = summarize_for_prompt(entries, TODAY)
    assert "行動せずに達成" in text


def test_summarize_verdict_block_and_skip():
    entries = [
        _e(
            "2026-07-24",
            status="done",
            verdict="pass",
            eid="KZN-20260724-001",
            action="朝に枠を入れる｜PASS: focus_blocks >= 2｜FAIL: 1",
        ),
        _e(
            "2026-07-23",
            status="skipped",
            eid="KZN-20260723-001",
            action="長い会議を減らす",
        ),
    ]
    # set skip_reason
    entries[1] = MemoryEntry(
        id="KZN-20260723-001",
        date="2026-07-23",
        action="長い会議を減らす",
        status="skipped",
        skip_reason="スケジュール都合",
    )
    # verdict_date for yesterday relative to TODAY
    entries[0] = MemoryEntry(
        id="KZN-20260724-001",
        date="2026-07-24",
        action="朝に枠を入れる｜PASS: focus_blocks >= 2｜FAIL: 1",
        status="done",
        verdict="pass",
        verdict_value=3.0,
        verdict_date="2026-07-24",
    )
    text = summarize_for_prompt(entries, TODAY)
    assert "## 直近の判定" in text
    assert "実行済み" in text
    assert "スキップされた提案" in text
    assert "スケジュール都合" in text


def test_thriving_branch():
    # 7 done all pass out of 7 → high rates
    entries = [
        _e(
            f"2026-07-{d:02d}",
            status="done",
            verdict="pass",
            eid=f"KZN-202607{d:02d}-001",
        )
        for d in range(18, 25)
    ]
    text = summarize_for_prompt(entries, TODAY)
    assert "負荷は適正" in text


def test_dosing_max_actions():
    low = ActionStats(
        window_days=14, proposed=6, done=1, judged=0, passed=0, done_judged=0, done_passed=0
    )
    assert dosing_max_actions(low) == 1
    mid = ActionStats(
        window_days=14, proposed=6, done=3, judged=0, passed=0, done_judged=0, done_passed=0
    )
    assert dosing_max_actions(mid) == 2
    high = ActionStats(
        window_days=14, proposed=6, done=5, judged=0, passed=0, done_judged=0, done_passed=0
    )
    assert dosing_max_actions(high) == 3
    few = ActionStats(
        window_days=14, proposed=3, done=0, judged=0, passed=0, done_judged=0, done_passed=0
    )
    assert dosing_max_actions(few) == 3


def test_render_action_stats_line_with_and_without_proposals():
    empty = ActionStats(window_days=14, proposed=0, done=0, judged=0, passed=0)
    assert "まだ提案がありません" in render_action_stats_line(empty)

    filled = ActionStats(
        window_days=14,
        proposed=12,
        done=5,
        judged=8,
        passed=6,
        done_judged=4,
        done_passed=3,
        undone_passed=2,
        skipped=1,
    )
    line = render_action_stats_line(filled)
    assert "提案 12件" in line
    assert "消化 5件（42%）" in line
    assert "スキップ 1件" in line
    assert "実行済みPASS" in line
    assert "未実行での達成 2件" in line

    no_judge = ActionStats(window_days=14, proposed=3, done=0, judged=0, passed=0)
    line2 = render_action_stats_line(no_judge)
    assert "消化 0件（0%）" in line2
    assert "実行済みPASS 0件（-）" in line2
