from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from kaizenlog.cli import cmd_today
from kaizenlog.config import Config
from kaizenlog.memory import (
    MemoryEntry,
    _metric_scope_note,
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


def test_today_default_excludes_confirmed_pass_but_all_keeps_it(tmp_path, capsys):
    cfg = _config_with_entries(tmp_path, [_active_entry(), _achieved_entry()])
    day = date(2026, 8, 3)

    assert cmd_today(cfg, day, no_sync=True) == 0
    default_out = capsys.readouterr().out
    assert "今日の候補 1件" in default_out
    assert "KZN-20260802-001" in default_out
    assert "KZN-20260727-002" not in default_out
    assert "効果モニタリング 1件" in default_out

    assert cmd_today(cfg, day, no_sync=True, show_all=True) == 0
    all_out = capsys.readouterr().out
    assert "KZN-20260727-002" in all_out


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


def test_actions_render_action_monitor_goal_as_separate_blocks():
    active, achieved = _active_and_achieved_entries()
    history = _realistic_history()

    out = render_actions_section(
        [active, achieved],
        date(2026, 8, 3),
        note_content="# note without goal marker\n",
        stats_history=history,
    )

    assert out is not None
    assert "## 📌 今日やること（1件）" in out
    assert "- [ ] KZN-20260802-001" in out
    assert "- [ ] KZN-20260727-002" not in out
    assert "## 📈 効果モニタリング（今日やることではない）" in out
    assert "- KZN-20260727-002" in out
    assert "最新: 8/2 3.2 ✅" in out
    assert "直近5日: 4/5達成・未達1日（目標 >= 2.5）" in out
    assert "指標が戻っています" not in out
    assert "閾値超過" not in out
    assert "## 🎯 日次目標" in out
    assert '未設定: `kaizenlog goal "今日達成したい成果"`' in out


def test_action_keeps_effect_target_when_denominator_is_short():
    out = render_actions_section(
        [_active_entry()],
        date(2026, 8, 3),
        stats_history=[
            {
                "day": "2026-08-03",
                "total_minutes": 22.7,
                "context_switches": 25,
            }
        ],
    )

    assert out is not None
    assert "効果目標:" in out
    assert "65 以下" in out
    assert "測定: 未判定（集計待ち" in out
    assert "稼働22.7分" in out
    assert "分母不足" in out


def test_session_denominator_shortfall_is_explicitly_unknown():
    entry = MemoryEntry(
        id="KZN-20260802-002",
        date="2026-08-02",
        action=(
            "開始前→エラーを確認する"
            "｜PASS: ai_tool_errors_per_session <= 1"
            "｜FAIL: ai_tool_errors_per_session > 1"
        ),
    )

    out = render_actions_section(
        [entry],
        date(2026, 8, 3),
        stats_history=[
            {
                "day": "2026-08-03",
                "ai": {"tool_errors": 2, "sessions": 0},
            }
        ],
    )

    assert out is not None
    assert "測定: 未判定（集計待ち" in out
    assert "AIセッション0件/必要1件" in out


def test_monitoring_keeps_two_renderable_cards_after_unmeasurable_newest():
    entries = [
        MemoryEntry(
            id="KZN-20260802-001",
            date="2026-08-02",
            action="x｜PASS: ai_avg_turns >= 2.5｜FAIL: ai_avg_turns < 2.5",
            verdict="pass",
            verdict_date="2026-08-02",
            verdict_stage="confirmed",
        ),
        MemoryEntry(
            id="KZN-20260801-001",
            date="2026-08-01",
            action="x｜PASS: ai_avg_turns >= 2.5｜FAIL: ai_avg_turns < 2.5",
            verdict="pass",
            verdict_date="2026-07-31",
            verdict_stage="confirmed",
        ),
        MemoryEntry(
            id="KZN-20260731-001",
            date="2026-07-31",
            action="x｜PASS: ai_avg_turns >= 2.5｜FAIL: ai_avg_turns < 2.5",
            verdict="pass",
            verdict_date="2026-07-30",
            verdict_stage="confirmed",
        ),
    ]
    history = [
        {"day": "2026-08-01", "ai": {"avg_turns": 3.0, "sessions": 1}},
        {"day": "2026-08-02", "ai": {"avg_turns": 3.0, "sessions": 1}},
    ]

    out = render_actions_section(entries, date(2026, 8, 3), stats_history=history)

    assert out is not None
    assert "- KZN-20260801-001" in out
    assert "- KZN-20260731-001" in out
    assert "- KZN-20260802-001" not in out
    assert "ほか効果モニタリング 1件" in out


def test_metric_scope_note_omits_boolean_session_count():
    scope = _metric_scope_note("ai_avg_turns", {"ai": {"sessions": True}})

    assert scope is not None
    assert "セッション" not in scope


@pytest.mark.parametrize("sessions", [float("nan"), float("inf"), float("-inf")])
def test_metric_scope_note_omits_nonfinite_session_count(sessions):
    scope = _metric_scope_note("ai_avg_turns", {"ai": {"sessions": sessions}})

    assert scope is not None
    assert "セッション" not in scope


def test_monitor_warns_only_when_latest_observation_fails():
    out = render_actions_section(
        [_achieved_entry()],
        date(2026, 8, 3),
        stats_history=_history_with_latest(1.5),
    )

    assert out is not None
    assert "⚠ 最新観測が目標未達です" in out


@pytest.mark.parametrize(
    ("goal_section", "expected"),
    [
        (None, '未設定: `kaizenlog goal "今日達成したい成果"`'),
        ("🎯 今日の目標: 実装を終える", "達成度: 未入力"),
        (
            "🎯 今日の目標: 実装を終える\n達成度: 80%（自己申告）",
            "達成度: 80%（自己申告）",
        ),
    ],
)
def test_goal_monitoring_states(goal_section, expected):
    note = "# day\n"
    if goal_section is not None:
        note = upsert_section(note, GOAL_MARKER, goal_section)

    out = render_actions_section([_active_entry()], date(2026, 8, 3), note)

    assert out is not None
    assert expected in out
