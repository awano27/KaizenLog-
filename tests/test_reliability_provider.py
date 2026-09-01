import json
import subprocess

import pytest

from kaizenlog import advisor
from kaizenlog.advisor import BackendUnavailable
from kaizenlog.config import LLMConfig
from kaizenlog.reliability import FailureReason, GenerationTrace


def test_claude_exit_zero_login_payload_is_non_retryable(monkeypatch):
    completed = subprocess.CompletedProcess(
        ["claude"],
        0,
        stdout=json.dumps({
            "is_error": False,
            "subtype": "success",
            "result": "Not logged in · Please run /login",
            "model": "<synthetic>",
        }),
        stderr="",
    )
    monkeypatch.setattr(advisor.shutil, "which", lambda _: "claude.exe")
    monkeypatch.setattr(advisor.subprocess, "run", lambda *a, **k: completed)

    with pytest.raises(BackendUnavailable, match="未認証"):
        advisor._call_claude_code_cli(LLMConfig(), "system", "user")


def test_generate_text_trace_records_actual_fallback_backend(monkeypatch):
    monkeypatch.setattr(
        advisor,
        "_call_claude_code_cli",
        lambda *_: (_ for _ in ()).throw(BackendUnavailable("login")),
    )
    monkeypatch.setattr(advisor, "_call_openai_compatible", lambda *_: "local text")
    trace = GenerationTrace(configured_backend="claude-code-cli")
    cfg = LLMConfig(backend="claude-code-cli", fallback_to_local=True, retries=2)
    sleeps = []

    assert advisor.generate_text(cfg, "s", "u", sleep=sleeps.append, trace=trace) == "local text"
    assert sleeps == []
    assert trace.actual_backend == "openai-compatible"
    assert trace.fallback_used is True
    assert [attempt.reason for attempt in trace.attempts] == [
        FailureReason.PROVIDER_AUTH_REQUIRED,
        FailureReason.NONE,
    ]


def test_successful_claude_text_mentioning_http_403_is_not_auth_failure(monkeypatch):
    """Treating ordinary successful model text as a status envelope would reject advice."""
    completed = subprocess.CompletedProcess(
        ["claude"],
        0,
        stdout=json.dumps({
            "is_error": False,
            "result": "HTTP 403 is a useful example in the advice.",
        }),
        stderr="",
    )
    monkeypatch.setattr(advisor.shutil, "which", lambda _: "claude.exe")
    monkeypatch.setattr(advisor.subprocess, "run", lambda *a, **k: completed)

    assert advisor._call_claude_code_cli(LLMConfig(), "system", "user") == (
        "HTTP 403 is a useful example in the advice."
    )


def test_claude_error_envelope_with_http_401_is_auth_failure(monkeypatch):
    """A non-success envelope carrying 401 must still avoid retrying Claude."""
    completed = subprocess.CompletedProcess(
        ["claude"],
        0,
        stdout=json.dumps({"is_error": True, "result": "HTTP 401 token expired"}),
        stderr="",
    )
    monkeypatch.setattr(advisor.shutil, "which", lambda _: "claude.exe")
    monkeypatch.setattr(advisor.subprocess, "run", lambda *a, **k: completed)

    with pytest.raises(BackendUnavailable, match="未認証"):
        advisor._call_claude_code_cli(LLMConfig(), "system", "user")
