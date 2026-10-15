from __future__ import annotations

from concurrent.futures import Future
from datetime import date
from pathlib import Path
from typing import Callable
from types import SimpleNamespace

import pytest
import kaizenlog.home_gui as home_gui

from kaizenlog.config import Config
from kaizenlog.home_service import (
    HomeSnapshot,
    HumanSaveResult,
    KznCompletionResult,
    LiveMeasurementSnapshot,
    RecoverySnapshot,
    SavedDaySnapshot,
    NextActionView,
)
from kaizenlog.human_input import HumanDay, HumanOperation
from kaizenlog.home_gui import ControllerResult, HomeController, HomeView, ResultQueue, main
from kaizenlog.journal_service import SetupSaveResult


DAY = date(2026, 10, 15)


def _snapshot(day: date = DAY) -> HomeSnapshot:
    return HomeSnapshot(
        day=day,
        config_status="ready",
        saved=SavedDaySnapshot("missing", None, None, None, "missing"),
        stats_status="missing",
        human=HumanDay(day, Path("human.json"), "missing", 0, (), "none", None, None, None),
        next_actions=(NextActionView("kzn", "KZN-20261015-001", "digest", "候補", True, True, "available"),),
        measurements=LiveMeasurementSnapshot({}, "unknown", None, "activitywatch", "未更新"),
        recovery=RecoverySnapshot("none", None, None, None, None),
    )


class FakeQueue(ResultQueue):
    pass


class FakeScheduler:
    def __init__(self) -> None:
        self.callbacks: list[Callable[[], None]] = []
        self.cancelled: list[object] = []

    def after(self, delay_ms: int, callback: Callable[[], None]) -> object:
        self.callbacks.append(callback)
        return callback

    def after_cancel(self, token: object) -> None:
        self.cancelled.append(token)

    def run_once(self) -> None:
        callback = self.callbacks.pop(0)
        callback()


class FakeRoot:
    def __init__(self) -> None:
        self.destroyed = False

    def after(self, delay_ms: int, callback: Callable[[], None]) -> object:
        return callback

    def after_cancel(self, token: object) -> None:
        pass

    def mainloop(self) -> None:
        pass

    def destroy(self) -> None:
        self.destroyed = True


class BootstrapRoot:
    instances: list["BootstrapRoot"] = []
    setup_result_path: Path | None = None

    def __init__(self) -> None:
        self.callbacks: list[tuple[int, Callable[[], None]]] = []
        self.destroyed = False
        self.destroy_count = 0
        self.protocols: dict[str, Callable[[], None]] = {}
        self.controller = None
        self.setup_result_sent = False
        BootstrapRoot.instances.append(self)

    def title(self, value: str) -> None:
        self.window_title = value

    def columnconfigure(self, *args, **kwargs) -> None:
        pass

    def rowconfigure(self, *args, **kwargs) -> None:
        pass

    def protocol(self, name: str, callback: Callable[[], None]) -> None:
        self.protocols[name] = callback

    def after(self, delay_ms: int, callback: Callable[[], None]) -> object:
        token = (delay_ms, callback)
        self.callbacks.append(token)
        return token

    def after_cancel(self, token: object) -> None:
        self.callbacks = [item for item in self.callbacks if item != token]

    def mainloop(self) -> None:
        # Drain initial activation and one result pump.  A production Tk main
        # loop remains live; this fixture bounds the synthetic harness.
        for _ in range(3):
            if self.callbacks:
                index = min(range(len(self.callbacks)), key=lambda i: self.callbacks[i][0])
                _, callback = self.callbacks.pop(index)
                callback()
            pool = InlinePool.instances[-1] if InlinePool.instances else None
            if pool is not None and pool.jobs:
                pool.run_all()
            if self.controller is not None and self.setup_result_path is not None and not self.setup_result_sent:
                self.setup_result_sent = True
                self.controller.on_setup_saved(
                    SetupSaveResult("saved", self.setup_result_path, "config-digest", (self.setup_result_path,), (), None, False)
                )
            if self.controller is not None and self.controller._snapshot is not None:
                break

    def destroy(self) -> None:
        self.destroyed = True
        self.destroy_count += 1


class InlinePool:
    instances: list["InlinePool"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.jobs: list[Callable[[], object]] = []
        InlinePool.instances.append(self)

    def submit(self, fn: Callable[[], object]) -> Future[object]:
        future: Future[object] = Future()
        self.jobs.append(fn)
        return future

    def run_all(self) -> None:
        while self.jobs:
            self.jobs.pop(0)()

    def shutdown(self, **kwargs) -> None:
        self.jobs.clear()


class BootstrapView:
    instances: list["BootstrapView"] = []

    def __init__(self, root, *, controller=None) -> None:
        self.root = root
        self.controller = controller
        self.snapshots = []
        self.pending = []
        self.results = []
        self.errors = []
        self.confirm = True
        self.enabled = []
        self.samples = []
        self.setup_entries = 0
        self.setup_available: list[bool] = []
        self.destroyed = False
        BootstrapView.instances.append(self)

    def bind_controller(self, controller) -> None:
        self.controller = controller
        self.root.controller = controller

    def show_sample_preview(self, sample) -> None:
        self.samples.append(sample)

    def show_snapshot(self, snapshot) -> None:
        self.snapshots.append(snapshot)

    def show_pending(self, operation: str) -> None:
        self.pending.append(operation)

    def show_result(self, result) -> None:
        self.results.append(result)

    def show_error(self, message: str) -> None:
        self.errors.append(message)

    def show_setup_entry(self) -> None:
        self.setup_entries += 1

    def set_setup_available(self, available: bool) -> None:
        self.setup_available.append(available)

    def set_buttons_enabled(self, enabled: bool) -> None:
        self.enabled.append(enabled)

    def show_free_text_outcome_prompt(self) -> None:
        pass

    def show_kzn_completion_confirmation(self, action_id: str, text: str, entry_digest: str) -> bool:
        return self.confirm

    def show_correction_targets(self, targets: object) -> None:
        pass

    def ask_confirmation(self, title: str, message: str) -> bool:
        return self.confirm


class InlineExecutor:
    def __init__(self) -> None:
        self.jobs: list[Callable[[], object]] = []

    def submit(self, fn: Callable[[], object]) -> Future[object]:
        future: Future[object] = Future()
        self.jobs.append(fn)
        return future

    def run_next(self) -> object:
        return self.jobs.pop(0)()


class FakeView:
    def __init__(self) -> None:
        self.snapshots: list[HomeSnapshot] = []
        self.pending: list[str] = []
        self.results: list[ControllerResult] = []
        self.errors: list[str] = []
        self.confirm = True
        self.completion_confirm = True
        self.enabled: list[bool] = []
        self.free_text_completion_button_enabled = False
        self.messages: list[str] = []
        self.targets = []
        self.worker_direct_calls: list[str] = []

    def show_snapshot(self, snapshot: HomeSnapshot) -> None:
        self.snapshots.append(snapshot)

    def show_pending(self, operation: str) -> None:
        self.pending.append(operation)

    def show_result(self, result: ControllerResult) -> None:
        self.results.append(result)

    def show_error(self, message: str) -> None:
        self.errors.append(message)

    def ask_confirmation(self, title: str, message: str) -> bool:
        return self.confirm

    def set_buttons_enabled(self, enabled: bool) -> None:
        self.enabled.append(enabled)

    def show_setup_entry(self) -> None:
        self.messages.append("setup")

    def show_sample_preview(self, sample: object) -> None:
        self.messages.append("sample")

    def show_free_text_outcome_prompt(self) -> None:
        self.free_text_completion_button_enabled = False

    def show_kzn_completion_confirmation(self, action_id: str, text: str, entry_digest: str) -> bool:
        return self.completion_confirm

    def show_correction_targets(self, targets: object) -> None:
        self.targets.append(targets)


class FakeService:
    def __init__(self) -> None:
        self.loaded: list[tuple[Config, date]] = []
        self.refreshed: list[tuple[Config, date]] = []
        self.human: list[tuple[HumanOperation, int]] = []
        self.projected: list[tuple[date, int]] = []
        self.completions: list[tuple[str, date, int, str]] = []
        self.refresh_value: object = _snapshot()
        self.human_result: object | None = None

    def load_home_snapshot(self, cfg: Config, day: date) -> HomeSnapshot:
        self.loaded.append((cfg, day))
        return _snapshot(day)

    def refresh_aw_snapshot(self, cfg: Config, day: date, **kwargs: object) -> object:
        self.refreshed.append((cfg, day))
        if isinstance(self.refresh_value, HomeSnapshot):
            return _snapshot(day)
        return self.refresh_value

    def apply_human_operation(self, cfg: Config, day: date, operation: HumanOperation, *, expected_revision: int) -> object:
        self.human.append((operation, expected_revision))
        return self.human_result if self.human_result is not None else object()

    def project_human_day(self, cfg: Config, day: date, *, expected_revision: int) -> object:
        self.projected.append((day, expected_revision))
        return object()

    def complete_kzn_action(self, cfg: Config, action_id: str, day: date, *, expected_revision: int, expected_action_digest: str) -> KznCompletionResult:
        self.completions.append((action_id, day, expected_revision, expected_action_digest))
        return KznCompletionResult("saved", action_id, day, expected_revision, "done", "synced", None)

    def load_correction_targets(self, cfg: Config, day: date) -> tuple[object, ...]:
        return ()


class FakeJournal:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def sample_journal(self, day=None):
        self.calls.append(("sample", (day,), {}))
        return SimpleNamespace(fixture_id="fixture", markdown="sample")

    def preview_initial_setup(self, opts, **kwargs):
        self.calls.append(("setup_preview", (opts,), kwargs))
        return SimpleNamespace(config_path=Path("config.toml"))

    def save_initial_setup(self, opts, preview, **kwargs):
        self.calls.append(("setup_save", (opts, preview), kwargs))
        return SimpleNamespace(status="saved")

    def preview_journal(self, cfg, day=None, **kwargs):
        self.calls.append(("journal_preview", (cfg, day), kwargs))
        return object()

    def execute_journal(self, cfg, preview):
        self.calls.append(("journal_execute", (cfg, preview), {}))
        return SimpleNamespace(status="generated")

    def open_daily_note(self, cfg, day):
        self.calls.append(("open", (cfg, day), {}))
        return SimpleNamespace(status="opened")

    def preview_report_no_llm(self, cfg, day=None):
        self.calls.append(("report_preview", (cfg, day), {}))
        return object()

    def execute_report_no_llm(self, cfg, preview):
        self.calls.append(("report_execute", (cfg, preview), {}))
        return SimpleNamespace(status="generated")


def _controller(tmp_path: Path) -> tuple[HomeController, FakeService, FakeView, InlineExecutor, FakeScheduler, ResultQueue]:
    cfg = Config(vault_dir=tmp_path)
    service = FakeService()
    view = FakeView()
    executor = InlineExecutor()
    scheduler = FakeScheduler()
    queue = ResultQueue()
    controller = HomeController(
        cfg,
        service=service,
        journal=object(),
        view=view,
        root=FakeRoot(),
        scheduler=scheduler,
        executor=executor,
        queue=queue,
    )
    return controller, service, view, executor, scheduler, queue


def test_initial_load_is_read_only_and_does_not_refresh_or_write(tmp_path: Path) -> None:
    controller, service, view, executor, scheduler, queue = _controller(tmp_path)

    controller.load(DAY)
    executor.run_next()
    scheduler.run_once()

    assert service.loaded == [(service.loaded[0][0], DAY)]
    assert service.refreshed == []
    assert view.snapshots[-1].day == DAY
    assert view.pending == ["load"]


def test_activated_controller_rejects_setup_preview_and_save_without_queueing(tmp_path: Path) -> None:
    controller, service, view, executor, scheduler, queue = _controller(tmp_path)
    opts = SimpleNamespace(config_path=tmp_path / "config.toml", vault=tmp_path)
    preview = SimpleNamespace(config_path=opts.config_path, writes=(), diagnostics=())

    controller.preview_initial_setup(opts, daily_notes_dir=None, daily_note_format="YYYY-MM-DD", backend="none", model="")
    controller.save_initial_setup(
        opts,
        preview,
        daily_notes_dir=None,
        daily_note_format="YYYY-MM-DD",
        backend="none",
        model="",
    )

    assert executor.jobs == []
    assert len(view.errors) == 2
    assert all("起動済み" in message for message in view.errors)


def test_main_help_parses_before_tk(monkeypatch) -> None:
    def fail_tk():
        raise AssertionError("Tk must not start for --help")

    monkeypatch.setattr("kaizenlog.home_gui.tk.Tk", fail_tk)
    assert main(["--help"]) == 0


def test_main_invalid_explicit_config_shows_readable_error_and_returns_one(monkeypatch) -> None:
    BootstrapRoot.instances.clear()
    BootstrapView.instances.clear()
    InlinePool.instances.clear()
    BootstrapRoot.setup_result_path = None
    monkeypatch.setattr("kaizenlog.home_gui.tk.Tk", BootstrapRoot)
    monkeypatch.setattr("kaizenlog.home_gui.HomeView", BootstrapView)
    monkeypatch.setattr(
        "kaizenlog.home_gui.find_config_file",
        lambda explicit=None: (_ for _ in ()).throw(FileNotFoundError("missing config")),
    )
    assert main(["--config", "missing.toml"]) == 1
    assert "設定を読み込めません" in BootstrapView.instances[-1].errors[0]


def test_main_destroys_root_and_closes_executor_when_view_construction_fails(monkeypatch) -> None:
    BootstrapRoot.instances.clear()
    InlinePool.instances.clear()
    BootstrapRoot.setup_result_path = None
    shutdowns: list[dict[str, object]] = []

    class RaisingView:
        def __init__(self, root, *, controller=None):
            raise RuntimeError("view construction failed")

    class ClosingPool(InlinePool):
        def shutdown(self, **kwargs):
            shutdowns.append(kwargs)
            super().shutdown(**kwargs)

    monkeypatch.setattr("kaizenlog.home_gui.tk.Tk", BootstrapRoot)
    monkeypatch.setattr("kaizenlog.home_gui.HomeView", RaisingView)
    monkeypatch.setattr("kaizenlog.home_gui.ThreadPoolExecutor", ClosingPool)
    monkeypatch.setattr("kaizenlog.home_gui.find_config_file", lambda explicit=None: None)

    assert main([]) == 1
    assert BootstrapRoot.instances[-1].destroyed is True
    assert BootstrapRoot.instances[-1].destroy_count == 1
    assert shutdowns == [{"wait": False, "cancel_futures": True}]


def test_main_without_config_keeps_bootstrap_alive_with_sample_and_setup(monkeypatch) -> None:
    BootstrapRoot.instances.clear()
    BootstrapView.instances.clear()
    InlinePool.instances.clear()
    BootstrapRoot.setup_result_path = None
    sample = SimpleNamespace(fixture_id="bootstrap-sample", markdown="# sample")
    monkeypatch.setattr("kaizenlog.home_gui.tk.Tk", BootstrapRoot)
    monkeypatch.setattr("kaizenlog.home_gui.HomeView", BootstrapView)
    monkeypatch.setattr("kaizenlog.home_gui.ThreadPoolExecutor", InlinePool)
    monkeypatch.setattr("kaizenlog.home_gui.find_config_file", lambda explicit=None: None)
    monkeypatch.setattr("kaizenlog.home_gui.journal_service.sample_journal", lambda day=None: sample)
    home_reads: list[date] = []
    monkeypatch.setattr(
        "kaizenlog.home_gui.home_service.load_home_snapshot",
        lambda cfg, day: home_reads.append(day),
    )

    assert main([]) == 0
    root = BootstrapRoot.instances[-1]
    view = BootstrapView.instances[-1]
    assert root.destroyed is True
    assert view.samples == [sample]
    assert view.setup_entries == 1
    assert view.setup_available == []
    assert home_reads == []


def test_main_existing_config_activates_actual_home_adapter_on_explicit_date(tmp_path: Path, monkeypatch) -> None:
    BootstrapRoot.instances.clear()
    BootstrapView.instances.clear()
    InlinePool.instances.clear()
    BootstrapRoot.setup_result_path = None
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[general]\nvault_dir = "{str(tmp_path).replace(chr(92), "/")}\"\ntimezone = "Asia/Tokyo"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("kaizenlog.home_gui.tk.Tk", BootstrapRoot)
    monkeypatch.setattr("kaizenlog.home_gui.HomeView", BootstrapView)
    monkeypatch.setattr("kaizenlog.home_gui.ThreadPoolExecutor", InlinePool)

    assert main(["--config", str(config_path), "--date", "2026-10-15"]) == 0
    root = BootstrapRoot.instances[-1]
    view = BootstrapView.instances[-1]
    assert root.destroyed is True
    assert view.samples == []
    assert view.setup_available == [False]
    assert view.controller.cfg is not None
    assert view.controller.service is home_gui.home_service
    assert [snapshot.day for snapshot in view.snapshots] == [DAY]


def test_main_setup_save_callback_activates_once_and_only_then_reads_home(tmp_path: Path, monkeypatch) -> None:
    BootstrapRoot.instances.clear()
    BootstrapView.instances.clear()
    InlinePool.instances.clear()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[general]\nvault_dir = "{str(tmp_path).replace(chr(92), "/")}"\ntimezone = "Asia/Tokyo"\n',
        encoding="utf-8",
    )
    BootstrapRoot.setup_result_path = config_path
    discovered = {"value": False}

    def find_config(explicit=None):
        if explicit:
            return Path(explicit)
        discovered["value"] = True
        return None

    home_reads: list[date] = []
    original_load = home_gui.home_service.load_home_snapshot

    def record_load(cfg, day):
        home_reads.append(day)
        return original_load(cfg, day)

    monkeypatch.setattr("kaizenlog.home_gui.tk.Tk", BootstrapRoot)
    monkeypatch.setattr("kaizenlog.home_gui.HomeView", BootstrapView)
    monkeypatch.setattr("kaizenlog.home_gui.ThreadPoolExecutor", InlinePool)
    monkeypatch.setattr("kaizenlog.home_gui.find_config_file", find_config)
    monkeypatch.setattr("kaizenlog.home_gui.home_service.load_home_snapshot", record_load)

    assert main(["--date", "2026-10-15"]) == 0
    view = BootstrapView.instances[-1]
    assert discovered["value"] is True
    assert home_reads == [DAY]
    assert [snapshot.day for snapshot in view.snapshots] == [DAY]
    assert view.controller.cfg is not None
    assert view.controller.service is home_gui.home_service
    BootstrapRoot.setup_result_path = None


def test_refresh_uses_queue_and_main_pump_updates_view_without_worker_view_call(tmp_path: Path) -> None:
    controller, service, view, executor, scheduler, queue = _controller(tmp_path)
    controller.request_refresh(DAY)
    assert view.worker_direct_calls == []
    assert view.pending[-1] == "aw_refresh"
    executor.run_next()
    assert view.snapshots == []
    scheduler.run_once()
    assert view.snapshots[-1].day == DAY


def test_date_change_drops_late_refresh_result(tmp_path: Path) -> None:
    controller, service, view, executor, scheduler, queue = _controller(tmp_path)
    controller.request_refresh(DAY)
    first_job = executor.jobs.pop(0)
    controller.request_refresh(date(2026, 10, 16))
    second_job = executor.jobs.pop(0)
    first_job()
    second_job()
    scheduler.run_once()

    assert [snapshot.day for snapshot in view.snapshots] == [date(2026, 10, 16)]


def test_cancel_and_close_ignore_late_refresh_result(tmp_path: Path) -> None:
    controller, service, view, executor, scheduler, queue = _controller(tmp_path)
    controller.request_refresh(DAY)
    job = executor.jobs.pop(0)
    controller.cancel_refresh()
    job()
    scheduler.run_once()
    assert view.snapshots == []

    controller.request_refresh(DAY)
    job = executor.jobs.pop(0)
    controller.close()
    job()
    assert view.snapshots == []
    assert scheduler.cancelled


def test_write_requires_confirmation_and_forwards_revision(tmp_path: Path) -> None:
    controller, service, view, executor, scheduler, queue = _controller(tmp_path)
    operation = HumanOperation("op-1", DAY, "outcome", {"text": "成果"}, None, None)
    view.confirm = False
    controller.submit_human(operation, expected_revision=3)
    assert executor.jobs == []
    assert service.human == []

    view.confirm = True
    controller.submit_human(operation, expected_revision=3)
    executor.run_next()
    scheduler.run_once()
    assert service.human == [(operation, 3)]


def test_successful_human_save_refreshes_current_snapshot_before_next_write(tmp_path: Path) -> None:
    controller, service, view, executor, scheduler, queue = _controller(tmp_path)
    service.human_result = HumanSaveResult("saved", DAY, "op-1", 4, tmp_path / "human.json", "pending", None)
    controller.load(DAY)
    executor.run_next()
    scheduler.run_once()
    operation = HumanOperation("op-1", DAY, "outcome", {"text": "成果"}, None, None)
    controller.submit_human(operation, expected_revision=3)
    executor.run_next()
    scheduler.run_once()
    assert view.results[-1].payload.status == "saved"
    assert view.enabled[-1] is False
    assert executor.jobs
    executor.run_next()
    scheduler.run_once()
    assert view.snapshots[-1].day == DAY
    assert view.enabled[-1] is True


def test_kzn_completion_requires_id_and_digest_confirmation(tmp_path: Path) -> None:
    controller, service, view, executor, scheduler, queue = _controller(tmp_path)
    controller.load(DAY)
    executor.run_next()
    scheduler.run_once()
    controller.complete_kzn("KZN-20261015-001", DAY, expected_revision=3, expected_action_digest="digest")
    executor.run_next()
    scheduler.run_once()
    assert service.completions == [("KZN-20261015-001", DAY, 3, "digest")]
    assert view.results[-1].payload.status == "saved"
    assert view.free_text_completion_button_enabled is False


def test_kzn_completion_does_not_submit_for_free_text_or_missing_candidate(tmp_path: Path) -> None:
    controller, service, view, executor, scheduler, queue = _controller(tmp_path)
    controller._snapshot = SimpleNamespace(
        next_actions=(SimpleNamespace(source="free_text", source_id=None, selectable=True, completable=False, status="available"),)
    )
    controller.complete_kzn("KZN-20261015-001", DAY, expected_revision=0, expected_action_digest="digest")
    assert service.completions == []
    assert executor.jobs == []

    controller._snapshot = SimpleNamespace(next_actions=())
    controller.complete_kzn("KZN-20261015-001", DAY, expected_revision=0, expected_action_digest="digest")
    assert service.completions == []
    assert executor.jobs == []


def test_correction_targets_are_loaded_on_executor_and_shown_by_pump(tmp_path: Path) -> None:
    controller, service, view, executor, scheduler, queue = _controller(tmp_path)
    controller.show_correction_targets(DAY)
    assert view.targets == []
    executor.run_next()
    scheduler.run_once()
    assert view.targets == [()]


def test_journal_actions_delegate_typed_objects_without_cli_or_stdout(tmp_path: Path) -> None:
    service = FakeService()
    journal = FakeJournal()
    view = FakeView()
    executor = InlineExecutor()
    scheduler = FakeScheduler()
    controller = HomeController(
        Config(vault_dir=tmp_path),
        service=service,
        journal=journal,
        view=view,
        root=FakeRoot(),
        scheduler=scheduler,
        executor=executor,
        queue=ResultQueue(),
    )
    controller.show_sample_preview(None)
    controller.preview_journal(DAY)
    executor.run_next()
    scheduler.run_once()
    preview = view.results[-1].payload
    controller.execute_journal(preview)
    executor.run_next()
    scheduler.run_once()
    controller.open_daily_note(DAY)
    executor.run_next()
    scheduler.run_once()
    controller.preview_report(DAY)
    executor.run_next()
    scheduler.run_once()
    report_preview = view.results[-1].payload
    controller.execute_report(report_preview)
    executor.run_next()
    scheduler.run_once()

    assert [name for name, _, _ in journal.calls] == [
        "sample", "journal_preview", "journal_execute", "open", "report_preview", "report_execute"
    ]


def test_preview_and_execution_feedback_preserve_snapshot_and_precondition_details(tmp_path: Path) -> None:
    controller, _, _, _, _, _ = _controller(tmp_path)
    preview = SimpleNamespace(
        target_day=DAY,
        snapshot_digest="snapshot-123",
        source_status=SimpleNamespace(status="observed", reason=None),
        planned_updates=(
            SimpleNamespace(
                target="target_day",
                path=tmp_path / "2026-10-15.md",
                action="upsert_section",
                reason="日誌本文を更新",
                expected_revision=7,
                expected_digest="expected-file-digest",
            ),
        ),
        preconditions=(
            SimpleNamespace(
                target="target_day",
                path=tmp_path / "2026-10-15.md",
                expected_revision=7,
                expected_digest="expected-file-digest",
            ),
        ),
    )
    confirmation = controller._preview_confirmation("日誌を生成", preview)
    assert "対象日: 2026-10-15" in confirmation
    assert "source status: observed" in confirmation
    assert "snapshot-123" in confirmation
    assert "expected_revision=7" in confirmation
    assert "expected-file-digest" in confirmation
    assert str(tmp_path / "2026-10-15.md") in confirmation

    view = HomeView(None)
    execution = SimpleNamespace(
        status="conflict",
        target_day=DAY,
        snapshot_digest="snapshot-123",
        source_status=SimpleNamespace(status="observed", reason=None),
        precondition_failures=(
            SimpleNamespace(
                target="target_day",
                path=tmp_path / "2026-10-15.md",
                expected_revision=7,
                expected_digest="expected-file-digest",
                observed_revision=8,
                observed_digest="observed-file-digest",
                reason="precondition changed",
            ),
        ),
        error="precondition changed",
    )
    view.show_result(ControllerResult(1, "journal_execute", payload=execution))
    rendered = view.render_text()
    assert "snapshot-123" in rendered
    assert "expected_revision=7" in rendered
    assert "observed_revision=8" in rendered
    assert "observed-file-digest" in rendered


def test_configless_bootstrap_can_preview_and_confirm_setup_without_data_controller(tmp_path: Path) -> None:
    journal = FakeJournal()
    view = FakeView()
    executor = InlineExecutor()
    scheduler = FakeScheduler()
    controller = HomeController(
        None,
        service=None,
        journal=journal,
        view=view,
        root=FakeRoot(),
        scheduler=scheduler,
        executor=executor,
        queue=ResultQueue(),
    )
    opts = SimpleNamespace()
    controller.preview_initial_setup(
        opts,
        daily_notes_dir="notes",
        daily_note_format="YYYY-MM-DD",
        backend="none",
        model="fixture",
    )
    executor.run_next()
    scheduler.run_once()
    preview = view.results[-1].payload
    controller.save_initial_setup(
        opts,
        preview,
        daily_notes_dir="notes",
        daily_note_format="YYYY-MM-DD",
        backend="none",
        model="fixture",
    )
    executor.run_next()
    scheduler.run_once()

    assert [name for name, _, _ in journal.calls] == ["setup_preview", "setup_save"]
