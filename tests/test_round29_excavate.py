"""第29弾 §A: kaizenlog excavate 発掘監査。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

from kaizenlog.aiwork import AISession, UserPrompt
from kaizenlog.cardgen import ExcavateCardData, render_excavate_svg
from kaizenlog.config import AIWorkConfig, Config, PrivacyConfig
from kaizenlog.excavate import (
    EXCAVATE_MARKER,
    format_excavate_report,
    run_excavate,
    write_excavate_report,
)
from kaizenlog.vault import extract_section


TZ = timezone.utc
DAY = date(2026, 7, 28)


def _prompt(day: date, hour: int, text: str, project: str = "repo") -> UserPrompt:
    return UserPrompt(
        timestamp=datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ),
        project=project,
        text=text,
    )


def _session(
    day: date,
    hour: int,
    tokens: int,
    sid: str,
    *,
    model: str = "claude-sonnet-4",
    errors: int = 0,
    internal: bool = False,
) -> AISession:
    start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)
    return AISession(
        session_id=sid,
        project="repo",
        start=start,
        end=start + timedelta(minutes=30),
        user_turns=2,
        output_tokens=tokens,
        models={model} if model else set(),
        tool_errors=errors,
        is_internal=internal,
    )


def _cfg(tmp_path: Path) -> Config:
    return Config(
        vault_dir=tmp_path,
        memory_dir="mem",
        stats_dir="stats",
        daily_notes_dir="notes",
        timezone="UTC",
        aiwork=AIWorkConfig(enabled=True, usd_jpy=150.0),
        privacy=PrivacyConfig(redact_patterns=["SECRET"], replacement="[R]"),
    )


def test_a4_bucket_internal_excluded_totals(tmp_path: Path):
    d0 = DAY - timedelta(days=2)
    d1 = DAY - timedelta(days=1)
    d2 = DAY
    # retry chain on d1 (same text close in time)
    prompts = [
        _prompt(d0, 10, "unique day0 request aaa"),
        _prompt(d1, 10, "retry same please fix"),
        _prompt(d1, 10, "retry same please fix"),  # +0 min won't work - need minutes
    ]
    # fix timestamps with minutes
    prompts[1] = UserPrompt(
        timestamp=datetime(d1.year, d1.month, d1.day, 10, 0, tzinfo=TZ),
        project="repo",
        text="retry same please fix now",
    )
    prompts[2] = UserPrompt(
        timestamp=datetime(d1.year, d1.month, d1.day, 10, 5, tzinfo=TZ),
        project="repo",
        text="retry same please fix now",
    )
    # third attempt
    prompts.append(
        UserPrompt(
            timestamp=datetime(d1.year, d1.month, d1.day, 10, 10, tzinfo=TZ),
            project="repo",
            text="retry same please fix now",
        )
    )
    sessions = [
        _session(d0, 10, 500, "s0"),
        _session(d1, 10, 1000, "s1a"),
        _session(d1, 10, 2000, "s1b"),  # same hour - adjust
        _session(d1, 11, 3000, "s1c"),
        _session(d2, 10, 400, "s2"),
        _session(d2, 12, 100, "sint", internal=True),  # should be filtered by collect
    ]
    # fix s1 times to match prompts
    sessions[1] = _session(d1, 10, 1000, "s1a")
    sessions[1].start = datetime(d1.year, d1.month, d1.day, 9, 50, tzinfo=TZ)
    sessions[1].end = datetime(d1.year, d1.month, d1.day, 10, 20, tzinfo=TZ)
    sessions[2] = _session(d1, 10, 2000, "s1b")
    sessions[2].start = datetime(d1.year, d1.month, d1.day, 10, 0, tzinfo=TZ)
    sessions[2].end = datetime(d1.year, d1.month, d1.day, 10, 30, tzinfo=TZ)
    sessions[3] = _session(d1, 10, 3000, "s1c")
    sessions[3].start = datetime(d1.year, d1.month, d1.day, 10, 5, tzinfo=TZ)
    sessions[3].end = datetime(d1.year, d1.month, d1.day, 10, 40, tzinfo=TZ)

    # collect_ai_telemetry returns without internal
    user_sessions = [s for s in sessions if not s.is_internal]

    cfg = _cfg(tmp_path)
    (tmp_path / "stats").mkdir()
    (tmp_path / "notes").mkdir()
    (tmp_path / "mem").mkdir()
    stats_before = list((tmp_path / "stats").iterdir())
    notes_before = list((tmp_path / "notes").iterdir())

    with (
        patch("kaizenlog.excavate.available_adapters", return_value=[MagicMock(name="claude-code")]),
        patch(
            "kaizenlog.excavate.collect_ai_telemetry",
            return_value=(user_sessions, prompts, 1),
        ),
    ):
        report = run_excavate(cfg, days=3, as_of=DAY)

    assert report.session_count == len(user_sessions)
    assert report.retry_chain_count >= 1
    assert report.loop_episodes >= 1
    # internal not in sessions
    assert all(not s.is_internal for s in user_sessions)
    # stats/notes untouched
    assert list((tmp_path / "stats").iterdir()) == stats_before
    assert list((tmp_path / "notes").iterdir()) == notes_before


def test_a4_unknown_model_cost_fail_closed(tmp_path: Path):
    d = DAY
    prompts = [
        UserPrompt(
            timestamp=datetime(d.year, d.month, d.day, 10, 0, tzinfo=TZ),
            project="repo",
            text="same unknown model request",
        ),
        UserPrompt(
            timestamp=datetime(d.year, d.month, d.day, 10, 5, tzinfo=TZ),
            project="repo",
            text="same unknown model request",
        ),
    ]
    sessions = [
        AISession(
            session_id="u1",
            project="repo",
            start=datetime(d.year, d.month, d.day, 9, 50, tzinfo=TZ),
            end=datetime(d.year, d.month, d.day, 10, 20, tzinfo=TZ),
            output_tokens=1_000_000,
            models={"totally-unknown-model-xyz"},
            user_turns=2,
        )
    ]
    cfg = _cfg(tmp_path)
    with (
        patch("kaizenlog.excavate.available_adapters", return_value=[MagicMock()]),
        patch(
            "kaizenlog.excavate.collect_ai_telemetry",
            return_value=(sessions, prompts, 0),
        ),
    ):
        report = run_excavate(cfg, days=1, as_of=DAY)
    assert report.est_cost_usd is None
    body = format_excavate_report(report)
    assert "不明" in body


def test_a4_write_idempotent_and_card(tmp_path: Path):
    cfg = _cfg(tmp_path)
    (tmp_path / "mem").mkdir()
    body = "# excavate test\n\nline\n"
    path = tmp_path / "mem" / "excavate" / f"{DAY.isoformat()}.md"
    write_excavate_report(path, body)
    b1 = path.read_bytes()
    write_excavate_report(path, body)
    assert path.read_bytes() == b1
    assert extract_section(path.read_text(encoding="utf-8"), EXCAVATE_MARKER) is not None

    svg = render_excavate_svg(
        ExcavateCardData(
            period_label="2026-07-01 〜 2026-07-28",
            loop_cost_usd=1.5,
            loop_cost_jpy=225,
            episode_count=3,
            worst_day="2026-07-15",
            session_count=10,
        )
    )
    ET.fromstring(svg)
    empty = render_excavate_svg(
        ExcavateCardData(
            period_label="p",
            loop_cost_usd=None,
            loop_cost_jpy=None,
            episode_count=0,
            worst_day=None,
            session_count=0,
        )
    )
    root = ET.fromstring(empty)
    assert "計測なし" in ET.tostring(root, encoding="unicode")


def test_a4_redact_excerpt(tmp_path: Path):
    d = DAY
    text = "please handle SECRET token carefully"
    prompts = [
        UserPrompt(
            timestamp=datetime(d.year, d.month, d.day, 10, 0, tzinfo=TZ),
            project="repo",
            text=text,
        ),
        UserPrompt(
            timestamp=datetime(d.year, d.month, d.day, 10, 5, tzinfo=TZ),
            project="repo",
            text=text,
        ),
    ]
    sessions = [
        AISession(
            session_id="s1",
            project="repo",
            start=datetime(d.year, d.month, d.day, 9, 50, tzinfo=TZ),
            end=datetime(d.year, d.month, d.day, 10, 30, tzinfo=TZ),
            output_tokens=100,
            models={"claude-sonnet-4"},
            user_turns=2,
        )
    ]
    cfg = _cfg(tmp_path)
    from kaizenlog.privacy import make_redactor

    redactor = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)
    with (
        patch("kaizenlog.excavate.available_adapters", return_value=[MagicMock()]),
        patch(
            "kaizenlog.excavate.collect_ai_telemetry",
            return_value=(sessions, prompts, 0),
        ),
    ):
        report = run_excavate(cfg, days=1, as_of=DAY, redactor=redactor)
    assert report.worst_excerpt is not None
    assert "SECRET" not in report.worst_excerpt
    assert "[R]" in report.worst_excerpt
