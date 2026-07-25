"""消化率 / PASS率の集計と適応投与・status 表示。"""
from __future__ import annotations

from datetime import date

from kaizenlog.memory import (
    ActionStats,
    MemoryEntry,
    compute_action_stats,
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
) -> MemoryEntry:
    return MemoryEntry(
        id=eid or f"KZN-{day.replace('-', '')}-001",
        date=day,
        action="x",
        status=status,
        verdict=verdict,
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
    assert abs(s.pass_rate - 2 / 3) < 1e-9


def test_compute_action_stats_zero_rates_none():
    s = compute_action_stats([], TODAY)
    assert s.proposed == 0 and s.done_rate is None and s.pass_rate is None

    only_open = [_e("2026-07-20")]
    s2 = compute_action_stats(only_open, TODAY)
    assert s2.proposed == 1 and s2.done_rate == 0.0
    assert s2.judged == 0 and s2.pass_rate is None


def test_summarize_for_prompt_stats_block_and_dosing():
    # 5件 + 消化率低めでもサンプル不足 → 適応投与なし
    five = [
        _e(f"2026-07-{d:02d}", status="proposed", eid=f"KZN-202607{d:02d}-001")
        for d in range(15, 20)
    ]
    s5 = summarize_for_prompt(five, TODAY)
    assert "## 提案の実績（直近14日）" in s5
    assert "提案5件" in s5
    assert "消化率が低いため" not in s5

    # 6件すべて未完了 → 消化率 0% < 40% → 適応投与あり
    six = [
        _e(f"2026-07-{d:02d}", status="proposed", eid=f"KZN-202607{d:02d}-001")
        for d in range(15, 21)
    ]
    s6 = summarize_for_prompt(six, TODAY)
    assert "提案6件" in s6
    assert "消化率0%" in s6
    assert "消化率が低いため" in s6
    assert "1件だけ" in s6

    # 6件中 3 done → 50% >= 40% → 適応投与なし
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
    assert "消化率が低いため" not in s41

    # 6件中 2 done → 約33% < 40%
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


def test_render_action_stats_line_with_and_without_proposals():
    empty = ActionStats(window_days=14, proposed=0, done=0, judged=0, passed=0)
    assert "まだ提案がありません" in render_action_stats_line(empty)

    filled = ActionStats(window_days=14, proposed=12, done=5, judged=8, passed=6)
    line = render_action_stats_line(filled)
    assert "提案 12件" in line
    assert "消化 5件（42%）" in line
    assert "自動判定 8件" in line
    assert "PASS 6件（75%）" in line

    no_judge = ActionStats(window_days=14, proposed=3, done=0, judged=0, passed=0)
    line2 = render_action_stats_line(no_judge)
    assert "消化 0件（0%）" in line2
    assert "PASS 0件（-）" in line2
