"""Side-effect-free environment detection for setup wizard."""
from __future__ import annotations

import json
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


def default_obsidian_config_path() -> Path:
    """Return Obsidian's registry location without creating or reading it."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Obsidian" / "obsidian.json"
    return Path.home() / "AppData" / "Roaming" / "Obsidian" / "obsidian.json"


def _registry_vaults(path: Path) -> list[Path]:
    """Read only valid, currently accessible vault paths from Obsidian's registry."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    vaults = data.get("vaults") if isinstance(data, dict) else None
    if not isinstance(vaults, dict):
        return []
    found: list[Path] = []
    for entry in vaults.values():
        raw_path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        try:
            candidate = Path(raw_path).expanduser()
            if candidate.is_dir():
                found.append(candidate.resolve())
        except (OSError, RuntimeError):
            continue
    return found


def detect_daily_notes_dir(vault: Path, settings_path: Path | None = None) -> str | None:
    """Return Obsidian Daily Notes' relative folder when it stays inside *vault*.

    The setup wizard only proposes this value.  It never creates the folder or
    changes Obsidian's plugin settings.
    """
    try:
        root = Path(vault).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    settings = settings_path or root / ".obsidian" / "daily-notes.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    raw_folder = data.get("folder") if isinstance(data, dict) else None
    if not isinstance(raw_folder, str):
        return None
    folder = raw_folder.strip().replace("\\", "/")
    if not folder:
        return "."
    relative = Path(folder)
    if relative.is_absolute() or relative.drive:
        return None
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate.relative_to(root).as_posix() or "."


def detect_vault_candidates(
    existing: Path | None = None,
    extra_roots: list[Path] | None = None,
    obsidian_config_path: Path | None = None,
) -> list[Path]:
    """Detect writable-looking vault roots without changing Obsidian or files.

    Obsidian's registry is advisory: malformed, missing, inaccessible, or
    no-longer-present entries are ignored.  The caller still asks the user to
    choose a candidate before writing KaizenLog configuration.
    """
    out: list[Path] = []
    try:
        if existing and Path(existing).expanduser().is_dir():
            out.append(Path(existing).expanduser().resolve())
    except (OSError, RuntimeError):
        pass
    registry = obsidian_config_path or default_obsidian_config_path()
    if registry.is_file():
        out.extend(_registry_vaults(registry))
    for root in extra_roots or []:
        try:
            root = Path(root).expanduser()
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    out.append(child.resolve())
        except (OSError, RuntimeError):
            continue
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
