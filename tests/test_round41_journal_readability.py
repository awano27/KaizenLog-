"""第41弾: 日誌可読性（アクション平文化・タイムライン突合・日報事実化）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kaizenlog.aiwork import AISession, render_aiwork_markdown
from kaizenlog.memory import (
    MemoryEntry,
    format_action_display_lines,
    humanize_action_body,
    render_actions_section,
)
from kaizenlog.nippou import generate_nippou_deterministic
from kaizenlog.outcome_git import RepoCommitStat, _parse_numstat
from kaizenlog.report import (
    Block,
    DailySummary,
    SessionSpan,
    build_session_spans,
    render_markdown,
)
from kaizenlog.stats import build_stats

TZ = ZoneInfo("Asia/Tokyo")
UTC = timezone.utc


def _machine_action(
    body: str = "終了するとき→git diff を実行する",
    metric: str = "ai_retry_chains",
    op: str = "<=",
    val: str = "0",
    label: str = "リトライ連鎖数",
) -> str:
    return (
        f"{body}｜PASS: {metric} {op} {val}（{label}）"
        f"｜FAIL: {metric} >= 1"
    )


# ---------- §A1 ----------


def test_a1_two_line_plain_action_no_raw_syntax():
    action = _machine_action()
    lines = format_action_display_lines(
        "KZN-20260801-001", action, mark=" ", tag="8/1提案・判定 ⏳ 集計中"
    )
    assert len(lines) == 2
    assert lines[0].startswith("- [ ] KZN-20260801-001:")
    assert " → " in lines[0]
    assert "｜PASS:" not in "\n".join(lines)
    assert "｜FAIL:" not in "\n".join(lines)
    assert "ai_retry_chains" not in "\n".join(lines)
    assert "効果指標: リトライ連鎖数 を 0 以下 に" in lines[1]
    assert "8/1提案" in lines[1]


def test_a1_freeform_one_line_compat():
    action = "メモを取る（自由文・PASSなし）"
    lines = format_action_display_lines(
        "KZN-20260728-001", action, mark=" ", tag="7/28提案"
    )
    assert len(lines) == 1
    assert "メモを取る" in lines[0]
    assert "効果指標" not in lines[0]
    assert humanize_action_body(action) == action


def test_a1_render_section_hides_machine_syntax():
    e = MemoryEntry(
        id="KZN-20260801-001",
        date="2026-08-01",
        action=_machine_action(),
        status="proposed",
        verdict="pass",
        verdict_value=0.0,
        verdict_date="2026-08-02",
        verdict_stage="provisional",
    )
    md = render_actions_section([e], date(2026, 8, 3))
    assert md is not None
    assert "｜PASS:" not in md
    assert "｜FAIL:" not in md
    assert "ai_retry_chains" not in md
    assert "効果指標:" in md
    assert "KZN-20260801-001" in md


# ---------- §A2 ----------


def test_a2_summary_plain_no_internal_terms():
    entries = []
    for i in range(8):
        d = date(2026, 7, 20) + timedelta(days=i % 5)
        entries.append(
            MemoryEntry(
                id=f"KZN-{d.strftime('%Y%m%d')}-{i:03d}",
                date=d.isoformat(),
                action="x",
                status="proposed" if i > 2 else "done",
                done_date=d.isoformat() if i <= 2 else None,
                verdict="pass" if i <= 3 else None,
                verdict_value=1.0 if i <= 3 else None,
                verdict_date=d.isoformat() if i <= 3 else None,
                verdict_stage="confirmed" if i <= 3 else "confirmed",
            )
        )
    # ensure low done rate with many proposed
    md = render_actions_section(entries, date(2026, 7, 28))
    assert md is not None
    assert "消化" not in md
    assert "実行済みPASS" not in md
    assert "未実行のままPASS到達" not in md
    assert "提案し" in md
    assert "チェック完了" in md


# ---------- §B1 ----------


def test_b1_timeline_matches_compatible_session():
    start = datetime(2026, 8, 2, 8, 56, tzinfo=TZ)
    end = start + timedelta(minutes=6)
    block = Block(
        start=start,
        end=end,
        category="AI作業",
        app="ChatGPT.exe",
        titles=["ChatGPT"],
        ai=True,
        tool="chatgpt",
    )
    s = DailySummary(
        day=date(2026, 8, 2),
        total_minutes=6.0,
        by_category={"AI作業": 6.0},
        by_app={},
        blocks=[block],
        ai_tool_minutes={},
        ai_sessions=1,
        context_switches=0,
        by_site={},
    )
    spans = [
        SessionSpan(
            start=start - timedelta(minutes=1),
            end=end + timedelta(minutes=1),
            tool_class="chatgpt",
            label="proj: fix the timeline labels now",
        )
    ]
    md = render_markdown(s, TZ, min_block_minutes=3.0, session_spans=spans)
    assert "proj: fix the timeline labels now" in md
    assert "（ログなし）" not in md


def test_b1_tool_mismatch_rejected():
    start = datetime(2026, 8, 2, 8, 0, tzinfo=TZ)
    end = start + timedelta(minutes=10)
    block = Block(
        start=start,
        end=end,
        category="AI作業",
        app="ChatGPT.exe",
        titles=["ChatGPT"],
        ai=True,
        tool="chatgpt",
    )
    s = DailySummary(
        day=date(2026, 8, 2),
        total_minutes=10.0,
        by_category={"AI作業": 10.0},
        by_app={},
        blocks=[block],
        ai_tool_minutes={},
        ai_sessions=1,
        context_switches=0,
        by_site={},
    )
    # long claude session overlapping chatgpt block must NOT match
    spans = [
        SessionSpan(
            start=start - timedelta(hours=1),
            end=end + timedelta(hours=1),
            tool_class="claude",
            label="ClaudeProj: long session",
        )
    ]
    md = render_markdown(s, TZ, min_block_minutes=3.0, session_spans=spans)
    assert "ClaudeProj" not in md
    assert "（ログなし）" in md


def test_b1_no_match_suffix_and_non_ai_unchanged():
    start = datetime(2026, 8, 2, 9, 0, tzinfo=TZ)
    blocks = [
        Block(
            start=start,
            end=start + timedelta(minutes=5),
            category="AI作業",
            app="Code.exe",
            titles=["Claude"],
            ai=True,
            tool="claude",
        ),
        Block(
            start=start + timedelta(hours=1),
            end=start + timedelta(hours=1, minutes=20),
            category="開発",
            app="Code.exe",
            titles=["main.py"],
            ai=False,
        ),
    ]
    s = DailySummary(
        day=date(2026, 8, 2),
        total_minutes=25.0,
        by_category={"AI作業": 5.0, "開発": 20.0},
        by_app={},
        blocks=blocks,
        ai_tool_minutes={},
        ai_sessions=1,
        context_switches=1,
        by_site={},
    )
    md = render_markdown(s, TZ, min_block_minutes=3.0, session_spans=[])
    assert "Claude（ログなし）" in md or "（ログなし）" in md
    assert "main.py" in md
    assert "main.py（ログなし）" not in md


def test_b1_redactor_applied_in_spans():
    sess = AISession(
        session_id="s1",
        project="secret-proj",
        start=datetime(2026, 8, 2, 10, tzinfo=UTC),
        end=datetime(2026, 8, 2, 11, tzinfo=UTC),
        user_turns=3,
        title="do SECRET_TOKEN work",
        source="claude-code",
    )
    spans = build_session_spans(
        [sess], redactor=lambda t: t.replace("SECRET_TOKEN", "[REDACTED]")
    )
    assert len(spans) == 1
    assert "SECRET_TOKEN" not in spans[0].label
    assert "[REDACTED]" in spans[0].label
    assert spans[0].tool_class == "claude"


# ---------- §B2 ----------


def test_b2_unlogged_minutes_one_decimal():
    sess = [
        AISession(
            session_id="s",
            project="p",
            start=datetime(2026, 8, 1, 10, tzinfo=UTC),
            end=datetime(2026, 8, 1, 11, tzinfo=UTC),
            user_turns=1,
            source="claude-code",
        )
    ]
    md = render_aiwork_markdown(
        sess,
        UTC,
        screen_tool_minutes={"claude": 25.6511, "gemini": 0.4},
    )
    assert "25.6511" not in md
    assert "25.7分" in md
    assert "0.4" not in md  # <0.5 excluded


# ---------- §C1 ----------


def test_c1_parse_subjects_and_stats_payload():
    text = "\x01fix subject one\n1\t0\ta.py\n\x01second subject two\n2\t1\tb.py\n"
    parsed = _parse_numstat(text)
    assert parsed is not None
    commits, ins, dels, subjects = parsed
    assert commits == 2
    assert subjects == ["fix subject one", "second subject two"]
    st = RepoCommitStat("KaizenLog-", commits, ins, dels, subjects=subjects)
    payload = [
        {
            "repo_label": st.repo_label,
            "commits": st.commits,
            "insertions": st.insertions,
            "deletions": st.deletions,
            "subjects": [s.replace("fix", "[R]") for s in st.subjects],
        }
    ]
    assert payload[0]["subjects"][0].startswith("[R]")


def test_c1_build_stats_persists_outcome_git():
    day = date(2026, 8, 2)
    summary = DailySummary(
        day=day,
        total_minutes=10.0,
        by_category={"開発": 10.0},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
        by_site={},
    )
    stats = build_stats(
        day,
        summary,
        [],
        outcome_git=[
            {
                "repo_label": "KaizenLog-",
                "commits": 2,
                "insertions": 10,
                "deletions": 1,
                "subjects": ["a", "b"],
            }
        ],
    )
    assert stats["outcome_git"][0]["subjects"] == ["a", "b"]


# ---------- §C2 ----------


def test_c2_nippou_project_facts_and_commits():
    stats = {
        "day": "2026-08-02",
        "total_minutes": 180.0,
        "goal_text": "日誌を読みやすくする",
        "by_category": {"AI作業": 60.0, "開発": 100.0, "エンタメ": 20.0},
        "blocks": [
            {
                "start": "2026-08-02T01:00:00+00:00",
                "end": "2026-08-02T01:20:00+00:00",
                "category": "エンタメ",
                "app": "Steam",
                "minutes": 20.0,
                "title": "ゲーム",
            },
            {
                "start": "2026-08-02T02:00:00+00:00",
                "end": "2026-08-02T02:30:00+00:00",
                "category": "開発",
                "app": "Code",
                "minutes": 30.0,
                "title": "feature work",
            },
        ],
        "ai": {
            "sessions": 3,
            "session_digests": [
                {
                    "project": "proj-A",
                    "title": "短い",
                    "user_turns": 2,
                    "edits": 1,
                    "tools_total": 5,
                    "source": "claude-code",
                    "tests_run": True,
                },
                {
                    "project": "proj-B",
                    "title": "長いプロンプトの代表タイトルですよ",
                    "user_turns": 10,
                    "edits": 4,
                    "tools_total": 20,
                    "source": "codex",
                    "tests_run": False,
                },
                {
                    "project": "proj-A",
                    "title": "A側のもう一つの長めタイトル",
                    "user_turns": 3,
                    "edits": 0,
                    "tools_total": 2,
                    "source": "claude-code",
                    "tests_run": False,
                },
            ],
        },
        "outcome_git": [
            {
                "repo_label": "KaizenLog-",
                "commits": 6,
                "insertions": 100,
                "deletions": 20,
                "subjects": ["fix readability", "add timeline match"],
            }
        ],
    }
    md = generate_nippou_deterministic(
        stats,
        TZ,
        intent="- [ ] 明日やる手書き\n",
        open_kzn_actions=[
            ("KZN-20260801-001", "終了するとき → git diff を実行する"),
            ("KZN-20260731-001", "二件目の行動"),
        ],
    )
    # ① project rows by turns desc: B (10) before A (5)
    i_b = md.index("proj-B")
    i_a = md.index("proj-A")
    assert i_b < i_a
    assert "セッション" in md and "往復" in md
    # ② entertainment excluded from work
    assert "ゲーム" not in md
    # ③ commit subjects
    assert "fix readability" in md
    assert "主な内容:" in md
    # ④ open KZN + intent
    assert "KZN-20260801-001" in md
    assert "明日やる手書き" in md
    # goal first-ish
    assert "目標: 日誌を読みやすくする" in md
    assert "テスト実行を伴うセッション 1回" in md


def test_c2_empty_fallback():
    md = generate_nippou_deterministic(
        {"day": "2026-08-02", "total_minutes": 0, "by_category": {}, "blocks": [], "ai": {}},
        TZ,
        None,
    )
    assert "本日の計測データがありません" in md
    assert "引き続き上記対応" in md
