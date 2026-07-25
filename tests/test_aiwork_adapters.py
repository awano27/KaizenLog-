"""テレメトリアダプタ層と Codex CLI 対応のテスト（架空フィクスチャのみ）。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.aiwork import (
    AISession,
    ClaudeCodeAdapter,
    available_adapters,
    collect_ai_telemetry,
    render_aiwork_markdown,
    scan_sessions,
)
from kaizenlog.aiwork_codex import CodexAdapter
from kaizenlog.config import Config
from kaizenlog.doctor import run_doctor
from kaizenlog.report import DailySummary
from kaizenlog.stats import build_stats


UTC = timezone.utc
DAY_START = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
DAY_END = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)


def _write_codex_rollout(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _codex_fixture(sessions_root: Path) -> Path:
    """対象日ディレクトリに架空の rollout を1本置く。"""
    day_dir = sessions_root / "2026" / "07" / "20"
    path = day_dir / "rollout-2026-07-20T10-00-00-test-session.jsonl"
    records = [
        {
            "type": "session_meta",
            "timestamp": "2026-07-20T10:00:00.000Z",
            "payload": {
                "session_id": "codex-sess-1",
                "cwd": "C:/develop/demo-app",
                "id": "codex-sess-1",
            },
        },
        {
            "type": "turn_context",
            "timestamp": "2026-07-20T10:00:01.000Z",
            "payload": {"model": "gpt-test", "cwd": "C:/develop/demo-app"},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-07-20T10:00:05.000Z",
            "payload": {"type": "user_message", "message": "Please fix the failing unit test."},
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-20T10:00:10.000Z",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "c1",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-20T10:00:11.000Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "Error: exit code 1\ncommand failed",
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-07-20T10:00:20.000Z",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"output_tokens": 120, "input_tokens": 50},
                    "last_token_usage": {"output_tokens": 40},
                },
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-07-20T10:00:30.000Z",
            "payload": {"type": "turn_aborted"},
        },
        # outside day window — should not count
        {
            "type": "event_msg",
            "timestamp": "2026-07-21T01:00:00.000Z",
            "payload": {"type": "user_message", "message": "next day message"},
        },
    ]
    _write_codex_rollout(path, records)
    return path


def test_codex_adapter_mapping_and_day_filter(tmp_path):
    root = tmp_path / "sessions"
    _codex_fixture(root)
    adapter = CodexAdapter(root)
    sessions = adapter.scan_sessions(DAY_START, DAY_END)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.source == "codex"
    assert s.project == "demo-app"
    assert s.user_turns == 1
    assert s.tool_counts["shell_command"] == 1
    assert s.tool_errors >= 1
    assert s.interruptions == 1
    assert s.output_tokens == 120
    assert "gpt-test" in s.models

    prompts = adapter.scan_user_prompts(DAY_START, DAY_END)
    assert len(prompts) == 1
    assert prompts[0].source == "codex"
    assert "unit test" in prompts[0].text


def test_codex_skips_broken_and_missing_dir(tmp_path):
    root = tmp_path / "sessions"
    day_dir = root / "2026" / "07" / "20"
    day_dir.mkdir(parents=True)
    bad = day_dir / "rollout-bad.jsonl"
    bad.write_bytes(b"\xff\xfe not-json\n{not json\n")
    adapter = CodexAdapter(root)
    assert adapter.scan_sessions(DAY_START, DAY_END) == []
    assert CodexAdapter(tmp_path / "missing").scan_sessions(DAY_START, DAY_END) == []


def test_merge_two_sources_and_stats(tmp_path):
    # claude-like session via AISession direct merge
    claude = AISession(
        session_id="c1",
        project="vault",
        start=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        end=datetime(2026, 7, 20, 9, 5, tzinfo=UTC),
        user_turns=3,
        source="claude-code",
    )
    root = tmp_path / "sessions"
    _codex_fixture(root)
    codex_sessions = CodexAdapter(root).scan_sessions(DAY_START, DAY_END)
    merged = [claude] + codex_sessions
    merged.sort(key=lambda s: s.start)
    assert [s.source for s in merged] == ["claude-code", "codex"]

    summary = DailySummary(
        day=datetime(2026, 7, 20).date(),
        total_minutes=10.0,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
    )
    stats = build_stats(datetime(2026, 7, 20).date(), summary, merged)
    assert stats["ai"]["sessions"] == 2
    assert stats["ai"]["sources"]["claude-code"]["sessions"] == 1
    assert stats["ai"]["sources"]["codex"]["sessions"] == 1
    assert (
        stats["ai"]["sources"]["claude-code"]["sessions"]
        + stats["ai"]["sources"]["codex"]["sessions"]
        == stats["ai"]["sessions"]
    )


def test_render_aiwork_source_breakdown():
    sessions = [
        AISession(
            session_id="a",
            project="vault",
            start=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
            end=datetime(2026, 7, 20, 9, 2, tzinfo=UTC),
            user_turns=2,
            source="claude-code",
        ),
        AISession(
            session_id="b",
            project="demo-app",
            start=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            end=datetime(2026, 7, 20, 10, 3, tzinfo=UTC),
            user_turns=1,
            source="codex",
        ),
    ]
    md = render_aiwork_markdown(sessions, ZoneInfo("UTC"), retry_chain_count=0)
    assert "### 🧠 AI作業の質" in md
    assert "claude-code 1" in md and "codex 1" in md
    assert "demo-app (codex)" in md
    assert "vault" in md and "vault (claude" not in md


def test_evidence_f5_with_and_without_sources():
    base = {
        "version": 1,
        "day": "2026-07-20",
        "total_minutes": 100.0,
        "context_switches": 5,
        "ai_activity_blocks": 1,
        "by_category": {"開発": 100.0},
        "by_app": {},
        "by_site": {},
        "blocks": [],
        "ai": {
            "sessions": 3,
            "fragmented": 1,
            "tool_errors": 0,
            "interruptions": 0,
            "projects": {},
        },
    }
    old = build_advice_evidence(base).markdown
    assert "構造化AIテレメトリ" in old
    assert "内訳" not in old

    base["ai"]["sources"] = {
        "claude-code": {"sessions": 2},
        "codex": {"sessions": 1},
    }
    new = build_advice_evidence(base).markdown
    assert "内訳: claude-code 2回 / codex 1回" in new


def test_available_adapters_and_doctor(tmp_path):
    cfg = Config(vault_dir=tmp_path)
    cfg.aiwork.enabled = True
    cfg.aiwork.claude_projects_dir = str(tmp_path / "no-claude")
    cfg.aiwork.codex_sessions_dir = str(tmp_path / "no-codex")
    assert available_adapters(cfg) == []

    codex = tmp_path / "codex"
    codex.mkdir()
    cfg.aiwork.codex_sessions_dir = str(codex)
    adapters = available_adapters(cfg)
    assert len(adapters) == 1 and adapters[0].name == "codex"

    report, _ = run_doctor(cfg)
    assert "codex:" in report
    assert "claude-code" in report


def test_claude_wrapper_still_works(tmp_path):
    # existing thin wrapper path
    assert scan_sessions(tmp_path / "missing", DAY_START, DAY_END) == []
    adapter = ClaudeCodeAdapter(tmp_path / "missing")
    assert adapter.scan_sessions(DAY_START, DAY_END) == []
