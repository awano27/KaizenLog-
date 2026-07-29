"""第29弾 §B: 改善風化センチネル。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.aiwork import UserPrompt
from kaizenlog.config import Config
from kaizenlog.decay import (
    DecayEvent,
    append_decay_events,
    detect_kzn_decay,
    detect_prm_decay,
    format_decay_status_line,
    format_decay_weekly_section,
    load_decay_events,
    run_decay_detection,
)
from kaizenlog.experiments import create_experiment
from kaizenlog.memory import MemoryEntry, append_entries
from kaizenlog.promptledger import PromptLedgerEntry, append_prompt_ledger
from kaizenlog.weekly_context import render_weekly_context


TZ = timezone.utc
AS_OF = date(2026, 7, 28)


def _p(text: str, day: date, hour: int = 10) -> UserPrompt:
    return UserPrompt(
        timestamp=datetime(day.year, day.month, day.day, hour, tzinfo=TZ),
        project="repo",
        text=text,
    )


def test_b4_prm_threshold_boundary(tmp_path: Path):
    mem = tmp_path / "mem"
    append_prompt_ledger(
        mem,
        [
            PromptLedgerEntry(
                id="PRM-20260701-001",
                representative="fix the flaky tests please",
                count_total=10,
                days_seen=5,
                first_seen="2026-07-01",
                last_seen="2026-07-20",
                status="skilled",
                skill_name="test-fix",
                marked_on="2026-07-10",
            )
        ],
    )
    # 2回 → 非発火
    prompts2 = [
        _p("fix the flaky tests please", AS_OF - timedelta(days=i))
        for i in range(2)
    ]
    assert detect_prm_decay(mem, prompts2, as_of=AS_OF) == []
    # 3回 → 発火
    prompts3 = [
        _p("fix the flaky tests please", AS_OF - timedelta(days=i))
        for i in range(3)
    ]
    ev = detect_prm_decay(mem, prompts3, as_of=AS_OF)
    assert len(ev) == 1
    assert ev[0].kind == "prm"
    assert ev[0].ref_id == "PRM-20260701-001"


def test_b4_kzn_min_measurable_and_majority(tmp_path: Path):
    mem = tmp_path / "mem"
    stats = tmp_path / "stats"
    stats.mkdir()
    append_entries(
        mem,
        [
            MemoryEntry(
                id="KZN-20260701-001",
                date="2026-07-01",
                action="減らす PASS: context_switches <= 10 FAIL: 超過",
                status="done",
                done_date="2026-07-02",
                verdict="pass",
                verdict_value=5.0,
                verdict_date="2026-07-02",
            )
        ],
    )
    # 2日だけ測定可能 → 判定しない
    for i in range(2):
        d = AS_OF - timedelta(days=i)
        (stats / f"{d.isoformat()}.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "day": d.isoformat(),
                    "total_minutes": 100,
                    "context_switches": 20,
                    "by_category": {},
                    "ai": {},
                }
            ),
            encoding="utf-8",
        )
    assert detect_kzn_decay(mem, stats, as_of=AS_OF) == []

    # 7日すべて違反 → 発火
    for i in range(7):
        d = AS_OF - timedelta(days=i)
        (stats / f"{d.isoformat()}.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "day": d.isoformat(),
                    "total_minutes": 100,
                    "context_switches": 20,
                    "by_category": {},
                    "ai": {},
                }
            ),
            encoding="utf-8",
        )
    ev = detect_kzn_decay(mem, stats, as_of=AS_OF)
    assert len(ev) == 1
    assert ev[0].kind == "kzn"
    assert "再悪化" in ev[0].detail


def test_b4_cooldown(tmp_path: Path):
    mem = tmp_path / "mem"
    append_prompt_ledger(
        mem,
        [
            PromptLedgerEntry(
                id="PRM-1",
                representative="hello skilled cluster text",
                count_total=5,
                days_seen=2,
                first_seen="2026-07-01",
                last_seen="2026-07-20",
                status="skilled",
                skill_name="s",
                marked_on="2026-07-01",
            )
        ],
    )
    prompts = [
        _p("hello skilled cluster text", AS_OF - timedelta(days=i))
        for i in range(3)
    ]
    cfg = Config(vault_dir=tmp_path, memory_dir="mem", experiments_dir="exp", stats_dir="stats")
    (tmp_path / "exp").mkdir()
    (tmp_path / "stats").mkdir()
    fresh1 = run_decay_detection(cfg, as_of=AS_OF, prompts=prompts)
    assert len(fresh1) == 1
    fresh2 = run_decay_detection(cfg, as_of=AS_OF, prompts=prompts)
    assert fresh2 == []  # cooldown
    all_ev = load_decay_events(mem)
    assert len([e for e in all_ev if e.ref_id == "PRM-1"]) == 1


def test_b4_weekly_and_status_and_f17(tmp_path: Path):
    mem = tmp_path / "mem"
    append_decay_events(
        mem,
        [
            DecayEvent(
                date=AS_OF.isoformat(),
                kind="kzn",
                ref_id="KZN-1",
                detail="KZN-1 の metric が再悪化（7日中5日違反）",
                evidence="context_switches <= 10",
            )
        ],
    )
    ev = load_decay_events(mem, window_days=7, as_of=AS_OF)
    assert format_decay_status_line(ev)
    assert format_decay_weekly_section(ev)
    assert format_decay_status_line([]) is None
    assert format_decay_weekly_section([]) is None

    md = render_weekly_context(
        tmp_path / "stats", mem, tmp_path / "exp", date(2026, 7, 27)
    )
    # week mon 2026-07-27 〜 2026-08-02。event 7/28 は直近7日窓内
    assert "風化した改善" in md
    assert "KZN-1" in md

    evidence = build_advice_evidence(
        {
            "version": 2,
            "day": AS_OF.isoformat(),
            "total_minutes": 100,
            "context_switches": 1,
            "blocks": [],
            "by_category": {"開発": 60},
            "by_app": {},
            "ai": {"sessions": 0},
        },
        decay_events=ev,
    )
    assert "[F17]" in evidence.markdown
    assert "風化" in evidence.markdown


def test_b4_notify_once_and_redact(tmp_path: Path):
    from kaizenlog.cli import cmd_generate
    from kaizenlog.report import DailySummary

    cfg = Config(
        vault_dir=tmp_path,
        daily_notes_dir="notes",
        stats_dir="stats",
        memory_dir="mem",
        logs_dir="logs",
        experiments_dir="exp",
    )
    for d in ("notes", "stats", "mem", "logs", "exp"):
        (tmp_path / d).mkdir()
    day = AS_OF
    summary = DailySummary(
        day=day,
        total_minutes=10,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
    )
    fresh = [
        DecayEvent(
            date=day.isoformat(),
            kind="prm",
            ref_id="PRM-X",
            detail="reoccurred",
            evidence="x",
        )
    ]
    with (
        patch("kaizenlog.cli.collect_day", return_value=([], True)),
        patch("kaizenlog.cli.collect_input", return_value=None),
        patch("kaizenlog.cli.summarize", return_value=summary),
        patch("kaizenlog.cli.render_markdown", return_value="### a\n"),
        patch("kaizenlog.cli.available_adapters", return_value=[]),
        patch("kaizenlog.cli.collect_ai_telemetry", return_value=([], [], 0)),
        patch("kaizenlog.cli.ActivityWatchClient"),
        patch("kaizenlog.cli.Classifier") as Cls,
        patch("kaizenlog.decay.run_decay_detection", return_value=fresh),
        patch("kaizenlog.cli.notify") as n,
        patch("kaizenlog.cli.load_experiments", return_value=[]),
        patch("kaizenlog.cli.detect_regressions", return_value=[]),
        patch("kaizenlog.cli.judge_entries", return_value=[]),
        patch("kaizenlog.cli.load_entries", return_value=[]),
    ):
        Cls.return_value.classify_all.return_value = []
        # run_decay is imported inside cmd_generate from .decay
        with patch("kaizenlog.cli.run_decay_detection", create=True):
            pass
        # The import is `from .decay import run_decay_detection` inside function
        with patch("kaizenlog.decay.run_decay_detection", return_value=fresh):
            cmd_generate(cfg, day)
        # decay notify + maybe loop tax none
        assert n.call_count >= 1
        titles = [c.args[0] for c in n.call_args_list]
        assert any("風化" in t for t in titles)


def test_b4_redact_on_append(tmp_path: Path):
    mem = tmp_path / "mem"
    append_prompt_ledger(
        mem,
        [
            PromptLedgerEntry(
                id="PRM-SEC",
                representative="do SECRET thing carefully",
                count_total=5,
                days_seen=2,
                first_seen="2026-07-01",
                last_seen="2026-07-20",
                status="skilled",
                skill_name="s",
                marked_on="2026-07-01",
            )
        ],
    )
    prompts = [
        _p("do SECRET thing carefully", AS_OF - timedelta(days=i)) for i in range(3)
    ]
    redactor = lambda t: t.replace("SECRET", "[R]")
    ev = detect_prm_decay(mem, prompts, as_of=AS_OF, redactor=redactor)
    assert ev
    assert "SECRET" not in ev[0].evidence
    assert "[R]" in ev[0].evidence
