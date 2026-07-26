"""設定ファイル（TOML）の読み込みとデフォルト値。"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .vault import atomic_write_text

# デフォルトのカテゴリ分類ルール。上から順に評価され、最初にマッチしたものが採用される。
# patterns は「アプリ名 | ウィンドウタイトル」を結合した文字列への正規表現（大文字小文字無視）。
DEFAULT_RULES: list[dict] = [
    {
        "name": "AI作業",
        "ai": True,
        "patterns": [
            r"claude", r"chatgpt", r"openai", r"copilot", r"cursor",
            r"gemini", r"perplexity", r"ollama", r"lm studio", r"notebooklm",
        ],
    },
    {
        "name": "開発",
        "patterns": [
            r"visual studio code", r"vscode", r"code\.exe", r"pycharm", r"intellij",
            r"windows ?terminal", r"powershell", r"cmd\.exe", r"wsl", r"github",
            r"gitlab", r"stack overflow", r"docker",
        ],
    },
    {
        "name": "執筆・ノート",
        "patterns": [r"obsidian", r"notion", r"onenote", r"\.md", r"typora", r"scrapbox"],
    },
    {
        "name": "コミュニケーション",
        "patterns": [
            r"slack", r"discord", r"teams", r"outlook", r"gmail", r"thunderbird",
            r"zoom", r"google meet", r"chatwork", r"line",
        ],
    },
    {
        "name": "ドキュメント・オフィス",
        "patterns": [r"excel", r"word", r"powerpoint", r"google (docs|sheets|slides)", r"pdf"],
    },
    # エンタメは「ブラウジング」より先に評価する（内容がコンテナに勝つ）。
    # 後ろに置くと "chrome | YouTube" がブラウザ名で先にマッチしてしまい、
    # エンタメがブラウザ内で一切検出されなくなる。
    {
        "name": "エンタメ",
        "patterns": [r"youtube", r"netflix", r"spotify", r"twitter", r"\bx\b", r"reddit", r"steam",
                     r"nicovideo", r"tiktok", r"twitch"],
    },
    {
        "name": "ブラウジング",
        "patterns": [r"chrome", r"edge", r"firefox", r"brave", r"vivaldi"],
    },
]


class ConfigError(ValueError):
    """設定ファイルの値が不正。どのキーが悪いかを含めて報告する。"""


def _coerce(cast, value, key):
    """数値系設定の変換。失敗時は生のValueErrorではなくキー名つきのConfigErrorに。

    生のまま漏らすとCLIの捕捉網を素通りし、夜間実行が実行ログ・通知なしで死ぬ。
    """
    try:
        return cast(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(
            f"設定値 {key} が不正です: {value!r}（数値を指定してください）") from e


def _as_str_list(value, key) -> list[str]:
    """リスト系設定の取得。文字列単体はよくある書き間違いなので1要素リストとして扱う。

    list("文字列") は1文字ずつに分解され、redact_patterns なら1文字ごとの
    マスクでプロンプトが原型を留めなくなる（静かな大規模破壊）。
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ConfigError(f"設定値 {key} が不正です: {value!r}（文字列の配列を指定してください）")


def _as_choice(value, key: str, allowed: set[str]) -> str:
    if isinstance(value, str) and value in allowed:
        return value
    choices = ", ".join(sorted(allowed))
    raise ConfigError(
        f"設定値 {key} が不正です: {value!r}（{choices} のいずれかを指定してください）"
    )


@dataclass
class LLMConfig:
    backend: str = "copilot-cli"  # "claude-code-cli" | "copilot-cli" | "openai-compatible" | "none"
    # 指定バックエンドが利用できないとき openai-compatible（Ollama等）へ自動切替
    fallback_to_local: bool = True
    # システムプロンプト: 同梱テンプレート名（daily_advisor / privacy_safe 等）またはファイルパス
    system_prompt: str = "daily_advisor"
    # claude-code-cli
    claude_command: str = "claude"
    claude_extra_args: list[str] = field(default_factory=list)
    # copilot-cli
    copilot_command: str = "copilot"
    copilot_extra_args: list[str] = field(default_factory=list)
    # openai-compatible (GitHub Models / Ollama / その他)
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen3:8b"
    api_key_env: str = "KAIZENLOG_API_KEY"
    reasoning_effort: str = "none"
    timeout_seconds: int = 600
    lookback_days: int = 7
    retries: int = 2  # 一時エラー時の再試行回数（合計 retries+1 回試行）
    retry_wait_seconds: int = 20


@dataclass
class AIWorkConfig:
    enabled: bool = True
    claude_projects_dir: str = "~/.claude/projects"
    codex_sessions_dir: str = "~/.codex/sessions"


@dataclass
class PrivacyConfig:
    redact_patterns: list[str] = field(default_factory=list)
    replacement: str = "[REDACTED]"


@dataclass
class Config:
    timezone: str = "Asia/Tokyo"
    vault_dir: Path = Path(".")
    daily_notes_dir: str = "01 Daily Notes"
    experiments_dir: str = "03 Areas/Kaizen Experiments"
    stats_dir: str = ".kaizenlog/stats"
    logs_dir: str = ".kaizenlog/logs"
    memory_dir: str = "Kaizen/Memory"
    auto_backfill_days: int = 3  # 直近N日の欠損を毎晩自動補完（0で無効）
    log_retention_days: int = 90
    notify_on_failure: bool = True  # 失敗時にWindows通知を出す
    aw_base_url: str = "http://localhost:5600"
    aiwork: AIWorkConfig = field(default_factory=AIWorkConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    min_block_minutes: float = 3.0  # タイムラインに載せる最小ブロック長
    session_gap_minutes: float = 5.0  # この間隔以上空いたら別画面ブロック扱い
    rules: list[dict] = field(default_factory=lambda: list(DEFAULT_RULES))
    llm: LLMConfig = field(default_factory=LLMConfig)

    @property
    def daily_notes_path(self) -> Path:
        return Path(self.vault_dir).expanduser() / self.daily_notes_dir

    @property
    def experiments_path(self) -> Path:
        return Path(self.vault_dir).expanduser() / self.experiments_dir

    @property
    def stats_path(self) -> Path:
        return Path(self.vault_dir).expanduser() / self.stats_dir

    @property
    def logs_path(self) -> Path:
        return Path(self.vault_dir).expanduser() / self.logs_dir

    @property
    def memory_path(self) -> Path:
        return Path(self.vault_dir).expanduser() / self.memory_dir


def default_config_path() -> Path:
    """OS 標準のユーザー設定ディレクトリ上の config.toml パスを返す。

    Windows: %APPDATA%/kaizenlog/config.toml
    それ以外: $XDG_CONFIG_HOME/kaizenlog/config.toml または ~/.config/kaizenlog/config.toml
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "kaizenlog" / "config.toml"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "kaizenlog" / "config.toml"
    return Path.home() / ".config" / "kaizenlog" / "config.toml"


def existing_config_candidates() -> list[Path]:
    """存在する設定ファイル候補を優先順で返す（先頭が実際に使われる）。

    優先順: KAIZENLOG_CONFIG → AppData/XDG → CWD の kaizenlog.toml / config.toml。
    doctor はこの一覧で「設定の影武者」を警告する。
    """
    found: list[Path] = []
    env = os.environ.get("KAIZENLOG_CONFIG")
    if env and Path(env).expanduser().is_file():
        found.append(Path(env).expanduser())
    app = default_config_path()
    if app.is_file():
        found.append(app)
    seen = {p.resolve() for p in found}
    for cand in (Path("kaizenlog.toml"), Path("config.toml")):
        if cand.is_file():
            resolved = cand.resolve()
            if resolved not in seen:
                found.append(resolved)
                seen.add(resolved)
    return found


def find_config_file(explicit: str | None = None) -> Path | None:
    """設定ファイルを探索する。

    優先順: 明示パス → KAIZENLOG_CONFIG（欠落時は fail-closed）→
    AppData/XDG → CWD の kaizenlog.toml / config.toml。
    """
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {p}")
        return p
    env = os.environ.get("KAIZENLOG_CONFIG")
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"KAIZENLOG_CONFIG の設定ファイルが見つかりません: {p}")
        return p
    app = default_config_path()
    if app.is_file():
        return app
    for cand in (Path("kaizenlog.toml"), Path("config.toml")):
        if cand.is_file():
            return cand.resolve()
    return None


def _toml_path_str(path: Path | str) -> str:
    """TOML 文字列用にパスをスラッシュ区切りへ正規化する。"""
    return str(path).replace("\\", "/")


def render_config_template(
    vault_dir: Path | str,
    backend: str,
    model: str,
    **kwargs,
) -> str:
    """設定ファイル雛形の本文を生成する。"""
    vault = _toml_path_str(vault_dir)
    return f'''\
# KaizenLog 設定ファイル
# 置き場所: %APPDATA%/kaizenlog/config.toml（推奨）または
#           環境変数 KAIZENLOG_CONFIG でパスを指定してください。
# 初期セットアップ: kaizenlog setup

[general]
timezone = "Asia/Tokyo"
vault_dir = "{vault}"   # Obsidianボールトのルート
daily_notes_dir = "01 Daily Notes"
experiments_dir = "03 Areas/Kaizen Experiments"   # カイゼン実験ノートの置き場所
stats_dir = ".kaizenlog/stats"   # パターン検出用の日次統計JSON（ドットフォルダ=Obsidian非表示）
logs_dir = ".kaizenlog/logs"     # 実行ログ（kaizenlog status で確認）
memory_dir = "Kaizen/Memory"     # 提案の記録（Kaizen Memory、重複提案の防止に使用）
auto_backfill_days = 3      # 毎晩の実行時に直近N日の欠損を自動補完（0で無効）
log_retention_days = 90     # 実行ログの保持日数
min_block_minutes = 3.0     # タイムラインに載せる最小ブロック長（分）
session_gap_minutes = 5.0   # この分数以上空いたら別画面ブロック扱い

[notifications]
on_failure = true   # 夜間実行が失敗したときWindows通知を出す

[privacy]
# LLMへ送信する前にマスクする正規表現（ボールト内の日誌は原文のまま保持される）
# 例: redact_patterns = ["(株)〇〇商事", "案件[A-Z]-\\d+", "\\S+@\\S+\\.co\\.jp"]
redact_patterns = []
replacement = "[REDACTED]"

[activitywatch]
base_url = "http://localhost:5600"

[aiwork]
# Claude Codeのセッションログ（JSONL）から「AI作業の質」を集計する
# 往復数・細切れセッション・ツールエラー・中断を検出し、改善提案の材料にする
enabled = true
claude_projects_dir = "~/.claude/projects"
codex_sessions_dir = "~/.codex/sessions"  # Codex CLI ロールアウトログ（無ければ無効）

[llm]
# "claude-code-cli"   : Claude Code CLI（要: https://claude.com/claude-code & ログイン済み）
# "copilot-cli"       : GitHub Copilot CLI（要: npm install -g @github/copilot & ログイン済み）
# "openai-compatible" : GitHub Models / Ollama などOpenAI互換API
# "none"              : 改善提案をスキップ（ログ生成のみ）
backend = "{backend}"
# システムプロンプト: 同梱テンプレート名（daily_advisor / privacy_safe /
# weekly_review / ai_work_deep_review）または自作プロンプトのファイルパス
system_prompt = "daily_advisor"
lookback_days = 7   # 傾向分析のために渡す過去日数
retries = 2                # 一時エラー時の再試行回数
retry_wait_seconds = 20    # 再試行までの待ち秒数
fallback_to_local = true   # 指定バックエンド不可時に openai-compatible へ切替

[llm.claude_code_cli]
command = "claude"
extra_args = []   # 例: ["--model", "haiku"]

[llm.copilot_cli]
command = "copilot"
extra_args = []   # 例: ["--model", "claude-sonnet-4"]

[llm.openai_compatible]
# --- Ollama（完全ローカル・GPU不要、8Bモデルは16GB RAM推奨）---
# model は setup が検出した実在モデル、またはプレースホルダ
base_url = "http://localhost:11434/v1"
model = "{model}"
reasoning_effort = "none"   # none | low | medium | high
# --- GitHub Models（無料API）を使う場合は下記に差し替え ---
# base_url = "https://models.github.ai/inference"
# model = "openai/gpt-4o"
# api_key_env に指定した環境変数へ models:read 権限のPATを設定
api_key_env = "KAIZENLOG_API_KEY"
timeout_seconds = 600

# カテゴリ分類ルールの追加例（デフォルトルールより優先されます）
# [[categories.rules]]
# name = "AI作業"
# ai = true
# patterns = ["自社のAIツール名"]
'''


def _upsert_toml_assignment(text: str, key: str, value: str) -> str:
    """先頭の `key = ...` 行を置換する。無ければ末尾に追記する。"""
    pattern = re.compile(rf"(?m)^({re.escape(key)}\s*=\s*).*$")
    if pattern.search(text):
        return pattern.sub(rf'\1"{value}"', text, count=1)
    return text.rstrip() + f'\n{key} = "{value}"\n'


def write_config_file(
    path: Path | str,
    vault_dir: Path | str,
    backend: str,
    model: str,
    merge: bool = False,
) -> None:
    """設定ファイルを原子的に書き込む。

    merge=False またはファイル未作成時はフルテンプレートを書く。
    merge=True かつ既存ファイルがあるときは vault_dir / backend / model だけ
    キー単位で upsert し、categories.rules 等の追記を残す。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    vault_s = _toml_path_str(vault_dir)

    if merge and path.is_file():
        text = path.read_text(encoding="utf-8")
        text = _upsert_toml_assignment(text, "vault_dir", vault_s)
        text = _upsert_toml_assignment(text, "backend", backend)
        text = _upsert_toml_assignment(text, "model", model)
        atomic_write_text(path, text)
        return

    content = render_config_template(vault_dir=vault_dir, backend=backend, model=model)
    atomic_write_text(path, content)


def load_config(path: str | None = None) -> Config:
    cfg_path = find_config_file(path)
    cfg = Config()
    if cfg_path is None:
        return cfg

    with open(cfg_path, "rb") as f:
        data = tomllib.load(f)

    general = data.get("general", {})
    cfg.timezone = general.get("timezone", cfg.timezone)
    if "vault_dir" in general:
        cfg.vault_dir = Path(general["vault_dir"])
    cfg.daily_notes_dir = general.get("daily_notes_dir", cfg.daily_notes_dir)
    cfg.experiments_dir = general.get("experiments_dir", cfg.experiments_dir)
    cfg.stats_dir = general.get("stats_dir", cfg.stats_dir)
    cfg.logs_dir = general.get("logs_dir", cfg.logs_dir)
    cfg.auto_backfill_days = _coerce(int, general.get("auto_backfill_days", cfg.auto_backfill_days), "general.auto_backfill_days")
    cfg.log_retention_days = _coerce(int, general.get("log_retention_days", cfg.log_retention_days), "general.log_retention_days")

    cfg.memory_dir = general.get("memory_dir", cfg.memory_dir)

    notifications = data.get("notifications", {})
    cfg.notify_on_failure = bool(notifications.get("on_failure", cfg.notify_on_failure))

    privacy = data.get("privacy", {})
    cfg.privacy.redact_patterns = _as_str_list(privacy.get("redact_patterns", []), "privacy.redact_patterns")
    cfg.privacy.replacement = privacy.get("replacement", cfg.privacy.replacement)
    cfg.min_block_minutes = _coerce(float, general.get("min_block_minutes", cfg.min_block_minutes), "general.min_block_minutes")
    cfg.session_gap_minutes = _coerce(float, general.get("session_gap_minutes", cfg.session_gap_minutes), "general.session_gap_minutes")

    aw = data.get("activitywatch", {})
    cfg.aw_base_url = aw.get("base_url", cfg.aw_base_url).rstrip("/")

    aiwork = data.get("aiwork", {})
    cfg.aiwork.enabled = bool(aiwork.get("enabled", cfg.aiwork.enabled))
    cfg.aiwork.claude_projects_dir = aiwork.get(
        "claude_projects_dir", cfg.aiwork.claude_projects_dir
    )
    cfg.aiwork.codex_sessions_dir = aiwork.get(
        "codex_sessions_dir", cfg.aiwork.codex_sessions_dir
    )

    cats = data.get("categories", {})
    user_rules = cats.get("rules", [])
    if user_rules:
        if cats.get("replace_defaults", False):
            cfg.rules = list(user_rules)
        else:
            # ユーザー定義ルールを先頭に置き、デフォルトより優先させる
            cfg.rules = list(user_rules) + list(DEFAULT_RULES)

    llm = data.get("llm", {})
    cfg.llm.backend = llm.get("backend", cfg.llm.backend)
    cfg.llm.fallback_to_local = bool(llm.get("fallback_to_local", cfg.llm.fallback_to_local))
    cfg.llm.system_prompt = llm.get("system_prompt", cfg.llm.system_prompt)
    cfg.llm.lookback_days = _coerce(int, llm.get("lookback_days", cfg.llm.lookback_days), "llm.lookback_days")
    cc = llm.get("claude_code_cli", {})
    cfg.llm.claude_command = cc.get("command", cfg.llm.claude_command)
    cfg.llm.claude_extra_args = _as_str_list(cc.get("extra_args", cfg.llm.claude_extra_args), "llm.claude_code_cli.extra_args")
    cop = llm.get("copilot_cli", {})
    cfg.llm.copilot_command = cop.get("command", cfg.llm.copilot_command)
    cfg.llm.copilot_extra_args = _as_str_list(cop.get("extra_args", cfg.llm.copilot_extra_args), "llm.copilot_cli.extra_args")
    oai = llm.get("openai_compatible", {})
    cfg.llm.base_url = oai.get("base_url", cfg.llm.base_url).rstrip("/")
    cfg.llm.model = oai.get("model", cfg.llm.model)
    cfg.llm.api_key_env = oai.get("api_key_env", cfg.llm.api_key_env)
    cfg.llm.reasoning_effort = _as_choice(
        oai.get("reasoning_effort", cfg.llm.reasoning_effort),
        "llm.openai_compatible.reasoning_effort",
        {"none", "low", "medium", "high"},
    )
    cfg.llm.timeout_seconds = _coerce(int, oai.get("timeout_seconds", cfg.llm.timeout_seconds), "llm.openai_compatible.timeout_seconds")
    cfg.llm.retries = _coerce(int, llm.get("retries", cfg.llm.retries), "llm.retries")
    cfg.llm.retry_wait_seconds = _coerce(int, llm.get("retry_wait_seconds", cfg.llm.retry_wait_seconds), "llm.retry_wait_seconds")

    return cfg
