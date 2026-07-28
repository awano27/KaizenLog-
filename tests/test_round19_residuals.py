"""第19弾: 第17-18弾残件 F1〜F3（red→green）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kaizenlog.aiwork import (
    AISession,
    RetryChain,
    UserPrompt,
    extract_session_title,
    session_digests_for_stats,
    top_friction_sessions,
)
from kaizenlog.runlog import classify_violation_kind
from kaizenlog.weekly_context import render_weekly_context


# ---- F1: classify_violation_kind --------------------------------------------


def test_f1_pass_side_machine_readable_kind():
    msg = (
        "actions[1] の pass は機械構文（指標 演算子 数値）にしてください"
        "（例: ai_tool_errors <= 60）。自由文は自動判定できず契約違反です"
    )
    assert classify_violation_kind(msg) == "pass_not_machine_readable"


def test_f1_fail_side_parse_message_not_json():
    """「機械構文として解析できません」が json に誤分類されないこと。"""
    msg = "actions[1] の fail は機械構文として解析できません（未知指標または形式不正）"
    assert classify_violation_kind(msg) == "pass_not_machine_readable"
    # Markdown 経路の文面
    msg2 = (
        "最小アクション1の FAIL: は機械構文として解析できません"
    )
    assert classify_violation_kind(msg2) == "pass_not_machine_readable"


def test_f1_true_json_errors_still_json():
    assert classify_violation_kind("日次提案の JSON オブジェクトが見つかりません") == "json"
    assert classify_violation_kind("JSON が空です") == "json"
    assert classify_violation_kind("閉じ括弧が閉じていません") == "json"


# ---- F2: retry_touch in digests ---------------------------------------------


def _sess(
    *,
    sid: str,
    project: str,
    hour: int,
    errors: int = 0,
    inter: int = 0,
) -> AISession:
    start = datetime(2026, 7, 28, hour, 0, tzinfo=timezone.utc)
    return AISession(
        session_id=sid,
        project=project,
        start=start,
        end=start + timedelta(hours=2),
        user_turns=3,
        tool_errors=errors,
        interruptions=inter,
        title="x",
    )


def test_f2_retry_touch_boosts_friction_score():
    # 低エラーだが連鎖あり → 高エラー無連鎖より上位
    with_retry = _sess(sid="r1", project="kaizen", hour=10, errors=1, inter=0)
    high_err = _sess(sid="e1", project="other", hour=12, errors=4, inter=0)
    t0 = datetime(2026, 7, 28, 10, 30, tzinfo=timezone.utc)
    chain = RetryChain(
        project="kaizen",
        prompts=[
            UserPrompt(timestamp=t0, project="kaizen", text="同じ依頼を再度"),
            UserPrompt(
                timestamp=t0 + timedelta(minutes=5),
                project="kaizen",
                text="同じ依頼を再度",
            ),
        ],
    )
    digests = session_digests_for_stats(
        [with_retry, high_err],
        "2026-07-28",
        retry_chains=[chain],
    )
    by_id = {d["session_id"]: d for d in digests}
    assert by_id["r1"]["retry_touch"] >= 1
    assert by_id["e1"]["retry_touch"] == 0
    # score: r1 = 1 + 0 + 1*5 = 6; e1 = 4
    top = top_friction_sessions(digests, limit=2)
    assert top[0]["session_id"] == "r1"


def test_f2_no_chains_retry_touch_zero():
    s = _sess(sid="s1", project="p", hour=9, errors=2)
    digests = session_digests_for_stats([s], "2026-07-28", retry_chains=[])
    assert digests[0]["retry_touch"] == 0
    assert digests[0]["friction"] == 2


def test_f2_weekly_worst_reflects_retry_touch(tmp_path):
    import json
    from datetime import date
    from pathlib import Path

    stats = tmp_path / "stats"
    stats.mkdir()
    day = date(2026, 7, 27)
    payload = {
        "version": 2,
        "day": day.isoformat(),
        "total_minutes": 100.0,
        "by_category": {"開発": 100.0},
        "ai": {
            "sessions": 2,
            "fragmented": 0,
            "tool_errors": 1,
            "interruptions": 0,
            "retry_chains": 1,
            "session_digests": [
                {
                    "day": day.isoformat(),
                    "session_id": "retry-heavy",
                    "project": "p",
                    "title": "retry session",
                    "tool_errors": 1,
                    "interruptions": 0,
                    "retry_touch": 2,
                    "edits": 0,
                },
                {
                    "day": day.isoformat(),
                    "session_id": "err-only",
                    "project": "q",
                    "title": "err only",
                    "tool_errors": 5,
                    "interruptions": 0,
                    "retry_touch": 0,
                },
            ],
        },
    }
    (stats / f"{day.isoformat()}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "suggestions.jsonl").write_text("", encoding="utf-8")
    exp = tmp_path / "exp"
    exp.mkdir()
    md = render_weekly_context(stats, mem, exp, day)
    # retry-heavy: 1+10=11 > err-only 5 → 先頭
    assert "retry session" in md
    assert md.index("retry session") < md.index("err only")


# ---- F3: shared title extraction --------------------------------------------


def test_f3_extract_session_title_strips_wrapper():
    assert extract_session_title("<command-name>x</command-name> hello") is None
    assert extract_session_title("<local-command>y</local-command>") is None
    got = extract_session_title("  設計を進めて  \n  ください  ")
    assert got is not None
    title, length = got
    assert title == "設計を進めて ください"
    assert length == len("設計を進めて ください")


def test_f3_extract_truncates_to_40():
    got = extract_session_title("Z" * 60)
    assert got is not None
    title, length = got
    assert len(title) == 40
    assert length == 60
