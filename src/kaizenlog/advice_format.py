"""日次改善提案の構造化出力: JSON 検証 → 決定論的 Markdown レンダリング。

LLM は JSON だけを返し、見出し・チェックボックス・PASS/FAIL 行は
KaizenLog が組み立てる。下流の KZN 付与・verdict 解析と完全互換な
Markdown を出力する。
"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from .advice_evidence import AdviceEvidence
from .verdict import is_known_metric, looks_like_machine_pass

# advisor とは循環 import になるため、AdviceContractError / 契約検証は関数内で遅延 import

_FACT_TOKEN_RE = re.compile(r"^\[?F(\d+)\]?$")
_NEWLINE_RE = re.compile(r"[\r\n]")
_KZN_RE = re.compile(r"KZN-\d{8}")
_DIGIT_RE = re.compile(r"\d")


def _contract_error(msg: str):
    from .advisor import AdviceContractError

    return AdviceContractError(msg)


def parse_advice_json(text: str) -> dict:
    """LLM 応答から JSON オブジェクトを取り出す。

    ```json フェンスや前後の説明文があっても、最初の { から対応する }
    までを抽出する。失敗時は AdviceContractError。
    """
    if not text or not str(text).strip():
        raise _contract_error("日次提案の JSON が空です")
    raw = str(text).strip()
    start = raw.find("{")
    if start < 0:
        raise _contract_error("日次提案の JSON オブジェクトが見つかりません")
    # 対応する閉じ括弧を深さ数えで探す
    depth = 0
    end = -1
    in_str = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        raise _contract_error("日次提案の JSON が閉じていません")
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as e:
        raise _contract_error(f"日次提案の JSON を解析できません: {e}") from e
    if not isinstance(data, dict):
        raise _contract_error("日次提案のトップレベルは JSON オブジェクトである必要があります")
    return data


def normalize_advice_cardinality(data: dict, evidence: AdviceEvidence) -> dict:
    """順序を保ったまま提案とアクションを安全な同数へ切り詰める。

    内容や根拠IDは変更せず、後段の validate_advice が全契約を再検証する。
    空配列や配列以外は正規化せず、従来どおり検証エラーに委ねる。
    """
    normalized = deepcopy(data)
    proposals = normalized.get("proposals")
    actions = normalized.get("actions")
    if not isinstance(proposals, list) or not proposals:
        return normalized
    if not isinstance(actions, list) or not actions:
        return normalized
    pair_count = min(len(proposals), len(actions), evidence.max_actions)
    normalized["proposals"] = proposals[:pair_count]
    normalized["actions"] = actions[:pair_count]
    return normalized


def _norm_fact_id(token: str) -> str | None:
    m = _FACT_TOKEN_RE.match(str(token).strip())
    if not m:
        return None
    return f"[F{m.group(1)}]"


def _as_str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise _contract_error(f"{field} は非空の配列である必要があります")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _contract_error(f"{field} の要素は非空文字列である必要があります")
        out.append(item.strip())
    return out


def _require_single_line(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _contract_error(f"{field} は非空文字列である必要があります")
    if _NEWLINE_RE.search(value):
        raise _contract_error(f"{field} に改行を含めないでください")
    return value.strip()


def _check_no_kzn_or_marker(text: str, field: str) -> None:
    if _KZN_RE.search(text):
        raise _contract_error(f"{field} にモデル生成のKZN IDを含めないでください")
    if "<!--" in text:
        raise _contract_error(f"{field} にマーカー文字列を含めないでください")


def _is_measurable(value: str) -> bool:
    return bool(re.search(r"\d", value) or re.search(
        r"(?:前日|前回|基準).{0,12}(?:比|より|同数|同水準|同じ)"
        r"|(?:同数|同水準|同数値|増加|減少|上昇|低下|変化なし)",
        value,
    ))


def validate_advice(data: dict, evidence: AdviceEvidence) -> list[str]:
    """構造検証。違反メッセージのリスト（空なら合格）。"""
    errors: list[str] = []
    try:
        _validate_advice_raise(data, evidence)
    except Exception as e:
        from .advisor import AdviceContractError

        if isinstance(e, AdviceContractError):
            errors.append(str(e))
        else:
            errors.append(str(e))
        return errors

    # 意味検証: 結合テキスト + fact_ids 文脈付き per-item ガード
    from .advisor import _semantic_contract_errors

    joined = _join_text_fields(data)
    errors.extend(_semantic_contract_errors(joined, evidence))
    errors.extend(_fact_context_semantic_errors(data, evidence))
    return errors


def _join_text_fields(data: dict) -> str:
    parts: list[str] = []
    pr = data.get("plan_review")
    if isinstance(pr, str):
        parts.append(pr)
    for key in ("proposals", "actions", "ai_review"):
        items = data.get(key) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for k, v in item.items():
                if k == "fact_ids":
                    continue
                if isinstance(v, str):
                    parts.append(v)
    return "\n".join(parts)


def _item_fact_ids(item: dict) -> set[str]:
    raw = item.get("fact_ids") or []
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for t in raw:
        n = _norm_fact_id(t) if isinstance(t, str) else None
        if n:
            out.add(n)
    return out


def _item_text(item: dict) -> str:
    parts: list[str] = []
    for k, v in item.items():
        if k == "fact_ids":
            continue
        if isinstance(v, str):
            parts.append(v)
    return "。".join(parts)


def _fact_context_semantic_errors(
    data: dict, evidence: AdviceEvidence
) -> list[str]:
    """fact_ids を文脈として使う意味ガード（JSON 層で F4/F1 系を捕捉）。

    _join_text_fields は fact_ids を落とすため、Markdown 向けの文中 [F#]
    前提ガードが JSON 経路で効かない。item 単位で同じ禁止推論を判定する。
    """
    from .advisor import _has_uncertainty_language, _observed_clause

    errors: list[str] = []
    conversion = re.compile(r"会話|セッション|往復")
    unsupported_cause = re.compile(
        r"(?:通知|割り込み|中断|(?:生産性|集中力).{0,6}(?:低下|下が|悪化))"
    )

    def is_measurement_instruction(clause: str) -> bool:
        return (
            bool(re.search(r"記録|計測|測定|確認|設定|目標|条件", clause))
            and not bool(re.search(r"した|だった|発生した|実績", clause))
        )

    for key in ("proposals", "actions", "ai_review"):
        items = data.get(key) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            facts = _item_fact_ids(item)
            text = _item_text(item)
            if not text.strip():
                continue
            for sentence in re.split(r"[。\n]", text):
                clause = _observed_clause(sentence.strip())
                if not clause:
                    continue
                if _has_uncertainty_language(clause) or is_measurement_instruction(clause):
                    continue
                if (
                    "[F4]" in facts
                    and "[F5]" not in facts
                    and conversion.search(clause)
                ):
                    msg = "AI関連画面ブロック数を会話数・セッション数・往復数へ変換しています"
                    if msg not in errors:
                        errors.append(msg)
                if (
                    facts & {"[F1]", "[F8]", "[F9]"}
                    and "[F5]" not in facts
                    and unsupported_cause.search(clause)
                ):
                    msg = "カテゴリ変更回数を通知・割り込み・生産性低下へ変換しています"
                    if msg not in errors:
                        errors.append(msg)
    return errors


def _validate_advice_raise(data: dict, evidence: AdviceEvidence) -> None:
    if not isinstance(data, dict):
        raise _contract_error("日次提案は JSON オブジェクトである必要があります")

    proposals = data.get("proposals")
    actions = data.get("actions")
    ai_review = data.get("ai_review")
    if not isinstance(proposals, list) or not isinstance(actions, list):
        raise _contract_error("proposals と actions は配列である必要があります")
    if not isinstance(ai_review, list):
        raise _contract_error("ai_review は配列である必要があります")

    if not 1 <= len(proposals) <= 3:
        raise _contract_error("proposals は1〜3件にしてください")
    if not 1 <= len(actions) <= 3:
        raise _contract_error("actions は1〜3件にしてください")
    if len(proposals) != len(actions):
        raise _contract_error("proposals と actions の件数を1対1にしてください")
    if len(proposals) > evidence.max_actions:
        raise _contract_error(
            f"当日のデータ量では改善アクションは最大{evidence.max_actions}件にしてください"
        )
    if not 1 <= len(ai_review) <= 2:
        raise _contract_error("ai_review は1〜2件にしてください")

    plan = data.get("plan_review")
    if plan is not None:
        if not isinstance(plan, str) or not plan.strip():
            raise _contract_error("plan_review は null か非空文字列にしてください")
        lines = [ln for ln in plan.splitlines() if ln.strip()]
        if len(lines) > 3:
            raise _contract_error("plan_review は1〜3行にしてください")
        _check_no_kzn_or_marker(plan, "plan_review")
        # 見出し・チェックボックス・フェンスは Markdown 契約を壊す（JSON 層で拒否）
        for ln in plan.splitlines():
            s = ln.strip()
            if s.startswith("#"):
                raise _contract_error("plan_review に Markdown 見出しを含めないでください")
            if s.startswith("- ["):
                raise _contract_error(
                    "plan_review にチェックボックス行を含めないでください"
                )
            if s.startswith("```") or s.startswith("~~~"):
                raise _contract_error("plan_review にコードフェンスを含めないでください")

    available = set(evidence.fact_ids)

    def parse_facts(raw: Any, field: str) -> list[str]:
        tokens = _as_str_list(raw, field)
        facts: list[str] = []
        for t in tokens:
            n = _norm_fact_id(t)
            if n is None:
                raise _contract_error(f"{field} の根拠IDが不正です: {t!r}")
            facts.append(n)
        if not facts:
            raise _contract_error(f"{field} に根拠IDがありません")
        if available and not set(facts) <= available:
            raise _contract_error(f"{field} が存在しない根拠IDを参照しています")
        return facts

    prop_facts_list: list[list[str]] = []
    for i, item in enumerate(proposals, 1):
        if not isinstance(item, dict):
            raise _contract_error(f"proposals[{i}] はオブジェクトである必要があります")
        facts = parse_facts(item.get("fact_ids"), f"proposals[{i}].fact_ids")
        prop_facts_list.append(facts)
        for key in ("interpretation", "proposal", "next_metric"):
            val = _require_single_line(item.get(key), f"proposals[{i}].{key}")
            _check_no_kzn_or_marker(val, f"proposals[{i}].{key}")
        interp = item["interpretation"].strip()
        if _DIGIT_RE.search(interp):
            raise _contract_error(
                f"proposals[{i}].interpretation に観測数値を書かないでください"
            )

    for i, item in enumerate(actions, 1):
        if not isinstance(item, dict):
            raise _contract_error(f"actions[{i}] はオブジェクトである必要があります")
        facts = parse_facts(item.get("fact_ids"), f"actions[{i}].fact_ids")
        if set(facts) & set(prop_facts_list[i - 1]) == set():
            raise _contract_error(
                f"actions[{i}] と proposals[{i}] の根拠IDが対応していません"
            )
        action = _require_single_line(item.get("action"), f"actions[{i}].action")
        # if-then 実行意図: 実在の日課・時刻をアンカーにした短い合図（目安15字）
        trigger = _require_single_line(item.get("trigger"), f"actions[{i}].trigger")
        if len(trigger) > 40:
            raise _contract_error(
                f"actions[{i}].trigger は短くしてください（目安15字、最大40字）"
            )
        _check_no_kzn_or_marker(trigger, f"actions[{i}].trigger")
        pass_v = _require_single_line(item.get("pass"), f"actions[{i}].pass")
        fail_v = _require_single_line(item.get("fail"), f"actions[{i}].fail")
        _check_no_kzn_or_marker(action, f"actions[{i}].action")
        _check_no_kzn_or_marker(pass_v, f"actions[{i}].pass")
        _check_no_kzn_or_marker(fail_v, f"actions[{i}].fail")
        if not _is_measurable(pass_v) or not _is_measurable(fail_v):
            raise _contract_error(
                f"actions[{i}] の pass/fail は数値条件にしてください"
            )
        from .verdict import strip_pass_annotation

        pass_core = strip_pass_annotation(pass_v)
        if looks_like_machine_pass(pass_core):
            m = re.match(r"^(\S+)\s*(?:<=|>=|<|>|==?)", pass_core.strip())
            metric = m.group(1) if m else pass_core.split()[0]
            if not is_known_metric(metric):
                raise _contract_error(
                    f"actions[{i}] の pass: 指標名が使用可能な指標にありません"
                )
            # 実在検証: 未知カテゴリの偽PASS・未観測ドメイン・計測不能指標の入口ガード
            # （計測不能な PASS を保存すると compute_metric=None で永久未判定になる）
            _INPUT_PASS_METRICS = frozenset(
                {"focus_blocks", "focus_minutes", "input_keypresses"}
            )
            _STRUCTURED_AI_PASS_METRICS = frozenset(
                {
                    "ai_cc_sessions",
                    "ai_fragmented_sessions",
                    "ai_retry_chains",
                    "ai_tool_errors",
                    "ai_interruptions",
                    "ai_avg_turns",
                    "ai_output_tokens",
                }
            )
            if metric in _INPUT_PASS_METRICS and not evidence.input_metrics_available:
                raise _contract_error(
                    f"actions[{i}] の pass: {metric} は入力watcherが無いため計測不能です"
                )
            if (
                metric in _STRUCTURED_AI_PASS_METRICS
                and not evidence.structured_ai_metrics_available
            ):
                raise _contract_error(
                    f"actions[{i}] の pass: {metric} は構造化AIテレメトリが無いため計測不能です"
                )
            if metric.startswith("category_minutes:"):
                cat = metric.split(":", 1)[1].strip()
                known = evidence.known_categories
                if known is not None and cat not in known:
                    raise _contract_error(
                        f"actions[{i}] の pass: カテゴリ {cat!r} は設定に存在しません"
                    )
            if metric.startswith("site_minutes:"):
                if not evidence.site_metrics_available:
                    raise _contract_error(
                        f"actions[{i}] の pass: site_minutes はブラウザwatcher統計が無いため計測不能です"
                    )
                site = metric.split(":", 1)[1].strip().lower()
                # 提案日当日に観測されたドメインのみ（0分になった既知サイトの後日判定は
                # 判定側の話。入口では「当日観測」を要求する）
                sites = evidence.observed_sites
                if sites is not None and site not in sites:
                    raise _contract_error(
                        f"actions[{i}] の pass: サイト {site!r} は当日観測されていません"
                    )
        # evidence ゲート付き内容チェック（注記付与前の生フィールド）
        from .advisor import evidence_gated_action_errors

        scan = f"{action} {pass_core} {fail_v}"
        for msg in evidence_gated_action_errors(scan, i, evidence):
            raise _contract_error(msg)

    ai_facts_all: set[str] = set()
    for i, item in enumerate(ai_review, 1):
        if not isinstance(item, dict):
            raise _contract_error(f"ai_review[{i}] はオブジェクトである必要があります")
        facts = parse_facts(item.get("fact_ids"), f"ai_review[{i}].fact_ids")
        ai_facts_all.update(facts)
        text = _require_single_line(item.get("text"), f"ai_review[{i}].text")
        _check_no_kzn_or_marker(text, f"ai_review[{i}].text")
        if _DIGIT_RE.search(text):
            raise _contract_error(
                f"ai_review[{i}].text に観測数値を書かないでください"
            )

    if available:
        needed = {"[F4]", "[F5]"} & available
        if needed and not (ai_facts_all & {"[F4]", "[F5]"}):
            raise _contract_error(
                "ai_review に AI根拠ID F4 または F5 がありません"
            )


def render_advice_markdown(data: dict, evidence: AdviceEvidence) -> str:
    """検証済み JSON を現行契約互換の Markdown にレンダリングする。"""
    from .advisor import (
        AdviceContractError,
        AdvisorError,
        _semantic_contract_errors,
        advice_contract_errors,
    )

    # 念のため再検証（呼び出し側が validate 済みでも壊れを防ぐ）
    errs = validate_advice(data, evidence)
    if errs:
        # validate 失敗は LLM 契約違反（レンダラバグではない）
        raise AdviceContractError(
            "LLMの改善提案が保存条件を満たしませんでした:\n- " + "\n- ".join(errs)
        )

    lines: list[str] = []
    plan = data.get("plan_review")
    if isinstance(plan, str) and plan.strip():
        lines.append("### 計画と実績")
        lines.append(plan.strip())
        lines.append("")

    # 表示から F-ID を外す（検証は JSON 層で完了。保存文は読者向けに）
    from .experiments import metric_display_label
    from .verdict import looks_like_machine_pass, strip_pass_annotation

    lines.append("### 今日の改善提案")
    for i, item in enumerate(data["proposals"], 1):
        lines.append(
            f"{i}. {item['interpretation'].strip()}。"
            f"{item['proposal'].strip()}。"
            f"翌日見る指標: {item['next_metric'].strip()}"
        )
    lines.append("")

    lines.append("### 明日の最小アクション")
    for item in data["actions"]:
        # LLM が自前注記を付けていても1段に正規化してからラベルを付ける
        core = strip_pass_annotation(item["pass"].strip())
        note = ""
        if looks_like_machine_pass(core):
            m = re.match(r"^(\S+)\s*(?:<=|>=|<|>|==?)", core)
            if m:
                label = metric_display_label(m.group(1))
                if label:
                    note = f"（{label}）"
        trigger = str(item.get("trigger") or "").strip()
        body = (
            f"{trigger}→{item['action'].strip()}"
            if trigger
            else item["action"].strip()
        )
        lines.append(
            f"- [ ] {body}"
            f"｜PASS: {core}{note}｜FAIL: {item['fail'].strip()}"
        )
    lines.append("")

    lines.append("### AI作業の改善")
    for item in data["ai_review"]:
        lines.append(f"- {item['text'].strip()}")

    rendered = "\n".join(lines).rstrip() + "\n"

    # レンダラのインバリアント: 旧 Markdown 契約を満たすこと。
    # 意味違反・evidence ゲート付き内容チェック → AdviceContractError（修復・L2 縮退へ）。
    # 構造エラーが混じる場合だけ renderer bug。
    from .advisor import collect_evidence_gated_errors

    contract_errs = advice_contract_errors(rendered, evidence)
    if contract_errs:
        semantic_errs = set(_semantic_contract_errors(rendered, evidence))
        semantic_errs.update(collect_evidence_gated_errors(rendered, evidence))
        if set(contract_errs) <= semantic_errs:
            raise AdviceContractError(
                "LLMの改善提案が保存条件を満たしませんでした:\n- "
                + "\n- ".join(contract_errs)
            )
        raise AdvisorError("renderer bug: " + "; ".join(contract_errs))
    return rendered
