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
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Callable

import requests

from .advice_evidence import AdviceEvidence, build_advice_evidence
from .config import LLMConfig

BUNDLED_PROMPTS = (
    "daily_advisor",
    "weekly_review",
    "ai_work_deep_review",
    "privacy_safe",
    "coach",
)

# Claude Code / Copilot CLI はセッション JSONL にプロンプトが残るため、
# KaizenLog 自身の呼び出しを計測から除外するためのセンチネル。
# openai-compatible（Ollama 等）はセッションログを残さないため対象外。
INTERNAL_SENTINEL = (
    "[kaizenlog-internal] 本行は計測除外用マーカーです。回答に影響させず無視してください。"
)
INTERNAL_SENTINEL_TOKEN = "[kaizenlog-internal]"


def apply_internal_sentinel(system_prompt: str, backend: str) -> str:
    """CLI バックエンドの system 先頭に計測除外センチネルを付ける（冪等）。

    dry-run 表示と本実行で同じ文字列になるよう、呼び出し側でも共有する。
    openai-compatible はセッションログを残さないため付与しない。
    """
    if backend not in ("claude-code-cli", "copilot-cli"):
        return system_prompt
    text = system_prompt or ""
    if text.lstrip().startswith(INTERNAL_SENTINEL_TOKEN):
        return text
    return INTERNAL_SENTINEL + chr(10) + text



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

_BASIC_PASS_METRICS = (
    "context_switches",
    "total_active_minutes",
)
_STRUCTURED_AI_PASS_METRICS = (
    "ai_cc_sessions",
    "ai_fragmented_sessions",
    "ai_retry_chains",
    "ai_tool_errors",
    "ai_interruptions",
    "ai_avg_turns",
    "ai_output_tokens",
)
_INPUT_PASS_METRICS = (
    "focus_blocks",
    "focus_minutes",
    "input_keypresses",
)
_PASS_METRIC_CONTRACT_MARKER = "{{KAIZENLOG_PASS_METRIC_CONTRACT}}"


def available_pass_metrics(evidence: AdviceEvidence) -> tuple[str, ...]:
    """当日の evidence で翌日に機械判定できる PASS 指標だけを返す。"""
    metrics = list(_BASIC_PASS_METRICS)
    if evidence.structured_ai_metrics_available:
        metrics.extend(_STRUCTURED_AI_PASS_METRICS)
    if evidence.input_metrics_available:
        metrics.extend(_INPUT_PASS_METRICS)
    if evidence.known_categories:
        metrics.extend(
            f"category_minutes:{category}"
            for category in sorted(evidence.known_categories)
        )
    if evidence.site_metrics_available and evidence.observed_sites:
        metrics.extend(
            f"site_minutes:{site}"
            for site in sorted(evidence.observed_sites)
        )
    return tuple(metrics)


def render_pass_metric_contract(evidence: AdviceEvidence) -> str:
    """初回生成と修復で共有する、当日のPASS指標可否契約。"""
    available = available_pass_metrics(evidence)
    forbidden: list[str] = []
    if not evidence.structured_ai_metrics_available:
        forbidden.extend(_STRUCTURED_AI_PASS_METRICS)
    if not evidence.input_metrics_available:
        forbidden.extend(_INPUT_PASS_METRICS)
    if not evidence.site_metrics_available:
        forbidden.append("site_minutes:<ドメイン>")
    available_text = " / ".join(available) or "なし"
    forbidden_text = " / ".join(forbidden) or "なし"
    return (
        "## 当日使用可能なPASS指標\n"
        f"{available_text}\n\n"
        "## 当日使用禁止のPASS指標\n"
        f"{forbidden_text}\n"
    )


def build_prompt(
    today_md: str,
    recent_summaries: list[str],
    intent: str | None = None,
    experiments: str | None = None,
    memory: str | None = None,
    evidence: str | AdviceEvidence | None = None,
    reflections: str | None = None,
) -> str:
    parts: list[str] = []
    if evidence:
        parts.append(evidence.markdown if isinstance(evidence, AdviceEvidence) else evidence)
        parts.append("\n\n")
    if reflections:
        parts.append("# ユーザーの振り返り（本人の言葉。最優先の文脈）\n")
        parts.append(reflections)
        parts.append("\n\n")
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


class AdviceContractError(AdvisorError):
    """改善提案が保存可能な出力契約を満たさない。"""

    def __init__(self, message: str, violations: list[str] | None = None):
        super().__init__(message)
        # 種別分類用の短いメッセージ列（本文・プロンプトは載せない前提で呼び出し側がタグ化）
        self.violations = list(violations) if violations else [str(message)]


@dataclass
class AdviceResult:
    """generate_advice の戻り値。markdown と品質 outcome。"""

    markdown: str
    outcome: str = "ok"  # ok | repaired
    violations: list[str] = field(default_factory=list)


@dataclass
class PipelineReport:
    """日次契約パイプラインの計測レポート（eval / デバッグ用）。"""

    parse_ok: bool = False
    first_pass: bool = False  # 初回生成で契約合格
    repaired: bool = False
    final_ok: bool = False
    first_violations: list[str] = field(default_factory=list)
    final_violations: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    # ok | repaired | degraded（契約未達）| passthrough（日次契約外）
    outcome: str = "ok"
    markdown: str | None = None


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


def _copilot_launch_command(exe: str) -> list[str]:
    """Copilot CLIをシェル解釈なしで起動するコマンドを返す。

    Windowsのnpm shim（copilot.CMD）へ複数行プロンプトを渡すと、cmd.exeが
    改行以降を別コマンドとして解釈し、先頭行しかCopilotへ届かない。また引用符や
    `%`等を置換する回避策はプロンプトの意味を変える。公式npm binの実体である
    npm-loader.jsをNodeで直接起動し、subprocessの引数配列を最後まで維持する。
    """
    if not exe.lower().endswith((".cmd", ".bat")):
        return [exe]

    shim_dir = Path(exe).parent
    loader = shim_dir / "node_modules" / "@github" / "copilot" / "npm-loader.js"
    if not loader.is_file():
        raise BackendUnavailable(
            f"Copilot CLIのnpm-loader.jsが見つかりません: {loader}\n"
            "`npm install -g @github/copilot` で再インストールしてください。"
        )

    bundled_node = shim_dir / "node.exe"
    node = str(bundled_node) if bundled_node.is_file() else shutil.which("node")
    if not node:
        raise BackendUnavailable(
            "Copilot CLIの起動に必要なNode.jsが見つかりません。"
            "Node.jsをインストールしてPATHを確認してください。"
        )
    return [node, str(loader)]


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
    prompt = f"{system_prompt}\n\n{user_prompt}"
    cmd = [
        *_copilot_launch_command(exe),
        "-p", prompt,
        "--silent",
        "--no-custom-instructions",
        "--disable-builtin-mcps",
        "--no-ask-user",
        *cfg.copilot_extra_args,
    ]
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
        "reasoning_effort": cfg.reasoning_effort,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if cfg.json_mode:
        payload["response_format"] = {"type": "json_object"}
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
                # CLI のみセンチネル付与（backend ごとに。フォールバック先 openai は対象外）
                sp = apply_internal_sentinel(system_prompt, backend)
                return call(cfg, sp, user_prompt)
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


_FACT_ID_RE = re.compile(r"\[F\d+\]")
_ACTION_RE = re.compile(r"^\s*- \[ \]\s+(.+)$", re.MULTILINE)


def _coerce_evidence(evidence: AdviceEvidence | None) -> AdviceEvidence:
    if isinstance(evidence, AdviceEvidence):
        return evidence
    if evidence is None:
        return build_advice_evidence(None)
    raise TypeError("evidence は AdviceEvidence で渡してください")


def _level3_sections(markdown: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in markdown.splitlines():
        if line.startswith("### "):
            current = line[4:].strip()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def evidence_gated_action_errors(
    action_text: str,
    index: int,
    evidence: AdviceEvidence,
) -> list[str]:
    """evidence ゲート付きの内容チェック（JSON 層・Markdown 層で共有）。

    意味系違反として AdviceContractError 経路へ送る（renderer bug にしない）。
    """
    errors: list[str] = []
    if not evidence.previous_day_available and "前日" in action_text:
        errors.append(
            f"最小アクション{index}は比較可能な前日データがないため前日比を使えません"
        )
    if "通知" in action_text:
        errors.append(
            f"最小アクション{index}は通知を計測していないため通知操作を根拠にできません"
        )
    if (
        not evidence.ai_conversation_metrics_available
        and re.search(
            r"\bAI\b|ChatGPT|Claude|Copilot|プロンプト|メッセージ",
            action_text,
            re.IGNORECASE,
        )
        and re.search(r"まとめ|一括|往復|依頼内容", action_text)
    ):
        errors.append(
            f"最小アクション{index}はAI会話を計測していないため依頼方法を最適化できません"
        )
    if (
        not evidence.browser_sample_sufficient
        and re.search(r"watcher|URL観測|拡張機能", action_text, re.IGNORECASE)
    ):
        errors.append(
            f"最小アクション{index}はブラウザ実測が短いためwatcher設定を優先できません"
        )
    return errors


def render_reader_advice(advice_md: str, evidence: AdviceEvidence) -> str:
    """検証済みの内部回答を、F-IDを見せない読者向け日次提案へ変換する。"""
    sections = _level3_sections(advice_md)
    actions = _ACTION_RE.findall(sections.get("明日の最小アクション", ""))
    proposal_contexts = {
        int(match.group("index")): (
            re.sub(r"\s{2,}", " ", _FACT_ID_RE.sub("", match.group("why"))).strip(),
            re.sub(r"\s{2,}", " ", _FACT_ID_RE.sub("", match.group("metric"))).strip(),
        )
        for match in re.finditer(
            r"^\s*(?P<index>\d+)\.\s*(?P<why>.+?)。(?P<proposal>[^。]+)。翌日見る指標:\s*(?P<metric>.+?)\s*$",
            sections.get("今日の改善提案", ""),
            re.MULTILINE,
        )
    }
    rendered_actions = []
    for index, action in enumerate(actions, 1):
        without_ids = _FACT_ID_RE.sub("", action)
        cleaned = re.sub(r"\s{2,}", " ", without_ids).strip()
        if cleaned:
            rendered = f"- [ ] {cleaned}"
            if (context := proposal_contexts.get(index)) is not None:
                why, metric = context
                if why and metric:
                    rendered += f"\n    - なぜ: {why}\n    - 明日見る数字: {metric}"
            rendered_actions.append(rendered)
    if not rendered_actions:
        raise AdviceContractError(
            "読者向け再構成に必要な明日の最小アクションを抽出できません",
            violations=["明日の最小アクションに有効なチェックボックスがありません"],
        )

    rendered = (
        "## 🚀 Kaizen（AIからの改善提案）\n\n"
        "### 今日の結論\n\n"
        f"{evidence.reader_summary}\n\n"
        "### 明日試すこと\n\n"
        + "\n".join(rendered_actions)
    )
    ai_review_lines = [
        re.sub(
            r"\s{2,}",
            " ",
            re.sub(r"^-\s+", "- ", _FACT_ID_RE.sub("", line)),
        ).strip()
        for line in sections.get("AI作業の改善", "").splitlines()
        if line.startswith("- ")
    ]
    if ai_review_lines:
        rendered += "\n\n### AI作業の見立て\n\n" + "\n".join(ai_review_lines)
    if not evidence.reader_notes:
        return rendered
    return rendered + "\n\n### 計測上の注意\n\n" + "\n".join(evidence.reader_notes)


def _has_uncertainty_language(sentence: str) -> bool:
    return any(
        phrase in sentence
        for phrase in (
            "ではない", "ではなく", "でない", "判断不能", "測定不能", "評価対象外",
            "不明", "根拠なし", "根拠がない", "断定しない", "意味しない",
            "とは限らない",
        )
    )


def _observed_clause(sentence: str) -> str:
    cutoffs = [
        position for marker in ("明日", "翌日", "PASS:", "FAIL:")
        if (position := sentence.find(marker)) >= 0
    ]
    return sentence[:min(cutoffs)] if cutoffs else sentence


# requires_daily_contract が真の同梱日本語プロンプト（daily_advisor / privacy_safe）専用。
# 英語版プロンプトを追加する場合は、このキーワード表に対訳を足すこと（沈黙劣化防止）。
_JA_ENTERTAINMENT_RE = re.compile(
    r"(?:(?:娯楽|私用|エンタメ).{0,12}(?:利用|閲覧|視聴|浪費|費や|食い込|多い|発生)"
    r"|ブラウジング.{0,12}(?:娯楽|私用))"
)
_JA_AI_NUMERIC_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:回|件)?\s*(?:往復|発話|会話セッション)"
    r"|(?:往復|発話|会話セッション)(?:数)?\s*(?:は|が|:|：)?\s*\d+)"
)
_JA_AI_QUALITY_RE = re.compile(
    r"(?:(?:会話|セッション|往復).{0,12}(?:多い|少ない|短い|長い|細切れ|過多|多発)"
    r"|(?:往復過多|会話品質|AI品質))"
)
_JA_F4_CONVERSION_RE = re.compile(r"会話|セッション|往復")
_JA_NOTIFY_CAUSE_RE = re.compile(
    r"(?:通知|割り込み|中断|(?:生産性|集中力).{0,6}(?:低下|下が|悪化))"
)
_JA_MEASURE_INSTRUCTION_RE = re.compile(r"記録|計測|測定|確認|設定|目標|条件")
_JA_MEASURE_PAST_RE = re.compile(r"した|だった|発生した|実績")


def _semantic_contract_errors(advice: str, evidence: AdviceEvidence) -> list[str]:
    """今回の根本原因になった既知の禁止推論を決定論的に止める。"""
    errors: list[str] = []
    sentences = [part.strip() for part in re.split(r"[。\n]", advice) if part.strip()]

    def is_measurement_instruction(clause: str) -> bool:
        return (
            bool(_JA_MEASURE_INSTRUCTION_RE.search(clause))
            and not bool(_JA_MEASURE_PAST_RE.search(clause))
        )

    if not evidence.entertainment_observed:
        entertainment_claim = _JA_ENTERTAINMENT_RE
        for sentence in sentences:
            clause = _observed_clause(sentence)
            if (
                entertainment_claim.search(clause)
                and not _has_uncertainty_language(clause)
                and not is_measurement_instruction(clause)
            ):
                errors.append("エンタメ根拠がない日に娯楽・私用利用を断定しています")
                break

    if not evidence.ai_conversation_metrics_available:
        for sentence in sentences:
            clause = _observed_clause(sentence)
            ai_fragmentation_claim = (
                "細切れ" in clause
                and bool(re.search(r"AI|会話|セッション|往復", clause))
            )
            if (
                (
                    _JA_AI_NUMERIC_RE.search(clause)
                    or _JA_AI_QUALITY_RE.search(clause)
                    or ai_fragmentation_claim
                )
                and not _has_uncertainty_language(clause)
                and not is_measurement_instruction(clause)
            ):
                errors.append("AI会話テレメトリがないのに回数・品質を断定しています")
                break

    if "[F4]" in evidence.fact_ids:
        for sentence in sentences:
            clause = _observed_clause(sentence)
            if (
                "[F4]" in clause
                and "[F5]" not in clause
                and _JA_F4_CONVERSION_RE.search(clause)
                and not _has_uncertainty_language(clause)
                and not is_measurement_instruction(clause)
            ):
                errors.append("AI関連画面ブロック数を会話数・セッション数・往復数へ変換しています")
                break

    if "[F1]" in evidence.fact_ids:
        for sentence in sentences:
            clause = _observed_clause(sentence)
            if (
                any(f"[F{fact_id}]" in clause for fact_id in (1, 8, 9))
                and "[F5]" not in clause
                and _JA_NOTIFY_CAUSE_RE.search(clause)
                and not _has_uncertainty_language(clause)
                and not is_measurement_instruction(clause)
            ):
                errors.append("カテゴリ変更回数を通知・割り込み・生産性低下へ変換しています")
                break
    return errors


def _baseline_repair_hint(evidence: AdviceEvidence) -> str:
    """修復時に明示するベースライン一覧（数値はコード側の確定事実）。"""
    basemap = evidence.metric_baselines
    if not basemap:
        return ""
    # 主要指標だけ短く（プロンプト肥大防止）
    keys = (
        "context_switches",
        "ai_tool_errors",
        "ai_cc_sessions",
        "focus_blocks",
        "total_active_minutes",
    )
    bits = [
        f"{k}={basemap[k]:g}"
        for k in keys
        if k in basemap and isinstance(basemap[k], (int, float))
    ]
    if not bits:
        # 先頭数件
        for k, v in sorted(basemap.items())[:5]:
            if isinstance(v, (int, float)):
                bits.append(f"{k}={float(v):g}")
    if not bits:
        return ""
    return (
        "## ベースライン（直近履歴の中央値・PASS はこの値より挑戦的に）\n"
        + " / ".join(bits)
        + "\n\n"
    )


def _contract_repair_prompt(
    evidence: AdviceEvidence,
    advice: str,
    errors: list[str],
) -> str:
    """日次提案の修復プロンプト。回答は JSON オブジェクトのみを要求する。"""
    rendered_errors = "\n".join(f"- {error}" for error in errors)
    allowed_ids = " ".join(sorted(evidence.fact_ids)) or "なし"
    # JSON / Markdown どちらが来ても KZN と過剰な数字を薄める
    repair_source = re.sub(r"KZN-\d{8}-\d+", "既存アクション", advice)
    repair_source = re.sub(r"\d{2,}", "数値省略", repair_source)
    return (
        "# 出力契約の修正依頼\n"
        "前回の回答は保存条件を満たしませんでした。分析事実を増やしたり推測したりせず、"
        "同じ内容を **JSON オブジェクトだけ** で書き直してください。\n"
        "説明文・Markdown・コードフェンスは付けないでください。\n"
        "\n## 必須スキーマ\n"
        '{"plan_review": string|null, "proposals": [...], "actions": [...], '
        '"ai_review": [...]}\n'
        "\n## 修正チェックリスト\n"
        "- proposals と actions は1〜3件かつ同数。ai_review は1〜2件\n"
        "- fact_ids は F 番号（例: \"F3\"）。使用可能: "
        f"{allowed_ids}\n"
        "- interpretation / ai_review.text に算用数字を書かない"
        "（観測値の再掲禁止）\n"
        "- pass は機械構文のみ: `指標名 演算子 数値`"
        "（例: `context_switches <= 40`）。"
        "当日使用可能なPASS指標だけを使い、自由文 PASS は禁止\n"
        "- fail は数値条件（機械構文可）。PASS 目標はベースラインより挑戦的に"
        "（減らす目標は baseline×0.95 超を禁止、増やす目標は baseline×1.05 未満を禁止）\n"
        "- KZN ID と HTML コメントは禁止\n"
        "- AI関連画面ブロックは会話数・セッション数・往復数ではない\n\n"
        f"{render_pass_metric_contract(evidence)}\n"
        f"{_baseline_repair_hint(evidence)}"
        f"## 違反\n{rendered_errors}\n\n"
        "## 前回の回答（一部マスク済み）\n"
        f"{repair_source}"
    )


def requires_daily_contract(cfg: LLMConfig) -> bool:
    """日次フォーマットを約束する同梱プロンプトだけを厳格検証する。

    weekly_review、ai_work_deep_review、自作プロンプトには別の出力契約があるため、
    日次の見出しを強制すると既存のsystem_prompt差し替え機能を壊してしまう。
    """
    return (cfg.system_prompt or "daily_advisor") in {"daily_advisor", "privacy_safe"}


def _assert_redaction_preserves_daily_protocol(
    system_prompt: str,
    prompt: str,
    evidence: AdviceEvidence,
) -> None:
    """広すぎるマスク規則がF-IDやJSONキーを壊したら、送信前に明示失敗する。"""
    expected_ids = set(evidence.fact_ids)
    remaining_ids = set(_FACT_ID_RE.findall(prompt))
    # 構造化出力移行後は見出しではなく JSON スキーマキーが制御トークン
    required_keys = ('"proposals"', '"actions"', '"ai_review"')
    if not expected_ids <= remaining_ids or any(
        key not in system_prompt for key in required_keys
    ):
        raise AdvisorError(
            "privacy.redact_patterns が改善提案の制御トークン（[F#] または JSON キー）"
            "までマスクしています。パターンを固有名詞へ限定してください。"
        )


def prepare_advice_request(
    cfg: LLMConfig,
    today_md: str,
    recent_summaries: list[str],
    intent: str | None = None,
    experiments: str | None = None,
    memory: str | None = None,
    redactor: Callable[[str], str] | None = None,
    evidence: AdviceEvidence | None = None,
    reflections: str | None = None,
) -> tuple[str, str, AdviceEvidence | None]:
    """dry-runと本実行で完全に同じsystem/user promptを準備する。"""
    daily_contract = requires_daily_contract(cfg)
    evidence_ctx = (
        _coerce_evidence(evidence)
        if evidence is not None or daily_contract
        else None
    )
    prompt = build_prompt(
        today_md,
        recent_summaries,
        intent,
        experiments,
        memory,
        evidence_ctx,
        reflections=reflections,
    )
    system_prompt = resolve_system_prompt(cfg)
    if daily_contract and evidence_ctx is not None:
        if system_prompt.count(_PASS_METRIC_CONTRACT_MARKER) != 1:
            raise AdvisorError(
                "日次system promptにPASS指標契約マーカーが1個必要です"
            )
        system_prompt = system_prompt.replace(
            _PASS_METRIC_CONTRACT_MARKER,
            render_pass_metric_contract(evidence_ctx),
        )
    if redactor:
        prompt = redactor(prompt)
        system_prompt = redactor(system_prompt)
        if daily_contract and evidence_ctx is not None:
            _assert_redaction_preserves_daily_protocol(system_prompt, prompt, evidence_ctx)
    return system_prompt, prompt, evidence_ctx


def _run_daily_pipeline(
    cfg: LLMConfig,
    system_prompt: str,
    prompt: str,
    evidence: AdviceEvidence,
    *,
    redactor: Callable[[str], str] | None = None,
    generate_fn: Callable[[LLMConfig, str, str], str] | None = None,
) -> tuple[str | None, PipelineReport]:
    """生成→解析→検証→修復1回→レンダリングを実行し、本文と計測レポートを返す。

    契約未達時は markdown=None と outcome=degraded を返し、例外は投げない
    （呼び出し側 generate_advice が AdviceContractError に変換する）。
    """
    from .advice_format import (
        normalize_advice_cardinality,
        parse_advice_json,
        render_advice_markdown,
        validate_advice,
    )

    gen = generate_fn or generate_text
    t0 = time.monotonic()
    report = PipelineReport()

    def _try_parse_and_validate(text: str) -> tuple[dict | None, list[str]]:
        try:
            data = parse_advice_json(text)
        except AdviceContractError as e:
            return None, [str(e)]
        data = normalize_advice_cardinality(data, evidence)
        return data, validate_advice(data, evidence)

    def _repair_once(source: str, errs: list[str]) -> str:
        print(f"⚠️  出力契約違反を検出、1回だけ修復を試みます: {errs[0]}")
        repair_prompt = _contract_repair_prompt(evidence, source, errs)
        if redactor:
            repair_prompt = redactor(repair_prompt)
        return gen(cfg, system_prompt, repair_prompt)

    raw = gen(cfg, system_prompt, prompt)
    data, errors = _try_parse_and_validate(raw)
    report.parse_ok = data is not None and not errors
    report.first_violations = list(errors)
    report.first_pass = bool(data is not None and not errors)
    repaired = False
    first_errors = list(errors)

    if errors:
        raw = _repair_once(raw, errors)
        repaired = True
        report.repaired = True
        data, errors = _try_parse_and_validate(raw)

    if errors or data is None:
        report.final_ok = False
        report.final_violations = list(errors or first_errors)
        report.outcome = "degraded"
        report.duration_seconds = round(time.monotonic() - t0, 3)
        return None, report

    try:
        markdown = render_advice_markdown(data, evidence)
    except AdviceContractError as e:
        if repaired:
            report.final_ok = False
            report.final_violations = list(e.violations or first_errors)
            report.outcome = "degraded"
            report.duration_seconds = round(time.monotonic() - t0, 3)
            return None, report
        err_list = [
            line.lstrip("- ").strip()
            for line in str(e).splitlines()
            if line.strip()
        ]
        if err_list and "保存条件" in err_list[0]:
            err_list = err_list[1:] or [str(e)]
        raw = _repair_once(raw, err_list)
        repaired = True
        report.repaired = True
        data, errors = _try_parse_and_validate(raw)
        if errors or data is None:
            report.final_ok = False
            report.final_violations = list(errors or err_list)
            report.outcome = "degraded"
            report.duration_seconds = round(time.monotonic() - t0, 3)
            return None, report
        try:
            markdown = render_advice_markdown(data, evidence)
        except AdviceContractError as e2:
            report.final_ok = False
            report.final_violations = list(e2.violations or err_list)
            report.outcome = "degraded"
            report.duration_seconds = round(time.monotonic() - t0, 3)
            return None, report
        full = f"## 🚀 Kaizen（AIからの改善提案）\n\n{markdown}"
        report.final_ok = True
        report.outcome = "repaired"
        report.markdown = full
        report.first_violations = err_list
        report.duration_seconds = round(time.monotonic() - t0, 3)
        return full, report

    full = f"## 🚀 Kaizen（AIからの改善提案）\n\n{markdown}"
    report.final_ok = True
    report.outcome = "repaired" if repaired else "ok"
    report.markdown = full
    if not repaired:
        report.first_violations = []
    report.duration_seconds = round(time.monotonic() - t0, 3)
    return full, report


def generate_advice(
    cfg: LLMConfig,
    today_md: str,
    recent_summaries: list[str],
    intent: str | None = None,
    experiments: str | None = None,
    memory: str | None = None,
    redactor: Callable[[str], str] | None = None,
    evidence: AdviceEvidence | None = None,
    reflections: str | None = None,
) -> AdviceResult:
    system_prompt, prompt, evidence_ctx = prepare_advice_request(
        cfg,
        today_md,
        recent_summaries,
        intent,
        experiments,
        memory,
        redactor,
        evidence,
        reflections=reflections,
    )

    # 日次プロンプト以外は従来どおり素通し
    if not requires_daily_contract(cfg):
        raw = generate_text(cfg, system_prompt, prompt)
        return AdviceResult(
            markdown=f"## 🚀 Kaizen（AIからの改善提案）\n\n{raw}",
            outcome="ok",
        )

    assert evidence_ctx is not None
    markdown, report = _run_daily_pipeline(
        cfg, system_prompt, prompt, evidence_ctx, redactor=redactor
    )
    if not report.final_ok or markdown is None:
        errs = report.final_violations or report.first_violations or ["契約未達"]
        raise AdviceContractError(
            "LLMの改善提案が保存条件を満たしませんでした:\n- " + "\n- ".join(errs),
            violations=errs,
        )
    return AdviceResult(
        markdown=markdown,
        outcome=report.outcome if report.outcome in ("ok", "repaired") else "ok",
        violations=report.first_violations if report.repaired else [],
    )
