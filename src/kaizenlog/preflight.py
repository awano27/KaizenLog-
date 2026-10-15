"""Small, deterministic and privacy-filtered SessionStart context."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .config import Config

from .file_lock import file_lock

MAX_PREFLIGHT_CHARS = 360
_STATE_FILE = "preflight.json"


def build_preflight_context(
    *,
    memory_dir: Path,
    cfg: Config,
    today: date,
    redactor: Callable[[str], str] | None = None,
    max_chars: int = MAX_PREFLIGHT_CHARS,
    skilled_prompt: str | None = None,
) -> str:
    """Return available context only; unknown sources deliberately add no text."""
    from .decision import format_verdict_brief, select_decision_question_entry, yesterday_confirmed_entries
    from .goal import read_goal
    from .memory import load_entries
    from .vault import DailyNoteStore

    entries = load_entries(Path(memory_dir))
    lines: list[str] = []
    candidate = select_decision_question_entry(entries, today, memory_dir=memory_dir)
    if candidate is not None:
        lines.append(f"未チェック: {candidate.id} {candidate.action}")
    confirmed = yesterday_confirmed_entries(entries, today)
    if confirmed:
        lines.append(f"昨日の確定: {format_verdict_brief(confirmed[-1])}")
    try:
        goal = read_goal(DailyNoteStore(cfg).read(today))
    except OSError:
        goal = None
    if goal is not None and goal.text.strip():
        lines.append(f"今日の目標: {goal.text}")
    # Global PromptLedger has no repository provenance.  Callers may pass only
    # evidence already established as belonging to this repository.
    if skilled_prompt and skilled_prompt.strip():
        lines.append(f"このリポジトリの skill: {skilled_prompt.strip()}")
    if not lines:
        return ""
    context = "\n".join(lines)
    if redactor is not None:
        context = redactor(context)
    return context[: max(0, int(max_chars))]


def _state_path(state_root: Path) -> Path:
    return Path(state_root) / _STATE_FILE


def _load_delivered(state_root: Path) -> dict[str, str]:
    try:
        data = json.loads(_state_path(state_root).read_text(encoding="utf-8"))
        values = data.get("delivered", {}) if isinstance(data, dict) else {}
        if isinstance(values, dict):
            return {str(key): str(value) for key, value in values.items() if isinstance(key, str) and isinstance(value, str)}
        # Backward-compatible empty migration from the first implementation.
        return {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_delivered(state_root: Path, delivered: dict[str, str]) -> bool:
    path = _state_path(state_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        tmp.write_text(json.dumps({"delivered": delivered}, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def preflight_hook_output(context: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}


def deliver_preflight_once(
    *,
    context: str,
    repo_path: Path,
    today: date,
    state_root: Path,
    deliver: Callable[[dict], bool],
) -> bool:
    """Deliver at most once per resolved repo/day; record only successful delivery."""
    if not context:
        return False
    try:
        with file_lock(Path(state_root) / ".preflight.lock", timeout=2.0):
            return _deliver_preflight_once_locked(
                context=context,
                repo_path=repo_path,
                today=today,
                state_root=state_root,
                deliver=deliver,
            )
    except (OSError, TimeoutError):
        return False


def _deliver_preflight_once_locked(
    *,
    context: str,
    repo_path: Path,
    today: date,
    state_root: Path,
    deliver: Callable[[dict], bool],
) -> bool:
    try:
        repo_key = str(Path(repo_path).resolve())
    except OSError:
        repo_key = str(repo_path)
    key = hashlib.sha256(f"{repo_key}\0{today.isoformat()}".encode("utf-8")).hexdigest()
    delivered = _load_delivered(Path(state_root))
    cutoff = (today - timedelta(days=7)).isoformat()
    delivered = {old_key: old_day for old_key, old_day in delivered.items() if old_day >= cutoff}
    if key in delivered:
        return False
    try:
        ok = bool(deliver(preflight_hook_output(context)))
    except Exception:
        return False
    if not ok:
        return False
    delivered[key] = today.isoformat()
    # A state-write failure is surfaced as False.  It never causes a failed
    # delivery to be debounced, and the next invocation may safely retry.
    return _save_delivered(Path(state_root), delivered)
