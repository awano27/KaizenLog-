from dataclasses import replace

import kaizenlog.advisor as advisor
from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.config import LLMConfig


def _evidence(**overrides):
    base = build_advice_evidence(None)
    return replace(
        base,
        known_categories=frozenset({"開発", "ブラウジング"}),
        observed_sites=frozenset({"github.com"}),
        **overrides,
    )


def test_available_pass_metrics_excludes_unmeasurable_metric_families():
    metrics = advisor.available_pass_metrics(_evidence())

    assert metrics == (
        "context_switches",
        "context_switches_per_hour",
        "total_active_minutes",
        "category_minutes:ブラウジング",
        "category_minutes:開発",
    )
    assert "ai_tool_errors" not in metrics
    assert "focus_blocks" not in metrics
    assert "site_minutes:github.com" not in metrics


def test_available_pass_metrics_includes_only_enabled_metric_families():
    metrics = advisor.available_pass_metrics(
        _evidence(
            structured_ai_metrics_available=True,
            input_metrics_available=True,
            site_metrics_available=True,
        )
    )

    assert "ai_retry_chains" in metrics
    assert "ai_tool_errors" in metrics
    assert "focus_blocks" in metrics
    assert "input_keypresses" in metrics
    assert "site_minutes:github.com" in metrics


def _metric_section(text, heading):
    start = text.index(heading) + len(heading)
    remainder = text[start:]
    end = remainder.find("\n## ")
    return remainder if end < 0 else remainder[:end]


def test_daily_prompt_marks_unmeasurable_metrics_as_forbidden():
    evidence = _evidence()

    system_prompt, _, _ = advisor.prepare_advice_request(
        LLMConfig(system_prompt="daily_advisor"),
        "activity",
        [],
        evidence=evidence,
    )

    available = _metric_section(system_prompt, "## 当日使用可能なPASS指標")
    forbidden = _metric_section(system_prompt, "## 当日使用禁止のPASS指標")
    assert "context_switches" in available
    assert "category_minutes:開発" in available
    assert "ai_tool_errors" not in available
    assert "focus_blocks" not in available
    assert "ai_tool_errors" in forbidden
    assert "focus_blocks" in forbidden
    assert "{{KAIZENLOG_PASS_METRIC_CONTRACT}}" not in system_prompt


def test_contract_repair_uses_the_same_available_metric_contract():
    evidence = _evidence()

    prompt = advisor._contract_repair_prompt(
        evidence,
        '{"actions":[]}',
        ["actions[1] の pass が計測不能です"],
    )

    available = _metric_section(prompt, "## 当日使用可能なPASS指標")
    forbidden = _metric_section(prompt, "## 当日使用禁止のPASS指標")
    assert "context_switches" in available
    assert "ai_tool_errors" not in available
    assert "ai_tool_errors" in forbidden
    assert "例: `context_switches <= 40`" in prompt
