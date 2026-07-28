from __future__ import annotations

import re
from copy import deepcopy
from zoneinfo import ZoneInfo

import pytest

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advisor import (
    AdviceContractError,
    AdvisorError,
    _contract_repair_prompt,
    advice_contract_errors,
    build_prompt,
    generate_advice,
    render_reader_advice,
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
- [ ] [F3] 始業時に25分枠を予定へ1件入れる｜PASS: focus_blocks >= 2｜FAIL: 1回以下
- [ ] [F9] 調査リンクを開く前に3件まとめる｜PASS: context_switches <= 40｜FAIL: 41回以上

### AI作業の改善
- [F5] 会話の往復数は測定不能なので、品質の良否は断定しない。
"""

# generate_advice の日次経路は JSON を返す前提
VALID_ADVICE_JSON = """{
  "plan_review": null,
  "proposals": [
    {
      "fact_ids": ["F3"],
      "interpretation": "開始条件を固定して集中枠の再現性を試す",
      "proposal": "始業時に集中枠を予定へ入れる",
      "next_metric": "集中ブロック回数"
    },
    {
      "fact_ids": ["F9"],
      "interpretation": "開発とブラウジングの往来が上位なので確認事項をまとめる",
      "proposal": "調査リンクをまとめて開く",
      "next_metric": "同遷移の回数"
    }
  ],
  "actions": [
    {
      "fact_ids": ["F3"],
      "trigger": "始業の直後",
      "action": "集中枠を予定へ一件入れる",
      "pass": "focus_blocks >= 2",
      "fail": "1回以下"
    },
    {
      "fact_ids": ["F9"],
      "trigger": "調査を始める前",
      "action": "リンクを三件まとめる",
      "pass": "context_switches <= 40",
      "fail": "41回以上"
    }
  ],
  "ai_review": [
    {
      "fact_ids": ["F5"],
      "text": "会話の往復数は測定不能なので品質の良否は断定しない"
    }
  ]
}
"""

MISSING_EVIDENCE_ADVICE = """### 今日の改善提案
1. [F0] 当日の機械可読統計がないため、まず生成状態を確認する。

### 明日の最小アクション
- [ ] [F0] 始業時に統計ファイルを1件確認する｜PASS: context_switches <= 200｜FAIL: 201回以上

### AI作業の改善
- [F5] AI会話の往復数と品質は測定不能なので断定しない。
"""

MISSING_EVIDENCE_JSON = """{
  "plan_review": null,
  "proposals": [
    {
      "fact_ids": ["F0"],
      "interpretation": "当日の機械可読統計がないためまず生成状態を確認する",
      "proposal": "統計生成を確認する",
      "next_metric": "統計ファイルの有無"
    }
  ],
  "actions": [
    {
      "fact_ids": ["F0"],
      "trigger": "始業の直後",
      "action": "統計ファイルを一件確認する",
      "pass": "context_switches <= 200",
      "fail": "201回以上"
    }
  ],
  "ai_review": [
    {
      "fact_ids": ["F5"],
      "text": "AI会話の往復数と品質は測定不能なので断定しない"
    }
  ]
}
"""

SHORT_DAY_ADVICE = """### 今日の改善提案
1. [F3] 集中できた時間帯の開始条件を明日も再現する。

### 明日の最小アクション
- [ ] [F3] 開発開始時に25分タイマーを1回設定する｜PASS: focus_blocks >= 1｜FAIL: 0回

### AI作業の改善
- [F5] AI会話の品質は測定不能なので評価しない。
"""


def test_evidence_prevents_ai_and_entertainment_misreading():
    evidence = build_advice_evidence(CURRENT, HISTORY).markdown
    assert "47ブロック" in evidence
    assert "AI会話セッション数・往復数ではない" in evidence
    assert "構造化AIテレメトリは0件" in evidence or "判断不能" in evidence
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


def test_short_day_reader_output_is_plain_and_limits_actions():
    current = deepcopy(CURRENT)
    current.update({
        "total_minutes": 44.0,
        "context_switches": 39,
        "by_category": {"AI作業": 32.0, "開発": 5.0, "ブラウジング": 2.0},
        "input": {
            "focus_blocks": 1,
            "focus_minutes": 32.0,
            "active_input_minutes": 35.0,
        },
    })
    evidence = build_advice_evidence(current, source_status="verified")

    assert evidence.max_actions == 1
    assert evidence.previous_day_available is False
    assert evidence.browser_sample_sufficient is False
    assert "合計44分" in evidence.reader_summary
    # 薄い日は結論1文のみ（N2: リッチ化は120分以上）
    assert "集中ブロックを1回" not in evidence.reader_summary
    assert "データ不足" in evidence.reader_summary
    assert any("前日比ではなく絶対値" in note for note in evidence.reader_notes)
    assert any("URL watcher" in note and "優先しません" in note for note in evidence.reader_notes)
    assert any("最大1件" in error for error in advice_contract_errors(VALID_ADVICE, evidence))

    rendered = render_reader_advice(
        f"## 🚀 Kaizen（AIからの改善提案）\n\n{SHORT_DAY_ADVICE}",
        evidence,
    )
    assert "### 今日の結論" in rendered
    assert "### 明日試すこと" in rendered
    assert "### 計測上の注意" in rendered
    assert "[F3]" not in rendered
    assert "[F5]" not in rendered
    assert "focus_blocks >= 1" in rendered


@pytest.mark.parametrize(
    ("replacement", "expected"),
    [
        ("通知を切って25分作業する", "通知を計測していない"),
        ("AIへの依頼内容を1メッセージにまとめる", "AI会話を計測していない"),
        ("URL watcher拡張機能を確認する", "watcher設定を優先できません"),
    ],
)
def test_short_day_rejects_unmeasured_or_low_priority_actions(replacement, expected):
    current = deepcopy(CURRENT)
    current.update({
        "total_minutes": 44.0,
        "by_category": {"AI作業": 32.0, "開発": 5.0, "ブラウジング": 2.0},
    })
    evidence = build_advice_evidence(current, source_status="verified")
    advice = SHORT_DAY_ADVICE.replace("開発開始時に25分タイマーを1回設定する", replacement)

    assert any(expected in error for error in advice_contract_errors(advice, evidence))


def test_without_previous_day_rejects_relative_action_condition():
    evidence = build_advice_evidence(CURRENT, source_status="verified")
    advice = SHORT_DAY_ADVICE.replace(
        "PASS: focus_blocks >= 1｜FAIL: 0回",
        "PASS: 前日比で増加｜FAIL: 前日比で減少または同数",
    )

    assert any("前日比を使えません" in error for error in advice_contract_errors(advice, evidence))


def test_missing_evidence_uses_safe_unmeasured_context():
    # F-ID 存在性は JSON 層（validate_advice）で担保。Markdown 契約では見出し等を見る
    invalid = MISSING_EVIDENCE_ADVICE.replace(
        "｜PASS: context_switches <= 200｜FAIL: 201回以上", "｜PASS: ｜FAIL:"
    )
    errors = advice_contract_errors(invalid)
    assert any("PASS:/FAIL:" in error for error in errors)


def test_contract_rejects_unknown_fact_missing_outcome_and_mismatched_action():
    evidence = build_advice_evidence(CURRENT)
    # Markdown 契約は PASS/FAIL と件数。F-ID 対応は JSON 層。
    # PASS 欠落は outcome エラー。件数不一致は別ケースで検証する。
    invalid = VALID_ADVICE.replace(
        "｜PASS: focus_blocks >= 2｜FAIL: 1回以下", ""
    )
    errors = advice_contract_errors(invalid, evidence)
    assert any("PASS:/FAIL:" in error for error in errors)


def test_contract_rejects_mismatched_proposal_action_counts():
    # 提案2件・アクション1件 → 件数1対1違反
    invalid = VALID_ADVICE.replace(
        "- [ ] [F9] 調査リンクを開く前に3件まとめる｜PASS: context_switches <= 40｜FAIL: 41回以上\n",
        "",
    )
    errors = advice_contract_errors(invalid, build_advice_evidence(CURRENT))
    assert any("件数を1対1" in error for error in errors)


def test_contract_rejects_empty_outcomes():
    evidence = build_advice_evidence(CURRENT)
    invalid = VALID_ADVICE.replace(
        "PASS: focus_blocks >= 2｜FAIL: 1回以下", "PASS: ｜FAIL:"
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
        "[F3] 始業時に25分枠を予定へ1件入れる｜PASS: focus_blocks >= 2｜FAIL: 1回以下",
        "[F1] 通知を切る｜PASS: context_switches <= 100｜FAIL: 101回以上",
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
        "[F3] 始業時に25分枠を予定へ1件入れる｜PASS: focus_blocks >= 2｜FAIL: 1回以下",
        "[F5] 中断理由を記録する｜PASS: ai_interruptions <= 100｜FAIL: 101回以上",
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
        "[F3] 始業時に25分枠を予定へ1件入れる｜PASS: focus_blocks >= 2｜FAIL: 1回以下",
        "[F4] セッション計測を設定する｜PASS: ai_cc_sessions >= 1｜FAIL: 0回",
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
        "[F3] 始業時に25分枠を予定へ1件入れる｜PASS: focus_blocks >= 2｜FAIL: 1回以下",
        "[F7] 分類設定を確認する｜PASS: category_minutes:エンタメ <= 0｜FAIL: 1分以上",
    )
    assert advice_contract_errors(advice, build_advice_evidence(CURRENT)) == []


def test_contract_rejects_observed_value_restatement_even_when_measured():
    # 観測数値再掲は JSON 層（interpretation / ai_review.text）で禁止。Markdown 契約からは削除。
    from kaizenlog.advice_format import validate_advice
    from tests.test_advice_format import _valid_data

    data = _valid_data()
    data["ai_review"] = [{"fact_ids": ["F5"], "text": "セッションは999回だった"}]
    errors = validate_advice(data, build_advice_evidence(CURRENT))
    assert any("観測数値" in error for error in errors)


def test_contract_rejects_fabricated_entertainment_value_when_observed():
    from kaizenlog.advice_format import validate_advice
    from tests.test_advice_format import _valid_data

    data = _valid_data()
    data["proposals"][0]["interpretation"] = "エンタメ利用が999分発生した"
    errors = validate_advice(data, build_advice_evidence(CURRENT))
    assert any("観測数値" in error for error in errors)


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
        "PASS: focus_blocks >= 2｜FAIL: 1回以下",
        "PASS: うまくできた｜FAIL: うまくできなかった",
    )
    errors = advice_contract_errors(invalid, build_advice_evidence(CURRENT))
    assert any("数値条件" in error or "機械構文" in error for error in errors)


def test_contract_rejects_measurable_freeform_pass():
    """数字を含む自由文 PASS も遮断（計測不能ガード迂回防止）。"""
    invalid = VALID_ADVICE.replace(
        "PASS: focus_blocks >= 2｜FAIL: 1回以下",
        "PASS: ChatGPT履歴にタグ付きが2件以上｜FAIL: 0件",
    )
    errors = advice_contract_errors(invalid, build_advice_evidence(CURRENT))
    assert any("機械構文" in error for error in errors)


def test_contract_rejects_unknown_fact_in_ai_section():
    from kaizenlog.advice_format import validate_advice
    from tests.test_advice_format import _valid_data

    data = _valid_data()
    data["ai_review"] = [{"fact_ids": ["F999"], "text": "測定不能なので断定しない"}]
    errors = validate_advice(data, build_advice_evidence(CURRENT))
    assert any("存在しない" in error for error in errors)


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
            VALID_ADVICE.replace("- [ ] [F3]", "- [ ] KZN-20260721-999: [F3]", 1),
            "モデル生成のKZN ID",
        ),
        (
            VALID_ADVICE.replace(
                "- [ ] [F9] 調査リンクを開く前に3件まとめる｜PASS: context_switches <= 40｜FAIL: 41回以上\n",
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
    replies = iter(["形式違反", VALID_ADVICE_JSON])
    calls = []

    def fake_generate(cfg, system, user):
        calls.append(user)
        return next(replies)

    monkeypatch.setattr("kaizenlog.advisor.generate_text", fake_generate)
    result = generate_advice(LLMConfig(), "ログ", [], evidence=evidence)
    assert result.outcome in ("ok", "repaired")
    assert "## 🚀 Kaizen" in result.markdown
    assert "### 明日の最小アクション" in result.markdown
    assert len(calls) == 2
    assert "出力契約の修正依頼" in calls[1]
    assert "JSON" in calls[1]
    assert "違反" in calls[1]


def test_contract_repair_masks_observed_numbers_and_kzn_ids():
    evidence = build_advice_evidence(CURRENT)
    invalid = (
        '{"proposals":[{"interpretation":"集中ブロックは47回（継続 KZN-20260721-003）"}]}'
    )

    prompt = _contract_repair_prompt(evidence, invalid, ["観測数値を再掲しています"])

    assert "KZN-20260721-003" not in prompt
    assert "既存アクション" in prompt
    assert "数値省略" in prompt


def test_contract_accepts_explicit_f4_non_session_statement():
    evidence = build_advice_evidence(CURRENT)
    advice = re.sub(
        r"(?m)^- \[F5\].*$",
        "- [F4] 画面ブロックは会話セッション数ではなく評価対象外。",
        VALID_ADVICE,
    )

    assert advice_contract_errors(advice, evidence) == []


def test_contract_accepts_measurable_relative_fail_condition():
    evidence = build_advice_evidence(
        CURRENT,
        [{"day": "2026-07-20", "total_minutes": 120.0, "context_switches": 20}],
    )
    advice = re.sub(
        r"FAIL: [^\n]+",
        "FAIL: 前日と同数以上",
        VALID_ADVICE,
        count=1,
    )

    assert advice_contract_errors(advice, evidence) == []


def test_contract_does_not_treat_non_ai_fragmentation_as_ai_quality():
    evidence = build_advice_evidence(CURRENT)
    advice = VALID_ADVICE.replace(
        "1. [F3]",
        "1. [F3] 細切れ区間をまとめる。",
        1,
    )

    errors = advice_contract_errors(advice, evidence)
    assert not any("AI会話テレメトリ" in error for error in errors)


def test_contract_repair_redacts_first_model_answer(monkeypatch):
    evidence = build_advice_evidence(CURRENT)
    replies = iter(["SECRET invalid", VALID_ADVICE_JSON])
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
        return VALID_ADVICE_JSON

    monkeypatch.setattr("kaizenlog.advisor.generate_text", fake_generate)
    # カスタムプロンプトは日次契約外（素通し）。マスクは prepare で適用される
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
    assert result.markdown.endswith("自由形式の回答")


def test_generate_advice_keeps_positional_redactor_compatibility(monkeypatch):
    captured = []

    def fake_generate(cfg, system, user):
        captured.append(user)
        return MISSING_EVIDENCE_JSON

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


def test_generate_advice_json_path_renders_markdown(monkeypatch):
    monkeypatch.setattr(
        "kaizenlog.advisor.generate_text", lambda *a, **k: VALID_ADVICE_JSON
    )
    result = generate_advice(
        LLMConfig(), "ログ", [], evidence=build_advice_evidence(CURRENT, HISTORY)
    )
    assert result.outcome == "ok"
    assert result.markdown.startswith("## 🚀 Kaizen")
    assert "### 今日の改善提案" in result.markdown
    assert "- [ ]" in result.markdown and "PASS:" in result.markdown
    assert "[F3]" not in result.markdown  # U3: 表示から F-ID を外す
