"""日次改善提案の構造化出力: JSON 検証 → 決定論的 Markdown レンダリング。

本番経路（一本線）:
  1. JSON 契約検証（validate_advice）— LLM 出力の意味・件数・PASS 機械構文
  2. 決定論レンダ（render_advice_markdown）
  3. 形状検査（_assert_render_shape）— レンダラ bug のみ検知

旧 Markdown 契約は廃止済み。LLM 契約と
レンダラ形状を二重に検査しない。
"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from .advice_evidence import AdviceEvidence
from .verdict import is_known_metric, looks_like_machine_pass, parse_pass_condition

# advisor とは循環 import になるため、AdviceContractError / 契約検証は関数内で遅延 import

_FACT_TOKEN_RE = re.compile(r"^\[?F(\d+)\]?$")
_NEWLINE_RE = re.compile(r"[\r\n]")
_KZN_RE = re.compile(r"KZN-\d{8}")
_DIGIT_RE = re.compile(r"\d")


def _contract_error(msg: str):
    from .advisor import AdviceContractError

    return AdviceContractError(msg)


# 履歴中央値に対し、減らす目標は5%以上、増やす目標は5%以上の改善を求める。
_MEDIAN_CHALLENGE_LE = 0.95
_MEDIAN_CHALLENGE_GE = 1.05
_PASS_OP_RE = re.compile(r"^(\S+)\s*(<=|>=|<|>|==?)\s*([\d.]+)\s*$")


def _pass_challenge_error(
    metric: str,
    pass_core: str,
    index: int,
    baselines: object,
) -> str | None:
    """非挑戦的 PASS 閾値ならエラー文、検査不能・合格なら None。"""
    if not isinstance(baselines, dict) and not (
        hasattr(baselines, "get") and baselines is not None
    ):
        return None
    try:
        bl_map = baselines  # type: ignore[assignment]
        baseline = bl_map.get(metric)  # type: ignore[union-attr]
    except Exception:
        return None
    if baseline is None:
        # ベースライン無しは検査しない（初日や新指標を弾かない）
        return None
    try:
        bl = float(baseline)
    except (TypeError, ValueError):
        return None
    if not (bl > 0):
        # 比率判定不能（0 日は挑戦性を測れない）
        return None
    m = _PASS_OP_RE.match(pass_core.strip())
    if not m:
        return None
    op = m.group(2)
    try:
        target = float(m.group(3))
    except ValueError:
        return None
    if op in ("<=", "<"):
        # 履歴中央値より5%以上低い目標だけを許可する。
        if target > bl * _MEDIAN_CHALLENGE_LE:
            return (
                f"actions[{index}] の pass: {metric} {op} {target:g} は"
                f"ベースライン {bl:g} より緩すぎます"
                f"（上限の目安は {bl * _MEDIAN_CHALLENGE_LE:g} 以下）"
            )
    elif op in (">=", ">"):
        if target < bl * _MEDIAN_CHALLENGE_GE:
            return (
                f"actions[{index}] の pass: {metric} {op} {target:g} は"
                f"ベースライン {bl:g} より緩すぎます"
                f"（下限の目安は {bl * _MEDIAN_CHALLENGE_GE:g} 以上）"
            )
    return None


def _pass_range_error(
    metric: str,
    pass_core: str,
    index: int,
    history_values: object,
) -> str | None:
    """実測ヒストリに対して桁違い・レンジ外の PASS 目標を拒否する。"""
    if history_values is None:
        return None
    try:
        vals_raw = history_values.get(metric)  # type: ignore[union-attr]
    except Exception:
        return None
    if not vals_raw:
        return None
    try:
        vals = [float(v) for v in vals_raw]
    except (TypeError, ValueError):
        return None
    if len(vals) < 3:
        return None
    m = _PASS_OP_RE.match(pass_core.strip())
    if not m:
        return None
    op = m.group(2)
    try:
        target = float(m.group(3))
    except ValueError:
        return None
    lo, hi = min(vals), max(vals)
    # 減少指標: 目標が観測最小の半分未満なら桁違い（40 vs 200〜400）
    if op in ("<=", "<"):
        if hi > 0 and target < lo * 0.5 and target < hi * 0.25:
            return (
                f"actions[{index}] の pass: {metric} {op} {target:g} は"
                f"直近実測 {lo:g}〜{hi:g} から見て厳しすぎます"
                f"（実測分布内・下位25%付近を使ってください）"
            )
    elif op in (">=", ">"):
        if lo > 0 and target > hi * 2 and target > lo * 2:
            return (
                f"actions[{index}] の pass: {metric} {op} {target:g} は"
                f"直近実測 {lo:g}〜{hi:g} から見て非現実的です"
            )
    return None


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
    max_actions==0 のときは空配列に切り詰める（同時アクティブ上限到達時）。
    """
    normalized = deepcopy(data)
    proposals = normalized.get("proposals")
    actions = normalized.get("actions")
    if evidence.max_actions <= 0:
        if isinstance(proposals, list):
            normalized["proposals"] = []
        if isinstance(actions, list):
            normalized["actions"] = []
        return normalized
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

    if evidence.max_actions <= 0:
        # 同時アクティブ上限などで新規提案枠が無い日
        if len(proposals) != 0 or len(actions) != 0:
            raise _contract_error(
                "未完了の提案が上限に達しているため新規 actions/proposals は0件にしてください"
            )
    else:
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
        estimated_minutes = item.get("estimated_minutes")
        if (
            isinstance(estimated_minutes, bool)
            or not isinstance(estimated_minutes, int)
            or not 5 <= estimated_minutes <= 15
        ):
            raise _contract_error(
                f"actions[{i}].estimated_minutes は5〜15の整数にしてください"
            )
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
        # §C1: mechanism / falsifier 必須（最大50字・改行禁止）。数字は mechanism のみ禁止。
        mechanism = _require_single_line(
            item.get("mechanism"), f"actions[{i}].mechanism"
        )
        falsifier = _require_single_line(
            item.get("falsifier"), f"actions[{i}].falsifier"
        )
        if len(mechanism) > 50:
            raise _contract_error(
                f"actions[{i}].mechanism は50字以内にしてください"
            )
        if len(falsifier) > 50:
            raise _contract_error(
                f"actions[{i}].falsifier は50字以内にしてください"
            )
        _check_no_kzn_or_marker(mechanism, f"actions[{i}].mechanism")
        _check_no_kzn_or_marker(falsifier, f"actions[{i}].falsifier")
        if _DIGIT_RE.search(mechanism):
            raise _contract_error(
                f"actions[{i}].mechanism に観測数値を書かないでください"
            )
        if not _is_measurable(pass_v) or not _is_measurable(fail_v):
            raise _contract_error(
                f"actions[{i}] の pass/fail は数値条件にしてください"
            )
        from .verdict import strip_pass_annotation

        pass_core = strip_pass_annotation(pass_v)
        fail_core = strip_pass_annotation(fail_v)
        # 自由文PASSは実在・計測可否・挑戦性ガードを全迂回するため禁止。
        # parse_pass_condition が通る機械構文のみ受理し、以降のガードを必ず適用する。
        parsed = parse_pass_condition(f"x｜PASS: {pass_core}｜FAIL: 0")
        if parsed is None:
            raise _contract_error(
                f"actions[{i}] の pass は機械構文（指標 演算子 数値）にしてください"
                f"（例: ai_tool_errors <= 60）。自由文は自動判定できず契約違反です"
            )
        # FAIL も機械構文なら既知指標であることを要求（自由文＋数値は可）
        if looks_like_machine_pass(fail_core):
            fail_parsed = parse_pass_condition(f"x｜PASS: {fail_core}｜FAIL: 0")
            if fail_parsed is None:
                raise _contract_error(
                    f"actions[{i}] の fail は機械構文として解析できません"
                    f"（未知指標または形式不正）"
                )
        metric, _op, _target = parsed
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
                "ai_tool_errors_per_session",
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
        # 挑戦性: ベースラインより大幅に緩い閾値を拒否（空虚PASS防止）
        # ベースライン未取得の指標は検査しない（初日・新指標を弾かない）
        challenge_err = _pass_challenge_error(
            metric, pass_core, i, evidence.metric_baselines
        )
        if challenge_err:
            raise _contract_error(challenge_err)
        # §E: 実測分布外の桁違い目標を拒否（例: 実測200〜400なのに <=40）
        range_err = _pass_range_error(
            metric, pass_core, i, evidence.metric_history_values
        )
        if range_err:
            raise _contract_error(range_err)
        if evidence.suppressed_metrics and metric in evidence.suppressed_metrics:
            raise _contract_error(
                f"actions[{i}] の pass: 指標 {metric} はチェックなし達成の履歴があり"
                f"新規提案を抑制しています"
            )
        # evidence ゲート付き内容チェック（注記付与前の生フィールド）
        from .advisor import evidence_gated_action_errors

        scan = f"{action} {pass_core} {fail_v}"
        for msg in evidence_gated_action_errors(scan, i, evidence):
            raise _contract_error(msg)

    if evidence.max_actions <= 0:
        # 新規 actions が無い日は ai_review のみでも可
        ai_facts_all: set[str] = set()
        for i, item in enumerate(ai_review, 1):
            if not isinstance(item, dict):
                raise _contract_error(
                    f"ai_review[{i}] はオブジェクトである必要があります"
                )
            facts = parse_facts(item.get("fact_ids"), f"ai_review[{i}].fact_ids")
            ai_facts_all.update(facts)
            text = _require_single_line(item.get("text"), f"ai_review[{i}].text")
            _check_no_kzn_or_marker(text, f"ai_review[{i}].text")
        return

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


def _assert_render_shape(markdown: str, *, n_actions: int) -> None:
    """レンダラ出力の形状のみ検査（LLM 契約は validate_advice の責務）。

    memory.assign_action_ids / verdict はチェックボックスと見出し形状に依存する。
    契約の意味検査は JSON 層に一本化し、ここは形状破壊だけを renderer bug とする。
    """
    from .advisor import AdvisorError

    if "<!-- kaizenlog:" in markdown:
        raise AdvisorError("renderer bug: kaizenlog marker leaked into advice")
    if re.search(r"\[F\d+", markdown):
        raise AdvisorError("renderer bug: fact id [F leaked into advice")
    if re.search(r"KZN-\d{8}-\d+", markdown):
        raise AdvisorError("renderer bug: KZN id leaked into advice (pre-assign)")
    # 内部レンダ見出し（render_reader_advice 変換前）。下流 ID 付与は
    # LEGACY_ACTION_SECTION「明日の最小アクション」を含むこの形を起点にする。
    for heading in ("今日の改善提案", "明日の最小アクション", "AI作業の改善"):
        if f"### {heading}" not in markdown:
            raise AdvisorError(f"renderer bug: missing heading ### {heading}")
    checkboxes = re.findall(r"^- \[ \] ", markdown, re.MULTILINE)
    if len(checkboxes) != n_actions:
        raise AdvisorError(
            f"renderer bug: checkbox count {len(checkboxes)} != actions {n_actions}"
        )


def render_advice_markdown(data: dict, evidence: AdviceEvidence) -> str:
    """検証済み JSON を現行契約互換の Markdown にレンダリングする。"""
    from .advisor import AdviceContractError

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
        body += f"（目安{item['estimated_minutes']}分）"
        lines.append(
            f"- [ ] {body}"
            f"｜PASS: {core}{note}｜FAIL: {item['fail'].strip()}"
        )
        # 内部専用の対応付け: 素の箇条書きで運ぶ（- [ ] 禁止＝assign_action_ids 汚染防止）
        mechanism = str(item.get("mechanism") or "").strip()
        falsifier = str(item.get("falsifier") or "").strip()
        if mechanism:
            lines.append(f"    - なぜ効くと考えるか: {mechanism}")
        if falsifier:
            lines.append(f"    - 効かなかったと分かる条件: {falsifier}")
    lines.append("")

    lines.append("### AI作業の改善")
    for item in data["ai_review"]:
        lines.append(f"- {item['text'].strip()}")

    rendered = "\n".join(lines).rstrip() + "\n"
    n_actions = len(data["actions"]) if isinstance(data.get("actions"), list) else 0
    _assert_render_shape(rendered, n_actions=n_actions)
    return rendered
