"""第37弾: 提案の質と学習ループ再起動（§A1–§D1 / §E1）。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaizenlog.advice_evidence import (
    _BASELINE_SIMPLE_METRICS,
    build_advice_evidence,
)
from kaizenlog.advice_format import render_advice_markdown, validate_advice
from kaizenlog.advisor import (
    _BASIC_PASS_METRICS,
    _STRUCTURED_AI_PASS_METRICS as ADVISOR_STRUCTURED_AI,
    available_pass_metrics,
    render_reader_advice,
)
from kaizenlog.aiwork import UserPrompt
from kaizenlog.decay import detect_kzn_decay
from kaizenlog.experiments import (
    compute_metric,
    metric_from_stats,
)
from kaizenlog.memory import (
    MemoryEntry,
    append_entries,
    assign_action_ids,
    metric_behavior_rates,
    metric_pass_rates,
    render_actions_section,
)
from kaizenlog.promptledger import load_prompt_ledger, upsert_clusters
from kaizenlog.promptmine import cluster_prompts
from kaizenlog.report import DailySummary
from kaizenlog.stats import write_stats
from kaizenlog.verdict import is_known_metric as verdict_is_known
from kaizenlog.verdict import parse_pass_condition
from tests.test_advice_format import _evidence, _valid_data


# ---------- helpers ----------


def _summary(
    *,
    day: date = date(2026, 7, 30),
    total_minutes: float = 220.0,
    context_switches: int = 210,
) -> DailySummary:
    return DailySummary(
        day=day,
        total_minutes=total_minutes,
        by_category={"開発": total_minutes},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=context_switches,
        by_site={},
    )


def _ai_session(tool_errors: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        tool_errors=tool_errors,
        interruptions=0,
        user_turns=1,
        output_tokens=0,
        is_fragmented=False,
    )


def _entry(**kwargs) -> MemoryEntry:
    values = dict(
        id="KZN-20260729-001",
        date="2026-07-29",
        action="改善｜PASS: context_switches <= 185｜FAIL: 186",
        status="proposed",
    )
    values.update(kwargs)
    return MemoryEntry(**values)


# ---------- §A1 ----------


def test_a1_context_switches_per_hour_floor_and_rounding():
    assert (
        compute_metric(
            "context_switches_per_hour",
            _summary(total_minutes=45.0, context_switches=100),
            [],
            None,
        )
        is None
    )
    assert (
        compute_metric(
            "context_switches_per_hour",
            _summary(total_minutes=220.0, context_switches=210),
            [],
            None,
        )
        == 57.3
    )
    assert (
        compute_metric(
            "context_switches_per_hour",
            _summary(total_minutes=219.5, context_switches=210),
            [],
            None,
        )
        == 57.4
    )
    # metric_from_stats 同値
    assert (
        metric_from_stats(
            "context_switches_per_hour",
            {"context_switches": 210, "total_minutes": 45},
        )
        is None
    )
    assert (
        metric_from_stats(
            "context_switches_per_hour",
            {"context_switches": 210, "total_minutes": 219.5},
        )
        == 57.4
    )


def test_a1_ai_tool_errors_per_session():
    sessions = [_ai_session(tool_errors=63) for _ in range(10)]
    # 639 / 10 = 63.9 — use 63*10 + 9 = 639
    sessions = [_ai_session(63) for _ in range(9)] + [_ai_session(72)]
    # 63*9+72 = 567+72 = 639
    assert (
        compute_metric("ai_tool_errors_per_session", _summary(), sessions, None)
        == 63.9
    )
    assert compute_metric("ai_tool_errors_per_session", _summary(), [], None) is None
    assert (
        metric_from_stats(
            "ai_tool_errors_per_session",
            {"ai": {"tool_errors": 639, "sessions": 10}},
        )
        == 63.9
    )
    assert (
        metric_from_stats(
            "ai_tool_errors_per_session",
            {"ai": {"tool_errors": 10, "sessions": 0}},
        )
        is None
    )


# ---------- §A2 / §E1 registration ----------


def test_a2_e1_metrics_registered_in_both_pass_sets_and_baselines():
    assert "context_switches_per_hour" in _BASIC_PASS_METRICS
    assert "ai_tool_errors_per_session" in ADVISOR_STRUCTURED_AI
    assert "context_switches_per_hour" in _BASELINE_SIMPLE_METRICS
    assert "ai_tool_errors_per_session" in _BASELINE_SIMPLE_METRICS
    # advice_format 側ローカル frozenset（契約入口）
    import inspect
    import kaizenlog.advice_format as af

    src = inspect.getsource(af._validate_advice_raise)
    assert "ai_tool_errors_per_session" in src
    assert "context_switches_per_hour" in _BASIC_PASS_METRICS

    # 構造化AIなし → 契約エラー
    evidence = replace(
        _evidence(),
        structured_ai_metrics_available=False,
        metric_baselines={},
    )
    data = _valid_data()
    data["proposals"] = data["proposals"][:1]
    data["actions"] = [
        {
            "fact_ids": ["F3"],
            "trigger": "始業の直後",
            "action": "エラーを減らす",
            "pass": "ai_tool_errors_per_session <= 10",
            "fail": "11",
            "mechanism": "手順を固定すると再実行が減ると考える",
            "falsifier": "ai_tool_errors_per_session が増えた場合",
        }
    ]
    errs = validate_advice(data, evidence)
    assert any("ai_tool_errors_per_session" in e and "構造化AI" in e for e in errs)

    # 陽性: 構造化AIありなら available に出る
    available = available_pass_metrics(
        replace(_evidence(), structured_ai_metrics_available=True)
    )
    assert "ai_tool_errors_per_session" in available
    assert "context_switches_per_hour" in available

    # 実装禁止の固定
    assert not verdict_is_known("ai_tool_error_rate")
    from kaizenlog.experiments import METRIC_DESCRIPTIONS

    assert "ai_tool_error_rate" not in METRIC_DESCRIPTIONS


# ---------- §A3 challenge with median baseline ----------


def test_a3_challenge_uses_history_median_not_today():
    history = [
        {
            "day": "2026-07-28",
            "context_switches": 55,
            "total_minutes": 60.0,
        },
        {
            "day": "2026-07-29",
            "context_switches": 57.4,
            "total_minutes": 60.0,
        },
        {
            "day": "2026-07-30",
            "context_switches": 60,
            "total_minutes": 60.0,
        },
    ]
    # 当日は履歴と異なる値
    current = {
        "day": "2026-07-31",
        "context_switches": 999,
        "total_minutes": 999.0,
        "by_category": {"開発": 100.0},
        "ai": {"sessions": 1, "fragmented": 0, "tool_errors": 0, "interruptions": 0},
        "input": {"focus_blocks": 1, "focus_minutes": 10, "keypresses": 10},
    }
    evidence = build_advice_evidence(current, history)
    # 中央値 57.4（55, 57.4, 60）
    assert evidence.metric_baselines is not None
    assert evidence.metric_baselines.get("context_switches_per_hour") == 57.4

    data = _valid_data()
    data["proposals"] = data["proposals"][:1]
    data["actions"] = [
        {
            "fact_ids": ["F3"],
            "trigger": "始業の直後",
            "action": "切替を減らす",
            "pass": "context_switches_per_hour <= 80",
            "fail": "81",
            "mechanism": "切替を減らすと集中が途切れにくいと考える",
            "falsifier": "context_switches_per_hour が増えた場合",
        }
    ]
    errs = validate_advice(data, evidence)
    assert any("緩すぎ" in e for e in errs)

    data["actions"][0]["pass"] = "context_switches_per_hour <= 54"
    data["actions"][0]["fail"] = "55"
    assert validate_advice(data, evidence) == []

    # 履歴2日以下 → 帯なし・ベースラインなし
    short = build_advice_evidence(current, history[:2])
    assert short.metric_baselines is None or (
        "context_switches_per_hour" not in (short.metric_baselines or {})
    )


# ---------- §A4 判定不成立 ----------


def test_a4_denominator_shortfall_shows_unmeasurable_not_fail():
    entry = _entry(
        id="KZN-20260729-010",
        date="2026-07-29",
        action="切替を減らす｜PASS: context_switches_per_hour <= 50｜FAIL: 51",
        status="proposed",
    )
    # 測定日 7/30 = 稼働45分
    stats_history = [
        {
            "day": "2026-07-30",
            "context_switches": 100,
            "total_minutes": 45.0,
        }
    ]
    out = render_actions_section(
        [entry], date(2026, 7, 31), stats_history=stats_history
    )
    assert out is not None
    assert "判定不成立" in out
    assert "稼働45" in out or "稼働45.0" in out
    assert "❌" not in out

    # stats_history 未指定なら従来どおり（提案日のみ）
    out_legacy = render_actions_section([entry], date(2026, 7, 31))
    assert out_legacy is not None
    assert "判定不成立" not in out_legacy
    assert "7/29提案" in out_legacy


# ---------- §B1 metric_behavior_rates ----------


def test_b1_behavior_rates_ignore_done_gate_and_exclude_provisional():
    today = date(2026, 8, 1)
    action = "改善｜PASS: context_switches <= 10｜FAIL: 11"
    entries: list[MemoryEntry] = []
    # proposed + confirmed: pass6 / fail2
    for i in range(6):
        entries.append(
            _entry(
                id=f"KZN-2026072{i}-001",
                date=f"2026-07-2{i}" if i < 10 else f"2026-07-{20 + i}",
                action=action,
                status="proposed",
                verdict="pass",
                verdict_value=5.0,
                verdict_date=f"2026-07-{21 + i:02d}" if 21 + i <= 31 else "2026-07-31",
                verdict_stage="confirmed",
            )
        )
    # fix dates properly
    entries = []
    for i, (v, st) in enumerate(
        [("pass", "proposed")] * 6 + [("fail", "proposed")] * 2
    ):
        d = date(2026, 7, 20) + timedelta(days=i)
        entries.append(
            _entry(
                id=f"KZN-{d.strftime('%Y%m%d')}-001",
                date=d.isoformat(),
                action=action,
                status=st,
                verdict=v,
                verdict_value=5.0 if v == "pass" else 20.0,
                verdict_date=(d + timedelta(days=1)).isoformat(),
                verdict_stage="confirmed",
            )
        )
    # noise: provisional, superseded, skipped, unmeasurable, graduated, retired
    for i, status in enumerate(
        ("superseded", "skipped", "unmeasurable", "graduated", "retired")
    ):
        d = date(2026, 7, 10) + timedelta(days=i)
        entries.append(
            _entry(
                id=f"KZN-{d.strftime('%Y%m%d')}-009",
                date=d.isoformat(),
                action=action,
                status=status,
                verdict="pass",
                verdict_value=1.0,
                verdict_date=(d + timedelta(days=1)).isoformat(),
                verdict_stage="confirmed",
            )
        )
    entries.append(
        _entry(
            id="KZN-20260715-099",
            date="2026-07-15",
            action=action,
            status="proposed",
            verdict="fail",
            verdict_value=99.0,
            verdict_date="2026-07-16",
            verdict_stage="provisional",
        )
    )

    assert metric_pass_rates(entries, today, min_judged=1) == []
    rates = metric_behavior_rates(entries, today, min_judged=1)
    assert rates == [("context_switches", 6, 8)]


# ---------- §B2 post-verdict trajectory ----------


def test_b2_post_verdict_trajectory_confirmed_only():
    action = "エラーを減らす｜PASS: ai_tool_errors <= 100｜FAIL: 101"
    confirmed = _entry(
        id="KZN-20260728-001",
        date="2026-07-28",
        action=action,
        status="proposed",
        verdict="pass",
        verdict_value=121.0,
        verdict_date="2026-07-28",
        verdict_stage="confirmed",
    )
    provisional = MemoryEntry(
        **{**confirmed.__dict__, "verdict_stage": "provisional", "id": "KZN-20260728-002"}
    )
    # 判定後: 7/29, 7/30, 7/31
    history = [
        {"day": "2026-07-29", "ai": {"tool_errors": 178, "sessions": 1}},
        {"day": "2026-07-30", "ai": {"tool_errors": 639, "sessions": 1}},
        {"day": "2026-07-31", "ai": {"tool_errors": 48, "sessions": 1}},
    ]
    # 第39弾: 達成済みは件数1行に畳むため、still_open 側の fail で推移を検証
    confirmed_fail = MemoryEntry(
        **{**confirmed.__dict__, "verdict": "fail", "verdict_value": 200.0}
    )
    out = render_actions_section(
        [confirmed_fail], date(2026, 8, 1), stats_history=history
    )
    assert out is not None
    assert "判定後の実測: 7/29 178 ❌ → 7/30 639 ❌ → 7/31 48 ✅" in out
    assert "実行の有無は問わない指標の挙動です" in out

    out_p = render_actions_section(
        [provisional], date(2026, 8, 1), stats_history=history
    )
    assert out_p is not None
    assert "判定後の実測" not in out_p

    out_no = render_actions_section([confirmed_fail], date(2026, 8, 1))
    assert out_no is not None
    assert "判定後の実測" not in out_no


# ---------- §B3 decay without done gate ----------


def test_b3_decay_fires_for_proposed_confirmed_pass(tmp_path):
    memory = tmp_path / "mem"
    stats = tmp_path / "stats"
    memory.mkdir()
    stats.mkdir()
    action = "改善｜PASS: context_switches <= 10｜FAIL: 11"
    entry = _entry(
        id="KZN-20260720-001",
        date="2026-07-20",
        action=action,
        status="proposed",  # not done
        verdict="pass",
        verdict_value=5.0,
        verdict_date="2026-07-21",
        verdict_stage="confirmed",
    )
    append_entries(memory, [entry])
    as_of = date(2026, 7, 28)
    for i in range(7):
        d = as_of - timedelta(days=i)
        write_stats(stats, d, _summary(day=d, context_switches=20), [])
    events = detect_kzn_decay(memory, stats, as_of=as_of)
    assert len(events) == 1
    assert events[0].ref_id == "KZN-20260720-001"


# ---------- §C1 / §C2 mechanism falsifier ----------


def test_c1_mechanism_falsifier_contract():
    data = _valid_data()
    evidence = _evidence()
    # 欠落
    del data["actions"][0]["mechanism"]
    errs = validate_advice(data, evidence)
    assert any("mechanism" in e for e in errs)

    data = _valid_data()
    data["actions"][0]["mechanism"] = "あ" * 51
    assert any("50字" in e for e in validate_advice(data, evidence))

    data = _valid_data()
    data["actions"][0]["mechanism"] = "数値12を含む"
    assert any("観測数値" in e or "mechanism" in e for e in validate_advice(data, evidence))

    data = _valid_data()
    data["actions"][0]["falsifier"] = "context_switches が 200 を超えた場合"
    assert validate_advice(data, evidence) == []


def test_c2_reader_lines_and_no_id_pollution():
    data = _valid_data()
    evidence = _evidence()
    md = render_advice_markdown(data, evidence)
    reader = render_reader_advice(
        "## 🚀 Kaizen（AIからの改善提案）\n\n" + md, evidence
    )
    assert "なぜ効くと考えるか:" in reader
    assert "効かなかったと分かる条件:" in reader
    # サブ行は - [ ] ではない
    for line in reader.splitlines():
        if "なぜ効くと考えるか" in line or "効かなかったと分かる条件" in line:
            assert not line.lstrip().startswith("- [")

    with_ids, entries = assign_action_ids(
        reader, date(2026, 7, 21), []
    )
    # アクション数と新規エントリ数が一致（サブ行にIDが振られない）
    assert len(entries) == len(data["actions"])
    # parse_pass_condition は action 行から同じ (metric, op, target)
    for e in entries:
        parsed = parse_pass_condition(e.action)
        assert parsed is not None
    # falsifier は MemoryEntry に入らない（第2段階のスコープ外・回帰固定）
    # 恒真にしないこと: "falsifier" は英語表記なので action には決して現れず、
    # or の左辺が常に真になって検証が無効化される。
    for e in entries:
        assert "効かなかったと分かる条件" not in (e.action or "")
        assert "なぜ効くと考えるか" not in (e.action or "")


# ---------- §D1 PRM daily line ----------


def test_d1_repeat_prompt_line(tmp_path):
    from kaizenlog.cli import _format_repeat_prompt_line
    from kaizenlog.promptledger import PromptLedgerEntry, append_prompt_ledger

    memory = tmp_path / "mem"
    memory.mkdir()
    day = date(2026, 8, 1)
    # 正規依頼を複数
    prompts = [
        UserPrompt(
            text="pushしてください",
            timestamp=datetime(2026, 8, 1, 10, i, tzinfo=timezone.utc),
            project="p",
            source="claude-code",
        )
        for i in range(3)
    ]
    # システム注入XML（表示から除外）
    prompts.extend(
        UserPrompt(
            text="<task-notification>something</task-notification>",
            timestamp=datetime(2026, 8, 1, 11, i, tzinfo=timezone.utc),
            project="p",
            source="claude-code",
        )
        for i in range(5)
    )
    # skilled エントリを台帳に先置き → 表示行にその representative が出ない
    append_prompt_ledger(
        memory,
        [
            PromptLedgerEntry(
                id="PRM-20260701-001",
                representative="既にスキル化した依頼文XYZ",
                count_total=9,
                days_seen=4,
                first_seen="2026-07-01",
                last_seen="2026-07-10",
                status="skilled",
                skill_name="x",
                marked_on="2026-07-10",
            )
        ],
    )
    line = _format_repeat_prompt_line(prompts, memory, day)
    assert "台帳の最終観測" in line
    assert "push" in line
    assert "task-notification" not in line
    assert "既にスキル化した依頼文XYZ" not in line

    # 0件なら空
    assert _format_repeat_prompt_line([], memory, day) == ""
    single = [
        UserPrompt(
            text="一度だけの依頼",
            timestamp=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
            project="p",
            source="claude-code",
        )
    ]
    assert _format_repeat_prompt_line(single, memory, day) == ""

    # 同日2回で count_total が増えない
    line2 = _format_repeat_prompt_line(prompts, memory, day)
    ledger2 = load_prompt_ledger(memory)
    push_entries = [e for e in ledger2 if "push" in e.representative.lower()]
    assert push_entries
    assert push_entries[0].count_total == 3  # max, not doubled
