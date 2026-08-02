"""第45弾: 読者UX — アクション「今日1件」リードと日報の可読化。"""
from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from kaizenlog.memory import MemoryEntry, render_actions_section
from kaizenlog.nippou import (
    _display_work_title,
    generate_nippou_deterministic,
)

TZ = ZoneInfo("Asia/Tokyo")


def _machine_action(
    body: str = "codexのセッションを終了するとき→git diff --stat を実行する（1分）",
    metric: str = "ai_retry_chains",
    op: str = "<=",
    val: str = "0",
    label: str = "リトライ連鎖数",
) -> str:
    return (
        f"{body}｜PASS: {metric} {op} {val}（{label}）"
        f"｜FAIL: {metric} >= 1"
    )


def test_actions_lead_with_todays_experiment_not_zero_scoreboard():
    """先頭は「今日の実験」、チェック0件の糾弾文は出さない。"""
    entries = [
        MemoryEntry(
            id="KZN-20260801-001",
            date="2026-08-01",
            action=_machine_action(),
            status="proposed",
        ),
        MemoryEntry(
            id="KZN-20260731-001",
            date="2026-07-31",
            action=_machine_action(body="別の長い行動→メモする"),
            status="proposed",
        ),
    ]
    # pad with proposed to look like a busy week (done=0)
    for i in range(6):
        d = date(2026, 7, 28) + timedelta(days=i % 3)
        entries.append(
            MemoryEntry(
                id=f"KZN-{d.strftime('%Y%m%d')}-{i+10:03d}",
                date=d.isoformat(),
                action="x→y",
                status="proposed",
            )
        )
    md = render_actions_section(entries, date(2026, 8, 2))
    assert md is not None
    assert "今日の実験" in md
    assert "チェック完了は0件" not in md
    # only one open checkbox row
    checkbox_rows = [
        ln for ln in md.splitlines() if ln.startswith("- [ ] KZN-")
    ]
    assert len(checkbox_rows) == 1
    assert "KZN-20260801-001" in checkbox_rows[0]


def test_display_work_title_strips_leading_ellipsis_and_prefers_tail():
    raw = "...みて想定通り価値のある日誌ができているか評価して改善案を提案してください。"
    out = _display_work_title(raw, max_chars=40)
    assert out is not None
    assert not out.startswith("...")
    assert "改善案" in out or "評価" in out
    # short garbage
    assert _display_work_title("A") is None
    assert _display_work_title("短い") is None


def test_nippou_skips_short_titles_and_avoids_leading_ellipsis():
    stats = {
        "day": "2026-08-02",
        "total_minutes": 120.0,
        "by_category": {"AI作業": 90.0, "開発": 30.0},
        "blocks": [],
        "ai": {
            "session_digests": [
                {
                    "project": "KaizenLog-",
                    "title": "...みて想定通り価値のある日誌ができているか評価して改善案を提案してください。",
                    "user_turns": 8,
                    "edits": 10,
                    "tools_total": 20,
                    "source": "claude-code",
                    "tests_run": False,
                },
                {
                    "project": "KaizenLog",
                    "title": "A",
                    "user_turns": 1,
                    "edits": 0,
                    "tools_total": 0,
                    "source": "codex",
                    "tests_run": False,
                },
            ],
        },
        "outcome_git": [
            {
                "repo_label": "KaizenLog-",
                "commits": 2,
                "insertions": 10,
                "deletions": 1,
                "subjects": ["feat: reader ux"],
            }
        ],
    }
    md = generate_nippou_deterministic(stats, TZ)
    # no leading-ellipsis title quote
    assert "「..." not in md
    assert "「…" not in md or "改善" in md  # tail ellipsis ok if long
    # short "A" project has no 「A」
    assert "「A」" not in md
    assert "KaizenLog-" in md
    assert "主な内容: feat: reader ux" in md


def test_nippou_tomorrow_is_plain_action_without_kzn_id():
    stats = {
        "day": "2026-08-02",
        "total_minutes": 10.0,
        "by_category": {"開発": 10.0},
        "blocks": [],
        "ai": {},
    }
    body = "codexのセッションを終了するとき → git diff --stat を実行する"
    md = generate_nippou_deterministic(
        stats,
        TZ,
        open_kzn_actions=[
            ("KZN-20260801-001", body),
            ("KZN-20260731-001", "二件目は出さない"),
        ],
    )
    tomorrow = md.split("【明日の予定】", 1)[1]
    assert "KZN-20260801-001" not in tomorrow
    assert "git diff" in tomorrow
    assert "二件目は出さない" not in tomorrow
