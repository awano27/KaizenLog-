"""AI Work Telemetry: Claude Code のセッションログ（JSONL）から「AI作業の質」を抽出する。

Claude Code は全セッションを ~/.claude/projects/<プロジェクト>/<セッションID>.jsonl に
ローカル保存している。ここから対象日のセッションを走査し、往復数・使用ツール・
エラー・中断・トークン量を集計する。ネットワークアクセスは一切行わない。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from difflib import SequenceMatcher
from math import isfinite
from pathlib import Path
from typing import Protocol, runtime_checkable

# リトライ連鎖: 「ほぼ同文の再送」を捕まえる。promptmine の 0.6 より高いのは
# 言い直し・言い換えクラスタではなく、短時間のほぼ同一依頼を対象にするため。
_RETRY_WINDOW_MINUTES = 30
_RETRY_SIMILARITY = 0.85
_MEASURABLE_TOOL_SOURCES = frozenset({"claude-code", "codex"})

# セッション内容列: 最初の依頼文の先頭 N 字
SESSION_TITLE_MAX = 40
# システム注入 XML: <task-notification> / <command-name> 等
_SYSTEM_XML_TAG_RE = re.compile(r"^<[a-z][a-z0-9_-]*(\s|>|/)", re.IGNORECASE)
# 依頼長さの層別閾値（字）。因果断定せず観察値のみ出す。
PROMPT_LENGTH_SHORT = 80

# 変更系ツール（成果プロキシ「編集した」）。正しさの判定はしない。
_EDIT_TOOLS = frozenset(
    {
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
        "apply_patch",
        "ApplyPatch",
        "str_replace",
        "create_file",
        "delete_file",
    }
)
_TEST_CMD_MARKERS = (
    "pytest",
    "python -m pytest",
    "npm test",
    "npm run test",
    "pnpm test",
    "yarn test",
    "cargo test",
    "go test",
    "vitest",
    "jest",
)

# ユーザーがツール実行を拒否/中断したときに tool_result に入る典型文言
_INTERRUPT_MARKERS = (
    "doesn't want to proceed",
    "user rejected",
    "request interrupted by user",
)

# 既定単価表: モデル名の部分一致（小文字）→ USD / 100万 output tokens。
# 目安のみ。市場単価は変動するため [aiwork.pricing] で上書き・追加すること。
DEFAULT_OUTPUT_USD_PER_MTOK: list[tuple[str, float]] = [
    ("claude-opus-4", 15.0),
    ("claude-opus", 15.0),
    ("claude-sonnet-4", 3.0),
    ("claude-sonnet", 3.0),
    ("claude-haiku", 1.25),
    # より具体的なパターンを先に（gpt-4o が gpt-4o-mini に部分一致しないよう）
    ("gpt-4o-mini", 0.6),
    ("gpt-4o", 10.0),
    ("gpt-4.1", 10.0),
    ("o3", 40.0),
    ("o4-mini", 4.4),
]


def resolve_output_price(
    model: str | None,
    pricing: dict[str, float] | None = None,
) -> float | None:
    """モデル名から output 単価（USD/MTok）を返す。未登録は None。"""
    if not model:
        return None
    m = model.lower()
    # config 上書きを先に（ユーザーパターン優先）
    table: list[tuple[str, float]] = []
    if pricing:
        table.extend((str(k).lower(), float(v)) for k, v in pricing.items())
    table.extend(DEFAULT_OUTPUT_USD_PER_MTOK)
    for pattern, price in table:
        if pattern and pattern in m:
            return float(price)
    return None


def estimate_sessions_cost(
    sessions: list[AISession],
    pricing: dict[str, float] | None = None,
) -> tuple[float, int, dict[str, dict]]:
    """セッション群の概算コスト。

    戻り値: (est_cost_usd, uncosted_tokens, per_source 内訳)
    モデル不明・単価未登録のトークンは uncosted に回し cost に含めない。
    """
    total_cost = 0.0
    uncosted = 0
    per_source: dict[str, dict] = {}
    for s in sessions:
        # ブラウザ等: output_tokens 未設定 → コスト集計から除外（文字数をトークンと偽らない）
        if not s.tools_measurable and not s.output_tokens:
            continue
        src = s.source.strip() if isinstance(s.source, str) and s.source.strip() else "unknown"
        bucket = per_source.setdefault(
            src, {"est_cost_usd": 0.0, "uncosted_tokens": 0, "output_tokens": 0}
        )
        bucket["output_tokens"] += int(s.output_tokens or 0)
        price = _resolve_session_price_costing(
            set(s.models) if s.models else None,
            pricing,
        )
        if price is None or not s.output_tokens:
            uncosted += int(s.output_tokens or 0)
            bucket["uncosted_tokens"] += int(s.output_tokens or 0)
            continue
        cost = (s.output_tokens / 1_000_000.0) * price
        total_cost += cost
        bucket["est_cost_usd"] = float(bucket["est_cost_usd"]) + cost
    return round(total_cost, 4), uncosted, per_source


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
    source: str = "claude-code"  # テレメトリアダプタ ID
    # 内容: 最初のユーザー依頼の先頭字（ラッパー除去後）。未抽出は None
    title: str | None = None
    # 層別用: 初回依頼の文字数（タイトル切詰前）
    first_prompt_len: int = 0
    # 成果プロキシ（正しさは判定しない — 変更した/テストした/末尾エラーのみ）
    edits: int = 0
    tests_run: bool = False
    ended_in_error: bool = False
    # 走査中: 直近ツール結果がエラーか（セッション終了時に ended_in_error へ）
    _last_tool_error: bool = field(default=False, repr=False)
    # False: ブラウザ等でツール概念が無い → 表では 0 ではなく `-`
    tools_measurable: bool = True
    # ブラウザ応答の文字数（トークンではない。コスト/output_tokens に混ぜない）
    assistant_chars: int = 0
    # セッション cwd の実パス（表示名 project とは別。stats には保存しない）
    repo_path: str | None = None
    # KaizenLog 自身の LLM 呼び出し（advise 等）— 全指標から除外
    is_internal: bool = False

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60

    @property
    def is_fragmented(self) -> bool:
        """2往復以下の「細切れ」セッションか。"""
        return self.user_turns <= 2

    def friction_score(self, retry_chain_touch: int = 0) -> int:
        """摩擦スコア: エラー + 中断×5 + リトライ連鎖関与×5。"""
        return (
            int(self.tool_errors)
            + int(self.interruptions) * 5
            + int(retry_chain_touch) * 5
        )


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


def _is_system_wrapper(text: str) -> bool:
    """システム注入 XML / スラッシュコマンドラッパーか。

    user_turns・プロンプト資産化・リトライ連鎖・guard から除外する。
    `< 1000円` のようなタグ形式でない文は除外しない。
    """
    if not text:
        return False
    stripped = text.lstrip()
    if not stripped.startswith("<"):
        return False
    if _SYSTEM_XML_TAG_RE.match(stripped):
        return True
    # 旧 command 系（タグ正規表現外の変形があっても拾う）
    head = stripped[:40]
    return "command-" in head or "local-command" in head


def _is_command_wrapper(text: str) -> bool:
    """後方互換エイリアス（=_is_system_wrapper）。"""
    return _is_system_wrapper(text)


def normalize_prompt_text(text: str) -> str:
    """改行・連続空白を畳んで一行化する。"""
    return " ".join((text or "").split())


def bundled_prompt_head_prefixes() -> tuple[str, ...]:
    """同梱 prompts/*.md 各ファイルの最初の非空行（正規化）。

    テンプレ改訂に自動追従する（ハードコードしない）。過去ログ除外用。
    """
    from importlib import resources

    prefixes: list[str] = []
    try:
        root = resources.files("kaizenlog") / "prompts"
    except (TypeError, FileNotFoundError, ModuleNotFoundError, AttributeError):
        return tuple()
    try:
        names = sorted(p.name for p in root.iterdir() if p.name.endswith(".md"))
    except (OSError, AttributeError):
        return tuple()
    for name in names:
        try:
            text = (root / name).read_text(encoding="utf-8")
        except (OSError, TypeError, AttributeError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if line:
                prefixes.append(normalize_prompt_text(line))
                break
    return tuple(prefixes)


def is_kaizenlog_internal_text(text: str) -> bool:
    """セッション初回ユーザー文が KaizenLog 内部呼び出しなら True。

    - センチネル [kaizenlog-internal]
    - 同梱プロンプト先頭行との前方一致（導入前の過去ログ用）
    """
    if not text:
        return False
    if "[kaizenlog-internal]" in text:
        return True
    head = normalize_prompt_text(text)
    if not head:
        return False
    for prefix in bundled_prompt_head_prefixes():
        if prefix and head.startswith(prefix):
            return True
    return False


def _prefer_path_basename(text: str, max_chars: int) -> str:
    """長いパスは末尾要素優先（...docs\\file.md）。"""
    t = text
    if len(t) <= max_chars:
        return t
    if "\\" not in t and "/" not in t:
        return t
    sep = "\\" if t.count("\\") >= t.count("/") else "/"
    parts = [p for p in re.split(r"[\\/]", t) if p]
    if not parts:
        return t
    base = parts[-1]
    if len(base) >= max_chars - 3:
        return "..." + base[-(max_chars - 3) :]
    if len(parts) >= 2:
        cand = f"...{parts[-2]}{sep}{base}"
        if len(cand) <= max_chars:
            return cand
    cand2 = f"...{base}"
    if len(cand2) <= max_chars:
        return cand2
    return "..." + base[-(max_chars - 3) :]


def session_title_from_text(text: str, max_chars: int = SESSION_TITLE_MAX) -> str:
    """依頼文から表用 title。ラッパーは呼び出し側で除外済み想定。

    ファイルパスは先頭切りではなく末尾要素優先。
    """
    t = normalize_prompt_text(text)
    t = _prefer_path_basename(t, max_chars)
    if len(t) <= max_chars:
        return t
    return t[:max_chars]


def extract_session_title(
    text: str, max_chars: int = SESSION_TITLE_MAX
) -> tuple[str, int] | None:
    """初回依頼から (title, 整形後文字数) を返す。ラッパー・空は None。

    Claude / Codex 両アダプタの共通整形（ラッパー除去→一行化→40字切詰）。
    システム XML は None（呼び出し側が次の実ユーザー発話へフォールバック）。
    """
    if not text or _is_system_wrapper(text):
        return None
    cleaned = normalize_prompt_text(text)
    if not cleaned:
        return None
    return session_title_from_text(cleaned, max_chars=max_chars), len(cleaned)


def _looks_like_test_command(command: str) -> bool:
    low = (command or "").lower()
    return any(m in low for m in _TEST_CMD_MARKERS)


def _count_edit_tool(name: str) -> bool:
    n = name or ""
    if n in _EDIT_TOOLS:
        return True
    low = n.lower()
    return low in {x.lower() for x in _EDIT_TOOLS} or "apply_patch" in low


def _note_tool_use(session: AISession, name: str, tool_input: object = None) -> None:
    session.tool_counts[str(name or "unknown")] += 1
    if _count_edit_tool(str(name or "")):
        session.edits += 1
    # Bash 等のコマンドからテスト実行を検出
    if str(name) in ("Bash", "bash", "Shell", "shell", "local_shell"):
        cmd = ""
        if isinstance(tool_input, dict):
            cmd = str(
                tool_input.get("command")
                or tool_input.get("cmd")
                or tool_input.get("input")
                or ""
            )
        elif isinstance(tool_input, str):
            cmd = tool_input
        if _looks_like_test_command(cmd):
            session.tests_run = True
    # Codex の test 系ツール名
    if "test" in str(name).lower() and str(name).lower() not in ("get_test",):
        if any(m in str(name).lower() for m in ("pytest", "jest", "vitest")):
            session.tests_run = True


def _maybe_set_title(session: AISession, text: str) -> None:
    if session.title is not None:
        return
    # 初回ユーザー文で内部呼び出し判定（title 切詰前の全文を見る）
    if is_kaizenlog_internal_text(text):
        session.is_internal = True
    extracted = extract_session_title(text)
    if extracted is None:
        return
    title, length = extracted
    session.first_prompt_len = length
    session.title = title


def _update_session(session: AISession, record: dict, ts: datetime) -> None:
    session.start = min(session.start, ts)
    session.end = max(session.end, ts)
    rtype = record.get("type")

    if rtype == "user" and not record.get("isMeta"):
        items = _content_items(record)
        tool_results = [i for i in items if isinstance(i, dict) and i.get("type") == "tool_result"]
        if tool_results:
            any_err = False
            for tr in tool_results:
                text = json.dumps(tr.get("content", ""), ensure_ascii=False)
                if _is_interruption(text):
                    session.interruptions += 1
                elif tr.get("is_error"):
                    session.tool_errors += 1
                    any_err = True
            session._last_tool_error = any_err
            session.ended_in_error = any_err
        else:
            texts = [
                i.get("text", "") for i in items
                if isinstance(i, dict) and i.get("type") == "text"
            ]
            joined = " ".join(texts)
            if _is_interruption(joined):
                session.interruptions += 1
            elif joined.strip() and not _is_system_wrapper(joined):
                # システム注入・コマンドラッパーはユーザー往復に数えない
                session.user_turns += 1
                _maybe_set_title(session, joined)

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
                _note_tool_use(
                    session,
                    str(item.get("name", "unknown")),
                    item.get("input"),
                )
                # ツール起動時点では未解決扱いに戻す（結果待ち）
                session._last_tool_error = False
                session.ended_in_error = False


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
                        repo_path = None
                        cwd = record.get("cwd")
                        if isinstance(cwd, str) and cwd.strip():
                            try:
                                cwd_p = Path(cwd).expanduser()
                                if cwd_p.is_dir():
                                    repo_path = str(cwd_p.resolve())
                            except (OSError, RuntimeError):
                                repo_path = None
                        sessions[sid] = AISession(
                            session_id=sid,
                            project=_project_name(record, path),
                            start=ts,
                            end=ts,
                            source="claude-code",
                            repo_path=repo_path,
                        )
                    # 同一セッション内の cwd 変更は最初の値を保持（上書きしない）
                    sess = sessions[sid]
                    if sess.repo_path is None:
                        cwd = record.get("cwd")
                        if isinstance(cwd, str) and cwd.strip():
                            try:
                                cwd_p = Path(cwd).expanduser()
                                if cwd_p.is_dir():
                                    sess.repo_path = str(cwd_p.resolve())
                            except (OSError, RuntimeError):
                                pass
                    _update_session(sess, record, ts)
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
    source: str = "claude-code"


@dataclass
class RetryChain:
    """短時間にほぼ同文で再依頼されたプロンプト列。"""

    project: str
    prompts: list[UserPrompt]  # 時系列順、2件以上

    @property
    def length(self) -> int:
        return len(self.prompts)


def detect_retry_chains(
    prompts: list[UserPrompt],
    window_minutes: int = _RETRY_WINDOW_MINUTES,
    similarity: float = _RETRY_SIMILARITY,
) -> list[RetryChain]:
    """同一 project 内のほぼ同文・短時間再送をチェーンとして検出する。

    各プロンプトは進行中チェーンの末尾とだけ比較する（全ペア比較はしない）。
    window_minutes ちょうどは連結、超えたら分断。
    """
    # 遅延 import: promptmine → aiwork.UserPrompt の循環を避ける
    from .promptmine import normalize

    # プロジェクトごとの進行中チェーン（末尾と比較）
    open_chains: dict[str, list[UserPrompt]] = {}
    completed: list[RetryChain] = []

    for p in prompts:
        current = open_chains.get(p.project)
        if current is None:
            open_chains[p.project] = [p]
            continue
        last = current[-1]
        delta_min = (p.timestamp - last.timestamp).total_seconds() / 60.0
        if delta_min < 0:
            # 時刻逆行は安全側で新規チェーン
            if len(current) >= 2:
                completed.append(RetryChain(project=p.project, prompts=list(current)))
            open_chains[p.project] = [p]
            continue
        ratio = SequenceMatcher(
            None, normalize(last.text), normalize(p.text)
        ).ratio()
        if delta_min <= window_minutes and ratio >= similarity:
            current.append(p)
        else:
            if len(current) >= 2:
                completed.append(RetryChain(project=p.project, prompts=list(current)))
            open_chains[p.project] = [p]

    for project, current in open_chains.items():
        if len(current) >= 2:
            completed.append(RetryChain(project=project, prompts=list(current)))
    return completed


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
                    if _is_system_wrapper(text):
                        continue
                    if is_kaizenlog_internal_text(text):
                        continue
                    out.append(
                        UserPrompt(
                            timestamp=ts,
                            project=_project_name(record, path),
                            text=text,
                            source="claude-code",
                        )
                    )
        except OSError:
            continue
    out.sort(key=lambda p: p.timestamp)
    return out


@runtime_checkable
class TelemetryAdapter(Protocol):
    """AI作業テレメトリのソース抽象。"""

    name: str  # "claude-code" | "codex"

    def scan_sessions(
        self, day_start: datetime, day_end: datetime
    ) -> list[AISession]: ...

    def scan_user_prompts(
        self, start: datetime, end: datetime, min_chars: int = 8
    ) -> list[UserPrompt]: ...


@dataclass
class ClaudeCodeAdapter:
    """Claude Code の ~/.claude/projects JSONL アダプタ。"""

    projects_dir: Path
    name: str = "claude-code"

    def scan_sessions(
        self, day_start: datetime, day_end: datetime
    ) -> list[AISession]:
        return scan_sessions(self.projects_dir, day_start, day_end)

    def scan_user_prompts(
        self, start: datetime, end: datetime, min_chars: int = 8
    ) -> list[UserPrompt]:
        return scan_user_prompts(self.projects_dir, start, end, min_chars=min_chars)


def available_adapters(cfg) -> list[TelemetryAdapter]:
    """設定とディレクトリ存在から有効なテレメトリアダプタを返す。"""
    adapters: list[TelemetryAdapter] = []
    if not getattr(cfg, "aiwork", None) or not cfg.aiwork.enabled:
        return adapters
    claude_dir = Path(cfg.aiwork.claude_projects_dir).expanduser()
    if claude_dir.is_dir():
        adapters.append(ClaudeCodeAdapter(claude_dir))
    codex_dir = Path(
        getattr(cfg.aiwork, "codex_sessions_dir", "~/.codex/sessions")
    ).expanduser()
    if codex_dir.is_dir():
        from .aiwork_codex import CodexAdapter

        adapters.append(CodexAdapter(codex_dir))
    browser_dir = Path(
        getattr(cfg.aiwork, "browser_export_dir", "~/Downloads/kaizenlog-browser-ai")
    ).expanduser()
    if browser_dir.is_dir():
        from .aiwork_browser import BrowserAIAdapter

        adapters.append(BrowserAIAdapter(browser_dir))
    return adapters


def collect_ai_telemetry(
    adapters: list[TelemetryAdapter],
    day_start: datetime,
    day_end: datetime,
) -> tuple[list[AISession], list[UserPrompt], int]:
    """全アダプタのセッション・プロンプトをマージして時系列ソートする。

    戻り値: (user sessions, user prompts, internal_ai_sessions 件数)
    KaizenLog 自身の LLM 呼び出しセッションは除外し、件数だけ返す。
    """
    sessions: list[AISession] = []
    prompts: list[UserPrompt] = []
    for adapter in adapters:
        try:
            sessions.extend(adapter.scan_sessions(day_start, day_end))
            prompts.extend(adapter.scan_user_prompts(day_start, day_end))
        except OSError:
            continue
    # アダプタ側で is_internal が立っていなくても、title/初回文から再判定
    internal_n = 0
    kept: list[AISession] = []
    for s in sessions:
        if s.is_internal:
            internal_n += 1
            continue
        # title は切詰後なのでセンチネル短文は title に残る。テンプレ先頭は
        # first_prompt_len と title だけでは足りない場合があるため、
        # スキャン時の is_internal を主とする。
        kept.append(s)
    sessions = kept
    prompts = [p for p in prompts if not is_kaizenlog_internal_text(p.text)]
    sessions.sort(key=lambda s: s.start)
    prompts.sort(key=lambda p: p.timestamp)
    return sessions, prompts, internal_n


def _fmt_minutes(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _md_cell(value: object) -> str:
    """表セル用: 一行化と | エスケープ。"""
    text = " ".join(str(value if value is not None else "").split())
    return text.replace("|", "\\|")


def _normalize_screen_tool_minutes(value: object) -> dict[str, float]:
    """画面AI時間を表示・保存可能な有限の非負数へ限定する。"""
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, float] = {}
    for tool, minutes in value.items():
        if not isinstance(minutes, (int, float)) or isinstance(minutes, bool):
            continue
        number = float(minutes)
        if not isfinite(number) or number < 0:
            continue
        normalized[str(tool)] = number
    return normalized


def prompt_length_observation(sessions: Sequence[AISession]) -> str | None:
    """依頼長さ層別の観察1行。両層に2セッション以上ある日のみ。

    因果（短いから失敗）は断定しない。平均エラー等の併記のみ。
    """
    short = [s for s in sessions if 0 < s.first_prompt_len < PROMPT_LENGTH_SHORT]
    long = [s for s in sessions if s.first_prompt_len >= PROMPT_LENGTH_SHORT]
    if len(short) < 2 or len(long) < 2:
        return None

    def _avg_err(xs: list[AISession]) -> float:
        return sum(s.tool_errors for s in xs) / len(xs)

    def _avg_turns(xs: list[AISession]) -> float:
        return sum(s.user_turns for s in xs) / len(xs)

    return (
        f"依頼の長さ別: 短い依頼({PROMPT_LENGTH_SHORT}字未満) {len(short)}回"
        f"（平均往復{_avg_turns(short):.1f}・平均エラー{_avg_err(short):.1f}）"
        f" / 詳細な依頼 {len(long)}回"
        f"（平均往復{_avg_turns(long):.1f}・平均エラー{_avg_err(long):.1f}）"
        "。観察値のみ（因果は断定しない）"
    )


def retry_chain_excerpts(
    chains: Sequence[RetryChain],
    *,
    redactor: Callable[[str], str] | None = None,
    max_chains: int = 3,
) -> list[str]:
    """連鎖起点依頼の抜粋（redact→正規化→先頭30字+「…」）。"""
    grouped: dict[tuple[str, str], int] = {}
    for chain in chains:
        if not chain.prompts:
            continue
        project = chain.project
        if redactor is not None:
            project = redactor(project)
        raw = chain.prompts[0].text or ""
        if redactor is not None:
            raw = redactor(raw)
        raw = normalize_prompt_text(raw)
        key = (project, raw)
        grouped[key] = grouped.get(key, 0) + 1

    out: list[str] = []
    for (proj, raw), count in list(grouped.items())[:max_chains]:
        if not raw:
            excerpt = "（依頼本文が無いため省略）"
        else:
            # 切詰めは結果が上限字数になる規約（digest / nippou と統一）
            excerpt = raw if len(raw) <= 30 else raw[:29] + "…"
            excerpt = _md_cell(excerpt)
        if count > 1:
            excerpt += f" ×{count}件"
        out.append(f"連鎖起点（{_md_cell(proj)}）: {excerpt}")
    return out


@dataclass
class LoopTaxEpisode:
    """リトライ連鎖1本を会計単位（エピソード）とみなす。"""

    chain: RetryChain
    wasted_tokens: int | None  # 最終試行を除くセッション output tokens 合算。不明は None
    has_tool_error: bool = False
    model: str | None = None


@dataclass
class LoopTaxSummary:
    episodes: list[LoopTaxEpisode]
    total_wasted_tokens: int | None  # 全て不明なら None
    est_cost_usd: float | None  # トークン不明 or 単価不明なら None
    tokens_known: bool

    @property
    def episode_count(self) -> int:
        return len(self.episodes)


def _session_for_prompt(
    prompt: UserPrompt, sessions: Sequence[AISession]
) -> AISession | None:
    hits = [
        s
        for s in sessions
        if s.project == prompt.project and s.start <= prompt.timestamp <= s.end
    ]
    if not hits:
        day = prompt.timestamp.date()
        hits = [
            s
            for s in sessions
            if s.project == prompt.project and s.start.date() == day
        ]
    if not hits:
        return None
    hits.sort(key=lambda s: abs((s.start - prompt.timestamp).total_seconds()))
    return hits[0]


def _resolve_session_price(
    models: set[str] | None,
    pricing: dict[str, float] | None,
) -> tuple[float | None, str | None]:
    """セッションのモデル群から単価を決定論的に解決する。

    ループ税の金額用（第28弾 §R4: 単価が割れたら fail-closed で「不明」）。
    コスト概算は登録済みトークンを未登録と偽らないため
    `_resolve_session_price_costing` を使うこと。

    戻り値: (price | None, representative_model | None)
    - モデル無し → (None, None)
    - 複数モデルで解決単価が異なる/一部未登録 → (None, sorted first model)
    - 全モデルが同一既知単価 → (price, sorted first model)
    """
    if not models:
        return None, None
    ordered = sorted(str(m) for m in models if m)
    if not ordered:
        return None, None
    prices: list[float] = []
    for m in ordered:
        p = resolve_output_price(m, pricing)
        if p is None:
            return None, ordered[0]
        prices.append(float(p))
    if len(set(prices)) != 1:
        return None, ordered[0]
    return prices[0], ordered[0]


def _resolve_session_price_costing(
    models: set[str] | None,
    pricing: dict[str, float] | None,
) -> float | None:
    """コスト概算用の単価解決（決定論・全モデル登録済みなら上限単価）。

    一部でも未登録なら None（そのトークンは uncosted へ）。全モデル登録済みなら
    単価が割れていても上限単価で換算する。ここで None を返すと日誌が
    「単価未登録が N tok」と表示し、登録済みのトークンを未登録と偽ることになる。
    """
    if not models:
        return None
    prices: list[float] = []
    for m in sorted(str(x) for x in models if x):
        p = resolve_output_price(m, pricing)
        if p is None:
            return None
        prices.append(float(p))
    return max(prices) if prices else None


def compute_loop_tax(
    chains: Sequence[RetryChain],
    sessions: Sequence[AISession],
    *,
    pricing: dict[str, float] | None = None,
) -> LoopTaxSummary:
    """リトライ連鎖の浪費トークン・金額をエピソード単位で集計する。

    浪費トークン: チェーンの最終試行を除く試行が属するセッションの
    output tokens をセッション単位で合算（按分なし）。
    日次合計はエピソード横断で session_id 一意（多重計上しない）。
    1件でも不明があれば総量・総額を既知部分だけで断定しない。
    """
    episodes: list[LoopTaxEpisode] = []
    any_unknown_tokens = False
    any_unknown_cost = False
    # 日次合計用: session_id → (tokens, cost_or_None)
    unique_sess_tokens: dict[str, int] = {}
    unique_sess_cost: dict[str, float | None] = {}

    for chain in chains:
        if len(chain.prompts) < 2:
            continue
        waste_prompts = chain.prompts[:-1]
        sess_ids: set[str] = set()
        wasted: int | None = 0
        known = True
        model: str | None = None
        has_err = False
        ep_cost = 0.0
        ep_cost_ok = True
        matched_any = False
        for p in waste_prompts:
            s = _session_for_prompt(p, sessions)
            if s is None:
                known = False
                continue
            matched_any = True
            if s.tool_errors:
                has_err = True
            if s.session_id in sess_ids:
                continue
            sess_ids.add(s.session_id)
            price, rep_model = _resolve_session_price(
                s.models if s.models else None, pricing
            )
            if rep_model and model is None:
                model = rep_model
            if not s.output_tokens:
                known = False
                ep_cost_ok = False
                # 不明セッションも unique に記録しない（tokens 不明扱い）
                continue
            tok = int(s.output_tokens)
            wasted = int(wasted or 0) + tok
            # 日次: 同一 session は1回のみ
            if s.session_id not in unique_sess_tokens:
                unique_sess_tokens[s.session_id] = tok
                if price is None:
                    unique_sess_cost[s.session_id] = None
                else:
                    unique_sess_cost[s.session_id] = (tok / 1_000_000.0) * price
            if price is None:
                ep_cost_ok = False
            else:
                ep_cost += (tok / 1_000_000.0) * price
        # waste prompt に対応セッションが1件も無い → tokens不明
        if not matched_any:
            wasted = None
            known = False
            ep_cost_ok = False
        elif not known:
            wasted = None
            ep_cost_ok = False

        if wasted is None:
            any_unknown_tokens = True
        if not ep_cost_ok:
            any_unknown_cost = True

        episodes.append(
            LoopTaxEpisode(
                chain=chain,
                wasted_tokens=wasted,
                has_tool_error=has_err,
                model=model,
            )
        )

    if not episodes:
        return LoopTaxSummary(
            episodes=[],
            total_wasted_tokens=0,
            est_cost_usd=0.0,
            tokens_known=True,
        )
    # 日次合計: session 一意。1件でも不明エピソード → 総量断定しない
    if any_unknown_tokens:
        total_wasted: int | None = None
    else:
        total_wasted = sum(unique_sess_tokens.values())
    if any_unknown_tokens or any_unknown_cost or any(
        v is None for v in unique_sess_cost.values()
    ):
        est: float | None = None
    elif unique_sess_cost:
        est = round(sum(float(v or 0) for v in unique_sess_cost.values()), 4)
    else:
        est = 0.0 if total_wasted == 0 else None

    return LoopTaxSummary(
        episodes=episodes,
        total_wasted_tokens=total_wasted,
        est_cost_usd=est,
        tokens_known=not any_unknown_tokens,
    )


def format_loop_tax_line(
    summary: LoopTaxSummary,
    *,
    usd_jpy: float | None = None,
    day_output_tokens: int | None = None,
    redactor: Callable[[str], str] | None = None,
) -> str:
    """日誌・status 用1行。不明は 0 にしない。金額不明時は「金額不明」。

    §E3: 金額もトークンも不明なら空文字（行ごと出さない）。
    """
    n = summary.episode_count
    if n == 0:
        return "💸 ループ税: $0.00（0エピソード / 0 tokens）"
    if summary.total_wasted_tokens is None and summary.est_cost_usd is None:
        return ""
    if summary.total_wasted_tokens is None:
        tok_s = "tokens不明"
    else:
        token_format = "," if day_output_tokens is not None else ""
        tok_s = f"{summary.total_wasted_tokens:{token_format}} tokens"
    if summary.est_cost_usd is None:
        money = "金額不明"
    else:
        money = f"${summary.est_cost_usd:.2f}"
        if usd_jpy is not None and usd_jpy > 0:
            jpy = int(round(summary.est_cost_usd * usd_jpy))
            money = f"{money}（¥{jpy}）"
    line = (
        f"💸 ループ税: {money}（{n}エピソード / {tok_s}）"
        " ※エピソード間で同一セッションは1回のみ計上"
    )
    if day_output_tokens is None or summary.total_wasted_tokens is None:
        return line

    day_tokens = int(day_output_tokens)
    if day_tokens <= 0:
        return line
    ratio = (summary.total_wasted_tokens / day_tokens) * 100
    if ratio > 100:
        ratio_text = "100.0%（入力不整合のため上限）"
    else:
        ratio_text = f"{ratio:.1f}%"
    line += f" / 当日出力 {day_tokens:,} tok に対し {ratio_text}"

    max_ep = max_loop_episode(summary)
    if max_ep is None:
        return line
    excerpts = retry_chain_excerpts(
        [max_ep.chain],
        redactor=redactor,
        max_chains=1,
    )
    excerpt = excerpts[0] if excerpts else "連鎖起点: 不明"
    wasted = (
        f"浪費{max_ep.wasted_tokens:,} tok"
        if max_ep.wasted_tokens is not None
        else "浪費tokens不明"
    )
    has_error = "ツールエラーあり" if max_ep.has_tool_error else "ツールエラーなし"
    return (
        f"{line}\n"
        f"   — 最悪例: {max_ep.chain.length}往復 / {wasted}"
        f" / {has_error} / {excerpt}"
    )


def max_loop_episode(summary: LoopTaxSummary) -> LoopTaxEpisode | None:
    """往復数最大のエピソード。同点は既知浪費tokens多い方。"""
    if not summary.episodes:
        return None

    def key(ep: LoopTaxEpisode):
        # None tokens は -1 扱いで既知を優先
        tw = ep.wasted_tokens if ep.wasted_tokens is not None else -1
        return (ep.chain.length, tw)

    return max(summary.episodes, key=key)


def loop_tax_to_stats_dict(
    summary: LoopTaxSummary,
    *,
    redactor=None,
) -> dict:
    """stats[\"ai\"][\"loop_tax\"] 用の JSON 互換 dict。不明は null。"""
    max_ep = max_loop_episode(summary)
    max_d: dict | None = None
    if max_ep is not None:
        excerpts = retry_chain_excerpts(
            [max_ep.chain], redactor=redactor, max_chains=1
        )
        excerpt = excerpts[0] if excerpts else ""
        # "連鎖起点（proj）: text" 形式から本文だけにしてもよいが、そのまま保存
        max_d = {
            "length": max_ep.chain.length,
            "wasted_tokens": max_ep.wasted_tokens,
            "has_tool_error": bool(max_ep.has_tool_error),
            "excerpt": excerpt,
        }
    return {
        "episode_count": summary.episode_count,
        "total_wasted_tokens": summary.total_wasted_tokens,
        "est_cost_usd": summary.est_cost_usd,
        "max_episode": max_d,
    }


def _retry_touch_for_session(
    session: AISession, chains: Sequence[RetryChain]
) -> int:
    """セッション期間・プロジェクトが重なるリトライ連鎖の件数。

    新規検出はせず、既存 RetryChain（detect_retry_chains の結果）から導出する。
    """
    if not chains:
        return 0
    touch = 0
    for chain in chains:
        if chain.project != session.project:
            continue
        if not chain.prompts:
            continue
        # いずれかのプロンプト時刻がセッション時間帯に入れば関与
        for p in chain.prompts:
            ts = p.timestamp
            if session.start <= ts <= session.end:
                touch += 1
                break
    return touch


def session_digests_for_stats(
    sessions: Sequence[AISession],
    day: str,
    *,
    redactor: Callable[[str], str] | None = None,
    retry_chains: Sequence[RetryChain] | None = None,
) -> list[dict]:
    """stats 保存用のセッション要約（週次ワースト選定用）。"""
    chains = list(retry_chains or [])
    digests: list[dict] = []
    for s in sessions:
        title = s.title or ""
        if redactor is not None and title:
            title = redactor(title)
        retry_touch = _retry_touch_for_session(s, chains)
        tools_total = (
            sum(s.tool_counts.values()) if s.tools_measurable else None
        )
        digests.append(
            {
                "day": day,
                "session_id": s.session_id,
                "project": s.project,
                "title": title,
                "tool_errors": int(s.tool_errors),
                "interruptions": int(s.interruptions),
                "user_turns": int(s.user_turns),
                "edits": int(s.edits),
                "tests_run": bool(s.tests_run),
                "ended_in_error": bool(s.ended_in_error),
                "source": s.source or "claude-code",
                "first_prompt_len": int(s.first_prompt_len),
                "retry_touch": int(retry_touch),
                "friction": s.friction_score(retry_touch),
                "tools_total": (
                    int(tools_total) if tools_total is not None else None
                ),
            }
        )
    return digests


def top_friction_sessions(
    digests: Sequence[dict],
    *,
    limit: int = 3,
) -> list[dict]:
    """摩擦スコア上位セッション。同点はエラー多い順。score=0 は除外。"""
    scored = []
    for d in digests:
        if not isinstance(d, dict):
            continue
        err = int(d.get("tool_errors") or 0)
        inter = int(d.get("interruptions") or 0)
        retry = int(d.get("retry_touch") or 0)
        score = err + inter * 5 + retry * 5
        if score <= 0:
            continue
        scored.append((score, err, d))
    scored.sort(key=lambda x: (-x[0], -x[1], str(x[2].get("day", ""))))
    return [d for _score, _e, d in scored[:limit]]


def render_aiwork_markdown(
    sessions: list[AISession],
    tz: tzinfo,
    max_rows: int = 15,
    retry_chain_count: int = 0,
    pricing: dict[str, float] | None = None,
    *,
    session_titles: bool = True,
    redactor: Callable[[str], str] | None = None,
    retry_chains: Sequence[RetryChain] | None = None,
    internal_ai_sessions: int = 0,
    usd_jpy: float | None = None,
    loop_tax_summary: LoopTaxSummary | None = None,
    breaker_fires: int = 0,
    screen_tool_minutes: Mapping[str, float] | None = None,
    commit_stats: Sequence[Any] | None = None,
    commit_repos_omitted: int = 0,
    screen_total_minutes: float | None = None,
    measurement_gap: bool | None = None,
    structured_cli_sessions: int | None = None,
) -> str:
    """「AI作業の質」セクションのMarkdownを生成する。セッションが無ければ空文字。

    measurement_gap: 呼び出し側が short_record 閾値と CLI セッション数で決める。
    True のとき欠測疑い1行を出す（閾値リテラルはここに持たない）。

    細切れ（2往復以下）は中立の観測値。摩擦の主指標はリトライ連鎖。
    内容列 title は依頼文の抜粋。日誌本体は通常原文だが、依頼逐語は
    画面タイトルより機密性が高くボールト同期で漏れ得るため、ここだけ
    privacy redact を適用する（session_titles=false で列ごと非表示可）。

    成果列は決定論プロキシ（変更数・テスト・末尾エラー）のみ。
    出力の正しさの LLM 判定は日次では行わない（週次レビューへ集約）。

    breaker_fires: 空転ブレーカーの当日発火回数（live_episodes の通知履歴。
    トークン会計には使わない — 夜間ループ税が正。二重計上禁止）。
    """
    if not sessions:
        return ""

    total_turns = sum(s.user_turns for s in sessions)
    fragmented = sum(1 for s in sessions if s.is_fragmented)
    # output_tokens: ブラウザは 0 のまま（assistant_chars は混ぜない）
    output_tokens = sum(int(s.output_tokens or 0) for s in sessions)
    avg_turns = total_turns / len(sessions) if sessions else 0.0
    est_cost, uncosted, _ = estimate_sessions_cost(sessions, pricing)
    all_tools = Counter()
    for s in sessions:
        all_tools.update(s.tool_counts)
    top_tools = ", ".join(f"{name}×{n}" for name, n in all_tools.most_common(5))

    def _source_bucket(src: str | None) -> str:
        s = src.strip() if isinstance(src, str) else ""
        if not s:
            return "unknown"
        if s.endswith("-web") or s in ("chatgpt-web", "claude-web", "gemini-web"):
            return "web"
        return s

    by_source: Counter = Counter(_source_bucket(s.source) for s in sessions)
    source_bits = " / ".join(
        f"{name} {count}" for name, count in sorted(by_source.items())
    )

    lines: list[str] = []
    # §Z3: 欠測疑い行。判定は呼び出し側（F19 と同じ母数）。数値は書き換えない。
    if measurement_gap:
        from .report import _fmt_minutes

        n_cli = (
            int(structured_cli_sessions)
            if isinstance(structured_cli_sessions, int)
            else sum(
                1
                for s in sessions
                if not str(getattr(s, "source", "") or "").endswith("-web")
            )
        )
        screen_txt = (
            _fmt_minutes(float(screen_total_minutes))
            if isinstance(screen_total_minutes, (int, float))
            else "—"
        )
        lines.append(
            f"⚠ 計測欠測の疑い: 画面{screen_txt}"
            f"に対しAIセッション{n_cli}件。"
            "kaizenlog doctor で watcher を確認"
        )
        lines.append("")
    lines.append("### 🧠 AI作業の質")
    lines.append("")
    lines.append("計測範囲: セッションログのある AI CLI / ブラウザ拡張のみが対象です。")
    screen_sources = {
        "chatgpt": ("chatgpt-web",),
        "claude": ("claude-web",),
        "gemini": ("gemini-web",),
    }
    session_sources = {
        s.source.strip()
        for s in sessions
        if isinstance(s.source, str) and s.source.strip()
    }
    unlogged_screen_tools: list[tuple[str, float]] = []
    for tool, minutes in _normalize_screen_tool_minutes(screen_tool_minutes).items():
        # §B2: 0.5分未満は表示から除外。小数は1位まで
        if float(minutes) < 0.5:
            continue
        expected_sources = screen_sources.get(str(tool), ())
        if not any(source in session_sources for source in expected_sources):
            unlogged_screen_tools.append((str(tool), float(minutes)))
    if unlogged_screen_tools:
        # §E2: 画面分類名であることを明示（claude-code セッションと区別）
        # §B2: 生 float 禁止・小数1位（例 25.7分）
        rendered_tools = "・".join(
            f"{tool}（ブラウザ/デスクトップ） {minutes:.1f}分"
            for tool, minutes in unlogged_screen_tools
        )
        lines.append(
            f"画面計測のAI作業のうち {rendered_tools} はログが無く、"
            "往復・エラー・トークンは計測できません。"
        )
    lines.append(
        f"セッション: {len(sessions)}回（{source_bits}） / ユーザー発話: {total_turns}回"
        f"（平均 {avg_turns:.1f}回/セッション、2往復以下: {fragmented}回）"
    )
    # ツール系は measurable セッションのみ合算（ブラウザは欠損）
    tool_sessions = [s for s in sessions if s.tools_measurable]
    tool_errors_m = sum(s.tool_errors for s in tool_sessions)
    interruptions_m = sum(s.interruptions for s in tool_sessions)
    def _known_tool_source(source: object) -> str | None:
        if not isinstance(source, str):
            return None
        normalized = source.strip()
        return normalized if normalized in _MEASURABLE_TOOL_SOURCES else None

    tool_sources = [_known_tool_source(s.source) for s in tool_sessions]
    measurable_sources = list(dict.fromkeys(source for source in tool_sources if source is not None))

    def _source_detail(values: Mapping[str, int]) -> str:
        return " / ".join(f"{source} {values[source]}" for source in measurable_sources)

    tool_errors_by_source = {
        source: sum(s.tool_errors for s in tool_sessions if _known_tool_source(s.source) == source)
        for source in measurable_sources
    }
    interruptions_by_source = {
        source: sum(s.interruptions for s in tool_sessions if _known_tool_source(s.source) == source)
        for source in measurable_sources
    }
    split_tool_metrics = len(measurable_sources) > 1 and all(source is not None for source in tool_sources)
    errors_part = f"ツールエラー: {tool_errors_m}回"
    interruptions_part = f"ユーザー中断・拒否: {interruptions_m}回"
    if split_tool_metrics:
        error_detail = _source_detail(tool_errors_by_source)
        if any("codex" in source.lower() for source in measurable_sources):
            error_detail += "。codexは文字列判定・過大計上の可能性"
        errors_part += f"（{error_detail}）"
        interruptions_part += f"（{_source_detail(interruptions_by_source)}）"
    elif any(source == "codex" for source in measurable_sources):
        errors_part += "（codexは文字列判定・過大計上の可能性）"

    retry_part = f"リトライ連鎖: {retry_chain_count}回"
    if split_tool_metrics and retry_chain_count > 0 and retry_chains:
        retry_sources: list[str] = []
        for chain in retry_chains:
            if not chain.prompts:
                retry_sources = []
                break
            source = _known_tool_source(chain.prompts[0].source)
            if source is None:
                retry_sources = []
                break
            retry_sources.append(source)
        if len(retry_sources) == retry_chain_count and all(source in measurable_sources for source in retry_sources):
            retry_counts = {source: retry_sources.count(source) for source in measurable_sources}
            retry_part += f"（{_source_detail(retry_counts)}）"
    lines.append(
        f"{errors_part} / {interruptions_part} / {retry_part}"
        f" / 出力トークン: {output_tokens:,}"
    )
    # 対象外トークンが計上分を上回る日は $ 額を出さない。
    # 総量の大半が単価不明だと「$0.04」がほぼ無意味で誤解を招くため。
    costed_tokens = max(0, int(output_tokens) - int(uncosted))
    if int(uncosted) > costed_tokens:
        lines.append(
            f"推定コスト(下限): 換算なし — 出力{output_tokens:,} tok のうち"
            f"単価未登録が{int(uncosted):,} tok。"
        )
        unknown_models = sorted(
            {
                str(model)
                for session in sessions
                for model in (session.models or set())
                if model and resolve_output_price(str(model), pricing) is None
            }
        )
        if unknown_models:
            lines.append(f"未登録モデル: {', '.join(unknown_models)}。")
        lines.append(
            "kaizenlog.toml の [aiwork.pricing] に $/1Mtok を設定すると金額換算されます。"
        )
    else:
        lines.append(
            f"推定コスト(下限): ${est_cost:.2f}（output tokens ベース概算、"
            f"対象外 {uncosted:,} tok。input/cache 未計上）"
        )
    if int(internal_ai_sessions) > 0:
        lines.append(
            f"内部呼び出し（KaizenLog自身のLLM実行）: "
            f"{int(internal_ai_sessions)}回を計測から除外"
        )
    if top_tools:
        lines.append(f"主なツール: {top_tools}")
    obs = prompt_length_observation(sessions)
    if obs:
        lines.append(obs)

    # ループ税（エピソード会計）
    tax = loop_tax_summary
    if tax is None and retry_chains:
        tax = compute_loop_tax(retry_chains, sessions, pricing=pricing)
    worst_excerpt: str | None = None
    # 抑止条件は format_loop_tax_line の出力条件と一致させる。
    # total_wasted_tokens が None の日は最悪例行が出ないため、ここで抑止すると
    # 最悪チェーンの抜粋が日誌から完全に消える。
    if (
        output_tokens > 0
        and tax is not None
        and tax.episode_count > 0
        and tax.total_wasted_tokens is not None
    ):
        max_ep = max_loop_episode(tax)
        if max_ep is not None:
            excerpts = retry_chain_excerpts(
                [max_ep.chain], redactor=redactor, max_chains=1
            )
            worst_excerpt = excerpts[0] if excerpts else None
    if retry_chains:
        for excerpt in retry_chain_excerpts(retry_chains, redactor=redactor):
            if worst_excerpt is not None and excerpt == worst_excerpt:
                continue
            lines.append(f"リトライ{excerpt}")
    if tax is not None and tax.episode_count > 0:
        tax_line = format_loop_tax_line(
            tax,
            usd_jpy=usd_jpy,
            day_output_tokens=output_tokens,
            redactor=redactor,
        )
        if tax_line:
            lines.append(tax_line)
    # 空転ブレーカー発火（通知履歴のみ。会計はループ税側）
    if int(breaker_fires or 0) > 0:
        lines.append(f"⚡ ブレーカー発動: {int(breaker_fires)}回")
    digests = session_digests_for_stats(
        sessions,
        sessions[0].start.astimezone(tz).date().isoformat(),
        redactor=redactor,
        retry_chains=retry_chains,
    )
    worst = top_friction_sessions(digests, limit=1)
    if worst:
        digest = worst[0]
        title = _md_cell(digest.get("title") or "—") if session_titles else "—"
        project_raw = str(digest.get("project") or "—")
        source_raw = str(digest.get("source") or "—")
        if redactor is not None:
            project_raw = redactor(project_raw)
            source_raw = redactor(source_raw)
        project = _md_cell(project_raw)
        source = _md_cell(source_raw)
        errors = int(digest.get("tool_errors") or 0)
        interruptions = int(digest.get("interruptions") or 0)
        retry_touch = int(digest.get("retry_touch") or 0)
        friction = int(digest.get("friction") or 0)
        # §E5: ツール実行総数が取れるとき率を併記（スコア自体は変えない）
        tools_total = digest.get("tools_total")
        if tools_total is None:
            # session_digests に無い場合は tool_counts 合計キーを探す
            tools_total = digest.get("tool_calls")
        err_part = f"ツールエラー{errors}"
        if isinstance(tools_total, (int, float)) and float(tools_total) > 0:
            rate = errors / float(tools_total) * 100
            err_part = (
                f"ツールエラー{errors}/ツール実行{int(tools_total)}回={rate:.1f}%"
            )
        lines.extend(
            [
                f"⚠ 本日の摩擦ワースト: {project} ({source})「{title}」",
                f"   — 摩擦{friction}（{err_part} ＋ 中断{interruptions}×5 ＋ リトライ連鎖関与{retry_touch}×5）",
                "   ※ 摩擦はスコア順位であり、AIの良し悪しの判定ではありません。",
            ]
        )
    lines.append("")

    def _is_tiny_session(s: AISession) -> bool:
        """往復0 かつ ツール<=1 かつ 変更0 → 表から省略（集計からは除外しない）。"""
        tools_n = sum(s.tool_counts.values()) if s.tools_measurable else 0
        return (
            int(s.user_turns or 0) == 0
            and tools_n <= 1
            and int(s.edits or 0) == 0
        )

    table_sessions = [s for s in sessions if not _is_tiny_session(s)]
    tiny_n = len(sessions) - len(table_sessions)
    rows = table_sessions[:max_rows]
    # §E7: 壁時計の開始〜最終であり作業時間ではない
    lines.append(
        "※「開始-最終」はセッションの最初と最後の記録時刻であり、作業時間ではありません。"
    )
    if session_titles:
        lines.append(
            "| 開始-最終 | プロジェクト | 内容 | 往復 | ツール | エラー | 中断 | 変更 |"
        )
        lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | --- |")
    else:
        lines.append(
            "| 開始-最終 | プロジェクト | 往復 | ツール | エラー | 中断 | 変更 |"
        )
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- |")
    for s in rows:
        start = s.start.astimezone(tz).strftime("%H:%M")
        end = s.end.astimezone(tz).strftime("%H:%M")
        project = _md_cell(s.project)
        src = s.source or "claude-code"
        if src != "claude-code":
            # web: "chatgpt (web)"、CLI: "proj (codex)"
            if src.endswith("-web"):
                label = src.replace("-web", "")
                project = f"{label} (web)" if project in ("", "—", label) else f"{project} (web)"
            else:
                project = f"{project} ({src})"
        if s.tools_measurable:
            # 変更プロキシ + 末尾エラー⚠ / テスト✓（正しさは見ない）
            outcome_bits: list[str] = [f"変更{int(s.edits)}"]
            if s.tests_run:
                outcome_bits.append("✓")
            if s.ended_in_error:
                outcome_bits.append("⚠")
            outcome = " ".join(outcome_bits)
            tools_n: object = sum(s.tool_counts.values())
            err_s: object = s.tool_errors
            inter_s: object = s.interruptions
        else:
            # ブラウザ等: 無い指標は 0 ではなく `-`（既存の欠損原則）
            tools_n = err_s = inter_s = outcome = "-"
        if session_titles:
            title = s.title or "—"
            if redactor is not None and title not in ("—", "（本文未保存）"):
                title = redactor(title)
            title = _md_cell(title)
            lines.append(
                f"| {start}-{end} | {project} | {title} | {s.user_turns} "
                f"| {tools_n} | {err_s} | {inter_s} | {outcome} |"
            )
        else:
            lines.append(
                f"| {start}-{end} | {project} | {s.user_turns} "
                f"| {tools_n} | {err_s} | {inter_s} | {outcome} |"
            )
    omitted_rest = max(0, len(table_sessions) - max_rows)
    if omitted_rest or tiny_n:
        lines.append("")
        bits: list[str] = []
        if omitted_rest:
            bits.append(f"他 {omitted_rest} セッション省略")
        if tiny_n:
            bits.append(f"ほか短小セッション {tiny_n}件")
        lines.append(f"（{' / '.join(bits)}）")
    # §C3: コミット突合（空なら行ごと省略。「0件」とも書かない）
    if commit_stats:
        parts = [
            f"{s.repo_label} {s.commits}件 +{s.insertions:,}/-{s.deletions:,}行"
            for s in commit_stats
        ]
        omit_note = ""
        if commit_repos_omitted > 0:
            omit_note = f"（ほか {commit_repos_omitted} リポジトリは上限のため省略）"
        lines.append("")
        lines.append(
            "📦 当日のコミット（AIセッションが触れたリポジトリ・ローカル計測）:"
        )
        lines.append(f"   {', '.join(parts)}{omit_note}")
        lines.append(
            "   ※ コミットとAIセッションの因果は判定しません"
            "（同日・同リポジトリの並置のみ）。"
        )
        lines.append(
            "     git が無い／リポジトリでないパスはスキップします。"
        )
    lines.append("")
    return "\n".join(lines)
