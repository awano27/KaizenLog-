from __future__ import annotations

from kaizenlog import advisor
from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.config import LLMConfig
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
