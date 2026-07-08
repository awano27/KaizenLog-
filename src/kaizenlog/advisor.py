"""LLMバックエンドを呼び出して改善提案を生成する。

対応バックエンド:
- claude-code-cli:    Claude Code CLI をヘッドレスモード（-p）で呼び出す
- copilot-cli:        GitHub Copilot CLI をプログラマティックモード（-p）で呼び出す
- openai-compatible:  OpenAI互換API（GitHub Models / Ollama / その他）を叩く

いずれのバックエンドもテキスト生成のみを行い、ファイルへの書き込みは常に
KaizenLog側がマーカー区間に対して行う（LLMにファイルを直接触らせない）。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from importlib import resources
from pathlib import Path
from typing import Callable

import requests

from .config import LLMConfig

BUNDLED_PROMPTS = ("daily_advisor", "weekly_review", "ai_work_deep_review", "privacy_safe")


def load_bundled_prompt(name: str) -> str:
    return (resources.files("kaizenlog") / "prompts" / f"{name}.md").read_text(encoding="utf-8")


def resolve_system_prompt(cfg: LLMConfig) -> str:
    """system_prompt設定を解決する。同梱テンプレート名 or ファイルパス。"""
    name = cfg.system_prompt or "daily_advisor"
    if name in BUNDLED_PROMPTS:
        return load_bundled_prompt(name)
    path = Path(name).expanduser()
    if path.is_file():
        return path.read_text(encoding="utf-8")
    raise AdvisorError(
        f"system_prompt が見つかりません: {name!r}"
        f"（同梱テンプレート {', '.join(BUNDLED_PROMPTS)} か、実在するファイルパスを指定）"
    )


# 後方互換: 既定のシステムプロンプト
SYSTEM_PROMPT = load_bundled_prompt("daily_advisor")


def build_prompt(
    today_md: str,
    recent_summaries: list[str],
    intent: str | None = None,
    experiments: str | None = None,
    memory: str | None = None,
) -> str:
    parts: list[str] = []
    if intent:
        parts.append("# 本日の計画（ユーザーが手書きしたToday's Focus / Tasks）\n")
        parts.append(intent)
        parts.append("\n\n")
    if experiments:
        parts.append("# 実行中のカイゼン実験と実測値\n")
        parts.append(experiments)
        parts.append("\n\n")
    if memory:
        parts.append("# 過去の提案の記録（重複提案を避けるための参照）\n")
        parts.append(memory)
        parts.append("\n\n")
    parts.append("以下は本日の作業ログです。\n\n# 本日のログ\n")
    parts.append(today_md)
    if recent_summaries:
        parts.append("\n\n# 直近の日別サマリー（傾向の参考）\n")
        parts.extend(recent_summaries)
    return "".join(parts)


class AdvisorError(RuntimeError):
    pass


def _call_copilot_cli(cfg: LLMConfig, system_prompt: str, user_prompt: str) -> str:
    """GitHub Copilot CLI のプログラマティックモードでテキストを生成する。"""
    cmd = [cfg.copilot_command, "-p", f"{system_prompt}\n\n{user_prompt}", *cfg.copilot_extra_args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=cfg.timeout_seconds,
        )
    except FileNotFoundError as e:
        raise AdvisorError(
            f"Copilot CLI ('{cfg.copilot_command}') が見つかりません。"
            "`npm install -g @github/copilot` でインストールし、`copilot` で一度ログインしてください。"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise AdvisorError(f"Copilot CLI がタイムアウトしました（{cfg.timeout_seconds}秒）。") from e
    if result.returncode != 0:
        raise AdvisorError(f"Copilot CLI がエラーを返しました:\n{result.stderr.strip()}")
    text = result.stdout.strip()
    if not text:
        raise AdvisorError("Copilot CLI の出力が空でした。")
    return text


def _call_claude_code_cli(cfg: LLMConfig, system_prompt: str, user_prompt: str) -> str:
    """Claude Code CLI をヘッドレスモード（-p）で呼び出してテキストを生成する。

    `--output-format json` の応答（最終テキストは result フィールド）を第一候補とし、
    JSONとして解釈できない場合はプレーンテキスト出力として扱う（方式の自動検出）。
    ツールは一切許可しないため、Claude Codeがファイルを変更することはない。
    """
    cmd = [
        cfg.claude_command,
        "-p", f"{system_prompt}\n\n{user_prompt}",
        "--output-format", "json",
        *cfg.claude_extra_args,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=cfg.timeout_seconds,
        )
    except FileNotFoundError as e:
        raise AdvisorError(
            f"Claude Code CLI ('{cfg.claude_command}') が見つかりません。"
            "https://claude.com/claude-code からインストールし、`claude` で一度ログインしてください。"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise AdvisorError(f"Claude Code CLI がタイムアウトしました（{cfg.timeout_seconds}秒）。") from e
    if result.returncode != 0:
        stderr = result.stderr.strip()[:500]
        hint = ""
        if "log" in stderr.lower() or "auth" in stderr.lower():
            hint = "\n認証切れの可能性があります。`claude` を対話起動して /login してください。"
        raise AdvisorError(f"Claude Code CLI がエラーを返しました:\n{stderr}{hint}")
    stdout = result.stdout.strip()
    if not stdout:
        raise AdvisorError("Claude Code CLI の出力が空でした。")
    try:
        data = json.loads(stdout)
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            text = data["result"].strip()
            if not text:
                raise AdvisorError("Claude Code CLI のresultが空でした。")
            return text
    except json.JSONDecodeError:
        pass  # 古いCLI等でJSON非対応の場合はプレーンテキストとして扱う
    return stdout


def _call_openai_compatible(cfg: LLMConfig, system_prompt: str, user_prompt: str) -> str:
    """OpenAI互換API（GitHub Models / Ollama など）でテキストを生成する。"""
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get(cfg.api_key_env, "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    try:
        r = requests.post(
            f"{cfg.base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=cfg.timeout_seconds,
        )
        r.raise_for_status()
    except requests.ConnectionError as e:
        raise AdvisorError(
            f"LLM API に接続できません ({cfg.base_url})。"
            "Ollamaの場合は `ollama serve` が動作しているか確認してください。"
        ) from e
    except requests.HTTPError as e:
        raise AdvisorError(f"LLM API がエラーを返しました: {e}\n{r.text[:500]}") from e
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise AdvisorError(f"LLM API の応答形式が想定外です: {data}") from e


def generate_text(
    cfg: LLMConfig, system_prompt: str, user_prompt: str, sleep=time.sleep
) -> str:
    """設定されたバックエンドでテキストを生成する（改善提案・日報などの共通経路）。

    一時エラー（レート制限・タイムアウト・接続断など）に備えて cfg.retries 回まで
    間隔を空けて再試行する。無人の夜間実行の成功率を上げるための処置。
    """
    if cfg.backend == "claude-code-cli":
        call = _call_claude_code_cli
    elif cfg.backend == "copilot-cli":
        call = _call_copilot_cli
    elif cfg.backend == "openai-compatible":
        call = _call_openai_compatible
    elif cfg.backend == "none":
        raise AdvisorError("llm.backend = 'none' のためLLM生成はスキップされました。")
    else:
        raise AdvisorError(f"不明なLLMバックエンドです: {cfg.backend}")

    last_error: AdvisorError | None = None
    for attempt in range(cfg.retries + 1):
        try:
            return call(cfg, system_prompt, user_prompt)
        except AdvisorError as e:
            last_error = e
            if attempt < cfg.retries:
                print(f"⚠️  LLM呼び出しに失敗（{attempt + 1}回目）。"
                      f"{cfg.retry_wait_seconds}秒後に再試行します: {e}")
                sleep(cfg.retry_wait_seconds)
    raise last_error


def generate_advice(
    cfg: LLMConfig,
    today_md: str,
    recent_summaries: list[str],
    intent: str | None = None,
    experiments: str | None = None,
    memory: str | None = None,
    redactor: Callable[[str], str] | None = None,
) -> str:
    prompt = build_prompt(today_md, recent_summaries, intent, experiments, memory)
    if redactor:
        prompt = redactor(prompt)  # 送信プロンプトのみマスク。日誌本体は原文のまま
    advice = generate_text(cfg, resolve_system_prompt(cfg), prompt)
    return f"## 🚀 Kaizen（AIからの改善提案）\n\n{advice}"
