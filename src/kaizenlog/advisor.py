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
    evidence: str | AdviceEvidence | None = None,
) -> str:
    parts: list[str] = []
    if evidence:
        parts.append(evidence.markdown if isinstance(evidence, AdviceEvidence) else evidence)
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


_FACT_ID_RE = re.compile(r"\[F\d+\]")
_NUMBERED_ITEM_RE = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_ACTION_RE = re.compile(r"^\s*- \[ \]\s+(.+)$", re.MULTILINE)
_ANY_CHECKBOX_RE = re.compile(r"^\s*- \[[ xX]\]\s+.+$", re.MULTILINE)
_MEASURABLE_COMPARISON_RE = re.compile(
    r"(?:前日|前回|基準).{0,12}(?:比|より|同数|同水準|同じ)"
    r"|(?:同数|同水準|同数値|増加|減少|上昇|低下|変化なし)"
)


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


def _numbered_items(section: str) -> list[str]:
    matches = list(_NUMBERED_ITEM_RE.finditer(section))
    return [
        section[match.start(): matches[i + 1].start() if i + 1 < len(matches) else None].strip()
        for i, match in enumerate(matches)
    ]


def advice_contract_errors(
    advice: str,
    evidence: AdviceEvidence | None = None,
) -> list[str]:
    """保存前に、根拠・行動・翌日判定が1対1で揃っているか検証する。"""
    errors: list[str] = []
    evidence_ctx = _coerce_evidence(evidence)
    if "```" in advice or "~~~" in advice:
        errors.append("日次回答をコードフェンスで囲まないでください")
    headings = [
        line[4:].strip()
        for line in advice.splitlines()
        if line.startswith("### ")
    ]
    allowed_headings = {"計画と実績", "今日の改善提案", "明日の最小アクション", "AI作業の改善"}
    unexpected = sorted(set(headings) - allowed_headings)
    if unexpected:
        errors.append("許可されていない見出しがあります: " + "、".join(unexpected))
    sections = _level3_sections(advice)
    required = ("今日の改善提案", "明日の最小アクション", "AI作業の改善")
    for heading in required:
        if headings.count(heading) != 1:
            errors.append(f"見出し「### {heading}」は1回だけ使用してください")
        body = sections.get(heading, "")
        if not body:
            errors.append(f"必須見出し「### {heading}」がない、または空です")
        elif any(line.lstrip().startswith("#") for line in body.splitlines()):
            errors.append(f"「### {heading}」内にサブ見出しを置かないでください")
    if headings.count("計画と実績") > 1:
        errors.append("見出し「### 計画と実績」は最大1回にしてください")

    improvements = _numbered_items(sections.get("今日の改善提案", ""))
    if not 1 <= len(improvements) <= 3:
        errors.append("「今日の改善提案」は番号付きで1〜3件にしてください")

    actions = _ACTION_RE.findall(sections.get("明日の最小アクション", ""))
    if not 1 <= len(actions) <= 3:
        errors.append("「明日の最小アクション」は未チェックのチェックボックスで1〜3件にしてください")
    elif len(actions) > evidence_ctx.max_actions:
        errors.append(
            f"当日のデータ量では改善アクションは最大{evidence_ctx.max_actions}件にしてください"
        )
    if improvements and actions and len(improvements) != len(actions):
        errors.append("改善提案と最小アクションの件数を1対1にしてください")
    if len(_ANY_CHECKBOX_RE.findall(advice)) != len(actions):
        errors.append("チェックボックスは「明日の最小アクション」の未チェック行だけにしてください")
    # F-ID 引用・観測数値再掲の検査は JSON 層で行う（U3: レンダ後テキストには F-ID を出さない）

    for index, action in enumerate(actions, 1):
        pass_position = action.find("PASS:")
        fail_position = action.find("FAIL:")
        pass_value = (
            action[pass_position + len("PASS:"):fail_position].strip(" ｜|/\t")
            if 0 <= pass_position < fail_position else ""
        )
        fail_value = (
            action[fail_position + len("FAIL:"):].strip(" ｜|/\t")
            if fail_position >= 0 else ""
        )
        if not pass_value or not fail_value:
            errors.append(f"最小アクション{index}に翌日の PASS:/FAIL: 条件がありません")
        else:
            # 機械構文らしい PASS は既知指標のみ。注記括弧（…）は除去して判定。
            from .verdict import is_known_metric, looks_like_machine_pass, strip_pass_annotation
            core_pass = strip_pass_annotation(pass_value)
            if looks_like_machine_pass(core_pass):
                m_metric = re.match(r"^(\S+)\s*(?:<=|>=|<|>|==?)", core_pass.strip())
                metric_name = m_metric.group(1) if m_metric else core_pass.split()[0]
                if not is_known_metric(metric_name):
                    errors.append(
                        f"最小アクション{index}の PASS: 指標名が使用可能な指標にありません"
                    )
                if not _is_measurable_condition(fail_value):
                    errors.append(
                        f"最小アクション{index}の PASS:/FAIL: は数値条件にしてください"
                    )
            elif (
                not _is_measurable_condition(pass_value)
                or not _is_measurable_condition(fail_value)
            ):
                errors.append(
                    f"最小アクション{index}の PASS:/FAIL: は数値条件にしてください"
                )
        if re.search(r"KZN-\d{8}-\d+", action):
            errors.append(f"最小アクション{index}にモデル生成のKZN IDがあります")
        # evidence ゲート付き内容チェックは PASS 注記を剥がしてから（レンダラ由来ラベル誤爆防止）
        errors.extend(
            evidence_gated_action_errors(
                _action_text_without_pass_annotation(action), index, evidence_ctx
            )
        )
        # 改善提案とアクションの F-ID 対応は JSON 層で検証済み（表示文には F-ID 無し）

    errors.extend(_semantic_contract_errors(advice, evidence_ctx))

    return errors


def _action_text_without_pass_annotation(action: str) -> str:
    """アクション行の PASS セグメントから注記括弧を除去した走査用テキスト。"""
    from .verdict import strip_pass_annotation

    pass_position = action.find("PASS:")
    fail_position = action.find("FAIL:")
    if not (0 <= pass_position < fail_position):
        return action
    head = action[: pass_position + len("PASS:")]
    # 全角｜区切りを落としてから注記 strip（末尾が ｜ だと括弧除去が効かない）
    pass_value = action[pass_position + len("PASS:") : fail_position].strip(" ｜|/\t")
    tail = action[fail_position:]
    return f"{head} {strip_pass_annotation(pass_value)} {tail}"


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


def collect_evidence_gated_errors(
    advice: str, evidence: AdviceEvidence
) -> list[str]:
    """レンダ済み Markdown から evidence ゲート付きエラーだけを集める（分類用）。"""
    sections = _level3_sections(advice)
    actions = _ACTION_RE.findall(sections.get("明日の最小アクション", ""))
    out: list[str] = []
    for index, action in enumerate(actions, 1):
        out.extend(
            evidence_gated_action_errors(
                _action_text_without_pass_annotation(action), index, evidence
            )
        )
    return out


def render_reader_advice(advice_md: str, evidence: AdviceEvidence) -> str:
    """検証済みの内部回答を、F-IDを見せない読者向け日次提案へ変換する。"""
    sections = _level3_sections(advice_md)
    actions = _ACTION_RE.findall(sections.get("明日の最小アクション", ""))
    rendered_actions = []
    for action in actions:
        without_ids = _FACT_ID_RE.sub("", action)
        cleaned = re.sub(r"\s{2,}", " ", without_ids).strip()
        rendered_actions.append(f"- [ ] {cleaned}")

    notes = "\n".join(evidence.reader_notes)
    return (
        "## 🚀 Kaizen（AIからの改善提案）\n\n"
        "### 今日の結論\n\n"
        f"{evidence.reader_summary}\n\n"
        "### 明日試すこと\n\n"
        + "\n".join(rendered_actions)
        + "\n\n### 計測上の注意\n\n"
        + notes
    )


def _is_measurable_condition(value: str) -> bool:
    """数値リテラルまたは前日値等との明示的な相対比較を判定可能とみなす。"""
    return bool(re.search(r"\d", value) or _MEASURABLE_COMPARISON_RE.search(value))


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


def _observed_value_restatement_errors(sections: dict[str, str]) -> list[str]:
    """観測値はコード生成のF本文だけに置き、LLMには数値を再入力させない。"""
    for heading in ("今日の改善提案", "AI作業の改善"):
        for sentence in re.split(r"[。\n]", sections.get(heading, "")):
            clause = _observed_clause(sentence)
            without_ids = _FACT_ID_RE.sub("", clause)
            without_numbering = re.sub(r"^\s*\d+[.)]\s*", "", without_ids)
            if re.search(r"\d", without_numbering):
                return [
                    f"「### {heading}」では観測数値を再掲せず、根拠ID [F#] だけを参照してください"
                ]
    return []


def _semantic_contract_errors(advice: str, evidence: AdviceEvidence) -> list[str]:
    """今回の根本原因になった既知の禁止推論を決定論的に止める。"""
    errors: list[str] = []
    sentences = [part.strip() for part in re.split(r"[。\n]", advice) if part.strip()]

    def is_measurement_instruction(clause: str) -> bool:
        return (
            bool(re.search(r"記録|計測|測定|確認|設定|目標|条件", clause))
            and not bool(re.search(r"した|だった|発生した|実績", clause))
        )

    if not evidence.entertainment_observed:
        entertainment_claim = re.compile(
            r"(?:(?:娯楽|私用|エンタメ).{0,12}(?:利用|閲覧|視聴|浪費|費や|食い込|多い|発生)"
            r"|ブラウジング.{0,12}(?:娯楽|私用))"
        )
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
        numeric_ai_claim = re.compile(
            r"(?:\d+(?:\.\d+)?\s*(?:回|件)?\s*(?:往復|発話|会話セッション)"
            r"|(?:往復|発話|会話セッション)(?:数)?\s*(?:は|が|:|：)?\s*\d+)"
        )
        unsupported_ai_quality = re.compile(
            r"(?:(?:会話|セッション|往復).{0,12}(?:多い|少ない|短い|長い|細切れ|過多|多発)"
            r"|(?:往復過多|会話品質|AI品質))"
        )
        for sentence in sentences:
            clause = _observed_clause(sentence)
            ai_fragmentation_claim = (
                "細切れ" in clause
                and bool(re.search(r"AI|会話|セッション|往復", clause))
            )
            if (
                (
                    numeric_ai_claim.search(clause)
                    or unsupported_ai_quality.search(clause)
                    or ai_fragmentation_claim
                )
                and not _has_uncertainty_language(clause)
                and not is_measurement_instruction(clause)
            ):
                errors.append("AI会話テレメトリがないのに回数・品質を断定しています")
                break

    if "[F4]" in evidence.fact_ids:
        conversion = re.compile(r"会話|セッション|往復")
        for sentence in sentences:
            clause = _observed_clause(sentence)
            if (
                "[F4]" in clause
                and "[F5]" not in clause
                and conversion.search(clause)
                and not _has_uncertainty_language(clause)
                and not is_measurement_instruction(clause)
            ):
                errors.append("AI関連画面ブロック数を会話数・セッション数・往復数へ変換しています")
                break

    if "[F1]" in evidence.fact_ids:
        unsupported_cause = re.compile(
            r"(?:通知|割り込み|中断|(?:生産性|集中力).{0,6}(?:低下|下が|悪化))"
        )
        for sentence in sentences:
            clause = _observed_clause(sentence)
            if (
                any(f"[F{fact_id}]" in clause for fact_id in (1, 8, 9))
                and "[F5]" not in clause
                and unsupported_cause.search(clause)
                and not _has_uncertainty_language(clause)
                and not is_measurement_instruction(clause)
            ):
                errors.append("カテゴリ変更回数を通知・割り込み・生産性低下へ変換しています")
                break
    return errors


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
        "- pass/fail は数値条件。pass は可能な限り "
        "`指標 演算子 数値` の機械構文\n"
        "- KZN ID と HTML コメントは禁止\n"
        "- AI関連画面ブロックは会話数・セッション数・往復数ではない\n\n"
        f"## 違反\n{rendered_errors}\n\n"
        "## 前回の回答（一部マスク済み）\n"
        f"{repair_source}"
    )


def _sanitize_repair_source(advice: str) -> str:
    """修復モデルが禁止済みの観測数値やKZN IDをそのまま複写しないようにする。"""
    current_heading = ""
    output: list[str] = []
    for line in advice.splitlines():
        if line.startswith("### "):
            current_heading = line[4:].strip()
            output.append(line)
            continue
        if current_heading not in {"今日の改善提案", "AI作業の改善"}:
            output.append(line)
            continue

        sanitized = re.sub(r"KZN-\d{8}-\d+", "既存アクション", line)
        facts: list[str] = []

        def protect_fact(match: re.Match[str]) -> str:
            facts.append(match.group(0))
            return f"§FACT{chr(65 + len(facts) - 1)}§"

        sanitized = _FACT_ID_RE.sub(protect_fact, sanitized)
        numbering = re.match(r"^(\s*\d+[.)]\s*)", sanitized)
        prefix = numbering.group(1) if numbering else ""
        body = sanitized[len(prefix):]
        body = re.sub(r"\d+(?:\.\d+)?", "数値省略", body)
        for index, fact in enumerate(facts):
            body = body.replace(f"§FACT{chr(65 + index)}§", fact)
        output.append(prefix + body)
    return "\n".join(output)


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
) -> tuple[str, str, AdviceEvidence | None]:
    """dry-runと本実行で完全に同じsystem/user promptを準備する。"""
    daily_contract = requires_daily_contract(cfg)
    evidence_ctx = (
        _coerce_evidence(evidence)
        if evidence is not None or daily_contract
        else None
    )
    prompt = build_prompt(
        today_md, recent_summaries, intent, experiments, memory, evidence_ctx
    )
    system_prompt = resolve_system_prompt(cfg)
    if redactor:
        prompt = redactor(prompt)
        system_prompt = redactor(system_prompt)
        if daily_contract and evidence_ctx is not None:
            _assert_redaction_preserves_daily_protocol(system_prompt, prompt, evidence_ctx)
    return system_prompt, prompt, evidence_ctx


def generate_advice(
    cfg: LLMConfig,
    today_md: str,
    recent_summaries: list[str],
    intent: str | None = None,
    experiments: str | None = None,
    memory: str | None = None,
    redactor: Callable[[str], str] | None = None,
    evidence: AdviceEvidence | None = None,
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
    )
    raw = generate_text(cfg, system_prompt, prompt)

    # 日次プロンプト以外は従来どおり素通し
    if not requires_daily_contract(cfg):
        return AdviceResult(
            markdown=f"## 🚀 Kaizen（AIからの改善提案）\n\n{raw}",
            outcome="ok",
        )

    from .advice_format import (
        normalize_advice_cardinality,
        parse_advice_json,
        render_advice_markdown,
        validate_advice,
    )

    assert evidence_ctx is not None

    def _try_parse_and_validate(text: str) -> tuple[dict | None, list[str]]:
        try:
            data = parse_advice_json(text)
        except AdviceContractError as e:
            return None, [str(e)]
        data = normalize_advice_cardinality(data, evidence_ctx)
        return data, validate_advice(data, evidence_ctx)

    def _repair_once(source: str, errs: list[str]) -> str:
        print(f"⚠️  出力契約違反を検出、1回だけ修復を試みます: {errs[0]}")
        repair_prompt = _contract_repair_prompt(evidence_ctx, source, errs)
        if redactor:
            repair_prompt = redactor(repair_prompt)
        return generate_text(cfg, system_prompt, repair_prompt)

    data, errors = _try_parse_and_validate(raw)
    repaired = False
    first_errors = list(errors)
    if errors:
        # 形式違反は1回だけ JSON 修復を試みる（失敗後は L2 縮退保存が受ける）
        raw = _repair_once(raw, errors)
        repaired = True
        data, errors = _try_parse_and_validate(raw)
    if errors or data is None:
        raise AdviceContractError(
            "LLMの改善提案が保存条件を満たしませんでした:\n- " + "\n- ".join(errors),
            violations=errors or first_errors,
        )
    try:
        markdown = render_advice_markdown(data, evidence_ctx)
    except AdviceContractError as e:
        # validate 通過後でもレンダ側で意味違反が残るケース → 未修復なら1回だけ再試行
        if repaired:
            raise AdviceContractError(str(e), violations=e.violations or first_errors) from e
        err_list = [line.lstrip("- ").strip() for line in str(e).splitlines() if line.strip()]
        # 先頭の説明行を除き違反リストを渡す
        if err_list and "保存条件" in err_list[0]:
            err_list = err_list[1:] or [str(e)]
        raw = _repair_once(raw, err_list)
        repaired = True
        data, errors = _try_parse_and_validate(raw)
        if errors or data is None:
            raise AdviceContractError(
                "LLMの改善提案が保存条件を満たしませんでした:\n- " + "\n- ".join(errors),
                violations=errors or err_list,
            )
        markdown = render_advice_markdown(data, evidence_ctx)
        return AdviceResult(
            markdown=f"## 🚀 Kaizen（AIからの改善提案）\n\n{markdown}",
            outcome="repaired",
            violations=err_list,
        )
    return AdviceResult(
        markdown=f"## 🚀 Kaizen（AIからの改善提案）\n\n{markdown}",
        outcome="repaired" if repaired else "ok",
        violations=first_errors if repaired else [],
    )
