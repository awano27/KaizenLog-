"""Codex CLI テレメトリアダプタ。

実ログ形式（2026-07 時点・本機 ~/.codex/sessions）:
  sessions/YYYY/MM/DD/rollout-*.jsonl
  各行: {\"type\": \"session_meta\"|\"event_msg\"|\"response_item\"|\"turn_context\"|...,
         \"timestamp\": ISO, \"payload\": {...}}

想定との差異（完了報告用）:
  - ツールは function_call / custom_tool_call（Claude の tool_use とは別名）
  - ユーザー発話は event_msg/user_message（response_item/message role=user も存在）
  - 中断は event_msg/turn_aborted
  - トークンは event_msg/token_count.info.total_token_usage（累積）
  - 日跨ぎは「ウィンドウ前の最大累積」を base とし当日差分のみ計上
  - 同一 session_id の複数 rollout はカウンタを加算マージ（resume 対応）
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
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


@dataclass
class _SessionAccum:
    """同一 session_id を複数ファイルからマージするための中間状態。"""

    session_id: str
    project: str = "unknown"
    start: datetime | None = None
    end: datetime | None = None
    user_turns: int = 0
    tool_counts: Counter = field(default_factory=Counter)
    tool_errors: int = 0
    interruptions: int = 0
    # api_calls は形式が混在すると二重になるため系統を分ける。
    # response_item/message が1件でもあればそちらを採用（構造化ログ優先）。
    api_calls_response_item: int = 0
    api_calls_event_msg: int = 0
    has_response_item_assistant: bool = False
    # 累積トークン: ウィンドウ前の最大を base、窓内最大 − base が当日分
    token_base: int | None = None
    token_peak: int | None = None
    models: set[str] = field(default_factory=set)
    touched: bool = False

    def add_token(self, ts: datetime, ot: int, day_start: datetime, day_end: datetime) -> None:
        if ts < day_start:
            self.token_base = ot if self.token_base is None else max(self.token_base, ot)
        elif day_start <= ts < day_end:
            self.token_peak = ot if self.token_peak is None else max(self.token_peak, ot)

    def day_output_tokens(self) -> int:
        if self.token_peak is None:
            return 0
        if self.token_base is None:
            return self.token_peak
        return max(0, self.token_peak - self.token_base)

    def api_calls(self) -> int:
        if self.has_response_item_assistant:
            return self.api_calls_response_item
        return self.api_calls_event_msg

    def to_session(self) -> AISession | None:
        if not self.touched or self.start is None or self.end is None:
            return None
        if self.user_turns <= 0 and self.interruptions <= 0:
            return None
        return AISession(
            session_id=self.session_id,
            project=self.project,
            start=self.start,
            end=self.end,
            user_turns=self.user_turns,
            tool_counts=self.tool_counts,
            tool_errors=self.tool_errors,
            interruptions=self.interruptions,
            api_calls=self.api_calls(),
            output_tokens=self.day_output_tokens(),
            models=set(self.models),
            source="codex",
        )


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
        accums: dict[str, _SessionAccum] = {}
        for folder in _codex_day_dirs(self.sessions_dir, day_start, day_end):
            for path in folder.glob("rollout-*.jsonl"):
                try:
                    self._ingest_file(path, day_start, day_end, accums)
                except OSError:
                    continue
        result: list[AISession] = []
        for acc in accums.values():
            session = acc.to_session()
            if session is not None:
                result.append(session)
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
        accums: dict[str, _SessionAccum],
    ) -> None:
        # ファイル名 stem を仮 ID。session_meta で上書きし、同 sid はマージする
        file_sid = path.stem
        session_id = file_sid
        project = "unknown"
        model: str | None = None
        # このファイル内で触った ID（meta 後に確定した sid）
        active_sid = file_sid

        def acc_for(sid: str) -> _SessionAccum:
            if sid not in accums:
                accums[sid] = _SessionAccum(session_id=sid, project=project)
            a = accums[sid]
            if a.project == "unknown" and project != "unknown":
                a.project = project
            if model:
                a.models.add(model)
            return a

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
                        active_sid = sid
                    project = _project_from_cwd(payload.get("cwd"))
                    a = acc_for(active_sid)
                    if project != "unknown":
                        a.project = project
                    continue
                if top == "turn_context":
                    if isinstance(payload.get("model"), str) and payload["model"]:
                        model = payload["model"]
                        acc_for(active_sid).models.add(model)
                    if project == "unknown":
                        project = _project_from_cwd(payload.get("cwd"))
                        if project != "unknown":
                            acc_for(active_sid).project = project
                    continue
                if ts is None:
                    continue

                # トークンは窓外でも base 用に読む（前日ディレクトリの累積）
                if top == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if isinstance(info, dict):
                        total = info.get("total_token_usage")
                        if isinstance(total, dict):
                            ot = total.get("output_tokens")
                            if isinstance(ot, (int, float)):
                                acc_for(active_sid).add_token(
                                    ts, int(ot), day_start, day_end
                                )

                if not (day_start <= ts < day_end):
                    continue

                a = acc_for(active_sid)
                a.touched = True
                a.start = ts if a.start is None else min(a.start, ts)
                a.end = ts if a.end is None else max(a.end, ts)

                if top == "event_msg":
                    et = payload.get("type")
                    if et == "user_message":
                        a.user_turns += 1
                    elif et == "turn_aborted":
                        a.interruptions += 1
                    elif et == "agent_message":
                        a.api_calls_event_msg += 1
                    continue

                if top == "response_item":
                    pt = payload.get("type")
                    if pt == "message" and payload.get("role") == "assistant":
                        a.has_response_item_assistant = True
                        a.api_calls_response_item += 1
                    elif pt == "function_call":
                        name = str(payload.get("name") or "function")
                        a.tool_counts[name] += 1
                    elif pt == "custom_tool_call":
                        name = str(payload.get("name") or "custom_tool")
                        a.tool_counts[name] += 1
                        status = str(payload.get("status") or "").lower()
                        if status and status not in ("completed", "success", "ok"):
                            a.tool_errors += 1
                    elif pt == "function_call_output":
                        if _tool_output_is_error(payload.get("output")):
                            a.tool_errors += 1
                    elif pt == "custom_tool_call_output":
                        if _tool_output_is_error(payload.get("output")):
                            a.tool_errors += 1

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
