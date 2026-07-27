"""第13弾: 較正・if-then・指標実績・ストリーク（K1〜K4）。"""
from __future__ import annotations

from datetime import date

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advice_format import (
    parse_advice_json,
    render_advice_markdown,
    validate_advice,
)
from kaizenlog.memory import (
    MemoryEntry,
    assign_action_ids,
    compute_streaks,
    metric_pass_rates,
    render_actions_section,
    summarize_for_prompt,
)
from kaizenlog.verdict import apply_verdicts_to_advice_note, parse_pass_condition
from kaizenlog.vault import ADVICE_MARKER, extract_section, upsert_section
from tests.test_advice_evidence import CURRENT, HISTORY
from tests.test_advice_format import _valid_data


def test_k1_trigger_required_and_render():
    data = _valid_data()
    assert validate_advice(data, build_advice_evidence(CURRENT, HISTORY)) == []
    md = render_advice_markdown(data, build_advice_evidence(CURRENT, HISTORY))
    assert "始業の直後→" in md or "→" in md
    # missing trigger
    bad = _valid_data()
    del bad["actions"][0]["trigger"]
    errs = validate_advice(bad, build_advice_evidence(CURRENT, HISTORY))
    assert any("trigger" in e for e in errs)
    # newline in trigger
    bad2 = _valid_data()
    bad2["actions"][0]["trigger"] = "a\nb"
    assert any("改行" in e for e in validate_advice(bad2, build_advice_evidence(CURRENT, HISTORY)))


def test_k1_roundtrip_id_pass_verdict():
    data = _valid_data()
    md = render_advice_markdown(data, build_advice_evidence(CURRENT, HISTORY))
    full = f"## 🚀 Kaizen\n\n{md}"
    with_ids, entries = assign_action_ids(full, date(2026, 7, 21), [])
    assert entries
    assert "→" in entries[0].action
    parsed = parse_pass_condition(entries[0].action)
    assert parsed is not None
    # verdict writeback
    e = entries[0]
    e = MemoryEntry(
        id=e.id,
        date=e.date,
        action=e.action,
        status="done",
        verdict="pass",
        verdict_value=2.0,
        verdict_date="2026-07-22",
    )
    content = upsert_section("---\n---\n", ADVICE_MARKER, with_ids)
    updated = apply_verdicts_to_advice_note(content, [e])
    assert updated and "｜判定:" in updated


def test_k2_calibration_branches():
    today = date(2026, 7, 25)
    # low pass rate: 0/3 done fails
    low = [
        MemoryEntry(
            id=f"KZN-2026072{i}-001",
            date=f"2026-07-2{i}",
            action="x｜PASS: context_switches <= 40｜FAIL: 41",
            status="done",
            verdict="fail",
            verdict_value=50.0,
            verdict_date=f"2026-07-2{i}",
        )
        for i in range(2, 5)
    ]
    text = summarize_for_prompt(low, today)
    assert "一段緩める" in text
    # high pass
    high = [
        MemoryEntry(
            id=f"KZN-2026071{i}-001",
            date=f"2026-07-1{i}",
            action="x｜PASS: context_switches <= 40｜FAIL: 41",
            status="done",
            verdict="pass",
            verdict_value=10.0,
            verdict_date=f"2026-07-1{i}",
        )
        for i in range(5, 9)
    ]
    text_h = summarize_for_prompt(high, today)
    assert "挑戦的" in text_h
    # <3 judged → no calibrate
    few = high[:2]
    assert "較正" not in summarize_for_prompt(few, today)
    # 2 consecutive fail same metric
    fails = [
        MemoryEntry(
            id="KZN-20260723-001",
            date="2026-07-23",
            action="x｜PASS: focus_blocks >= 3｜FAIL: 2",
            status="done",
            verdict="fail",
            verdict_value=1.0,
            verdict_date="2026-07-23",
        ),
        MemoryEntry(
            id="KZN-20260724-001",
            date="2026-07-24",
            action="y｜PASS: focus_blocks >= 3｜FAIL: 2",
            status="done",
            verdict="fail",
            verdict_value=0.0,
            verdict_date="2026-07-24",
        ),
        MemoryEntry(
            id="KZN-20260722-001",
            date="2026-07-22",
            action="z｜PASS: focus_blocks >= 3｜FAIL: 2",
            status="done",
            verdict="pass",
            verdict_value=3.0,
            verdict_date="2026-07-22",
        ),
    ]
    t2 = summarize_for_prompt(fails, today)
    assert "focus_blocks" in t2 and "2連続FAIL" in t2


def test_k2_f10_band_and_insufficient_history():
    ev = build_advice_evidence(CURRENT, HISTORY)
    assert "[F10]" in ev.markdown
    assert "推奨PASS帯" in ev.markdown
    # no history → no F10
    ev2 = build_advice_evidence(CURRENT, [])
    assert "[F10]" not in ev2.markdown


def test_k3_metric_pass_rates():
    today = date(2026, 7, 25)
    entries = [
        MemoryEntry(
            id=f"KZN-202607{d:02d}-001",
            date=f"2026-07-{d:02d}",
            action="x｜PASS: context_switches <= 40｜FAIL: 41",
            status="done",
            verdict="pass" if d % 2 == 0 else "fail",
            verdict_value=30.0,
            verdict_date=f"2026-07-{d:02d}",
        )
        for d in range(10, 16)
    ]
    rows = metric_pass_rates(entries, today, window_days=30, min_judged=3)
    assert any(m == "context_switches" for m, _, _ in rows)
    text = summarize_for_prompt(entries, today)
    assert "指標別PASS実績" in text
    assert "context_switches" in text


def test_k4_streaks():
    today = date(2026, 7, 25)
    # done on 23, 24, 25 → current 3
    entries = []
    for d in (23, 24, 25):
        entries.append(
            MemoryEntry(
                id=f"KZN-202607{d:02d}-001",
                date=f"2026-07-{d:02d}",
                action="a",
                status="done",
                done_date=f"2026-07-{d:02d}",
            )
        )
    s = compute_streaks(entries, today)
    assert s.current >= 3
    assert s.best >= 3
    # miss yesterday breaks current for today
    entries2 = [
        MemoryEntry(
            id="KZN-20260724-001",
            date="2026-07-24",
            action="a",
            status="proposed",
        ),
        MemoryEntry(
            id="KZN-20260723-001",
            date="2026-07-23",
            action="a",
            status="done",
            done_date="2026-07-23",
        ),
    ]
    s2 = compute_streaks(entries2, today)
    assert s2.broken_yesterday is True
    # skip-only day does not break
    entries3 = [
        MemoryEntry(
            id="KZN-20260725-001",
            date="2026-07-25",
            action="a",
            status="done",
            done_date="2026-07-25",
        ),
        MemoryEntry(
            id="KZN-20260724-001",
            date="2026-07-24",
            action="a",
            status="skipped",
        ),
        MemoryEntry(
            id="KZN-20260723-001",
            date="2026-07-23",
            action="a",
            status="done",
            done_date="2026-07-23",
        ),
    ]
    s3 = compute_streaks(entries3, today)
    assert s3.current >= 2


def test_k4_streak_and_low_period_display():
    today = date(2026, 7, 26)
    # 6 proposed 1 done → low rate framing
    entries = [
        MemoryEntry(
            id=f"KZN-202607{d:02d}-001",
            date=f"2026-07-{d:02d}",
            action="a",
            status="done" if d == 20 else "proposed",
            done_date="2026-07-20" if d == 20 else None,
        )
        for d in range(15, 21)
    ]
    # add streak material
    for d in (24, 25):
        entries.append(
            MemoryEntry(
                id=f"KZN-202607{d:02d}-099",
                date=f"2026-07-{d:02d}",
                action="b",
                status="done",
                done_date=f"2026-07-{d:02d}",
            )
        )
    md = render_actions_section(entries, today)
    assert md is not None
    assert "今週の消化" in md or "消化率" in md
    assert "🔥" in md or "連続" in md or "再スタート" in md
