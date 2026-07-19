"""kaizenlog doctor: セットアップと環境の健全性を一発診断する。

導入時のつまずきと障害時の切り分けを1コマンドで行う。
出力は ✅（正常）/ ⚠️（動くが注意）/ ❌（要修正）の3段階。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import requests

from .config import Config, existing_config_candidates, find_config_file
from .runlog import load_runs


class Check:
    def __init__(self):
        self.lines: list[str] = []
        self.has_error = False

    def ok(self, msg: str) -> None:
        self.lines.append(f"✅ {msg}")

    def warn(self, msg: str) -> None:
        self.lines.append(f"⚠️  {msg}")

    def error(self, msg: str) -> None:
        self.lines.append(f"❌ {msg}")
        self.has_error = True


def _check_config(c: Check, config_path: str | None) -> None:
    try:
        found = find_config_file(config_path)
    except FileNotFoundError as e:
        c.error(str(e))
        return
    if found:
        c.ok(f"設定ファイル: {found}")
        # 複数の設定ファイルが存在すると、実行時のカレントディレクトリ次第で
        # 別の設定が選ばれる（例: 手動実行はリポジトリの kaizenlog.toml、
        # 夜間タスクは %APPDATA% の config.toml）。内容がズレていると
        # 「手動では正常なのに夜間だけ別ボールトに書く」事故になるため警告する。
        if not config_path:
            others = [p for p in existing_config_candidates() if p.resolve() != found.resolve()]
            if others:
                c.warn("他の場所にも設定ファイルがあります（現在は無視）: "
                       + ", ".join(str(p) for p in others)
                       + " — 作業ディレクトリが異なる実行（夜間タスク等）では"
                         "そちらが使われる可能性があります。内容を同期するか片方を削除してください")
    else:
        c.warn("設定ファイルが見つかりません（デフォルト設定で動作中）。"
               "`kaizenlog init-config` で作成してください")


def _check_vault(c: Check, cfg: Config) -> None:
    vault = Path(cfg.vault_dir).expanduser()
    if not vault.is_dir():
        c.error(f"ボールトが存在しません: {vault}（config.tomlのvault_dirを確認）")
        return
    if not os.access(vault, os.W_OK):
        c.error(f"ボールトに書き込めません: {vault}")
        return
    c.ok(f"ボールト書き込み可: {vault}")
    if cfg.daily_notes_path.is_dir():
        c.ok(f"デイリーノート: {cfg.daily_notes_path}")
    else:
        c.warn(f"デイリーノートのフォルダがありません（初回実行時に作成されます）: {cfg.daily_notes_path}")


def _check_activitywatch(c: Check, cfg: Config) -> None:
    try:
        r = requests.get(f"{cfg.aw_base_url}/api/0/buckets/", timeout=10)
        r.raise_for_status()
        buckets = r.json()
    except requests.RequestException as e:
        c.error(f"ActivityWatch ({cfg.aw_base_url}) に接続できません: {e.__class__.__name__}。"
                "起動しているか確認してください")
        return
    types = {info.get("type") for info in buckets.values()}
    c.ok(f"ActivityWatch 応答あり（バケット{len(buckets)}個）")
    if "currentwindow" in types:
        c.ok("ウィンドウwatcher検出（aw-watcher-window）")
    else:
        c.error("ウィンドウwatcherが見つかりません。aw-watcher-windowの動作を確認してください")
    if "afkstatus" in types:
        c.ok("AFK watcher検出（離席時間を除外できます）")
    else:
        c.warn("AFK watcherが見つかりません（離席中の時間も計上されます）")


def _list_api_models(base_url: str, headers: dict) -> list[str] | None:
    """OpenAI互換APIのモデル一覧を取得する。取得・解釈できなければ None。"""
    try:
        r = requests.get(f"{base_url}/models", headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
        return [i for i in ids if i] or None
    except (requests.RequestException, ValueError):
        return None


def _check_openai_compatible(c: Check, llm, *, as_fallback: bool, essential: bool = True) -> None:
    """openai-compatible（Ollama / GitHub Models等）の接続とモデル存在を確認する。

    essential=False は「主バックエンドが健在で、これは予備経路」の場合。
    そのときだけ問題を警告に留める（主が欠けた状態で予備も死んでいたらエラー）。
    """
    label = "フォールバック先ローカルLLM" if as_fallback else "LLM API"
    report_problem = c.error if essential else c.warn
    headers = {}
    api_key = os.environ.get(llm.api_key_env, "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        if not as_fallback:
            c.ok(f"APIキー検出（環境変数 {llm.api_key_env}）")
    elif not as_fallback:
        c.warn(f"環境変数 {llm.api_key_env} が未設定（Ollamaなら不要、クラウドAPIなら必須）")

    try:
        r = requests.get(f"{llm.base_url}/models", headers=headers, timeout=15)
        if r.status_code >= 500:
            report_problem(f"{label} がエラー応答: HTTP {r.status_code}")
            return
    except requests.RequestException as e:
        report_problem(f"{label} ({llm.base_url}) に接続できません: {e.__class__.__name__}。"
                       "Ollamaの場合は起動を確認してください")
        return

    # 設定されたモデルが実際に利用可能かまで確認する（未pullのモデル指定を実行前に検出）
    models = _list_api_models(llm.base_url, headers)
    if models is not None and llm.model not in models:
        candidates = ", ".join(sorted(models)[:5])
        report_problem(f"{label}: モデル '{llm.model}' がありません（利用可能: {candidates} 等）。"
                       f"`ollama pull {llm.model}` するか config の model を変更してください")
        return
    c.ok(f"{label} 応答あり: {llm.base_url}（model: {llm.model}）")


def _check_llm(c: Check, cfg: Config) -> None:
    llm = cfg.llm
    if llm.backend == "none":
        c.warn("LLMバックエンド: none（改善提案・日報のLLMモードは無効）")
        return

    cli_checks = {
        "copilot-cli": ("Copilot CLI", llm.copilot_command,
                        "`npm install -g @github/copilot` 後、新しいシェルで再確認してください"),
        "claude-code-cli": ("Claude Code CLI", llm.claude_command,
                            "https://claude.com/claude-code からインストールしてください"),
    }
    if llm.backend in cli_checks:
        name, command, hint = cli_checks[llm.backend]
        path = shutil.which(command)
        if path:
            c.ok(f"{name} 検出: {path}")
        else:
            # フォールバックが効く場合は起動不能ではないため警告に留める
            report = c.warn if llm.fallback_to_local else c.error
            report(f"{name} ('{command}') が見つかりません。{hint}")
    elif llm.backend == "openai-compatible":
        _check_openai_compatible(c, llm, as_fallback=False)
        return
    else:
        c.error(f"不明なLLMバックエンド: {llm.backend}")
        return

    if llm.fallback_to_local:
        # 主CLIが欠けているなら予備経路が生命線 → その障害はエラー扱い
        _check_openai_compatible(c, llm, as_fallback=True, essential=(path is None))


def _check_aiwork(c: Check, cfg: Config) -> None:
    if not cfg.aiwork.enabled:
        c.warn("AI Work Telemetry: 無効（[aiwork] enabled = false）")
        return
    projects = Path(cfg.aiwork.claude_projects_dir).expanduser()
    if not projects.is_dir():
        c.warn(f"Claude Codeログが見つかりません: {projects}"
               "（Claude Code未使用なら問題ありません）")
        return
    count = sum(1 for _ in projects.rglob("*.jsonl"))
    c.ok(f"Claude Codeログ: {projects}（セッションファイル{count}個）")


def _check_history(c: Check, cfg: Config) -> None:
    stats_files = sorted(cfg.stats_path.glob("*.json")) if cfg.stats_path.is_dir() else []
    if stats_files:
        c.ok(f"日次統計: {len(stats_files)}日分（最新: {stats_files[-1].stem}）")
    else:
        c.warn("日次統計がまだありません（`kaizenlog generate` で蓄積が始まります）")

    runs = load_runs(cfg.logs_path)
    if not runs:
        c.warn("実行履歴がまだありません")
        return
    last = runs[-1]
    if last.get("ok"):
        c.ok(f"直近の実行: 成功（{last.get('command')} @ {last.get('ts', '')[:16]}）")
    else:
        c.error(f"直近の実行が失敗しています: {last.get('command')} — "
                f"{last.get('error', '')[:120]}（詳細は `kaizenlog status`）")


def run_doctor(cfg: Config, config_path: str | None = None) -> tuple[str, bool]:
    """全チェックを実行し、(レポート文字列, エラー有無) を返す。"""
    c = Check()
    _check_config(c, config_path)
    _check_vault(c, cfg)
    _check_activitywatch(c, cfg)
    _check_llm(c, cfg)
    _check_aiwork(c, cfg)
    _check_history(c, cfg)
    verdict = "\n❌ 修正が必要な項目があります。" if c.has_error else "\n✅ すべて正常です。"
    return "\n".join(c.lines) + verdict, c.has_error
