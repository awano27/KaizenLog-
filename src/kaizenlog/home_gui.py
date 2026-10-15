"""Main-thread controller, Tk view, and delayed launcher for Home.

Workers only call injected ports and put a value on the result queue; all view
calls happen in the main-thread pump.  The launcher binds the real service
modules only after a valid configuration has been resolved.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime
import json
from pathlib import Path
from queue import Empty, Queue
from threading import Event
from types import SimpleNamespace
import sys
from typing import Callable, Protocol, Sequence, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import home_service, journal_service, prompt_draft
from .config import Config, find_config_file, load_config
from .home_service import (
    HomeSnapshot,
    KznCompletionResult,
    LiveMeasurementSnapshot,
)
from .human_input import HumanOperation
from .journal_service import (
    JournalExecutionResult,
    JournalPreview,
    OpenResult,
    ReportExecutionResult,
    ReportPreview,
    SampleJournal,
    SetupPreview,
    SetupSaveResult,
)
from .setup import SetupOptions
from .prompt_draft import PromptDraftResult


@dataclass(frozen=True, slots=True)
class ControllerResult:
    """A worker result tagged with the request generation that created it."""

    generation: int
    operation: str
    payload: object | None = None
    error: str | None = None


class ResultQueue:
    """Small typed facade over a thread-safe queue used by workers and Tk."""

    def __init__(self) -> None:
        self._queue: Queue[ControllerResult] = Queue()

    def put(self, result: ControllerResult) -> None:
        self._queue.put(result)

    def get_nowait(self) -> ControllerResult:
        return self._queue.get_nowait()


class SchedulerPort(Protocol):
    def after(self, delay_ms: int, callback: Callable[[], None]) -> object: ...

    def after_cancel(self, token: object) -> None: ...


class ExecutorPort(Protocol):
    def submit(self, fn: Callable[[], object]) -> Future[object]: ...


class TkRootPort(Protocol):
    def after(self, delay_ms: int, callback: Callable[[], None]) -> object: ...

    def after_cancel(self, token: object) -> None: ...

    def mainloop(self) -> None: ...

    def destroy(self) -> None: ...


class HomeServicePort(Protocol):
    def load_home_snapshot(self, cfg: Config, day: date) -> HomeSnapshot: ...

    def refresh_aw_snapshot(
        self,
        cfg: Config,
        day: date,
        *,
        now: datetime | None = None,
        client: object | None = None,
    ) -> HomeSnapshot | LiveMeasurementSnapshot: ...

    def apply_human_operation(
        self,
        cfg: Config,
        day: date,
        operation: HumanOperation,
        *,
        expected_revision: int,
    ) -> object: ...

    def project_human_day(self, cfg: Config, day: date, *, expected_revision: int) -> object: ...

    def complete_kzn_action(
        self,
        cfg: Config,
        action_id: str,
        day: date,
        *,
        expected_revision: int,
        expected_action_digest: str,
    ) -> KznCompletionResult: ...


class JournalServicePort(Protocol):
    def sample_journal(self, day: date | None = None) -> SampleJournal: ...

    def preview_initial_setup(
        self,
        opts: SetupOptions,
        *,
        daily_notes_dir: str | None,
        daily_note_format: str,
        backend: str,
        model: str,
    ) -> SetupPreview: ...

    def save_initial_setup(
        self,
        opts: SetupOptions,
        preview: SetupPreview,
        *,
        daily_notes_dir: str | None,
        daily_note_format: str,
        backend: str,
        model: str,
    ) -> SetupSaveResult: ...

    def preview_journal(self, cfg: Config, day: date | None = None, *, now: datetime | None = None) -> JournalPreview: ...

    def execute_journal(self, cfg: Config, preview: JournalPreview) -> JournalExecutionResult: ...

    def open_daily_note(self, cfg: Config, day: date) -> OpenResult: ...

    def preview_report_no_llm(self, cfg: Config, day: date | None = None) -> ReportPreview: ...

    def execute_report_no_llm(self, cfg: Config, preview: ReportPreview) -> ReportExecutionResult: ...


class DraftServicePort(Protocol):
    def draft_prompt(
        self,
        cfg: Config,
        prm_id: str,
        *,
        project: Path | None,
        write: bool,
        force: bool,
        expected_fingerprint: str | None,
    ) -> PromptDraftResult: ...


class HomeViewPort(Protocol):
    def show_snapshot(self, snapshot: HomeSnapshot) -> None: ...

    def show_pending(self, operation: str) -> None: ...

    def show_result(self, result: ControllerResult) -> None: ...

    def show_error(self, message: str) -> None: ...

    def ask_confirmation(self, title: str, message: str) -> bool: ...

    def set_buttons_enabled(self, enabled: bool) -> None: ...

    def show_setup_entry(self) -> None: ...

    def show_sample_preview(self, sample: SampleJournal) -> None: ...

    def show_free_text_outcome_prompt(self) -> None: ...

    def show_kzn_completion_confirmation(self, action_id: str, text: str, entry_digest: str) -> bool: ...

    def show_correction_targets(self, targets: object) -> None: ...


class HomeController:
    """Coordinate Home operations while keeping Tk calls on the main thread."""

    PUMP_DELAY_MS = 40

    def __init__(
        self,
        cfg: Config | None,
        *,
        service: HomeServicePort | None,
        journal: JournalServicePort,
        view: HomeViewPort,
        root: TkRootPort,
        scheduler: SchedulerPort,
        executor: ExecutorPort,
        queue: ResultQueue,
        draft: DraftServicePort | None = None,
        on_setup_saved: Callable[[SetupSaveResult], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.service = service
        self.journal = journal
        self.view = view
        self.root = root
        self.scheduler = scheduler
        self.executor = executor
        self.queue = queue
        self.draft = draft
        self.on_setup_saved = on_setup_saved
        self._generation = 0
        self._closed = False
        self._busy = False
        self._operation: str | None = None
        self._cancel_event: Event | None = None
        self._day: date | None = None
        self._snapshot: HomeSnapshot | None = None
        self._pump_token: object | None = None
        self._schedule_pump()

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def current_day(self) -> date | None:
        return self._day

    def _schedule_pump(self) -> None:
        if not self._closed:
            self._pump_token = self.scheduler.after(self.PUMP_DELAY_MS, self.pump_once)

    def pump_once(self) -> None:
        if self._closed:
            return
        while True:
            try:
                result = self.queue.get_nowait()
            except Empty:
                break
            if result.generation != self._generation:
                continue
            if result.error is not None:
                self.view.show_error(result.error)
            elif result.payload is not None:
                self._handle_result(result)
            self.view.show_result(result)
            if (
                result.operation == "setup_save"
                and result.error is None
                and result.payload is not None
                and getattr(result.payload, "status", None) in {"saved", "already_saved"}
                and self.on_setup_saved is not None
            ):
                self.on_setup_saved(cast(SetupSaveResult, result.payload))
            if self._needs_snapshot_refresh(result) and self._day is not None and self.cfg is not None and self.service is not None:
                self._busy = False
                self._operation = None
                self._cancel_event = None
                self.view.set_buttons_enabled(True)
                day = self._day
                self._submit("post_save_refresh", lambda: self.service.load_home_snapshot(self.cfg, day))
            else:
                self._busy = False
                self._operation = None
                self._cancel_event = None
                self.view.set_buttons_enabled(True)
        self._schedule_pump()

    def _needs_snapshot_refresh(self, result: ControllerResult) -> bool:
        if result.error is not None or result.payload is None:
            return False
        status = getattr(result.payload, "status", None)
        if result.operation.startswith("human:"):
            return status in {"saved", "idempotent"}
        if result.operation == "human_projection":
            return status in {"applied", "already_applied"}
        if result.operation == "kzn_completion":
            return status in {"saved", "idempotent"}
        if result.operation in {"journal_execute", "report_execute"}:
            return status in {"generated", "already_generated"}
        return False

    def _handle_result(self, result: ControllerResult) -> None:
        payload = result.payload
        if isinstance(payload, HomeSnapshot):
            self._snapshot = payload
            self._day = payload.day
            self.view.show_snapshot(payload)
            self.view.show_free_text_outcome_prompt()
            return
        if isinstance(payload, LiveMeasurementSnapshot):
            if self._snapshot is not None and self._snapshot.day == self._day:
                self._snapshot = replace(self._snapshot, measurements=payload)
                self.view.show_snapshot(self._snapshot)
            return
        if result.operation == "correction_targets":
            show_targets = getattr(self.view, "show_correction_targets", None)
            if show_targets is not None:
                show_targets(payload)

    def _require_ready(self) -> bool:
        if self._closed:
            return False
        if self.cfg is None or self.service is None:
            self.view.show_error("設定がないためデータ操作を開始できません。初期設定を完了してください。")
            return False
        return True

    def _begin(self, operation: str, *, replace_pending: bool = False) -> tuple[int, Event] | None:
        if self._closed:
            return None
        if self._busy:
            if not replace_pending:
                self.view.show_error("別の処理が進行中です。完了するまで操作できません。")
                return None
            self._generation += 1
            if self._cancel_event is not None:
                self._cancel_event.set()
        self._generation += 1
        event = Event()
        self._cancel_event = event
        self._busy = True
        self._operation = operation
        self.view.show_pending(operation)
        self.view.set_buttons_enabled(False)
        return self._generation, event

    def _submit(
        self,
        operation: str,
        fn: Callable[[], object],
        *,
        replace_pending: bool = False,
    ) -> None:
        started = self._begin(operation, replace_pending=replace_pending)
        if started is None:
            return
        generation, cancel = started

        def worker() -> None:
            if cancel.is_set():
                return
            try:
                payload = fn()
            except Exception as exc:  # worker errors cross only through queue
                if not cancel.is_set():
                    self.queue.put(ControllerResult(generation, operation, error=f"{type(exc).__name__}: {exc}"))
                return
            if not cancel.is_set():
                self.queue.put(ControllerResult(generation, operation, payload=payload))

        try:
            self.executor.submit(worker)
        except Exception as exc:
            self._busy = False
            self._operation = None
            self._cancel_event = None
            self.view.set_buttons_enabled(True)
            self.view.show_error(f"{type(exc).__name__}: {exc}")

    def load(self, day: date) -> None:
        if not self._require_ready():
            return
        self._day = day
        assert self.cfg is not None and self.service is not None
        self._submit("load", lambda: self.service.load_home_snapshot(self.cfg, day), replace_pending=True)

    def request_refresh(self, day: date | None = None) -> None:
        if not self._require_ready():
            return
        target = day or self._day or date.today()
        self._day = target
        assert self.cfg is not None and self.service is not None
        self._submit("aw_refresh", lambda: self.service.refresh_aw_snapshot(self.cfg, target), replace_pending=True)

    def cancel_refresh(self) -> None:
        if self._operation not in {"load", "aw_refresh"}:
            return
        self._generation += 1
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._busy = False
        self._operation = None
        self._cancel_event = None
        self.view.set_buttons_enabled(True)

    def submit_human(self, operation: HumanOperation, *, expected_revision: int) -> None:
        if not self._require_ready():
            return
        if self._day is not None and operation.target_day != self._day:
            self.view.show_error("対象日が現在の表示日と一致しません。更新してから再試行してください。")
            return
        if not self.view.ask_confirmation("本人入力を保存", f"{operation.target_day.isoformat()} に本人入力を保存します。内容を確認しましたか？"):
            return
        assert self.cfg is not None and self.service is not None
        day = operation.target_day
        self._submit(
            f"human:{operation.kind}",
            lambda: self.service.apply_human_operation(self.cfg, day, operation, expected_revision=expected_revision),
        )

    def complete_kzn(
        self,
        action_id: str,
        day: date,
        *,
        expected_revision: int,
        expected_action_digest: str,
    ) -> None:
        if not self._require_ready():
            return
        if self._snapshot is None:
            self.view.show_error("先に対象日を表示してください。")
            return
        eligible = [
            action
            for action in self._snapshot.next_actions
            if action.source == "kzn"
            and action.source_id
            and action.selectable
            and action.completable
            and action.status == "available"
        ]
        if len(eligible) != 1 or eligible[0].source_id != action_id:
            self.view.show_error("完了できるKZN候補が一意に確認できません。更新してから再試行してください。")
            return
        action = eligible[0]
        text = action.text
        if action.entry_digest:
            expected_action_digest = expected_action_digest or action.entry_digest
        if not self.view.show_kzn_completion_confirmation(action_id, text, expected_action_digest):
            return
        assert self.cfg is not None and self.service is not None
        complete = getattr(self.service, "complete_kzn_action", None)
        if complete is None:
            self.view.show_error("KZN完了サービスはまだ接続されていません。")
            return
        self._submit(
            "kzn_completion",
            lambda: complete(
                self.cfg,
                action_id,
                day,
                expected_revision=expected_revision,
                expected_action_digest=expected_action_digest,
            ),
        )

    def project_human(self, day: date, expected_revision: int) -> None:
        if not self._require_ready():
            return
        if self._day is not None and day != self._day:
            self.view.show_error("対象日が現在の表示日と一致しません。更新してから再試行してください。")
            return
        if not self.view.ask_confirmation("日誌へ再反映", f"{day.isoformat()} の保存済み本人入力を日誌へ再反映します。"):
            return
        assert self.cfg is not None and self.service is not None
        self._submit(
            "human_projection",
            lambda: self.service.project_human_day(self.cfg, day, expected_revision=expected_revision),
        )

    def show_correction_targets(self, day: date) -> None:
        if not self._require_ready():
            return
        assert self.cfg is not None and self.service is not None
        loader = getattr(self.service, "load_correction_targets", None)
        if loader is None:
            self.view.show_error("分類訂正の対象サービスはまだ接続されていません。")
            return
        self._submit("correction_targets", lambda: loader(self.cfg, day))

    def request_draft(
        self,
        prm_id: str,
        *,
        project: Path | None,
        write: bool,
        force: bool,
        expected_fingerprint: str | None = None,
    ) -> None:
        if not self._require_ready():
            return
        if write and not self.view.ask_confirmation("依頼文草案を保存", f"{prm_id} の未検証草案を指定先へ保存します。"):
            return
        assert self.cfg is not None
        draft = self.draft or getattr(self.service, "draft_prompt", None)
        if draft is None:
            self.view.show_error("依頼文草案サービスはまだ接続されていません。")
            return
        self._submit(
            "draft_write" if write else "draft_preview",
            lambda: draft(
                self.cfg,
                prm_id,
                project=project,
                write=write,
                force=force,
                expected_fingerprint=expected_fingerprint,
            ),
        )

    def preview_journal(self, day: date | None = None) -> None:
        if not self._require_ready():
            return
        assert self.cfg is not None
        self._submit("journal_preview", lambda: self.journal.preview_journal(self.cfg, day))

    def show_sample_preview(self, day: date | None = None) -> None:
        """Render the fixed sample without creating a config or storage file."""
        self.view.show_sample_preview(self.journal.sample_journal(day))

    def preview_initial_setup(
        self,
        opts: SetupOptions,
        *,
        daily_notes_dir: str | None,
        daily_note_format: str,
        backend: str,
        model: str,
    ) -> None:
        if self._closed:
            return
        if self.cfg is not None or self.service is not None:
            self.view.show_error("初期設定は起動済みのデータ画面では変更できません。")
            return
        self._submit(
            "setup_preview",
            lambda: self.journal.preview_initial_setup(
                opts,
                daily_notes_dir=daily_notes_dir,
                daily_note_format=daily_note_format,
                backend=backend,
                model=model,
            ),
        )

    def save_initial_setup(
        self,
        opts: SetupOptions,
        preview: SetupPreview,
        *,
        daily_notes_dir: str | None,
        daily_note_format: str,
        backend: str,
        model: str,
    ) -> None:
        if self._closed:
            return
        if self.cfg is not None or self.service is not None:
            self.view.show_error("初期設定は起動済みのデータ画面では変更できません。")
            return
        planned = ", ".join(str(path) for path in getattr(preview, "writes", ())) or "設定ファイル"
        diagnostics = " / ".join(str(item) for item in getattr(preview, "diagnostics", ()))
        message = f"設定ファイル {preview.config_path} と予定先 {planned} を保存します。"
        if diagnostics:
            message += f"\n診断: {diagnostics}"
        if not self.view.ask_confirmation("初期設定を保存", message):
            return
        self._submit(
            "setup_save",
            lambda: self.journal.save_initial_setup(
                opts,
                preview,
                daily_notes_dir=daily_notes_dir,
                daily_note_format=daily_note_format,
                backend=backend,
                model=model,
            ),
        )

    def execute_journal(self, preview: JournalPreview) -> None:
        if not self._require_ready() or not self.view.ask_confirmation("日誌を作る", self._preview_confirmation("日誌を生成", preview)):
            return
        assert self.cfg is not None
        self._submit("journal_execute", lambda: self.journal.execute_journal(self.cfg, preview))

    def open_daily_note(self, day: date) -> None:
        if not self._require_ready():
            return
        assert self.cfg is not None
        self._submit("open_daily_note", lambda: self.journal.open_daily_note(self.cfg, day))

    def preview_report(self, day: date | None = None) -> None:
        if not self._require_ready():
            return
        assert self.cfg is not None
        self._submit("report_preview", lambda: self.journal.preview_report_no_llm(self.cfg, day))

    def execute_report(self, preview: ReportPreview) -> None:
        if not self._require_ready() or not self.view.ask_confirmation("日報を作る", self._preview_confirmation("LLMなしの日報を書き込む", preview)):
            return
        assert self.cfg is not None
        self._submit("report_execute", lambda: self.journal.execute_report_no_llm(self.cfg, preview))

    def _preview_confirmation(self, action: str, preview: object) -> str:
        snapshot = getattr(preview, "snapshot", None)
        target_day = getattr(preview, "target_day", None) or getattr(snapshot, "target_day", None)
        snapshot_digest = getattr(preview, "snapshot_digest", None)
        if snapshot_digest is None:
            snapshot_digest = getattr(preview, "_snapshot_digest", None)
        if snapshot_digest is None:
            snapshot_digest = getattr(snapshot, "source_digest", None)
        source_state = getattr(preview, "source_status", None)
        if source_state is None:
            source_state = getattr(snapshot, "source_status", None)
        source_status = getattr(source_state, "status", source_state)
        source_reason = getattr(source_state, "reason", None)
        rows = [
            f"対象日: {target_day.isoformat() if hasattr(target_day, 'isoformat') else target_day or '未確認'}",
            f"source status: {source_status or '未確認'}" + (f"（{source_reason}）" if source_reason else ""),
            f"snapshot_digest（詳細）: {snapshot_digest or '未確認'}",
        ]
        updates = []
        for update in getattr(preview, "planned_updates", ()):
            target = getattr(update, "target", None)
            path = getattr(update, "path", None)
            reason = getattr(update, "reason", None)
            operation = getattr(update, "action", None)
            expected_revision = getattr(update, "expected_revision", None)
            expected_digest = getattr(update, "expected_digest", None)
            detail = f"{target or '対象'} / {path or 'パス未確認'} / {operation or '操作'} / {reason or '予定'}"
            if expected_revision is not None:
                detail += f" / expected_revision={expected_revision}"
            if expected_digest:
                detail += f" / expected_digest={expected_digest}"
            updates.append(detail)
        rows.append("予定変更:")
        rows.extend(updates or ["対象なし"])
        preconditions = []
        for precondition in getattr(preview, "preconditions", ()):
            target = getattr(precondition, "target", "対象")
            path = getattr(precondition, "path", None) or "パス未確認"
            expected_revision = getattr(precondition, "expected_revision", None)
            expected_digest = getattr(precondition, "expected_digest", None)
            detail = f"{target} / {path}"
            if expected_revision is not None:
                detail += f" / expected_revision={expected_revision}"
            if expected_digest:
                detail += f" / expected_digest={expected_digest}"
            preconditions.append(detail)
        rows.append("事前条件:")
        rows.extend(preconditions or ["なし"])
        return f"{action}します。\n" + "\n".join(rows)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._busy = False
        if self._pump_token is not None:
            try:
                self.scheduler.after_cancel(self._pump_token)
            except Exception:
                pass
        self._pump_token = None
        self.root.destroy()


class HomeView:
    """Small standard ttk view with callback methods usable in headless tests."""

    _STATUS = {
        "observed": "観測済み",
        "missing": "記録なし／欠測",
        "stale": "古いデータ",
        "unavailable": "利用不可",
        "unknown": "未確認",
    }
    _RESULT_STATUS = {
        "saved": "保存済み",
        "idempotent": "保存済み（同じ操作）",
        "pending": "保存済み／日誌未反映",
        "partial_failure": "一部成功・日誌未反映の可能性あり",
        "already_applied": "日誌反映済み",
        "applied": "日誌へ反映済み",
        "generated": "生成済み",
        "already_generated": "生成済み（同じ予定）",
        "missing": "対象がありません",
        "conflict": "競合のため未反映",
        "stale": "表示が古いため未反映",
        "failed": "失敗",
        "invalid": "入力不正",
    }

    def __init__(self, root: object | None, *, controller: HomeController | None = None) -> None:
        self.root = root
        self.controller = controller
        self._text = ""
        self._pending = ""
        self._error = ""
        self._buttons_enabled = True
        self._buttons: list[object] = []
        self._always_enabled_buttons: list[object] = []
        self._text_widget: object | None = None
        self._snapshot: HomeSnapshot | None = None
        self._candidate_by_label: dict[str, object] = {}
        self._correction_by_label: dict[str, object] = {}
        self._journal_preview: JournalPreview | None = None
        self._report_preview: ReportPreview | None = None
        self._setup_preview: SetupPreview | None = None
        self._selected_candidate: object | None = None
        self._completion_visible = False
        self._outcome_operation_id: str | None = None
        self._outcome_text: str | None = None
        self._correction_operation_id: str | None = None
        self._cancel_operation_id: str | None = None
        self._force_draft = False
        self._result_feedback = ""
        self._setup_available = True
        self._setup_notebook: object | None = None
        self._setup_page: object | None = None
        self._setup_controls: list[object] = []
        # Keep only the most recent intent for each day/kind.  A bounded cache
        # preserves retries while preventing A -> B -> A from resurrecting the
        # old operation ID after B became the current intent.
        self._operation_ids: dict[tuple[str, str], tuple[str, str]] = {}
        if root is not None:
            self._build(root)

    def _build(self, root: object) -> None:
        root_widget = cast(tk.Misc, root)
        root_widget.title("KaizenLog")
        root_widget.columnconfigure(0, weight=1)
        root_widget.rowconfigure(0, weight=1)
        root_widget.protocol("WM_DELETE_WINDOW", self.on_close)

        frame = ttk.Frame(root_widget, padding=10)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)
        ttk.Label(frame, text="KaizenLog — 日誌と毎日の振り返り").grid(row=0, column=0, columnspan=4, sticky="w")

        ttk.Label(frame, text="対象日 (YYYY-MM-DD)").grid(row=1, column=0, sticky="w")
        self.date_entry = ttk.Entry(frame, width=14)
        self.date_entry.grid(row=1, column=1, sticky="w")
        self._load_button = ttk.Button(frame, text="表示", command=self._on_load_from_entry)
        self._load_button.grid(row=1, column=2, sticky="w")
        self._refresh_button = ttk.Button(frame, text="計測を更新", command=self._on_refresh_from_entry)
        self._refresh_button.grid(row=1, column=3, sticky="w")
        self._cancel_button = ttk.Button(frame, text="処理を取消", command=self._on_cancel_refresh)
        self._cancel_button.grid(row=2, column=2, sticky="w")
        self._close_button = ttk.Button(frame, text="閉じる", command=self.on_close)
        self._close_button.grid(row=2, column=3, sticky="w")
        self._always_enabled_buttons.extend((self._cancel_button, self._close_button))

        self.path_label = ttk.Label(frame, text="保存先: 未解決")
        self.path_label.grid(row=2, column=0, columnspan=2, sticky="w")
        self.status_label = ttk.Label(frame, text="状態: 未確認")
        self.status_label.grid(row=3, column=0, columnspan=4, sticky="w")

        notebook = ttk.Notebook(frame)
        notebook.grid(row=4, column=0, columnspan=4, sticky="nsew")
        overview = ttk.Frame(notebook, padding=6)
        outputs = ttk.Frame(notebook, padding=6)
        setup = ttk.Frame(notebook, padding=6)
        self._setup_notebook = notebook
        self._setup_page = setup
        notebook.add(overview, text="日次入力")
        notebook.add(outputs, text="生成・草案")
        notebook.add(setup, text="初期設定")
        for page in (overview, outputs, setup):
            page.columnconfigure(1, weight=1)

        overview.rowconfigure(1, weight=1)
        self._text_widget = tk.Text(overview, height=11, width=76, wrap="word")
        self._text_widget.grid(row=1, column=0, columnspan=4, sticky="nsew")

        ttk.Label(overview, text="成果を1行で記録").grid(row=2, column=0, sticky="w")
        self.outcome_entry = ttk.Entry(overview, width=62)
        self.outcome_entry.grid(row=2, column=1, columnspan=2, sticky="ew")
        self._outcome_button = ttk.Button(overview, text="成果を保存", command=self._on_outcome)
        self._outcome_button.grid(row=2, column=3, sticky="w")

        ttk.Label(overview, text="次に試す候補").grid(row=3, column=0, sticky="w")
        self.candidate_combo = ttk.Combobox(overview, state="readonly", width=62, values=())
        self.candidate_combo.grid(row=3, column=1, columnspan=2, sticky="ew")
        self._choose_candidate_button = ttk.Button(overview, text="候補を選ぶ", command=self._on_choose_candidate)
        self._choose_candidate_button.grid(row=3, column=3, sticky="w")
        self.free_text_entry = ttk.Entry(overview, width=62)
        self.free_text_entry.grid(row=4, column=1, columnspan=2, sticky="ew")
        self._choose_free_text_button = ttk.Button(overview, text="自由文を選ぶ", command=self._on_choose_free_text)
        self._choose_free_text_button.grid(row=4, column=3, sticky="w")
        self._complete_button = ttk.Button(overview, text="選択KZNを完了", command=self._on_complete_candidate)
        self._complete_button.grid(row=5, column=3, sticky="w")
        self._completion_grid = {"row": 5, "column": 3, "sticky": "w"}
        self._complete_button.grid_remove()

        ttk.Label(overview, text="分類訂正対象").grid(row=6, column=0, sticky="w")
        self.correction_combo = ttk.Combobox(overview, state="readonly", width=62, values=())
        self.correction_combo.grid(row=6, column=1, columnspan=2, sticky="ew")
        self.category_entry = ttk.Entry(overview, width=20)
        self.category_entry.grid(row=7, column=1, sticky="w")
        self.project_entry = ttk.Entry(overview, width=20)
        self.project_entry.grid(row=7, column=2, sticky="w")
        self._correction_button = ttk.Button(overview, text="訂正を保存", command=self._on_correction)
        self._correction_button.grid(row=7, column=3, sticky="w")

        ttk.Label(overview, text="取消対象操作ID").grid(row=8, column=0, sticky="w")
        self.cancel_combo = ttk.Combobox(overview, state="readonly", width=62, values=())
        self.cancel_combo.grid(row=8, column=1, columnspan=2, sticky="ew")
        self._cancel_human_button = ttk.Button(overview, text="本人入力を取消", command=self._on_cancel_human)
        self._cancel_human_button.grid(row=8, column=3, sticky="w")
        self._project_button = ttk.Button(overview, text="日誌へ再反映", command=self._on_project_human)
        self._project_button.grid(row=9, column=3, sticky="w")

        ttk.Label(outputs, text="依頼文草案 PRM-ID").grid(row=0, column=0, sticky="w")
        self.prm_entry = ttk.Entry(outputs, width=28)
        self.prm_entry.grid(row=0, column=1, sticky="w")
        self.project_path_entry = ttk.Entry(outputs, width=38)
        self.project_path_entry.grid(row=0, column=2, sticky="w")
        self._draft_force_button = ttk.Checkbutton(outputs, text="force", command=self._toggle_force_draft)
        self._draft_force_button.grid(row=1, column=2, sticky="w")
        self._draft_preview_button = ttk.Button(outputs, text="草案プレビュー", command=lambda: self._on_draft(False))
        self._draft_preview_button.grid(row=1, column=0, sticky="w")
        self._draft_write_button = ttk.Button(outputs, text="草案を保存", command=lambda: self._on_draft(True))
        self._draft_write_button.grid(row=1, column=1, sticky="w")

        self._sample_button = ttk.Button(outputs, text="サンプル", command=self._on_sample)
        self._sample_button.grid(row=3, column=0, sticky="w")
        self._journal_preview_button = ttk.Button(outputs, text="日誌予定", command=self._on_journal_preview)
        self._journal_preview_button.grid(row=3, column=1, sticky="w")
        self._journal_execute_button = ttk.Button(outputs, text="日誌を作る", command=self._on_journal_execute)
        self._journal_execute_button.grid(row=3, column=2, sticky="w")
        self._journal_open_button = ttk.Button(outputs, text="日誌を開く", command=self._on_journal_open)
        self._journal_open_button.grid(row=3, column=3, sticky="w")
        self._report_preview_button = ttk.Button(outputs, text="日報予定", command=self._on_report_preview)
        self._report_preview_button.grid(row=4, column=0, sticky="w")
        self._report_execute_button = ttk.Button(outputs, text="日報を作る", command=self._on_report_execute)
        self._report_execute_button.grid(row=4, column=1, sticky="w")

        ttk.Label(setup, text="設定ファイル / Vault / 日誌フォルダ").grid(row=0, column=0, columnspan=4, sticky="w")
        self.setup_config_entry = ttk.Entry(setup, width=32)
        self.setup_config_entry.grid(row=1, column=0, sticky="w")
        self.setup_vault_entry = ttk.Entry(setup, width=32)
        self.setup_vault_entry.grid(row=1, column=1, sticky="w")
        self.setup_notes_entry = ttk.Entry(setup, width=20)
        self.setup_notes_entry.grid(row=1, column=2, sticky="w")
        self.setup_format_combo = ttk.Combobox(setup, state="readonly", width=15, values=("YYYY-MM-DD", "YYYYMMDD", "YYYY/MM/DD"))
        self.setup_format_combo.grid(row=2, column=0, sticky="w")
        self.setup_backend_entry = ttk.Entry(setup, width=18)
        self.setup_backend_entry.grid(row=2, column=1, sticky="w")
        self.setup_model_entry = ttk.Entry(setup, width=18)
        self.setup_model_entry.grid(row=2, column=2, sticky="w")
        self._setup_preview_button = ttk.Button(setup, text="初期設定予定", command=self._on_setup_preview)
        self._setup_preview_button.grid(row=2, column=3, sticky="w")
        self._setup_save_button = ttk.Button(setup, text="初期設定を保存", command=self._on_setup_save)
        self._setup_save_button.grid(row=3, column=3, sticky="w")
        self._setup_controls.extend(
            (
                self.setup_config_entry,
                self.setup_vault_entry,
                self.setup_notes_entry,
                self.setup_format_combo,
                self.setup_backend_entry,
                self.setup_model_entry,
                self._setup_preview_button,
                self._setup_save_button,
            )
        )

        self._buttons.extend(
            (
                self._load_button, self._refresh_button, self._cancel_button, self._close_button,
                self._outcome_button, self._choose_candidate_button, self._choose_free_text_button,
                self._complete_button, self._correction_button, self._cancel_human_button, self._project_button,
                self._draft_preview_button, self._draft_write_button, self._sample_button,
                self._journal_preview_button, self._journal_execute_button, self._journal_open_button,
                self._report_preview_button, self._report_execute_button, self._setup_preview_button,
                self._setup_save_button,
            )
        )

    def bind_controller(self, controller: HomeController) -> None:
        self.controller = controller

    def set_setup_available(self, available: bool) -> None:
        """Keep setup controls usable only before a config is activated."""

        self._setup_available = bool(available)
        state = "normal" if self._setup_available else "disabled"
        for widget in self._setup_controls:
            try:
                widget.configure(state=state)
            except Exception:
                pass
        notebook = self._setup_notebook
        page = self._setup_page
        if notebook is not None and page is not None:
            try:
                if self._setup_available:
                    notebook.add(page, text="初期設定")
                else:
                    notebook.hide(page)
            except Exception:
                # Headless fakes may not implement Notebook.hide/add; the
                # controls above still enforce the bootstrap-only boundary.
                pass
        if hasattr(self, "_buttons_enabled"):
            self.set_buttons_enabled(self._buttons_enabled)

    def on_refresh(self, day: date | None) -> None:
        if self.controller is not None:
            self.controller.request_refresh(day)

    def on_load(self, day: date) -> None:
        if self.controller is not None:
            self.controller.load(day)

    def on_close(self) -> None:
        if self.controller is not None:
            self.controller.close()

    def on_submit_human(self, operation: HumanOperation, *, expected_revision: int) -> None:
        if self.controller is not None:
            self.controller.submit_human(operation, expected_revision=expected_revision)

    def on_project_human(self, day: date, *, expected_revision: int) -> None:
        if self.controller is not None:
            self.controller.project_human(day, expected_revision)

    def on_complete_kzn(
        self,
        action_id: str,
        day: date,
        *,
        expected_revision: int,
        expected_action_digest: str,
    ) -> None:
        if self.controller is not None:
            self.controller.complete_kzn(
                action_id,
                day,
                expected_revision=expected_revision,
                expected_action_digest=expected_action_digest,
            )

    def on_request_draft(
        self,
        prm_id: str,
        *,
        project: Path | None,
        write: bool,
        force: bool,
        expected_fingerprint: str | None = None,
    ) -> None:
        if self.controller is not None:
            self.controller.request_draft(
                prm_id,
                project=project,
                write=write,
                force=force,
                expected_fingerprint=expected_fingerprint,
            )

    def _entry_value(self, widget: object) -> str:
        value = getattr(widget, "get")()
        return str(value).strip()

    def _entry_day(self) -> date | None:
        raw = self._entry_value(self.date_entry)
        try:
            return date.fromisoformat(raw)
        except ValueError:
            self.show_error("対象日は YYYY-MM-DD 形式で入力してください。")
            return None

    def _on_load_from_entry(self) -> None:
        day = self._entry_day()
        if day is not None:
            self.on_load(day)

    def _on_refresh_from_entry(self) -> None:
        day = self._entry_day()
        if day is not None:
            self.on_refresh(day)

    def _on_cancel_refresh(self) -> None:
        if self.controller is not None:
            self.controller.cancel_refresh()

    def _current_day_revision(self) -> tuple[date, int] | None:
        if self._snapshot is None:
            self.show_error("先に対象日を表示してください。")
            return None
        return self._snapshot.day, self._snapshot.human.revision

    def _operation_id_for(
        self,
        day: date,
        kind: str,
        payload: dict[str, object],
        target_key: str | None = None,
        cancel_operation_id: str | None = None,
    ) -> str:
        fingerprint = json.dumps(
            {"payload": payload, "target_key": target_key, "cancel_operation_id": cancel_operation_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        cache_key = (day.isoformat(), kind)
        cached = self._operation_ids.get(cache_key)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        operation_id = f"gui-{kind}-{uuid4().hex}"
        self._operation_ids[cache_key] = (fingerprint, operation_id)
        return operation_id

    def _on_outcome(self) -> None:
        target = self._current_day_revision()
        text = self._entry_value(self.outcome_entry)
        if target is None or not text:
            if not text:
                self.show_error("成果を1行入力してください。")
            return
        day, revision = target
        payload = {"text": text}
        operation = HumanOperation(
            self._operation_id_for(day, "outcome", payload),
            day,
            "outcome",
            payload,
            None,
            None,
        )
        self.on_submit_human(operation, expected_revision=revision)

    def _on_choose_candidate(self) -> None:
        label = self._entry_value(self.candidate_combo)
        action = self._candidate_by_label.get(label)
        if action is None:
            self.show_error("候補を選択してください。")
            return
        self._selected_candidate = action if self._is_completion_candidate(action) else None
        self._set_completion_visibility(self._selected_candidate is not None)
        target = self._current_day_revision()
        if target is None:
            return
        day, revision = target
        source = getattr(action, "source", "")
        if source == "free_text":
            self.show_free_text_outcome_prompt()
            return
        action_id = getattr(action, "source_id", None)
        digest = getattr(action, "entry_digest", None)
        if not action_id or not digest:
            self.show_error("KZN候補のIDまたはdigestが未確認です。")
            return
        payload = {"text": getattr(action, "text", ""), "source": "kzn", "source_id": action_id}
        self.on_submit_human(
            HumanOperation(
                self._operation_id_for(day, "next_action", payload), day, "next_action",
                payload, None, None,
            ),
            expected_revision=revision,
        )

    def _on_choose_free_text(self) -> None:
        text = self._entry_value(self.free_text_entry)
        if not text:
            self.show_error("自由文の候補を入力してください。")
            return
        self._selected_candidate = None
        self._set_completion_visibility(False)
        target = self._current_day_revision()
        if target is None:
            return
        day, revision = target
        payload = {"text": text, "source": "free_text", "source_id": None}
        self.on_submit_human(
            HumanOperation(self._operation_id_for(day, "next_action", payload), day, "next_action", payload, None, None),
            expected_revision=revision,
        )
        self.show_free_text_outcome_prompt()

    def _on_complete_candidate(self) -> None:
        action = self._selected_candidate
        target = self._current_day_revision()
        if target is None or action is None or not self._completion_visible or not self._is_completion_candidate(action):
            self.show_error("完了するKZN候補を選択してください。")
            return
        action_id = getattr(action, "source_id", None)
        digest = getattr(action, "entry_digest", None)
        if not action_id or not digest:
            self.show_error("KZN候補のIDまたはdigestが未確認です。")
            return
        day, revision = target
        self.on_complete_kzn(action_id, day, expected_revision=revision, expected_action_digest=digest)

    def _on_correction(self) -> None:
        target = self._current_day_revision()
        label = self._entry_value(self.correction_combo)
        selected = self._correction_by_label.get(label)
        category = self._entry_value(self.category_entry)
        project = self._entry_value(self.project_entry)
        if target is None or selected is None:
            self.show_error("訂正対象を選択してください。")
            return
        if not category and not project:
            self.show_error("カテゴリまたはプロジェクトを入力してください。")
            return
        day, revision = target
        payload: dict[str, object] = {}
        if category:
            payload["category"] = category
        if project:
            payload["project"] = project
        operation = HumanOperation(
            self._operation_id_for(day, "classification_correction", payload, getattr(selected, "stable_key", None)),
            day,
            "classification_correction",
            payload,
            getattr(selected, "stable_key", None),
            None,
        )
        self.on_submit_human(operation, expected_revision=revision)

    def _on_cancel_human(self) -> None:
        target = self._current_day_revision()
        operation_id = self._entry_value(self.cancel_combo)
        if target is None or not operation_id:
            self.show_error("取消対象の操作IDを選択してください。")
            return
        day, revision = target
        operation = HumanOperation(
            self._operation_id_for(day, "cancel", {}, None, operation_id), day, "cancel", {}, None, operation_id
        )
        self.on_submit_human(operation, expected_revision=revision)

    def _on_project_human(self) -> None:
        target = self._current_day_revision()
        if target is not None:
            day, revision = target
            self.on_project_human(day, expected_revision=revision)

    def _is_completion_candidate(self, action: object) -> bool:
        return (
            getattr(action, "source", None) == "kzn"
            and bool(getattr(action, "source_id", None))
            and bool(getattr(action, "entry_digest", None))
            and bool(getattr(action, "selectable", False))
            and bool(getattr(action, "completable", False))
            and getattr(action, "status", None) == "available"
        )

    def _set_completion_visibility(self, visible: bool) -> None:
        self._completion_visible = bool(visible)
        button = getattr(self, "_complete_button", None)
        if button is None:
            return
        if self._completion_visible:
            button.grid(**getattr(self, "_completion_grid", {"row": 5, "column": 3, "sticky": "w"}))
        else:
            button.grid_remove()
        state = "normal" if self._buttons_enabled and self._completion_visible else "disabled"
        button.configure(state=state)

    def _toggle_force_draft(self) -> None:
        self._force_draft = not self._force_draft

    def _on_draft(self, write: bool) -> None:
        prm_id = self._entry_value(self.prm_entry)
        project_raw = self._entry_value(self.project_path_entry)
        if not prm_id:
            self.show_error("PRM-IDを入力してください。")
            return
        project = Path(project_raw) if project_raw else None
        self.on_request_draft(prm_id, project=project, write=write, force=self._force_draft)

    def _on_sample(self) -> None:
        if self.controller is not None:
            self.controller.show_sample_preview(None)

    def _on_journal_preview(self) -> None:
        target = self._snapshot.day if self._snapshot is not None else self._entry_day()
        if self.controller is not None:
            self.controller.preview_journal(target)

    def _on_journal_execute(self) -> None:
        if self._journal_preview is None:
            self.show_error("先に日誌の予定を表示してください。")
            return
        if self.controller is not None:
            self.controller.execute_journal(self._journal_preview)

    def _on_journal_open(self) -> None:
        target = self._snapshot.day if self._snapshot is not None else self._entry_day()
        if target is not None and self.controller is not None:
            self.controller.open_daily_note(target)

    def _on_report_preview(self) -> None:
        target = self._snapshot.day if self._snapshot is not None else self._entry_day()
        if self.controller is not None:
            self.controller.preview_report(target)

    def _on_report_execute(self) -> None:
        if self._report_preview is None:
            self.show_error("先に日報の予定を表示してください。")
            return
        if self.controller is not None:
            self.controller.execute_report(self._report_preview)

    def _setup_values(self) -> tuple[SetupOptions, dict[str, object]] | None:
        config_raw = self._entry_value(self.setup_config_entry)
        vault_raw = self._entry_value(self.setup_vault_entry)
        if not config_raw or not vault_raw:
            self.show_error("設定ファイルとVaultのフルパスを入力してください。")
            return None
        fmt = self._entry_value(self.setup_format_combo) or "YYYY-MM-DD"
        kwargs = {
            "daily_notes_dir": self._entry_value(self.setup_notes_entry) or None,
            "daily_note_format": fmt,
            "backend": self._entry_value(self.setup_backend_entry) or "none",
            "model": self._entry_value(self.setup_model_entry) or "",
        }
        return SetupOptions(config_path=Path(config_raw), vault=Path(vault_raw)), kwargs

    def _on_setup_preview(self) -> None:
        values = self._setup_values()
        if values is not None and self.controller is not None:
            opts, kwargs = values
            self.controller.preview_initial_setup(opts, **kwargs)

    def _on_setup_save(self) -> None:
        if self._setup_preview is None:
            self.show_error("先に初期設定の予定を表示してください。")
            return
        values = self._setup_values()
        if values is not None and self.controller is not None:
            opts, kwargs = values
            self.controller.save_initial_setup(opts, self._setup_preview, **kwargs)

    def _set_text(self, value: str) -> None:
        self._text = value
        widget = self._text_widget
        if widget is not None:
            try:
                widget.delete("1.0", "end")
                widget.insert("1.0", value)
            except Exception:
                pass

    def render_text(self, value: str | None = None) -> str:
        return self._text if value is None else str(value)

    def show_snapshot(self, snapshot: HomeSnapshot) -> None:
        self._snapshot = snapshot
        cancelled_ids: set[str] = set()
        history_ids: set[str] = set()
        latest_active_by_kind: dict[str, str] = {}
        for record in getattr(snapshot.human, "history", ()):
            operation = getattr(record, "operation", None)
            operation_id = getattr(operation, "operation_id", None)
            if operation_id:
                history_ids.add(operation_id)
            if not getattr(record, "active", False) and operation_id:
                cancelled_ids.add(operation_id)
            if getattr(record, "active", False) and operation_id:
                operation_kind = getattr(operation, "kind", None)
                if operation_kind:
                    latest_active_by_kind[str(operation_kind)] = operation_id
            if getattr(operation, "kind", None) == "cancel":
                target_id = getattr(operation, "cancel_operation_id", None)
                if target_id:
                    cancelled_ids.add(target_id)
        for cache_key, (_, cached_id) in tuple(self._operation_ids.items()):
            if cached_id in cancelled_ids:
                self._operation_ids.pop(cache_key, None)
                continue
            latest_id = latest_active_by_kind.get(cache_key[1])
            if latest_id is not None and cached_id in history_ids and cached_id != latest_id:
                self._operation_ids.pop(cache_key, None)
        eligible = [action for action in snapshot.next_actions if self._is_completion_candidate(action)]
        self._selected_candidate = eligible[0] if len(eligible) == 1 else None
        self._set_completion_visibility(self._selected_candidate is not None)
        if hasattr(self, "date_entry"):
            self.date_entry.delete(0, "end")
            self.date_entry.insert(0, snapshot.day.isoformat())
        path = str(snapshot.saved.path) if snapshot.saved.path is not None else "保存先未解決"
        measurement = self._STATUS.get(snapshot.measurements.status, snapshot.measurements.status)
        stats = self._STATUS.get(snapshot.stats_status, snapshot.stats_status)
        if hasattr(self, "path_label"):
            self.path_label.configure(text=f"保存先: {path}")
            self.status_label.configure(text=f"状態: 日誌={snapshot.saved.status} / 統計={stats} / 計測={measurement}")
        self._candidate_by_label = {}
        candidate_labels = []
        for action in snapshot.next_actions:
            source_id = getattr(action, "source_id", None) or "自由文"
            label = f"{source_id}: {getattr(action, 'text', '')}"
            self._candidate_by_label[label] = action
            candidate_labels.append(label)
        if hasattr(self, "candidate_combo"):
            self.candidate_combo.configure(values=tuple(candidate_labels))
        self._correction_by_label = {}
        if hasattr(self, "cancel_combo"):
            active_ids = []
            for record in getattr(snapshot.human, "history", ()):
                if getattr(record, "active", False):
                    operation_id = getattr(getattr(record, "operation", None), "operation_id", None)
                    if operation_id:
                        active_ids.append(operation_id)
            self.cancel_combo.configure(values=tuple(active_ids))
        lines = [
            f"対象日: {snapshot.day.isoformat()}",
            f"日誌保存先: {path}",
            f"日誌: {snapshot.saved.status}",
            f"統計: {stats}",
            f"計測: {measurement}（{snapshot.measurements.source}）",
            f"次に試す候補: {len(snapshot.next_actions)}件",
        ]
        if snapshot.measurements.status != "observed":
            lines.append(f"次の手順: ActivityWatchを確認してから更新してください。理由: {snapshot.measurements.reason or '未確認'}")
        if snapshot.recovery.status not in {"none", "success"}:
            lines.append(f"復旧状態: {snapshot.recovery.status}（{snapshot.recovery.message or '確認が必要です'}）")
        if snapshot.saved.content:
            lines.extend(("", "保存済み日誌:", snapshot.saved.content))
        lines.extend(("", f"本人入力 revision: {snapshot.human.revision}", f"本人入力投影: {getattr(snapshot.human, 'projection_state', 'unknown')}"))
        human_reason = getattr(snapshot.human, "reason", None)
        if human_reason:
            lines.append(f"本人入力の状態: {human_reason}")
        for action in snapshot.next_actions:
            lines.append(f"候補: {getattr(action, 'source_id', None) or '自由文'} / {getattr(action, 'text', '')} / 出自={getattr(action, 'source', 'unknown')}")
        if self._result_feedback:
            lines.extend(("", self._result_feedback))
        self._set_text("\n".join(lines))

    def show_pending(self, operation: str) -> None:
        self._pending = operation
        if hasattr(self, "status_label"):
            self.status_label.configure(text=f"状態: {operation} を実行中")

    def show_result(self, result: ControllerResult) -> None:
        if result.operation == "journal_preview" and isinstance(result.payload, JournalPreview):
            self._journal_preview = result.payload
        elif result.operation == "report_preview" and isinstance(result.payload, ReportPreview):
            self._report_preview = result.payload
        elif result.operation == "setup_preview" and isinstance(result.payload, SetupPreview):
            self._setup_preview = result.payload
        if result.error:
            self.show_error(result.error)
        elif result.payload is not None:
            status = getattr(result.payload, "status", None)
            if status is not None:
                label = self._RESULT_STATUS.get(str(status), str(status))
                if getattr(result.payload, "projection_state", None) == "pending":
                    label = "保存済み／日誌未反映"
                details = self._result_details(result.payload, operation=result.operation)
                feedback = f"処理結果: {label}" + ("\n" + "\n".join(details) if details else "")
                self._result_feedback = feedback
                self._set_text(self._text + f"\n\n{feedback}")

    def _result_details(self, payload: object, *, operation: str | None = None) -> list[str]:
        lines: list[str] = []
        target_day = getattr(payload, "target_day", None)
        if target_day is not None:
            lines.append(f"対象日: {target_day.isoformat() if hasattr(target_day, 'isoformat') else target_day}")
        source_state = getattr(payload, "source_status", None)
        if source_state is None:
            source_state = getattr(getattr(payload, "snapshot", None), "source_status", None)
        source_status = getattr(source_state, "status", source_state)
        source_reason = getattr(source_state, "reason", None)
        if source_status is not None:
            lines.append(f"source status: {source_status}" + (f"（{source_reason}）" if source_reason else ""))
        snapshot_digest = getattr(payload, "snapshot_digest", None)
        if snapshot_digest is None:
            snapshot_digest = getattr(payload, "_snapshot_digest", None)
        if snapshot_digest is None:
            snapshot_digest = getattr(getattr(payload, "snapshot", None), "source_digest", None)
        if snapshot_digest is None and operation == "report_execute" and self._report_preview is not None:
            snapshot_digest = getattr(self._report_preview, "_snapshot_digest", None)
            if snapshot_digest is None:
                snapshot_digest = getattr(getattr(self._report_preview, "snapshot", None), "source_digest", None)
        if snapshot_digest:
            lines.append(f"snapshot_digest（詳細）: {snapshot_digest}")
        operation_id = getattr(payload, "operation_id", None)
        if operation_id:
            lines.append(f"操作ID: {operation_id}")
        revision = getattr(payload, "revision", None)
        if revision is None:
            revision = getattr(payload, "human_revision", None)
        if revision is not None:
            lines.append(f"revision: {revision}")
        projection_state = getattr(payload, "projection_state", None)
        if projection_state is not None:
            lines.append(f"日誌反映: {projection_state}")
        for field, title in (("entry_id", "KZN ID"), ("done_date", "完了日"), ("memory_status", "Memory"), ("note_status", "日誌")):
            value = getattr(payload, field, None)
            if value is not None:
                lines.append(f"{title}: {value}")
        path = getattr(payload, "path", None)
        if path is not None:
            lines.append(f"保存先: {path}")
        for field, title in (("config_path", "設定ファイル"), ("written_path", "書込先"), ("human_path", "本人入力"), ("note_path", "日誌")):
            value = getattr(payload, field, None)
            if value is not None:
                lines.append(f"{title}: {value}")
        for field, title in (("written_paths", "書込予定"), ("writes", "書込予定"), ("applied_updates", "適用済み"), ("pending_updates", "保留"), ("precondition_failures", "事前条件失敗")):
            values = getattr(payload, field, None)
            if values:
                rows = []
                for item in values:
                    item_target = getattr(item, "target", None)
                    item_path = getattr(item, "path", None)
                    item_reason = getattr(item, "reason", None)
                    item_action = getattr(item, "action", None)
                    item_expected_revision = getattr(item, "expected_revision", None)
                    item_expected_digest = getattr(item, "expected_digest", None)
                    item_observed_revision = getattr(item, "observed_revision", None)
                    item_observed_digest = getattr(item, "observed_digest", None)
                    detail = f"{item_target or '対象'} / {item_path or item_reason or '対象'}"
                    if item_action:
                        detail += f" / {item_action}"
                    if item_expected_revision is not None:
                        detail += f" / expected_revision={item_expected_revision}"
                    if item_expected_digest:
                        detail += f" / expected_digest={item_expected_digest}"
                    if item_observed_revision is not None:
                        detail += f" / observed_revision={item_observed_revision}"
                    if item_observed_digest:
                        detail += f" / observed_digest={item_observed_digest}"
                    rows.append(detail)
                lines.append(f"{title}: {', '.join(rows)}")
        preconditions = getattr(payload, "preconditions", None)
        if preconditions:
            rows = []
            for item in preconditions:
                target = getattr(item, "target", "対象")
                path = getattr(item, "path", None) or "パス未確認"
                expected_revision = getattr(item, "expected_revision", None)
                expected_digest = getattr(item, "expected_digest", None)
                detail = f"{target} / {path}"
                if expected_revision is not None:
                    detail += f" / expected_revision={expected_revision}"
                if expected_digest:
                    detail += f" / expected_digest={expected_digest}"
                rows.append(detail)
            lines.append(f"preconditions（詳細）: {'; '.join(rows)}")
        diagnostics = getattr(payload, "diagnostics", None)
        if diagnostics:
            lines.append(f"診断: {' / '.join(str(item) for item in diagnostics)}")
        reason = getattr(payload, "reason", None) or getattr(payload, "error", None)
        if reason:
            lines.append(f"理由: {reason}")
        content = getattr(payload, "content", None)
        if content:
            lines.extend(("内容:", str(content)))
        snapshot = getattr(payload, "snapshot", None)
        if snapshot is not None:
            rendered = getattr(snapshot, "rendered_markdown", None)
            if rendered:
                lines.extend(("生成予定本文:", str(rendered)))
        planned = getattr(payload, "planned_updates", None)
        if planned:
            planned_rows = []
            for item in planned:
                item_path = getattr(item, "path", None)
                action = getattr(item, "action", None)
                reason = getattr(item, "reason", None)
                planned_rows.append(f"{item_path or '対象'} / {action or '操作'} / {reason or '予定'}")
            lines.append(f"予定変更: {'; '.join(planned_rows)}")
        return lines

    def show_error(self, message: str) -> None:
        self._error = message
        self._set_text(self._text + f"\n\nエラー: {message}")

    def ask_confirmation(self, title: str, message: str) -> bool:
        if self.root is None:
            return False
        from tkinter import messagebox

        return bool(messagebox.askyesno(title, message, parent=cast(tk.Misc, self.root)))

    def set_buttons_enabled(self, enabled: bool) -> None:
        self._buttons_enabled = enabled
        state = "normal" if enabled else "disabled"
        for button in self._buttons:
            if button in self._always_enabled_buttons:
                continue
            try:
                if button in self._setup_controls and not self._setup_available:
                    button.configure(state="disabled")
                    continue
                if button is getattr(self, "_complete_button", None):
                    button.configure(state="normal" if enabled and self._completion_visible else "disabled")
                else:
                    button.configure(state=state)
            except Exception:
                pass

    def show_setup_entry(self) -> None:
        # Bootstrap keeps the fixture journal readable while adding the setup
        # guidance.  The actual four-field form lives on the persistent
        # ``初期設定`` notebook page built above, so a missing config never
        # replaces the sample with an otherwise empty setup message.
        guidance = "設定がありません。初期設定タブで設定を入力して保存すると、保存済みデータを読み込みます。"
        self._set_text((self._text + "\n\n" if self._text else "") + guidance)

    def show_sample_preview(self, sample: SampleJournal) -> None:
        self._set_text(f"架空のサンプル日誌（{sample.fixture_id}）\n\n{sample.markdown}")

    def show_free_text_outcome_prompt(self) -> None:
        self._set_text(self._text + "\n\n自由文の候補は完了扱いにせず、成果を1行で記録できます。")

    def show_kzn_completion_confirmation(self, action_id: str, text: str, entry_digest: str) -> bool:
        return self.ask_confirmation("KZN完了の確認", f"ID: {action_id}\n内容: {text}\ndigest: {entry_digest}\n完了として記録しますか？")

    def show_correction_targets(self, targets: object) -> None:
        rows = []
        self._correction_by_label = {}
        for target in targets:
            label = f"{getattr(target, 'display_label', '対象')}（{getattr(target, 'privacy', 'unknown')}）"
            rows.append(label)
            self._correction_by_label[label] = target
        if hasattr(self, "correction_combo"):
            self.correction_combo.configure(values=tuple(rows))
        self._set_text(self._text + "\n\n分類訂正の対象:\n" + ("\n".join(rows) or "対象なし"))


def _parse_cli_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--date は YYYY-MM-DD 形式で指定してください") from exc


def _main_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kaizenlog-home",
        description="KaizenLog の日誌ホーム画面",
        add_help=False,
    )
    parser.add_argument("--help", action="store_true", help="このヘルプを表示して終了")
    parser.add_argument("--config", metavar="PATH", help="使用する設定ファイル")
    parser.add_argument("--date", type=_parse_cli_day, metavar="YYYY-MM-DD", help="表示する対象日")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Launch the Home surface with real service ports and delayed activation."""

    parser = _main_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.help:
        parser.print_help()
        return 0

    config_path: Path | None = None
    config_error: Exception | None = None
    try:
        config_path = find_config_file(args.config)
    except Exception as exc:
        config_error = exc

    try:
        root = tk.Tk()
    except Exception:
        return 1

    executor: ThreadPoolExecutor | None = None
    controller: HomeController | None = None
    result_code = 1 if config_error is not None else 0
    activation_pending = False
    activated = False

    try:
        executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="kaizenlog-home")
        view = HomeView(root)

        def activate(path_hint: Path | None = None) -> None:
            nonlocal activation_pending, activated, result_code
            activation_pending = False
            if activated or controller is None or controller._closed:
                return
            try:
                resolved = find_config_file(str(path_hint) if path_hint is not None else args.config)
                if resolved is None:
                    view.show_error("設定ファイルが見つかりません。初期設定を保存してください。")
                    result_code = 1
                    return
                cfg = load_config(str(resolved))
                zone = ZoneInfo(cfg.timezone)
                day = args.date or datetime.now(zone).date()
            except Exception as exc:
                view.show_error(f"設定を読み込めません: {type(exc).__name__}: {exc}")
                result_code = 1
                return
            controller.cfg = cfg
            controller.service = home_service
            controller.draft = prompt_draft
            view.set_setup_available(False)
            activated = True
            controller.load(day)

        def schedule_activation(path_hint: Path | None = None) -> None:
            nonlocal activation_pending
            if activated or activation_pending or controller is None or controller._closed:
                return
            activation_pending = True
            root.after(0, lambda: activate(path_hint))

        def on_setup_saved(result: SetupSaveResult) -> None:
            if result.status in {"saved", "already_saved"}:
                schedule_activation(Path(result.config_path))

        controller = HomeController(
            None,
            service=None,
            journal=journal_service,
            view=view,
            root=root,
            scheduler=root,
            executor=executor,
            queue=ResultQueue(),
            draft=None,
            on_setup_saved=on_setup_saved,
        )
        view.bind_controller(controller)

        if config_error is not None:
            view.show_error(f"設定を読み込めません: {type(config_error).__name__}: {config_error}")
        elif config_path is None:
            view.show_sample_preview(journal_service.sample_journal(None))
            view.show_setup_entry()
        else:
            schedule_activation(config_path)

        try:
            root.mainloop()
        except Exception as exc:
            view.show_error(f"ホーム画面を開始できません: {type(exc).__name__}: {exc}")
            result_code = 1
    except Exception as exc:
        result_code = 1
        if controller is not None:
            controller.view.show_error(f"ホーム画面を開始できません: {type(exc).__name__}: {exc}")
    finally:
        if controller is not None and not controller._closed:
            try:
                controller.close()
            except Exception:
                result_code = 1
        elif controller is None:
            try:
                # HomeView or HomeController construction may have failed
                # before ownership of the root reached the controller.
                root.destroy()
            except Exception:
                result_code = 1
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                result_code = 1
    return result_code


__all__ = [
    "ControllerResult",
    "DraftServicePort",
    "ExecutorPort",
    "HomeController",
    "HomeServicePort",
    "HomeView",
    "HomeViewPort",
    "JournalServicePort",
    "main",
    "ResultQueue",
    "SchedulerPort",
    "TkRootPort",
]
