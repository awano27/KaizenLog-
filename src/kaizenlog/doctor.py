"""kaizenlog doctor: セットアップと環境の健全性を一発診断する。

導入時のつまずきと障害時の切り分けを1コマンドで行う。
出力は ✅（正常）/ ⚠️（動くが注意）/ ❌（要修正）の3段階。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import requests

from .config import Config, find_config_file
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


def _check_llm(c: Check, cfg: Config) -> None:
    llm = cfg.llm
    if llm.backend == "none":
        c.warn("LLMバックエンド: none（改善提案・日報のLLMモードは無効）")
        return
    if llm.backend == "copilot-cli":
        path = shutil.which(llm.copilot_command)
        if path:
            c.ok(f"Copilot CLI 検出: {path}")
        else:
            c.error(f"Copilot CLI ('{llm.copilot_command}') が見つかりません。"
                    "`npm install -g @github/copilot` 後、新しいシェルで再確認してください")
        return
    if llm.backend == "openai-compatible":
        headers = {}
        api_key = os.environ.get(llm.api_key_env, "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            c.ok(f"APIキー検出（環境変数 {llm.api_key_env}）")
        else:
            c.warn(f"環境変数 {llm.api_key_env} が未設定（Ollamaなら不要、クラウドAPIなら必須）")
        try:
            r = requests.get(f"{llm.base_url}/models", headers=headers, timeout=15)
            if r.status_code < 500:
                c.ok(f"LLM API 応答あり: {llm.base_url}（model: {llm.model}）")
            else:
                c.error(f"LLM API がエラー応答: HTTP {r.status_code}")
        except requests.RequestException as e:
            c.error(f"LLM API ({llm.base_url}) に接続できません: {e.__class__.__name__}。"
                    "Ollamaの場合は起動を確認してください")
        return
    c.error(f"不明なLLMバックエンド: {llm.backend}")


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
