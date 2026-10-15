"""Unknown adapter telemetry must stay unknown through rendering and stats."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.aiwork import (
    AISession,
    UserPrompt,
    detect_retry_chains,
    render_aiwork_markdown,
)
from kaizenlog.aiwork_cursor import CursorAdapter
from kaizenlog.aiwork_grok import GrokAdapter
from kaizenlog.narrative import write_monthly_sparklines
from kaizenlog.report import DailySummary
from kaizenlog.stats import build_stats


UTC = timezone.utc
DAY = datetime(2026, 9, 6, tzinfo=UTC)
END = DAY + timedelta(days=1)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _summary() -> DailySummary:
    return DailySummary(
        day=DAY.date(), total_minutes=1.0, by_category={}, by_app={}, blocks=[],
        ai_tool_minutes={}, ai_sessions=0, context_switches=0,
    )


def _known_session() -> AISession:
    return AISession(
        session_id="known", project="repo", start=DAY, end=DAY + timedelta(minutes=1),
        user_turns=1, output_tokens=1_000_000, models={"claude-sonnet-4"},
        source="claude-code",
    )


def test_new_adapter_unknown_metrics_remain_unknown_in_stats_render_and_monthly_card(
    tmp_path: Path,
) -> None:
    grok_root = tmp_path / "grok"
    _write_jsonl(
        grok_root / "workspace" / "session" / "updates.jsonl",
        [{
            "timestamp": int((DAY + timedelta(minutes=1)).timestamp()),
            "method": "session/update",
            "params": {"sessionId": "upstream-id", "update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "please investigate"},
                "_meta": {"promptIndex": 1},
            }},
        }],
    )
    grok = GrokAdapter(grok_root).scan_sessions(DAY, END)[0]

    cursor_root = tmp_path / "cursor"
    timestamp_ms = int((DAY + timedelta(minutes=2)).timestamp() * 1000)
    _write_jsonl(
        cursor_root / "stream.jsonl",
        [
            {"type": "user", "session_id": "cursor", "timestamp_ms": timestamp_ms,
             "message": {"role": "user", "content": [{"type": "text", "text": "summarize"}]}},
            {"type": "assistant", "session_id": "cursor", "timestamp_ms": timestamp_ms + 1,
             "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}},
        ],
    )
    cursor = CursorAdapter(cursor_root).scan_sessions(DAY, END)[0]

    assert grok.output_tokens_measurable is False
    assert grok.assistant_chars_measurable is False
    assert cursor.output_tokens_measurable is False
    assert cursor.assistant_chars_measurable is True

    sessions = [_known_session(), grok, cursor]
    stats = build_stats(DAY.date(), _summary(), sessions)

    assert stats["ai"]["measurement"]["output_tokens"] == "partial"
    assert stats["ai"]["measurement"]["tool_errors"] == "partial"
    assert stats["ai"]["output_tokens"] is None
    assert stats["ai"]["est_cost_usd"] is None
    assert stats["ai"]["tool_errors"] is None
    assert stats["ai"]["sources"]["grok"]["tool_errors"] is None
    assert stats["ai"]["session_digests"][1]["assistant_chars"] is None
    assert stats['ai']['session_digests'][1]['edits'] is None
    assert stats['ai']['session_digests'][1]['tests_run'] is None
    assert stats['ai']['api_calls'] is None

    markdown = render_aiwork_markdown(sessions, ZoneInfo("UTC"))
    assert "ツールエラー: 一部計測不能" in markdown
    assert "出力トークン: 一部計測不能" in markdown
    assert "推定コスト: 計測不能" in markdown
    assert '編集未計測' in markdown

    paths = write_monthly_sparklines(
        tmp_path / "assets", 2026, 9, [stats], write=True
    )
    cost_card = next(path for path in paths if path.name.startswith("est-cost-usd-"))
    card = cost_card.read_text(encoding="utf-8")
    assert "記録なし" in card
    assert "<polyline" not in card


def test_retry_detection_does_not_join_matching_prompts_from_distinct_sessions() -> None:
    prompts = [
        UserPrompt(DAY, "shared-project", "please fix this test", source="cursor", session_id="one"),
        UserPrompt(DAY + timedelta(minutes=1), "shared-project", "please fix this test", source="cursor", session_id="two"),
        UserPrompt(DAY + timedelta(minutes=2), "shared-project", "please fix this test", source="cursor", session_id="one"),
    ]

    chains = detect_retry_chains(prompts)

    assert len(chains) == 1
    assert chains[0].project == "shared-project"
    assert [prompt.session_id for prompt in chains[0].prompts] == ["one", "one"]


def test_known_session_identity_prevents_cross_channel_cost_attribution():
    from kaizenlog.aiwork import _session_for_prompt
    from kaizenlog.promptroi import _session_for_prompt as roi_session
    known = _known_session()
    prompt = UserPrompt(DAY, 'repo', 'request same repo', 'grok', session_id='missing-grok')
    assert _session_for_prompt(prompt, [known]) is None
    assert roi_session(prompt, [known]) is None


def test_handoff_does_not_describe_unknown_tool_errors_as_zero():
    from kaizenlog.handoff import _tool_errors_text
    text = _tool_errors_text([{'day': '2026-09-06', 'ai': {'tool_errors': None}}])
    assert '0 件' not in text
    assert '計測' in text
