"""第10弾: 計測・判定の信頼性（M1〜M5）。"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advice_format import validate_advice
from kaizenlog.aiwork_codex import CodexAdapter
from kaizenlog.classifier import known_category_names
from kaizenlog.config import DEFAULT_RULES
from kaizenlog.experiments import compute_metric, metric_from_stats
from kaizenlog.memory import MemoryEntry, append_entries, load_entries
from kaizenlog.notify import notify
from kaizenlog.report import DailySummary
from kaizenlog.runlog import load_runs, log_run, render_status
from kaizenlog.stats import build_stats, write_stats
from kaizenlog.verdict import (
    apply_verdicts_to_advice_note,
    backfill_verdicts,
    measure_day_for_entry,
)
from tests.test_advice_evidence import CURRENT, HISTORY
from tests.test_advice_format import _valid_data


UTC = timezone.utc
DAY_START = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
DAY_END = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)


def _summary(**kwargs):
    base = dict(
        day=date(2026, 7, 20),
        total_minutes=100.0,
        by_category={"開発": 50.0, "エンタメ": 0.0},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=10,
        by_site={"youtube.com": 0.0},
    )
    base.update(kwargs)
    return DailySummary(**base)


# ---- M5 ----

def test_notify_returns_false_on_nonzero_returncode(monkeypatch):
    monkeypatch.setattr("kaizenlog.notify.sys.platform", "win32")

    class R:
        returncode = 1

    monkeypatch.setattr(
        "kaizenlog.notify.subprocess.run", lambda *a, **k: R()
    )
    assert notify("t", "m") is False


def test_notify_returns_false_on_exception(monkeypatch):
    monkeypatch.setattr("kaizenlog.notify.sys.platform", "win32")

    def boom(*a, **k):
        raise OSError("fail")

    monkeypatch.setattr("kaizenlog.notify.subprocess.run", boom)
    assert notify("t", "m") is False


def test_notify_failed_logged_and_status_warns(tmp_path, monkeypatch):
    from kaizenlog.cli import _notify
    from kaizenlog.config import Config

    vault = tmp_path / "v"
    vault.mkdir()
    logs = vault / ".kaizenlog" / "logs"
    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="d",
        stats_dir=".kaizenlog/stats",
        memory_dir="m",
        logs_dir=".kaizenlog/logs",
    )
    monkeypatch.setattr("kaizenlog.cli.notify", lambda *a, **k: False)
    assert _notify(cfg, "fail", "msg") is False
    runs = load_runs(logs)
    assert any(r.get("notify_failed") for r in runs)
    text = render_status(runs)
    assert "失敗通知の送出に失敗" in text


def test_notify_failed_log_swallow_secondary(tmp_path, monkeypatch):
    from kaizenlog.cli import _safe_log_notify_failed
    from kaizenlog.config import Config

    cfg = Config(
        vault_dir=tmp_path,
        timezone="Asia/Tokyo",
        daily_notes_dir="d",
        stats_dir="s",
        memory_dir="m",
        logs_dir="logs",
    )

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr("kaizenlog.cli.log_run", boom)
    _safe_log_notify_failed(cfg, "x")  # must not raise


# ---- M3 ----

def _write_rollout(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_codex_merges_split_session_files(tmp_path):
    """同一 session_id の2ファイルで往復・ツールが合算される。"""
    root = tmp_path / "sessions"
    day = root / "2026" / "07" / "20"
    meta = {
        "type": "session_meta",
        "timestamp": "2026-07-20T10:00:00Z",
        "payload": {"session_id": "sid-merge", "cwd": "C:/p/demo"},
    }
    f1 = [
        meta,
        {
            "type": "event_msg",
            "timestamp": "2026-07-20T10:01:00Z",
            "payload": {"type": "user_message", "message": "first turn"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-20T10:01:10Z",
            "payload": {"type": "function_call", "name": "shell_command"},
        },
    ]
    f2 = [
        meta,
        {
            "type": "event_msg",
            "timestamp": "2026-07-20T10:02:00Z",
            "payload": {"type": "user_message", "message": "second turn"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-20T10:02:10Z",
            "payload": {"type": "function_call", "name": "shell_command"},
        },
    ]
    _write_rollout(day / "rollout-a.jsonl", f1)
    _write_rollout(day / "rollout-b.jsonl", f2)
    sessions = CodexAdapter(root).scan_sessions(DAY_START, DAY_END)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.user_turns == 2
    assert s.tool_counts["shell_command"] == 2


def test_codex_day_token_delta_not_full_cumulative(tmp_path):
    """前日から続く累積トークンは当日差分のみ。"""
    root = tmp_path / "sessions"
    prev = root / "2026" / "07" / "19"
    day = root / "2026" / "07" / "20"
    sid = "sid-tokens"
    _write_rollout(
        prev / "rollout-prev.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-19T23:00:00Z",
                "payload": {"session_id": sid, "cwd": "C:/p/x"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-19T23:30:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"output_tokens": 1000}},
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-19T23:31:00Z",
                "payload": {"type": "user_message", "message": "yesterday"},
            },
        ],
    )
    _write_rollout(
        day / "rollout-day.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-20T01:00:00Z",
                "payload": {"session_id": sid, "cwd": "C:/p/x"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-20T01:10:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"output_tokens": 1150}},
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-20T01:11:00Z",
                "payload": {"type": "user_message", "message": "today continue"},
            },
        ],
    )
    sessions = CodexAdapter(root).scan_sessions(DAY_START, DAY_END)
    assert len(sessions) == 1
    assert sessions[0].output_tokens == 150  # 1150 - 1000


def test_codex_api_calls_not_double_counted(tmp_path):
    """response_item と event_msg 併存時は response_item のみ。"""
    root = tmp_path / "sessions"
    day = root / "2026" / "07" / "20"
    _write_rollout(
        day / "rollout-mix.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-20T10:00:00Z",
                "payload": {"session_id": "sid-api", "cwd": "C:/p"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-20T10:01:00Z",
                "payload": {"type": "user_message", "message": "hi there"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-20T10:01:05Z",
                "payload": {"type": "agent_message"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-07-20T10:01:06Z",
                "payload": {"type": "message", "role": "assistant"},
            },
        ],
    )
    s = CodexAdapter(root).scan_sessions(DAY_START, DAY_END)[0]
    assert s.api_calls == 1  # not 2


# ---- M1 ----

def test_unknown_category_pass_is_contract_violation():
    cats = known_category_names(DEFAULT_RULES)
    evidence = build_advice_evidence(
        CURRENT, HISTORY, known_categories=cats
    )
    data = _valid_data()
    data["actions"] = [data["actions"][0]]
    data["proposals"] = [data["proposals"][0]]
    data["actions"][0]["pass"] = "category_minutes:SNS <= 30"
    data["actions"][0]["fail"] = "31分以上"
    errs = validate_advice(data, evidence)
    assert any("SNS" in e and "存在しません" in e for e in errs)


def test_known_category_zero_is_zero_not_none():
    cats = known_category_names(DEFAULT_RULES)
    v = compute_metric(
        "category_minutes:エンタメ",
        _summary(by_category={"エンタメ": 0.0}),
        [],
        known_categories=cats,
    )
    assert v == 0.0


def test_unknown_category_compute_metric_returns_none():
    cats = known_category_names(DEFAULT_RULES)
    assert (
        compute_metric(
            "category_minutes:SNS",
            _summary(),
            [],
            known_categories=cats,
        )
        is None
    )


def test_site_minutes_missing_stays_zero():
    assert (
        compute_metric("site_minutes:never-seen.example", _summary(by_site={}), [])
        == 0.0
    )


def test_compute_metric_omit_known_categories_compat():
    # 引数省略時は未知カテゴリも 0.0（後方互換）
    assert compute_metric("category_minutes:SNS", _summary(by_category={}), []) == 0.0


def test_unobserved_site_pass_rejected_at_gate():
    cats = known_category_names(DEFAULT_RULES)
    evidence = build_advice_evidence(CURRENT, HISTORY, known_categories=cats)
    data = _valid_data()
    data["actions"] = [data["actions"][0]]
    data["proposals"] = [data["proposals"][0]]
    data["actions"][0]["pass"] = "site_minutes:not-in-day.example <= 5"
    data["actions"][0]["fail"] = "6以上"
    errs = validate_advice(data, evidence)
    assert any("観測" in e for e in errs)


def test_focus_blocks_pass_rejected_without_input_watcher():
    """入力watcher無し環境で focus_blocks PASS は契約違反（永久未判定防止）。"""
    cats = known_category_names(DEFAULT_RULES)
    stats = dict(CURRENT)
    stats.pop("input", None)
    evidence = build_advice_evidence(stats, HISTORY, known_categories=cats)
    assert evidence.input_metrics_available is False
    data = _valid_data()
    data["actions"] = [data["actions"][0]]
    data["proposals"] = [data["proposals"][0]]
    data["actions"][0]["pass"] = "focus_blocks >= 1"
    data["actions"][0]["fail"] = "0回"
    errs = validate_advice(data, evidence)
    assert any("focus_blocks" in e and "計測不能" in e for e in errs)


def test_focus_blocks_pass_ok_with_input_watcher():
    cats = known_category_names(DEFAULT_RULES)
    evidence = build_advice_evidence(CURRENT, HISTORY, known_categories=cats)
    assert evidence.input_metrics_available is True
    data = _valid_data()
    data["actions"] = [data["actions"][0]]
    data["proposals"] = [data["proposals"][0]]
    data["actions"][0]["pass"] = "focus_blocks >= 1"
    data["actions"][0]["fail"] = "0回"
    assert validate_advice(data, evidence) == []


# ---- M4 ----

def test_afk_flag_in_stats_and_l12():
    summary = _summary()
    stats = build_stats(
        date(2026, 7, 20), summary, [], afk_watcher_available=False
    )
    assert stats["afk_watcher_available"] is False
    ev = build_advice_evidence(stats, HISTORY)
    assert "[L12]" in ev.markdown
    assert "AFK未計測" in ev.markdown
    assert ev.afk_watcher_available is False


def test_legacy_stats_without_afk_key_behaves_as_available():
    stats = dict(CURRENT)
    stats.pop("afk_watcher_available", None)
    ev = build_advice_evidence(stats, HISTORY)
    assert "[L12]" not in ev.markdown
    assert ev.afk_watcher_available is True


# ---- M2 ----

def test_backfill_judges_when_next_day_stats_arrive(tmp_path):
    """翌日 stats が無かった提案が、翌々日の backfill で判定される。"""
    stats_dir = tmp_path / "stats"
    # 測定日 7/21 の stats（提案 7/20 → 測定 7/21）
    write_stats(
        stats_dir,
        date(2026, 7, 21),
        _summary(day=date(2026, 7, 21), context_switches=12),
        [],
    )
    entries = [
        MemoryEntry(
            id="KZN-20260720-001",
            date="2026-07-20",
            action="x｜PASS: context_switches <= 40｜FAIL: 41",
            status="proposed",
        )
    ]
    # as_of=7/21 では測定日=7/21 の stats あり → 判定
    bf = backfill_verdicts(entries, stats_dir, date(2026, 7, 21))
    assert bf.judged_count == 1
    assert bf.judged[0].verdict == "pass"
    assert bf.judged[0].verdict_value == 12.0
    assert "judged 1" in bf.log_line()


def test_backfill_uses_done_date_plus_one_stats(tmp_path):
    """done_date ありは done_date+1 の stats で判定。"""
    stats_dir = tmp_path / "stats"
    # done 7/22 → 測定 7/23
    write_stats(
        stats_dir,
        date(2026, 7, 23),
        _summary(day=date(2026, 7, 23), context_switches=99),
        [],
    )
    # 提案翌日 7/21 の stats は別値（使われない）
    write_stats(
        stats_dir,
        date(2026, 7, 21),
        _summary(day=date(2026, 7, 21), context_switches=5),
        [],
    )
    entries = [
        MemoryEntry(
            id="KZN-20260720-001",
            date="2026-07-20",
            action="x｜PASS: context_switches <= 40｜FAIL: 41",
            status="done",
            done_date="2026-07-22",
        )
    ]
    assert measure_day_for_entry(entries[0]) == date(2026, 7, 23)
    bf = backfill_verdicts(entries, stats_dir, date(2026, 7, 24))
    assert bf.judged_count == 1
    assert bf.judged[0].verdict == "fail"
    assert bf.judged[0].verdict_value == 99.0


def test_backfill_idempotent_no_jsonl_growth(tmp_path):
    mem = tmp_path / "mem"
    stats_dir = tmp_path / "stats"
    write_stats(
        stats_dir,
        date(2026, 7, 21),
        _summary(day=date(2026, 7, 21), context_switches=10),
        [],
    )
    e = MemoryEntry(
        id="KZN-20260720-001",
        date="2026-07-20",
        action="x｜PASS: context_switches <= 40｜FAIL: 41",
        status="proposed",
    )
    bf1 = backfill_verdicts([e], stats_dir, date(2026, 7, 21))
    append_entries(mem, bf1.judged)
    loaded = load_entries(mem)
    bf2 = backfill_verdicts(loaded, stats_dir, date(2026, 7, 21))
    assert bf2.judged_count == 0
    append_entries(mem, bf2.judged)
    # 同一 verdict は増えない
    assert sum(1 for x in load_entries(mem) if x.id == e.id) == 1


def test_backfill_skip_counts_no_stats(tmp_path):
    entries = [
        MemoryEntry(
            id="KZN-20260720-001",
            date="2026-07-20",
            action="x｜PASS: context_switches <= 40｜FAIL: 41",
            status="proposed",
        )
    ]
    bf = backfill_verdicts(entries, tmp_path / "empty", date(2026, 7, 21))
    assert bf.judged_count == 0
    assert bf.skipped_no_stats == 1
    assert "no-stats 1" in bf.log_line()


def test_backfill_note_writeback_idempotent():
    from kaizenlog.vault import ADVICE_MARKER, upsert_section

    entry = MemoryEntry(
        id="KZN-20260720-001",
        date="2026-07-20",
        action="x｜PASS: context_switches <= 40｜FAIL: 41",
        status="proposed",
        verdict="pass",
        verdict_value=10.0,
        verdict_date="2026-07-21",
    )
    content = upsert_section(
        "---\n---\n",
        ADVICE_MARKER,
        "- [ ] KZN-20260720-001: x｜PASS: context_switches <= 40｜FAIL: 41\n",
    )
    once = apply_verdicts_to_advice_note(content, [entry])
    assert once and once.count("｜判定:") == 1
    twice = apply_verdicts_to_advice_note(once, [entry])
    # 冪等: 同一なら変更なし
    assert twice is None or twice.count("｜判定:") == 1
