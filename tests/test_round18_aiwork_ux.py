"""第18弾: AI作業の質 UX — 内容列・依頼層別・成果プロキシ・週次ワースト。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.aiwork import (
    AISession,
    RetryChain,
    UserPrompt,
    prompt_length_observation,
    render_aiwork_markdown,
    scan_sessions,
    session_title_from_text,
    top_friction_sessions,
)
from kaizenlog.privacy import make_redactor
from kaizenlog.weekly_context import render_weekly_context
from tests.test_aiwork import (
    DAY_END,
    DAY_START,
    TZ,
    _assistant,
    _tool_result,
    _ts,
    _user_text,
    _write_jsonl,
)


def _sess(
    *,
    sid: str = "s1",
    project: str = "p",
    hour: int = 10,
    turns: int = 3,
    errors: int = 0,
    inter: int = 0,
    title: str | None = "hello",
    plen: int = 10,
    edits: int = 0,
    tests: bool = False,
    ended_err: bool = False,
    source: str = "claude-code",
) -> AISession:
    start = datetime(2026, 7, 28, hour, 0, tzinfo=timezone.utc)
    return AISession(
        session_id=sid,
        project=project,
        start=start,
        end=start + timedelta(hours=1),
        user_turns=turns,
        tool_errors=errors,
        interruptions=inter,
        title=title,
        first_prompt_len=plen,
        edits=edits,
        tests_run=tests,
        ended_in_error=ended_err,
        source=source,
    )


# ---- Q1 title ---------------------------------------------------------------


def test_q1_title_from_first_user_prompt(tmp_path):
    long = "A" * 60
    records = [
        _user_text("<command-name>x</command-name>", _ts(9)),  # wrapper skip
        _user_text(long, _ts(9, 1)),
        _assistant(_ts(9, 2), tools=("Edit",)),
    ]
    _write_jsonl(tmp_path / "-home-user-myproj" / "s1.jsonl", records)
    sessions = scan_sessions(tmp_path, DAY_START, DAY_END)
    assert len(sessions) == 1
    assert sessions[0].title == "A" * 40
    assert sessions[0].first_prompt_len == 60
    assert sessions[0].edits >= 1


def test_q1_title_redacted_in_markdown():
    s = _sess(title="秘密PROJECTのバグを直して")
    red = make_redactor([r"秘密PROJECT"], "[REDACTED]")
    md = render_aiwork_markdown([s], TZ, redactor=red, session_titles=True)
    assert "秘密PROJECT" not in md
    assert "[REDACTED]" in md
    assert "| 内容 |" in md


def test_q1_session_titles_off_hides_column():
    s = _sess(title="should-not-appear")
    md = render_aiwork_markdown([s], TZ, session_titles=False)
    header = next(ln for ln in md.splitlines() if ln.startswith("| 時刻"))
    assert "内容" not in header
    assert "should-not-appear" not in md
    assert "変更" in header


def test_q1_session_title_helper_truncates():
    assert len(session_title_from_text("x" * 100)) == 40


# ---- Q3 outcomes ------------------------------------------------------------


def test_q3_edit_test_and_ended_error(tmp_path):
    records = [
        _user_text("テストして", _ts(11)),
        _assistant(
            _ts(11, 1),
            tools=(),
        ),
        # manual tool_use with Bash pytest via custom assistant
        {
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": _ts(11, 2),
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "id": "m1",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": "t1",
                        "input": {"command": "pytest -q"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "id": "t2",
                        "input": {"file_path": "a.py"},
                    },
                ],
                "usage": {"output_tokens": 10},
            },
        },
        _tool_result(_ts(11, 3), is_error=True, text="fail"),
    ]
    _write_jsonl(tmp_path / "-home-user-myproj" / "s1.jsonl", records)
    sessions = scan_sessions(tmp_path, DAY_START, DAY_END)
    assert sessions[0].tests_run is True
    assert sessions[0].edits >= 1
    assert sessions[0].ended_in_error is True
    md = render_aiwork_markdown(sessions, TZ)
    assert "⚠" in md
    assert "✓" in md
    assert "変更" in md


# ---- Q2 prompt length -------------------------------------------------------


def test_q2_prompt_length_both_layers():
    short = [_sess(sid=f"s{i}", plen=20, errors=12, turns=2) for i in range(2)]
    long = [_sess(sid=f"l{i}", plen=100, errors=2, turns=5) for i in range(2)]
    line = prompt_length_observation(short + long)
    assert line is not None
    assert "短い依頼" in line and "詳細な依頼" in line
    assert "平均エラー" in line
    assert "因果" in line or "観察" in line
    md = render_aiwork_markdown(short + long, TZ)
    assert "依頼の長さ別" in md


def test_q2_prompt_length_hidden_when_one_layer():
    only_short = [_sess(sid=f"s{i}", plen=10) for i in range(3)]
    assert prompt_length_observation(only_short) is None


def test_q2_retry_chain_excerpt_redacted():
    p = UserPrompt(
        timestamp=datetime(2026, 7, 28, 10, tzinfo=timezone.utc),
        project="p",
        text="秘密TOKEN をもう一度やって",
    )
    chain = RetryChain(project="p", prompts=[p, p])
    red = make_redactor([r"秘密TOKEN"], "[REDACTED]")
    md = render_aiwork_markdown(
        [_sess()], TZ, retry_chains=[chain], redactor=red, retry_chain_count=1
    )
    assert "秘密TOKEN" not in md
    assert "[REDACTED]" in md
    assert "連鎖起点" in md


def test_q2_f11_in_evidence():
    stats = {
        "version": 2,
        "day": "2026-07-28",
        "total_minutes": 200.0,
        "context_switches": 10,
        "by_category": {"開発": 100.0},
        "ai": {
            "sessions": 4,
            "fragmented": 0,
            "tool_errors": 1,
            "interruptions": 0,
            "prompt_length_observation": (
                "依頼の長さ別: 短い依頼(80字未満) 2回（平均エラー12.0）"
                " / 詳細な依頼 2回（平均エラー2.0）。観察値のみ"
            ),
        },
    }
    ev = build_advice_evidence(stats, [])
    assert "[F11]" in ev.markdown
    assert "依頼の長さ別" in ev.markdown


# ---- Q4 weekly worst --------------------------------------------------------


def test_q4_top_friction_ordering():
    digests = [
        {"day": "2026-07-21", "project": "a", "tool_errors": 1, "interruptions": 0, "title": "low"},
        {
            "day": "2026-07-22",
            "project": "b",
            "tool_errors": 2,
            "interruptions": 2,
            "title": "high",
        },
        {
            "day": "2026-07-23",
            "project": "c",
            "tool_errors": 0,
            "interruptions": 0,
            "title": "zero",
        },
    ]
    top = top_friction_sessions(digests, limit=3)
    assert len(top) == 2  # zero score excluded
    assert top[0]["project"] == "b"  # 2+10=12
    assert top[1]["project"] == "a"


def test_q4_weekly_context_worst_section(tmp_path):
    stats = tmp_path / "stats"
    stats.mkdir()
    # Monday 2026-07-27
    day = date(2026, 7, 27)
    payload = {
        "version": 2,
        "day": day.isoformat(),
        "total_minutes": 100.0,
        "by_category": {"開発": 100.0},
        "ai": {
            "sessions": 2,
            "fragmented": 0,
            "tool_errors": 5,
            "interruptions": 1,
            "retry_chains": 0,
            "session_digests": [
                {
                    "day": day.isoformat(),
                    "project": "kaizen",
                    "title": "secret X design",
                    "tool_errors": 10,
                    "interruptions": 1,
                    "edits": 3,
                    "ended_in_error": True,
                    "tests_run": False,
                },
                {
                    "day": day.isoformat(),
                    "project": "other",
                    "title": "tiny",
                    "tool_errors": 1,
                    "interruptions": 0,
                },
            ],
        },
    }
    import json

    (stats / f"{day.isoformat()}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "suggestions.jsonl").write_text("", encoding="utf-8")
    exp = tmp_path / "exp"
    exp.mkdir()
    md = render_weekly_context(stats, mem, exp, day)
    assert "摩擦ワーストセッション" in md
    assert "kaizen" in md
    assert "score=" in md
    assert "secret X design" in md
