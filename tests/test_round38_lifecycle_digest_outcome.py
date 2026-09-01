"""第38弾: 提案寿命管理・digest・git突合 + Phase0 残件。"""
from __future__ import annotations

import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advice_format import validate_advice
from kaizenlog.decay import detect_kzn_decay
from kaizenlog.collector import InputObservation
from kaizenlog.digest import build_digest
from kaizenlog.memory import (
    MemoryEntry,
    append_entries,
    compute_action_stats,
    compute_streaks,
    format_lifecycle_reader_notes,
    graduate_entries,
    open_actions_in_window,
    render_actions_section,
)
from kaizenlog.outcome_git import collect_commit_stats
from kaizenlog.report import DailySummary
from kaizenlog.reliability import FailureReason, QualityState
from kaizenlog.stats import write_stats
from kaizenlog.vault import (
    ADVICE_MARKER,
    DIGEST_MARKER,
    GOAL_MARKER,
    DailyNoteStore,
    extract_section,
)
from kaizenlog.verdict import backfill_verdicts, judge_entries
from kaizenlog.weekly_context import render_weekly_context
from tests.test_advice_format import _evidence, _valid_data


def _entry(**kwargs) -> MemoryEntry:
    values = dict(
        id="KZN-20260720-001",
        date="2026-07-20",
        action="改善｜PASS: context_switches <= 10｜FAIL: 11",
        status="proposed",
    )
    values.update(kwargs)
    return MemoryEntry(**values)


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


# ---------- §Z1 ----------


def test_z1_shortfall_only_when_numerator_present_and_denominator_low():
    entry = _entry(
        id="KZN-20260729-010",
        date="2026-07-29",
        action="切替｜PASS: context_switches_per_hour <= 50｜FAIL: 51",
    )
    # 分母不足・分子あり
    hist_ok = [{"day": "2026-07-30", "context_switches": 100, "total_minutes": 45.0}]
    out = render_actions_section([entry], date(2026, 7, 31), stats_history=hist_ok)
    assert out and "判定不成立" in out and "稼働45" in out

    # 分子欠落・稼働300 → 注記なし
    hist_miss = [{"day": "2026-07-30", "total_minutes": 300.0}]
    out2 = render_actions_section([entry], date(2026, 7, 31), stats_history=hist_miss)
    assert out2 and "判定不成立" not in out2 and "7/29提案" in out2

    # per_session: sessions=0 かつ tool_errors あり
    e2 = _entry(
        id="KZN-20260729-011",
        date="2026-07-29",
        action="err｜PASS: ai_tool_errors_per_session <= 1｜FAIL: 2",
    )
    hist_s0 = [
        {"day": "2026-07-30", "ai": {"tool_errors": 5, "sessions": 0}}
    ]
    out3 = render_actions_section([e2], date(2026, 7, 31), stats_history=hist_s0)
    assert out3 and "判定不成立" in out3 and "AIセッション0" in out3

    hist_no_err = [{"day": "2026-07-30", "ai": {"sessions": 0}}]
    out4 = render_actions_section([e2], date(2026, 7, 31), stats_history=hist_no_err)
    assert out4 and "判定不成立" not in out4


# ---------- §Z2 ----------


def test_z2_f10_label_matches_actual_window_length():
    def day_stats(d: str, cs: float = 100.0) -> dict:
        return {
            "day": d,
            "total_minutes": 180.0,
            "context_switches": cs,
            "by_category": {"開発": 100.0},
            "ai": {
                "sessions": 2,
                "fragmented": 0,
                "tool_errors": 1,
                "interruptions": 0,
            },
            "input": {
                "focus_blocks": 2,
                "focus_minutes": 30,
                "keypresses": 100,
                "active_input_minutes": 60,
            },
        }

    current = day_stats("2026-08-01")
    hist3 = [day_stats(f"2026-07-{28+i}", 100 + i) for i in range(3)]
    hist8 = [day_stats((date(2026, 7, 24) + timedelta(days=i)).isoformat()) for i in range(8)]
    hist20 = [day_stats((date(2026, 7, 12) + timedelta(days=i)).isoformat()) for i in range(20)]

    for hist, expect_n in ((hist3, 3), (hist8, 8), (hist20, 14)):
        ev = build_advice_evidence(current, hist, source_status="verified")
        f10 = [ln for ln in ev.markdown.splitlines() if "[F10]" in ln]
        assert f10, f"expected F10 for {expect_n}"
        assert f"過去{expect_n}日中央値" in f10[0]


# ---------- §Z3 ----------


def test_z3_trajectory_uses_latest_five_days():
    action = "改善｜PASS: context_switches <= 10｜FAIL: 11"
    # 提案日を recent 窓内に置き、判定後8日分の最新5日を固定
    entry = _entry(
        id="KZN-20260720-001",
        date="2026-07-20",
        action=action,
        verdict="pass",
        verdict_value=5.0,
        verdict_date="2026-07-20",
        verdict_stage="confirmed",
    )
    hist = []
    for i in range(1, 9):
        d = date(2026, 7, 20) + timedelta(days=i)
        hist.append(
            {
                "day": d.isoformat(),
                "context_switches": 100 + i,  # all fail
                "total_minutes": 120.0,
            }
        )
    # target を提案+1日に近づけ、pass_achieved に載るよう status は proposed のまま
    # partition: recent = target-7 .. target-1 → target=7/28 なら 7/21-7/27 が recent
    # 提案日 7/20 は stale になるので、表示経路を直接 _post_verdict_trajectory_lines で検証
    from kaizenlog.memory import _post_verdict_trajectory_lines, _stats_by_day

    lines = _post_verdict_trajectory_lines(
        entry, date(2026, 7, 29), _stats_by_day(hist)
    )
    assert lines
    joined = "\n".join(lines)
    assert "判定後の実測:" in joined
    assert "7/24" in joined and "7/28" in joined
    assert "7/21" not in joined


def test_z3_trajectory_all_unmeasurable_omits_line():
    entry = _entry(
        verdict="pass",
        verdict_value=5.0,
        verdict_date="2026-07-20",
        verdict_stage="confirmed",
        action="改善｜PASS: focus_blocks >= 1｜FAIL: 0",
    )
    hist = [{"day": "2026-07-21", "total_minutes": 100.0}]  # no input
    out = render_actions_section(
        [entry], date(2026, 7, 25), stats_history=hist
    )
    assert out is not None
    assert "判定後の実測" not in out


# ---------- §Z4 fragments (decay provisional / falsifier / skilled) ----------


def test_z4_decay_excludes_provisional(tmp_path):
    mem = tmp_path / "m"
    st = tmp_path / "s"
    mem.mkdir()
    st.mkdir()
    append_entries(
        mem,
        [
            _entry(
                status="proposed",
                verdict="pass",
                verdict_value=5.0,
                verdict_date="2026-07-21",
                verdict_stage="provisional",
            )
        ],
    )
    as_of = date(2026, 7, 28)
    for i in range(7):
        write_stats(st, as_of - timedelta(days=i), _summary(as_of, cs=20), [])
    assert detect_kzn_decay(mem, st, as_of=as_of) == []


def test_z4_falsifier_contract_sides():
    data = _valid_data()
    evidence = _evidence()
    del data["actions"][0]["falsifier"]
    assert any("falsifier" in e for e in validate_advice(data, evidence))
    data = _valid_data()
    data["actions"][0]["falsifier"] = "あ" * 51
    assert any("50字" in e for e in validate_advice(data, evidence))
    data = _valid_data()
    data["actions"][0]["falsifier"] = "一行\n二行"
    assert any("falsifier" in e or "改行" in e for e in validate_advice(data, evidence))
    data = _valid_data()
    data["actions"][0]["mechanism"] = "一行\n二行"
    assert any("mechanism" in e or "改行" in e for e in validate_advice(data, evidence))


# ---------- §A1-A3 ----------


def test_a1_terminal_statuses_excluded_from_active_denominators(tmp_path):
    today = date(2026, 8, 1)
    terms = [
        _entry(id=f"KZN-2026072{i}-00{i}", status=st, date="2026-07-25")
        for i, st in enumerate(("unmeasurable", "graduated", "retired"), start=1)
    ]
    active = _entry(id="KZN-20260725-099", status="proposed", date="2026-07-25")
    entries = terms + [active]
    stats = compute_action_stats(entries, today)
    assert stats.proposed == 1
    assert stats.skipped == 0
    assert open_actions_in_window(entries, today)
    assert all(e.status == "proposed" for e in open_actions_in_window(entries, today))
    streaks = compute_streaks(entries, today)
    # terminal は連続を切らない（current は 0 以上の実値）
    assert streaks.current >= 0
    assert streaks.best >= 0

    judged = judge_entries(
        terms, date(2026, 7, 25), _summary(date(2026, 7, 26)), [], None, date(2026, 7, 26)
    )
    assert judged == []

    (tmp_path / "stats").mkdir(exist_ok=True)
    write_stats(tmp_path / "stats", date(2026, 7, 26), _summary(date(2026, 7, 26)), [])
    bf = backfill_verdicts(terms, tmp_path / "stats", date(2026, 7, 28))
    assert bf.judged_count == 0


def test_a2_unmeasurable_after_three_days_idempotent(tmp_path):
    stats = tmp_path / "stats"
    stats.mkdir()
    today = date(2026, 7, 25)
    free = _entry(
        id="KZN-20260722-001",
        date="2026-07-22",
        action="自由文PASS: 集中する",  # no machine pass
        status="proposed",
    )
    machine = _entry(
        id="KZN-20260710-001",
        date="2026-07-10",
        action="改善｜PASS: context_switches <= 10｜FAIL: 11",
        status="proposed",
    )
    # day 2: no transition for free (age=2 from 22 to 24)
    g = graduate_entries([free], date(2026, 7, 24), stats_dir=stats)
    assert g == []
    # day 3
    g = graduate_entries([free, machine], today, stats_dir=stats)
    unm = [x for x in g if x.status == "unmeasurable"]
    assert len(unm) == 1 and unm[0].id == "KZN-20260722-001"
    assert unm[0].closed_reason == "no_machine_pass"
    # machine not unmeasurable
    assert all(x.id != "KZN-20260710-001" or x.status != "unmeasurable" for x in g)
    # idempotent
    again = graduate_entries(g + [machine], today, stats_dir=stats)
    assert not any(x.id == "KZN-20260722-001" for x in again)


def test_a3_graduated_and_retired(tmp_path):
    stats = tmp_path / "stats"
    stats.mkdir()
    # 2 day pass → graduated
    e = _entry(
        id="KZN-20260720-001",
        date="2026-07-20",
        action="改善｜PASS: context_switches <= 10｜FAIL: 11",
        status="proposed",
    )
    write_stats(stats, date(2026, 7, 21), _summary(date(2026, 7, 21), cs=5), [])
    write_stats(stats, date(2026, 7, 22), _summary(date(2026, 7, 22), cs=4), [])
    g = graduate_entries([e], date(2026, 7, 23), stats_dir=stats)
    assert len(g) == 1 and g[0].status == "graduated"
    notes = format_lifecycle_reader_notes(g, today=date(2026, 7, 23))
    text = "\n".join(notes)
    assert "実行の有無は問いません" in text
    assert "習慣" not in text and "達成" not in text
    assert "experiment new" in text and "--title" in text

    # 1 day only → no graduate（別 stats で汚染しない）
    stats1 = tmp_path / "stats1"
    stats1.mkdir()
    e2 = _entry(id="KZN-20260720-002", date="2026-07-20")
    write_stats(stats1, date(2026, 7, 21), _summary(date(2026, 7, 21), cs=5), [])
    g2 = graduate_entries([e2], date(2026, 7, 22), stats_dir=stats1)
    assert not any(x.status == "graduated" for x in g2)

    # 3日以上 → retired（§E: 測定日が無い未チェックも退役。closed_reason 付き）
    stats2 = tmp_path / "stats2"
    stats2.mkdir()
    e3 = _entry(id="KZN-20260701-001", date="2026-07-01")
    g3 = graduate_entries([e3], date(2026, 7, 4), stats_dir=stats2)
    assert any(
        x.status == "retired"
        and x.id == "KZN-20260701-001"
        and x.closed_reason == "unchecked_no_measurement"
        for x in g3
    )


def test_a1_cli_done_skip_terminal_exit_1(tmp_path, monkeypatch, capsys):
    from kaizenlog import cli as cli_mod
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
    append_entries(
        cfg.memory_path,
        [_entry(id="KZN-20260720-001", status="graduated", closed_reason="metric_sustained")],
    )
    assert cli_mod.cmd_done(cfg, "KZN-20260720-001", date(2026, 8, 1)) == 1
    assert "終了済み" in capsys.readouterr().err
    assert cli_mod.cmd_skip(cfg, "KZN-20260720-001") == 1


# ---------- §B1-B2 ----------


def test_b1_digest_none_and_no_eval_words():
    redactor = lambda s: s
    assert build_digest(None, [], today=date(2026, 8, 1), redactor=redactor) is None
    stats = {
        "source_status": "verified",
        "activity_sha256": "abc",
        "total_minutes": 120.0,
        "by_category": {"AI作業": 40.0},
        "ai": {"session_digests": []},
    }
    body = build_digest(
        stats,
        [
            MemoryEntry(
                id="KZN-20260801-001",
                date="2026-08-01",
                action="trigger→close tabs before session",
                status="proposed",
            )
        ],
        today=date(2026, 8, 1),
        redactor=redactor,
        existing_markers={ADVICE_MARKER},
    )
    assert body is not None
    assert "摩擦" not in body  # 0件なら行なし
    # 決定論行（1手の行動文は自由記述のため検査外）に評価語が無い
    deterministic = "\n".join(
        ln for ln in body.splitlines() if not ln.startswith("- 今日の1手:")
    )
    assert "良い" not in deterministic and "悪い" not in deterministic
    assert "🚀" in body


def test_b2_digest_write_preserves_outside_bytes(tmp_path):
    from kaizenlog.vault import upsert_section

    notes = tmp_path / "notes"
    notes.mkdir()
    store = DailyNoteStore(notes)
    day = date(2026, 8, 1)
    handwritten = "手書き行 keep me\n"
    content = (
        "---\ndate: 2026-08-01\n---\n"
        + handwritten
        + f"<!-- {GOAL_MARKER}:start -->\n## goal\n<!-- {GOAL_MARKER}:end -->\n"
        + f"<!-- {ADVICE_MARKER}:start -->\n## advice\n<!-- {ADVICE_MARKER}:end -->\n"
    )
    store.path_for(day).write_text(content, encoding="utf-8", newline="")

    body = "## ⏱ 30秒サマリ\n\n- 稼働: 2h00m\n"
    store.write_section(day, DIGEST_MARKER, body, position="top")
    store.write_section(day, DIGEST_MARKER, body + "\n- 未完了: 1件\n", position="top")
    final = store.read(day) or ""
    assert "手書き行 keep me" in final
    assert extract_section(final, GOAL_MARKER) is not None
    assert extract_section(final, ADVICE_MARKER) is not None
    # digest が存在する
    assert extract_section(final, DIGEST_MARKER) is not None
    # goal/advice 内容
    assert "goal" in (extract_section(final, GOAL_MARKER) or "")
    assert "advice" in (extract_section(final, ADVICE_MARKER) or "")


# ---------- §C1-C3 ----------


def test_c1_repo_path_from_cwd(tmp_path):
    from kaizenlog.aiwork import AISession

    s = AISession(
        session_id="x",
        project="proj",
        start=datetime.now(timezone.utc),
        end=datetime.now(timezone.utc),
        repo_path=str(tmp_path),
    )
    assert s.repo_path == str(tmp_path)
    assert Path(s.repo_path).name == tmp_path.name
    s2 = AISession(
        session_id="y",
        project="p",
        start=datetime.now(timezone.utc),
        end=datetime.now(timezone.utc),
    )
    assert s2.repo_path is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_c2_collect_commit_stats_real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    f = repo / "a.txt"
    f.write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True)
    # fixed date env for commit
    env = {
        **dict(__import__("os").environ),
        "GIT_AUTHOR_DATE": "2026-08-01T12:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-08-01T12:00:00+00:00",
    }
    subprocess.run(
        ["git", "commit", "-m", "msg should not appear"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )
    stats, omitted = collect_commit_stats(
        [repo], date(2026, 8, 1), tz=timezone.utc, timeout=5.0
    )
    assert omitted == 0
    assert len(stats) == 1
    assert stats[0].commits == 1
    assert stats[0].insertions >= 1
    assert stats[0].repo_label == "repo"
    assert "\\" not in stats[0].repo_label and "/" not in stats[0].repo_label

    # non-repo dropped
    stats2, _ = collect_commit_stats(
        [tmp_path / "not-a-repo"], date(2026, 8, 1), tz=timezone.utc
    )
    assert stats2 == []


def test_c3_render_has_disclaimer_no_forbidden(tmp_path):
    from kaizenlog.aiwork import AISession, render_aiwork_markdown
    from kaizenlog.outcome_git import RepoCommitStat

    sess = AISession(
        session_id="s1",
        project="p",
        start=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
        end=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
        user_turns=2,
    )
    md = render_aiwork_markdown(
        [sess],
        timezone.utc,
        commit_stats=[
            RepoCommitStat(repo_label="KaizenLog-", commits=2, insertions=10, deletions=1)
        ],
        commit_repos_omitted=1,
    )
    assert "📦 当日のコミット" in md
    assert "因果は判定しません" in md
    assert "上限のため省略" in md
    assert "msg should not" not in md
    assert "C:\\" not in md and "/home/" not in md
    # empty → no line
    md2 = render_aiwork_markdown([sess], timezone.utc, commit_stats=None)
    assert "📦 当日のコミット" not in md2


def test_c3_outcome_git_false_no_subprocess(monkeypatch, tmp_path):
    from kaizenlog import cli as cli_mod
    from kaizenlog.config import Config
    from unittest.mock import MagicMock

    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise AssertionError("should not call git")

    monkeypatch.setattr("kaizenlog.outcome_git.collect_commit_stats", boom)
    monkeypatch.setattr(cli_mod, "collect_day", lambda *a: ([], True))
    monkeypatch.setattr(
        cli_mod,
        "collect_input_observation",
        lambda *args: InputObservation(
            QualityState.MISSING, [], None, FailureReason.INPUT_BUCKET_MISSING
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "summarize",
        lambda day, *a, **k: _summary(day),
    )
    monkeypatch.setattr(cli_mod, "render_markdown", lambda *a, **k: "### a")
    monkeypatch.setattr(cli_mod, "load_stats", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "write_stats", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "load_experiments", lambda *a: [])
    monkeypatch.setattr(cli_mod, "detect_regressions", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "judge_entries", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "load_entries", lambda *a: [])
    monkeypatch.setattr(cli_mod, "ActivityWatchClient", MagicMock)
    monkeypatch.setattr(cli_mod, "Classifier", MagicMock())
    monkeypatch.setattr("kaizenlog.decay.run_decay_detection", lambda *a, **k: [])
    monkeypatch.setattr("kaizenlog.coachledger.judge_coach_entries", lambda *a, **k: [])

    for name in ("notes", "stats", "mem", "logs", "exp"):
        (tmp_path / name).mkdir()
    cfg = Config(
        vault_dir=tmp_path,
        daily_notes_dir="notes",
        stats_dir="stats",
        memory_dir="mem",
        logs_dir="logs",
        experiments_dir="exp",
        timezone="UTC",
    )
    cfg.aiwork.enabled = False  # no sessions path either
    cfg.aiwork.outcome_git = False
    cli_mod.cmd_generate(cfg, date(2026, 8, 1))
    assert calls == []
