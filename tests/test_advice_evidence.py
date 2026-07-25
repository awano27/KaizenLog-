from __future__ import annotations

import re
from copy import deepcopy
from zoneinfo import ZoneInfo

import pytest

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advisor import (
    AdviceContractError,
    AdvisorError,
    advice_contract_errors,
    build_prompt,
    generate_advice,
)
from kaizenlog.config import LLMConfig


CURRENT = {
    "version": 1,
    "day": "2026-07-21",
    "total_minutes": 204.7,
    "context_switches": 192,
    "ai_activity_blocks": 47,
    "by_category": {
        "開発": 95.9,
        "AI作業": 53.5,
        "ブラウジング": 39.1,
    },
    "by_app": {
        "Code.exe": 95.9,
        "chrome.exe": 30.5,
        "msedge.exe": 22.7,
        "SearchApp.exe": 0.3,
    },
    "by_site": {"github.com": 2.7, "learn.microsoft.com": 0.8, "patreon.com": 0.0},
    "blocks": [
        {"start": "2026-07-21T09:00:00+09:00", "category": "開発"},
        {"start": "2026-07-21T09:10:00+09:00", "category": "ブラウジング"},
        {"start": "2026-07-21T09:15:00+09:00", "category": "開発"},
        {"start": "2026-07-21T10:00:00+09:00", "category": "ブラウジング"},
        {"start": "2026-07-21T10:05:00+09:00", "category": "AI作業"},
    ],
    "input": {"focus_blocks": 1, "focus_minutes": 26.0, "active_input_minutes": 99.8},
    "ai": {"sessions": 0, "fragmented": 0, "tool_errors": 0, "interruptions": 0},
}

HISTORY = [
    {"day": "2026-07-19", "total_minutes": 240.0, "context_switches": 80},
    {"day": "2026-07-20", "total_minutes": 180.0, "context_switches": 90},
    {"day": "2026-07-18", "total_minutes": 300.0, "context_switches": 110},
]


VALID_ADVICE = """### 今日の改善提案
1. [F3] 開始条件を固定して集中枠の再現性を試す。翌日は集中ブロック2回以上を目標にする。
2. [F9] 開発とブラウジングの往来が上位なので、確認事項をまとめて開く。翌日は同遷移を見る。

### 明日の最小アクション
- [ ] [F3] 始業時に25分枠を予定へ1件入れる｜PASS: 集中ブロック2回以上｜FAIL: 1回以下
- [ ] [F9] 調査リンクを開く前に3件まとめる｜PASS: 開発→ブラウジング1回以下｜FAIL: 2回以上

### AI作業の改善
- [F5] 会話の往復数は測定不能なので、品質の良否は断定しない。
"""

MISSING_EVIDENCE_ADVICE = """### 今日の改善提案
1. [F0] 当日の機械可読統計がないため、まず生成状態を確認する。

### 明日の最小アクション
- [ ] [F0] 始業時に統計ファイルを1件確認する｜PASS: 1件確認｜FAIL: 0件

### AI作業の改善
- [F5] AI会話の往復数と品質は測定不能なので断定しない。
"""


def test_evidence_prevents_ai_and_entertainment_misreading():
    evidence = build_advice_evidence(CURRENT, HISTORY).markdown
    assert "47ブロック" in evidence
    assert "AI会話セッション数・往復数ではない" in evidence
    assert "AI会話の発話数・往復数は判断不能" in evidence
    assert "娯楽利用を示す定量根拠なし" in evidence
    assert "patreon.com" not in evidence  # 0.0分は利用証拠として渡さない
    assert "概算URL観測率 6.6%" in evidence


def test_evidence_adds_baseline_and_transition_pattern():
    evidence = build_advice_evidence(CURRENT, HISTORY).markdown
    assert "過去3日中央値" in evidence
    assert "中央値比" in evidence
    assert "開発→ブラウジング 2回" in evidence
    assert "09時台" in evidence


def test_evidence_does_not_compare_with_too_few_baseline_days():
    evidence = build_advice_evidence(CURRENT, HISTORY[:2]).markdown
    assert "比較可能な過去統計が不足" in evidence
    assert "中央値比" not in evidence


def test_zero_baseline_does_not_claim_zero_percent_change():
    history = [
        {"day": f"2026-07-{day:02d}", "total_minutes": 120.0, "context_switches": 0}
        for day in (18, 19, 20)
    ]
    evidence = build_advice_evidence(CURRENT, history).markdown
    assert "中央値が0のため比率算出不能" in evidence
    assert "中央値比 +0%" not in evidence


def test_malformed_history_is_excluded_from_baseline():
    history = [
        {"day": f"2026-07-{day:02d}", "total_minutes": 120.0, "context_switches": 20}
        for day in (18, 19, 20)
    ]
    history.extend(
        {"day": f"2026-07-{day:02d}", "total_minutes": 10_000.0}
        for day in (14, 15, 16, 17)
    )
    evidence = build_advice_evidence(CURRENT, history).markdown
    assert "比較可能な過去3日中央値" in evidence
    assert "アクティブ時間中央値 120分" in evidence
    assert "10000分" not in evidence


def test_rounded_site_totals_do_not_report_over_100_percent_coverage():
    current = deepcopy(CURRENT)
    current["by_app"] = {"chrome.exe": 0.1}
    current["by_site"] = {"a.example": 0.1, "b.example": 0.1}
    evidence = build_advice_evidence(current).markdown
    assert "URL観測率は算出不能" in evidence
    assert "200%" not in evidence


@pytest.mark.parametrize("bad_value", [None, -1, float("nan"), float("inf")])
def test_invalid_required_stats_are_downgraded_to_unmeasured(bad_value):
    current = deepcopy(CURRENT)
    current["total_minutes"] = bad_value
    evidence = build_advice_evidence(current).markdown
    assert "[F0] 当日統計が不正または不完全" in evidence
    assert "合計アクティブ時間 0分" not in evidence


def test_invalid_nested_stats_only_downgrade_affected_facts():
    current = deepcopy(CURRENT)
    current["ai_activity_blocks"] = -2
    current["by_category"] = {"AI作業": -10.0}
    current["by_app"] = {"chrome.exe": float("nan")}
    current["ai"] = {
        "sessions": -1,
        "fragmented": 0,
        "tool_errors": -3,
        "interruptions": 0,
    }
    evidence = build_advice_evidence(current).markdown
    assert "[F1] 合計アクティブ時間 204.7分" in evidence
    assert "[F2] カテゴリ別統計なし" in evidence
    assert "[F4]" in evidence and "ブロック数は測定不能" in evidence
    assert "[F5] 構造化AIテレメトリ欄なし" in evidence
    assert "-2" not in evidence and "-3" not in evidence and "-10" not in evidence


def test_transition_peak_hours_use_configured_timezone():
    current = deepcopy(CURRENT)
    current["blocks"] = [
        {"start": "2026-07-20T22:00:00+00:00", "category": "開発"},
        {"start": "2026-07-20T22:10:00+00:00", "category": "ブラウジング"},
    ]
    evidence = build_advice_evidence(
        current, timezone=ZoneInfo("Asia/Tokyo")
    ).markdown
    assert "07時台 1回" in evidence
    assert "22時台" not in evidence


def test_evidence_marks_missing_values_as_unmeasured():
    evidence = build_advice_evidence(None).markdown
    assert "[F0]" in evidence
    assert "[F4]" in evidence and "測定不能" in evidence
    assert "[F5]" in evidence and "往復数・品質は測定不能" in evidence


def test_evidence_does_not_duplicate_system_prompt_output_contract():
    assert "## 提案の出力契約" not in build_advice_evidence(CURRENT).markdown


def test_old_stats_derive_ai_activity_blocks_without_calling_them_sessions():
    old = deepcopy(CURRENT)
    old.pop("ai_activity_blocks")
    old["blocks"] = [
        {"category": "AI作業"},
        {"category": "開発"},
        {"category": "AI作業"},
    ]
    evidence = build_advice_evidence(old).markdown
    assert "2ブロック" in evidence
    assert "セッション数・往復数ではない" in evidence


def test_evidence_is_before_human_readable_log():
    evidence = build_advice_evidence(CURRENT)
    prompt = build_prompt("AIセッション数: 47回", [], evidence=evidence)
    assert prompt.index("[L1]") < prompt.index("AIセッション数: 47回")


def test_valid_advice_contract():
    evidence = build_advice_evidence(CURRENT)
    assert advice_contract_errors(VALID_ADVICE, evidence) == []


def test_missing_evidence_uses_safe_unmeasured_context():
    invalid = MISSING_EVIDENCE_ADVICE.replace("[F0]", "[F999]", 1)
    errors = advice_contract_errors(invalid)
    assert any("存在しない根拠ID" in error for error in errors)


def test_contract_rejects_unknown_fact_missing_outcome_and_mismatched_action():
    evidence = build_advice_evidence(CURRENT)
    invalid = VALID_ADVICE.replace("[F3] 開始", "[F99] 開始", 1).replace(
        "｜PASS: 集中ブロック2回以上｜FAIL: 1回以下", ""
    )
    errors = advice_contract_errors(invalid, evidence)
    assert any("存在しない根拠ID" in error for error in errors)
    assert any("PASS:/FAIL:" in error for error in errors)
    assert any("対応していません" in error for error in errors)


def test_contract_rejects_empty_outcomes():
    evidence = build_advice_evidence(CURRENT)
    invalid = VALID_ADVICE.replace(
        "PASS: 集中ブロック2回以上｜FAIL: 1回以下", "PASS: ｜FAIL:"
    )
    assert any(
        "PASS:/FAIL:" in error
        for error in advice_contract_errors(invalid, evidence)
    )


@pytest.mark.parametrize(
    "claim",
    [
        "[F4] 47ブロックは47会話だったため、明日は減らす。",
        "[F4] AI関連画面ブロックが多く、短い会話セッションが多発した。",
        "[F5] AIとは10往復したので、明日は依頼をまとめる。",
        "[F5] AI会話が細切れだったので、明日は依頼をまとめる。",
        "[F7] エンタメ利用が39分発生したので、明日は減らす。",
    ],
)
def test_contract_rejects_known_semantic_misreadings(claim):
    invalid = VALID_ADVICE.replace(
        "[F5] 会話の往復数は測定不能なので、品質の良否は断定しない。",
        claim,
    )
    errors = advice_contract_errors(invalid, build_advice_evidence(CURRENT))
    assert any(
        "ブロック数を会話数" in error
        or "テレメトリがない" in error
        or "娯楽・私用利用" in error
        for error in errors
    )


def test_contract_rejects_category_changes_as_notification_interruptions():
    invalid = VALID_ADVICE.replace(
        "[F3] 開始条件を固定して集中枠の再現性を試す。翌日は集中ブロック2回以上を目標にする。",
        "[F1] 192回の通知割り込みで生産性が低下したため、明日は通知を切る。",
    ).replace(
        "[F3] 始業時に25分枠を予定へ1件入れる｜PASS: 集中ブロック2回以上｜FAIL: 1回以下",
        "[F1] 通知を切る｜PASS: カテゴリ変更100回以下｜FAIL: 101回以上",
    )
    errors = advice_contract_errors(invalid, build_advice_evidence(CURRENT))
    assert any("通知・割り込み" in error for error in errors)

    numberless = invalid.replace(
        "192回の通知割り込みで生産性が低下した",
        "頻繁なカテゴリ変更は通知割り込みを示し、生産性が低下した",
    )
    errors = advice_contract_errors(numberless, build_advice_evidence(CURRENT))
    assert any("通知・割り込み" in error for error in errors)


def test_switch_guard_allows_independently_measured_f5_interruptions():
    current = deepcopy(CURRENT)
    current["ai"] = {
        "sessions": 1,
        "fragmented": 0,
        "tool_errors": 0,
        "interruptions": 192,
    }
    advice = VALID_ADVICE.replace(
        "[F3] 開始条件を固定して集中枠の再現性を試す。翌日は集中ブロック2回以上を目標にする。",
        "[F5] Claude Codeで中断が記録されたため、明日は原因を記録する。",
    ).replace(
        "[F3] 始業時に25分枠を予定へ1件入れる｜PASS: 集中ブロック2回以上｜FAIL: 1回以下",
        "[F5] 中断理由を記録する｜PASS: 1件記録｜FAIL: 0件",
    ).replace(
        "[F5] 会話の往復数は測定不能なので、品質の良否は断定しない。",
        "[F5] 中断回数は明示テレメトリで測定済み。",
    )

    assert advice_contract_errors(advice, build_advice_evidence(current)) == []


def test_semantic_guard_allows_measurement_and_maintenance_actions():
    current = deepcopy(CURRENT)
    current["ai_activity_blocks"] = 1
    advice = VALID_ADVICE.replace(
        "[F3] 開始条件を固定して集中枠の再現性を試す。翌日は集中ブロック2回以上を目標にする。",
        "[F4] セッション計測を設定し、明日から1往復ごとに記録する。",
    ).replace(
        "[F3] 始業時に25分枠を予定へ1件入れる｜PASS: 集中ブロック2回以上｜FAIL: 1回以下",
        "[F4] セッション計測を設定する｜PASS: 明日1件記録｜FAIL: 0件記録",
    ).replace(
        "[F5] 会話の往復数は測定不能なので、品質の良否は断定しない。",
        "[F5] 明日から1往復ごとにログへ記録する。",
    )
    assert advice_contract_errors(advice, build_advice_evidence(current)) == []


def test_semantic_guard_allows_zero_entertainment_maintenance():
    advice = VALID_ADVICE.replace(
        "[F3] 開始条件を固定して集中枠の再現性を試す。翌日は集中ブロック2回以上を目標にする。",
        "[F7] 娯楽利用の定量根拠がない状態を維持するため、分類精度を明日確認する。",
    ).replace(
        "[F3] 始業時に25分枠を予定へ1件入れる｜PASS: 集中ブロック2回以上｜FAIL: 1回以下",
        "[F7] 分類設定を確認する｜PASS: エンタメ0分｜FAIL: 誤分類1件以上",
    )
    assert advice_contract_errors(advice, build_advice_evidence(CURRENT)) == []


def test_contract_rejects_observed_value_restatement_even_when_measured():
    current = deepcopy(CURRENT)
    current["ai_activity_blocks"] = 5
    current["ai"] = {
        "sessions": 5,
        "fragmented": 1,
        "tool_errors": 0,
        "interruptions": 0,
    }
    advice = VALID_ADVICE.replace(
        "[F5] 会話の往復数は測定不能なので、品質の良否は断定しない。",
        "[F5] Claude Codeセッションは999回だった。",
    )
    errors = advice_contract_errors(advice, build_advice_evidence(current))
    assert any("観測数値を再掲せず" in error for error in errors)


def test_contract_rejects_fabricated_entertainment_value_when_observed():
    current = deepcopy(CURRENT)
    current["by_category"]["エンタメ"] = 10.0
    advice = VALID_ADVICE.replace(
        "[F3] 開始条件を固定して集中枠の再現性を試す。翌日は集中ブロック2回以上を目標にする。",
        "[F7] エンタメ利用が999分発生したため分類を見直す。翌日は0分を目標にする。",
    ).replace(
        "[F3] 始業時に25分枠を予定へ1件入れる｜PASS: 集中ブロック2回以上｜FAIL: 1回以下",
        "[F7] 分類設定を確認する｜PASS: エンタメ0分｜FAIL: 1分以上",
    )
    errors = advice_contract_errors(advice, build_advice_evidence(current))
    assert any("観測数値を再掲せず" in error for error in errors)


def test_contract_requires_typed_evidence_context():
    evidence = build_advice_evidence(CURRENT)
    with pytest.raises(TypeError, match="AdviceEvidence"):
        advice_contract_errors(VALID_ADVICE, evidence.markdown)  # type: ignore[arg-type]


def test_contract_rejects_code_fenced_daily_answer():
    errors = advice_contract_errors(
        f"```markdown\n{VALID_ADVICE}\n```", build_advice_evidence(CURRENT)
    )
    assert any("コードフェンス" in error for error in errors)


def test_contract_rejects_subheading_inside_action_section():
    invalid = VALID_ADVICE.replace(
        "### 明日の最小アクション\n",
        "### 明日の最小アクション\n#### 詳細\n",
    )
    errors = advice_contract_errors(invalid, build_advice_evidence(CURRENT))
    assert any("サブ見出し" in error for error in errors)


def test_contract_rejects_unknown_heading_and_extra_checkbox():
    invalid = VALID_ADVICE + "\n### 追加\n- [ ] [F1] 余分な行｜PASS: 1件｜FAIL: 0件\n"
    errors = advice_contract_errors(invalid, build_advice_evidence(CURRENT))
    assert any("許可されていない見出し" in error for error in errors)
    assert any("チェックボックス" in error for error in errors)


def test_contract_rejects_subjective_pass_fail_conditions():
    invalid = VALID_ADVICE.replace(
        "PASS: 集中ブロック2回以上｜FAIL: 1回以下",
        "PASS: うまくできた｜FAIL: うまくできなかった",
    )
    errors = advice_contract_errors(invalid, build_advice_evidence(CURRENT))
    assert any("数値条件" in error for error in errors)


def test_contract_rejects_unknown_fact_in_ai_section():
    invalid = VALID_ADVICE.replace("[F5] 会話", "[F5] [F999] 会話")
    errors = advice_contract_errors(invalid, build_advice_evidence(CURRENT))
    assert any("存在しない根拠ID" in error for error in errors)


def test_missing_ai_mapping_still_blocks_fabricated_turn_count():
    current = deepcopy(CURRENT)
    current.pop("ai")
    invalid = VALID_ADVICE.replace(
        "[F5] 会話の往復数は測定不能なので、品質の良否は断定しない。",
        "[F5] AIとは10往復したので依頼をまとめる。",
    )
    errors = advice_contract_errors(invalid, build_advice_evidence(current))
    assert any("テレメトリがない" in error for error in errors)


@pytest.mark.parametrize(
    ("invalid", "expected"),
    [
        (
            VALID_ADVICE.replace("- [ ] [F3]", "- [ ] 先に説明 [F3]", 1),
            "根拠ID [F#] から開始",
        ),
        (
            VALID_ADVICE.replace("[F5] 会話", "[F1] 会話"),
            "AI根拠ID [F4] または [F5]",
        ),
        (
            VALID_ADVICE.replace("- [ ] [F3]", "- [ ] KZN-20260721-999: [F3]", 1),
            "モデル生成のKZN ID",
        ),
        (
            VALID_ADVICE.replace(
                "- [ ] [F9] 調査リンクを開く前に3件まとめる｜PASS: 開発→ブラウジング1回以下｜FAIL: 2回以上\n",
                "",
            ),
            "件数を1対1",
        ),
    ],
)
def test_contract_rejects_independent_structural_violations(invalid, expected):
    errors = advice_contract_errors(invalid, build_advice_evidence(CURRENT))
    assert any(expected in error for error in errors)


def test_generate_advice_repairs_contract_once(monkeypatch):
    evidence = build_advice_evidence(CURRENT)
    replies = iter(["形式違反", VALID_ADVICE])
    calls = []

    def fake_generate(cfg, system, user):
        calls.append(user)
        return next(replies)

    monkeypatch.setattr("kaizenlog.advisor.generate_text", fake_generate)
    result = generate_advice(LLMConfig(), "ログ", [], evidence=evidence)
    assert result.endswith(VALID_ADVICE)
    assert len(calls) == 2
    assert "出力契約の修正依頼" in calls[1]


def test_contract_repair_redacts_first_model_answer(monkeypatch):
    evidence = build_advice_evidence(CURRENT)
    replies = iter(["SECRET invalid", VALID_ADVICE])
    calls = []

    def fake_generate(cfg, system, user):
        calls.append(user)
        return next(replies)

    monkeypatch.setattr("kaizenlog.advisor.generate_text", fake_generate)
    generate_advice(
        LLMConfig(),
        "ログ",
        [],
        evidence=evidence,
        redactor=lambda value: value.replace("SECRET", "[MASKED]"),
    )
    assert len(calls) == 2
    assert "SECRET" not in calls[1]
    assert "[MASKED] invalid" in calls[1]


def test_generate_advice_rejects_second_invalid_response(monkeypatch):
    monkeypatch.setattr("kaizenlog.advisor.generate_text", lambda *args: "形式違反")
    with pytest.raises(AdviceContractError, match="保存条件"):
        generate_advice(LLMConfig(), "ログ", [], evidence=build_advice_evidence(CURRENT))


def test_generate_advice_redacts_system_and_user(monkeypatch, tmp_path):
    custom = tmp_path / "prompt.md"
    custom.write_text("SECRET system", encoding="utf-8")
    calls = []

    def fake_generate(cfg, system, user):
        calls.append((system, user))
        return VALID_ADVICE

    monkeypatch.setattr("kaizenlog.advisor.generate_text", fake_generate)
    generate_advice(
        LLMConfig(system_prompt=str(custom)),
        "SECRET user log",
        [],
        evidence=build_advice_evidence(CURRENT),
        redactor=lambda value: value.replace("SECRET", "[MASKED]"),
    )
    assert calls
    assert all("SECRET" not in system + user for system, user in calls)


def test_redactor_that_breaks_fact_ids_fails_before_llm(monkeypatch):
    calls = []
    monkeypatch.setattr("kaizenlog.advisor.generate_text", lambda *args: calls.append(args))
    with pytest.raises(AdvisorError, match="制御トークン"):
        generate_advice(
            LLMConfig(),
            "ログ",
            [],
            evidence=build_advice_evidence(CURRENT),
            redactor=lambda value: re.sub(r"F\d+", "MASKED", value),
        )
    assert calls == []


def test_custom_prompt_keeps_its_own_output_contract(monkeypatch, tmp_path):
    custom = tmp_path / "prompt.md"
    custom.write_text("自由形式で返す", encoding="utf-8")
    monkeypatch.setattr("kaizenlog.advisor.generate_text", lambda *args: "自由形式の回答")
    result = generate_advice(
        LLMConfig(system_prompt=str(custom)),
        "ログ",
        [],
        evidence=build_advice_evidence(CURRENT),
    )
    assert result.endswith("自由形式の回答")


def test_generate_advice_keeps_positional_redactor_compatibility(monkeypatch):
    captured = []

    def fake_generate(cfg, system, user):
        captured.append(user)
        return MISSING_EVIDENCE_ADVICE

    monkeypatch.setattr("kaizenlog.advisor.generate_text", fake_generate)
    # v1.5以前の呼び出し形: memoryの次（7番目）がredactor。
    generate_advice(
        LLMConfig(),
        "SECRET",
        [],
        None,
        None,
        None,
        lambda value: value.replace("SECRET", "[MASKED]"),
    )
    assert captured and "SECRET" not in captured[0]
