"""第15弾: 学習ループ調整 L1〜L4。"""
from __future__ import annotations

import time
from copy import deepcopy
from datetime import date

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.memory import (
    MemoryEntry,
    compute_action_stats,
    compute_streaks,
    metric_pass_rates,
    summarize_for_prompt,
)
from tests.test_advice_evidence import CURRENT, HISTORY


TODAY = date(2026, 7, 28)


def _done(
    day: str,
    *,
    status: str = "done",
    verdict: str = "pass",
    done_date: str | None = None,
    verdict_date: str | None = None,
    action: str = "x｜PASS: context_switches <= 40｜FAIL: 41",
    eid: str | None = None,
) -> MemoryEntry:
    return MemoryEntry(
        id=eid or f"KZN-{day.replace('-', '')}-001",
        date=day,
        action=action,
        status=status,
        done_date=done_date,
        verdict=verdict,
        verdict_value=30.0 if verdict else None,
        verdict_date=verdict_date,
    )


# ---- §L1: pre-execution PASS を done_passed から分離 ---------------------------


def test_l1_pre_execution_pass_not_in_done_passed():
    """verdict_date < done_date → done_passed に入らず undone_passed 側。"""
    entries = [
        _done(
            "2026-07-20",
            verdict="pass",
            verdict_date="2026-07-21",  # 夜間オート判定
            done_date="2026-07-25",  # 後日チェック
            eid="KZN-20260720-001",
        ),
        _done(
            "2026-07-22",
            verdict="pass",
            verdict_date="2026-07-23",
            done_date="2026-07-23",  # 実行後判定
            eid="KZN-20260722-001",
        ),
    ]
    s = compute_action_stats(entries, TODAY, window_days=14)
    assert s.done == 2
    assert s.done_judged == 1
    assert s.done_passed == 1
    assert s.undone_judged == 1
    assert s.undone_passed == 1
    assert s.pass_rate == 1.0


def test_l1_verdict_on_or_after_done_stays_done_passed():
    entries = [
        _done(
            "2026-07-20",
            verdict="pass",
            verdict_date="2026-07-21",
            done_date="2026-07-20",
        ),
        _done(
            "2026-07-21",
            verdict="fail",
            verdict_date="2026-07-22",
            done_date="2026-07-22",
            eid="KZN-20260721-001",
        ),
    ]
    s = compute_action_stats(entries, TODAY)
    assert s.done_judged == 2
    assert s.done_passed == 1
    assert s.undone_passed == 0


def test_l1_metric_pass_rates_same_stratification():
    """metric_pass_rates も verdict_date < done_date を除外。"""
    # 3件とも事前PASS+後日done → 実行済み層に0 → 行自体が出ない
    pre = [
        _done(
            f"2026-07-{d:02d}",
            verdict="pass",
            verdict_date=f"2026-07-{d + 1:02d}",
            done_date="2026-07-27",
            eid=f"KZN-202607{d:02d}-001",
        )
        for d in (10, 12, 14)
    ]
    rows = metric_pass_rates(pre, TODAY, window_days=30, min_judged=3)
    assert rows == []

    # 実行後判定3件 → 行が出る
    post = [
        _done(
            f"2026-07-{d:02d}",
            verdict="pass",
            verdict_date=f"2026-07-{d + 1:02d}",
            done_date=f"2026-07-{d:02d}",
            eid=f"KZN-202607{d:02d}-001",
        )
        for d in (10, 12, 14)
    ]
    rows2 = metric_pass_rates(post, TODAY, window_days=30, min_judged=3)
    assert any(m == "context_switches" and p == 3 and j == 3 for m, p, j in rows2)


def test_l1_summarize_calibration_not_polluted_by_pre_pass():
    """事前オートPASSだけで done_passed を膨らませ較正を汚さない。"""
    # 3件すべて done+pass だが verdict < done → done_judged=0 → 較正なし
    entries = [
        _done(
            f"2026-07-{d:02d}",
            verdict="pass",
            verdict_date=f"2026-07-{d:02d}",
            done_date="2026-07-27",
            eid=f"KZN-202607{d:02d}-001",
        )
        for d in (20, 21, 22)
    ]
    text = summarize_for_prompt(entries, TODAY)
    assert "挑戦的" not in text  # 高PASS率較正が出ない
    assert "未実行での達成" in text


# ---- §L2: F10 帯と当日計測可否の対称 ------------------------------------------


def _history_with_focus_ai(n: int = 5) -> list[dict]:
    rows = []
    for i in range(n):
        rows.append(
            {
                "day": f"2026-07-{15 + i:02d}",
                "total_minutes": 200.0,
                "context_switches": 80 + i,
                "by_category": {"開発": 90.0, "AI作業": 40.0},
                "input": {
                    "focus_blocks": 4 + i % 2,
                    "focus_minutes": 100.0,
                    "active_input_minutes": 80.0,
                },
                "ai": {
                    "sessions": 3 + i % 2,
                    "fragmented": 0,
                    "tool_errors": 0,
                    "interruptions": 0,
                },
            }
        )
    return rows


def test_l2_f10_omits_focus_ai_when_watchers_down():
    """当日 input/ai 欄なし → F10 に focus_*/ai_* 帯が出ない。"""
    stats = deepcopy(CURRENT)
    del stats["input"]
    del stats["ai"]
    hist = _history_with_focus_ai()
    ev = build_advice_evidence(stats, hist)
    assert "[F3]" in ev.markdown and "測定不能" in ev.markdown
    # F10 自体は context_switches 等で残りうる
    if "[F10]" in ev.markdown:
        f10 = [ln for ln in ev.markdown.splitlines() if "[F10]" in ln][0]
        assert "focus_blocks" not in f10
        assert "ai_cc_sessions" not in f10
    assert not ev.input_metrics_available
    assert not ev.structured_ai_metrics_available


def test_l2_f10_includes_focus_ai_when_available():
    """当日 watcher あり + 履歴十分 → focus / ai 帯が出る。"""
    hist = _history_with_focus_ai()
    ev = build_advice_evidence(CURRENT, hist)
    assert ev.input_metrics_available
    assert ev.structured_ai_metrics_available
    assert "[F10]" in ev.markdown
    f10 = [ln for ln in ev.markdown.splitlines() if "[F10]" in ln][0]
    assert "focus_blocks" in f10
    assert "ai_cc_sessions" in f10


# ---- §L3: 2連続FAIL の30日窓 --------------------------------------------------


def test_l3_consecutive_fail_outside_window_ignored():
    today = date(2026, 7, 28)
    # 31日以上前の2連続FAIL
    old = [
        MemoryEntry(
            id="KZN-20260620-001",
            date="2026-06-20",
            action="x｜PASS: focus_blocks >= 3｜FAIL: 2",
            status="done",
            done_date="2026-06-20",
            verdict="fail",
            verdict_value=1.0,
            verdict_date="2026-06-20",
        ),
        MemoryEntry(
            id="KZN-20260621-001",
            date="2026-06-21",
            action="y｜PASS: focus_blocks >= 3｜FAIL: 2",
            status="done",
            done_date="2026-06-21",
            verdict="fail",
            verdict_value=0.0,
            verdict_date="2026-06-21",
        ),
    ]
    text = summarize_for_prompt(old, today)
    assert "2連続FAIL" not in text


def test_l3_consecutive_fail_inside_window_still_fires():
    today = date(2026, 7, 28)
    recent = [
        MemoryEntry(
            id="KZN-20260720-001",
            date="2026-07-20",
            action="x｜PASS: focus_blocks >= 3｜FAIL: 2",
            status="done",
            done_date="2026-07-20",
            verdict="fail",
            verdict_value=1.0,
            verdict_date="2026-07-20",
        ),
        MemoryEntry(
            id="KZN-20260721-001",
            date="2026-07-21",
            action="y｜PASS: focus_blocks >= 3｜FAIL: 2",
            status="done",
            done_date="2026-07-21",
            verdict="fail",
            verdict_value=0.0,
            verdict_date="2026-07-21",
        ),
    ]
    text = summarize_for_prompt(recent, today)
    assert "focus_blocks" in text and "2連続FAIL" in text


# ---- §L4: 遠未来日付でストリークが退行しない ----------------------------------


def test_l4_far_future_date_does_not_slow_streaks():
    today = date(2026, 7, 28)
    entries = [
        MemoryEntry(
            id="KZN-20260727-001",
            date="2026-07-27",
            action="a",
            status="done",
            done_date="2026-07-27",
        ),
        MemoryEntry(
            id="KZN-20260726-001",
            date="2026-07-26",
            action="a",
            status="done",
            done_date="2026-07-26",
        ),
        # 破損行
        MemoryEntry(
            id="KZN-99990101-001",
            date="9999-01-01",
            action="corrupt",
            status="done",
            done_date="9999-01-01",
        ),
    ]
    t0 = time.perf_counter()
    s = compute_streaks(entries, today)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5  # 全日走査なら数秒級; 存在する日のみなら即時
    assert s.current >= 2
    assert s.best >= 2


def test_l4_normal_streak_unchanged():
    today = date(2026, 7, 25)
    entries = [
        MemoryEntry(
            id=f"KZN-202607{d:02d}-001",
            date=f"2026-07-{d:02d}",
            action="a",
            status="done",
            done_date=f"2026-07-{d:02d}",
        )
        for d in (23, 24, 25)
    ]
    s = compute_streaks(entries, today)
    assert s.current >= 3
    assert s.best >= 3
