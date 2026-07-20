"""AI Work Telemetry: Claude Code のセッションログ（JSONL）から「AI作業の質」を抽出する。

Claude Code は全セッションを ~/.claude/projects/<プロジェクト>/<セッションID>.jsonl に
ローカル保存している。ここから対象日のセッションを走査し、往復数・使用ツール・
エラー・中断・トークン量を集計する。ネットワークアクセスは一切行わない。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path

# ユーザーがツール実行を拒否/中断したときに tool_result に入る典型文言
_INTERRUPT_MARKERS = (
    "doesn't want to proceed",
    "user rejected",
    "request interrupted by user",
)


@dataclass
class AISession:
    session_id: str
    project: str
    start: datetime
    end: datetime
    user_turns: int = 0
    tool_counts: Counter = field(default_factory=Counter)
    tool_errors: int = 0
    interruptions: int = 0
    api_calls: int = 0
    output_tokens: int = 0
    models: set[str] = field(default_factory=set)
    # usage重複排除用: 1つのAPI応答が複数のJSONL行（thinking/text/tool_use毎）に
    # 分かれて記録され、各行が同じusageを繰り返し持つ
    seen_message_ids: set[str] = field(default_factory=set)

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60

    @property
    def is_fragmented(self) -> bool:
        """2往復以下の「細切れ」セッションか。"""
        return self.user_turns <= 2


def _parse_ts(record: dict) -> datetime | None:
    ts = record.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _content_items(record: dict) -> list:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _is_interruption(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _INTERRUPT_MARKERS)


def _project_name(record: dict, file_path: Path) -> str:
    cwd = record.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd).name or cwd
    # ディレクトリ名はパスを "-" つなぎにしたもの（例: -home-user-myproj）
    return file_path.parent.name.split("-")[-1] or file_path.parent.name


def _update_session(session: AISession, record: dict, ts: datetime) -> None:
    session.start = min(session.start, ts)
    session.end = max(session.end, ts)
    rtype = record.get("type")

    if rtype == "user" and not record.get("isMeta"):
        items = _content_items(record)
        tool_results = [i for i in items if isinstance(i, dict) and i.get("type") == "tool_result"]
        if tool_results:
            for tr in tool_results:
                text = json.dumps(tr.get("content", ""), ensure_ascii=False)
                if _is_interruption(text):
                    session.interruptions += 1
                elif tr.get("is_error"):
                    session.tool_errors += 1
        else:
            texts = [
                i.get("text", "") for i in items
                if isinstance(i, dict) and i.get("type") == "text"
            ]
            joined = " ".join(texts)
            if _is_interruption(joined):
                session.interruptions += 1
            elif joined.strip():
                session.user_turns += 1

    elif rtype == "assistant":
        msg = record.get("message")
        if not isinstance(msg, dict):
            msg = {}
        # 1回のAPI呼び出しは複数行（コンテンツブロック毎）に分割記録され、
        # 各行が同一の message.id と同一の usage を持つ。行ごとに加算すると
        # api_calls とトークン量が2〜3倍に膨らむため、初出のidのみ計上する
        msg_id = msg.get("id")
        first_line_of_call = not (isinstance(msg_id, str) and msg_id in session.seen_message_ids)
        if isinstance(msg_id, str):
            session.seen_message_ids.add(msg_id)
        if first_line_of_call:
            session.api_calls += 1
            usage = msg.get("usage", {})
            if isinstance(usage, dict):
                tokens = usage.get("output_tokens")
                if isinstance(tokens, (int, float)):
                    session.output_tokens += int(tokens)
        model = msg.get("model")
        if isinstance(model, str) and model and model != "<synthetic>":
            session.models.add(model)
        # tool_useブロックは行ごとに一度しか現れないため、行単位の計上でよい
        for item in _content_items(record):
            if isinstance(item, dict) and item.get("type") == "tool_use":
                session.tool_counts[str(item.get("name", "unknown"))] += 1


def scan_sessions(
    projects_dir: Path, day_start: datetime, day_end: datetime
) -> list[AISession]:
    """対象日のイベントを含む全セッションを走査・集計する。"""
    if not projects_dir.is_dir():
        return []

    sessions: dict[str, AISession] = {}
    for path in projects_dir.rglob("*.jsonl"):
        try:
            # 最終更新が対象日より前のファイルは対象日のイベントを含み得ないのでスキップ
            if datetime.fromtimestamp(path.stat().st_mtime, tz=day_start.tzinfo) < day_start:
                continue
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
                    if record.get("type") not in ("user", "assistant"):
                        continue
                    # サブエージェント（isSidechain）の行は親と同じsessionIdを持ち、
                    # その「user」プロンプトは親モデルが書いたもの。混ぜると往復数・
                    # ツール数・トークンが大きく水増しされる
                    if record.get("isSidechain"):
                        continue
                    # 自動コンパクションの要約はtype=userだがユーザー発話ではない
                    if record.get("isCompactSummary"):
                        continue
                    ts = _parse_ts(record)
                    if ts is None or not (day_start <= ts < day_end):
                        continue
                    sid = str(record.get("sessionId") or path.stem)
                    if sid not in sessions:
                        sessions[sid] = AISession(
                            session_id=sid,
                            project=_project_name(record, path),
                            start=ts,
                            end=ts,
                        )
                    _update_session(sessions[sid], record, ts)
        except OSError:
            continue

    # ユーザーの関与が皆無のセッション断片は数えない。深夜を跨いだセッションの
    # 翌日分（assistant継続イベントのみ）が「2往復以下の細切れセッション」として
    # 誤カウントされるのを防ぐ
    result = [s for s in sessions.values() if s.user_turns > 0 or s.interruptions > 0]
    result.sort(key=lambda s: s.start)
    return result


@dataclass
class UserPrompt:
    timestamp: datetime
    project: str
    text: str


def scan_user_prompts(
    projects_dir: Path, start: datetime, end: datetime, min_chars: int = 8
) -> list[UserPrompt]:
    """期間内にユーザーがClaude Codeへ送った「生の依頼文」を収集する。

    ツール実行結果・メタメッセージ・スラッシュコマンドのラッパー・中断は除外する。
    プロンプト資産化（promptmine）の入力になる。
    """
    if not projects_dir.is_dir():
        return []
    out: list[UserPrompt] = []
    for path in projects_dir.rglob("*.jsonl"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, tz=start.tzinfo) < start:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict) or record.get("type") != "user":
                        continue
                    if record.get("isMeta"):
                        continue
                    # サブエージェントへの指示は親モデルが書いた文章、コンパクション
                    # 要約は数十KBの自動生成テキスト。どちらも「ユーザーの依頼文」では
                    # ないため、プロンプト資産化の入力に混ぜない
                    if record.get("isSidechain") or record.get("isCompactSummary"):
                        continue
                    ts = _parse_ts(record)
                    if ts is None or not (start <= ts < end):
                        continue
                    items = _content_items(record)
                    if any(isinstance(i, dict) and i.get("type") == "tool_result" for i in items):
                        continue
                    text = " ".join(
                        i.get("text", "") for i in items
                        if isinstance(i, dict) and i.get("type") == "text"
                    ).strip()
                    if len(text) < min_chars or _is_interruption(text):
                        continue
                    if text.startswith("<") and ("command-" in text[:40] or "local-command" in text[:40]):
                        continue
                    out.append(
                        UserPrompt(timestamp=ts, project=_project_name(record, path), text=text)
                    )
        except OSError:
            continue
    out.sort(key=lambda p: p.timestamp)
    return out


def _fmt_minutes(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def render_aiwork_markdown(
    sessions: list[AISession], tz: tzinfo, max_rows: int = 15
) -> str:
    """「AI作業の質」セクションのMarkdownを生成する。セッションが無ければ空文字。"""
    if not sessions:
        return ""

    total_turns = sum(s.user_turns for s in sessions)
    fragmented = sum(1 for s in sessions if s.is_fragmented)
    tool_errors = sum(s.tool_errors for s in sessions)
    interruptions = sum(s.interruptions for s in sessions)
    output_tokens = sum(s.output_tokens for s in sessions)
    avg_turns = total_turns / len(sessions) if sessions else 0.0
    all_tools = Counter()
    for s in sessions:
        all_tools.update(s.tool_counts)
    top_tools = ", ".join(f"{name}×{n}" for name, n in all_tools.most_common(5))

    lines: list[str] = []
    lines.append("### 🧠 AI作業の質（Claude Code）")
    lines.append("")
    lines.append(
        f"セッション: {len(sessions)}回 / ユーザー発話: {total_turns}回"
        f"（平均 {avg_turns:.1f}回/セッション、2往復以下の細切れ: {fragmented}回）"
    )
    lines.append(
        f"ツールエラー: {tool_errors}回 / ユーザー中断・拒否: {interruptions}回"
        f" / 出力トークン: {output_tokens:,}"
    )
    if top_tools:
        lines.append(f"主なツール: {top_tools}")
    lines.append("")

    rows = sessions[:max_rows]
    lines.append("| 時刻 | プロジェクト | 往復 | ツール実行 | エラー | 中断 |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
    for s in rows:
        start = s.start.astimezone(tz).strftime("%H:%M")
        end = s.end.astimezone(tz).strftime("%H:%M")
        project = s.project.replace("|", "\\|")
        lines.append(
            f"| {start}-{end} | {project} | {s.user_turns} "
            f"| {sum(s.tool_counts.values())} | {s.tool_errors} | {s.interruptions} |"
        )
    if len(sessions) > max_rows:
        lines.append("")
        lines.append(f"（他 {len(sessions) - max_rows} セッション省略）")
    lines.append("")
    return "\n".join(lines)
