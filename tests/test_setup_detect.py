"""Pure detection helpers for kaizenlog setup."""
from __future__ import annotations

from pathlib import Path

from kaizenlog.setup_detect import (
    detect_llm,
    recommend_ollama_model,
    detect_activitywatch,
    detect_vault_candidates,
    propose_backend,
)


def test_propose_backend_prefers_claude():
    assert propose_backend(claude=True, copilot=True, ollama_models=["gemma4:latest"]) == "claude-code-cli"


def test_propose_backend_copilot_then_ollama_then_none():
    assert propose_backend(claude=False, copilot=True, ollama_models=[]) == "copilot-cli"
    assert propose_backend(claude=False, copilot=False, ollama_models=["gemma4:latest"]) == "openai-compatible"
    assert propose_backend(claude=False, copilot=False, ollama_models=None) == "none"


def test_recommend_ollama_model_skips_embed_and_prefers_qwen_gemma():
    models = ["nomic-embed-text:latest", "gemma4:latest", "llama3:8b"]
    assert recommend_ollama_model(models, preferred=None) == "gemma4:latest"
    assert recommend_ollama_model(models, preferred="llama3:8b") == "llama3:8b"
    assert recommend_ollama_model(["nomic-embed-text:latest"], preferred=None) is None


def test_detect_llm_uses_which_and_models(monkeypatch):
    monkeypatch.setattr(
        "kaizenlog.setup_detect.shutil.which",
        lambda c: f"C:/{c}.exe" if c in ("claude",) else None,
    )

    def fake_models(base_url, timeout=15):
        assert "11434" in base_url
        return ["gemma4:latest"]

    monkeypatch.setattr("kaizenlog.setup_detect.list_openai_models", fake_models)
    info = detect_llm(base_url="http://localhost:11434/v1")
    assert info.claude_path is not None
    assert info.copilot_path is None
    assert info.ollama_models == ["gemma4:latest"]
    assert info.proposed_backend == "claude-code-cli"


def test_detect_activitywatch_ok(monkeypatch):
    monkeypatch.setattr(
        "kaizenlog.setup_detect.probe_aw_api",
        lambda url, timeout=5: True,
    )
    st = detect_activitywatch("http://localhost:5600")
    assert st.reachable is True


def test_detect_vault_candidates_includes_existing(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    (v / "note.md").write_text("x", encoding="utf-8")
    cands = detect_vault_candidates(existing=v, extra_roots=[tmp_path])
    assert v.resolve() in [p.resolve() for p in cands]
