"""週次コンテキストの時点境界を合成データで守る。"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from kaizenlog.decay import _recent_ref_ids, load_decay_events
from kaizenlog.weekly_context import render_weekly_context


WEEK_START = date(2026, 7, 20)
WEEK_END = WEEK_START + timedelta(days=6)


def _write_week_stats(stats_dir: Path) -> None:
    for offset in range(7):
        day = WEEK_START + timedelta(days=offset)
        (stats_dir / f"{day.isoformat()}.json").write_text(
            json.dumps(
                {
                    "day": day.isoformat(),
                    "total_minutes": 10,
                    "by_category": {},
                    "ai": {},
                }
            ),
            encoding="utf-8",
        )


def _write_experiment(
    experiments_dir: Path,
    *,
    filename: str,
    title: str,
    status: str,
    start: date,
    measurements: list[tuple[date, float]],
) -> None:
    measurement_rows = "".join(
        f"| {day.isoformat()} | {value:g} |\n" for day, value in measurements
    )
    (experiments_dir / filename).write_text(
        "---\n"
        f"title: {title}\n"
        f"date: {start.isoformat()}\n"
        "tags: [type/kaizen-experiment]\n"
        f"status: {status}\n"
        "metric: context_switches\n"
        "target: <= 10\n"
        "baseline: 20\n"
        "deadline: 2026-07-26\n"
        "---\n\n"
        "<!-- kaizenlog:measurements:start -->\n"
        "| 日付 | 実測 |\n| --- | ---: |\n"
        f"{measurement_rows}"
        "<!-- kaizenlog:measurements:end -->\n",
        encoding="utf-8",
    )


def test_weekly_experiment_numbers_are_immutable_after_week_end(tmp_path: Path):
    stats_dir = tmp_path / "stats"
    memory_dir = tmp_path / "memory"
    experiments_dir = tmp_path / "experiments"
    stats_dir.mkdir()
    memory_dir.mkdir()
    experiments_dir.mkdir()
    _write_week_stats(stats_dir)
    _write_experiment(
        experiments_dir,
        filename="historical.md",
        title="Historical experiment",
        status="expired",
        start=WEEK_START,
        measurements=[(date(2026, 7, 21), 9), (date(2026, 8, 10), 99)],
    )
    _write_experiment(
        experiments_dir,
        filename="running.md",
        title="Still running",
        status="running",
        start=WEEK_START,
        measurements=[(date(2026, 7, 21), 9), (date(2026, 8, 10), 99)],
    )

    text = render_weekly_context(stats_dir, memory_dir, experiments_dir, WEEK_START)

    line = next(
        line
        for line in text.splitlines()
        if 'expired 「Historical experiment」' in line
    )
    assert "達成率 1/1（100%）" in line
    assert "効果量 -55%" in line
    assert "99" not in line
    running_line = next(
        line for line in text.splitlines() if 'running 「Still running」' in line
    )
    assert "直近 2026-07-21 = 9" in running_line
    assert "効果量 -55%" in running_line
    assert "2026-08-10" not in running_line
    assert "週末までの計測 / 現在のstatus" in text


def test_weekly_excludes_experiments_created_after_target_week(tmp_path: Path):
    stats_dir = tmp_path / "stats"
    memory_dir = tmp_path / "memory"
    experiments_dir = tmp_path / "experiments"
    stats_dir.mkdir()
    memory_dir.mkdir()
    experiments_dir.mkdir()
    _write_week_stats(stats_dir)
    _write_experiment(
        experiments_dir,
        filename="later.md",
        title="Created after this week",
        status="adopted",
        start=date(2026, 8, 1),
        measurements=[
            (date(2026, 7, 20), 99),
            (date(2026, 7, 21), 99),
            (date(2026, 7, 22), 99),
        ],
    )

    text = render_weekly_context(stats_dir, memory_dir, experiments_dir, WEEK_START)

    assert "Created after this week" not in text
    assert "退行" not in text


def test_weekly_current_state_sections_and_friction_limit_are_labeled(
    tmp_path: Path,
):
    stats_dir = tmp_path / "stats"
    memory_dir = tmp_path / "memory"
    experiments_dir = tmp_path / "experiments"
    stats_dir.mkdir()
    memory_dir.mkdir()
    experiments_dir.mkdir()
    _write_week_stats(stats_dir)

    text = render_weekly_context(stats_dir, memory_dir, experiments_dir, WEEK_START)

    assert "観測された摩擦のみ" in text
    assert "入力/出力の品質は直接証拠がないため Unknown" in text
    assert "対象週の提案 / 現在の状態" in text
    assert "現在の台帳状態" in text


def test_decay_as_of_has_an_upper_bound_and_cooldown_uses_it(tmp_path: Path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "decay_ledger.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "date": "2026-07-20",
                        "kind": "kzn",
                        "ref_id": "old",
                        "detail": "old event",
                        "evidence": "",
                    }
                ),
                json.dumps(
                    {
                        "date": "2026-08-10",
                        "kind": "kzn",
                        "ref_id": "future",
                        "detail": "future event",
                        "evidence": "",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    events = load_decay_events(memory_dir, as_of=WEEK_END)

    assert [event.ref_id for event in events] == ["old"]
    assert [event.ref_id for event in load_decay_events(memory_dir)] == [
        "old",
        "future",
    ]
    assert _recent_ref_ids(memory_dir, WEEK_END, cooldown_days=30) == {"old"}
