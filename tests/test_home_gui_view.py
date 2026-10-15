from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import kaizenlog.home_gui as home_gui
from kaizenlog.home_gui import HomeView
from kaizenlog.config import Config
from kaizenlog.human_input import apply_human_operation


def test_view_action_callbacks_reach_controller_methods_without_internal_dataclass_dump() -> None:
    calls: list[tuple[str, object]] = []

    class Controller:
        def request_refresh(self, day=None):
            calls.append(("refresh", day))

        def load(self, day):
            calls.append(("load", day))

        def close(self):
            calls.append(("close", None))

    view = HomeView(None, controller=Controller())
    view.on_refresh(date(2026, 10, 15))
    view.on_close()

    assert calls == [("refresh", date(2026, 10, 15)), ("close", None)]
    assert "HomeView" not in view.render_text("saved path")


def test_bootstrap_sample_remains_visible_when_setup_guidance_is_shown() -> None:
    view = HomeView(None)
    view.show_sample_preview(SimpleNamespace(fixture_id="bootstrap", markdown="サンプル本文"))
    view.show_setup_entry()

    rendered = view.render_text()
    assert "サンプル本文" in rendered
    assert "設定がありません" in rendered
    assert "初期設定タブ" in rendered


class FakeWidget:
    def __init__(self, master=None, **kwargs):
        self.master = master
        self.kwargs = kwargs
        self.command = kwargs.get("command")
        self.value = kwargs.get("text", "")
        self.values = kwargs.get("values", ())
        self.state = "normal"
        self.visible = True
        self.children = []
        if master is not None and hasattr(master, "children"):
            master.children.append(self)

    def grid(self, **kwargs):
        self.grid_kwargs = kwargs
        self.visible = True

    def grid_remove(self):
        self.visible = False

    def columnconfigure(self, *args, **kwargs):
        pass

    def rowconfigure(self, *args, **kwargs):
        pass

    def configure(self, **kwargs):
        self.kwargs.update(kwargs)
        if "state" in kwargs:
            self.state = kwargs["state"]
        if "values" in kwargs:
            self.values = kwargs["values"]

    config = configure

    def insert(self, index, value):
        self.value = value

    def delete(self, *args):
        self.value = ""

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeText(FakeWidget):
    def insert(self, index, value):
        self.value = value


class FakeNotebook(FakeWidget):
    def add(self, child, **kwargs):
        self.tabs = getattr(self, "tabs", [])
        self.tabs.append((child, kwargs))

    def hide(self, child):
        self.hidden = getattr(self, "hidden", [])
        self.hidden.append(child)


class FakeRoot:
    def __init__(self):
        self.children = []
        self.protocols = {}

    def title(self, value):
        self.window_title = value

    def protocol(self, name, command):
        self.protocols[name] = command

    def columnconfigure(self, *args, **kwargs):
        pass

    def rowconfigure(self, *args, **kwargs):
        pass


class FakeController:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None

        return record


def test_ttk_controls_are_built_and_button_commands_reach_controller(monkeypatch) -> None:
    fake_ttk = SimpleNamespace(
        Frame=FakeWidget,
        Notebook=FakeNotebook,
        Label=FakeWidget,
        Entry=FakeWidget,
        Button=FakeWidget,
        Combobox=FakeWidget,
        Checkbutton=FakeWidget,
        Separator=FakeWidget,
    )
    monkeypatch.setattr(home_gui, "ttk", fake_ttk)
    monkeypatch.setattr(home_gui.tk, "Text", FakeText)
    root = FakeRoot()
    controller = FakeController()
    view = HomeView(root, controller=controller)

    commands = [widget.command for widget in _walk(root) if callable(getattr(widget, "command", None))]
    assert len(commands) >= 12
    view.date_entry.value = "2026-10-15"
    view._journal_preview = object()
    view._report_preview = object()
    view._refresh_button.command()
    view._cancel_button.command()
    view._close_button.command()
    view._sample_button.command()
    view._journal_preview_button.command()
    view._journal_execute_button.command()
    view._journal_open_button.command()
    view._report_preview_button.command()
    view._report_execute_button.command()

    called = [name for name, _, _ in controller.calls]
    assert called[:3] == ["request_refresh", "cancel_refresh", "close"]
    assert {"show_sample_preview", "preview_journal", "execute_journal", "open_daily_note", "preview_report", "execute_report"} <= set(called)


def test_snapshot_updates_candidate_and_human_controls_without_repr(monkeypatch) -> None:
    fake_ttk = SimpleNamespace(
        Frame=FakeWidget,
        Notebook=FakeNotebook,
        Label=FakeWidget,
        Entry=FakeWidget,
        Button=FakeWidget,
        Combobox=FakeWidget,
        Checkbutton=FakeWidget,
        Separator=FakeWidget,
    )
    monkeypatch.setattr(home_gui, "ttk", fake_ttk)
    monkeypatch.setattr(home_gui.tk, "Text", FakeText)
    controller = FakeController()
    view = HomeView(FakeRoot(), controller=controller)
    snapshot = SimpleNamespace(
        day=date(2026, 10, 15),
        saved=SimpleNamespace(path=Path("C:/vault/notes/2026-10-15.md"), status="selected", content="保存済み本文"),
        stats_status="observed",
        measurements=SimpleNamespace(status="observed", source="activitywatch", observed={"active_minutes": 42}, reason=None),
        next_actions=(SimpleNamespace(source="kzn", source_id="KZN-20261015-001", entry_digest="digest", text="候補", selectable=True, completable=True, status="available"),),
        human=SimpleNamespace(revision=3, history=()),
        recovery=SimpleNamespace(status="none", source_status=None, message=None),
    )
    view.show_snapshot(snapshot)
    assert str(Path("C:/vault/notes/2026-10-15.md")) in view.render_text()
    assert "保存済み本文" in view.render_text()
    assert "HomeSnapshot" not in view.render_text()
    assert tuple(view.candidate_combo.values) == ("KZN-20261015-001: 候補",)


def test_setup_controls_are_bootstrap_only_and_hidden_after_activation(monkeypatch) -> None:
    fake_ttk = SimpleNamespace(
        Frame=FakeWidget,
        Notebook=FakeNotebook,
        Label=FakeWidget,
        Entry=FakeWidget,
        Button=FakeWidget,
        Combobox=FakeWidget,
        Checkbutton=FakeWidget,
        Separator=FakeWidget,
    )
    monkeypatch.setattr(home_gui, "ttk", fake_ttk)
    monkeypatch.setattr(home_gui.tk, "Text", FakeText)
    view = HomeView(FakeRoot(), controller=FakeController())

    assert all(widget.state == "normal" for widget in view._setup_controls)
    view.set_setup_available(False)

    assert all(widget.state == "disabled" for widget in view._setup_controls)
    assert view._setup_page in view._setup_notebook.hidden


def _walk(widget):
    yield widget
    for child in getattr(widget, "children", ()):
        yield from _walk(child)


def test_human_and_setup_draft_controls_use_typed_controller_and_real_validator(tmp_path, monkeypatch) -> None:
    fake_ttk = SimpleNamespace(
        Frame=FakeWidget,
        Notebook=FakeNotebook,
        Label=FakeWidget,
        Entry=FakeWidget,
        Button=FakeWidget,
        Combobox=FakeWidget,
        Checkbutton=FakeWidget,
        Separator=FakeWidget,
    )
    monkeypatch.setattr(home_gui, "ttk", fake_ttk)
    monkeypatch.setattr(home_gui.tk, "Text", FakeText)
    config = Config(vault_dir=tmp_path)

    class TypedController:
        def __init__(self):
            self.human_calls = []
            self.human_results = []
            self.other_calls = []

        def submit_human(self, operation, *, expected_revision):
            self.human_calls.append((operation, expected_revision))
            self.human_results.append(apply_human_operation(config, operation.target_day, operation, expected_revision=expected_revision))

        def complete_kzn(self, *args, **kwargs):
            self.other_calls.append(("complete_kzn", args, kwargs))

        def project_human(self, *args, **kwargs):
            self.other_calls.append(("project_human", args, kwargs))

        def request_draft(self, *args, **kwargs):
            self.other_calls.append(("request_draft", args, kwargs))

        def preview_initial_setup(self, *args, **kwargs):
            self.other_calls.append(("preview_initial_setup", args, kwargs))

        def save_initial_setup(self, *args, **kwargs):
            self.other_calls.append(("save_initial_setup", args, kwargs))

        def cancel_refresh(self):
            self.other_calls.append(("cancel_refresh", (), {}))

        def close(self):
            self.other_calls.append(("close", (), {}))

    controller = TypedController()
    view = HomeView(FakeRoot(), controller=controller)
    view.ask_confirmation = lambda title, message: True
    snapshot = SimpleNamespace(
        day=date(2026, 10, 15),
        saved=SimpleNamespace(path=tmp_path / "2026-10-15.md", status="missing", content=None),
        stats_status="missing",
        measurements=SimpleNamespace(status="unknown", source="activitywatch", observed={}, reason="未更新"),
        next_actions=(SimpleNamespace(source="kzn", source_id="KZN-20261015-001", entry_digest="digest", text="候補", selectable=True, completable=True, status="available"),),
        human=SimpleNamespace(revision=0, history=(), projection_state="none", reason=None),
        recovery=SimpleNamespace(status="none", source_status=None, message=None),
    )
    view.show_snapshot(snapshot)

    view.outcome_entry.value = "成果A"
    view._outcome_button.command()
    first_id, first_revision = controller.human_calls[-1]
    assert dict(first_id.payload) == {"text": "成果A"}
    assert controller.human_results[-1].status == "saved"
    snapshot.human.revision = 1
    view.show_snapshot(snapshot)
    view._outcome_button.command()
    assert controller.human_calls[-1][0].operation_id == first_id.operation_id
    view.outcome_entry.value = "成果B"
    view._outcome_button.command()
    assert controller.human_calls[-1][0].operation_id != first_id.operation_id
    assert dict(controller.human_calls[-1][0].payload) == {"text": "成果B"}

    view.free_text_entry.value = "自由文候補"
    view._choose_free_text_button.command()
    free_operation = controller.human_calls[-1][0]
    assert dict(free_operation.payload) == {"text": "自由文候補", "source": "free_text", "source_id": None}
    assert free_operation.kind == "next_action"
    assert controller.human_calls[-1][1] == 1
    view._choose_free_text_button.command()
    assert controller.human_calls[-1][0].operation_id == free_operation.operation_id
    view.candidate_combo.value = "KZN-20261015-001: 候補"
    view._choose_candidate_button.command()
    assert controller.human_calls[-1][0].payload["source"] == "kzn"
    view._complete_button.command()
    assert controller.other_calls[-1][0] == "complete_kzn"

    target = SimpleNamespace(stable_key="target-1", display_label="Code", privacy="public")
    label = "Code（public）"
    view._correction_by_label[label] = target
    view.correction_combo.value = label
    view.category_entry.value = "開発"
    view.project_entry.value = ""
    view._correction_button.command()
    assert dict(controller.human_calls[-1][0].payload) == {"category": "開発"}
    view.cancel_combo.value = first_id.operation_id
    view._cancel_human_button.command()
    assert controller.human_calls[-1][0].kind == "cancel"
    assert dict(controller.human_calls[-1][0].payload) == {}
    assert controller.human_calls[-1][0].cancel_operation_id == first_id.operation_id
    view._project_button.command()
    assert controller.other_calls[-1] == ("project_human", (date(2026, 10, 15), 1), {})

    view.prm_entry.value = "PRM-20261015-001"
    view.project_path_entry.value = str(tmp_path)
    view._draft_preview_button.command()
    view._draft_write_button.command()
    assert [name for name, _, _ in controller.other_calls if name == "request_draft"]

    view.setup_config_entry.value = str(tmp_path / "config.toml")
    view.setup_vault_entry.value = str(tmp_path)
    view.setup_notes_entry.value = "notes"
    view.setup_format_combo.value = "YYYY-MM-DD"
    view.setup_backend_entry.value = "none"
    view.setup_model_entry.value = "fixture"
    view._setup_preview_button.command()
    view._setup_preview = SimpleNamespace(config_path=tmp_path / "config.toml")
    view._setup_save_button.command()
    setup_calls = [name for name, _, _ in controller.other_calls if name in {"preview_initial_setup", "save_initial_setup"}]
    assert setup_calls == ["preview_initial_setup", "save_initial_setup"]


def test_operation_ids_track_latest_intent_and_cancelled_reactivation(tmp_path, monkeypatch) -> None:
    fake_ttk = SimpleNamespace(
        Frame=FakeWidget,
        Notebook=FakeNotebook,
        Label=FakeWidget,
        Entry=FakeWidget,
        Button=FakeWidget,
        Combobox=FakeWidget,
        Checkbutton=FakeWidget,
        Separator=FakeWidget,
    )
    monkeypatch.setattr(home_gui, "ttk", fake_ttk)
    monkeypatch.setattr(home_gui.tk, "Text", FakeText)
    config = Config(vault_dir=tmp_path)

    class TypedController:
        def __init__(self):
            self.human_calls = []
            self.human_results = []

        def submit_human(self, operation, *, expected_revision):
            self.human_calls.append((operation, expected_revision))
            self.human_results.append(
                apply_human_operation(config, operation.target_day, operation, expected_revision=expected_revision)
            )

    controller = TypedController()
    view = HomeView(FakeRoot(), controller=controller)
    view.ask_confirmation = lambda title, message: True
    snapshot = SimpleNamespace(
        day=date(2026, 10, 15),
        saved=SimpleNamespace(path=tmp_path / "2026-10-15.md", status="missing", content=None),
        stats_status="missing",
        measurements=SimpleNamespace(status="unknown", source="fixture", observed={}, reason="未更新"),
        next_actions=(),
        human=SimpleNamespace(revision=0, history=(), projection_state="none", reason=None),
        recovery=SimpleNamespace(status="none", source_status=None, message=None),
    )
    view.show_snapshot(snapshot)

    view.outcome_entry.value = "A"
    view._outcome_button.command()
    first = controller.human_calls[-1][0]
    snapshot.human.revision = 1
    view.show_snapshot(snapshot)
    view.outcome_entry.value = "A"
    view._outcome_button.command()
    assert controller.human_calls[-1][0].operation_id == first.operation_id
    assert controller.human_results[-1].status == "idempotent"

    view.outcome_entry.value = "B"
    view._outcome_button.command()
    second = controller.human_calls[-1][0]
    snapshot.human.revision = 2
    view.show_snapshot(snapshot)
    view.outcome_entry.value = "A"
    view._outcome_button.command()
    latest_a = controller.human_calls[-1][0]
    assert latest_a.operation_id not in {first.operation_id, second.operation_id}

    snapshot.human.revision = 3
    view.show_snapshot(snapshot)
    view.cancel_combo.value = latest_a.operation_id
    view._cancel_human_button.command()
    cancel = controller.human_calls[-1][0]
    snapshot.human.revision = 4
    snapshot.human.history = (
        SimpleNamespace(operation=latest_a, active=False),
        SimpleNamespace(operation=cancel, active=True),
    )
    view.show_snapshot(snapshot)
    view.outcome_entry.value = "A"
    view._outcome_button.command()
    reactivated = controller.human_calls[-1][0]
    assert reactivated.operation_id not in {first.operation_id, latest_a.operation_id}


def test_completion_button_visibility_follows_authoritative_kzn_candidate(monkeypatch) -> None:
    fake_ttk = SimpleNamespace(
        Frame=FakeWidget,
        Notebook=FakeNotebook,
        Label=FakeWidget,
        Entry=FakeWidget,
        Button=FakeWidget,
        Combobox=FakeWidget,
        Checkbutton=FakeWidget,
        Separator=FakeWidget,
    )
    monkeypatch.setattr(home_gui, "ttk", fake_ttk)
    monkeypatch.setattr(home_gui.tk, "Text", FakeText)
    view = HomeView(FakeRoot(), controller=FakeController())

    def snapshot(actions):
        return SimpleNamespace(
            day=date(2026, 10, 15),
            saved=SimpleNamespace(path=Path("note.md"), status="missing", content=None),
            stats_status="missing",
            measurements=SimpleNamespace(status="unknown", source="fixture", observed={}, reason="未更新"),
            next_actions=tuple(actions),
            human=SimpleNamespace(revision=0, history=(), projection_state="none", reason=None),
            recovery=SimpleNamespace(status="none", source_status=None, message=None),
        )

    eligible = SimpleNamespace(
        source="kzn", source_id="KZN-1", entry_digest="digest-1", text="候補1",
        selectable=True, completable=True, status="available",
    )
    free_text = SimpleNamespace(
        source="free_text", source_id=None, entry_digest=None, text="自由文",
        selectable=True, completable=False, status="available",
    )
    terminal = SimpleNamespace(
        source="kzn", source_id="KZN-1", entry_digest="digest-1", text="完了済み",
        selectable=False, completable=False, status="missing",
    )

    assert view._complete_button.visible is False
    view.show_snapshot(snapshot((eligible,)))
    assert view._complete_button.visible is True
    assert view._completion_visible is True
    view.set_buttons_enabled(False)
    assert view._complete_button.visible is True
    assert view._complete_button.state == "disabled"
    view.show_free_text_outcome_prompt()
    assert view._complete_button.visible is True
    view.set_buttons_enabled(True)
    assert view._complete_button.state == "normal"

    view.show_snapshot(snapshot((free_text,)))
    assert view._complete_button.visible is False
    assert view._selected_candidate is None
    view.show_snapshot(snapshot((eligible,)))
    assert view._complete_button.visible is True
    view.show_snapshot(snapshot((terminal,)))
    assert view._complete_button.visible is False
    assert view._selected_candidate is None
