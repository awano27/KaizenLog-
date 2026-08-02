"""第43弾: AIコーチ節の平文化・status文言・日報切詰め。"""
from __future__ import annotations

from datetime import date

from kaizenlog.advisor import render_reader_advice
from kaizenlog.advice_evidence import AdviceEvidence
from kaizenlog.memory import (
    ActionStats,
    assign_action_ids,
    humanize_advice_markdown_actions,
    render_action_stats_line,
)
from kaizenlog.nippou import generate_nippou_deterministic
from kaizenlog.verdict import parse_pass_condition
from zoneinfo import ZoneInfo


def _evidence() -> AdviceEvidence:
    return AdviceEvidence(
        markdown="",
        fact_ids=frozenset(),
        ai_conversation_metrics_available=True,
        entertainment_observed=False,
        reader_summary="今日は作業ログがあります。",
        reader_notes=(),
        max_actions=1,
        previous_day_available=True,
        browser_sample_sufficient=True,
        total_minutes=180.0,
        input_metrics_available=True,
        structured_ai_metrics_available=True,
    )


INTERNAL_ADVICE = """\
### 今日の改善提案
1. 終了時の確認を習慣化する。git diff で変更を見る。翌日見る指標: リトライ連鎖数

### 明日の最小アクション
- [ ] 終了するとき→git diff --stat を実行する｜PASS: ai_retry_chains <= 0（リトライ連鎖数）｜FAIL: ai_retry_chains >= 1
    - なぜ効くと考えるか: 終了前に成果物を確認できる
    - 効かなかったと分かる条件: リトライが翌日も続く

### AI作業の改善
- リトライ連鎖を観測する
"""


def _note_pipeline(advice_internal: str = INTERNAL_ADVICE, day: date | None = None):
    """render_reader → assign_ids → humanize（ノート書き込み直前と同じ順）。"""
    d = day or date(2026, 8, 2)
    reader = render_reader_advice(advice_internal, _evidence())
    with_ids, entries = assign_action_ids(reader, d, [])
    note = humanize_advice_markdown_actions(with_ids)
    return note, entries, reader, with_ids


# ---------- §R1 ----------


def test_r1_note_has_no_pass_fail_or_raw_metric():
    note, entries, _, _ = _note_pipeline()
    assert "｜PASS:" not in note
    assert "｜FAIL:" not in note
    assert "|PASS:" not in note
    assert "ai_retry_chains" not in note
    assert "効果指標: リトライ連鎖数 を 0 以下 に" in note


def test_r1_metric_label_fallback_when_no_annotation():
    advice = """\
### 今日の改善提案
1. 切替を減らす。窓を閉じる。翌日見る指標: コンテキストスイッチ

### 明日の最小アクション
- [ ] 窓を閉じる→作業を始める｜PASS: context_switches <= 30｜FAIL: 31

### AI作業の改善
- なし
"""
    note, _, _, _ = _note_pipeline(advice)
    # annotation 無しでも metric_display_label か生名
    assert "｜PASS:" not in note
    assert "効果指標:" in note
    assert "30" in note
    # ラベルが取れる指標なので生名はラベルへ置換される
    assert "コンテキストスイッチ回数" in note
    assert "context_switches" not in note


def test_r1_keeps_kzn_id_and_checkbox():
    note, entries, _, with_ids = _note_pipeline()
    assert entries
    kid = entries[0].id
    assert kid.startswith("KZN-20260802-")
    assert f"- [ ] {kid}:" in note
    assert kid in with_ids  # 付与直後にもある


def test_r1_keeps_mechanism_falsifier_sublines():
    note, _, _, _ = _note_pipeline()
    assert "なぜ効くと考えるか: 終了前に成果物を確認できる" in note
    assert "効かなかったと分かる条件: リトライが翌日も続く" in note


def test_r1_freeform_action_unchanged():
    advice = """\
### 今日の改善提案
1. メモする。書く。翌日見る指標: なし

### 明日の最小アクション
- [ ] ただメモを取る（自由文・機械構文なし）

### AI作業の改善
- x
"""
    note, entries, _, with_ids = _note_pipeline(advice)
    assert "ただメモを取る（自由文・機械構文なし）" in note
    assert "効果指標:" not in note
    # 自由文は PASS 無しのまま台帳へ
    assert entries
    assert "PASS" not in entries[0].action
    assert "ただメモを取る" in entries[0].action


def test_r1_ledger_keeps_machine_syntax():
    note, entries, _, _ = _note_pipeline()
    assert entries
    action = entries[0].action
    assert "｜PASS:" in action or "PASS:" in action
    assert "ai_retry_chains" in action
    assert parse_pass_condition(action) is not None
    # ノート側は平文
    assert "｜PASS:" not in note


# ---------- §R2 ----------


def test_r2_status_line_plain_terms_and_numbers():
    filled = ActionStats(
        window_days=14,
        proposed=12,
        done=5,
        judged=8,
        passed=6,
        done_judged=4,
        done_passed=3,
        undone_passed=2,
        skipped=1,
    )
    line = render_action_stats_line(filled)
    assert "提案 12件" in line
    assert "チェック完了 5件" in line
    assert "42%" in line  # 5/12
    assert "スキップ 1件" in line
    assert "チェック済みで指標達成 3件" in line
    assert "75%" in line  # pass_rate 3/4
    assert "チェックなしで指標達成 2件" in line
    assert "消化" not in line
    assert "実行済みPASS" not in line
    assert "未実行のままPASS到達" not in line

    empty = ActionStats(window_days=14, proposed=0, done=0, judged=0, passed=0)
    assert "まだ提案がありません" in render_action_stats_line(empty)


# ---------- §R3 ----------


def test_r3_nippou_tomorrow_plain_no_id_head_truncate():
    """日報の明日予定は KZN-ID なし平文。60字超は先頭優先（第48弾: 末尾優先廃止）。"""
    tz = ZoneInfo("Asia/Tokyo")
    stats = {
        "day": "2026-08-02",
        "total_minutes": 10.0,
        "by_category": {"開発": 10.0},
        "blocks": [],
        "ai": {},
    }
    body60 = "あ" * 60
    body61 = "あ" * 61
    md60 = generate_nippou_deterministic(
        stats, tz, open_kzn_actions=[("KZN-20260801-001", body60)]
    )
    md61 = generate_nippou_deterministic(
        stats, tz, open_kzn_actions=[("KZN-20260801-001", body61)]
    )
    tomorrow60 = md60.split("【明日の予定】", 1)[1]
    assert "KZN-" not in tomorrow60
    assert body60 in tomorrow60
    tomorrow61 = md61.split("【明日の予定】", 1)[1]
    line = next(ln for ln in tomorrow61.splitlines() if ln.startswith("- "))
    # 先頭優先: 行頭が … で始まらない
    assert line.startswith("- あ")
    assert not line.startswith("- …")
    assert line.endswith("…")
    assert len(line[2:]) == 60  # 59字 + ellipsis
