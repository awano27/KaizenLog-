"""Explicit setup journal generation and no-write diagnosis contracts."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kaizenlog.config import load_config
from kaizenlog.setup import SetupOptions, run_setup


class SilentUI:
    def __init__(self) -> None:
        self.logs: list[str] = []

    def print(self, msg: str = "") -> None:
        self.logs.append(msg)

    def confirm(self, msg: str, default: bool = True) -> bool:
        return default

    def choose(self, msg: str, options: list[str], default: int = 0) -> int:
        return default

    def ask_path(self, msg: str) -> Path:
        raise AssertionError(msg)


def _patch_quiet_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    from kaizenlog.setup_detect import LlmDetection

    monkeypatch.setattr("kaizenlog.setup_detect.detect_llm", lambda **_: LlmDetection(None, None, None, "none", None))
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda *_: ("ok", False))


def _detected_model_and_stopped_aw(monkeypatch: pytest.MonkeyPatch) -> None:
    from kaizenlog.setup_detect import AwDetection, LlmDetection

    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_llm",
        lambda **_: LlmDetection(None, None, ["detected-model"], "openai-compatible", "detected-model"),
    )
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_activitywatch",
        lambda _url: AwDetection(False, Path("C:/synthetic/aw-qt.exe")),
    )
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda *_: ("ok", False))


def test_generate_today_is_explicit_typed_generation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = tmp_path / "config.toml"
    _patch_quiet_setup(monkeypatch)
    calls: list[str] = []
    preview = SimpleNamespace(status="ready", target_day=None, reason=None)
    result = SimpleNamespace(status="generated", target_day=None, error=None)
    monkeypatch.setattr("kaizenlog.journal_service.preview_journal", lambda cfg, day: calls.append("preview") or preview)
    monkeypatch.setattr("kaizenlog.journal_service.execute_journal", lambda cfg, value: calls.append("execute") or result)
    monkeypatch.setattr("kaizenlog.setup.generate_initial_journal", lambda *_: pytest.fail("legacy generator called"))
    monkeypatch.setattr("kaizenlog.setup.oldest_window_event_day", lambda *_: pytest.fail("catch-up probe called"))
    monkeypatch.setattr("kaizenlog.setup.backfill_after_confirmation", lambda *_: pytest.fail("backfill called"))

    code = run_setup(
        SetupOptions(
            config_path=config,
            vault=vault,
            yes=True,
            generate_today=True,
            skip_aw=True,
            skip_task=True,
            skip_skills=True,
        ),
        SilentUI(),
    )

    assert code == 0
    assert calls == ["preview", "execute"]


def test_bare_yes_is_diagnosis_only_and_does_not_create_config_or_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    config = tmp_path / "missing.toml"
    writes: list[str] = []
    tasks: list[str] = []
    monkeypatch.setattr("kaizenlog.setup.write_config_file", lambda *args, **kwargs: writes.append("config"))
    monkeypatch.setattr("kaizenlog.setup._register_daily_with_config", lambda *args, **kwargs: tasks.append("daily") or True)
    monkeypatch.setattr("kaizenlog.setup.register_weekly_task", lambda *args, **kwargs: tasks.append("weekly") or True)

    code = run_setup(
        SetupOptions(config_path=config, yes=True, skip_aw=True, skip_task=False, skip_skills=True),
        SilentUI(),
    )

    assert code == 2
    assert writes == []
    assert tasks == []
    assert not config.exists()


def test_detected_root_daily_folder_is_saved_as_empty_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "daily-notes.json").write_text('{"folder":"."}', encoding="utf-8")
    config = tmp_path / "config.toml"
    _patch_quiet_setup(monkeypatch)

    assert run_setup(
        SetupOptions(config_path=config, vault=vault, yes=True, skip_aw=True, skip_task=True, skip_skills=True),
        SilentUI(),
    ) == 0
    assert load_config(str(config)).daily_notes_dir == ""


def test_explicit_vault_yes_only_saves_config_when_aw_and_model_are_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = tmp_path / "config.toml"
    _detected_model_and_stopped_aw(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr("kaizenlog.setup.start_aw_and_wait", lambda *args, **kwargs: events.append("aw") or True)
    monkeypatch.setattr("kaizenlog.setup.install_all_skills", lambda *args, **kwargs: events.append("skills") or [])
    monkeypatch.setattr("kaizenlog.setup._register_daily_with_config", lambda *args, **kwargs: events.append("task") or True)
    monkeypatch.setattr("kaizenlog.setup.register_weekly_task", lambda *args, **kwargs: events.append("weekly") or True)
    monkeypatch.setattr("kaizenlog.setup.generate_initial_journal", lambda *args: events.append("note"))

    code = run_setup(
        SetupOptions(config_path=config, vault=vault, yes=True, skip_aw=False, skip_task=False, skip_skills=False),
        SilentUI(),
    )

    cfg = load_config(str(config))
    assert code == 1
    assert events == []
    assert cfg.llm.backend == "none"
    assert cfg.llm.model == ""


def test_generate_today_with_yes_has_only_typed_generation_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = tmp_path / "config.toml"
    _detected_model_and_stopped_aw(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr("kaizenlog.setup.start_aw_and_wait", lambda *args, **kwargs: events.append("aw") or True)
    monkeypatch.setattr("kaizenlog.setup.install_all_skills", lambda *args, **kwargs: events.append("skills") or [])
    monkeypatch.setattr("kaizenlog.setup._register_daily_with_config", lambda *args, **kwargs: events.append("task") or True)
    monkeypatch.setattr("kaizenlog.setup.register_weekly_task", lambda *args, **kwargs: events.append("weekly") or True)
    monkeypatch.setattr("kaizenlog.setup.generate_initial_journal", lambda *args: events.append("legacy-note"))
    typed: list[str] = []
    preview = SimpleNamespace(status="ready", reason=None)
    result = SimpleNamespace(status="generated", error=None)
    monkeypatch.setattr("kaizenlog.journal_service.preview_journal", lambda *_: typed.append("preview") or preview)
    monkeypatch.setattr("kaizenlog.journal_service.execute_journal", lambda *_: typed.append("execute") or result)

    code = run_setup(
        SetupOptions(
            config_path=config,
            vault=vault,
            yes=True,
            generate_today=True,
            skip_aw=False,
            skip_task=False,
            skip_skills=False,
        ),
        SilentUI(),
    )

    cfg = load_config(str(config))
    assert code == 1
    assert typed == ["preview", "execute"]
    assert events == []
    assert cfg.llm.backend == "none"
    assert cfg.llm.model == ""


def test_interactive_aw_start_requires_positive_confirmation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = tmp_path / "config.toml"
    _detected_model_and_stopped_aw(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr("kaizenlog.setup.start_aw_and_wait", lambda *args, **kwargs: calls.append("aw") or True)

    code = run_setup(
        SetupOptions(config_path=config, vault=vault, skip_aw=False, skip_task=True, skip_skills=True),
        SilentUI(),
    )

    assert code == 1
    assert calls == []

    calls.clear()
    config.unlink()
    ui = SilentUI()
    ui.choose = lambda msg, options, default=0: 0  # type: ignore[method-assign]
    ui.confirm = lambda msg, default=True: "ActivityWatch" in msg  # type: ignore[method-assign]
    assert run_setup(
        SetupOptions(config_path=config, vault=vault, skip_aw=False, skip_task=True, skip_skills=True),
        ui,
    ) == 0
    assert calls == ["aw"]


def test_interactive_skills_install_requires_positive_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = tmp_path / "config.toml"
    _patch_quiet_setup(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr("kaizenlog.setup.install_all_skills", lambda *args, **kwargs: calls.append("skills") or [])

    ui = SilentUI()
    ui.choose = lambda msg, options, default=0: 0  # type: ignore[method-assign]
    ui.confirm = lambda msg, default=True: "スキル" in msg  # type: ignore[method-assign]
    assert run_setup(
        SetupOptions(config_path=config, vault=vault, skip_aw=True, skip_task=True, skip_skills=False),
        ui,
    ) == 0
    assert calls == ["skills"]
