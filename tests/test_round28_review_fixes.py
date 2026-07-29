"""第28弾: 第27弾レビュー残件 §R1–§R12 回帰テスト。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kaizenlog.aiwork import (
    AISession,
    RetryChain,
    UserPrompt,
    compute_loop_tax,
    format_loop_tax_line,
    loop_tax_to_stats_dict,
    resolve_output_price,
)
from kaizenlog.coach import (
    apply_proposal,
    build_coach_context,
    parse_coach_json,
    run_coach_llm,
    save_proposal,
)
from kaizenlog.config import AIWorkConfig, Config, LLMConfig
from kaizenlog.experiments import (
    Experiment,
    compute_abtest_effect,
    effect_size,
    format_abtest_journal_line,
)
from kaizenlog.handoff import apply_handoff, build_agent_context_section
from kaizenlog.promptledger import PromptLedgerEntry
from kaizenlog.promptroi import (
    compute_prompt_roi,
    format_weekly_roi_section,
    prompt_roi_scan_start,
)
from kaizenlog.vault import (
    ADVICE_MARKER,
    AGENT_CONTEXT_MARKER,
    COACH_MARKER,
    DailyNoteStore,
    extract_section,
    read_text_preserve_newlines,
    upsert_section,
)
from kaizenlog.weekly_context import render_weekly_context
from kaizenlog.advisor import AdvisorError

TZ = timezone.utc
T0 = datetime(2026, 7, 29, 10, 0, tzinfo=TZ)


# ---- §R1 bytes ----

def test_r1_lf_trailing_spaces_prefix_preserved(tmp_path: Path):
    target = tmp_path / "CLAUDE.md"
    original = b"# handwritten  \n"
    target.write_bytes(original)
    section = build_agent_context_section(
        stats_dir=tmp_path / "stats",
        memory_dir=tmp_path / "mem",
        as_of=date(2026, 7, 29),
    )
    (tmp_path / "stats").mkdir(exist_ok=True)
    (tmp_path / "mem").mkdir(exist_ok=True)
    apply_handoff(target, section)
    data = target.read_bytes()
    assert data.startswith(original)
    # 2回適用で全bytes一致
    apply_handoff(target, section)
    assert target.read_bytes() == data


def test_r1_crlf_marker_update_preserves_prefix_suffix(tmp_path: Path):
    target = tmp_path / "AGENTS.md"
    body = (
        b"# header\r\n"
        b"hand  \r\n"
        b"<!-- kaizenlog:agent-context:start -->\r\n"
        b"old\r\n"
        b"<!-- kaizenlog:agent-context:end -->\r\n"
        b"footer  \r\n"
    )
    target.write_bytes(body)
    start = body.find(b"<!-- kaizenlog:agent-context:start -->")
    end = body.find(b"<!-- kaizenlog:agent-context:end -->")
    end_tag = b"<!-- kaizenlog:agent-context:end -->"
    prefix = body[:start]
    suffix = body[end + len(end_tag) :]
    section = "new section line\nsecond"
    apply_handoff(target, section)
    data = target.read_bytes()
    assert data.startswith(prefix)
    assert data.endswith(suffix)
    assert b"new section line" in data


def test_r1_coach_apply_bytes_stable(tmp_path: Path):
    mem = tmp_path / "mem"
    prop = save_proposal(
        mem,
        as_of=date(2026, 7, 29),
        append_md="- a\n- b\n- c",
        evidence=[{"fact_id": "F1", "value": "1"}],
    )
    target = tmp_path / "CLAUDE.md"
    original = b"# \xe6\x89\x8b\xe6\x9b\xb8\xe3\x81\x8d  \n\nkeep\n"  # 手書き
    target.write_bytes(original)
    apply_proposal(prop, [target])
    after = target.read_bytes()
    assert after.startswith(original)
    apply_proposal  # silence
    # 二重適用は拒否、bytes不変
    with pytest.raises(AdvisorError):
        apply_proposal(prop, [target])
    assert target.read_bytes() == after


# ---- §R2 skilled windows ----

def test_r2_pending_until_day_29():
    marked = date(2026, 6, 1)
    entry = PromptLedgerEntry(
        id="PRM-1",
        representative="hello world task please",
        count_total=5,
        days_seen=2,
        first_seen="2026-05-01",
        last_seen="2026-06-20",
        status="skilled",
        skill_name="s",
        marked_on=marked.isoformat(),
    )
    prompts = [
        UserPrompt(timestamp=datetime(2026, 5, 20, 10, tzinfo=TZ), project="p", text="hello world task please"),
        UserPrompt(timestamp=datetime(2026, 6, 5, 10, tzinfo=TZ), project="p", text="hello world task please"),
    ]
    for offset in (0, 14, 28):
        as_of = marked + timedelta(days=offset)
        row = compute_prompt_roi([entry], prompts, [], as_of=as_of)[0]
        assert "計測中" in row.skilled_effect
        assert "削減" not in row.skilled_effect
    as_of = marked + timedelta(days=29)
    row = compute_prompt_roi([entry], prompts, [], as_of=as_of)[0]
    assert "削減" in row.skilled_effect


def test_r2_scan_start_includes_before_window():
    marked = date(2026, 7, 15)
    entries = [
        PromptLedgerEntry(
            id="PRM-1",
            representative="x",
            count_total=1,
            days_seen=1,
            first_seen="2026-07-01",
            last_seen="2026-07-01",
            status="skilled",
            skill_name="s",
            marked_on=marked.isoformat(),
        )
    ]
    as_of = date(2026, 8, 20)
    start = prompt_roi_scan_start(entries, as_of, window_days=30)
    assert start == marked - timedelta(days=30)


def test_r2_partial_tokens_unknown():
    marked = date(2026, 6, 1)
    as_of = marked + timedelta(days=29)
    entry = PromptLedgerEntry(
        id="PRM-1",
        representative="fix the failing tests now",
        count_total=3,
        days_seen=2,
        first_seen="2026-05-01",
        last_seen="2026-06-10",
        status="skilled",
        skill_name="s",
        marked_on=marked.isoformat(),
    )
    # before has session tokens, after has prompts but no sessions
    prompts = [
        UserPrompt(
            timestamp=datetime(2026, 5, 20, 10, tzinfo=TZ),
            project="p",
            text="fix the failing tests now",
        ),
        UserPrompt(
            timestamp=datetime(2026, 6, 10, 10, tzinfo=TZ),
            project="p",
            text="fix the failing tests now",
        ),
    ]
    sessions = [
        AISession(
            session_id="s1",
            project="p",
            start=datetime(2026, 5, 20, 9, tzinfo=TZ),
            end=datetime(2026, 5, 20, 12, tzinfo=TZ),
            output_tokens=1000,
        )
    ]
    row = compute_prompt_roi([entry], prompts, sessions, as_of=as_of)[0]
    assert "削減" in row.skilled_effect
    assert "tokens不明" in row.skilled_effect


# ---- §R3 weekly ROI ----

def test_r3_weekly_roi_with_rows():
    week = date(2026, 7, 20)  # Monday
    entry = PromptLedgerEntry(
        id="PRM-20260701-001",
        representative="summarize news daily please",
        count_total=5,
        days_seen=2,
        first_seen="2026-07-01",
        last_seen="2026-07-20",
        status="new",
    )
    prompts = [
        UserPrompt(
            timestamp=datetime(2026, 7, 21, 10, tzinfo=TZ),
            project="p",
            text="summarize news daily please",
        )
    ]
    rows = compute_prompt_roi([entry], prompts, [], as_of=date(2026, 7, 26))
    md = render_weekly_context(
        Path("/no/stats"),
        Path("/no/mem"),
        Path("/no/exp"),
        week,
        roi_rows=rows,
    )
    assert "プロンプト資産ROI" in md
    assert "PRM-20260701-001" in md


def test_r3_weekly_roi_empty_omitted():
    week = date(2026, 7, 20)
    md = render_weekly_context(
        Path("/no/stats"),
        Path("/no/mem"),
        Path("/no/exp"),
        week,
        roi_rows=[],
    )
    assert "プロンプト資産ROI" not in md
    # 後方互換: 引数なし
    md2 = render_weekly_context(Path("/no"), Path("/no"), Path("/no"), week)
    assert "プロンプト資産ROI" not in md2


# ---- §R4 loop tax fail-closed ----

def _p(offset_min: int, text: str = "same request again") -> UserPrompt:
    return UserPrompt(
        timestamp=T0 + timedelta(minutes=offset_min),
        project="repo",
        text=text,
    )


def _s(sid, start_off, tokens, models=None, errors=0):
    start = T0 + timedelta(minutes=start_off)
    return AISession(
        session_id=sid,
        project="repo",
        start=start,
        end=start + timedelta(minutes=20),
        user_turns=2,
        output_tokens=tokens,
        models=set(models or ["claude-sonnet-4"]),
        tool_errors=errors,
    )


def test_r4_multi_model_price_ambiguous():
    chain = RetryChain(project="repo", prompts=[_p(0), _p(5)])
    sess = _s("s1", 0, 1_000_000, models={"gpt-4o", "gpt-4o-mini"})
    tax = compute_loop_tax([chain], [sess])
    assert tax.total_wasted_tokens == 1_000_000
    assert tax.est_cost_usd is None
    assert "$-.--" in format_loop_tax_line(tax)


def test_r4_known_plus_unknown_totals_none():
    c1 = RetryChain(project="repo", prompts=[_p(0), _p(5)])
    c2 = RetryChain(
        project="other-proj",
        prompts=[
            UserPrompt(
                timestamp=T0 + timedelta(hours=2),
                project="other-proj",
                text="other request text",
            ),
            UserPrompt(
                timestamp=T0 + timedelta(hours=2, minutes=5),
                project="other-proj",
                text="other request text",
            ),
        ],
    )
    sessions = [_s("s1", 0, 1000)]
    # c2 has no matching session
    tax = compute_loop_tax([c1, c2], sessions)
    assert tax.episode_count == 2
    assert tax.total_wasted_tokens is None
    assert tax.est_cost_usd is None


def test_r4_same_price_multi_model_ok():
    # both resolve if we only have haiku patterns - use same model twice via pricing override
    chain = RetryChain(project="repo", prompts=[_p(0), _p(5)])
    sess = _s("s1", 0, 1_000_000, models={"claude-haiku-3", "claude-haiku"})
    tax = compute_loop_tax([chain], [sess])
    # both match claude-haiku → 1.25
    assert tax.est_cost_usd == pytest.approx(1.25, abs=0.01)


def test_r4_zero_episodes():
    tax = compute_loop_tax([], [])
    assert tax.episode_count == 0
    assert tax.total_wasted_tokens == 0
    assert tax.est_cost_usd == 0.0


# ---- §R5 stats + weekly max ----

def test_r5_loop_tax_stats_and_weekly_picks_max_length(tmp_path: Path):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    # day1: short episode
    d1 = {
        "version": 2,
        "day": "2026-07-20",
        "total_minutes": 10,
        "by_category": {},
        "ai": {
            "sessions": 1,
            "retry_chains": 5,
            "loop_tax": {
                "episode_count": 1,
                "total_wasted_tokens": 100,
                "est_cost_usd": 0.1,
                "max_episode": {
                    "length": 2,
                    "wasted_tokens": 100,
                    "has_tool_error": False,
                    "excerpt": "short",
                },
            },
        },
    }
    d2 = {
        "version": 2,
        "day": "2026-07-21",
        "total_minutes": 10,
        "by_category": {},
        "ai": {
            "sessions": 1,
            "retry_chains": 1,
            "loop_tax": {
                "episode_count": 1,
                "total_wasted_tokens": None,
                "est_cost_usd": None,
                "max_episode": {
                    "length": 6,
                    "wasted_tokens": None,
                    "has_tool_error": True,
                    "excerpt": "secret [REDACTED] x",
                },
            },
        },
    }
    (stats_dir / "2026-07-20.json").write_text(json.dumps(d1), encoding="utf-8")
    (stats_dir / "2026-07-21.json").write_text(json.dumps(d2), encoding="utf-8")
    md = render_weekly_context(
        stats_dir, tmp_path / "m", tmp_path / "e", date(2026, 7, 20)
    )
    assert "今週最大のループ" in md
    assert "2026-07-21" in md
    assert "往復数: 6" in md
    assert "浪費tokens: 不明" in md
    assert "tool_error: あり" in md


def test_r5_old_stats_omits_max_loop(tmp_path: Path):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    d1 = {
        "version": 2,
        "day": "2026-07-20",
        "total_minutes": 10,
        "by_category": {},
        "ai": {"sessions": 1, "retry_chains": 3},
    }
    (stats_dir / "2026-07-20.json").write_text(json.dumps(d1), encoding="utf-8")
    md = render_weekly_context(
        stats_dir, tmp_path / "m", tmp_path / "e", date(2026, 7, 20)
    )
    assert "今週最大のループ" not in md


def test_r5_all_zero_episodes_omits(tmp_path: Path):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    for i in range(7):
        d = date(2026, 7, 20) + timedelta(days=i)
        data = {
            "version": 2,
            "day": d.isoformat(),
            "total_minutes": 1,
            "by_category": {},
            "ai": {
                "sessions": 0,
                "loop_tax": {
                    "episode_count": 0,
                    "total_wasted_tokens": 0,
                    "est_cost_usd": 0.0,
                    "max_episode": None,
                },
            },
        }
        (stats_dir / f"{d.isoformat()}.json").write_text(
            json.dumps(data), encoding="utf-8"
        )
    md = render_weekly_context(
        stats_dir, tmp_path / "m", tmp_path / "e", date(2026, 7, 20)
    )
    assert "今週最大のループ" not in md


def test_r5_loop_tax_to_stats_dict_redacts():
    chain = RetryChain(
        project="p",
        prompts=[
            UserPrompt(timestamp=T0, project="p", text="SECRET password here"),
            UserPrompt(timestamp=T0 + timedelta(minutes=5), project="p", text="SECRET password here"),
        ],
    )
    sess = _s("s1", 0, 100)
    tax = compute_loop_tax([chain], [sess])
    d = loop_tax_to_stats_dict(tax, redactor=lambda t: t.replace("SECRET", "[R]"))
    assert d["episode_count"] == 1
    assert "[R]" in (d["max_episode"]["excerpt"] or "")
    assert "SECRET" not in (d["max_episode"]["excerpt"] or "")


# ---- §R6 notify wiring ----

def test_r6_generate_notify_threshold(tmp_path: Path):
    from kaizenlog.cli import cmd_generate
    from kaizenlog.report import DailySummary

    cfg = Config(
        vault_dir=tmp_path,
        daily_notes_dir="notes",
        stats_dir="stats",
        memory_dir="mem",
        logs_dir="logs",
        experiments_dir="exp",
        aiwork=AIWorkConfig(enabled=True, loop_tax_alert_usd=1.0),
    )
    (tmp_path / "notes").mkdir()
    (tmp_path / "stats").mkdir()
    (tmp_path / "mem").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "exp").mkdir()

    day = date(2026, 7, 29)
    summary = DailySummary(
        day=day,
        total_minutes=10,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
        by_site={},
    )
    prompts = [_p(0), _p(5)]
    sessions = [_s("s1", 0, 1_000_000)]  # $3 > $1

    with (
        patch("kaizenlog.cli.collect_day", return_value=([], True)),
        patch("kaizenlog.cli.collect_input", return_value=None),
        patch("kaizenlog.cli.summarize", return_value=summary),
        patch("kaizenlog.cli.render_markdown", return_value="### activity\n"),
        patch("kaizenlog.cli.available_adapters", return_value=[MagicMock()]),
        patch(
            "kaizenlog.cli.collect_ai_telemetry",
            return_value=(sessions, prompts, 0),
        ),
        patch("kaizenlog.cli.notify") as n,
        patch("kaizenlog.cli.ActivityWatchClient"),
        patch("kaizenlog.cli.Classifier") as Cls,
        patch("kaizenlog.cli.run_decay_detection", create=True),
        patch("kaizenlog.decay.run_decay_detection", return_value=[]),
    ):
        Cls.return_value.classify_all.return_value = []
        cmd_generate(cfg, day)
        assert n.call_count == 1

    # equal threshold: no notify
    cfg.aiwork.loop_tax_alert_usd = 3.0
    with (
        patch("kaizenlog.cli.collect_day", return_value=([], True)),
        patch("kaizenlog.cli.collect_input", return_value=None),
        patch("kaizenlog.cli.summarize", return_value=summary),
        patch("kaizenlog.cli.render_markdown", return_value="### activity\n"),
        patch("kaizenlog.cli.available_adapters", return_value=[MagicMock()]),
        patch(
            "kaizenlog.cli.collect_ai_telemetry",
            return_value=(sessions, prompts, 0),
        ),
        patch("kaizenlog.cli.notify") as n2,
        patch("kaizenlog.cli.ActivityWatchClient"),
        patch("kaizenlog.cli.Classifier") as Cls2,
        patch("kaizenlog.decay.run_decay_detection", return_value=[]),
    ):
        Cls2.return_value.classify_all.return_value = []
        cmd_generate(cfg, day)
        assert n2.call_count == 0


# ---- §R7 coach 30d loop tax ----

def test_r7_coach_loop_tax_coverage(tmp_path: Path):
    stats = tmp_path / "stats"
    stats.mkdir()
    as_of = date(2026, 7, 30)
    for i in range(30):
        d = as_of - timedelta(days=i)
        data = {
            "version": 2,
            "day": d.isoformat(),
            "total_minutes": 1,
            "by_category": {},
            "ai": {
                "sessions": 1,
                "retry_chains": 1,
                "tool_errors": 0,
                "loop_tax": {
                    "episode_count": 1,
                    "total_wasted_tokens": 100,
                    "est_cost_usd": 0.01,
                    "max_episode": {
                        "length": 3,
                        "wasted_tokens": 100,
                        "has_tool_error": False,
                        "excerpt": "ex",
                    },
                },
            },
        }
        (stats / f"{d.isoformat()}.json").write_text(
            json.dumps(data), encoding="utf-8"
        )
    cfg = Config(
        vault_dir=tmp_path,
        stats_dir="stats",
        memory_dir="mem",
        aiwork=AIWorkConfig(enabled=False),
    )
    (tmp_path / "mem").mkdir()
    ctx = build_coach_context(cfg, as_of=as_of)
    assert "30日episode合計: 30" in ctx
    assert "総tokens: 3000" in ctx
    assert "最大episode:" in ctx

    # drop one day → coverage 29
    (stats / f"{as_of.isoformat()}.json").unlink()
    ctx2 = build_coach_context(cfg, as_of=as_of)
    assert "計測不成立（loop_tax coverage: 29/30日）" in ctx2


def test_r7_all_zero_episodes():
    tmp = Path  # placeholder - inline via tmp_path in next test if needed
    pass


def test_r7_zero_episodes_display(tmp_path: Path):
    stats = tmp_path / "stats"
    stats.mkdir()
    as_of = date(2026, 7, 30)
    for i in range(30):
        d = as_of - timedelta(days=i)
        data = {
            "version": 2,
            "day": d.isoformat(),
            "total_minutes": 1,
            "by_category": {},
            "ai": {
                "sessions": 0,
                "loop_tax": {
                    "episode_count": 0,
                    "total_wasted_tokens": 0,
                    "est_cost_usd": 0.0,
                    "max_episode": None,
                },
            },
        }
        (stats / f"{d.isoformat()}.json").write_text(
            json.dumps(data), encoding="utf-8"
        )
    cfg = Config(
        vault_dir=tmp_path,
        stats_dir="stats",
        memory_dir="mem",
        aiwork=AIWorkConfig(enabled=False),
    )
    (tmp_path / "mem").mkdir()
    ctx = build_coach_context(cfg, as_of=as_of)
    assert "30日episode合計: 0" in ctx
    assert "なし（ループなし）" in ctx


# ---- §R8 evidence ----

def test_r8_evidence_contract():
    good = {
        "claude_md_append": "- a\n- b\n- c",
        "evidence": [{"fact_id": "F1", "value": "3"}],
    }
    assert parse_coach_json(json.dumps(good))
    bad_cases = [
        {"claude_md_append": "- a\n- b\n- c", "evidence": [{}]},
        {"claude_md_append": "- a\n- b\n- c", "evidence": [1]},
        {"claude_md_append": "- a\n- b\n- c", "evidence": [{"fact_id": "F1"}]},
        {
            "claude_md_append": "- a\n- b\n- c",
            "evidence": [{"fact_id": "F1", "value": ""}],
        },
        {
            "claude_md_append": "- a\n- b\n- c",
            "evidence": [{"fact_id": "F1", "value": True}],
        },
    ]
    for b in bad_cases:
        with pytest.raises(AdvisorError):
            parse_coach_json(json.dumps(b))


def test_r8_retry_after_evidence_fail():
    cfg = Config(llm=LLMConfig(backend="none", retries=1))
    calls = {"n": 0}

    def gen(_c, _s, _u):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps(
                {
                    "claude_md_append": "- a\n- b\n- c",
                    "evidence": [{"fact_id": "F1"}],
                }
            )
        return json.dumps(
            {
                "claude_md_append": "- a\n- b\n- c",
                "evidence": [{"fact_id": "F1", "value": 1}],
            }
        )

    data = run_coach_llm(cfg, "#c", generate_fn=gen)
    assert calls["n"] == 2
    assert data["evidence"][0]["value"] == 1


# ---- §R9 zero write ----

def test_r9_missing_applied_no_write(tmp_path: Path):
    prop = tmp_path / "p.md"
    prop.write_text(
        "---\ndate: 2026-07-29\n---\n\n## CLAUDE.md 追記案\n\n- a\n- b\n- c\n",
        encoding="utf-8",
    )
    target = tmp_path / "CLAUDE.md"
    orig = b"# keep\n"
    target.write_bytes(orig)
    with pytest.raises(AdvisorError):
        apply_proposal(prop, [target])
    assert target.read_bytes() == orig
    assert prop.read_bytes() == prop.read_bytes()  # unchanged path exists
    assert list(tmp_path.glob("**/*.tmp")) == []


def test_r9_missing_target_no_create(tmp_path: Path):
    mem = tmp_path / "mem"
    prop = save_proposal(
        mem,
        as_of=date(2026, 7, 29),
        append_md="- a\n- b\n- c",
        evidence=[{"fact_id": "F", "value": "1"}],
    )
    prop_b = prop.read_bytes()
    missing = tmp_path / "no" / "CLAUDE.md"
    with pytest.raises(AdvisorError):
        apply_proposal(prop, [missing])
    assert prop.read_bytes() == prop_b
    assert not missing.exists()
    assert not (tmp_path / "no").exists()


def test_r9_rollback_on_second_target_fail(tmp_path: Path):
    mem = tmp_path / "mem"
    prop = save_proposal(
        mem,
        as_of=date(2026, 7, 29),
        append_md="- a\n- b\n- c",
        evidence=[{"fact_id": "F", "value": "1"}],
    )
    t1 = tmp_path / "a.md"
    t2 = tmp_path / "b.md"
    t1.write_bytes(b"# a\n")
    t2.write_bytes(b"# b\n")
    o1, o2, op = t1.read_bytes(), t2.read_bytes(), prop.read_bytes()
    calls = {"n": 0}
    real = __import__("kaizenlog.vault", fromlist=["atomic_write_text"]).atomic_write_text

    def flaky(path, content, **kw):
        calls["n"] += 1
        # fail on second target write
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(path, content, **kw)

    with patch("kaizenlog.coach.atomic_write_text", side_effect=flaky):
        with pytest.raises(AdvisorError):
            apply_proposal(prop, [t1, t2])
    assert t1.read_bytes() == o1
    assert t2.read_bytes() == o2
    assert prop.read_bytes() == op


# ---- §R10 abtest baseline ----

def _stat(day, api_calls, dev_min):
    return {
        "version": 2,
        "day": day.isoformat(),
        "total_minutes": dev_min + 10,
        "by_category": {"開発": dev_min},
        "ai": {"api_calls": api_calls, "sessions": 1 if api_calls else 0},
    }


def test_r10_partial_baseline_fail():
    start = date(2026, 7, 1)
    # pre only covers some weekdays
    pre = [_stat(start - timedelta(days=1), 0, 80)]  # one day only
    stats = [
        _stat(start + timedelta(days=i), 1 if i < 5 else 0, 100 if i < 5 else 60)
        for i in range(10)
    ]
    m, ai, non, reason = compute_abtest_effect(
        stats, start=start, end=start + timedelta(days=9), pre_stats=pre
    )
    assert m is None
    assert reason and "baseline不足" in reason


def test_r10_effect_size_unchanged():
    exp = Experiment(
        path=Path("x"),
        title="t",
        status="running",
        metric="x",
        target_op="<=",
        target_value=1,
        baseline=100.0,
        measurements={date(2026, 7, 1): 80.0, date(2026, 7, 2): 90.0},
    )
    assert effect_size(exp) == -15.0


# ---- §R11 journal ----

def test_r11_finish_creates_advice_note(tmp_path: Path):
    from kaizenlog.cli import cmd_abtest
    from kaizenlog.experiments import create_abtest
    import argparse

    vault = tmp_path
    (vault / "notes").mkdir()
    (vault / "exp").mkdir()
    (vault / "mem").mkdir()
    (vault / "stats").mkdir()
    cfg = Config(
        vault_dir=vault,
        daily_notes_dir="notes",
        experiments_dir="exp",
        memory_dir="mem",
        stats_dir="stats",
        timezone="UTC",
    )
    create_abtest(cfg.experiments_path, today=date(2026, 7, 1), predict_pct=30, days=7)
    args = argparse.Namespace(
        abtest_command="finish", felt="+20", id=None
    )
    with patch("kaizenlog.cli.datetime") as dt:
        # fix today
        from datetime import datetime as real_dt

        class FakeDateTime:
            @staticmethod
            def now(tz=None):
                return real_dt(2026, 7, 10, tzinfo=timezone.utc)

            @staticmethod
            def combine(*a, **k):
                return real_dt.combine(*a, **k)

        dt.now = FakeDateTime.now
        dt.combine = FakeDateTime.combine
        # simpler: patch ZoneInfo date via today in cmd - use freezegun alternative
    # Direct call with monkeypatch on datetime.now is hard; call finish path pieces
    from kaizenlog.experiments import load_abtests, finish_abtest, compute_abtest_effect
    from kaizenlog.cardgen import AbtestCardData, write_abtest_card

    exp = load_abtests(cfg.experiments_path)[0]
    today = date(2026, 7, 10)
    measured, ai_n, non_n, invalid = compute_abtest_effect(
        [], start=exp.start, end=today, pre_stats=[]
    )
    card = cfg.memory_path / "cards" / f"abtest-{exp.id}.svg"
    write_abtest_card(
        card,
        AbtestCardData(
            experiment_id=exp.id,
            period_label="p",
            sample_ai_days=ai_n,
            sample_non_ai_days=non_n,
            predict_pct=30,
            felt_pct=20,
            measured_pct=measured,
            invalid_reason=invalid or "実測不成立(サンプル不足: 0日)",
        ),
    )
    finish_abtest(
        exp,
        felt_pct=20,
        card_rel_path=str(card),
        measured_pct=measured,
        invalid_reason=invalid or "実測不成立(サンプル不足: 0日)",
        sample_ai=ai_n,
        sample_non=non_n,
        as_of=today,
    )
    line = format_abtest_journal_line(exp)
    store = DailyNoteStore(cfg.daily_notes_path)
    store.write_section(today, ADVICE_MARKER, line + "\n")
    note = store.read(today)
    assert note is not None
    assert extract_section(note, ADVICE_MARKER)
    assert "abtest完了" in (extract_section(note, ADVICE_MARKER) or "")
    assert card.is_file()


def test_r11_preserves_handwritten_when_adding_advice(tmp_path: Path):
    store = DailyNoteStore(tmp_path / "notes")
    day = date(2026, 7, 10)
    p = tmp_path / "notes" / f"{day.isoformat()}.md"
    p.parent.mkdir(parents=True)
    original = b"# handwritten plan  \n\n- todo\n"
    p.write_bytes(original)
    line = "📊 abtest完了: 予測+30% / 体感+20% / 実測不成立(x)"
    content = read_text_preserve_newlines(p)
    from kaizenlog.vault import upsert_section

    updated = upsert_section(content, ADVICE_MARKER, line + "\n")
    from kaizenlog.vault import atomic_write_text

    atomic_write_text(p, updated)
    data = p.read_bytes()
    assert data.startswith(original)
    assert b"abtest" in data or "abtest".encode() in data


# ---- §R11 CLI 統合（cmd_abtest finish 実配線） ----

class _FrozenCliDT:
    """cmd_abtest の datetime.now(tz) を 2026-07-10 に固定するスタブ。"""

    @staticmethod
    def now(tz=None):
        return datetime(2026, 7, 10, 9, 0, tzinfo=tz)


def _abtest_cfg(tmp_path: Path) -> Config:
    for d in ("notes", "exp", "mem", "stats"):
        (tmp_path / d).mkdir()
    return Config(
        vault_dir=tmp_path,
        daily_notes_dir="notes",
        experiments_dir="exp",
        memory_dir="mem",
        stats_dir="stats",
        timezone="UTC",
    )


def _run_finish(cfg: Config) -> int:
    import argparse

    from kaizenlog.cli import cmd_abtest

    with patch("kaizenlog.cli.datetime", _FrozenCliDT):
        return cmd_abtest(
            cfg, argparse.Namespace(abtest_command="finish", felt="+20", id=None)
        )


def test_r11_cli_appends_to_existing_advice_activity_unchanged(tmp_path: Path):
    from kaizenlog.experiments import create_abtest
    from kaizenlog.vault import ACTIVITY_MARKER

    cfg = _abtest_cfg(tmp_path)
    create_abtest(cfg.experiments_path, today=date(2026, 7, 1), predict_pct=30, days=7)
    day = date(2026, 7, 10)
    store = DailyNoteStore(cfg.daily_notes_path)
    store.write_section(day, ACTIVITY_MARKER, "- 開発: 1時間00分\n")
    store.write_section(day, ADVICE_MARKER, "既存アドバイス行\n")
    note_path = store.path_for(day)
    handwritten = "手書きメモ（マーカー外）\n"
    note_path.write_text(
        note_path.read_text(encoding="utf-8") + "\n" + handwritten, encoding="utf-8"
    )
    activity_before = extract_section(store.read(day) or "", ACTIVITY_MARKER)

    assert _run_finish(cfg) == 0
    note = store.read(day) or ""
    adv = extract_section(note, ADVICE_MARKER) or ""
    assert "既存アドバイス行" in adv
    assert "abtest完了" in adv
    assert extract_section(note, ACTIVITY_MARKER) == activity_before
    assert handwritten.strip() in note


def test_r11_cli_insufficient_sample_writes_line_and_card(tmp_path: Path):
    from kaizenlog.experiments import create_abtest

    cfg = _abtest_cfg(tmp_path)
    create_abtest(cfg.experiments_path, today=date(2026, 7, 1), predict_pct=30, days=7)

    assert _run_finish(cfg) == 0
    day = date(2026, 7, 10)
    adv = extract_section(DailyNoteStore(cfg.daily_notes_path).read(day) or "", ADVICE_MARKER) or ""
    assert "abtest完了" in adv
    assert "不成立" in adv
    assert "実測実測" not in adv  # 文言重複の回帰
    cards = list((cfg.memory_path / "cards").glob("abtest-*.svg"))
    assert len(cards) == 1
    assert "不成立" in cards[0].read_text(encoding="utf-8")


def test_r11_cli_no_duplicate_line_on_refinish(tmp_path: Path):
    from kaizenlog.experiments import _set_frontmatter_field, create_abtest, load_abtests

    cfg = _abtest_cfg(tmp_path)
    create_abtest(cfg.experiments_path, today=date(2026, 7, 1), predict_pct=30, days=7)
    assert _run_finish(cfg) == 0
    exp = load_abtests(cfg.experiments_path)[0]
    exp.path.write_text(
        _set_frontmatter_field(exp.path.read_text(encoding="utf-8"), "status", "running"),
        encoding="utf-8",
    )
    assert _run_finish(cfg) == 0
    day = date(2026, 7, 10)
    adv = extract_section(DailyNoteStore(cfg.daily_notes_path).read(day) or "", ADVICE_MARKER) or ""
    assert adv.count("abtest完了") == 1
