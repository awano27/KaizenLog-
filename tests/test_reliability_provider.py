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
