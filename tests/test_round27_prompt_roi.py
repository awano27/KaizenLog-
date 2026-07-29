"""第27弾 §B: プロンプト資産ROI。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from kaizenlog.aiwork import AISession, UserPrompt
from kaizenlog.promptledger import (
    PromptLedgerEntry,
    append_prompt_ledger,
    load_prompt_ledger,
    mark_prompt_entry,
)
from kaizenlog.promptroi import compute_prompt_roi, format_roi_table, format_weekly_roi_section

TZ = timezone.utc


def _p(text: str, day: date, hour: int = 10) -> UserPrompt:
    return UserPrompt(
        timestamp=datetime(day.year, day.month, day.day, hour, tzinfo=TZ),
        project="repo",
        text=text,
    )


def _s(day: date, tokens: int, sid: str = "s1") -> AISession:
    start = datetime(day.year, day.month, day.day, 9, tzinfo=TZ)
    end = datetime(day.year, day.month, day.day, 12, tzinfo=TZ)
    return AISession(
        session_id=sid,
        project="repo",
        start=start,
        end=end,
        user_turns=3,
        output_tokens=tokens,
    )


def test_b4_attribution_and_empty_ledger():
    as_of = date(2026, 7, 29)
    entries = [
        PromptLedgerEntry(
            id="PRM-20260701-001",
            representative="今日のAIニュースを要約して",
            count_total=5,
            days_seen=2,
            first_seen="2026-07-01",
            last_seen="2026-07-20",
            status="new",
        )
    ]
    prompts = [
        _p("今日のAIニュースを5件要約して", as_of - timedelta(days=1)),
        _p("今日のAIニュースを3件要約して", as_of),
        _p("全く別の依頼です", as_of),
    ]
    sessions = [
        _s(as_of - timedelta(days=1), 1000, "a"),
        _s(as_of, 2000, "b"),
    ]
    rows = compute_prompt_roi(entries, prompts, sessions, as_of=as_of)
    assert len(rows) == 1
    assert rows[0].recurrence_30d == 2
    assert rows[0].est_tokens == 3000
    assert format_roi_table([]) == "プロンプト資産ROI: （台帳なし）"
    assert format_weekly_roi_section([]) is None


def test_b4_marked_on_backward_compat(tmp_path: Path):
    """旧形式 JSONL（marked_on 無し）を読める。"""
    mem = tmp_path / "mem"
    mem.mkdir()
    path = mem / "prompt_clusters.jsonl"
    path.write_text(
        '{"id":"PRM-20260701-001","representative":"x","count_total":1,'
        '"days_seen":1,"first_seen":"2026-07-01","last_seen":"2026-07-01",'
        '"status":"new","skill_name":null}\n',
        encoding="utf-8",
    )
    entries = load_prompt_ledger(mem)
    assert len(entries) == 1
    assert entries[0].marked_on is None


def test_b4_skilled_before_after_reduction():
    marked = date(2026, 7, 15)
    # after窓完了は marked+29 日以降
    as_of = marked + timedelta(days=29)
    entry = PromptLedgerEntry(
        id="PRM-20260701-001",
        representative="テストを実行して直して",
        count_total=10,
        days_seen=5,
        first_seen="2026-06-01",
        last_seen="2026-07-28",
        status="skilled",
        skill_name="test-fix",
        marked_on=marked.isoformat(),
    )
    prompts = []
    sessions = []
    # before: 4回
    for i in range(4):
        d = marked - timedelta(days=i + 1)
        prompts.append(_p("テストを実行して直して", d, hour=10 + i))
        sessions.append(_s(d, 500, sid=f"b{i}"))
    # after: 1回
    d_after = marked + timedelta(days=2)
    prompts.append(_p("テストを実行して直して", d_after))
    sessions.append(_s(d_after, 100, sid="a0"))
    rows = compute_prompt_roi([entry], prompts, sessions, as_of=as_of)
    assert rows[0].skilled_before == 4
    assert rows[0].skilled_after == 1
    assert "削減 3回" in rows[0].skilled_effect
    # 未完了時は削減を出さない
    rows_pending = compute_prompt_roi(
        [entry], prompts, sessions, as_of=marked + timedelta(days=14)
    )
    assert "計測中" in rows_pending[0].skilled_effect
    assert "削減" not in rows_pending[0].skilled_effect


def test_b4_mark_sets_marked_on():
    e = PromptLedgerEntry(
        id="PRM-1",
        representative="r",
        count_total=1,
        days_seen=1,
        first_seen="2026-07-01",
        last_seen="2026-07-01",
    )
    m = mark_prompt_entry(e, "skilled", skill_name="s", marked_on=date(2026, 7, 20))
    assert m.marked_on == "2026-07-20"
    assert m.status == "skilled"
