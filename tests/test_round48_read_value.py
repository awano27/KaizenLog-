"""第48弾: 日誌の読む価値 — digest / reorder / baseline / nippou 切詰め。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.baseline import baseline, format_with_baseline
from kaizenlog.digest import build_digest
from kaizenlog.effort import EffortReport, render_effort_markdown
from kaizenlog.memory import MemoryEntry
from kaizenlog.nippou import (
    _truncate_action_for_tomorrow,
    generate_nippou_deterministic,
)
from kaizenlog.report import DailySummary, render_markdown
from kaizenlog.vault import (
    ACTIVITY_MARKER,
    DIGEST_MARKER,
    EFFORT_MARKER,
    FOOTNOTES_MARKER,
    GOAL_MARKER,
    SECTION_ORDER,
    consolidate_disclaimers,
    extract_section,
    reorder_sections,
    upsert_section,
)

TZ = ZoneInfo("Asia/Tokyo")


def test_baseline_needs_three_days():
    hist = [
        {"day": "2026-07-26", "total_minutes": 100},
        {"day": "2026-07-27", "total_minutes": 200},
    ]
    med, lab = baseline(hist, "total_minutes", today_value=300)
    assert med is None and lab == ""

    hist.append({"day": "2026-07-28", "total_minutes": 150})
    med, lab = baseline(hist, "total_minutes", today_value=300)
    assert med == 150.0
    assert "倍" in lab


def test_format_with_baseline():
    assert format_with_baseline("863回", 512.0, "1.7倍") == (
        "863回（7日中央値 512 の 1.7倍）"
    )
    assert format_with_baseline("10回", None, "") == "10回"


def test_digest_friction_without_redactor():
    stats = {
        "source_status": "verified",
        "activity_sha256": "x",
        "total_minutes": 120.0,
        "by_category": {"AI作業": 40.0},
        "ai": {
            "session_digests": [
                {
                    "day": "2026-08-02",
                    "project": "KaizenLog",
                    "title": "secret title",
                    "source": "codex",
                    "tool_errors": 17,
                    "tools_total": 22,
                    "edits": 0,
                    "interruptions": 0,
                    "retry_touch": 0,
                    "friction": 17,
                }
            ]
        },
        "effort": {"minutes": {"KaizenLog-": 130.0, "（私的）": 50.0}},
        "outcome_git": [
            {
                "repo_label": "KaizenLog-",
                "commits": 14,
                "insertions": 100,
                "deletions": 10,
                "subjects": ["feat: hide private titles (round 46)"],
            }
        ],
        "goal_text": "KaizenLog-アプリを完成させる",
    }
    body = build_digest(
        stats,
        [
            MemoryEntry(
                id="KZN-20260801-001",
                date="2026-08-01",
                action="codexのセッションを終了するとき → git diff --stat を実行",
                status="proposed",
            )
        ],
        today=date(2026, 8, 2),
        redactor=None,
        existing_markers=set(),
        goal_text="KaizenLog-アプリを完成させる",
    )
    assert body is not None
    assert "今日いちばんの摩擦" in body
    assert "ツールエラー17/22回" in body
    assert "変更0件" in body
    assert "secret title" not in body  # タイトルは redactor 無しでは付けない
    assert "今日の1手" not in body
    assert "目標:" in body
    assert "手を動かした先" in body


def test_nippou_arrow_truncate_and_no_joined_subjects():
    text = (
        "codexのセッションを終了するとき → git diff --stat を実行して変更件数を確認し、"
        "0件なら理由を1行ターミナルにコメントとして残す"
    )
    out = _truncate_action_for_tomorrow(text)
    assert out.startswith("codex")
    assert not out.startswith("…")
    assert "→" in out
    assert "git diff" in out

    stats = {
        "day": "2026-08-02",
        "total_minutes": 200.0,
        "by_category": {"AI作業": 100.0},
        "goal_text": "done",
        "blocks": [],
        "effort": {"minutes": {"KaizenLog-": 130.0}},
        "ai": {"session_digests": []},
        "outcome_git": [
            {
                "repo_label": "KaizenLog-",
                "commits": 14,
                "insertions": 17377,
                "deletions": 805,
                "subjects": [
                    "feat: hide private titles, compress timeline (round 46)",
                    "feat: add effort allocation (round 45)",
                ],
            }
        ],
    }
    md = generate_nippou_deterministic(
        stats,
        TZ,
        open_kzn_actions=[("KZN-1", text)],
    )
    assert " / feat:" not in md  # 切詰め subject 同士の連結なし
    assert "（round" not in md  # round 識別子を落とす
    assert "hide private" in md or "私的" in md or "titles" in md
    tomorrow = md.split("【明日の予定】", 1)[1]
    line = next(ln for ln in tomorrow.splitlines() if ln.startswith("- "))
    assert not line.startswith("- …")
    assert "→" in line


def test_reorder_sections_idempotent_and_preserves_handwriting():
    content = (
        "---\ndate: 2026-08-02\n---\n\n"
        "手書きメモ keep\n\n"
        f"<!-- {ACTIVITY_MARKER}:start -->\n## act\n<!-- {ACTIVITY_MARKER}:end -->\n\n"
        "間の手書き\n\n"
        f"<!-- {DIGEST_MARKER}:start -->\n## dig\n<!-- {DIGEST_MARKER}:end -->\n\n"
        f"<!-- {EFFORT_MARKER}:start -->\n## eff\n<!-- {EFFORT_MARKER}:end -->\n"
    )
    r = reorder_sections(content)
    assert r.count("手書きメモ keep") == 1
    assert r.count("間の手書き") == 1
    assert r.index(DIGEST_MARKER) < r.index(EFFORT_MARKER) < r.index(ACTIVITY_MARKER)
    assert reorder_sections(r) == r
    # SECTION_ORDER 定数の先頭が digest
    assert SECTION_ORDER[0] == DIGEST_MARKER


def test_effort_percentages_sum_100():
    report = EffortReport(
        minutes={"A": 50.0, "B": 50.0, "C": 1.0},
        evidence={"A": {"x": 1}, "B": {"x": 1}, "C": {"x": 1}},
        unclassified_apps=[],
        total_minutes=101.0,
    )
    md = render_effort_markdown(report, min_display_minutes=0.0)
    pcts = []
    for ln in md.splitlines():
        if not ln.startswith("|") or "つけ先" in ln or "---" in ln:
            continue
        parts = [p.strip() for p in ln.strip("|").split("|")]
        if len(parts) >= 3 and parts[2].endswith("%"):
            pcts.append(int(parts[2][:-1]))
    assert pcts
    assert sum(pcts) == 100


def test_consolidate_disclaimers_moves_extra_notes():
    body = (
        "## sec\n"
        "※ first note\n"
        "※ second note about causality\n"
        "data line\n"
    )
    content = upsert_section("", ACTIVITY_MARKER, body, position="bottom")
    out = consolidate_disclaimers(content, max_inline=1)
    act = extract_section(out, ACTIVITY_MARKER) or ""
    assert "※ first note" in act
    assert "※ second" not in act
    assert "[^" in act
    fn = extract_section(out, FOOTNOTES_MARKER)
    assert fn is not None
    assert "second note" in fn


def test_timeline_no_fragment_bucket_rows():
    from kaizenlog.report import Block

    day = date(2026, 8, 2)
    blocks = [
        Block(
            start=datetime(2026, 8, 2, 10, 0, tzinfo=TZ),
            end=datetime(2026, 8, 2, 10, 1, tzinfo=TZ),
            category="AI作業",
            app="x",
            titles=["t"],
        ),
        Block(
            start=datetime(2026, 8, 2, 11, 0, tzinfo=TZ),
            end=datetime(2026, 8, 2, 11, 20, tzinfo=TZ),
            category="開発",
            app="Code",
            titles=["main"],
        ),
    ]
    s = DailySummary(
        day=day,
        total_minutes=21.0,
        by_category={"AI作業": 1.0, "開発": 20.0},
        by_app={},
        blocks=blocks,
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=1,
    )
    md = render_markdown(s, TZ, min_block_minutes=3.0)
    assert "| 細切れ |" not in md
    assert "細切れ（3分未満）" in md
    assert "開発" in md
