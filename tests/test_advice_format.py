"""構造化日次提案: parse / validate / render と下流互換。"""
from __future__ import annotations

import json

import pytest

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advice_format import (
    parse_advice_json,
    render_advice_markdown,
    validate_advice,
)
from kaizenlog.advisor import AdviceContractError, AdvisorError, advice_contract_errors
from kaizenlog.memory import assign_action_ids
from kaizenlog.verdict import parse_pass_condition
from tests.test_advice_evidence import CURRENT, HISTORY

from datetime import date


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
                "action": "始業時に集中枠を予定へ一件入れる",
                "pass": "focus_blocks >= 2",
                "fail": "1回以下",
            },
            {
                "fact_ids": ["F9"],
                "action": "調査リンクを開く前に三件まとめる",
                "pass": "context_switches <= 40",
                "fail": "41回以上",
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
    assert any("指標名" in e for e in errs)


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
                "action": "行動する",
                "pass": "focus_blocks >= 1",
                "fail": "0回",
            }
        ],
        "ai_review": [
            {"fact_ids": ["F5"], "text": "測定不能なので断定しない"}
        ],
    }
    # max_actions may be 3; single proposal ok
    md = render_advice_markdown(data, _evidence())
    expected = (
        "### 今日の改善提案\n"
        "1. [F3] 解釈文。提案文。翌日見る指標: 指標名\n"
        "\n"
        "### 明日の最小アクション\n"
        "- [ ] [F3] 行動する｜PASS: focus_blocks >= 1｜FAIL: 0回\n"
        "\n"
        "### AI作業の改善\n"
        "- [F5] 測定不能なので断定しない\n"
    )
    assert md == expected


def test_roundtrip_contract_ids_and_pass_parse():
    data = _valid_data()
    evidence = _evidence()
    md = render_advice_markdown(data, evidence)
    assert advice_contract_errors(md, evidence) == []
    full = f"## 🚀 Kaizen（AIからの改善提案）\n\n{md}"
    with_ids, entries = assign_action_ids(full, date(2026, 7, 21), [])
    assert len(entries) == 2
    assert "KZN-20260721-001" in with_ids
    # 付与後の行から PASS 機械構文を解析
    line = [ln for ln in with_ids.splitlines() if "KZN-20260721-002" in ln][0]
    # second action has context_switches
    assert parse_pass_condition(line) == ("context_switches", "<=", 40.0)


def test_renderer_invariant_raises_on_tamper(monkeypatch):
    data = _valid_data()
    evidence = _evidence()
    # 壊した render を強制: validate をスキップして空 proposals を描画させない
    # 代わりに advice_contract_errors がエラーを返すようにモック
    import kaizenlog.advice_format as af

    real_validate = af.validate_advice

    def ok_validate(d, e):
        return []

    monkeypatch.setattr(af, "validate_advice", ok_validate)
    monkeypatch.setattr(
        "kaizenlog.advisor.advice_contract_errors",
        lambda *a, **k: ["fake renderer violation"],
    )
    # render は data をそのまま使うので契約チェックで落ちる
    with pytest.raises(AdvisorError, match="renderer bug"):
        # validate_advice is called first with monkeypatched empty - then contract fails
        af.render_advice_markdown(data, evidence)
