from __future__ import annotations

from kaizenlog import advisor
from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.config import LLMConfig
from kaizenlog.reliability import FailureReason
from kaizenlog.runlog import load_runs, log_advise_health


def test_daily_contract_falls_back_once_after_primary_repair_fails(monkeypatch):
    from tests.test_advice_evidence import CURRENT, VALID_ADVICE_JSON

    calls: list[str] = []
    evidence = build_advice_evidence(CURRENT)

    def fake_generate(cfg, system, user, **kwargs):
        calls.append(cfg.backend)
        return VALID_ADVICE_JSON if cfg.backend == "openai-compatible" else "not json"

    monkeypatch.setattr(advisor, "generate_text", fake_generate)
    cfg = LLMConfig(
        backend="claude-code-cli",
        fallback_to_local=True,
        system_prompt="daily_advisor",
    )

    result = advisor.generate_advice(cfg, "today", [], evidence=evidence)

    assert calls == ["claude-code-cli", "claude-code-cli", "openai-compatible"]
    assert result.actual_backend == "openai-compatible"
    assert result.fallback_used is True
    assert result.reason_codes == ["contract_invalid"]


def test_advice_health_records_configured_and_actual_backend(tmp_path):
    log_advise_health(
        tmp_path,
        day="2026-09-01",
        backend="openai-compatible",
        configured_backend="claude-code-cli",
        actual_backend="openai-compatible",
        outcome="repaired",
        duration_seconds=1.2,
        reason_codes=["contract_invalid"],
    )

    row = load_runs(tmp_path)[-1]

    assert row["schema_version"] == 2
    assert row["configured_backend"] == "claude-code-cli"
    assert row["actual_backend"] == "openai-compatible"
    assert row["backend"] == "openai-compatible"
    assert row["reason_codes"] == ["contract_invalid"]
    assert len(row["run_id"]) == 32


def test_advice_health_keeps_unknown_actual_backend_distinct(tmp_path):
    log_advise_health(
        tmp_path,
        day="2026-09-01",
        backend="claude-code-cli",
        configured_backend="claude-code-cli",
        actual_backend=None,
        outcome="failed",
        duration_seconds=1.2,
    )

    row = load_runs(tmp_path)[-1]

    assert row["actual_backend"] is None
    assert row["backend"] == "claude-code-cli"


def test_primary_auth_fallback_repairs_with_active_local_backend_once(monkeypatch):
    """Re-probing a dead primary during repair would exceed the provider call bound."""
    from tests.test_advice_evidence import CURRENT, VALID_ADVICE_JSON

    calls: list[str] = []
    evidence = build_advice_evidence(CURRENT)

    def unavailable(*_):
        calls.append("claude-code-cli")
        raise advisor.BackendUnavailable("not logged in")

    def local(_, __, ___):
        calls.append("openai-compatible")
        return "not json" if calls.count("openai-compatible") == 1 else VALID_ADVICE_JSON

    monkeypatch.setattr(advisor, "_call_claude_code_cli", unavailable)
    monkeypatch.setattr(advisor, "_call_openai_compatible", local)
    cfg = LLMConfig(
        backend="claude-code-cli",
        fallback_to_local=True,
        retries=2,
        system_prompt="daily_advisor",
    )

    result = advisor.generate_advice(cfg, "today", [], evidence=evidence)

    assert calls == ["claude-code-cli", "openai-compatible", "openai-compatible"]
    assert result.actual_backend == "openai-compatible"
    assert result.reason_codes == [FailureReason.PROVIDER_AUTH_REQUIRED.value]
