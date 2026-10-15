from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import Future
from datetime import date

import pytest


class _CliRoot:
    instances: list["_CliRoot"] = []

    def __init__(self):
        self.callbacks: list[tuple[int, object]] = []
        self.controller = None
        self.mainloop_called = False
        self.destroyed = False
        type(self).instances.append(self)

    def after(self, delay_ms, callback):
        token = (delay_ms, callback)
        self.callbacks.append(token)
        return token

    def after_cancel(self, token):
        self.callbacks = [item for item in self.callbacks if item != token]

    def mainloop(self):
        self.mainloop_called = True
        for _ in range(8):
            if self.callbacks:
                index = min(range(len(self.callbacks)), key=lambda i: self.callbacks[i][0])
                _, callback = self.callbacks.pop(index)
                callback()
            for pool in list(_CliExecutor.instances):
                pool.run_all()
            if self.controller is not None and self.controller._snapshot is not None:
                break

    def destroy(self):
        self.destroyed = True


class _CliExecutor:
    instances: list["_CliExecutor"] = []

    def __init__(self, *args, **kwargs):
        self.jobs = []
        type(self).instances.append(self)

    def submit(self, fn):
        future = Future()
        self.jobs.append(fn)
        return future

    def run_all(self):
        while self.jobs:
            self.jobs.pop(0)()

    def shutdown(self, **kwargs):
        self.jobs.clear()


class _CliView:
    instances: list["_CliView"] = []

    def __init__(self, root, *, controller=None):
        self.root = root
        self.controller = controller
        self.snapshots = []
        self.samples = []
        self.errors = []
        self.setup_available = []
        type(self).instances.append(self)

    def bind_controller(self, controller):
        self.controller = controller
        self.root.controller = controller

    def show_snapshot(self, snapshot):
        self.snapshots.append(snapshot)

    def show_sample_preview(self, sample):
        self.samples.append(sample)

    def show_pending(self, operation):
        pass

    def show_result(self, result):
        pass

    def show_error(self, message):
        self.errors.append(message)

    def set_buttons_enabled(self, enabled):
        pass

    def show_setup_entry(self):
        pass

    def set_setup_available(self, available):
        self.setup_available.append(available)

    def show_free_text_outcome_prompt(self):
        pass

    def show_kzn_completion_confirmation(self, action_id, text, entry_digest):
        return False

    def ask_confirmation(self, title, message):
        return False

    def show_correction_targets(self, targets):
        pass


def test_home_branch_bypasses_normal_config_loading(monkeypatch):
    from kaizenlog import cli
    import kaizenlog.home_gui as home_gui

    seen: list[list[str]] = []

    def fake_home(argv):
        seen.append(list(argv))
        return 7

    monkeypatch.setattr(home_gui, "main", fake_home, raising=False)

    def fail_load(*args, **kwargs):
        raise AssertionError("home must route before normal config loading")

    monkeypatch.setattr(cli, "find_config_file", fail_load)
    monkeypatch.setattr(cli, "load_config", fail_load)

    assert cli.main(["home"]) == 7
    assert seen == [[]]


def test_home_passes_explicit_config_then_date_argv(monkeypatch, tmp_path):
    from kaizenlog import cli
    import kaizenlog.home_gui as home_gui

    seen: list[list[str]] = []
    monkeypatch.setattr(home_gui, "main", lambda argv: seen.append(list(argv)) or 0, raising=False)

    config = tmp_path / "kaizenlog.toml"
    assert cli.main(
        ["--config", str(config), "home", "--date", "2026-10-15"]
    ) == 0
    assert seen == [["--config", str(config), "--date", "2026-10-15"]]


def test_home_accepts_subcommand_config_and_keeps_order(monkeypatch, tmp_path):
    from kaizenlog import cli
    import kaizenlog.home_gui as home_gui

    seen: list[list[str]] = []
    monkeypatch.setattr(home_gui, "main", lambda argv: seen.append(list(argv)) or 0, raising=False)
    config = tmp_path / "nested.toml"

    assert cli.main(["home", "--date", "2026-10-15", "--config", str(config)]) == 0
    assert seen == [["--config", str(config), "--date", "2026-10-15"]]


def test_cli_home_reaches_actual_launcher_and_loads_explicit_day(monkeypatch, tmp_path):
    """The public CLI route reaches home_gui.main before normal CLI loading."""

    from kaizenlog import cli
    import kaizenlog.home_gui as home_gui

    _CliRoot.instances.clear()
    _CliExecutor.instances.clear()
    _CliView.instances.clear()
    config = tmp_path / "config.toml"
    vault_dir = str(tmp_path).replace(chr(92), "/")
    config.write_text(
        "[general]\n"
        f'vault_dir = "{vault_dir}"\n'
        'timezone = "Asia/Tokyo"\n',
        encoding="utf-8",
    )

    def fail_normal_cli_loader(*args, **kwargs):
        raise AssertionError("home must bypass cli.py normal config loading")

    monkeypatch.setattr(cli, "find_config_file", fail_normal_cli_loader)
    monkeypatch.setattr(cli, "load_config", fail_normal_cli_loader)
    monkeypatch.setattr(home_gui.tk, "Tk", _CliRoot)
    monkeypatch.setattr(home_gui, "HomeView", _CliView)
    monkeypatch.setattr(home_gui, "ThreadPoolExecutor", _CliExecutor)

    assert cli.main(["home", "--config", str(config), "--date", "2026-10-15"]) == 0
    root = _CliRoot.instances[-1]
    view = _CliView.instances[-1]
    assert root.mainloop_called is True
    assert root.destroyed is True
    assert view.errors == []
    assert view.setup_available == [False]
    assert view.controller.cfg is not None
    assert [snapshot.day for snapshot in view.snapshots] == [date(2026, 10, 15)]


def test_draft_cli_preview_prints_content_and_maps_success(monkeypatch, tmp_path, capsys):
    from kaizenlog import cli
    from kaizenlog.config import Config
    from kaizenlog.prompt_draft import PromptDraftResult

    cfg = Config(vault_dir=tmp_path)
    monkeypatch.setattr(cli, "find_config_file", lambda _path: tmp_path / "config.toml")
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    calls = []

    def fake_draft(received_cfg, prm_id, **kwargs):
        calls.append((received_cfg, prm_id, kwargs))
        return PromptDraftResult("preview", prm_id, None, "draft body", "fp", None, None)

    monkeypatch.setattr("kaizenlog.prompt_draft.draft_prompt", fake_draft)

    assert cli.main(["--config", str(tmp_path / "config.toml"), "prompts", "draft", "PRM-20261015-001"]) == 0
    assert capsys.readouterr().out.splitlines() == ["draft body", "preview"]
    assert calls[0][1:] == ("PRM-20261015-001", {"project": None, "write": False, "force": False})


def test_draft_cli_write_requires_project_and_force_requires_write(monkeypatch, tmp_path, capsys):
    from kaizenlog import cli
    from kaizenlog.config import Config

    cfg = Config(vault_dir=tmp_path)
    monkeypatch.setattr(cli, "find_config_file", lambda _path: tmp_path / "config.toml")
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    calls = []
    monkeypatch.setattr(
        "kaizenlog.prompt_draft.draft_prompt",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert cli.main(["--config", "cfg.toml", "prompts", "draft", "PRM-20261015-001", "--write"]) == 2
    assert cli.main(["--config", "cfg.toml", "prompts", "draft", "PRM-20261015-001", "--force"]) == 2
    assert calls == []
    assert "project" in capsys.readouterr().err


@pytest.mark.parametrize("status", ["conflict", "stale", "invalid", "missing"])
def test_draft_cli_maps_non_success_status_to_two(monkeypatch, tmp_path, status):
    from kaizenlog import cli
    from kaizenlog.config import Config
    from kaizenlog.prompt_draft import PromptDraftResult

    cfg = Config(vault_dir=tmp_path)
    monkeypatch.setattr(cli, "find_config_file", lambda _path: tmp_path / "config.toml")
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    monkeypatch.setattr(
        "kaizenlog.prompt_draft.draft_prompt",
        lambda *args, **kwargs: PromptDraftResult(status, "PRM-20261015-001", None, "", None, None, "reason"),
    )

    assert cli.main(["--config", "cfg.toml", "prompts", "draft", "PRM-20261015-001"]) == 2


def test_draft_cli_unexpected_exception_maps_to_one(monkeypatch, tmp_path):
    from kaizenlog import cli
    from kaizenlog.config import Config

    cfg = Config(vault_dir=tmp_path)
    monkeypatch.setattr(cli, "find_config_file", lambda _path: tmp_path / "config.toml")
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    monkeypatch.setattr("kaizenlog.prompt_draft.draft_prompt", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    assert cli.main(["--config", "cfg.toml", "prompts", "draft", "PRM-20261015-001"]) == 1


def test_draft_cli_rejects_path_like_id_before_writer(monkeypatch, tmp_path):
    from kaizenlog import cli
    from kaizenlog.config import Config

    cfg = Config(vault_dir=tmp_path)
    monkeypatch.setattr(cli, "find_config_file", lambda _path: tmp_path / "config.toml")
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    called = []
    monkeypatch.setattr("kaizenlog.prompt_draft.draft_prompt", lambda *a, **k: called.append(1))

    assert cli.main(["--config", "cfg.toml", "prompts", "draft", "../PRM-20261015-001"]) == 2
    assert called == []
