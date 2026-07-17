from datetime import date

import pytest

from kaizenlog.experiments import (
    ExperimentError,
    compute_metric,
    create_experiment,
    load_experiments,
    parse_target,
    record_measurement,
    render_experiments_context,
    target_met,
)
from kaizenlog.report import DailySummary

TODAY = date(2026, 7, 6)


def _summary(**kw):
    defaults = dict(
        day=TODAY, total_minutes=300.0,
        by_category={"開発": 200.0, "エンタメ": 45.0},
        by_app={}, blocks=[], ai_tool_minutes={}, ai_sessions=4, context_switches=18,
    )
    defaults.update(kw)
    return DailySummary(**defaults)


def _make(tmp_path, title="実験A", metric="context_switches", target="<= 15", days=7):
    return create_experiment(
        tmp_path, title=title, metric=metric, target=target,
        today=TODAY, deadline=TODAY.replace(day=TODAY.day + days), hypothesis="仮説",
    )


def test_parse_target():
    assert parse_target("<= 15") == ("<=", 15.0)
    assert parse_target(">=120") == (">=", 120.0)
    assert parse_target("= 3") == ("==", 3.0)
    with pytest.raises(ExperimentError):
        parse_target("about 15")


def test_target_met():
    assert target_met(10, "<=", 15)
    assert not target_met(20, "<=", 15)
    assert target_met(130, ">=", 120)


def test_target_met_float_tolerance():
    # 0.1+0.2 は 0.30000000000000004 だが実用上は 0.3 と同値として扱う
    assert target_met(0.1 + 0.2, "==", 0.3)
    assert target_met(0.1 + 0.2, "<=", 0.3)
    assert target_met(0.1 + 0.2, ">=", 0.3)
    assert not target_met(0.1 + 0.2, "<", 0.3)
    assert not target_met(0.1 + 0.2, ">", 0.3)
    assert not target_met(0.31, "==", 0.3)


def test_compute_metrics():
    s = _summary()
    assert compute_metric("context_switches", s, []) == 18.0
    assert compute_metric("total_active_minutes", s, []) == 300.0
    assert compute_metric("category_minutes:エンタメ", s, []) == 45.0
    assert compute_metric("category_minutes:存在しない", s, []) == 0.0
    assert compute_metric("ai_cc_sessions", s, []) == 0.0
    assert compute_metric("ai_avg_turns", s, []) == 0.0
    assert compute_metric("unknown_metric", s, []) is None


def test_create_and_load(tmp_path):
    path = _make(tmp_path)
    assert path.is_file()
    exps = load_experiments(tmp_path)
    assert len(exps) == 1
    e = exps[0]
    assert e.title == "実験A"
    assert e.status == "running"
    assert (e.target_op, e.target_value) == ("<=", 15.0)
    assert e.baseline is None
    assert e.measurements == {}


def test_create_duplicate_raises(tmp_path):
    _make(tmp_path)
    with pytest.raises(ExperimentError):
        _make(tmp_path)


def test_record_measurement_and_baseline(tmp_path):
    _make(tmp_path)
    e = load_experiments(tmp_path)[0]
    met = record_measurement(e, TODAY, 18.0)
    assert met is False  # 18 > 15

    e2 = load_experiments(tmp_path)[0]
    assert e2.baseline == 18.0  # 最初の実測値がbaselineになる
    assert e2.measurements == {TODAY: 18.0}
    content = e2.path.read_text(encoding="utf-8")
    assert "| 2026-07-06 | 18 | ❌ |" in content


def test_record_measurement_idempotent_and_sorted(tmp_path):
    _make(tmp_path)
    e = load_experiments(tmp_path)[0]
    record_measurement(e, date(2026, 7, 7), 14.0)
    record_measurement(e, date(2026, 7, 6), 18.0)
    record_measurement(e, date(2026, 7, 7), 12.0)  # 同日再実行→置換

    e2 = load_experiments(tmp_path)[0]
    assert e2.measurements == {date(2026, 7, 6): 18.0, date(2026, 7, 7): 12.0}
    content = e2.path.read_text(encoding="utf-8")
    assert content.count("2026-07-07") == 1
    assert content.index("2026-07-06") < content.index("2026-07-07")
    # baselineは最初に記録した値のまま
    assert e2.baseline == 14.0


def test_expired_after_deadline(tmp_path):
    _make(tmp_path, days=1)  # 期限 7/7
    e = load_experiments(tmp_path)[0]
    record_measurement(e, date(2026, 7, 8), 10.0)  # 期限翌日
    e2 = load_experiments(tmp_path)[0]
    assert e2.status == "expired"


def test_handwritten_note_preserved(tmp_path):
    path = _make(tmp_path)
    content = path.read_text(encoding="utf-8")
    path.write_text(content + "\n手書きの追記メモ\n", encoding="utf-8")
    e = load_experiments(tmp_path)[0]
    record_measurement(e, TODAY, 10.0)
    assert "手書きの追記メモ" in path.read_text(encoding="utf-8")


def test_render_context(tmp_path):
    _make(tmp_path)
    e = load_experiments(tmp_path)[0]
    record_measurement(e, TODAY, 18.0)
    ctx = render_experiments_context(load_experiments(tmp_path))
    assert "実験A" in ctx
    assert "context_switches <= 15" in ctx
    assert "07/06=18" in ctx


def test_render_context_excludes_finished(tmp_path):
    path = _make(tmp_path)
    content = path.read_text(encoding="utf-8").replace("status: running", "status: adopted")
    path.write_text(content, encoding="utf-8")
    assert render_experiments_context(load_experiments(tmp_path)) == ""


def test_non_experiment_notes_ignored(tmp_path):
    (tmp_path / "ただのメモ.md").write_text("---\ntitle: memo\n---\n本文", encoding="utf-8")
    (tmp_path / "壊れた実験.md").write_text(
        "---\nmetric: context_switches\ntarget: だいたい15\n---\n", encoding="utf-8")
    assert load_experiments(tmp_path) == []
