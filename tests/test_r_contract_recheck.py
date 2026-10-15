"""Current-task rechecks for the approved R5/R6/R7 contracts.

These are synthetic, credential-free checks.  They deliberately assert the
consumer-visible unknown/partial states rather than source implementation.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.aiwork import AISession, measured_metric_total, render_aiwork_markdown
from kaizenlog.experiments import Experiment, compute_abtest_effect, format_effect_size
from kaizenlog.ops_ledger import OpsLedger
from kaizenlog.reliability import FailureReason
from kaizenlog.report import DailySummary
from kaizenlog.runlog import load_runs, log_run
from kaizenlog.stats import build_stats


def _session(*, measured: bool, tokens: int = 0) -> AISession:
    start = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)
    return AISession(
        session_id="s1",
        project="demo",
        start=start,
        end=start,
        output_tokens=tokens,
        output_tokens_measurable=measured,
    )


def _summary() -> DailySummary:
    return DailySummary(
        day=date(2026, 10, 15),
        total_minutes=30.0,
        by_category={"開発": 30.0},
        by_app={"Code.exe": 30.0},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
        by_site={},
    )


def test_r5_unknown_not_zero_in_consumer() -> None:
    sessions = [_session(measured=False)]

    assert measured_metric_total(sessions, "output_tokens", "output_tokens_measurable") is None
    rendered = render_aiwork_markdown(sessions, timezone.utc)
    assert "出力トークン: 計測不能" in rendered
    stats = build_stats(date(2026, 10, 15), _summary(), sessions)
    assert stats["ai"]["output_tokens"] is None
    assert stats["ai"]["measurement"]["output_tokens"] == "unknown"


def test_r5_partial_coverage_keeps_gap_reason() -> None:
    evidence = build_advice_evidence(
        {
            "total_minutes": 60,
            "context_switches": 2,
            "by_category": {"ブラウジング": 20},
            "by_app": {"chrome.exe": 30},
            "by_site": {"example.com": 10},
            "blocks": [],
        },
        source_status="verified",
    )

    assert "URL観測率" in evidence.markdown
    assert "未観測部分の用途は不明" in evidence.markdown
    assert evidence.browser_sample_sufficient is False


def test_r6_missing_baseline_blocks_effect_and_pass(tmp_path) -> None:
    effect, ai_days, non_ai_days, reason = compute_abtest_effect(
        [
            {"day": "2026-10-13", "total_minutes": 100, "ai": {"sessions": 1}},
        ],
        start=date(2026, 10, 13),
        end=date(2026, 10, 13),
        pre_stats=[],
    )
    assert effect is None
    assert ai_days == 0 and non_ai_days == 0
    assert reason and "baseline不足" in reason
    exp = Experiment(
        path=tmp_path / "EXP-1.md",
        title="synthetic",
        metric="total_minutes",
        target_op="<=",
        target_value=10,
        status="running",
        baseline=None,
        measurements={},
    )
    assert format_effect_size(exp) is None


def test_r7_run_ids_are_retained_and_ledger_failure_is_not_success(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ops.sqlite3"
    assert log_run(tmp_path, "run", True, 0.1, run_id="run-a", ops_db_path=db) is None
    assert log_run(tmp_path, "run", True, 0.1, run_id="run-b", ops_db_path=db) is None
    assert {row["run_id"] for row in load_runs(tmp_path)} == {"run-a", "run-b"}
    assert {row["run_id"] for row in OpsLedger(db).load_runs()} == {"run-a", "run-b"}

    def fail_append(_self, _entry):
        raise OSError("synthetic ledger failure")

    monkeypatch.setattr(OpsLedger, "append", fail_append)
    result = log_run(tmp_path, "run", True, 0.1, run_id="run-c", ops_db_path=db)
    assert result is FailureReason.LEDGER_WRITE_FAILED
    assert any(row["command"] == "ops_ledger_health" and row["ok"] is False for row in load_runs(tmp_path))
