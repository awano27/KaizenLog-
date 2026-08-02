from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from kaizenlog.cli import cmd_today
from kaizenlog.config import Config
from kaizenlog.memory import (
    MemoryEntry,
    _post_verdict_trajectory,
    append_entries,
    render_actions_section,
    split_action_candidates,
)
from kaizenlog.vault import GOAL_MARKER, upsert_section


def _active_entry() -> MemoryEntry:
    return MemoryEntry(
        id="KZN-20260802-001",
        date="2026-08-02",
        action=(
            "午前と午後のアラームが鳴ったとき→30分タイマーをかけ、"
            "その時点で使っているカテゴリのアプリ以外を最小化する"
            "｜PASS: context_switches_per_hour <= 65"
            "（1時間あたりのカテゴリ変更回数）"
            "｜FAIL: context_switches_per_hour > 65"
        ),
    )


def _achieved_entry() -> MemoryEntry:
    return MemoryEntry(
        id="KZN-20260727-002",
        date="2026-07-27",
        action=(
            "codexセッション起動前→期待成果物を書き、終了後にgit logで確認する"
            "｜PASS: ai_avg_turns >= 2.5"
            "（Claude Codeセッションの平均往復数）"
            "｜FAIL: ai_avg_turns < 2.5"
        ),
        verdict="pass",
        verdict_value=3.4,
        verdict_date="2026-07-28",
        verdict_stage="confirmed",
    )


def _active_and_achieved_entries() -> tuple[MemoryEntry, MemoryEntry]:
    return _active_entry(), _achieved_entry()


def _realistic_history(*, latest_ai_turns: float = 3.2) -> list[dict]:
    days = ["2026-07-29", "2026-07-30", "2026-07-31", "2026-08-01", "2026-08-02"]
    values = [6.1, 12.3, 1.5, 13.3, latest_ai_turns]
    history = [
        {
            "day": day,
            "total_minutes": 120.0,
            "context_switches": 20,
            "ai": {"avg_turns": value, "sessions": 22},
        }
        for day, value in zip(days, values)
    ]
    history.append(
        {
            "day": "2026-08-03",
            "total_minutes": 22.7,
            "context_switches": 25,
            "ai": {"avg_turns": 1.0, "sessions": 1},
        }
    )
    return history


def _history_with_latest(value: float) -> list[dict]:
    return _realistic_history(latest_ai_turns=value)


def _config_with_entries(tmp_path: Path, entries: list[MemoryEntry]) -> Config:
    vault = tmp_path / "vault"
    (vault / "01 Daily Notes").mkdir(parents=True)
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / ".kaizenlog" / "logs").mkdir(parents=True)
    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        memory_dir="Kaizen/Memory",
        stats_dir=".kaizenlog/stats",
        logs_dir=".kaizenlog/logs",
        actions_position="top",
    )
    append_entries(cfg.memory_path, entries)
    return cfg


def test_confirmed_pass_is_monitoring_not_action_candidate():
    active = MemoryEntry(
        id="KZN-20260802-001",
        date="2026-08-02",
        action="alarm→minimize｜PASS: context_switches_per_hour <= 65｜FAIL: context_switches_per_hour > 65",
    )
    achieved = MemoryEntry(
        id="KZN-20260727-002",
        date="2026-07-27",
        action="note→verify｜PASS: ai_avg_turns >= 2.5｜FAIL: ai_avg_turns < 2.5",
        verdict="pass",
        verdict_date="2026-07-28",
        verdict_stage="confirmed",
    )

    actionable, monitoring = split_action_candidates([active, achieved], set())

    assert [e.id for e in actionable] == [active.id]
    assert [e.id for e in monitoring] == [achieved.id]

    checked_actionable, checked_monitoring = split_action_candidates(
        [active, achieved], {achieved.id}
    )
    assert [e.id for e in checked_actionable] == [active.id]
    assert checked_monitoring == []


def test_post_verdict_trajectory_keeps_operator_and_latest_state():
    entry = MemoryEntry(
        id="KZN-20260727-002",
        date="2026-07-27",
        action="note→verify｜PASS: ai_avg_turns >= 2.5｜FAIL: ai_avg_turns < 2.5",
        verdict="pass",
        verdict_date="2026-07-28",
        verdict_stage="confirmed",
    )
    values = [6.1, 12.3, 1.5, 13.3, 3.2]
    stats = {
        f"2026-{month_day}": {"day": f"2026-{month_day}", "ai": {"avg_turns": value, "sessions": 22}}
        for month_day, value in zip(
            ["07-29", "07-30", "07-31", "08-01", "08-02"], values
        )
    }

    trajectory = _post_verdict_trajectory(entry, date(2026, 8, 3), stats)

    assert trajectory is not None
    assert (trajectory.metric, trajectory.op, trajectory.target) == (
        "ai_avg_turns",
        ">=",
        2.5,
    )
    assert [point.met for point in trajectory.observations] == [True, True, False, True, True]
    assert trajectory.observations[-1].value == 3.2
