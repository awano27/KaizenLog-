"""ACTION-UX 残件: atomic retry / 3日退役 / 同時3件 / パーセンタイル拒否 / 因果抑制。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advice_format import normalize_advice_cardinality, validate_advice
from kaizenlog.memory import (
    MAX_ACTIVE_PROPOSED,
    MemoryEntry,
    causal_mismatch_metrics,
    count_open_proposed,
    format_lifecycle_reader_notes,
    graduate_entries,
)
from kaizenlog.vault import atomic_write_text
from tests.test_advice_evidence import CURRENT, HISTORY
from tests.test_advice_format import _evidence, _valid_data


def test_atomic_write_retries_permission_error(tmp_path):
    path = tmp_path / "note.md"
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        Path(dst).write_bytes(Path(src).read_bytes())
        Path(src).unlink(missing_ok=True)

    with patch("kaizenlog.vault.os.replace", side_effect=flaky_replace):
        atomic_write_text(path, "hello\n")
    assert path.read_text(encoding="utf-8") == "hello\n"
    assert calls["n"] == 3


def test_graduate_retires_after_three_days_unchecked(tmp_path):
    stats = tmp_path / "stats"
    stats.mkdir()
    e = MemoryEntry(
        id="KZN-20260701-001",
        date="2026-07-01",
        action="x｜PASS: context_switches <= 10｜FAIL: 11",
        status="proposed",
    )
    # age=3, no measurable days → retired with reason
    g = graduate_entries([e], date(2026, 7, 4), stats_dir=stats)
    assert len(g) == 1
    assert g[0].status == "retired"
    assert g[0].closed_reason == "unchecked_no_measurement"
    notes = format_lifecycle_reader_notes(g, today=date(2026, 7, 4))
    assert any("退役" in n for n in notes)
    assert any("終了扱いは達成を意味しません" in n for n in notes)


def test_count_open_and_max_active_zero_actions():
    ents = [
        MemoryEntry(
            id=f"KZN-2026072{i}-001",
            date=f"2026-07-2{i}",
            action="a｜PASS: context_switches <= 10｜FAIL: 11",
            status="proposed",
        )
        for i in range(5, 9)
    ]
    assert count_open_proposed(ents) == 4
    assert count_open_proposed(ents) > MAX_ACTIVE_PROPOSED
    ev = build_advice_evidence(
        {**CURRENT, "total_minutes": 400.0},
        HISTORY,
        open_proposed=4,
    )
    assert ev.max_actions == 0
    data = _valid_data()
    out = normalize_advice_cardinality(data, ev)
    assert out["proposals"] == []
    assert out["actions"] == []
    # empty + ai_review only
    ok = {
        "plan_review": None,
        "proposals": [],
        "actions": [],
        "ai_review": [{"fact_ids": ["F4"], "text": "計測状況を維持する"}],
    }
    assert validate_advice(ok, ev) == []


def test_pass_range_rejects_out_of_distribution():
    # HISTORY has tool errors in a high range typically; build evidence with series
    ev = build_advice_evidence(
        {**CURRENT, "total_minutes": 400.0},
        HISTORY,
    )
    # Force history values for ai_tool_errors
    from dataclasses import replace

    forced = replace(
        ev,
        metric_history_values={"ai_tool_errors": (200.0, 300.0, 400.0, 406.0)},
        max_actions=1,
    )
    data = _valid_data()
    data["actions"][0]["pass"] = "ai_tool_errors <= 40"
    data["actions"][0]["fail"] = "41以上"
    # only keep 1 pair
    data["proposals"] = data["proposals"][:1]
    data["actions"] = data["actions"][:1]
    errs = validate_advice(data, forced)
    assert errs
    assert any("厳しすぎ" in e or "実測" in e for e in errs)


def test_causal_mismatch_suppresses_metric():
    e = MemoryEntry(
        id="KZN-20260801-001",
        date="2026-08-01",
        action="x｜PASS: focus_blocks >= 2｜FAIL: 1回以下",
        status="proposed",
        verdict="pass",
        verdict_stage="confirmed",
        verdict_value=5.0,
        verdict_date="2026-08-01",
    )
    assert "focus_blocks" in causal_mismatch_metrics([e])
    from dataclasses import replace

    base = build_advice_evidence(
        {**CURRENT, "total_minutes": 400.0},
        HISTORY,
        open_proposed=0,
    )
    ev = replace(
        base,
        suppressed_metrics=frozenset({"focus_blocks"}),
        metric_history_values=None,
        max_actions=1,
    )
    data = _valid_data()
    data["proposals"] = data["proposals"][:1]
    data["actions"] = data["actions"][:1]
    data["actions"][0]["pass"] = "focus_blocks >= 2"
    data["actions"][0]["fail"] = "1回以下"
    errs = validate_advice(data, ev)
    assert any("抑制" in err for err in errs), errs
