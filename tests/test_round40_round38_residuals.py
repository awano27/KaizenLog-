"""第40弾: 第38弾レビュー残件修正の受け入れ + 変異生存9箇所。"""
from __future__ import annotations

import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from kaizenlog.decay import detect_kzn_decay
from kaizenlog.collector import InputObservation
from kaizenlog.digest import build_digest
from kaizenlog.memory import (
    TERMINAL_STATUSES,
    MemoryEntry,
    append_entries,
    compute_streaks,
    format_lifecycle_reader_notes,
    graduate_entries,
    summarize_for_prompt,
    update_statuses_from_note,
)
from kaizenlog.outcome_git import collect_commit_stats
from kaizenlog.report import DailySummary
from kaizenlog.reliability import FailureReason, QualityState
from kaizenlog.stats import write_stats
from kaizenlog.vault import (
    ACTIONS_MARKER,
    ADVICE_MARKER,
    DIGEST_MARKER,
    GOAL_MARKER,
    DailyNoteStore,
    extract_section,
)
from kaizenlog.weekly_context import render_weekly_context


def _entry(**kw) -> MemoryEntry:
    base = dict(
        id="KZN-20260720-001",
        date="2026-07-20",
        action="改善｜PASS: context_switches <= 10｜FAIL: 11",
        status="proposed",
    )
    base.update(kw)
    return MemoryEntry(**base)


def _summary(day: date, cs: int = 5, total: float = 120.0) -> DailySummary:
    return DailySummary(
        day=day,
        total_minutes=total,
        by_category={"開発": total, "AI作業": 30.0},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=cs,
        by_site={},
    )


# ---------- §A1 digest redactor ----------


def test_a1_digest_without_redactor_has_stats_lines_no_friction():
    stats = {
        "source_status": "verified",
        "activity_sha256": "x",
        "total_minutes": 120.0,
        "by_category": {"AI作業": 40.0},
        "ai": {
            "session_digests": [
                {
                    "day": "2026-08-01",
                    "project": "p",
                    "title": "secret title",
                    "tool_errors": 5,
                    "interruptions": 0,
                    "retry_touch": 0,
                    "friction": 5,
                }
            ]
        },
    }
    body = build_digest(
        stats,
        [_entry(date="2026-08-01", status="proposed")],
        today=date(2026, 8, 1),
        redactor=None,
        existing_markers={ADVICE_MARKER},
    )
    assert body is not None
    assert "稼働" in body
    # 第48弾: 摩擦は数値のみ redactor 無しでも出す。タイトル・1手自由文は出さない
    assert "今日いちばんの摩擦" in body
    assert "「" not in body  # タイトル無し
    assert "今日の1手" not in body

    body2 = build_digest(
        stats,
        [],
        today=date(2026, 8, 1),
        redactor=lambda s: s,
        existing_markers=set(),
    )
    assert body2 is not None
    assert "今日いちばんの摩擦" in body2


# ---------- §A2 git root dedupe ----------


@pytest.mark.skipif(shutil.which("git") is None, reason="git missing")
def test_a2_root_and_subdir_dedupe(tmp_path):
    repo = tmp_path / "MyRepo"
    sub = repo / "src"
    sub.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    for k, v in (("user.email", "t@ex.com"), ("user.name", "t")):
        subprocess.run(["git", "config", k, v], cwd=repo, check=True, capture_output=True)
    (repo / "a.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True)
    env = {
        **dict(__import__("os").environ),
        "GIT_AUTHOR_DATE": "2026-08-01T12:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-08-01T12:00:00+00:00",
    }
    subprocess.run(
        ["git", "commit", "-m", "c1"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )
    stats, omitted = collect_commit_stats(
        [repo, sub], date(2026, 8, 1), tz=timezone.utc
    )
    assert omitted == 0
    assert len(stats) == 1
    assert stats[0].commits == 1
    assert stats[0].repo_label == "MyRepo"


# ---------- §A3 known_categories ----------


def test_a3_unknown_category_does_not_graduate(tmp_path):
    stats = tmp_path / "stats"
    stats.mkdir()
    e = _entry(
        id="KZN-20260720-001",
        date="2026-07-20",
        action="x｜PASS: category_minutes:未知カテゴリ <= 0｜FAIL: 1",
        status="proposed",
    )
    write_stats(stats, date(2026, 7, 21), _summary(date(2026, 7, 21)), [])
    write_stats(stats, date(2026, 7, 22), _summary(date(2026, 7, 22)), [])
    g = graduate_entries(
        [e],
        date(2026, 7, 23),
        stats_dir=stats,
        known_categories=frozenset({"開発"}),
    )
    assert not any(x.status == "graduated" for x in g)

    # 既知カテゴリ + 実測0で閾値満たす
    e2 = _entry(
        id="KZN-20260720-002",
        date="2026-07-20",
        action="x｜PASS: category_minutes:開発 <= 200｜FAIL: 201",
    )
    g2 = graduate_entries(
        [e2],
        date(2026, 7, 23),
        stats_dir=stats,
        known_categories=frozenset({"開発"}),
    )
    assert any(x.status == "graduated" and x.id == "KZN-20260720-002" for x in g2)


# ---------- §A4 day boundary ----------


def test_a4_graduation_excludes_today(tmp_path):
    stats = tmp_path / "stats"
    stats.mkdir()
    e = _entry(
        date="2026-07-20",
        action="x｜PASS: context_switches <= 10｜FAIL: 11",
    )
    # 前日まで1日、当日だけ2日目 → 卒業しない
    write_stats(stats, date(2026, 7, 21), _summary(date(2026, 7, 21), cs=5), [])
    write_stats(stats, date(2026, 7, 22), _summary(date(2026, 7, 22), cs=5), [])
    g = graduate_entries([e], date(2026, 7, 22), stats_dir=stats)
    assert not any(x.status == "graduated" for x in g)
    # today=7/23 なら 21,22 の2日で卒業
    g2 = graduate_entries([e], date(2026, 7, 23), stats_dir=stats)
    assert any(x.status == "graduated" for x in g2)


# ---------- §B1 eval words in title ----------


def test_b1_friction_title_with_kaizen_word_drops_whole_digest():
    # 第40弾 §Z4: 目標以外の評価語は digest 全体を None
    stats = {
        "source_status": "verified",
        "activity_sha256": "x",
        "total_minutes": 100.0,
        "by_category": {"AI作業": 10.0},
        "ai": {
            "session_digests": [
                {
                    "day": "2026-08-01",
                    "project": "p",
                    "title": "改善を試す",
                    "tool_errors": 3,
                    "interruptions": 0,
                    "retry_touch": 0,
                    "friction": 3,
                }
            ]
        },
    }
    body = build_digest(
        stats, [], today=date(2026, 8, 1), redactor=lambda s: s
    )
    assert body is None


# ---------- §B2 empty stats ----------


def test_b2_no_stats_keys_returns_none():
    stats = {"source_status": "verified", "activity_sha256": "x"}
    assert (
        build_digest(
            stats,
            [_entry(date="2026-08-01")],
            today=date(2026, 8, 1),
            redactor=lambda s: s,
        )
        is None
    )


# ---------- §B3 blank line cycle (unit-level delete path) ----------


def test_b3_digest_delete_no_blank_growth(tmp_path):
    from kaizenlog.vault import _end_tag, _start_tag, atomic_write_text

    notes = tmp_path / "notes"
    notes.mkdir()
    store = DailyNoteStore(notes)
    day = date(2026, 8, 1)
    base = (
        "---\ndate: 2026-08-01\n---\n"
        "handwritten\n"
        f"<!-- {GOAL_MARKER}:start -->\ngoal\n<!-- {GOAL_MARKER}:end -->\n"
    )
    store.path_for(day).write_text(base, encoding="utf-8", newline="")
    start = _start_tag(DIGEST_MARKER)
    end = _end_tag(DIGEST_MARKER)

    def insert_delete():
        c = store.read(day) or ""
        # insert at top after frontmatter-ish
        wrapped = f"{start}\n## dig\n{end}\n"
        # simplistic top insert after first ---\n block end
        idx = c.find("---\n", 4)
        insert_at = c.find("\n", idx + 4) + 1 if idx >= 0 else 0
        c2 = c[:insert_at] + wrapped + c[insert_at:]
        atomic_write_text(store.path_for(day), c2)
        # delete like cli
        content = store.read(day) or ""
        s_idx = content.find(start)
        e_idx = content.find(end)
        before = content[:s_idx]
        after = content[e_idx + len(end) :]
        if before.endswith("\n"):
            while before.endswith("\n\n"):
                before = before[:-1]
        if after.startswith("\n"):
            after = after[1:]
        atomic_write_text(store.path_for(day), before + after)

    initial = store.read(day)
    for _ in range(3):
        insert_delete()
    assert store.read(day) == initial


# ---------- §B4 terminal not in prompt verdict block ----------


def test_b4_terminal_excluded_from_recent_verdicts():
    today = date(2026, 8, 1)
    entries = [
        _entry(
            id="KZN-20260730-001",
            date="2026-07-30",
            status="graduated",
            verdict="pass",
            verdict_value=1.0,
            verdict_date="2026-07-31",
            verdict_stage="confirmed",
        ),
        _entry(
            id="KZN-20260730-002",
            date="2026-07-30",
            status="proposed",
            verdict="fail",
            verdict_value=20.0,
            verdict_date="2026-07-31",
            verdict_stage="confirmed",
        ),
    ]
    text = summarize_for_prompt(entries, today)
    assert "KZN-20260730-001" not in text or "直近の判定" not in text
    if "直近の判定" in text:
        block = text.split("## 直近の判定")[1].split("##")[0]
        assert "KZN-20260730-001" not in block
        assert "KZN-20260730-002" in block


# ---------- §B5 retired wording ----------


def test_b5_retired_note_no_internal_instruction():
    notes = format_lifecycle_reader_notes(
        [
            _entry(
                status="retired",
                closed_reason="expired",
                closed_date="2026-08-01",
            )
        ],
        today=date(2026, 8, 1),
    )
    joined = "\n".join(notes)
    assert "個別IDは出さない" not in joined
    assert "終了扱いは達成を意味しません" in joined


# ---------- §B6 CLI/note restart contract ----------


def test_b6_skipped_can_become_done_via_note_terminal_cannot():
    skipped = _entry(id="KZN-20260720-001", status="skipped")
    graduated = _entry(id="KZN-20260720-002", status="graduated")
    note = (
        "- [x] KZN-20260720-001: x\n"
        "- [x] KZN-20260720-002: y\n"
    )
    ups = update_statuses_from_note(note, [skipped, graduated], date(2026, 8, 1))
    ids = {u.id: u.status for u in ups}
    assert ids.get("KZN-20260720-001") == "done"
    assert "KZN-20260720-002" not in ids


# ---------- §C1 mutation survival tests ----------


def test_c1_streaks_terminal_does_not_break():
    """終端 status 除外: 潰すと連続が切れる。"""
    today = date(2026, 7, 28)
    # 7/25 done, 7/26 terminal only, 7/27 done → current は 7/25-27 が連続扱い
    entries = [
        _entry(id="KZN-20260725-001", date="2026-07-25", status="done", done_date="2026-07-25"),
        _entry(id="KZN-20260726-001", date="2026-07-26", status="graduated"),
        _entry(id="KZN-20260727-001", date="2026-07-27", status="done", done_date="2026-07-27"),
    ]
    s = compute_streaks(entries, today)
    assert s.current >= 2  # terminal 日をスキップして連続


def test_c1_weekly_excludes_terminal(tmp_path):
    mem = tmp_path / "m"
    st = tmp_path / "s"
    exp = tmp_path / "e"
    mem.mkdir()
    st.mkdir()
    exp.mkdir()
    week_start = date(2026, 7, 20)
    append_entries(
        mem,
        [
            _entry(id="KZN-20260721-001", date="2026-07-21", status="proposed"),
            _entry(id="KZN-20260721-002", date="2026-07-21", status="retired"),
        ],
    )
    for i in range(7):
        write_stats(st, week_start + timedelta(days=i), _summary(week_start + timedelta(days=i)), [])
    text = render_weekly_context(st, mem, exp, week_start)
    assert "終了: 1件" in text or "終了" in text
    # 現役提案は proposed のみ
    assert "週の提案: 1件" in text


def test_c1_decay_skips_terminal(tmp_path):
    mem = tmp_path / "m"
    st = tmp_path / "s"
    mem.mkdir()
    st.mkdir()
    append_entries(
        mem,
        [
            _entry(
                id="KZN-20260720-001",
                date="2026-07-20",
                status="graduated",
                verdict="pass",
                verdict_value=5.0,
                verdict_date="2026-07-21",
                verdict_stage="confirmed",
            )
        ],
    )
    as_of = date(2026, 7, 28)
    for i in range(7):
        write_stats(st, as_of - timedelta(days=i), _summary(as_of, cs=20), [])
    assert detect_kzn_decay(mem, st, as_of=as_of) == []


def test_c1_retired_boundary_2_vs_3(tmp_path):
    """§E: age=2 は退役なし、age=3 で retired。"""
    stats = tmp_path / "s"
    stats.mkdir()
    e = _entry(date="2026-07-01", action="x｜PASS: context_switches <= 10｜FAIL: 11")
    g2 = graduate_entries([e], date(2026, 7, 3), stats_dir=stats)  # age=2
    assert not any(x.status == "retired" for x in g2)
    g3 = graduate_entries([e], date(2026, 7, 4), stats_dir=stats)  # age=3
    assert any(x.status == "retired" for x in g3)


def test_c1_outcome_git_fail_closed_returncode(monkeypatch):
    import kaizenlog.outcome_git as og

    class FakeProc:
        returncode = 1
        stdout = ""

    def fake_run(*a, **k):
        cmd = a[0] if a else k.get("args", [])
        if "rev-parse" in cmd:
            class Ok:
                returncode = 0
                stdout = str(Path.cwd())
            return Ok()
        return FakeProc()

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    stats, _ = collect_commit_stats([Path.cwd()], date(2026, 8, 1), tz=timezone.utc)
    assert stats == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git missing")
def test_c1_until_boundary_excludes_next_midnight(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    for k, v in (("user.email", "t@ex.com"), ("user.name", "t")):
        subprocess.run(["git", "config", k, v], cwd=repo, check=True, capture_output=True)
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    env1 = {
        **dict(__import__("os").environ),
        "GIT_AUTHOR_DATE": "2026-08-01T23:59:59+00:00",
        "GIT_COMMITTER_DATE": "2026-08-01T23:59:59+00:00",
    }
    subprocess.run(
        ["git", "commit", "-m", "end-of-day"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env1,
    )
    (repo / "b.txt").write_text("2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    env2 = {
        **dict(__import__("os").environ),
        "GIT_AUTHOR_DATE": "2026-08-02T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-08-02T00:00:00+00:00",
    }
    subprocess.run(
        ["git", "commit", "-m", "next-day"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env2,
    )
    stats, _ = collect_commit_stats([repo], date(2026, 8, 1), tz=timezone.utc)
    assert len(stats) == 1
    assert stats[0].commits == 1  # 翌日0時は含まない


def test_c1_repo_path_from_cwd_jsonl(tmp_path):
    """cwd 付き JSONL から repo_path が立つ（None 化で落ちる）。"""
    from kaizenlog.aiwork import scan_sessions

    proj = tmp_path / "projects" / "-home-user-proj"
    proj.mkdir(parents=True)
    cwd = tmp_path / "work"
    cwd.mkdir()
    day = date(2026, 8, 1)
    start = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 1, 11, 0, tzinfo=timezone.utc)
    import json

    lines = [
        json.dumps(
            {
                "type": "user",
                "sessionId": "sess1",
                "cwd": str(cwd),
                "timestamp": start.isoformat().replace("+00:00", "Z"),
                "message": {"content": [{"type": "text", "text": "hello world"}]},
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "sess1",
                "cwd": str(cwd),
                "timestamp": end.isoformat().replace("+00:00", "Z"),
                "message": {"content": [], "usage": {"output_tokens": 10}},
            }
        ),
    ]
    (proj / "sess1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sessions = scan_sessions(proj, start, end + timedelta(hours=1))
    assert sessions
    assert sessions[0].repo_path is not None
    assert Path(sessions[0].repo_path).resolve() == cwd.resolve()


def test_c1_console_judge_confirmed_and_backfill_provisional(
    tmp_path, monkeypatch, capsys
):
    from kaizenlog import cli as cli_mod
    from kaizenlog.config import Config
    from kaizenlog.vault import ADVICE_MARKER

    vault = tmp_path / "vault"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    memory = vault / "Kaizen" / "Memory"
    memory.mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / "03 Areas" / "Kaizen Experiments").mkdir(parents=True)

    proposal = date(2026, 7, 30)
    measure = date(2026, 7, 31)
    append_entries(
        memory,
        [
            MemoryEntry(
                id="KZN-20260730-001",
                date=proposal.isoformat(),
                action="x｜PASS: context_switches <= 40｜FAIL: y",
            )
        ],
    )
    start = f"<!-- {ADVICE_MARKER}:start -->"
    end = f"<!-- {ADVICE_MARKER}:end -->"
    (daily / f"{proposal.isoformat()}.md").write_text(
        f"{start}\n- [ ] KZN-20260730-001: body\n{end}\n",
        encoding="utf-8",
    )
    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        experiments_dir="03 Areas/Kaizen Experiments",
        stats_dir=".kaizenlog/stats",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
    )
    cfg.aiwork.enabled = False

    monkeypatch.setattr(cli_mod, "collect_day", lambda *a, **k: ([], True))
    monkeypatch.setattr(cli_mod, "collect_input", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_mod,
        "summarize",
        lambda day, *a, **k: _summary(day, cs=30, total=120),
    )
    monkeypatch.setattr(cli_mod, "render_markdown", lambda *a, **k: "### a\n")
    monkeypatch.setattr(cli_mod.Classifier, "classify_all", lambda self, e: [])
    monkeypatch.setattr(cli_mod, "ActivityWatchClient", lambda url: MagicMock())

    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            # measure day == today → provisional for judge path would need today=measure
            # For confirmed: today after measure
            return datetime(2026, 8, 1, 12, 0, tzinfo=tz or ZoneInfo("Asia/Tokyo"))

    monkeypatch.setattr(cli_mod, "datetime", FixedDT)
    # pre-write stats for measure day so backfill can confirm
    write_stats(cfg.stats_path, measure, _summary(measure, cs=30), [])

    # Run generate on measure day with today after measure → confirmed
    class DayMeasure(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 1, 12, 0, tzinfo=tz or ZoneInfo("Asia/Tokyo"))

    monkeypatch.setattr(cli_mod, "datetime", DayMeasure)
    # First: generate on measure day with today=measure for provisional
    class DayProv(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 31, 21, 30, tzinfo=tz or ZoneInfo("Asia/Tokyo"))

    monkeypatch.setattr(cli_mod, "datetime", DayProv)
    capsys.readouterr()
    cli_mod.cmd_generate(cfg, measure)
    out = capsys.readouterr().out
    # judge path provisional
    assert "⏳" in out and "途中値" in out

    # next day backfill confirmed
    monkeypatch.setattr(cli_mod, "datetime", DayMeasure)
    capsys.readouterr()
    cli_mod.cmd_generate(cfg, date(2026, 8, 1))
    out2 = capsys.readouterr().out
    assert "バックフィル判定" in out2
    assert "✅" in out2 and "実測" in out2


def test_c1_prm_line_in_generate_section(tmp_path, monkeypatch):
    """generate 経路で PRM 行が ACTIVITY section へ連結される。"""
    from kaizenlog import cli as cli_mod
    from kaizenlog.aiwork import UserPrompt
    from kaizenlog.config import Config

    vault = tmp_path
    for name in ("notes", "stats", "mem", "logs", "exp"):
        (vault / name).mkdir()
    cfg = Config(
        vault_dir=vault,
        daily_notes_dir="notes",
        stats_dir="stats",
        memory_dir="mem",
        logs_dir="logs",
        experiments_dir="exp",
        timezone="UTC",
    )
    cfg.aiwork.enabled = True
    day = date(2026, 8, 1)
    prompts = [
        UserPrompt(
            text="pushしてください",
            timestamp=datetime(2026, 8, 1, 10, i, tzinfo=timezone.utc),
            project="p",
            source="claude-code",
        )
        for i in range(3)
    ]
    captured = {}

    monkeypatch.setattr(cli_mod, "collect_day", lambda *a: ([], True))
    monkeypatch.setattr(
        cli_mod,
        "collect_input_observation",
        lambda *args: InputObservation(
            QualityState.MISSING, [], None, FailureReason.INPUT_BUCKET_MISSING
        ),
    )
    monkeypatch.setattr(
        cli_mod, "summarize", lambda day, *a, **k: _summary(day)
    )
    monkeypatch.setattr(cli_mod, "render_markdown", lambda *a, **k: "### activity")
    monkeypatch.setattr(
        cli_mod,
        "collect_ai_telemetry",
        lambda *a, **k: ([], prompts, 0),
    )
    monkeypatch.setattr(cli_mod, "available_adapters", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "detect_retry_chains", lambda *a, **k: [])
    monkeypatch.setattr(
        cli_mod,
        "render_aiwork_markdown",
        lambda *a, **k: "### ai\n",
    )
    monkeypatch.setattr(
        cli_mod,
        "write_stats",
        lambda *a, **k: captured.update(activity_md=k.get("activity_md")),
    )
    monkeypatch.setattr(cli_mod, "load_stats", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "load_experiments", lambda *a: [])
    monkeypatch.setattr(cli_mod, "detect_regressions", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "judge_entries", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "load_entries", lambda *a: [])
    monkeypatch.setattr(cli_mod, "ActivityWatchClient", MagicMock)
    monkeypatch.setattr(cli_mod, "Classifier", MagicMock())
    monkeypatch.setattr("kaizenlog.decay.run_decay_detection", lambda *a, **k: [])
    monkeypatch.setattr("kaizenlog.coachledger.judge_coach_entries", lambda *a, **k: [])

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 1, 12, 0, tzinfo=tz or timezone.utc)

    monkeypatch.setattr(cli_mod, "datetime", Fixed)
    cli_mod.cmd_generate(cfg, day)
    section = captured.get("activity_md") or ""
    # activity_md may be set from write_stats; also check note
    note = DailyNoteStore(cfg.daily_notes_path).read(day) or ""
    blob = section + note
    assert "台帳の最終観測" in blob or "繰り返している依頼" in blob
