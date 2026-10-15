"""Unknown AI telemetry must never be interpreted as a measured zero downstream."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from kaizenlog.aiwork import AISession, UserPrompt
from kaizenlog.config import AIWorkConfig, Config
from kaizenlog.excavate import format_excavate_report, run_excavate
from kaizenlog.experiments import Experiment, compute_metric, metric_from_stats, should_measure_experiment
from kaizenlog.handoffledger import HandoffLesson, measure_effect
from kaizenlog.report import DailySummary


UTC = timezone.utc
DAY = date(2026, 9, 6)


def _summary() -> DailySummary:
    return DailySummary(
        day=DAY,
        total_minutes=60.0,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
    )


def _session(
    session_id: str,
    when: datetime,
    *,
    tool_errors: int = 0,
    output_tokens: int = 0,
    tool_metrics_measurable: bool = True,
    output_tokens_measurable: bool = True,
) -> AISession:
    return AISession(
        session_id=session_id,
        project="repo",
        start=when,
        end=when + timedelta(minutes=5),
        user_turns=3,
        tool_errors=tool_errors,
        output_tokens=output_tokens,
        models={"claude-sonnet-4"},
        tool_metrics_measurable=tool_metrics_measurable,
        output_tokens_measurable=output_tokens_measurable,
    )


def test_experiment_metrics_and_stats_reject_mixed_unknown_capabilities(tmp_path: Path) -> None:
    now = datetime(2026, 9, 6, 10, tzinfo=UTC)
    sessions = [
        _session("known", now, tool_errors=2, output_tokens=100),
        _session(
            "unknown",
            now + timedelta(minutes=10),
            tool_metrics_measurable=False,
            output_tokens_measurable=False,
        ),
    ]
    experiment = Experiment(
        path=tmp_path / "experiment.md",
        title="ツールエラーを減らす",
        status="running",
        metric="ai_tool_errors_per_session",
        target_op="<=",
        target_value=1.0,
        start=DAY,
    )

    assert should_measure_experiment(experiment, DAY) is True
    assert compute_metric("ai_tool_errors", _summary(), sessions) is None
    assert compute_metric("ai_tool_errors_per_session", _summary(), sessions) is None
    assert compute_metric("ai_interruptions", _summary(), sessions) is None
    assert compute_metric("ai_output_tokens", _summary(), sessions) is None
    assert experiment.measurements == {}
    assert metric_from_stats(
        "ai_tool_errors_per_session",
        {"ai": {"sessions": 2, "tool_errors": 0, "measurement": {"tool_errors": "partial"}}},
    ) is None
    assert metric_from_stats(
        "ai_output_tokens",
        {"ai": {"output_tokens": 0, "measurement": {"output_tokens": "partial"}}},
    ) is None


def test_handoff_tool_error_effect_is_unknown_when_either_window_has_unmeasured_session(
    tmp_path: Path,
) -> None:
    first_injected = DAY - timedelta(days=29)
    target = tmp_path / "repo" / "CLAUDE.md"
    target.parent.mkdir()
    lesson = HandoffLesson(
        lesson_id="HND-tool-errors",
        kind="toolerr",
        ref_id="tool-errors",
        text="ツールエラーを確認する",
    )
    before = _session(
        "before", datetime(2026, 8, 7, 10, tzinfo=UTC), tool_errors=5
    )
    after_known = _session(
        "after-known", datetime(2026, 9, 6, 10, tzinfo=UTC), tool_errors=0
    )
    after_unknown = _session(
        "after-unknown",
        datetime(2026, 9, 6, 11, tzinfo=UTC),
        tool_metrics_measurable=False,
    )

    display, good = measure_effect(
        lesson,
        target=target,
        first_injected=first_injected.isoformat(),
        as_of=DAY,
        prompts=[],
        sessions=[before, after_known, after_unknown],
        memory_dir=tmp_path / "memory",
        stats_dir=tmp_path / "stats",
    )

    assert (display, good) == ("不明", None)


def test_excavate_renders_mixed_unknown_tool_and_token_totals_as_unknown(tmp_path: Path) -> None:
    now = datetime(2026, 9, 6, 10, tzinfo=UTC)
    sessions = [
        _session("known", now, tool_errors=2, output_tokens=100),
        _session(
            "unknown",
            now + timedelta(minutes=10),
            tool_metrics_measurable=False,
            output_tokens_measurable=False,
        ),
    ]
    cfg = Config(
        vault_dir=tmp_path,
        timezone="UTC",
        aiwork=AIWorkConfig(enabled=True),
    )
    prompts = [
        UserPrompt(
            timestamp=now,
            project="repo",
            text="please fix failing test now",
            source="claude-code",
            session_id="known",
        ),
        UserPrompt(
            timestamp=now + timedelta(minutes=5),
            project="repo",
            text="please fix failing test now",
            source="claude-code",
            session_id="known",
        ),
    ]

    with (
        patch("kaizenlog.excavate.available_adapters", return_value=[MagicMock()]),
        patch("kaizenlog.excavate.collect_ai_telemetry", return_value=(sessions, prompts, 0)),
    ):
        report = run_excavate(cfg, days=1, as_of=DAY)

    assert report.tool_errors is None
    assert report.output_tokens is None
    assert report.est_cost_usd is None
    assert len(report.worst_days) == 1
    assert report.worst_days[0].tool_errors is None
    assert report.worst_days[0].output_tokens is None
    assert "出力トークン: 不明" in format_excavate_report(report)
    assert "ツールエラー: 不明" in format_excavate_report(report)
