"""第11弾 I1〜I3: 実験設計v2・evalハーネス・PC外測定限界。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advisor import (
    AdviceContractError,
    PipelineReport,
    _run_daily_pipeline,
    generate_advice,
)
from kaizenlog.config import Config, LLMConfig
from kaizenlog.experiments import (
    MEASUREMENTS_MARKER,
    create_experiment,
    effect_size,
    format_effect_size,
    load_experiments,
    record_measurement,
    render_experiments_context,
    weekday_baseline,
)
from kaizenlog.intervention import BlockRule, TimeSink, render_plan
from kaizenlog.vault import extract_section
from tests.test_advice_evidence import CURRENT, HISTORY
from tests.test_advice_format import _valid_data


# ---- I1 weekday baseline / effect size ------------------------------------


def _stats_day(d: date, metric_val: float, metric: str = "context_switches") -> dict:
    s: dict = {
        "day": d.isoformat(),
        "total_minutes": 120.0,
        "context_switches": float(metric_val) if metric == "context_switches" else 10.0,
    }
    if metric.startswith("category_minutes:"):
        cat = metric.split(":", 1)[1]
        s["by_category"] = {cat: float(metric_val)}
    return s


def test_i1_weekday_baseline_needs_two_samples():
    # Monday 2026-07-20
    mon = date(2026, 7, 20)
    # only one prior Monday
    stats = [_stats_day(date(2026, 7, 13), 40)]
    assert weekday_baseline("context_switches", mon, stats) is None
    stats.append(_stats_day(date(2026, 7, 6), 60))
    assert weekday_baseline("context_switches", mon, stats) == 50.0


def test_i1_weekday_baseline_ignores_other_weekdays():
    mon = date(2026, 7, 20)
    stats = [
        _stats_day(date(2026, 7, 13), 40),  # Mon
        _stats_day(date(2026, 7, 14), 999),  # Tue — ignore
        _stats_day(date(2026, 7, 6), 20),  # Mon
    ]
    assert weekday_baseline("context_switches", mon, stats) == 30.0


def test_i1_record_measurement_four_columns_and_legacy_three(tmp_path):
    path = create_experiment(
        tmp_path,
        title="t",
        metric="context_switches",
        target="<= 40",
        today=date(2026, 7, 20),
        deadline=date(2026, 8, 3),
        baseline=50.0,
    )
    exp = load_experiments(tmp_path)[0]
    assert exp.start == date(2026, 7, 20)
    # legacy 3-col table
    content = path.read_text(encoding="utf-8")
    legacy = (
        "## Measurements（自動計測）\n\n"
        "| 日付 | 値 | 目標達成 |\n"
        "| --- | ---: | :-: |\n"
        "| 2026-07-21 | 30 | ✅ |\n"
    )
    from kaizenlog.vault import upsert_section

    content = upsert_section(content, MEASUREMENTS_MARKER, legacy)
    path.write_text(content, encoding="utf-8")
    exp = load_experiments(tmp_path)[0]
    assert exp.measurements[date(2026, 7, 21)] == 30.0

    record_measurement(
        exp,
        date(2026, 7, 22),
        35.0,
        weekday_baselines={
            date(2026, 7, 21): 45.0,
            date(2026, 7, 22): 48.0,
        },
    )
    text = path.read_text(encoding="utf-8")
    sec = extract_section(text, MEASUREMENTS_MARKER)
    assert sec is not None
    assert "同曜日基準" in sec
    assert "48" in sec
    # still parseable
    exp2 = load_experiments(tmp_path)[0]
    assert exp2.measurements[date(2026, 7, 22)] == 35.0
    assert exp2.measurements[date(2026, 7, 21)] == 30.0


def test_i1_effect_size_none_and_percent(tmp_path):
    path = create_experiment(
        tmp_path,
        "e",
        "context_switches",
        "<= 40",
        date(2026, 7, 1),
        date(2026, 7, 15),
        baseline=100.0,
    )
    exp = load_experiments(tmp_path)[0]
    assert effect_size(exp) is None  # no measurements
    exp.measurements[date(2026, 7, 2)] = 68.0
    exp.measurements[date(2026, 7, 3)] = 72.0
    # median 70 → -30%
    assert effect_size(exp) == -30.0
    assert format_effect_size(exp) == "効果量 -30%"
    exp.baseline = 0
    assert effect_size(exp) is None
    exp.baseline = None
    assert effect_size(exp) is None


def test_i1_render_context_includes_effect_size(tmp_path):
    create_experiment(
        tmp_path,
        "e",
        "context_switches",
        "<= 40",
        date(2026, 7, 1),
        date(2026, 7, 15),
        baseline=100.0,
    )
    exp = load_experiments(tmp_path)[0]
    exp.measurements[date(2026, 7, 2)] = 50.0
    ctx = render_experiments_context([exp])
    assert "効果量 -50%" in ctx
    assert "風船" in ctx or "移行" in ctx


def test_i1_template_notes_confound(tmp_path):
    path = create_experiment(
        tmp_path,
        "n",
        "context_switches",
        "<= 40",
        date(2026, 7, 1),
        date(2026, 7, 15),
    )
    text = path.read_text(encoding="utf-8")
    assert "同曜日基準" in text
    assert "交絡" in text


# ---- I2 eval harness --------------------------------------------------------


def _mock_valid_json(*_a, **_k) -> str:
    return json.dumps(_valid_data(), ensure_ascii=False)


def _mock_bad_then_good():
    calls = {"n": 0}

    def fn(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not-json at all"
        return json.dumps(_valid_data(), ensure_ascii=False)

    return fn, calls


def test_i2_pipeline_first_pass():
    from kaizenlog.advisor import prepare_advice_request

    cfg = LLMConfig(backend="openai-compatible", system_prompt="daily_advisor")
    evidence = build_advice_evidence(CURRENT, HISTORY)
    system, prompt, ev = prepare_advice_request(
        cfg, "## log", [], None, None, None, None, evidence
    )
    assert ev is not None
    md, report = _run_daily_pipeline(
        cfg, system, prompt, ev, generate_fn=_mock_valid_json
    )
    assert md is not None
    assert report.first_pass is True
    assert report.final_ok is True
    assert report.repaired is False
    assert report.outcome == "ok"


def test_i2_pipeline_repaired():
    from kaizenlog.advisor import prepare_advice_request

    cfg = LLMConfig(backend="openai-compatible", system_prompt="daily_advisor")
    evidence = build_advice_evidence(CURRENT, HISTORY)
    system, prompt, ev = prepare_advice_request(
        cfg, "## log", [], None, None, None, None, evidence
    )
    fn, calls = _mock_bad_then_good()
    md, report = _run_daily_pipeline(cfg, system, prompt, ev, generate_fn=fn)
    assert md is not None
    assert report.repaired is True
    assert report.final_ok is True
    assert report.first_pass is False
    assert report.outcome == "repaired"
    assert calls["n"] >= 2


def test_i2_pipeline_degraded():
    from kaizenlog.advisor import prepare_advice_request

    cfg = LLMConfig(backend="openai-compatible", system_prompt="daily_advisor")
    evidence = build_advice_evidence(CURRENT, HISTORY)
    system, prompt, ev = prepare_advice_request(
        cfg, "## log", [], None, None, None, None, evidence
    )
    md, report = _run_daily_pipeline(
        cfg, system, prompt, ev, generate_fn=lambda *a, **k: "still broken"
    )
    assert md is None
    assert report.final_ok is False
    assert report.outcome == "degraded"


def test_i2_generate_advice_raises_on_degraded(monkeypatch):
    cfg = LLMConfig(backend="openai-compatible", system_prompt="daily_advisor")
    monkeypatch.setattr(
        "kaizenlog.advisor.generate_text",
        lambda *a, **k: "broken",
    )
    with pytest.raises(AdviceContractError):
        generate_advice(
            cfg,
            "## log",
            [],
            evidence=build_advice_evidence(CURRENT, HISTORY),
        )


def test_i2_eval_run_aggregate_and_min_pass_rate(tmp_path, monkeypatch):
    from kaizenlog.evalharness import (
        EvalAggregate,
        EvalCase,
        format_eval_table,
        run_eval,
        save_case,
    )
    from kaizenlog import cli as cli_mod

    case = EvalCase(
        id="c1",
        day="2026-07-21",
        current_stats=CURRENT,
        prior_stats=HISTORY,
        today_md="## Activity",
        known_categories=["開発", "AI作業", "ブラウジング"],
    )
    cases_dir = tmp_path / "cases"
    save_case(cases_dir / "c1.json", case)

    llm = LLMConfig(backend="openai-compatible", system_prompt="daily_advisor")
    agg = run_eval([case], llm, repeat=2, generate_fn=_mock_valid_json)
    assert agg.total_runs == 2
    assert agg.final_pass_rate == 1.0
    assert "修復後合格率" in format_eval_table(agg)

    def always_bad(*_a, **_k):
        return "nope"

    agg2 = run_eval([case], llm, repeat=2, generate_fn=always_bad)
    assert agg2.final_pass_rate == 0.0
    assert agg2.degraded_rate == 1.0

    vault = tmp_path / "v"
    vault.mkdir()
    cfg = Config(vault_dir=vault, timezone="Asia/Tokyo")

    # cmd_eval_run は関数内 import なのでモジュール属性を差し替える
    monkeypatch.setattr(
        "kaizenlog.evalharness.run_eval",
        lambda *a, **k: agg2,
    )
    monkeypatch.setattr(
        "kaizenlog.evalharness.load_cases_dir",
        lambda d: [case],
    )
    code = cli_mod.cmd_eval_run(cfg, cases_dir, repeat=1, min_pass_rate=0.5)
    assert code == 1
    code_ok = cli_mod.cmd_eval_run(cfg, cases_dir, repeat=1, min_pass_rate=None)
    assert code_ok == 0


def test_i2_record_applies_redaction(tmp_path, monkeypatch):
    from kaizenlog.evalharness import (
        build_case_from_inputs,
        redact_case,
    )
    from kaizenlog.privacy import make_redactor

    case = build_case_from_inputs(
        day=date(2026, 7, 21),
        current_stats={"day": "2026-07-21", "total_minutes": 10, "secret_title": "CONFIDENTIAL_TOKEN_XYZ"},
        prior_stats=[],
        today_md="title CONFIDENTIAL_TOKEN_XYZ in log",
        recent_summaries=[],
        intent=None,
        experiments_ctx=None,
        memory_ctx=None,
        source_status="unverified",
        timezone="Asia/Tokyo",
        known_categories=[],
    )
    red = make_redactor([r"CONFIDENTIAL_TOKEN_XYZ"], "[REDACTED]")
    out = redact_case(case, red)
    assert "CONFIDENTIAL_TOKEN_XYZ" not in json.dumps(out.to_dict())
    assert "[REDACTED]" in out.today_md


def test_i2_gitignore_mentions_eval_cases():
    root = Path(__file__).resolve().parents[1]
    gi = (root / ".gitignore").read_text(encoding="utf-8")
    assert "eval/cases" in gi


# ---- I3 balloon / L13 -------------------------------------------------------


def test_i3_l13_always_in_evidence():
    md = build_advice_evidence(CURRENT, HISTORY).markdown
    assert "[L13]" in md
    assert "スマホ" in md or "他デバイス" in md
    md2 = build_advice_evidence(None, []).markdown
    assert "[L13]" in md2


def test_i3_render_plan_balloon_note():
    sinks = [
        TimeSink(
            domains="youtube.com",
            label="youtube.com",
            total_minutes=280.0,
            days_with_data=7,
            source="site",
        )
    ]
    rules = [
        BlockRule(
            set_name="KZN: youtube",
            sites="youtube.com",
            times="",
            limit_mins=15,
            limit_period=86400,
            metric="site_minutes:youtube.com",
            target="<= 15",
            evidence="平均40分",
            window=None,
        )
    ]
    plan = render_plan(sinks, rules)
    assert "風船" in plan or "スマホ" in plan


def test_i3_prompts_mention_l13():
    from importlib import resources

    daily = (resources.files("kaizenlog") / "prompts" / "daily_advisor.md").read_text(
        encoding="utf-8"
    )
    priv = (resources.files("kaizenlog") / "prompts" / "privacy_safe.md").read_text(
        encoding="utf-8"
    )
    assert "L13" in daily
    assert "L13" in priv
