"""Setup wizard orchestration with FakeUI."""
from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, field

import pytest

from kaizenlog.setup import SetupOptions, SetupUI, run_setup


@dataclass
class FakeUI:
    answers: list  # bool | str | int in call order
    logs: list[str] = field(default_factory=list)

    def print(self, msg: str = "") -> None:
        self.logs.append(msg)

    def confirm(self, msg: str, default: bool = True) -> bool:
        if not self.answers:
            return default
        v = self.answers.pop(0)
        return bool(v)

    def choose(self, msg: str, options: list[str], default: int = 0) -> int:
        if not self.answers:
            return default
        v = self.answers.pop(0)
        return int(v)

    def ask_path(self, msg: str) -> Path:
        v = self.answers.pop(0)
        return Path(v)


def test_setup_yes_writes_appdata_config(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg_path = tmp_path / "cfg" / "config.toml"
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_llm",
        lambda **k: __import__("kaizenlog.setup_detect", fromlist=["LlmDetection"]).LlmDetection(
            None, None, ["gemma4:latest"], "openai-compatible", "gemma4:latest"
        ),
    )
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_activitywatch",
        lambda url: __import__("kaizenlog.setup_detect", fromlist=["AwDetection"]).AwDetection(False, None),
    )
    monkeypatch.setattr("kaizenlog.setup_detect.is_task_registered", lambda name="KaizenLog Daily": False)
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda cfg, p=None: ("✅ mock doctor", False))

    opts = SetupOptions(
        config_path=cfg_path,
        vault=vault,
        yes=True,
        skip_aw=True,
        skip_task=True,
        skip_skills=True,
    )
    code = run_setup(opts, ui=FakeUI([]))
    assert code == 0
    assert cfg_path.is_file()
    text = cfg_path.read_text(encoding="utf-8")
    assert "gemma4:latest" in text
    assert "openai-compatible" in text


def test_setup_yes_does_not_winget_without_flag(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg_path = tmp_path / "config.toml"
    called = []

    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_llm",
        lambda **k: __import__("kaizenlog.setup_detect", fromlist=["LlmDetection"]).LlmDetection(
            None, "copilot", None, "copilot-cli", None
        ),
    )
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_activitywatch",
        lambda url: __import__("kaizenlog.setup_detect", fromlist=["AwDetection"]).AwDetection(False, None),
    )
    monkeypatch.setattr("kaizenlog.setup.try_winget_install_aw", lambda: called.append("winget") or False)
    monkeypatch.setattr("kaizenlog.setup_detect.is_task_registered", lambda name="KaizenLog Daily": True)
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda cfg, p=None: ("❌ AW", True))

    code = run_setup(
        SetupOptions(config_path=cfg_path, vault=vault, yes=True, skip_aw=False, skip_task=True, skip_skills=True),
        ui=FakeUI([]),
    )
    assert called == []  # no --install-aw
    assert code == 1  # partial: doctor has error
    assert cfg_path.is_file()


def test_setup_missing_vault_noninteractive_fails(tmp_path):
    cfg_path = tmp_path / "config.toml"
    code = run_setup(
        SetupOptions(config_path=cfg_path, vault=None, yes=True, skip_aw=True, skip_task=True, skip_skills=True),
        ui=FakeUI([]),  # no path answers
    )
    assert code == 2


def test_setup_install_aw_flag_calls_winget(tmp_path, monkeypatch):
    vault = tmp_path / "v"
    vault.mkdir()
    cfg = tmp_path / "c.toml"
    called = []
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_llm",
        lambda **k: __import__("kaizenlog.setup_detect", fromlist=["LlmDetection"]).LlmDetection(
            None, None, None, "none", None
        ),
    )
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_activitywatch",
        lambda url: __import__("kaizenlog.setup_detect", fromlist=["AwDetection"]).AwDetection(False, None),
    )
    monkeypatch.setattr("kaizenlog.setup.try_winget_install_aw", lambda: called.append(1) or True)
    monkeypatch.setattr("kaizenlog.setup.start_aw_and_wait", lambda url, exe=None, timeout=60: True)
    monkeypatch.setattr("kaizenlog.setup_detect.is_task_registered", lambda name="KaizenLog Daily": True)
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda cfg, p=None: ("ok", False))

    code = run_setup(
        SetupOptions(
            config_path=cfg, vault=vault, yes=True,
            skip_aw=False, install_aw=True, skip_task=True, skip_skills=True,
        ),
        ui=FakeUI([]),
    )
    assert called == [1]
    assert code == 0


def test_setup_installs_skills_when_not_skipped(tmp_path, monkeypatch):
    vault = tmp_path / "v"
    vault.mkdir()
    cfg = tmp_path / "c.toml"
    installed = []

    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_llm",
        lambda **k: __import__("kaizenlog.setup_detect", fromlist=["LlmDetection"]).LlmDetection(
            None, None, None, "none", None
        ),
    )
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_activitywatch",
        lambda url: __import__("kaizenlog.setup_detect", fromlist=["AwDetection"]).AwDetection(True, None),
    )
    monkeypatch.setattr("kaizenlog.setup_detect.is_task_registered", lambda name="KaizenLog Daily": True)
    monkeypatch.setattr(
        "kaizenlog.setup.install_all_skills",
        lambda v, force=False: installed.append(str(v)) or [("daily-kaizen", "installed")],
    )
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda cfg, p=None: ("ok", False))

    run_setup(
        SetupOptions(config_path=cfg, vault=vault, yes=True, skip_aw=True, skip_task=True, skip_skills=False),
        ui=FakeUI([]),
    )
    assert installed == [str(vault.resolve())]
