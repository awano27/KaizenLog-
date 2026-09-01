"""第22弾: doctor 運用衛生 H1/H2。"""
from __future__ import annotations

from pathlib import Path

import pytest

from kaizenlog.config import AIWorkConfig, Config
from kaizenlog.doctor import (
    TASK_MORNING,
    TASK_NIGHTLY,
    _check_artifacts,
    _check_schedule,
    run_doctor,
)
from kaizenlog.doctor import Check


# ---- H1 schedule ------------------------------------------------------------


def test_h1_schedule_both_registered(monkeypatch):
    monkeypatch.setattr(
        "kaizenlog.doctor.query_task_registered",
        lambda name: True,
    )
    c = Check()
    _check_schedule(c)
    text = "\n".join(c.lines)
    assert "夜間タスク登録済み" in text
    assert "朝タスク登録済み" in text
    assert not c.has_error


def test_h1_schedule_nightly_missing_is_error(monkeypatch):
    def q(name: str):
        if name == TASK_NIGHTLY:
            return False
        if name == TASK_MORNING:
            return True
        return None

    monkeypatch.setattr("kaizenlog.doctor.query_task_registered", q)
    c = Check()
    _check_schedule(c)
    text = "\n".join(c.lines)
    assert c.has_error
    assert "夜間タスクが未登録" in text
    assert "setup --register-task" in text
    assert "朝タスク登録済み" in text


def test_h1_schedule_morning_missing_is_warn(monkeypatch):
    def q(name: str):
        if name == TASK_NIGHTLY:
            return True
        if name == TASK_MORNING:
            return False
        return None

    monkeypatch.setattr("kaizenlog.doctor.query_task_registered", q)
    c = Check()
    _check_schedule(c)
    text = "\n".join(c.lines)
    assert not c.has_error
    assert "朝タスク" in text and "未登録" in text
    assert any(ln.startswith("⚠️") for ln in c.lines)


def test_h1_schedule_detection_unavailable(monkeypatch):
    monkeypatch.setattr(
        "kaizenlog.doctor.query_task_registered",
        lambda name: None,
    )
    c = Check()
    _check_schedule(c)
    text = "\n".join(c.lines)
    assert "検出をスキップ" in text or "検出不能" in text
    assert not c.has_error


# ---- H2 artifacts -----------------------------------------------------------


def test_h2_artifacts_memory_and_broken_jsonl(tmp_path):
    vault = tmp_path / "vault"
    mem = vault / "Kaizen" / "Memory"
    mem.mkdir(parents=True)
    (mem / "suggestions.jsonl").write_text(
        '{"id":"a","date":"2026-07-01","action":"x","status":"proposed"}\n'
        "not-json\n"
        '{"id":"b","date":"2026-07-02","action":"y","status":"done"}\n',
        encoding="utf-8",
    )
    exp = vault / "03 Areas" / "Kaizen Experiments"
    exp.mkdir(parents=True)
    (exp / "EXP.md").write_text("# e\n", encoding="utf-8")
    daily = vault / "01 Daily Notes"
    weekly = daily / "Weekly Reviews"
    weekly.mkdir(parents=True)
    (weekly / "2026-W30.md").write_text("# w\n", encoding="utf-8")

    cfg = Config(vault_dir=vault)
    cfg.aiwork = AIWorkConfig(
        enabled=True,
        browser_export_dir=str(tmp_path / "no-browser-export"),
    )
    c = Check()
    _check_artifacts(c, cfg)
    text = "\n".join(c.lines)
    assert "Kaizen Memory" in text and "2行" in text
    assert "パース不能行が 1 件" in text
    assert "実験ノート" in text and "1件" in text
    assert "Weekly Reviews" in text and "2026-W30" in text
    assert "browser_export_dir" in text or "ブラウザ" in text
    # broken jsonl is warn, not overall error from parse alone
    assert any("パース不能" in ln and ln.startswith("⚠️") for ln in c.lines)


def test_h2_artifacts_missing_paths_are_soft(tmp_path):
    vault = tmp_path / "empty-vault"
    vault.mkdir()
    cfg = Config(vault_dir=vault)
    cfg.aiwork = AIWorkConfig(enabled=False)
    c = Check()
    _check_artifacts(c, cfg)
    assert not c.has_error
    text = "\n".join(c.lines)
    assert "Memory" in text
    # browser check skipped when aiwork disabled
    assert "browser_export" not in text


def test_h2_run_doctor_includes_schedule_when_configured(tmp_path, monkeypatch):
    vault = tmp_path / "v"
    (vault / "01 Daily Notes").mkdir(parents=True)
    cfg = Config(vault_dir=vault)
    monkeypatch.setattr(
        "kaizenlog.doctor.query_task_registered",
        lambda name: False,
    )
    monkeypatch.setattr(
        "kaizenlog.doctor._check_activitywatch",
        lambda c, cfg: c.ok("AW mock"),
    )
    monkeypatch.setattr(
        "kaizenlog.doctor._check_llm",
        lambda c, cfg: c.ok("LLM mock"),
    )
    report, has_err = run_doctor(cfg, config_path=None)
    assert has_err
    assert "夜間タスクが未登録" in report


# ---- query_task_registered の subprocess 層マッピング（第22弾レビュー残: モック層が浅かった分） ----


class _FakeCompleted:
    def __init__(self, returncode):
        self.returncode = returncode


def test_query_task_registered_maps_returncode(monkeypatch):
    from kaizenlog import setup_detect

    monkeypatch.setattr(setup_detect.sys, "platform", "win32")
    monkeypatch.setattr(
        setup_detect.subprocess, "run", lambda *a, **k: _FakeCompleted(0)
    )
    assert setup_detect.query_task_registered("X") is True
    monkeypatch.setattr(
        setup_detect.subprocess, "run", lambda *a, **k: _FakeCompleted(1)
    )
    assert setup_detect.query_task_registered("X") is False


def test_query_task_registered_error_timeout_platform(monkeypatch):
    from kaizenlog import setup_detect

    monkeypatch.setattr(setup_detect.sys, "platform", "win32")

    def boom(*a, **k):
        raise OSError("no schtasks")

    monkeypatch.setattr(setup_detect.subprocess, "run", boom)
    assert setup_detect.query_task_registered("X") is None

    def slow(*a, **k):
        raise setup_detect.subprocess.TimeoutExpired(cmd="schtasks", timeout=15)

    monkeypatch.setattr(setup_detect.subprocess, "run", slow)
    assert setup_detect.query_task_registered("X") is None

    monkeypatch.setattr(setup_detect.sys, "platform", "linux")
    assert setup_detect.query_task_registered("X") is None


def test_doctor_claude_auth_probe_reports_stable_reason_only(monkeypatch):
    """Claude's raw auth response must never enter doctor output."""
    from kaizenlog.doctor import _check_llm

    class Completed:
        returncode = 0
        stdout = '{"loggedIn": false, "secret": "do-not-display"}'
        stderr = "also-private"

    observed = {}

    def probe(*args, **kwargs):
        observed.update(kwargs)
        assert args[0] == ["C:/tools/claude.exe", "auth", "status", "--json"]
        return Completed()

    cfg = Config()
    cfg.llm.backend = "claude-code-cli"
    cfg.llm.claude_command = "claude"
    cfg.llm.fallback_to_local = False
    monkeypatch.setattr("kaizenlog.doctor.shutil.which", lambda _: "C:/tools/claude.exe")
    monkeypatch.setattr("kaizenlog.doctor.subprocess.run", probe)
    c = Check()

    _check_llm(c, cfg)

    text = "\n".join(c.lines)
    assert "provider_auth_required" in text
    assert "do-not-display" not in text
    assert observed == {"capture_output": True, "text": True, "encoding": "utf-8", "timeout": 10}


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("timeout", "provider_probe_timeout"),
        ("invalid-json", "provider_probe_unknown"),
    ],
)
def test_doctor_claude_auth_probe_maps_unavailable_probe_results(monkeypatch, failure, expected):
    """Timeout and malformed output must have stable non-secret classifications."""
    from kaizenlog import doctor

    cfg = Config()
    cfg.llm.backend = "claude-code-cli"
    cfg.llm.fallback_to_local = False
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "claude")
    if failure == "timeout":
        monkeypatch.setattr(
            doctor.subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(doctor.subprocess.TimeoutExpired(a[0], 10)),
        )
    else:
        monkeypatch.setattr(
            doctor.subprocess,
            "run",
            lambda *a, **k: type("Done", (), {"returncode": 0, "stdout": "not-json", "stderr": ""})(),
        )
    c = Check()

    doctor._check_llm(c, cfg)

    assert expected in "\n".join(c.lines)


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("[]", 0, "provider_probe_unknown"),
        ("null", 0, "provider_probe_unknown"),
        ("{}", 0, "provider_probe_unknown"),
        ('"false"', 0, "provider_probe_unknown"),
        ('{"loggedIn": "true"}', 0, "provider_probe_unknown"),
        ('{"loggedIn": true}', 0, "認証状態: ok"),
        ('{"loggedIn": false}', 0, "provider_auth_required"),
        ('{"loggedIn": true}', 2, "provider_probe_unknown"),
    ],
)
def test_doctor_claude_auth_probe_accepts_only_boolean_logged_in(
    monkeypatch, stdout, returncode, expected
):
    """Malformed or unexpected auth-probe output must not crash or become healthy."""
    from kaizenlog import doctor

    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *a, **k: type("Done", (), {"returncode": returncode, "stdout": stdout, "stderr": "secret"})(),
    )
    c = Check()

    doctor._check_claude_auth(c, "claude")

    assert expected in "\n".join(c.lines)
