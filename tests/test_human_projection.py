from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from kaizenlog.config import Config
from kaizenlog.human_input import (
    HumanOperation,
    apply_human_operation,
    load_human_day,
    project_human_day,
    human_overlay_lines,
    stable_target_key,
)
from kaizenlog.note_paths import DailyNoteResolution
from kaizenlog.vault import HUMAN_INPUT_MARKER, extract_section


DAY = date(2026, 10, 15)


def _cfg(tmp_path: Path) -> Config:
    return Config(vault_dir=tmp_path, daily_notes_dir="Daily", memory_dir="Memory")


def _op(operation_id: str = "op-1", text: str = "成果を記録") -> HumanOperation:
    return HumanOperation(
        operation_id=operation_id,
        target_day=DAY,
        kind="outcome",
        payload={"text": text},
        target_key=None,
        cancel_operation_id=None,
    )


def _note(cfg: Config, body: str = "# 手書き\n\n") -> Path:
    path = cfg.daily_notes_path / f"{DAY.isoformat()}.md"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8", newline="")
    return path


def test_projection_commit_order_and_recovery(tmp_path: Path):
    cfg = _cfg(tmp_path)
    note = _note(cfg)
    saved = apply_human_operation(cfg, DAY, _op(), expected_revision=0)
    assert saved.status == "saved"

    first = project_human_day(cfg, DAY, expected_revision=1)
    assert first.status == "applied"
    assert first.projection_digest
    before_mtime = note.stat().st_mtime_ns
    second = project_human_day(cfg, DAY, expected_revision=1)
    assert second.status == "already_applied"
    assert note.stat().st_mtime_ns == before_mtime
    assert extract_section(note.read_text(encoding="utf-8"), HUMAN_INPUT_MARKER)
    assert load_human_day(cfg, DAY).projection_state == "applied"


def test_projection_external_edit_conflicts(tmp_path: Path):
    cfg = _cfg(tmp_path)
    note = _note(cfg)
    apply_human_operation(cfg, DAY, _op(), expected_revision=0)
    assert project_human_day(cfg, DAY, expected_revision=1).status == "applied"
    note.write_text(note.read_text(encoding="utf-8").replace("成果を記録", "外部変更"), encoding="utf-8")
    apply_human_operation(cfg, DAY, _op("op-2", "次の成果"), expected_revision=1)
    result = project_human_day(cfg, DAY, expected_revision=2)
    assert result.status == "conflict"
    assert "外部" in note.read_text(encoding="utf-8")
    assert load_human_day(cfg, DAY).projection_state == "pending"


def test_revision_two_recovery_compares_desired_bytes_before_old_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = _cfg(tmp_path)
    note = _note(cfg)
    apply_human_operation(cfg, DAY, _op("op-1", "成果1"), expected_revision=0)
    assert project_human_day(cfg, DAY, expected_revision=1).status == "applied"
    apply_human_operation(cfg, DAY, _op("op-2", "成果2"), expected_revision=1)

    monkeypatch.setattr("kaizenlog.human_input.atomic_write_text", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("metadata")))
    assert project_human_day(cfg, DAY, expected_revision=2).status == "partial_failure"
    monkeypatch.undo()
    before = note.read_bytes()
    assert project_human_day(cfg, DAY, expected_revision=2).status == "already_applied"
    assert note.read_bytes() == before


def test_projection_rejects_resolver_change_while_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = _cfg(tmp_path)
    note = _note(cfg)
    apply_human_operation(cfg, DAY, _op(), expected_revision=0)
    original = __import__("kaizenlog.note_paths", fromlist=["resolve_daily_note"]).resolve_daily_note(cfg, DAY)
    alternate_path = cfg.daily_notes_path / "alternate.md"
    alternate = DailyNoteResolution(
        DAY,
        alternate_path,
        "selected",
        "override",
        (),
        None,
    )
    calls = iter((original, original, alternate))
    monkeypatch.setattr("kaizenlog.note_paths.resolve_daily_note", lambda *args: next(calls))
    before = note.read_bytes()
    result = project_human_day(cfg, DAY, expected_revision=1)
    assert result.status == "conflict"
    assert note.read_bytes() == before
    assert load_human_day(cfg, DAY).projection_state == "pending"


@pytest.mark.parametrize(
    "marker_text",
    [
        "<!-- kaizenlog:human-input:start -->\nfirst\n<!-- kaizenlog:human-input:start -->\nsecond\n<!-- kaizenlog:human-input:end -->",
        "<!-- kaizenlog:human-input:start -->\nfirst\n<!-- kaizenlog:human-input:end -->\n<!-- kaizenlog:human-input:end -->",
    ],
)
def test_projection_malformed_or_duplicate_markers_are_conflict(tmp_path: Path, marker_text: str):
    cfg = _cfg(tmp_path)
    note = _note(cfg, marker_text + "\n")
    apply_human_operation(cfg, DAY, _op(), expected_revision=0)
    before = note.read_bytes()
    result = project_human_day(cfg, DAY, expected_revision=1)
    assert result.status == "conflict"
    assert note.read_bytes() == before
    assert load_human_day(cfg, DAY).projection_state == "pending"


def test_projection_missing_note_stays_pending(tmp_path: Path):
    cfg = _cfg(tmp_path)
    apply_human_operation(cfg, DAY, _op(), expected_revision=0)
    result = project_human_day(cfg, DAY, expected_revision=1)
    assert result.status == "pending"
    assert result.note_path == cfg.daily_notes_path / f"{DAY.isoformat()}.md"
    assert not result.note_path.exists()
    assert load_human_day(cfg, DAY).projection_state == "pending"


def test_projection_management_failure_is_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = _cfg(tmp_path)
    note = _note(cfg)
    apply_human_operation(cfg, DAY, _op(), expected_revision=0)

    def fail_management(*args, **kwargs):
        raise OSError("metadata disk full")

    monkeypatch.setattr("kaizenlog.human_input.atomic_write_text", fail_management)
    result = project_human_day(cfg, DAY, expected_revision=1)
    assert result.status == "partial_failure"
    assert "metadata" in (result.reason or "")
    assert HUMAN_INPUT_MARKER in note.read_text(encoding="utf-8")
    assert load_human_day(cfg, DAY).projection_state == "pending"

    monkeypatch.undo()
    before_mtime = note.stat().st_mtime_ns
    recovered = project_human_day(cfg, DAY, expected_revision=1)
    assert recovered.status == "already_applied"
    assert note.stat().st_mtime_ns == before_mtime


def test_projection_arbitrary_marker_text_is_escaped(tmp_path: Path):
    cfg = _cfg(tmp_path)
    note = _note(cfg)
    text = "literal <!-- kaizenlog:human-input:start --> token"
    apply_human_operation(cfg, DAY, _op(text=text), expected_revision=0)
    result = project_human_day(cfg, DAY, expected_revision=1)
    assert result.status == "applied"
    content = note.read_text(encoding="utf-8")
    assert content.count("<!-- kaizenlog:human-input:start -->") == 1
    assert "&lt;!-- kaizenlog:human-input:start --&gt;" in content


def test_correction_overlay_shows_original_manual_and_stale_target(tmp_path: Path):
    cfg = _cfg(tmp_path)
    stats_path = cfg.stats_path / f"{DAY.isoformat()}.json"
    stats_path.parent.mkdir(parents=True)
    source = {
        "day": DAY.isoformat(),
        "blocks": [{
            "start": "2026-10-15T00:00:00+00:00",
            "end": "2026-10-15T01:00:00+00:00",
            "app": "Code.exe",
            "title": "main.py",
            "category": "開発",
            "project": None,
        }],
    }
    stats_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    key = stable_target_key(
        __import__("datetime").datetime.fromisoformat("2026-10-15T00:00:00+00:00"),
        __import__("datetime").datetime.fromisoformat("2026-10-15T01:00:00+00:00"),
        "Code.exe",
        "main.py",
    )
    operation = HumanOperation("correction-1", DAY, "classification_correction", {"category": "集中"}, key, None)
    assert apply_human_operation(cfg, DAY, operation, expected_revision=0).status == "saved"
    human = load_human_day(cfg, DAY)
    lines = human_overlay_lines(human, source_stats=source)
    assert any("開発" in line and "集中" in line and "Code.exe" in line for line in lines)
    source["blocks"][0]["title"] = "other.py"
    stale = human_overlay_lines(human, source_stats=source)
    assert any("stale" in line and "未適用" in line for line in stale)


@pytest.mark.parametrize(
    "end",
    [
        "2026-10-15T00:00:00+00:00",
        "2026-10-14T23:00:00+00:00",
    ],
)
def test_contradictory_current_block_keeps_saved_correction_unconfirmed_and_totals(
    tmp_path: Path, end: str
):
    from datetime import timezone
    from kaizenlog.nippou import generate_nippou_deterministic

    cfg = _cfg(tmp_path)
    source = {
        "day": DAY.isoformat(),
        "blocks": [{
            "start": "2026-10-15T00:00:00+00:00",
            "end": "2026-10-15T01:00:00+00:00",
            "app": "Code.exe",
            "title": "main.py",
            "category": "開発",
            "project": None,
        }],
        "effort": {"minutes": {"開発": 60}, "total_minutes": 60},
    }
    stats_path = cfg.stats_path / f"{DAY.isoformat()}.json"
    stats_path.parent.mkdir(parents=True)
    stats_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    start = __import__("datetime").datetime.fromisoformat(source["blocks"][0]["start"])
    valid_end = __import__("datetime").datetime.fromisoformat(source["blocks"][0]["end"])
    key = stable_target_key(start, valid_end, "Code.exe", "main.py")
    operation = HumanOperation(
        "correction-contradictory",
        DAY,
        "classification_correction",
        {"category": "集中"},
        key,
        None,
    )
    assert apply_human_operation(cfg, DAY, operation, expected_revision=0).status == "saved"
    human = load_human_day(cfg, DAY)

    source["blocks"][0]["end"] = end
    lines = human_overlay_lines(human, source_stats=source)
    assert any("未確認 / 未適用" in line and "集中" in line for line in lines)
    daily = generate_nippou_deterministic(source, timezone.utc, human_day=human)
    assert "未確認 / 未適用" in daily
    assert "1時間0分" in daily


def test_overlay_uses_latest_active_and_cancel_restores_previous(tmp_path: Path):
    cfg = _cfg(tmp_path)
    apply_human_operation(cfg, DAY, _op("outcome-1", "最初の成果"), expected_revision=0)
    apply_human_operation(cfg, DAY, _op("outcome-2", "更新した成果"), expected_revision=1)
    cancel = HumanOperation("cancel-2", DAY, "cancel", {}, None, "outcome-2")
    assert apply_human_operation(cfg, DAY, cancel, expected_revision=2).status == "saved"
    human = load_human_day(cfg, DAY)
    lines = human_overlay_lines(human)
    assert any("最初の成果" in line for line in lines)
    assert not any("更新した成果" in line for line in lines)
    assert not any("cancel" in line.lower() for line in lines)


def test_wrong_day_stats_never_validates_same_key_across_views(tmp_path: Path):
    from datetime import datetime, timezone
    from kaizenlog.monthly import MonthlyReport, render_monthly_markdown
    from kaizenlog.nippou import generate_nippou_deterministic

    cfg = _cfg(tmp_path)
    block = {
        "start": "2026-10-15T00:00:00+00:00",
        "end": "2026-10-15T01:00:00+00:00",
        "app": "Code.exe",
        "title": "main.py",
        "category": "開発",
        "project": None,
    }
    key = stable_target_key(
        datetime.fromisoformat(block["start"]),
        datetime.fromisoformat(block["end"]),
        block["app"],
        block["title"],
    )
    operation = HumanOperation(
        "wrong-day-correction", DAY, "classification_correction", {"category": "集中"}, key, None
    )
    stats_path = cfg.stats_path / f"{DAY.isoformat()}.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(json.dumps({"day": DAY.isoformat(), "blocks": [block]}, ensure_ascii=False), encoding="utf-8")
    assert apply_human_operation(cfg, DAY, operation, expected_revision=0).status == "saved"
    human = load_human_day(cfg, DAY)
    wrong_day_stats = {"day": "2026-10-16", "blocks": [block]}

    pure_lines = human_overlay_lines(human, source_stats=wrong_day_stats)
    assert any("未確認 / 未適用" in line for line in pure_lines)
    nippou = generate_nippou_deterministic(
        {
            "day": "2026-10-16",
            "blocks": [block],
            "effort": {"minutes": {"開発": 60}, "total_minutes": 60},
        },
        timezone.utc,
        human_day=human,
    )
    assert "未確認 / 未適用" in nippou
    rep = MonthlyReport(2026, 10, work_days=1, total_minutes=60, project_minutes={"開発": 60})
    monthly = render_monthly_markdown(
        rep,
        human_overlays={DAY.isoformat(): human},
        source_stats_by_day={DAY.isoformat(): wrong_day_stats},
    )
    assert "未確認 / 未適用" in monthly

    note = _note(cfg)
    stats_path.write_text(json.dumps(wrong_day_stats, ensure_ascii=False), encoding="utf-8")
    before_stats = stats_path.read_bytes()
    before_stats_mtime = stats_path.stat().st_mtime_ns
    projected = project_human_day(cfg, DAY, expected_revision=1)
    assert projected.status == "applied"
    assert "未確認 / 未適用" in note.read_text(encoding="utf-8")
    assert stats_path.read_bytes() == before_stats
    assert stats_path.stat().st_mtime_ns == before_stats_mtime


def test_display_keeps_measured_totals(tmp_path: Path):
    from datetime import timezone
    from kaizenlog.monthly import MonthlyReport, render_monthly_markdown
    from kaizenlog.nippou import generate_nippou_deterministic

    cfg = _cfg(tmp_path)
    apply_human_operation(cfg, DAY, _op(), expected_revision=0)
    human = load_human_day(cfg, DAY)
    rep = MonthlyReport(2026, 10, work_days=1, total_minutes=60, project_minutes={"開発": 60}, project_days={"開発": {DAY.isoformat()}})
    monthly = render_monthly_markdown(rep, human_overlays={DAY.isoformat(): human})
    assert "合計 1h00m" in monthly
    assert "本人入力" in monthly
    daily = generate_nippou_deterministic({"day": DAY.isoformat(), "effort": {"total_minutes": 60, "minutes": {"開発": 60}}}, timezone.utc, human_day=human)
    assert "本人入力" in daily
    assert "1時間0分" in daily
