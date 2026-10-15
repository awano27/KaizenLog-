"""Schema-gated local Grok and Cursor telemetry fixtures.

These fixtures contain only the fields documented by the upstream producers.
They deliberately do not model a guessed, normalized export format.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kaizenlog.aiwork_cursor import CursorAdapter
from kaizenlog.aiwork_grok import GrokAdapter


UTC = timezone.utc
DAY = datetime(2026, 9, 6, tzinfo=UTC)
END = DAY + timedelta(days=1)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _grok_update(
    timestamp: int,
    session_id: str,
    update: dict,
    *,
    method: str = "session/update",
) -> dict:
    return {
        "timestamp": timestamp,
        "method": method,
        "params": {"sessionId": session_id, "update": update},
    }


def _cursor_text_event(
    event_type: str,
    session_id: str,
    text: str,
    timestamp_ms: int | None = None,
) -> dict:
    row = {
        "type": event_type,
        "message": {
            "role": "user" if event_type == "user" else "assistant",
            "content": [{"type": "text", "text": text}],
        },
        "session_id": session_id,
    }
    if timestamp_ms is not None:
        row["timestamp_ms"] = timestamp_ms
    return row


def test_grok_adapter_reads_timestamped_acp_user_prompt_and_never_measures_tools(
    tmp_path: Path,
):
    root = tmp_path / "sessions"
    path = root / "encoded-workspace" / "disk-session" / "updates.jsonl"
    _write_jsonl(
        path,
        [
            _grok_update(
                int(DAY.timestamp()) + 60,
                "payload-session-id-is-not-the-storage-identity",
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "調査してテストも実行してください"},
                    "_meta": {"promptIndex": 1},
                },
            ),
            _grok_update(
                int(DAY.timestamp()) + 120,
                "payload-session-id-is-not-the-storage-identity",
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "了解しました"},
                },
            ),
        ],
    )

    sessions = GrokAdapter(root).scan_sessions(DAY, END)

    assert len(sessions) == 1
    session = sessions[0]
    assert session.session_id == "grok:encoded-workspace/disk-session"
    assert session.source == "grok"
    assert session.user_turns == 1
    assert session.title == "調査してテストも実行してください"
    assert session.tools_measurable is False
    assert session.output_tokens == 0
    assert session.tool_counts == {}
    assert [p.text for p in GrokAdapter(root).scan_user_prompts(DAY, END)] == [
        "調査してテストも実行してください"
    ]


def test_grok_adapter_joins_same_prompt_index_and_skips_bash_host_and_bad_lines(
    tmp_path: Path,
):
    root = tmp_path / "sessions"
    path = root / "workspace" / "session" / "updates.jsonl"
    _write_jsonl(
        path,
        [
            _grok_update(
                int(DAY.timestamp()) + 10,
                "same",
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "one real prompt"},
                    "_meta": {"promptIndex": 7},
                },
            ),
            _grok_update(
                int(DAY.timestamp()) + 11,
                "same",
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "duplicate chunk"},
                    "_meta": {"promptIndex": 7},
                },
            ),
            _grok_update(
                int(DAY.timestamp()) + 12,
                "same",
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {
                        "type": "text",
                        "text": "! dir",
                        "_meta": {"bash_command": "dir"},
                    },
                    "_meta": {"promptIndex": 8},
                },
            ),
            _grok_update(
                int(DAY.timestamp()) + 13,
                "same",
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "host turn"},
                    "_meta": {"promptIndex": 9, "host_turn": True},
                },
            ),
            _grok_update(
                int(END.timestamp()),
                "same",
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "tomorrow"},
                    "_meta": {"promptIndex": 10},
                },
            ),
        ],
    )
    path.write_text(path.read_text(encoding="utf-8") + "{truncated", encoding="utf-8")

    sessions = GrokAdapter(root).scan_sessions(DAY, END)

    assert len(sessions) == 1
    assert sessions[0].user_turns == 1
    # Upstream's prompt collector joins consecutive chunks for one promptIndex.
    assert sessions[0].title == "one real promptduplicate chunk"
    prompts = GrokAdapter(root).scan_user_prompts(DAY, END)
    assert [p.text for p in prompts] == ["one real promptduplicate chunk"]


def test_grok_adapter_applies_timestamped_rewind_marker_before_day_filter(
    tmp_path: Path,
):
    root = tmp_path / "sessions"
    _write_jsonl(
        root / "workspace" / "session" / "updates.jsonl",
        [
            _grok_update(
                int(DAY.timestamp()) + 10,
                "same",
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "keep this prompt"},
                    "_meta": {"promptIndex": 0},
                },
            ),
            _grok_update(
                int(DAY.timestamp()) + 11,
                "same",
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "reply"},
                },
            ),
            _grok_update(
                int(DAY.timestamp()) + 12,
                "same",
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "discard this prompt"},
                    "_meta": {"promptIndex": 1},
                },
            ),
            _grok_update(
                int(DAY.timestamp()) + 13,
                "same",
                {"sessionUpdate": "rewind_marker", "target_prompt_index": 1},
                method="_x.ai/session/update",
            ),
        ],
    )

    sessions = GrokAdapter(root).scan_sessions(DAY, END)

    assert len(sessions) == 1
    assert sessions[0].user_turns == 1
    assert sessions[0].title == "keep this prompt"


def test_cursor_adapter_reads_only_raw_timestamped_stream_json_events(tmp_path: Path):
    root = tmp_path / "captured-streams"
    path = root / "capture.jsonl"
    timestamp_ms = int((DAY + timedelta(hours=9)).timestamp() * 1000)
    user = _cursor_text_event("user", "cursor-session", "READMEを要約して", timestamp_ms)
    _write_jsonl(
        path,
        [
            {
                "type": "system",
                "subtype": "init",
                "cwd": "C:/work/demo",
                "session_id": "cursor-session",
                "model": "Cursor Model",
            },
            user,
            user,  # exact replay/capture duplicate: count it once
            _cursor_text_event("assistant", "cursor-session", "summary", timestamp_ms + 1),
            {
                "type": "tool_call",
                "subtype": "completed",
                "session_id": "cursor-session",
                "tool_call": {"readToolCall": {"args": {"path": "README.md"}}},
            },
        ],
    )

    sessions = CursorAdapter(root).scan_sessions(DAY, END)

    assert len(sessions) == 1
    session = sessions[0]
    assert session.source == "cursor"
    assert session.session_id == "cursor-session"
    assert session.project == "demo"
    assert session.models == {"Cursor Model"}
    assert session.user_turns == 1
    assert session.assistant_chars == len("summary")
    assert session.tools_measurable is False
    assert session.output_tokens == 0
    assert session.tool_counts == {}
    assert [p.text for p in CursorAdapter(root).scan_user_prompts(DAY, END)] == [
        "READMEを要約して"
    ]


def test_cursor_adapter_does_not_use_file_mtime_for_untimestamped_events(
    tmp_path: Path,
):
    root = tmp_path / "captured-streams"
    path = root / "no-event-time.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "system",
                "subtype": "init",
                "cwd": "C:/work/demo",
                "session_id": "cursor-session",
                "model": "Cursor Model",
            },
            _cursor_text_event("user", "cursor-session", "日時がない依頼"),
            _cursor_text_event("assistant", "cursor-session", "日時がない返答"),
        ],
    )

    assert CursorAdapter(root).scan_sessions(DAY, END) == []
    assert CursorAdapter(root).scan_user_prompts(DAY, END) == []


def test_cursor_adapter_keeps_metadata_scoped_to_its_own_session_id(tmp_path: Path):
    root = tmp_path / "captured-streams"
    timestamp_ms = int((DAY + timedelta(hours=10)).timestamp() * 1000)
    _write_jsonl(
        root / "mixed.jsonl",
        [
            {
                "type": "system",
                "subtype": "init",
                "cwd": "C:/work/one",
                "session_id": "one",
                "model": "model-one",
            },
            {
                "type": "system",
                "subtype": "init",
                "cwd": "C:/work/two",
                "session_id": "two",
                "model": "model-two",
            },
            _cursor_text_event("user", "two", "second session", timestamp_ms),
        ],
    )

    sessions = CursorAdapter(root).scan_sessions(DAY, END)

    assert len(sessions) == 1
    assert sessions[0].session_id == "two"
    assert sessions[0].project == "two"
    assert sessions[0].models == {"model-two"}


def test_cursor_adapter_bounds_assistant_char_count_to_the_requested_day(
    tmp_path: Path,
):
    root = tmp_path / "captured-streams"
    previous_ms = int((DAY - timedelta(seconds=1)).timestamp() * 1000)
    current_ms = int((DAY + timedelta(hours=1)).timestamp() * 1000)
    _write_jsonl(
        root / "cross-day.jsonl",
        [
            {
                "type": "system",
                "subtype": "init",
                "cwd": "C:/work/demo",
                "session_id": "cross-day",
                "model": "Cursor Model",
            },
            _cursor_text_event("assistant", "cross-day", "previous", previous_ms),
            _cursor_text_event("user", "cross-day", "today prompt", current_ms),
            _cursor_text_event("assistant", "cross-day", "today", current_ms + 1),
        ],
    )

    session = CursorAdapter(root).scan_sessions(DAY, END)[0]

    assert session.assistant_chars == len("today")
