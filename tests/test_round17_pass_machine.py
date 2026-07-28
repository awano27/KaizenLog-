"""第17弾: PASS 機械構文強制 (P1) と結論文面 (P2)。"""
from __future__ import annotations

from copy import deepcopy

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advice_format import validate_advice
from kaizenlog.runlog import classify_violation_kind
from tests.test_advice_evidence import CURRENT, HISTORY
from tests.test_advice_format import _valid_data


def test_p1_freeform_pass_rejected_json():
    data = _valid_data()
    data["actions"][0]["pass"] = (
        "ChatGPT履歴に[タグ]付きセッションが翌朝2件以上ある"
    )
    data["actions"][0]["fail"] = "[タグ]付きセッションが0件"
    errs = validate_advice(data, build_advice_evidence(CURRENT, HISTORY))
    assert any("機械構文" in e for e in errs)


def test_p1_machine_pass_still_ok():
    data = _valid_data()
    assert validate_advice(data, build_advice_evidence(CURRENT, HISTORY)) == []


# markdown 経路の旧契約検査は廃止。自由文 PASS 拒否は
# test_p1_freeform_pass_rejected_json が JSON 契約層で担保する。


def test_p1_fail_machine_unknown_metric_rejected():
    data = _valid_data()
    data["actions"][0]["pass"] = "focus_blocks >= 2"
    data["actions"][0]["fail"] = "pomodoro_count <= 0"
    errs = validate_advice(data, build_advice_evidence(CURRENT, HISTORY))
    assert any("fail" in e.lower() or "FAIL" in e or "機械構文" in e for e in errs)


def test_p1_fail_freeform_with_digits_still_ok():
    data = _valid_data()
    data["actions"][0]["fail"] = "1回以下"
    assert validate_advice(data, build_advice_evidence(CURRENT, HISTORY)) == []


def test_p1_violation_kind_pass_not_machine_readable():
    msg = (
        "actions[1] の pass は機械構文（指標 演算子 数値）にしてください"
        "（例: ai_tool_errors <= 60）。自由文は自動判定できず契約違反です"
    )
    assert classify_violation_kind(msg) == "pass_not_machine_readable"
    # FAIL 側「解析できません」が json に誤分類されないこと
    fail_msg = "actions[1] の fail は機械構文として解析できません（未知指標または形式不正）"
    assert classify_violation_kind(fail_msg) == "pass_not_machine_readable"
    assert classify_violation_kind("指標名が使用可能な指標にありません") == "pass_fail"


def test_p2_category_sentence_natural():
    stats = deepcopy(CURRENT)
    stats["total_minutes"] = 200.0
    ev = build_advice_evidence(stats, HISTORY)
    assert "カテゴリ別では" in ev.reader_summary
    assert "最多（" in ev.reader_summary
    assert "最多が記録" not in ev.reader_summary
