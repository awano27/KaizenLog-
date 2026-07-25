import json
import os
from datetime import datetime, timedelta, timezone

from kaizenlog.aiwork import render_aiwork_markdown, scan_sessions

TZ = timezone.utc
DAY_START = datetime(2020, 1, 1, tzinfo=TZ)
DAY_END = DAY_START + timedelta(days=1)


def _ts(hour, minute=0):
    return DAY_START.replace(hour=hour, minute=minute).isoformat().replace("+00:00", "Z")


def _user_text(text, ts, session="s1", **extra):
    return {
        "type": "user", "sessionId": session, "timestamp": ts,
        "cwd": "/home/user/myproj",
        "message": {"role": "user", "content": text}, **extra,
    }


def _assistant(ts, session="s1", tools=(), output_tokens=100):
    content = [{"type": "text", "text": "ok"}]
    content += [{"type": "tool_use", "name": t, "id": f"t_{t}", "input": {}} for t in tools]
    return {
        "type": "assistant", "sessionId": session, "timestamp": ts,
        "message": {
            "role": "assistant", "model": "claude-sonnet-5",
            "content": content, "usage": {"input_tokens": 10, "output_tokens": output_tokens},
        },
    }


def _tool_result(ts, session="s1", is_error=False, text="done"):
    return {
        "type": "user", "sessionId": session, "timestamp": ts,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t_Bash",
                         "is_error": is_error, "content": text}],
        },
    }


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_scan_basic_session(tmp_path):
    records = [
        _user_text("バグを直して", _ts(9)),
        _assistant(_ts(9, 1), tools=("Bash", "Edit")),
        _tool_result(_ts(9, 2)),
        _user_text("テストも実行して", _ts(9, 10)),
        _assistant(_ts(9, 11), output_tokens=250),
        _user_text("コミットして", _ts(9, 20)),
        _assistant(_ts(9, 21), output_tokens=50),
    ]
    _write_jsonl(tmp_path / "-home-user-myproj" / "s1.jsonl", records)

    sessions = scan_sessions(tmp_path, DAY_START, DAY_END)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.project == "myproj"
    assert s.user_turns == 3
    assert s.api_calls == 3
    assert s.tool_counts == {"Bash": 1, "Edit": 1}
    assert s.output_tokens == 400
    assert s.models == {"claude-sonnet-5"}
    assert not s.is_fragmented


def test_errors_and_interruptions_detected(tmp_path):
    records = [
        _user_text("やって", _ts(10)),
        _assistant(_ts(10, 1), tools=("Bash",)),
        _tool_result(_ts(10, 2), is_error=True, text="command failed: exit 1"),
        _assistant(_ts(10, 3), tools=("Edit",)),
        _tool_result(_ts(10, 4), is_error=True,
                     text="The user doesn't want to proceed with this tool use."),
    ]
    _write_jsonl(tmp_path / "-home-user-myproj" / "s2.jsonl", records)

    sessions = scan_sessions(tmp_path, DAY_START, DAY_END)
    s = sessions[0]
    assert s.tool_errors == 1
    assert s.interruptions == 1
    assert s.user_turns == 1
    assert s.is_fragmented  # 2往復以下


def test_day_filter_and_meta_excluded(tmp_path):
    outside = DAY_END + timedelta(hours=1)
    records = [
        _user_text("今日の依頼", _ts(9)),
        _assistant(_ts(9, 1)),
        _user_text("翌日の依頼", outside.isoformat()),
        _user_text("meta", _ts(9, 5), isMeta=True),
    ]
    _write_jsonl(tmp_path / "-home-user-myproj" / "s3.jsonl", records)

    sessions = scan_sessions(tmp_path, DAY_START, DAY_END)
    assert len(sessions) == 1
    assert sessions[0].user_turns == 1


def test_old_files_skipped_by_mtime(tmp_path):
    p = tmp_path / "-home-user-old" / "s4.jsonl"
    _write_jsonl(p, [_user_text("古い", _ts(9)), _assistant(_ts(9, 1))])
    old = (DAY_START - timedelta(days=30)).timestamp()
    os.utime(p, (old, old))
    assert scan_sessions(tmp_path, DAY_START, DAY_END) == []


def test_broken_lines_ignored(tmp_path):
    p = tmp_path / "-home-user-myproj" / "s5.jsonl"
    p.parent.mkdir(parents=True)
    good = json.dumps(_user_text("正常な行", _ts(9)))
    p.write_text(f"{{broken json\n\n{good}\n[1,2,3]\n", encoding="utf-8")
    sessions = scan_sessions(tmp_path, DAY_START, DAY_END)
    assert len(sessions) == 1
    assert sessions[0].user_turns == 1


def test_malformed_records_do_not_crash(tmp_path):
    records = [
        _user_text("正常な依頼です", _ts(9)),
        {"type": "user", "sessionId": "s1", "timestamp": _ts(9, 1),
         "message": "not-a-dict"},  # messageがdictでない
        {"type": "assistant", "sessionId": "s1", "timestamp": _ts(9, 2),
         "message": {"role": "assistant", "content": [],
                     "usage": {"output_tokens": "broken"}}},  # トークンが数値でない
        _assistant(_ts(9, 3)),
    ]
    _write_jsonl(tmp_path / "-home-user-myproj" / "s7.jsonl", records)

    sessions = scan_sessions(tmp_path, DAY_START, DAY_END)
    assert len(sessions) == 1
    assert sessions[0].user_turns == 1
    assert sessions[0].output_tokens == 100  # 不正なusageは無視、正常分のみ集計


def test_render_markdown(tmp_path):
    records = [
        _user_text("依頼", _ts(9)),
        _assistant(_ts(9, 1), tools=("Bash",)),
        _tool_result(_ts(9, 2), is_error=True, text="failed"),
    ]
    _write_jsonl(tmp_path / "-home-user-myproj" / "s6.jsonl", records)
    sessions = scan_sessions(tmp_path, DAY_START, DAY_END)

    md = render_aiwork_markdown(sessions, TZ)
    assert "### 🧠 AI作業の質" in md
    assert "claude-code" in md
    assert "セッション: 1回" in md
    assert "2往復以下: 1回" in md
    assert "リトライ連鎖: 0回" in md
    assert "ツールエラー: 1回" in md
    assert "Bash×1" in md
    assert "myproj" in md


def test_render_empty_returns_empty_string():
    assert render_aiwork_markdown([], TZ) == ""
