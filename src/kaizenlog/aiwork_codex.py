"""Codex CLI テレメトリアダプタ。

実ログ形式（2026-07 時点・本機 ~/.codex/sessions）:
  sessions/YYYY/MM/DD/rollout-*.jsonl
  各行: {\"type\": \"session_meta\"|\"event_msg\"|\"response_item\"|\"turn_context\"|...,
         \"timestamp\": ISO, \"payload\": {...}}

想定との差異（完了報告用）:
  - ツールは function_call / custom_tool_call（Claude の tool_use とは別名）
  - ユーザー発話は event_msg/user_message（response_item/message role=user も存在）
  - 中断は event_msg/turn_aborted
  - トークンは event_msg/token_count.info.total_token_usage（累積。最大値を採用）
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .aiwork import AISession, UserPrompt, _parse_ts


def _payload(record: dict) -> dict:
    p = record.get("payload")
    return p if isinstance(p, dict) else {}


def _codex_day_dirs(root: Path, day_start: datetime, day_end: datetime) -> list[Path]:
    """対象日とその前日の YYYY/MM/DD ディレクトリ（深夜跨ぎ用）。"""
    # セッション配置はローカル日付ディレクトリ。day_start のタイムゾーンに合わせる
    dates = set()
    for dt in (day_start - timedelta(days=1), day_start, day_end - timedelta(seconds=1)):
        local = dt.astimezone() if dt.tzinfo else dt.replace(tzinfo=timezone.utc).astimezone()
        dates.add(local.date())
    dirs: list[Path] = []
    for d in sorted(dates):
        folder = root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
        if folder.is_dir():
            dirs.append(folder)
    return dirs


def _project_from_cwd(cwd: object) -> str:
    if isinstance(cwd, str) and cwd.strip():
        return Path(cwd).name or cwd
    return "unknown"


def _tool_output_is_error(output: object) -> bool:
    """ツール出力が失敗らしいか（本文はログに残さない）。"""
    if isinstance(output, str):
        head = output[:400].lower()
        return any(
            marker in head
            for marker in (
                "traceback",
                "error:",
                "exit code",
                "command failed",
                "failed with",
                "errno",
            )
        )
    if isinstance(output, list):
        for item in output[:5]:
            if isinstance(item, dict) and _tool_output_is_error(str(item.get("text", ""))):
                return True
    if isinstance(output, dict):
        if output.get("exit_code") not in (None, 0, "0"):
            return True
        if output.get("success") is False:
            return True
        err = output.get("error")
        if err:
            return True
    return False


class CodexAdapter:
    """Codex CLI の ~/.codex/sessions ロールアウト JSONL アダプタ。"""

    name = "codex"

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = Path(sessions_dir)

    def scan_sessions(
        self, day_start: datetime, day_end: datetime
    ) -> list[AISession]:
        if not self.sessions_dir.is_dir():
            return []
        sessions: dict[str, AISession] = {}
        for folder in _codex_day_dirs(self.sessions_dir, day_start, day_end):
            for path in folder.glob("rollout-*.jsonl"):
                try:
                    self._ingest_file(path, day_start, day_end, sessions)
                except OSError:
                    continue
        result = [
            s for s in sessions.values() if s.user_turns > 0 or s.interruptions > 0
        ]
        result.sort(key=lambda s: s.start)
        return result

    def scan_user_prompts(
        self, start: datetime, end: datetime, min_chars: int = 8
    ) -> list[UserPrompt]:
        if not self.sessions_dir.is_dir():
            return []
        out: list[UserPrompt] = []
        for folder in _codex_day_dirs(self.sessions_dir, start, end):
            for path in folder.glob("rollout-*.jsonl"):
                try:
                    out.extend(self._prompts_from_file(path, start, end, min_chars))
                except OSError:
                    continue
        out.sort(key=lambda p: p.timestamp)
        return out

    def _ingest_file(
        self,
        path: Path,
        day_start: datetime,
        day_end: datetime,
        sessions: dict[str, AISession],
    ) -> None:
        session_id = path.stem
        project = "unknown"
        model: str | None = None
        # トークンは累積値の最大を採用（token_count が複数回出る）
        max_output_tokens = 0
        # セッション境界内で day フィルタされたイベントだけを集計
        touched = False
        user_turns = 0
        tool_counts: Counter = Counter()
        tool_errors = 0
        interruptions = 0
        api_calls = 0
        start: datetime | None = None
        end: datetime | None = None
        models: set[str] = set()

        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                top = record.get("type")
                payload = _payload(record)
                ts = _parse_ts(record)
                # session_meta はタイムスタンプ外でもメタを取る
                if top == "session_meta":
                    sid = payload.get("session_id") or payload.get("id")
                    if isinstance(sid, str) and sid:
                        session_id = sid
                    project = _project_from_cwd(payload.get("cwd"))
                    continue
                if top == "turn_context":
                    if isinstance(payload.get("model"), str) and payload["model"]:
                        model = payload["model"]
                        models.add(model)
                    if project == "unknown":
                        project = _project_from_cwd(payload.get("cwd"))
                    continue
                if ts is None or not (day_start <= ts < day_end):
                    continue
                touched = True
                start = ts if start is None else min(start, ts)
                end = ts if end is None else max(end, ts)

                if top == "event_msg":
                    et = payload.get("type")
                    if et == "user_message":
                        user_turns += 1
                    elif et == "turn_aborted":
                        interruptions += 1
                    elif et == "token_count":
                        info = payload.get("info")
                        if isinstance(info, dict):
                            total = info.get("total_token_usage")
                            if isinstance(total, dict):
                                ot = total.get("output_tokens")
                                if isinstance(ot, (int, float)):
                                    max_output_tokens = max(max_output_tokens, int(ot))
                    elif et == "agent_message":
                        api_calls += 1
                    continue

                if top == "response_item":
                    pt = payload.get("type")
                    if pt == "message" and payload.get("role") == "assistant":
                        api_calls += 1
                    elif pt == "function_call":
                        name = str(payload.get("name") or "function")
                        tool_counts[name] += 1
                    elif pt == "custom_tool_call":
                        name = str(payload.get("name") or "custom_tool")
                        tool_counts[name] += 1
                        status = str(payload.get("status") or "").lower()
                        if status and status not in ("completed", "success", "ok"):
                            tool_errors += 1
                    elif pt == "function_call_output":
                        if _tool_output_is_error(payload.get("output")):
                            tool_errors += 1
                    elif pt == "custom_tool_call_output":
                        if _tool_output_is_error(payload.get("output")):
                            tool_errors += 1

        if not touched or start is None or end is None:
            return
        if user_turns <= 0 and interruptions <= 0:
            return
        if model:
            models.add(model)
        sessions[session_id] = AISession(
            session_id=session_id,
            project=project,
            start=start,
            end=end,
            user_turns=user_turns,
            tool_counts=tool_counts,
            tool_errors=tool_errors,
            interruptions=interruptions,
            api_calls=api_calls,
            output_tokens=max_output_tokens,
            models=models,
            source="codex",
        )

    def _prompts_from_file(
        self,
        path: Path,
        start: datetime,
        end: datetime,
        min_chars: int,
    ) -> list[UserPrompt]:
        project = "unknown"
        out: list[UserPrompt] = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = _payload(record)
                if record.get("type") == "session_meta":
                    project = _project_from_cwd(payload.get("cwd"))
                    continue
                if record.get("type") != "event_msg":
                    continue
                if payload.get("type") != "user_message":
                    continue
                ts = _parse_ts(record)
                if ts is None or not (start <= ts < end):
                    continue
                text = payload.get("message")
                if not isinstance(text, str):
                    text = ""
                text = text.strip()
                if len(text) < min_chars:
                    continue
                out.append(
                    UserPrompt(
                        timestamp=ts,
                        project=project,
                        text=text,
                        source="codex",
                    )
                )
        return out
