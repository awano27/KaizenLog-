import json
from datetime import date

import pytest

from kaizenlog.cli import _is_valid_date
from kaizenlog.memory import MemoryEntry, append_entries
from kaizenlog.decay import load_decay_events
from kaizenlog.experiments import load_experiments, _effect_size_from_values
from kaizenlog.weekly_context import render_weekly_context
from tests.test_skill_weekly_boundaries import WEEK_START, _write_week_stats, _write_experiment


@pytest.mark.parametrize("bad", [20260721, None, "2026-07-21T00:00:00", "20260721", "2026-07-99"])
def test_weekly_ignores_invalid_action_dates(tmp_path, bad):
    stats = tmp_path / "stats"
    stats.mkdir()
    _write_week_stats(stats)
    memory = tmp_path / "memory"
    append_entries(memory, [MemoryEntry("KZN-20260721-001", bad, "invalid")])
    actual = render_weekly_context(stats, memory, tmp_path / "experiments", WEEK_START)
    expected = render_weekly_context(stats, tmp_path / "empty", tmp_path / "experiments", WEEK_START)
    assert actual == expected
    assert not _is_valid_date(bad)


@pytest.mark.parametrize("bounds", [{"window_days": 7, "as_of": date(2026, 7, 26)}, {"as_of": date(2026, 7, 26)}])
def test_dated_decay_window_excludes_undated_and_invalid_events(tmp_path, bounds):
    dates = [None, "", 20260721, "2026-07-21T00:00:00", "2026-07-99", "2026-07-21"]
    (tmp_path / "decay_ledger.jsonl").write_text("\n".join(json.dumps({"ref_id": str(i), "date": d}) for i, d in enumerate(dates)), encoding="utf-8")
    assert [e.ref_id for e in load_decay_events(tmp_path, **bounds)] == ["5"]


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_nonfinite_baseline_is_unknown(tmp_path, value):
    _write_experiment(tmp_path, filename="experiment.md", title="finite", status="running", start=WEEK_START, measurements=[(WEEK_START, 10)])
    p = tmp_path / "experiment.md"
    p.write_text(p.read_text(encoding="utf-8").replace("baseline: 20", f"baseline: {value}"), encoding="utf-8")
    assert load_experiments(tmp_path)[0].baseline is None
    assert _effect_size_from_values(float(value), [10]) is None
    assert _effect_size_from_values(20, [float(value)]) is None
    assert _effect_size_from_values(20, [10]) == -50.0
