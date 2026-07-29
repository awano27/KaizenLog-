"""第16弾: 日誌価値向上 N1〜N4。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advice_format import validate_advice
from kaizenlog.aiwork import AISession, render_aiwork_markdown
from kaizenlog.experiments import METRIC_DESCRIPTIONS, metric_display_label
from tests.test_advice_evidence import CURRENT, HISTORY
from tests.test_advice_format import _valid_data


# ---- §N1 challenge gate ----------------------------------------------------


def _action(pass_v: str, fail_v: str = "fail") -> dict:
    data = _valid_data()
    # single action day
    data["proposals"] = data["proposals"][:1]
    data["actions"] = [
        {
            "fact_ids": ["F3"],
            "trigger": "始業の直後",
            "action": "試す",
            "pass": pass_v,
            "fail": fail_v,
        }
    ]
    return data


def test_n1_loose_le_rejected_tight_accepted():
    ev = build_advice_evidence(CURRENT, HISTORY)
    # force baseline for tool errors
    basemap = dict(ev.metric_baselines or {})
    basemap["ai_tool_errors"] = 121.0
    ev = replace(ev, metric_baselines=basemap)
    # need structured AI available for ai_tool_errors
    assert ev.structured_ai_metrics_available
    bad = _action("ai_tool_errors <= 500", "501")
    errs = validate_advice(bad, ev)
    assert any("緩すぎ" in e or "ベースライン" in e for e in errs)

    good = _action("ai_tool_errors <= 100", "101")
    assert validate_advice(good, ev) == []


def test_n1_ge_challenge_bounds():
    ev = build_advice_evidence(CURRENT, HISTORY)
    basemap = dict(ev.metric_baselines or {})
    basemap["focus_blocks"] = 3.4
    ev = replace(
        ev,
        metric_baselines=basemap,
        input_metrics_available=True,
    )
    # >= 2.8 is >= 3.4*0.8=2.72 → ok; >= 1 too loose
    ok = _action("focus_blocks >= 2.8", "2")
    assert validate_advice(ok, ev) == []
    bad = _action("focus_blocks >= 1", "0")
    errs = validate_advice(bad, ev)
    assert any("緩すぎ" in e or "ベースライン" in e for e in errs)


def test_n1_no_baseline_skips_challenge():
    ev = build_advice_evidence(CURRENT, HISTORY)
    # strip all baselines
    ev = replace(ev, metric_baselines=None, structured_ai_metrics_available=True)
    data = _action("ai_tool_errors <= 500", "501")
    # may still fail for other reasons; challenge specifically should not fire
    errs = validate_advice(data, ev)
    assert not any("緩すぎ" in e for e in errs)


# ---- §N2 reader summary -----------------------------------------------------


def test_n2_error_concentration_in_summary():
    stats = deepcopy(CURRENT)
    stats["total_minutes"] = 200.0
    stats["ai"] = {
        "sessions": 3,
        "fragmented": 0,
        "tool_errors": 121,
        "interruptions": 0,
        "projects": {
            "kaizenlog": {"sessions": 1, "turns": 2, "errors": 70, "fragmented": 0},
            "other": {"sessions": 2, "turns": 4, "errors": 51, "fragmented": 0},
        },
    }
    ev = build_advice_evidence(stats, HISTORY)
    assert "ツールエラー121回中70回" in ev.reader_summary
    assert "kaizenlog" in ev.reader_summary
    assert "合計" in ev.reader_summary
    # no entertainment diagnosis language
    assert "無駄" not in ev.reader_summary
    assert "生産性" not in ev.reader_summary


def test_n2_short_day_single_sentence():
    stats = deepcopy(CURRENT)
    stats["total_minutes"] = 44.0
    stats["ai"] = {
        "sessions": 1,
        "fragmented": 0,
        "tool_errors": 50,
        "interruptions": 0,
        "projects": {"p": {"sessions": 1, "turns": 1, "errors": 50, "fragmented": 0}},
    }
    ev = build_advice_evidence(stats, HISTORY)
    assert "データ不足" in ev.reader_summary
    assert "ツールエラー" not in ev.reader_summary


def test_n2_previous_day_delta():
    stats = deepcopy(CURRENT)
    stats["day"] = "2026-07-21"
    stats["total_minutes"] = 300.0
    hist = [
        {"day": "2026-07-20", "total_minutes": 180.0, "context_switches": 90},
        {"day": "2026-07-19", "total_minutes": 200.0, "context_switches": 80},
        {"day": "2026-07-18", "total_minutes": 220.0, "context_switches": 70},
    ]
    ev = build_advice_evidence(stats, hist)
    assert ev.previous_day_available
    assert "前日比" in ev.reader_summary


# ---- §N3 labels -------------------------------------------------------------


def test_n3_ai_metric_labels_not_claude_only():
    for key in (
        "ai_cc_sessions",
        "ai_fragmented_sessions",
        "ai_tool_errors",
        "ai_interruptions",
        "ai_avg_turns",
        "ai_output_tokens",
    ):
        desc = METRIC_DESCRIPTIONS[key]
        # 単一製品のみの旧文言を禁止
        assert "Claude Codeの" not in desc
        label = metric_display_label(key)
        assert label
        assert "（" not in label and "(" not in label
        if key == "ai_tool_errors":
            assert "AI CLI" in label or "合算" in label


# ---- §N4 cost display -------------------------------------------------------


def _sess(tokens: int, model: str, sid: str = "1") -> AISession:
    return AISession(
        session_id=sid,
        project="p",
        start=datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 7, 28, 10, 30, tzinfo=timezone.utc),
        user_turns=2,
        output_tokens=tokens,
        models={model},
        source="codex",
    )


def test_n4_uncosted_majority_hides_dollar():
    # known small + unknown large
    known = _sess(10_000, "claude-sonnet-4", "k")  # priced
    unknown = _sess(200_000, "unknown-local", "u")
    md = render_aiwork_markdown([known, unknown], timezone.utc)
    # R25 S3: フォールバックは「推定コスト: -（…換算なし）」に統一（トークン数値は1回）
    assert "換算なし" in md
    assert "推定コスト: $" not in md
    assert "210,000" in md or "210000" in md.replace(",", "")
    assert md.count("210,000") == 1 or md.replace(",", "").count("210000") == 1


def test_n4_costed_majority_keeps_dollar():
    known = _sess(1_000_000, "claude-sonnet-4", "k")
    unknown = _sess(5_000, "unknown-local", "u")
    md = render_aiwork_markdown([known, unknown], timezone.utc)
    assert "推定コスト: $" in md
    assert "換算なし" not in md
