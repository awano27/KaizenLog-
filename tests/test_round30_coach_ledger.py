"""第30弾: コーチ効果検証台帳。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from kaizenlog.advisor import AdvisorError
from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.coach import apply_proposal, save_proposal
from kaizenlog.coachledger import (
    CoachLedgerEntry,
    WATCH_METRICS,
    append_coach_ledger,
    format_coach_status_line,
    format_coach_weekly_section,
    format_f18_lines,
    generate_rollback_proposal,
    judge_coach_entries,
    load_coach_ledger,
    record_coach_application,
)
from kaizenlog.experiments import METRIC_DESCRIPTIONS, metric_from_stats
from kaizenlog.vault import COACH_MARKER, extract_section, read_text_preserve_newlines
from kaizenlog.verdict import parse_pass_condition
from kaizenlog.weekly_context import render_weekly_context


AS_OF = date(2026, 8, 10)
APPLIED = date(2026, 8, 1)  # +7 days → 8/7, so as_of 8/10 is past window


def _stat(day: date, *, retry: int, errors: int, episodes: int | None) -> dict:
    ai: dict = {
        "sessions": 1,
        "retry_chains": retry,
        "tool_errors": errors,
    }
    if episodes is not None:
        ai["loop_tax"] = {
            "episode_count": episodes,
            "total_wasted_tokens": 0,
            "est_cost_usd": 0.0,
            "max_episode": None,
        }
    return {
        "version": 2,
        "day": day.isoformat(),
        "total_minutes": 120,
        "context_switches": 5,
        "by_category": {},
        "ai": ai,
    }


def _write_stats(stats_dir: Path, days: list[tuple[date, int, int, int | None]]) -> None:
    stats_dir.mkdir(parents=True, exist_ok=True)
    for d, r, e, ep in days:
        (stats_dir / f"{d.isoformat()}.json").write_text(
            json.dumps(_stat(d, retry=r, errors=e, episodes=ep)),
            encoding="utf-8",
        )


def test_a1_last_wins_and_legacy_compat(tmp_path: Path):
    mem = tmp_path / "mem"
    mem.mkdir()
    path = mem / "coach_ledger.jsonl"
    # 旧形式（append_md 欠落）
    path.write_text(
        json.dumps(
            {
                "id": "CCH-20260801-001",
                "applied_on": "2026-08-01",
                "proposal_file": "coach/x.md",
                "targets": ["a.md"],
                "evidence": [],
                "watch_metrics": WATCH_METRICS,
                "status": "watching",
            },
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {
                "id": "CCH-20260801-001",
                "applied_on": "2026-08-01",
                "proposal_file": "coach/x.md",
                "targets": ["a.md"],
                "evidence": [],
                "watch_metrics": WATCH_METRICS,
                "status": "pass",
                "verdict_date": "2026-08-10",
                "verdict_detail": "ok",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    entries = load_coach_ledger(mem)
    assert len(entries) == 1
    assert entries[0].status == "pass"
    assert entries[0].append_md is None


def test_a2_record_supersedes_watching(tmp_path: Path):
    mem = tmp_path / "mem"
    prop1 = save_proposal(
        mem,
        as_of=date(2026, 8, 1),
        append_md="- a\n- b\n- c",
        evidence=[{"fact_id": "F", "value": "1"}],
    )
    t = tmp_path / "CLAUDE.md"
    t.write_text("# h\n", encoding="utf-8")
    e1 = record_coach_application(
        mem,
        as_of=date(2026, 8, 1),
        proposal_path=prop1,
        targets=[t],
        evidence=[{"fact_id": "F", "value": "1"}],
        append_md="- a\n- b\n- c",
    )
    assert e1.status == "watching"
    prop2 = save_proposal(
        mem,
        as_of=date(2026, 8, 2),
        append_md="- d\n- e\n- f",
        evidence=[],
    )
    e2 = record_coach_application(
        mem,
        as_of=date(2026, 8, 2),
        proposal_path=prop2,
        targets=[t],
        evidence=[],
        append_md="- d\n- e\n- f",
    )
    ledger = load_coach_ledger(mem)
    by_id = {e.id: e for e in ledger}
    assert by_id[e1.id].status == "superseded"
    assert by_id[e2.id].status == "watching"


def test_a3_unmeasurable_vs_judge_and_majority(tmp_path: Path):
    mem = tmp_path / "mem"
    stats = tmp_path / "stats"
    mem.mkdir()
    # watching entry applied 8/1
    append_coach_ledger(
        mem,
        [
            CoachLedgerEntry(
                id="CCH-20260801-001",
                applied_on=APPLIED.isoformat(),
                proposal_file="coach/p.md",
                targets=[],
                watch_metrics=list(WATCH_METRICS),
                status="watching",
                append_md="- a\n- b\n- c",
            )
        ],
    )
    # pre baseline: 28 days of low values
    pre_rows = []
    for i in range(28):
        d = APPLIED - timedelta(days=i + 1)
        pre_rows.append((d, 2, 2, 1))
    # only 2 measurable post days → unmeasurable
    post2 = [
        (APPLIED + timedelta(days=0), 2, 2, 1),
        (APPLIED + timedelta(days=1), 2, 2, 1),
    ]
    _write_stats(stats, pre_rows + post2)
    r = judge_coach_entries(mem, stats, as_of=AS_OF)
    assert len(r) == 1
    assert r[0].status == "unmeasurable"

    # reset with 7 post days: 2/3 metrics FAIL (high values) → overall FAIL
    mem2 = tmp_path / "mem2"
    mem2.mkdir()
    append_coach_ledger(
        mem2,
        [
            CoachLedgerEntry(
                id="CCH-20260801-002",
                applied_on=APPLIED.isoformat(),
                proposal_file="coach/p.md",
                targets=[],
                watch_metrics=["ai_retry_chains", "ai_tool_errors", "loop_tax_episodes"],
                status="watching",
                append_md="- a\n- b\n- c",
            )
        ],
    )
    # high post: retry=10, errors=10, episodes=10 vs baseline 2 → index 500 → FAIL all
    post7 = [
        (APPLIED + timedelta(days=i), 10, 10, 10) for i in range(7)
    ]
    stats2 = tmp_path / "stats2"
    _write_stats(stats2, pre_rows + post7)
    r2 = judge_coach_entries(mem2, stats2, as_of=AS_OF)
    assert r2[0].status == "fail"

    # 1/3 FAIL → PASS: only retry high, others at baseline
    mem3 = tmp_path / "mem3"
    mem3.mkdir()
    append_coach_ledger(
        mem3,
        [
            CoachLedgerEntry(
                id="CCH-20260801-003",
                applied_on=APPLIED.isoformat(),
                proposal_file="coach/p.md",
                targets=[],
                watch_metrics=["ai_retry_chains", "ai_tool_errors", "loop_tax_episodes"],
                status="watching",
                append_md="- a\n- b\n- c",
            )
        ],
    )
    post_mixed = [
        (APPLIED + timedelta(days=i), 10, 2, 1) for i in range(7)
    ]
    stats3 = tmp_path / "stats3"
    _write_stats(stats3, pre_rows + post_mixed)
    r3 = judge_coach_entries(mem3, stats3, as_of=AS_OF)
    assert r3[0].status == "pass"

    # median exactly 100 → PASS
    mem4 = tmp_path / "mem4"
    mem4.mkdir()
    append_coach_ledger(
        mem4,
        [
            CoachLedgerEntry(
                id="CCH-20260801-004",
                applied_on=APPLIED.isoformat(),
                proposal_file="coach/p.md",
                targets=[],
                watch_metrics=["ai_retry_chains"],
                status="watching",
            )
        ],
    )
    post_eq = [(APPLIED + timedelta(days=i), 2, 2, 1) for i in range(7)]
    stats4 = tmp_path / "stats4"
    _write_stats(stats4, pre_rows + post_eq)
    r4 = judge_coach_entries(mem4, stats4, as_of=AS_OF)
    assert r4[0].status == "pass"
    assert "中央値100" in (r4[0].verdict_detail or "") or "100" in (
        r4[0].verdict_detail or ""
    )


def test_a3_baseline_missing_excluded(tmp_path: Path):
    mem = tmp_path / "mem"
    stats = tmp_path / "stats"
    mem.mkdir()
    append_coach_ledger(
        mem,
        [
            CoachLedgerEntry(
                id="CCH-X",
                applied_on=APPLIED.isoformat(),
                proposal_file="p",
                watch_metrics=["ai_retry_chains"],
                status="watching",
            )
        ],
    )
    # no pre stats → all days baseline missing → unmeasurable
    post7 = [(APPLIED + timedelta(days=i), 5, 0, 0) for i in range(7)]
    _write_stats(stats, post7)
    r = judge_coach_entries(mem, stats, as_of=AS_OF)
    assert r[0].status == "unmeasurable"


def test_b_rollback_generate_and_apply(tmp_path: Path):
    mem = tmp_path / "mem"
    mem.mkdir()
    append_md = "- rule one\n- rule two\n- rule three"
    # original proposal applied content
    target = tmp_path / "CLAUDE.md"
    target.write_text("# hand\n", encoding="utf-8")
    prop = save_proposal(
        mem,
        as_of=APPLIED,
        append_md=append_md,
        evidence=[{"fact_id": "F1", "value": "1"}],
    )
    apply_proposal(prop, [target])
    assert extract_section(read_text_preserve_newlines(target), COACH_MARKER)
    entry = CoachLedgerEntry(
        id="CCH-20260801-099",
        applied_on=APPLIED.isoformat(),
        proposal_file=str(prop.relative_to(mem)).replace("\\", "/"),
        targets=[str(target)],
        status="fail",
        verdict_detail="ai_retry_chains 中央値150",
        append_md=append_md,
    )
    rb = generate_rollback_proposal(mem, entry, as_of=AS_OF)
    assert rb is not None and rb.is_file()
    # 同一 id 再生成なし
    assert generate_rollback_proposal(mem, entry, as_of=AS_OF) is None

    # 一致時の除去
    # re-open applied=false for test apply
    text = rb.read_text(encoding="utf-8")
    text = text.replace("applied: true", "applied: false")
    # was never applied
    from kaizenlog.vault import atomic_write_text

    atomic_write_text(rb, text)
    apply_proposal(rb, [target])
    sec = extract_section(read_text_preserve_newlines(target), COACH_MARKER)
    assert sec is None or sec.strip() == ""

    # 不一致拒否
    target2 = tmp_path / "CLAUDE2.md"
    target2.write_text("# h\n", encoding="utf-8")
    prop2 = save_proposal(
        mem,
        as_of=date(2026, 8, 3),
        append_md="- x\n- y\n- z",
        evidence=[{"fact_id": "F", "value": "1"}],
    )
    apply_proposal(prop2, [target2])
    entry2 = CoachLedgerEntry(
        id="CCH-20260803-001",
        applied_on="2026-08-03",
        proposal_file="coach/x.md",
        status="fail",
        append_md="- old content that is gone\n- b\n- c",
        verdict_detail="fail",
    )
    rb2 = generate_rollback_proposal(mem, entry2, as_of=AS_OF)
    assert rb2 is not None
    with pytest.raises(AdvisorError, match="上書き済み"):
        apply_proposal(rb2, [target2])
    # ゼロ書き込み: 内容不変
    assert "- x" in read_text_preserve_newlines(target2)

    # 二重適用拒否
    with pytest.raises(AdvisorError, match="適用済み"):
        apply_proposal(rb, [target])


def test_c_surface_weekly_status_f18(tmp_path: Path):
    mem = tmp_path / "mem"
    mem.mkdir()
    append_coach_ledger(
        mem,
        [
            CoachLedgerEntry(
                id="CCH-1",
                applied_on="2026-07-01",
                proposal_file="p",
                status="pass",
                verdict_date="2026-07-10",
                verdict_detail="ok",
            ),
            CoachLedgerEntry(
                id="CCH-2",
                applied_on="2026-07-01",
                proposal_file="p",
                status="fail",
                verdict_date="2026-07-11",
                verdict_detail="ai_retry_chains 中央値120",
            ),
            CoachLedgerEntry(
                id="CCH-3",
                applied_on="2026-08-01",
                proposal_file="p",
                status="watching",
            ),
        ],
    )
    entries = load_coach_ledger(mem)
    assert format_coach_status_line(entries)
    sec = format_coach_weekly_section(entries)
    assert sec and "勝率" in sec and "CCH-2" in sec
    assert format_coach_weekly_section([]) is None
    f18 = format_f18_lines(entries)
    assert f18 and "[F18]" in f18[0]
    md = render_weekly_context(
        tmp_path / "s", mem, tmp_path / "e", date(2026, 7, 27)
    )
    assert "コーチ勝率" in md
    evidence = build_advice_evidence(
        {
            "version": 2,
            "day": "2026-08-10",
            "total_minutes": 100,
            "context_switches": 1,
            "blocks": [],
            "by_category": {"開発": 10},
            "by_app": {},
            "ai": {"sessions": 0},
        },
        coach_entries=entries,
    )
    assert "[F18]" in evidence.markdown


def test_d_loop_tax_metric_known_for_kzn():
    assert "loop_tax_episodes" in METRIC_DESCRIPTIONS
    s = _stat(date(2026, 8, 1), retry=1, errors=2, episodes=3)
    assert metric_from_stats("loop_tax_episodes", s) == 3.0
    assert metric_from_stats("loop_tax_episodes", {"ai": {}}) is None
    parsed = parse_pass_condition(
        "摩擦を減らす PASS: loop_tax_episodes <= 2 FAIL: 超過"
    )
    assert parsed is not None
    assert parsed[0] == "loop_tax_episodes"


def test_fail_notify_once(tmp_path: Path):
    from kaizenlog.cli import cmd_generate
    from kaizenlog.config import Config
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
    fail_entry = CoachLedgerEntry(
        id="CCH-FAIL",
        applied_on=APPLIED.isoformat(),
        proposal_file="p",
        status="fail",
        verdict_date=day.isoformat(),
        verdict_detail="bad",
        append_md="- a\n- b\n- c",
    )
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
    with (
        patch("kaizenlog.cli.collect_day", return_value=([], True)),
        patch("kaizenlog.cli.collect_input", return_value=None),
        patch("kaizenlog.cli.summarize", return_value=summary),
        patch("kaizenlog.cli.render_markdown", return_value="### a\n"),
        patch("kaizenlog.cli.available_adapters", return_value=[]),
        patch("kaizenlog.cli.collect_ai_telemetry", return_value=([], [], 0)),
        patch("kaizenlog.cli.ActivityWatchClient"),
        patch("kaizenlog.cli.Classifier") as Cls,
        patch("kaizenlog.decay.run_decay_detection", return_value=[]),
        patch(
            "kaizenlog.coachledger.judge_coach_entries",
            return_value=[fail_entry],
        ),
        patch("kaizenlog.coachledger.generate_rollback_proposal", return_value=None),
        patch("kaizenlog.cli.notify") as n,
        patch("kaizenlog.cli.load_experiments", return_value=[]),
        patch("kaizenlog.cli.detect_regressions", return_value=[]),
        patch("kaizenlog.cli.judge_entries", return_value=[]),
        patch("kaizenlog.cli.load_entries", return_value=[]),
    ):
        Cls.return_value.classify_all.return_value = []
        cmd_generate(cfg, day)
        assert n.call_count == 1
        assert "コーチ" in n.call_args[0][0]
