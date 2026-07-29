"""第27弾 §A: kaizenlog handoff — 実測教訓の冪等注入。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from kaizenlog.handoff import apply_handoff, build_agent_context_section
from kaizenlog.memory import MemoryEntry, append_entries
from kaizenlog.promptledger import PromptLedgerEntry, append_prompt_ledger
from kaizenlog.vault import AGENT_CONTEXT_MARKER, extract_section


def _write_stats(stats_dir: Path, day: date, *, retry: int = 0, errors: int = 0) -> None:
    stats_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 2,
        "day": day.isoformat(),
        "total_minutes": 100,
        "context_switches": 1,
        "ai_activity_blocks": 0,
        "by_category": {},
        "by_app": {},
        "by_site": {},
        "blocks": [],
        "ai": {
            "sessions": 1,
            "retry_chains": retry,
            "tool_errors": errors,
            "api_calls": 1,
        },
    }
    (stats_dir / f"{day.isoformat()}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def test_a5_idempotent_and_preserves_handwritten(tmp_path: Path):
    stats = tmp_path / "stats"
    mem = tmp_path / "mem"
    mem.mkdir()
    as_of = date(2026, 7, 29)
    for i in range(10):
        d = as_of - timedelta(days=i)
        _write_stats(stats, d, retry=i % 3, errors=i)
    target = tmp_path / "CLAUDE.md"
    handwritten = "# プロジェクト方針\n\n手動メモ：日本語を含む手書き行です。\n"
    target.write_text(handwritten, encoding="utf-8")
    section = build_agent_context_section(
        stats_dir=stats, memory_dir=mem, as_of=as_of
    )
    apply_handoff(target, section)
    first = target.read_text(encoding="utf-8")
    apply_handoff(target, section)
    second = target.read_text(encoding="utf-8")
    assert first == second
    assert "手動メモ：日本語を含む手書き行です。" in second
    assert "プロジェクト方針" in second
    body = extract_section(second, AGENT_CONTEXT_MARKER)
    assert body is not None
    assert "自動生成" in body
    assert "リトライ" in body


def test_a5_first_append_without_marker(tmp_path: Path):
    stats = tmp_path / "stats"
    mem = tmp_path / "mem"
    mem.mkdir()
    target = tmp_path / "AGENTS.md"
    target.write_text("hello\n", encoding="utf-8")
    section = build_agent_context_section(
        stats_dir=stats, memory_dir=mem, as_of=date(2026, 7, 29)
    )
    apply_handoff(target, section)
    text = target.read_text(encoding="utf-8")
    assert text.startswith("hello")
    assert f"<!-- {AGENT_CONTEXT_MARKER}:start -->" in text


def test_a5_empty_data_shows_no_measurement(tmp_path: Path):
    stats = tmp_path / "stats"
    stats.mkdir()
    mem = tmp_path / "mem"
    mem.mkdir()
    section = build_agent_context_section(
        stats_dir=stats, memory_dir=mem, as_of=date(2026, 7, 29)
    )
    assert section.count("(計測なし)") >= 3


def test_a5_dry_run_does_not_write(tmp_path: Path):
    stats = tmp_path / "stats"
    mem = tmp_path / "mem"
    mem.mkdir()
    target = tmp_path / "CLAUDE.md"
    original = "# keep\n"
    target.write_text(original, encoding="utf-8")
    section = build_agent_context_section(
        stats_dir=stats, memory_dir=mem, as_of=date(2026, 7, 29)
    )
    apply_handoff(target, section, dry_run=True)
    assert target.read_text(encoding="utf-8") == original


def test_a5_skilled_wait_and_fail_actions(tmp_path: Path):
    stats = tmp_path / "stats"
    mem = tmp_path / "mem"
    as_of = date(2026, 7, 29)
    _write_stats(stats, as_of, retry=2, errors=5)
    append_prompt_ledger(
        mem,
        [
            PromptLedgerEntry(
                id="PRM-20260701-001",
                representative="ニュースを要約して",
                count_total=9,
                days_seen=3,
                first_seen="2026-07-01",
                last_seen="2026-07-20",
                status="new",
            )
        ],
    )
    append_entries(
        mem,
        [
            MemoryEntry(
                id="KZN-20260728-001",
                date="2026-07-28",
                action="リトライ連鎖 <= 2（metric: ai_retry_chains）",
                status="done",
                done_date="2026-07-28",
                verdict="fail",
                verdict_value=5.0,
                verdict_date="2026-07-28",
            ),
            MemoryEntry(
                id="KZN-20260727-001",
                date="2026-07-27",
                action="リトライ連鎖 <= 2（metric: ai_retry_chains）",
                status="done",
                done_date="2026-07-27",
                verdict="fail",
                verdict_value=4.0,
                verdict_date="2026-07-27",
            ),
        ],
    )
    section = build_agent_context_section(
        stats_dir=stats, memory_dir=mem, as_of=as_of
    )
    assert "PRM-20260701-001" in section
    assert "連続FAIL" in section or "KZN-" in section
