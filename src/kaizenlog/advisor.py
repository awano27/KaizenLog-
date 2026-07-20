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
import shutil
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


class BackendUnavailable(AdvisorError):
    """バックエンドがそもそも利用できない（未インストール・未起動）。

    一時エラーと違いリトライしても直らないため、generate_text は即座に
    次のバックエンド（ローカルLLM）へフォールバックする。
    """


def _resolve_command(command: str, hint: str) -> str:
    """CLIコマンドを実行可能なフルパスに解決する。

    Windowsではnpm製CLIが `copilot.CMD` のようなバッチファイルとして配置されるが、
    subprocess.run（CreateProcess）はPATHEXTによる拡張子解決を行わないため
    コマンド名のままでは FileNotFoundError になる。doctor と同じ shutil.which で
    事前にフルパス化し、検出結果と実行結果のズレをなくす。
    """
    path = shutil.which(command)
    if not path:
        raise BackendUnavailable(hint)
    return path


# Windows CreateProcess のコマンドライン上限（32,767文字）。超えると起動自体が失敗する
_WINDOWS_CMDLINE_LIMIT = 30000


def _prompt_arg_for_batch(exe: str, prompt: str) -> str:
    """バッチファイル（.cmd/.bat）経由のCLIに渡すプロンプトを無害化する。

    npm製CLIは copilot.CMD のようなバッチとして配置され、Windowsは内部的に
    cmd.exe で実行する。cmd.exe は引数内の `"` や `%` を再解釈するため、
    Webページタイトル等に含まれる特殊文字がコマンド注入・引数破壊になりうる
    （CVE-2024-24576 と同類。Pythonのsubprocessは.batに対して安全な引用を保証
    しない）。意味を保ったまま全角に置換して無害化する。
    """
    if not exe.lower().endswith((".cmd", ".bat")):
        return prompt
    return prompt.replace('"', "”").replace("%", "％").replace("^", "＾")


def _check_cmdline_length(cmd: list[str], backend_name: str) -> None:
    total = sum(len(c) + 3 for c in cmd)
    if total > _WINDOWS_CMDLINE_LIMIT:
        # 引数で渡せないほど長い日誌（lookback含む）はこのバックエンドでは処理不能。
        # リトライで直らないため即フォールバック（Ollama経由ならHTTPなので制限なし）
        raise BackendUnavailable(
            f"{backend_name}: プロンプトがWindowsのコマンドライン上限を超えています"
            f"（約{total:,}文字）。ローカルLLMへのフォールバックを試みます。")


def _call_copilot_cli(cfg: LLMConfig, system_prompt: str, user_prompt: str) -> str:
    """GitHub Copilot CLI のプログラマティックモードでテキストを生成する。"""
    missing_hint = (
        f"Copilot CLI ('{cfg.copilot_command}') が見つかりません。"
        "`npm install -g @github/copilot` でインストールし、`copilot` で一度ログインしてください。"
    )
    exe = _resolve_command(cfg.copilot_command, missing_hint)
    prompt = _prompt_arg_for_batch(exe, f"{system_prompt}\n\n{user_prompt}")
    cmd = [exe, "-p", prompt, *cfg.copilot_extra_args]
    _check_cmdline_length(cmd, "Copilot CLI")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=cfg.timeout_seconds,
        )
    except FileNotFoundError as e:
        raise BackendUnavailable(missing_hint) from e
    except subprocess.TimeoutExpired as e:
        raise AdvisorError(f"Copilot CLI がタイムアウトしました（{cfg.timeout_seconds}秒）。") from e
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "authentication" in stderr.lower() or "login" in stderr.lower():
            # 未認証はリトライで直らない → 即フォールバック対象
            raise BackendUnavailable(
                "Copilot CLI が未認証です。`copilot` を対話起動して /login してください。")
        raise AdvisorError(f"Copilot CLI がエラーを返しました:\n{stderr}")
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
    missing_hint = (
        f"Claude Code CLI ('{cfg.claude_command}') が見つかりません。"
        "https://claude.com/claude-code からインストールし、`claude` で一度ログインしてください。"
    )
    exe = _resolve_command(cfg.claude_command, missing_hint)
    # プロンプトは引数ではなくstdinで渡す。Windowsの32,767文字のコマンドライン上限と、
    # .CMDシム経由時のcmd.exeによる特殊文字の再解釈（コマンド注入）を両方回避できる
    cmd = [
        exe,
        "-p",
        "--output-format", "json",
        *cfg.claude_extra_args,
    ]
    try:
        result = subprocess.run(
            cmd,
            input=f"{system_prompt}\n\n{user_prompt}",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=cfg.timeout_seconds,
        )
    except FileNotFoundError as e:
        raise BackendUnavailable(missing_hint) from e
    except subprocess.TimeoutExpired as e:
        raise AdvisorError(f"Claude Code CLI がタイムアウトしました（{cfg.timeout_seconds}秒）。") from e
    if result.returncode != 0:
        stderr = result.stderr.strip()[:500]
        stdout = result.stdout.strip()[:500]
        # 認証エラーの本文はstderrではなく stdout のJSON（result/api_error_status）に
        # 入ることがある（実CLI: exit 1・stderr空・stdoutに "401 OAuth ... expired"）。
        # 両方を見て判定しないと、リトライで直らない未認証を一時エラー扱いして
        # 20秒×リトライを空回りさせた挙句、意味不明な空メッセージを出す
        detail = stderr or stdout
        combined = f"{stderr}\n{stdout}".lower()
        if any(k in combined for k in ("authenticate", "unauthor", "oauth",
                                       "/login", "log in", "401", "api key")):
            # 未認証はリトライで直らない → 即フォールバック対象
            raise BackendUnavailable(
                f"Claude Code CLI が未認証の可能性があります。"
                f"`claude` を対話起動して /login してください:\n{detail}")
        raise AdvisorError(f"Claude Code CLI がエラーを返しました:\n{detail}")
    stdout = result.stdout.strip()
    if not stdout:
        raise AdvisorError("Claude Code CLI の出力が空でした。")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout  # 古いCLI等でJSON非対応の場合はプレーンテキストとして扱う
    if isinstance(data, dict):
        # JSONで応答した以上はJSONプロトコルとして厳密に扱う。exit 0 でも
        # {"is_error": true, "result": null} のようなエラー封筒がありうるため、
        # そのままreturnすると生JSONが「改善提案」としてノートに書き込まれる
        result_text = data.get("result")
        if not data.get("is_error") and isinstance(result_text, str) and result_text.strip():
            return result_text.strip()
        # 認証切れは exit 0・is_error:true・api_error_status:401 で返ることがある。
        # リトライで直らないので即フォールバック対象（BackendUnavailable）
        err_body = f"{data.get('api_error_status', '')} {result_text or ''}".lower()
        if data.get("api_error_status") in (401, 403) or "authenticate" in err_body:
            raise BackendUnavailable(
                f"Claude Code CLI が未認証です。`claude` を対話起動して /login してください:\n"
                f"{str(result_text)[:300]}")
        subtype = data.get("subtype", "unknown")
        raise AdvisorError(
            f"Claude Code CLI がエラー応答を返しました（subtype: {subtype}）:\n{stdout[:500]}")
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
        data = r.json()
    except requests.ConnectionError as e:
        raise BackendUnavailable(
            f"LLM API に接続できません ({cfg.base_url})。"
            "Ollamaの場合は `ollama serve` が動作しているか確認してください。"
        ) from e
    except requests.Timeout as e:
        # Ollamaの初回モデルロード等で起きる。リトライで直る可能性があるのでAdvisorError
        raise AdvisorError(
            f"LLM API がタイムアウトしました ({cfg.base_url}, {cfg.timeout_seconds}秒)。"
        ) from e
    except requests.HTTPError as e:
        raise AdvisorError(f"LLM API がエラーを返しました: {e}\n{r.text[:500]}") from e
    except (requests.RequestException, ValueError) as e:
        # その他の通信エラー・非JSON応答（プロキシのHTMLエラーページ等）
        raise AdvisorError(f"LLM API の応答を処理できません: {e.__class__.__name__}: {e}") from e
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise AdvisorError(f"LLM API の応答形式が想定外です: {str(data)[:500]}") from e
    if not isinstance(content, str) or not content.strip():
        # content: null はコンテンツフィルタ停止やツール呼び出し応答などで発生する
        raise AdvisorError(f"LLM API がテキストを返しませんでした: {str(data)[:500]}")
    return content.strip()


def generate_text(
    cfg: LLMConfig, system_prompt: str, user_prompt: str, sleep=time.sleep
) -> str:
    """設定されたバックエンドでテキストを生成する（改善提案・日報などの共通経路）。

    一時エラー（レート制限・タイムアウトなど）は cfg.retries 回まで間隔を空けて
    再試行する。未インストール・未起動（BackendUnavailable）はリトライしても
    直らないため即座に打ち切り、fallback_to_local が有効ならローカルLLM
    （openai-compatible = Ollama等）に切り替えて続行する。
    """
    # 呼び出し時に解決する（モジュール属性の差し替え＝テストのモックを効かせるため）
    backend_calls: dict[str, Callable[[LLMConfig, str, str], str]] = {
        "claude-code-cli": _call_claude_code_cli,
        "copilot-cli": _call_copilot_cli,
        "openai-compatible": _call_openai_compatible,
    }
    if cfg.backend == "none":
        raise AdvisorError("llm.backend = 'none' のためLLM生成はスキップされました。")
    if cfg.backend not in backend_calls:
        raise AdvisorError(f"不明なLLMバックエンドです: {cfg.backend}")

    chain = [cfg.backend]
    if cfg.fallback_to_local and cfg.backend != "openai-compatible":
        chain.append("openai-compatible")

    failures: list[str] = []
    last_error: AdvisorError | None = None
    for i, backend in enumerate(chain):
        call = backend_calls[backend]
        for attempt in range(cfg.retries + 1):
            try:
                return call(cfg, system_prompt, user_prompt)
            except BackendUnavailable as e:
                last_error = e
                break  # 環境起因の失敗はリトライしても直らない
            except AdvisorError as e:
                last_error = e
                if attempt < cfg.retries:
                    print(f"⚠️  {backend} の呼び出しに失敗（{attempt + 1}回目）。"
                          f"{cfg.retry_wait_seconds}秒後に再試行します: {e}")
                    sleep(cfg.retry_wait_seconds)
        failures.append(f"{backend}: {last_error}")
        if i + 1 < len(chain):
            print(f"⚠️  {backend} が使えないため、ローカルLLM"
                  f"（{cfg.base_url} / {cfg.model}）にフォールバックします: {last_error}")

    if len(failures) == 1:
        raise last_error
    raise AdvisorError("すべてのLLMバックエンドに失敗しました:\n- " + "\n- ".join(failures))


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
