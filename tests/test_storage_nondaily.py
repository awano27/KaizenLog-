from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import threading
import re
from types import SimpleNamespace

import pytest

from kaizenlog.experiments import (
    ExperimentError,
    create_abtest,
    create_experiment,
    load_experiments,
    record_measurement,
    finish_abtest,
    load_abtests,
)
from kaizenlog.config import Config
from kaizenlog.cli import _resync_stats_activity_fingerprint
from kaizenlog.delight_views import write_monthly_album, write_weekly_story
from kaizenlog.vault import ACTIVITY_MARKER, DailyNoteStore
from kaizenlog.file_lock import file_lock, lock_path_for
from kaizenlog.memory import MemoryEntry, append_entries
from kaizenlog.monthly import write_monthly
from kaizenlog.prompt_export import export_prompt
from kaizenlog.promptledger import PromptLedgerEntry, append_prompt_ledger
from kaizenlog.report import DailySummary
from kaizenlog.runlog import load_runs, log_run
from kaizenlog.stats import write_stats
from kaizenlog.weekly_context import write_weekly_context, weekly_review_path


def _summary(day: date) -> DailySummary:
    return DailySummary(
        day=day,
        total_minutes=0.0,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
    )


def test_stats_write_waits_for_the_completed_json_lock(tmp_path: Path):
    day = date(2026, 10, 15)
    target = tmp_path / f"{day.isoformat()}.json"
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with file_lock(lock_path_for(target), timeout=1.0):
            entered.set()
            release.wait(1.0)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(1.0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(write_stats, tmp_path, day, _summary(day), [])
        assert not future.done()
        release.set()
        assert future.result() == target
    holder.join()


def test_runlog_concurrent_append_preserves_each_run(tmp_path: Path):
    def write(index: int) -> None:
        log_run(
            tmp_path,
            "synthetic",
            ok=True,
            duration_seconds=0.1,
            run_id=f"run-{index}",
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write, range(48)))

    assert {row["run_id"] for row in load_runs(tmp_path)} == {
        f"run-{index}" for index in range(48)
    }


def test_weekly_and_monthly_writers_use_their_completed_path_lock(tmp_path: Path):
    week_start = date(2026, 10, 12)
    weekly_target = weekly_review_path(tmp_path, week_start)
    monthly_target = tmp_path / "2026-10.md"

    def run_while_held(target: Path, writer):
        entered = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with file_lock(lock_path_for(target), timeout=1.0):
                entered.set()
                release.wait(1.0)

        holder = threading.Thread(target=hold)
        holder.start()
        assert entered.wait(1.0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            assert not future.done()
            release.set()
            result = future.result()
        holder.join()
        return result

    assert run_while_held(
        weekly_target, lambda: write_weekly_context(tmp_path, "weekly", week_start)
    ) == weekly_target
    assert run_while_held(
        monthly_target, lambda: write_monthly(tmp_path, 2026, 10, "monthly")
    ) == monthly_target


def test_experiment_creation_has_a_unique_save_guard(tmp_path: Path):
    def create() -> Path:
        return create_experiment(
            tmp_path,
            "same title",
            "total_minutes",
            ">= 1",
            date(2026, 10, 15),
            date(2026, 10, 28),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _attempt(create), range(2)))

    assert sum(result[0] for result in results) == 1
    assert sum(result[1] for result in results) == 1


def _attempt(function):
    try:
        function()
        return True, False
    except ExperimentError:
        return False, True


def test_jsonl_stores_create_their_completed_path_locks(tmp_path: Path):
    entry = MemoryEntry("KZN-20261015-001", "2026-10-15", "write")
    append_entries(tmp_path / "memory", [entry])
    append_prompt_ledger(
        tmp_path / "prompts",
        [
            PromptLedgerEntry(
                "PRM-20261015-001", "example", 1, 1, "2026-10-15", "2026-10-15"
            )
        ],
    )

    assert (tmp_path / "memory" / "suggestions.jsonl.lock").is_file()
    assert (tmp_path / "prompts" / "prompt_clusters.jsonl.lock").is_file()


def test_prompt_export_locks_the_exact_completed_path(tmp_path: Path):
    row = SimpleNamespace(
        skilled_before=2,
        skilled_after=1,
        skilled_pending=False,
        entry=SimpleNamespace(id="PRM-20261015-001", status="skilled"),
    )

    result = export_prompt(tmp_path, row, "synthetic request", write=True)

    assert result.status == "written"
    assert (result.path.parent / (result.path.name + ".lock")).is_file()


def test_weekly_lock_failure_is_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def fail_lock(*args, **kwargs):
        raise TimeoutError("synthetic lock timeout")

    monkeypatch.setattr("kaizenlog.file_lock.file_lock", fail_lock)

    with pytest.raises(TimeoutError, match="synthetic lock timeout"):
        write_weekly_context(tmp_path, "weekly", date(2026, 10, 12))


def test_measurement_reloads_baseline_from_current_frontmatter(tmp_path: Path):
    create_experiment(
        tmp_path,
        "baseline race",
        "total_minutes",
        ">= 1",
        date(2026, 10, 15),
        date(2026, 10, 28),
    )
    first = load_experiments(tmp_path)[0]
    second = load_experiments(tmp_path)[0]

    record_measurement(first, date(2026, 10, 15), 10.0)
    record_measurement(second, date(2026, 10, 16), 20.0)

    current = load_experiments(tmp_path)[0]
    assert current.baseline == 10.0
    assert current.measurements == {
        date(2026, 10, 15): 10.0,
        date(2026, 10, 16): 20.0,
    }


def test_measurement_keeps_expired_status_when_using_stale_object(tmp_path: Path):
    create_experiment(
        tmp_path,
        "status race",
        "total_minutes",
        ">= 1",
        date(2026, 10, 15),
        date(2026, 10, 15),
    )
    stale = load_experiments(tmp_path)[0]
    fresh = load_experiments(tmp_path)[0]

    record_measurement(fresh, date(2026, 10, 16), 10.0)
    assert load_experiments(tmp_path)[0].status == "expired"
    record_measurement(stale, date(2026, 10, 17), 20.0)

    assert load_experiments(tmp_path)[0].status == "expired"


def test_measurement_rejects_changed_metric_definition_without_writing(tmp_path: Path):
    create_experiment(
        tmp_path,
        "definition race",
        "total_minutes",
        ">= 1",
        date(2026, 10, 15),
        date(2026, 10, 28),
    )
    exp = load_experiments(tmp_path)[0]
    before = exp.path.read_bytes()
    exp.path.write_bytes(before.replace(b"metric: total_minutes", b"metric: ai_output_tokens"))
    changed = exp.path.read_bytes()

    with pytest.raises(ExperimentError, match="指標・目標定義"):
        record_measurement(exp, date(2026, 10, 15), 10.0)

    assert exp.path.read_bytes() == changed

def test_abtest_creation_waits_for_exact_completed_path_lock(tmp_path: Path):
    target = tmp_path / "ABTEST ABT-20261015-01.md"
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with file_lock(lock_path_for(target), timeout=1.0):
            entered.set()
            release.wait(1.0)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(1.0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            create_abtest, tmp_path, today=date(2026, 10, 15), predict_pct=5.0
        )
        assert not future.done()
        release.set()
        assert future.result() == target
    holder.join()


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_measurement_preserves_crlf_or_lf_and_unmanaged_bytes(
    tmp_path: Path, newline: bytes
):
    create_experiment(
        tmp_path,
        "byte measurement",
        "total_minutes",
        ">= 1",
        date(2026, 10, 15),
        date(2026, 10, 28),
    )
    exp = load_experiments(tmp_path)[0]
    original = exp.path.read_bytes().replace(b"\r\n", b"\n")
    suffix = (
        b"Handwritten Japanese  \n"
        b"<!-- unknown:start -->\nunknown  \n<!-- unknown:end -->\n"
    )
    exp.path.write_bytes(original.replace(b"\n", newline) + suffix.replace(b"\n", newline))

    record_measurement(exp, date(2026, 10, 16), 10.0)

    updated = exp.path.read_bytes()
    assert suffix.replace(b"\n", newline) in updated
    if newline == b"\r\n":
        assert re.search(rb"(?<!\r)\n", updated) is None
    assert b"baseline: 10" + newline in updated


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_abtest_finalize_preserves_crlf_or_lf_and_unmanaged_bytes(
    tmp_path: Path, newline: bytes
):
    create_abtest(tmp_path, today=date(2026, 10, 15), predict_pct=5.0)
    exp = load_abtests(tmp_path)[0]
    original = exp.path.read_bytes().replace(b"\r\n", b"\n")
    suffix = (
        b"Handwritten Japanese  \n"
        b"<!-- unknown:start -->\nunknown  \n<!-- unknown:end -->\n"
    )
    exp.path.write_bytes(original.replace(b"\n", newline) + suffix.replace(b"\n", newline))

    finish_abtest(
        exp,
        felt_pct=4.0,
        card_rel_path=None,
        measured_pct=3.0,
        invalid_reason=None,
        sample_ai=3,
        sample_non=3,
        as_of=date(2026, 10, 20),
    )

    updated = exp.path.read_bytes()
    assert suffix.replace(b"\n", newline) in updated
    if newline == b"\r\n":
        assert re.search(rb"(?<!\r)\n", updated) is None
    assert b"status: finished" + newline in updated


def test_cli_stats_fingerprint_waits_for_lock_and_preserves_live_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    day = date(2026, 10, 15)
    cfg = Config(vault_dir=tmp_path, daily_notes_dir="notes", stats_dir="stats")
    store = DailyNoteStore(cfg)
    store.write_section(day, ACTIVITY_MARKER, "## Activity\n外部本文  ")
    stats_path = cfg.stats_path / f"{day.isoformat()}.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text('{"day":"2026-10-15","live_field":"keep"}', encoding="utf-8")

    _assert_writer_waits_for_lock(
        stats_path,
        lambda: _resync_stats_activity_fingerprint(cfg, store, day),
        None,
        monkeypatch,
    )

    data = __import__("json").loads(stats_path.read_text(encoding="utf-8"))
    assert data["live_field"] == "keep"
    assert isinstance(data["activity_sha256"], str)


def test_delight_weekly_story_waits_for_completed_path_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = Config(vault_dir=tmp_path, daily_notes_dir="notes", stats_dir="stats", memory_dir="mem")
    week = date(2026, 10, 12)
    target = cfg.daily_notes_path / "Weekly Reviews" / "2026-W42.md"
    _assert_writer_waits_for_lock(
        target, lambda: write_weekly_story(cfg, week), target, monkeypatch
    )


def test_delight_monthly_album_waits_for_completed_path_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = Config(
        vault_dir=tmp_path,
        daily_notes_dir="notes",
        stats_dir="stats",
        memory_dir="mem",
        monthly_dir="monthly",
    )
    target = cfg.monthly_path / "2026-10.md"
    _assert_writer_waits_for_lock(
        target, lambda: write_monthly_album(cfg, 2026, 10), target, monkeypatch
    )


def _assert_writer_waits_for_lock(
    target: Path, writer, expected: Path | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_file_lock = __import__("kaizenlog.file_lock", fromlist=["file_lock"]).file_lock
    attempted = threading.Event()

    def traced_file_lock(path, *args, **kwargs):
        if Path(path).resolve() == lock_path_for(target).resolve():
            attempted.set()
        return original_file_lock(path, *args, **kwargs)

    monkeypatch.setattr("kaizenlog.file_lock.file_lock", traced_file_lock)
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with file_lock(lock_path_for(target), timeout=1.0):
            entered.set()
            release.wait(1.0)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(1.0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        assert attempted.wait(1.0)
        assert not future.done()
        release.set()
        result = future.result()
        if expected is not None:
            assert result == expected
    holder.join()
