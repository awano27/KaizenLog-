"""第24弾: プロンプト資産の記憶層（クラスタ台帳）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from kaizenlog.aiwork import UserPrompt
from kaizenlog.promptledger import (
    append_prompt_ledger,
    find_matching_entry,
    load_prompt_ledger,
    mark_prompt_entry,
    next_prm_id,
    resolve_prm_id,
    upsert_clusters,
)
from kaizenlog.promptmine import cluster_prompts, normalize
from kaizenlog.weekly_context import render_weekly_context


TZ = timezone.utc
DAY0 = datetime(2026, 7, 20, 10, tzinfo=TZ)


def _prompt(text: str, day_offset: int = 0, project: str = "ai-news") -> UserPrompt:
    return UserPrompt(
        timestamp=DAY0 + timedelta(days=day_offset),
        project=project,
        text=text,
    )


def test_pr1_stable_id_merges_similar_rep_next_day(tmp_path: Path):
    """翌日わずかに違う代表文でも同一 PRM-ID に合流する。"""
    mem = tmp_path / "Kaizen" / "Memory"
    day1 = date(2026, 7, 21)
    day2 = date(2026, 7, 22)
    prompts_d1 = [
        _prompt("今日のAIニュースを5件要約して", 0),
        _prompt("今日のAIニュースを8件要約して", 0),
        _prompt("今日のAIニュースを3件要約して", 1),
    ]
    c1 = [c for c in cluster_prompts(prompts_d1) if c.count >= 2]
    assert c1
    touched1 = upsert_clusters(mem, c1, as_of=day1)
    assert len(touched1) == 1
    pid = touched1[0].id
    assert pid.startswith("PRM-20260721-")

    # 翌日: 数値だけ違う類似依頼
    prompts_d2 = [
        _prompt("今日のAIニュースを12件要約して", 2),
        _prompt("今日のAIニュースを4件要約して", 2),
    ]
    c2 = cluster_prompts(prompts_d2)
    touched2 = upsert_clusters(mem, c2, as_of=day2)
    ledger = load_prompt_ledger(mem)
    assert any(e.id == pid for e in ledger)
    # 別IDが増えていない（合流）
    news_ids = [
        e.id
        for e in ledger
        if "ニュース" in e.representative or "ai" in e.representative
    ]
    assert news_ids == [pid]


def test_pr1_different_request_gets_new_id(tmp_path: Path):
    """別の依頼は別 ID になる。"""
    mem = tmp_path / "mem"
    day = date(2026, 7, 21)
    prompts = [
        _prompt("今日のAIニュースを5件要約して", 0),
        _prompt("今日のAIニュースを8件要約して", 1),
        _prompt("テストを実行してエラーを直して", 0, project="vault"),
        _prompt("テストを実行して失敗を修正して", 1, project="vault"),
    ]
    clusters = [c for c in cluster_prompts(prompts) if c.count >= 2]
    touched = upsert_clusters(mem, clusters, as_of=day)
    assert len(touched) >= 2
    ids = {e.id for e in touched}
    assert len(ids) >= 2


def test_pr1_double_upsert_same_day_is_idempotent(tmp_path: Path):
    """同日再実行で count が二重加算されない。"""
    mem = tmp_path / "mem"
    day = date(2026, 7, 21)
    prompts = [
        _prompt("今日のAIニュースを5件要約して", 0),
        _prompt("今日のAIニュースを8件要約して", 1),
        _prompt("今日のAIニュースを3件要約して", 2),
    ]
    clusters = [c for c in cluster_prompts(prompts) if c.count >= 2]
    t1 = upsert_clusters(mem, clusters, as_of=day)
    assert len(t1) == 1
    count1 = t1[0].count_total
    days1 = t1[0].days_seen
    # 二重 upsert
    t2 = upsert_clusters(mem, clusters, as_of=day)
    assert t2[0].id == t1[0].id
    assert t2[0].count_total == count1
    assert t2[0].days_seen == days1
    # ファイル行は増えても後勝ちで同じ
    ledger = load_prompt_ledger(mem)
    assert len(ledger) == 1
    assert ledger[0].count_total == count1


def test_pr1_redact_before_persist(tmp_path: Path):
    mem = tmp_path / "mem"
    day = date(2026, 7, 21)
    secret = "ACME-SECRET-CLIENT の週次報告を要約して"
    prompts = [
        _prompt(secret, 0),
        _prompt(secret.replace("週次", "日次"), 1),
        _prompt(secret, 2),
    ]
    clusters = [c for c in cluster_prompts(prompts) if c.count >= 2]

    def redactor(t: str) -> str:
        return t.replace("ACME-SECRET-CLIENT", "[REDACTED]")

    upsert_clusters(mem, clusters, as_of=day, redactor=redactor)
    ledger = load_prompt_ledger(mem)
    assert ledger
    assert "ACME-SECRET-CLIENT" not in ledger[0].representative
    assert "[REDACTED]" in ledger[0].representative or "redacted" in ledger[0].representative


def test_pr2_resolve_exact_suffix_ambiguous_missing(tmp_path: Path):
    mem = tmp_path / "mem"
    from kaizenlog.promptledger import PromptLedgerEntry

    a = PromptLedgerEntry(
        id="PRM-20260721-001",
        representative=normalize("ニュースを要約して"),
        count_total=5,
        days_seen=3,
        first_seen="2026-07-19",
        last_seen="2026-07-21",
        status="new",
    )
    b = PromptLedgerEntry(
        id="PRM-20260721-002",
        representative=normalize("テストを直して"),
        count_total=4,
        days_seen=2,
        first_seen="2026-07-20",
        last_seen="2026-07-21",
        status="new",
    )
    c = PromptLedgerEntry(
        id="PRM-20260720-001",
        representative=normalize("別件"),
        count_total=3,
        days_seen=2,
        first_seen="2026-07-18",
        last_seen="2026-07-20",
        status="dismissed",
    )
    append_prompt_ledger(mem, [a, b, c])
    entries = load_prompt_ledger(mem)

    assert resolve_prm_id("PRM-20260721-001", entries).id == "PRM-20260721-001"
    assert resolve_prm_id("002", entries).id == "PRM-20260721-002"
    # 001 は new が2件（21-001 と 20-001 は dismissed）→ 21-001 のみ new で suffix 001?
    # 20-001 は dismissed なので suffix "001" は new の 21-001 のみ
    assert resolve_prm_id("001", entries).id == "PRM-20260721-001"
    # 曖昧: 同じ suffix を持つ new を2件用意
    d = PromptLedgerEntry(
        id="PRM-20260722-001",
        representative="x",
        count_total=1,
        days_seen=1,
        first_seen="2026-07-22",
        last_seen="2026-07-22",
        status="new",
    )
    append_prompt_ledger(mem, [d])
    entries = load_prompt_ledger(mem)
    amb = resolve_prm_id("001", entries)
    assert isinstance(amb, list) and len(amb) == 2
    # new にヒットしない suffix は既処理へフォールバック（mark 訂正用途）
    assert resolve_prm_id("20-001", entries).id == "PRM-20260720-001"
    assert resolve_prm_id("PRM-20999999-999", entries) is None
    assert resolve_prm_id("", entries) is None


def test_pr2_mark_skilled_and_dismissed(tmp_path: Path):
    mem = tmp_path / "mem"
    from kaizenlog.promptledger import PromptLedgerEntry

    e = PromptLedgerEntry(
        id="PRM-20260721-001",
        representative="news summary",
        count_total=5,
        days_seen=3,
        first_seen="2026-07-19",
        last_seen="2026-07-21",
        status="new",
    )
    append_prompt_ledger(mem, [e])
    entries = load_prompt_ledger(mem)
    marked = mark_prompt_entry(entries[0], "skilled", skill_name="ai-news")
    append_prompt_ledger(mem, [marked])
    ledger = load_prompt_ledger(mem)
    assert ledger[0].status == "skilled"
    assert ledger[0].skill_name == "ai-news"

    dismissed = mark_prompt_entry(ledger[0], "dismissed")
    append_prompt_ledger(mem, [dismissed])
    ledger = load_prompt_ledger(mem)
    assert ledger[0].status == "dismissed"
    assert ledger[0].skill_name is None

    with pytest.raises(ValueError):
        mark_prompt_entry(e, "skilled", skill_name=None)


def test_pr3_weekly_context_lists_new_only(tmp_path: Path):
    mem = tmp_path / "mem"
    from kaizenlog.promptledger import PromptLedgerEntry

    append_prompt_ledger(
        mem,
        [
            PromptLedgerEntry(
                id="PRM-20260721-001",
                representative="news",
                count_total=5,
                days_seen=3,
                first_seen="2026-07-19",
                last_seen="2026-07-21",
                status="new",
            ),
            PromptLedgerEntry(
                id="PRM-20260721-002",
                representative="tests",
                count_total=4,
                days_seen=2,
                first_seen="2026-07-20",
                last_seen="2026-07-21",
                status="skilled",
                skill_name="fix-fix",
            ),
            PromptLedgerEntry(
                id="PRM-20260720-001",
                representative="other",
                count_total=2,
                days_seen=1,
                first_seen="2026-07-20",
                last_seen="2026-07-20",
                status="dismissed",
            ),
        ],
    )
    stats = tmp_path / "stats"
    stats.mkdir()
    exp = tmp_path / "exp"
    exp.mkdir()
    md = render_weekly_context(stats, mem, exp, date(2026, 7, 20))
    assert "プロンプト資産" in md
    assert "PRM-20260721-001" in md
    assert "PRM-20260721-002" not in md  # skilled は列挙しない
    assert "PRM-20260720-001" not in md  # dismissed も列挙しない
    assert "スキル化済み 1件" in md
    assert "却下 1件" in md


def test_pr3_cluster_id_preferred_over_cluster_rep(tmp_path: Path):
    from kaizenlog.promptledger import PromptLedgerEntry, representative_for_cluster_id
    from kaizenlog.promptmine import count_cluster_matches

    mem = tmp_path / "mem"
    append_prompt_ledger(
        mem,
        [
            PromptLedgerEntry(
                id="PRM-20260721-001",
                representative=normalize("今日のAIニュースを要約して"),
                count_total=5,
                days_seen=3,
                first_seen="2026-07-19",
                last_seen="2026-07-21",
                status="skilled",
                skill_name="ai-news",
            )
        ],
    )
    rep = representative_for_cluster_id(mem, "PRM-20260721-001")
    assert rep
    prompts = [
        _prompt("今日のAIニュースを5件要約して", 0),
        _prompt("完全に無関係な依頼文です", 0, project="other"),
    ]
    n = count_cluster_matches(prompts, rep)
    assert n >= 1
    # cluster_rep 手書きミスがあっても ID が正なら計測できる
    wrong_rep = normalize("存在しない代表文xyz")
    assert count_cluster_matches(prompts, wrong_rep) == 0
    assert count_cluster_matches(prompts, rep) >= 1


def test_find_matching_entry_threshold():
    from kaizenlog.promptledger import PromptLedgerEntry

    entries = [
        PromptLedgerEntry(
            id="PRM-1",
            representative=normalize("今日のAIニュースを要約して保存する"),
            count_total=3,
            days_seen=2,
            first_seen="2026-07-01",
            last_seen="2026-07-02",
        )
    ]
    close = normalize("今日のAIニュースを10件要約して保存する")
    far = normalize("完全に別のタスクを実行して結果を報告")
    assert find_matching_entry(entries, close) is not None
    assert find_matching_entry(entries, far) is None


def test_next_prm_id_increments():
    from kaizenlog.promptledger import PromptLedgerEntry

    existing = [
        PromptLedgerEntry(
            id="PRM-20260721-001",
            representative="a",
            count_total=1,
            days_seen=1,
            first_seen="2026-07-21",
            last_seen="2026-07-21",
        )
    ]
    assert next_prm_id(existing, date(2026, 7, 21)) == "PRM-20260721-002"
