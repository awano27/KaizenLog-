"""構造化日次提案: parse / validate / render と下流互換。"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advice_format import (
    _assert_render_shape,
    normalize_advice_cardinality,
    parse_advice_json,
    render_advice_markdown,
    validate_advice,
)
from kaizenlog.advisor import AdviceContractError, AdvisorError
from kaizenlog.memory import assign_action_ids
from kaizenlog.verdict import parse_pass_condition
from tests.test_advice_evidence import CURRENT, HISTORY


def _evidence():
    return build_advice_evidence(CURRENT, HISTORY)


def _valid_data(**overrides):
    data = {
        "plan_review": None,
        "proposals": [
            {
                "fact_ids": ["F3"],
                "interpretation": "開始条件を固定して集中枠の再現性を試す",
                "proposal": "始業時に集中枠を予定へ入れる",
                "next_metric": "集中ブロック回数",
            },
            {
                "fact_ids": ["F9"],
                "interpretation": "開発とブラウジングの往来が上位なので確認事項をまとめる",
                "proposal": "調査リンクをまとめて開く",
                "next_metric": "同遷移の回数",
            },
        ],
        "actions": [
            {
                "fact_ids": ["F3"],
                "trigger": "始業の直後",
                "action": "集中枠を予定へ一件入れる",
                "estimated_minutes": 10,
                "pass": "focus_blocks >= 2",
                "fail": "1回以下",
                "mechanism": "枠を先に置くと開始が遅れにくいと考える",
                "falsifier": "focus_blocks が前日より減った場合",
            },
            {
                "fact_ids": ["F9"],
                "trigger": "調査を始める前",
                "action": "リンクを三件まとめる",
                "estimated_minutes": 10,
                "pass": "context_switches <= 40",
                "fail": "41回以上",
                "mechanism": "まとめ開きで切替が減ると考える",
                "falsifier": "context_switches が前日より増えた場合",
            },
        ],
        "ai_review": [
            {
                "fact_ids": ["F5"],
                "text": "会話の往復数は測定不能なので品質の良否は断定しない",
            }
        ],
    }
    data.update(overrides)
    return data


# ---- parse ----

def test_parse_raw_json():
    data = parse_advice_json(json.dumps(_valid_data(), ensure_ascii=False))
    assert len(data["proposals"]) == 2


def test_parse_fenced_and_preamble():
    body = json.dumps(_valid_data(), ensure_ascii=False)
    fenced = f"以下が回答です。\n```json\n{body}\n```\n以上"
    assert parse_advice_json(fenced)["actions"][0]["pass"].startswith("focus")


def test_parse_broken_json():
    with pytest.raises(AdviceContractError):
        parse_advice_json("{not json")


# ---- validate ----

def test_validate_ok():
    assert validate_advice(_valid_data(), _evidence()) == []


def test_validate_count_mismatch():
    data = _valid_data()
    data["actions"] = data["actions"][:1]
    errs = validate_advice(data, _evidence())
    assert any("1対1" in e for e in errs)


def test_normalize_cardinality_trims_ordered_pairs_to_short_day_limit():
    data = _valid_data()
    evidence = replace(_evidence(), max_actions=1)

    normalized = normalize_advice_cardinality(data, evidence)

    assert normalized is not data
    assert normalized["proposals"] == data["proposals"][:1]
    assert normalized["actions"] == data["actions"][:1]
    assert validate_advice(normalized, evidence) == []


def test_normalize_cardinality_keeps_semantic_validation_failures():
    data = _valid_data()
    data["actions"][0]["fact_ids"] = ["F9"]

    normalized = normalize_advice_cardinality(data, _evidence())

    assert len(normalized["proposals"]) == 2
    errs = validate_advice(normalized, _evidence())
    assert any("根拠IDが対応していません" in e for e in errs)


def test_validate_unknown_fact():
    data = _valid_data()
    data["proposals"][0]["fact_ids"] = ["F99"]
    data["actions"][0]["fact_ids"] = ["F99"]
    errs = validate_advice(data, _evidence())
    assert any("存在しない" in e for e in errs)


def test_validate_missing_f4_f5():
    data = _valid_data()
    data["ai_review"] = [{"fact_ids": ["F1"], "text": "画面の話だけ"}]
    errs = validate_advice(data, _evidence())
    assert any("F4" in e or "F5" in e for e in errs)


def test_validate_unknown_machine_metric():
    data = _valid_data()
    data["actions"][0]["pass"] = "pomodoro_count <= 4"
    errs = validate_advice(data, _evidence())
    assert any("機械構文" in e or "指標名" in e for e in errs)


def test_validate_interpretation_digits():
    data = _valid_data()
    data["proposals"][0]["interpretation"] = "集中ブロックは47回ある"
    errs = validate_advice(data, _evidence())
    assert any("観測数値" in e for e in errs)


def test_validate_action_newline():
    data = _valid_data()
    data["actions"][0]["action"] = "行1\n行2"
    errs = validate_advice(data, _evidence())
    assert any("改行" in e for e in errs)


def test_validate_kzn_forbidden():
    data = _valid_data()
    data["actions"][0]["action"] = "KZN-20260721-001 を続ける"
    errs = validate_advice(data, _evidence())
    assert any("KZN" in e for e in errs)


def test_validate_f4_only_session_claim():
    """F4 のみで会話/セッション断定 → JSON 層で意味違反（F2）。"""
    data = _valid_data()
    data["ai_review"] = [
        {"fact_ids": ["F4"], "text": "セッションが細切れになっている"}
    ]
    errs = validate_advice(data, _evidence())
    assert any("会話数・セッション数・往復数へ変換" in e for e in errs)


def test_validate_fact_ids_not_shared_between_pair():
    """件数は一致するが proposals[i] と actions[i] が根拠IDを共有しない（F7）。"""
    data = _valid_data()
    data["actions"][0]["fact_ids"] = ["F9"]  # proposals[0] は F3 のみ
    errs = validate_advice(data, _evidence())
    assert any("根拠IDが対応していません" in e for e in errs)


# ---- D1 shape assert --------------------------------------------------------

def test_d1_assert_render_shape_raises_on_missing_heading():
    with pytest.raises(AdvisorError, match="renderer bug"):
        _assert_render_shape(
            "### 明日の最小アクション\n- [ ] x｜PASS: a >= 1｜FAIL: 0\n",
            n_actions=1,
        )


def test_d1_assert_render_shape_raises_on_checkbox_mismatch():
    md = (
        "### 今日の改善提案\n1. a。b。翌日見る指標: c\n\n"
        "### 明日の最小アクション\n"
        "- [ ] a｜PASS: focus_blocks >= 1｜FAIL: 0\n"
        "- [ ] b｜PASS: focus_blocks >= 1｜FAIL: 0\n\n"
        "### AI作業の改善\n- ok\n"
    )
    with pytest.raises(AdvisorError, match="renderer bug.*checkbox"):
        _assert_render_shape(md, n_actions=1)


def test_d1_broken_render_raises_advisor_error(monkeypatch):
    """render 後の形状破壊で AdvisorError が送出される（D1 安全網）。"""
    data = _valid_data()
    evidence = _evidence()
    import kaizenlog.advice_format as af

    monkeypatch.setattr(af, "validate_advice", lambda d, e: [])
    real_assert = af._assert_render_shape

    def boom(md, *, n_actions, **_kwargs):
        raise AdvisorError("renderer bug: forced")

    monkeypatch.setattr(af, "_assert_render_shape", boom)
    with pytest.raises(AdvisorError, match="renderer bug"):
        af.render_advice_markdown(data, evidence)
    monkeypatch.setattr(af, "_assert_render_shape", real_assert)


def test_render_semantic_stays_contract_error():
    """意味違反は validate で AdviceContractError（renderer bug にしない）。"""
    data = _valid_data()
    data["ai_review"] = [
        {"fact_ids": ["F4"], "text": "セッションが細切れになっている"}
    ]
    with pytest.raises(AdviceContractError):
        render_advice_markdown(data, _evidence())


def test_generate_advice_repairs_semantic_via_json(monkeypatch):
    """意味違反 JSON が修復プロンプト経由で直る。"""
    from kaizenlog.advisor import generate_advice
    from kaizenlog.config import LLMConfig
    from tests.test_advice_evidence import VALID_ADVICE_JSON

    bad = json.loads(VALID_ADVICE_JSON)
    bad["ai_review"] = [
        {"fact_ids": ["F4"], "text": "セッションが細切れになっている"}
    ]
    replies = iter([json.dumps(bad, ensure_ascii=False), VALID_ADVICE_JSON])
    calls = []

    def fake_generate(cfg, system, user):
        calls.append(user)
        return next(replies)

    monkeypatch.setattr("kaizenlog.advisor.generate_text", fake_generate)
    result = generate_advice(
        LLMConfig(), "ログ", [], evidence=_evidence()
    )
    assert len(calls) == 2
    assert "会話数・セッション数・往復数へ変換" in calls[1] or "違反" in calls[1]
    assert "### 明日の最小アクション" in result.markdown


# ---- render + roundtrip ----

def test_render_golden():
    data = {
        "plan_review": None,
        "proposals": [
            {
                "fact_ids": ["F3"],
                "interpretation": "解釈文",
                "proposal": "提案文",
                "next_metric": "指標名",
            }
        ],
        "actions": [
            {
                "fact_ids": ["F3"],
                "trigger": "朝いちばんに",
                "action": "行動する",
                "estimated_minutes": 10,
                "pass": "focus_blocks >= 1",
                "fail": "0回",
                "mechanism": "朝の着手を早くすると枠が取れると考える",
                "falsifier": "focus_blocks がゼロの日が続いた場合",
            }
        ],
        "ai_review": [
            {"fact_ids": ["F5"], "text": "測定不能なので断定しない"}
        ],
    }
    # 洞察候補はゴールデンのコア形状検査と分離（候補0で節なし）
    md = render_advice_markdown(
        data, replace(_evidence(), insight_candidates=())
    )
    expected = (
        "### 今日の改善提案\n"
        "1. 解釈文。提案文。翌日見る指標: 指標名\n"
        "\n"
        "### 明日の最小アクション\n"
        "- [ ] 朝いちばんに→行動する（目安10分）｜PASS: focus_blocks >= 1（集中ブロック数）｜FAIL: 0回\n"
        "    - なぜ効くと考えるか: 朝の着手を早くすると枠が取れると考える\n"
        "    - 効かなかったと分かる条件: focus_blocks がゼロの日が続いた場合\n"
        "\n"
        "### AI作業の改善\n"
        "- 測定不能なので断定しない\n"
    )
    assert md == expected


def test_roundtrip_contract_ids_and_pass_parse():
    data = _valid_data()
    evidence = _evidence()
    md = render_advice_markdown(data, evidence)
    full = f"## 🚀 Kaizen（AIからの改善提案）\n\n{md}"
    with_ids, entries = assign_action_ids(full, date(2026, 7, 21), [])
    assert len(entries) == 2
    assert "KZN-20260721-001" in with_ids
    line = [ln for ln in with_ids.splitlines() if "KZN-20260721-002" in ln][0]
    assert parse_pass_condition(line) == ("context_switches", "<=", 40.0)
