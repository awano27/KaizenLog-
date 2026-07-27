"""第12弾: 学習ループ配線 J1〜J6。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advice_format import normalize_advice_cardinality
from kaizenlog.advisor import build_prompt
from kaizenlog.memory import (
    MemoryEntry,
    append_entries,
    compute_action_stats,
    dosing_max_actions,
    load_entries,
    mark_entry_skipped,
    render_actions_section,
    update_statuses_from_note,
)
from kaizenlog.verdict import format_verdict_suffix, judge_entries
from kaizenlog.report import DailySummary
from tests.test_advice_evidence import CURRENT, HISTORY
from tests.test_advice_format import _valid_data


# ---- J2 skip ----------------------------------------------------------------


def test_j2_checkbox_skip_and_reason():
    entries = [
        MemoryEntry(
            id="KZN-20260720-001",
            date="2026-07-20",
            action="会議を減らす",
            status="proposed",
        )
    ]
    note = "- [-] KZN-20260720-001: 会議を減らす｜理由: 今は無理\n"
    upd = update_statuses_from_note(note, entries, date(2026, 7, 25))
    assert len(upd) == 1
    assert upd[0].status == "skipped"
    assert upd[0].skip_reason == "今は無理"


def test_j2_skip_jsonl_compat(tmp_path):
    mem = tmp_path / "m"
    e = MemoryEntry(
        id="KZN-20260720-001",
        date="2026-07-20",
        action="a",
        status="skipped",
        skip_reason="x",
    )
    append_entries(mem, [e])
    loaded = load_entries(mem)
    assert loaded[0].status == "skipped"
    assert loaded[0].skip_reason == "x"
    # 旧行（skip_reason 無し）
    path = mem / "suggestions.jsonl"
    path.write_text(
        path.read_text(encoding="utf-8")
        + '{"id":"KZN-20260721-001","date":"2026-07-21","action":"b","status":"proposed"}\n',
        encoding="utf-8",
    )
    loaded2 = load_entries(mem)
    old = next(x for x in loaded2 if x.id == "KZN-20260721-001")
    assert old.skip_reason is None


def test_j2_skip_excluded_from_handoff():
    entries = [
        MemoryEntry(
            id="KZN-20260724-001",
            date="2026-07-24",
            action="a",
            status="skipped",
        ),
        MemoryEntry(
            id="KZN-20260724-002",
            date="2026-07-24",
            action="b",
            status="proposed",
        ),
    ]
    md = render_actions_section(entries, date(2026, 7, 25))
    assert md is not None
    assert "KZN-20260724-001" not in md
    assert "KZN-20260724-002" in md


def test_j2_judge_skips_skipped_status():
    from kaizenlog.aiwork import AISession
    from datetime import datetime, timezone

    entries = [
        MemoryEntry(
            id="KZN-20260724-001",
            date="2026-07-24",
            action="x｜PASS: context_switches <= 40｜FAIL: 41",
            status="skipped",
        ),
        MemoryEntry(
            id="KZN-20260724-002",
            date="2026-07-24",
            action="y｜PASS: context_switches <= 40｜FAIL: 41",
            status="proposed",
        ),
    ]
    summary = DailySummary(
        day=date(2026, 7, 25),
        total_minutes=100,
        by_category={"開発": 100},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=30,
    )
    judged = judge_entries(
        entries, date(2026, 7, 24), summary, [], None, date(2026, 7, 25)
    )
    ids = {e.id for e in judged}
    assert "KZN-20260724-001" not in ids
    assert "KZN-20260724-002" in ids


def test_j2_cmd_skip(tmp_path):
    from kaizenlog import cli as cli_mod
    from kaizenlog.config import Config

    vault = tmp_path / "v"
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    cfg = Config(
        vault_dir=vault,
        memory_dir="Kaizen/Memory",
        timezone="Asia/Tokyo",
    )
    append_entries(
        cfg.memory_path,
        [
            MemoryEntry(
                id="KZN-20260720-001",
                date="2026-07-20",
                action="a",
                status="proposed",
            )
        ],
    )
    assert cli_mod.cmd_skip(cfg, "KZN-20260720-001", reason="nope") == 0
    e = load_entries(cfg.memory_path)[-1]
    assert e.status == "skipped"
    assert e.skip_reason == "nope"


# ---- J5 verdict suffix ------------------------------------------------------


def test_j5_fail_distance_and_near():
    fail = MemoryEntry(
        id="KZN-20260720-001",
        date="2026-07-20",
        action="x｜PASS: context_switches <= 40｜FAIL: 41",
        status="done",
        verdict="fail",
        verdict_value=45.0,
    )
    s = format_verdict_suffix(fail)
    assert "あと5" in s
    assert "目標40" in s

    near = MemoryEntry(
        id="KZN-20260720-002",
        date="2026-07-20",
        action="x｜PASS: context_switches <= 40｜FAIL: 41",
        status="done",
        verdict="fail",
        verdict_value=43.0,  # 7.5% over → within 10%
    )
    s2 = format_verdict_suffix(near)
    assert "あと一歩" in s2

    ge_fail = MemoryEntry(
        id="KZN-20260720-003",
        date="2026-07-20",
        action="x｜PASS: focus_blocks >= 3｜FAIL: 2",
        status="done",
        verdict="fail",
        verdict_value=1.0,
    )
    s3 = format_verdict_suffix(ge_fail)
    assert "あと2" in s3 or "あと 2" in s3

    ok = MemoryEntry(
        id="KZN-20260720-004",
        date="2026-07-20",
        action="x｜PASS: context_switches <= 40｜FAIL: 41",
        status="done",
        verdict="pass",
        verdict_value=35.0,
    )
    assert "実測35" in format_verdict_suffix(ok)
    assert "目標40" in format_verdict_suffix(ok)


# ---- J4 max_actions dosing --------------------------------------------------


def test_j4_max_actions_min_with_short_record():
    from kaizenlog.memory import ActionStats

    # short record → 1, dosing would be 3 → min = 1
    stats_ok = ActionStats(
        window_days=14, proposed=10, done=9, judged=5, passed=5,
        done_judged=5, done_passed=5,
    )
    ev = build_advice_evidence(
        {**CURRENT, "total_minutes": 50.0},
        HISTORY,
        action_stats=stats_ok,
    )
    assert ev.max_actions == 1

    # long record + low done rate → 1
    low = ActionStats(
        window_days=14, proposed=6, done=1, judged=0, passed=0,
        done_judged=0, done_passed=0,
    )
    ev2 = build_advice_evidence(CURRENT, HISTORY, action_stats=low)
    assert ev2.max_actions == 1

    # long + mid
    mid = ActionStats(
        window_days=14, proposed=6, done=3, judged=0, passed=0,
        done_judged=0, done_passed=0,
    )
    ev3 = build_advice_evidence(CURRENT, HISTORY, action_stats=mid)
    assert ev3.max_actions == 2


def test_j4_normalize_caps_to_dosing():
    from kaizenlog.memory import ActionStats

    low = ActionStats(
        window_days=14, proposed=6, done=1, judged=0, passed=0,
        done_judged=0, done_passed=0,
    )
    ev = build_advice_evidence(CURRENT, HISTORY, action_stats=low)
    assert ev.max_actions == 1
    data = _valid_data()  # 2 proposals
    # force 2 proposals/actions but max 1
    out = normalize_advice_cardinality(data, ev)
    assert len(out["proposals"]) == 1
    assert len(out["actions"]) == 1


# ---- J6 reflections ---------------------------------------------------------


def test_j6_reflections_in_prompt():
    from kaizenlog.cli import _extract_reflections

    note = "## Reflections\n今日は疲れている\n\n## Other\nx\n"
    r = _extract_reflections(note)
    assert r is not None
    assert "疲れている" in r
    note2 = "## 振り返り\n困った\n"
    assert "困った" in (_extract_reflections(note2) or "")
    assert _extract_reflections("## Tasks\n- a\n") is None

    p = build_prompt(
        "log",
        [],
        reflections="## Reflections\n困り事あり",
    )
    assert "ユーザーの振り返り" in p
    assert "困り事あり" in p
