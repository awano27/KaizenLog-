"""Side-effect-free environment detection for setup wizard."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import requests


@dataclass
class LlmDetection:
    claude_path: str | None
    copilot_path: str | None
    ollama_models: list[str] | None  # None = unreachable
    proposed_backend: str
    proposed_model: str | None


@dataclass
class AwDetection:
    reachable: bool
    exe_path: Path | None = None


def propose_backend(*, claude: bool, copilot: bool, ollama_models: list[str] | None) -> str:
    if claude:
        return "claude-code-cli"
    if copilot:
        return "copilot-cli"
    if ollama_models:
        return "openai-compatible"
    return "none"


def recommend_ollama_model(models: list[str], preferred: str | None) -> str | None:
    if preferred and preferred in models:
        return preferred
    non_embed = [m for m in models if "embed" not in m.lower()]
    if not non_embed:
        return None
    for token in ("qwen3", "qwen", "gemma", "llama", "mistral", "phi"):
        for m in non_embed:
            if token in m.lower():
                return m
    return non_embed[0]


def list_openai_models(base_url: str, timeout: float = 15) -> list[str] | None:
    try:
        r = requests.get(f"{base_url.rstrip('/')}/models", timeout=timeout)
        if r.status_code >= 400:
            return None
        ids = [m.get("id") for m in r.json().get("data", []) if isinstance(m, dict)]
        return [i for i in ids if i] or None
    except (requests.RequestException, ValueError, TypeError):
        return None


def detect_llm(
    base_url: str = "http://localhost:11434/v1",
    preferred_model: str | None = None,
) -> LlmDetection:
    claude = shutil.which("claude")
    copilot = shutil.which("copilot")
    models = list_openai_models(base_url)
    backend = propose_backend(claude=bool(claude), copilot=bool(copilot), ollama_models=models)
    model = recommend_ollama_model(models or [], preferred_model) if models else None
    return LlmDetection(claude, copilot, models, backend, model)


def probe_aw_api(base_url: str, timeout: float = 5) -> bool:
    try:
        r = requests.get(f"{base_url.rstrip('/')}/api/0/buckets/", timeout=timeout)
        return r.status_code < 400
    except requests.RequestException:
        return False


def find_aw_exe() -> Path | None:
    which = shutil.which("aw-qt")
    if which:
        return Path(which)
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "activitywatch" / "aw-qt.exe",
        Path(os.environ.get("ProgramFiles", "")) / "ActivityWatch" / "aw-qt.exe",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def detect_activitywatch(base_url: str) -> AwDetection:
    return AwDetection(probe_aw_api(base_url), find_aw_exe())


def detect_vault_candidates(
    existing: Path | None = None,
    extra_roots: list[Path] | None = None,
) -> list[Path]:
    out: list[Path] = []
    if existing and Path(existing).expanduser().is_dir():
        out.append(Path(existing).expanduser().resolve())
    for root in extra_roots or []:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                out.append(child.resolve())
    # dedupe preserve order
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def query_task_registered(task_name: str = "KaizenLog Daily") -> bool | None:
    """Windows タスク登録状態。

    Returns:
      True  — 登録あり
      False — 未登録（クエリ成功・タスク無し）
      None  — 検出不能（非 Windows / schtasks 失敗 / タイムアウト）
    """
    if sys.platform != "win32":
        return None
    try:
        # schtasks は日本語 Windows で CP932 を返すことがある → bytes + 判定のみ
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name],
            capture_output=True,
            timeout=15,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


def is_task_registered(task_name: str = "KaizenLog Daily") -> bool:
    """Best-effort Windows check; returns False on non-Windows or errors."""
    return query_task_registered(task_name) is True
