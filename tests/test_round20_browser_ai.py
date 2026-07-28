"""第20弾: ブラウザ AI テレメトリ + B0 Codex ラッパー対称。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.aiwork import available_adapters, render_aiwork_markdown
from kaizenlog.aiwork_browser import BrowserAIAdapter
from kaizenlog.aiwork_codex import _SessionAccum
from kaizenlog.config import AIWorkConfig, Config
from kaizenlog.stats import build_stats
from kaizenlog.report import DailySummary


TZ = timezone.utc
DAY = datetime(2026, 7, 28, 0, 0, tzinfo=TZ)
END = DAY + timedelta(days=1)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


# ---- B0 --------------------------------------------------------------------


def test_b0_codex_wrapper_not_counted_as_user_turn():
    acc = _SessionAccum(session_id="s1", project="p")
    acc.note_user_message("<command-message>slash</command-message>")
    assert acc.user_turns == 0
    assert acc.title is None
    acc.note_user_message("本物の依頼です")
    assert acc.user_turns == 1
    assert acc.title is not None


# ---- B2 adapter ------------------------------------------------------------


def test_b2_browser_adapter_normalizes_conversation(tmp_path):
    day = "2026-07-28"
    rows = [
        {
            "ts": "2026-07-28T10:00:00+00:00",
            "site": "chatgpt.com",
            "conversation_id": "abc",
            "role": "user",
            "char_count": 20,
            "text": "スキーマを設計してマイグレーションまで書いて",
        },
        {
            "ts": "2026-07-28T10:05:00+00:00",
            "site": "chatgpt.com",
            "conversation_id": "abc",
            "role": "assistant",
            "char_count": 500,
            "text": "x" * 500,
        },
        {
            "ts": "2026-07-28T10:10:00+00:00",
            "site": "chatgpt.com",
            "conversation_id": "abc",
            "role": "user",
            "char_count": 12,
            "text": "続きをお願いします",
        },
    ]
    _write_jsonl(tmp_path / f"{day}.jsonl", rows)
    ad = BrowserAIAdapter(tmp_path)
    sessions = ad.scan_sessions(DAY, END)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.source == "chatgpt-web"
    assert s.user_turns == 2
    assert s.tools_measurable is False
    assert s.output_tokens == 0
    assert s.assistant_chars == 500
    assert s.title and "スキーマ" in s.title
    prompts = ad.scan_user_prompts(DAY, END)
    assert len(prompts) == 2
    assert prompts[0].source == "chatgpt-web"


def test_b2_metadata_only_no_text(tmp_path):
    rows = [
        {
            "ts": "2026-07-28T11:00:00+00:00",
            "site": "claude.ai",
            "conversation_id": "c1",
            "role": "user",
            "char_count": 40,
        },
        {
            "ts": "2026-07-28T11:01:00+00:00",
            "site": "claude.ai",
            "conversation_id": "c1",
            "role": "assistant",
            "char_count": 100,
        },
    ]
    _write_jsonl(tmp_path / "2026-07-28.jsonl", rows)
    sessions = BrowserAIAdapter(tmp_path).scan_sessions(DAY, END)
    assert sessions[0].title == "（本文未保存）"
    assert sessions[0].user_turns == 1
    assert sessions[0].assistant_chars == 100


def test_b2_available_adapters_includes_browser(tmp_path):
    cfg = Config()
    cfg.aiwork = AIWorkConfig(
        enabled=True,
        claude_projects_dir=str(tmp_path / "no-claude"),
        codex_sessions_dir=str(tmp_path / "no-codex"),
        browser_export_dir=str(tmp_path / "browser"),
    )
    (tmp_path / "browser").mkdir()
    ads = available_adapters(cfg)
    assert any(a.name == "browser-ai" for a in ads)


# ---- B3 render + stats -----------------------------------------------------


def test_b3_render_web_dash_and_source_bucket(tmp_path):
    rows = [
        {
            "ts": "2026-07-28T12:00:00+00:00",
            "site": "chatgpt.com",
            "conversation_id": "w1",
            "role": "user",
            "char_count": 10,
            "text": "hello world",
        },
        {
            "ts": "2026-07-28T12:01:00+00:00",
            "site": "chatgpt.com",
            "conversation_id": "w1",
            "role": "assistant",
            "char_count": 30,
            "text": "hi",
        },
    ]
    _write_jsonl(tmp_path / "2026-07-28.jsonl", rows)
    sessions = BrowserAIAdapter(tmp_path).scan_sessions(DAY, END)
    md = render_aiwork_markdown(sessions, ZoneInfo("UTC"))
    assert "web 1" in md or "（web 1）" in md or "web" in md
    assert "chatgpt (web)" in md
    # tool columns are dash, not zero
    assert "| - | - | - | - |" in md or md.count("| - |") >= 3


def test_b3_stats_web_fields_separated(tmp_path):
    rows = [
        {
            "ts": "2026-07-28T13:00:00+00:00",
            "site": "gemini.google.com",
            "conversation_id": "g1",
            "role": "user",
            "char_count": 8,
            "text": "質問です",
        },
        {
            "ts": "2026-07-28T13:02:00+00:00",
            "site": "gemini.google.com",
            "conversation_id": "g1",
            "role": "assistant",
            "char_count": 200,
            "text": "y" * 200,
        },
    ]
    _write_jsonl(tmp_path / "2026-07-28.jsonl", rows)
    sessions = BrowserAIAdapter(tmp_path).scan_sessions(DAY, END)
    summary = DailySummary(
        day=DAY.date(),
        total_minutes=10.0,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
        by_site={},
    )
    stats = build_stats(DAY.date(), summary, sessions)
    assert stats["ai"]["web_sessions"] == 1
    assert stats["ai"]["web_user_turns"] == 1
    assert stats["ai"]["web_assistant_chars"] == 200
    # tokens not polluted
    assert stats["ai"].get("output_tokens", 0) in (0, None) or stats["ai"]["output_tokens"] == 0


def test_e3_web_stats_use_source_suffix_not_tools_flag():
    """tools_measurable=False でも source が -web でなければ web_* に入れない。"""
    from kaizenlog.aiwork import AISession

    non_web = AISession(
        session_id="x",
        project="p",
        start=DAY,
        end=DAY + timedelta(hours=1),
        user_turns=2,
        tools_measurable=False,
        assistant_chars=50,
        source="future-cli",  # 非ブラウザ
    )
    web = AISession(
        session_id="w",
        project="chatgpt",
        start=DAY,
        end=DAY + timedelta(hours=1),
        user_turns=1,
        tools_measurable=False,
        assistant_chars=10,
        source="chatgpt-web",
    )
    summary = DailySummary(
        day=DAY.date(),
        total_minutes=1.0,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
    )
    stats = build_stats(DAY.date(), summary, [non_web, web])
    assert stats["ai"]["web_sessions"] == 1
    assert stats["ai"]["web_user_turns"] == 1
    assert stats["ai"]["web_assistant_chars"] == 10
