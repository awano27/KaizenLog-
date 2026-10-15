"""Regression coverage for the safe first-run setup flow."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from importlib import resources
from pathlib import Path

import pytest

from kaizenlog.config import DEFAULT_RULES, ConfigError, load_config, write_config_file
from kaizenlog.setup import SetupOptions, run_setup
from kaizenlog.setup_detect import detect_vault_candidates


@dataclass
class RecordingUI:
    choices: list[int] = field(default_factory=list)
    confirmations: list[bool] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    choice_calls: list[tuple[str, list[str], int]] = field(default_factory=list)
    confirm_calls: list[tuple[str, bool]] = field(default_factory=list)

    def print(self, msg: str = "") -> None:
        self.logs.append(msg)

    def confirm(self, msg: str, default: bool = True) -> bool:
        self.confirm_calls.append((msg, default))
        return self.confirmations.pop(0) if self.confirmations else default

    def choose(self, msg: str, options: list[str], default: int = 0) -> int:
        self.choice_calls.append((msg, options, default))
        return self.choices.pop(0) if self.choices else default

    def ask_path(self, msg: str) -> Path:
        raise AssertionError(f"unexpected path prompt: {msg}")


def test_obsidian_registry_detection_ignores_malformed_and_unavailable_entries(tmp_path: Path):
    from kaizenlog.setup_detect import detect_vault_candidates

    vault = tmp_path / "vault"
    vault.mkdir()
    missing = tmp_path / "missing"
    registry = tmp_path / "obsidian.json"
    registry.write_text(
        json.dumps(
            {
                "vaults": {
                    "ok": {"path": str(vault)},
                    "missing": {"path": str(missing)},
                    "bad": {"path": 7},
                    "also_bad": "not-a-vault",
                }
            }
        ),
        encoding="utf-8",
    )

    assert detect_vault_candidates(obsidian_config_path=registry) == [vault.resolve()]


def test_obsidian_daily_folder_is_relative_and_cannot_escape_vault(tmp_path: Path):
    from kaizenlog.setup_detect import detect_daily_notes_dir

    vault = tmp_path / "vault"
    vault.mkdir()
    settings = vault / ".obsidian" / "daily-notes.json"
    settings.parent.mkdir()
    settings.write_text('{"folder": "Journal/Daily"}', encoding="utf-8")
    assert detect_daily_notes_dir(vault) == "Journal/Daily"

    settings.write_text('{"folder": "../../outside"}', encoding="utf-8")
    assert detect_daily_notes_dir(vault) is None

    settings.write_text(json.dumps({"folder": str(tmp_path / "outside")}), encoding="utf-8")
    assert detect_daily_notes_dir(vault) is None


def test_setup_preserves_explicit_daily_folder_and_uses_detected_folder_for_new_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "daily-notes.json").write_text(
        '{"folder": "Detected Daily"}', encoding="utf-8"
    )
    existing = tmp_path / "existing.toml"
    existing.write_text(
        f'[general]\nvault_dir = "{vault.as_posix()}"\ndaily_notes_dir = "Keep This"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("kaizenlog.setup_detect.detect_llm", lambda **_: _llm_none())
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda *_: ("ok", False))

    assert run_setup(
        SetupOptions(config_path=existing, vault=vault, yes=True, skip_aw=True, skip_task=True, skip_skills=True),
        RecordingUI(),
    ) == 0
    assert load_config(str(existing)).daily_notes_dir == "Keep This"

    fresh = tmp_path / "fresh.toml"
    assert run_setup(
        SetupOptions(config_path=fresh, vault=vault, yes=True, skip_aw=True, skip_task=True, skip_skills=True),
        RecordingUI(),
    ) == 0
    assert load_config(str(fresh)).daily_notes_dir == "Detected Daily"


def test_setup_offers_none_first_four_backend_menu_with_truthful_no_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = tmp_path / "vault"
    vault.mkdir()
    ui = RecordingUI(choices=[0])
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_llm",
        lambda **_: _llm_all_available(),
    )
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda *_: ("ok", False))

    assert run_setup(
        SetupOptions(config_path=tmp_path / "config.toml", vault=vault, skip_aw=True, skip_task=True, skip_skills=True),
        ui,
    ) == 0
    _message, options, default = ui.choice_calls[0]
    assert default == 0
    assert options[0].startswith("none")
    assert len(options) == 4
    assert any("model=なし" in line for line in ui.logs)


def test_private_title_patterns_require_a_list_of_valid_regular_expressions(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('[privacy]\nprivate_title_patterns = ["secret.*", "calendar"]\n', encoding="utf-8")
    assert load_config(str(config)).privacy.private_title_patterns == ["secret.*", "calendar"]

    config.write_text('[privacy]\nprivate_title_patterns = "secret"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="privacy.private_title_patterns"):
        load_config(str(config))

    config.write_text('[privacy]\nprivate_title_patterns = ["["]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="privacy.private_title_patterns"):
        load_config(str(config))


def test_grok_is_a_default_ai_classification_pattern():
    ai_rule = next(rule for rule in DEFAULT_RULES if rule["name"] == "AI作業")
    assert r"\bgrok\b" in ai_rule["patterns"]


def test_detected_daily_folder_is_toml_escaped_before_it_is_saved(tmp_path: Path):
    config = tmp_path / "config.toml"
    write_config_file(
        config,
        vault_dir=tmp_path,
        backend="none",
        model="",
        daily_notes_dir='Journal "Daily"',
    )
    assert load_config(str(config)).daily_notes_dir == 'Journal "Daily"'

    existing = tmp_path / "existing.toml"
    existing.write_text('[general]\nvault_dir = "C:/vault"\n', encoding="utf-8")
    write_config_file(
        existing,
        vault_dir=tmp_path,
        backend="none",
        model="",
        merge=True,
        daily_notes_dir='Journal "Daily"',
    )
    assert load_config(str(existing)).daily_notes_dir == 'Journal "Daily"'


def test_scheduler_script_is_available_as_a_package_resource():
    script = resources.files("kaizenlog").joinpath("scripts", "register-task.ps1")
    assert script.is_file()


def test_backfill_proposal_is_bounded_to_the_oldest_observed_day_and_cap():
    from kaizenlog.setup import bounded_backfill_days

    assert bounded_backfill_days(date(2026, 9, 1), date(2026, 9, 6)) == 5
    assert bounded_backfill_days(date(2026, 7, 1), date(2026, 9, 6)) == 30
    assert bounded_backfill_days(date(2026, 9, 7), date(2026, 9, 6)) == 0


def test_setup_interactive_opt_in_generates_then_backfills_from_oldest_window_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = tmp_path / "vault"
    vault.mkdir()
    generated: list[date] = []
    backfilled: list[tuple[date, date, int]] = []
    ui = RecordingUI(choices=[0], confirmations=[True, True])
    _patch_setup_basics(monkeypatch)
    monkeypatch.setattr(
        "kaizenlog.setup.generate_initial_journal",
        lambda cfg, day: generated.append(day) or cfg.daily_notes_path / f"{day}.md",
    )
    monkeypatch.setattr("kaizenlog.setup.oldest_window_event_day", lambda *_: date(2026, 8, 1))
    monkeypatch.setattr(
        "kaizenlog.setup.backfill_after_confirmation",
        lambda cfg, oldest, today, cap: backfilled.append((oldest, today, cap)) or 3,
    )

    assert run_setup(
        SetupOptions(config_path=tmp_path / "config.toml", vault=vault, skip_aw=True, skip_skills=True, skip_task=True),
        ui,
    ) == 0
    assert len(generated) == 1
    assert backfilled == [(date(2026, 8, 1), generated[0], 30)]
    assert any("日誌を生成しました" in line for line in ui.logs)


def test_interactive_first_journal_remains_available_when_scheduler_diagnosis_fails(tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    vault.mkdir()
    _patch_setup_basics(monkeypatch)
    monkeypatch.setattr('kaizenlog.setup.run_doctor', lambda *_: ('日次タスク未登録', True))
    generated = []
    monkeypatch.setattr('kaizenlog.setup.generate_initial_journal', lambda cfg, day: generated.append(day) or cfg.daily_notes_path / f'{day}.md')
    monkeypatch.setattr('kaizenlog.setup.oldest_window_event_day', lambda *_: None)
    ui = RecordingUI(choices=[0], confirmations=[True])
    assert run_setup(SetupOptions(config_path=tmp_path / 'config.toml', vault=vault,
                                  skip_aw=True, skip_skills=True, skip_task=True), ui) == 1
    assert len(generated) == 1


def test_setup_yes_never_generates_or_backfills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    called: list[str] = []
    _patch_setup_basics(monkeypatch)
    monkeypatch.setattr("kaizenlog.setup.generate_initial_journal", lambda *_: called.append("generate"))
    monkeypatch.setattr("kaizenlog.setup.oldest_window_event_day", lambda *_: called.append("oldest"))
    monkeypatch.setattr("kaizenlog.setup.backfill_after_confirmation", lambda *_: called.append("backfill"))

    assert run_setup(
        SetupOptions(config_path=tmp_path / "config.toml", vault=vault, yes=True, skip_aw=True, skip_skills=True, skip_task=True),
        RecordingUI(),
    ) == 0
    assert called == []


def test_existing_llm_choice_is_the_interactive_menu_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[general]\nvault_dir = "{vault.as_posix()}"\n[llm]\nbackend = "copilot-cli"\n'
        '[llm.openai_compatible]\nmodel = "remembered-model"\n',
        encoding="utf-8",
    )
    ui = RecordingUI()
    _patch_setup_basics(monkeypatch)

    assert run_setup(
        SetupOptions(config_path=config, vault=vault, skip_aw=True, skip_skills=True, skip_task=True), ui
    ) == 0
    assert ui.choice_calls[0][2] == 2
    cfg = load_config(str(config))
    assert cfg.llm.backend == "copilot-cli"
    assert cfg.llm.model == "remembered-model"


def test_oldest_window_event_day_uses_only_currentwindow_bucket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from kaizenlog.setup import oldest_window_event_day

    calls: list[tuple[str, dict]] = []

    class FakeClient:
        def __init__(self, base_url: str):
            assert base_url == "http://aw.test"

        def buckets(self):
            return {
                "aw-watcher-window_test": {"type": "currentwindow"},
                "aw-watcher-afk_test": {"type": "afkstatus"},
            }

        def _get(self, path: str, **params):
            calls.append((path, params))
            assert 'desc' not in params
            event = datetime.fromisoformat('2026-08-01T23:30:00+00:00')
            return [{'timestamp': event.isoformat()}] if event < datetime.fromisoformat(params['end']) else []

    monkeypatch.setattr("kaizenlog.setup.ActivityWatchClient", FakeClient)
    assert oldest_window_event_day("http://aw.test", "Asia/Tokyo", as_of=date(2026, 8, 20)) == date(2026, 8, 2)
    assert len(calls) <= 7
    assert all(path.endswith('/aw-watcher-window_test/events') and params['limit'] == 1 for path, params in calls)


def test_partial_setup_backfill_is_not_reported_as_complete(tmp_path, monkeypatch):
    from kaizenlog.config import Config
    from kaizenlog.setup import backfill_after_confirmation
    monkeypatch.setattr('kaizenlog.cli.cmd_backfill', lambda *_: 1)
    with pytest.raises(RuntimeError, match='未完了'):
        backfill_after_confirmation(Config(vault_dir=tmp_path), date(2026, 9, 3), date(2026, 9, 6))


def test_oldest_window_event_day_fails_closed_when_bucket_response_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
):
    from kaizenlog.setup import oldest_window_event_day

    class FakeClient:
        def __init__(self, base_url: str):
            pass

        def buckets(self):
            return []

    monkeypatch.setattr("kaizenlog.setup.ActivityWatchClient", FakeClient)
    assert oldest_window_event_day("http://aw.test", "Asia/Tokyo") is None


def test_scheduler_helpers_pass_the_explicit_config_path_to_the_packaged_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from kaizenlog.setup import register_daily_task, register_weekly_task

    script = tmp_path / "register-task.ps1"
    script.write_text("# fake", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    config = tmp_path / "config.toml"
    captured: list[list[str]] = []

    monkeypatch.setattr("kaizenlog.setup._find_register_task_script", lambda: script)
    monkeypatch.setattr("kaizenlog.setup.shutil.which", lambda _: "C:/bin/kaizenlog.exe")
    monkeypatch.setattr(
        "kaizenlog.setup.subprocess.run",
        lambda args, **_: captured.append(args) or type("Run", (), {"returncode": 0})(),
    )

    assert register_daily_task(config_path=config)
    assert register_weekly_task(vault, config_path=config)
    assert all("-ConfigPath" in args and str(config) in args for args in captured)
    captured.clear()
    monkeypatch.chdir(tmp_path)
    assert register_daily_task(config_path=Path('config.toml'))
    assert register_weekly_task(Path('vault'), config_path=Path('config.toml'))
    assert all(str(config.resolve()) == args[args.index('-ConfigPath') + 1] for args in captured)


def test_aiwork_loads_opt_in_grok_and_cursor_session_paths(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[aiwork]\ngrok_sessions_dir = "~/grok"\ncursor_sessions_dir = "~/cursor"\n',
        encoding="utf-8",
    )
    cfg = load_config(str(config))
    assert cfg.aiwork.grok_sessions_dir == "~/grok"
    assert cfg.aiwork.cursor_sessions_dir == "~/cursor"


def test_interactive_schedule_defaults_daily_yes_and_weekly_no_with_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = tmp_path / "config.toml"
    ui = RecordingUI(choices=[0], confirmations=[True, False, False])
    calls: list[tuple[str, Path]] = []
    _patch_setup_basics(monkeypatch)
    monkeypatch.setattr("kaizenlog.setup_detect.is_task_registered", lambda *_: False)
    monkeypatch.setattr(
        "kaizenlog.setup.register_daily_task",
        lambda *args, config_path=None, **kwargs: calls.append(("daily", Path(config_path))) or True,
    )
    monkeypatch.setattr(
        "kaizenlog.setup.register_weekly_task",
        lambda *args, config_path=None, **kwargs: calls.append(("weekly", Path(config_path))) or True,
    )

    assert run_setup(
        SetupOptions(config_path=config, vault=vault, skip_aw=True, skip_skills=True), ui
    ) == 0
    assert calls == [("daily", config)]
    assert any(default is True and "タスク" in msg for msg, default in ui.confirm_calls)
    assert any(default is False and "週次" in msg for msg, default in ui.confirm_calls)


def _patch_setup_basics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kaizenlog.setup_detect.detect_llm", lambda **_: _llm_none())
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda *_: ("ok", False))


def _llm_none():
    from kaizenlog.setup_detect import LlmDetection

    return LlmDetection(None, None, None, "none", None)


def _llm_all_available():
    from kaizenlog.setup_detect import LlmDetection

    return LlmDetection("claude", "copilot", ["actual-local-model"], "claude-code-cli", "actual-local-model")
