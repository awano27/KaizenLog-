"""AI Work Telemetry の計数精度の回帰テスト（B-2: 監査で実証した過大計上バグ）。

実際の Claude Code JSONL 形式に基づく:
- 1回のAPI応答は複数の 'assistant' 行（thinking/text/tool_use ブロック毎）に
  分割され、各行が同一の message.id と同一の usage を持つ
- サブエージェントは isSidechain:true で親と同じ sessionId を持ち、その 'user'
  行のプロンプトは親モデルが書いたもの
- 自動コンパクション要約は type:'user' / isCompactSummary:true
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from kaizenlog.aiwork import scan_sessions, scan_user_prompts

DS = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
DE = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)


def _write(project_dir: Path, name: str, records: list[dict]) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / name).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")


def _user(sid, ts, text, **extra):
    return {"type": "user", "sessionId": sid, "timestamp": ts,
            "message": {"content": text}, **extra}


def _assistant(sid, ts, msg_id, tokens, blocks, **extra):
    return {"type": "assistant", "sessionId": sid, "timestamp": ts,
            "message": {"id": msg_id, "model": "claude-x",
                        "usage": {"output_tokens": tokens}, "content": blocks}, **extra}


# ---- トークン/API呼び出しの重複排除 ----

def test_split_assistant_lines_counted_once(tmp_path):
    """同一 message.id の3行（各usage=807）を1回のAPI呼び出し・807トークンとして数える。"""
    proj = tmp_path / "proj"
    _write(proj, "s1.jsonl", [
        _user("s1", "2026-07-19T09:00:00+00:00", "本物のユーザー依頼です"),
        _assistant("s1", "2026-07-19T09:00:01+00:00", "msg_A", 807, [{"type": "thinking", "thinking": "..."}]),
        _assistant("s1", "2026-07-19T09:00:02+00:00", "msg_A", 807, [{"type": "text", "text": "回答"}]),
        _assistant("s1", "2026-07-19T09:00:03+00:00", "msg_A", 807, [{"type": "tool_use", "name": "Read"}]),
    ])
    s = scan_sessions(tmp_path, DS, DE)[0]
    assert s.api_calls == 1           # 3行だが1回のAPI呼び出し
    assert s.output_tokens == 807     # 807×3=2421 ではない
    assert s.tool_counts["Read"] == 1  # tool_useは行ごとで正しい


def test_distinct_message_ids_counted_separately(tmp_path):
    proj = tmp_path / "proj"
    _write(proj, "s1.jsonl", [
        _user("s1", "2026-07-19T09:00:00+00:00", "依頼1"),
        _assistant("s1", "2026-07-19T09:00:01+00:00", "msg_A", 100, [{"type": "text", "text": "a"}]),
        _assistant("s1", "2026-07-19T09:00:05+00:00", "msg_B", 200, [{"type": "text", "text": "b"}]),
    ])
    s = scan_sessions(tmp_path, DS, DE)[0]
    assert s.api_calls == 2
    assert s.output_tokens == 300


# ---- サブエージェント（sidechain）の分離 ----

def test_sidechain_records_do_not_inflate_parent(tmp_path):
    proj = tmp_path / "proj"
    _write(proj, "s1.jsonl", [_user("s1", "2026-07-19T09:00:00+00:00", "本物のユーザー依頼")])
    _write(proj, "agent-x.jsonl", [
        _user("s1", "2026-07-19T09:01:00+00:00", "モデルが書いたサブエージェント指示", isSidechain=True)
        for _ in range(5)
    ])
    s = scan_sessions(tmp_path, DS, DE)[0]
    assert s.user_turns == 1  # サブエージェントの5件を足して6にしない


def test_sidechain_prompts_excluded_from_mining(tmp_path):
    proj = tmp_path / "proj"
    _write(proj, "s1.jsonl", [_user("s1", "2026-07-19T09:00:00+00:00", "ユーザーの本当の依頼文です")])
    _write(proj, "agent-x.jsonl", [
        _user("s1", "2026-07-19T09:01:00+00:00", "モデル生成のサブエージェント指示文", isSidechain=True)
    ])
    prompts = scan_user_prompts(tmp_path, DS, DE)
    assert len(prompts) == 1
    assert "本当の依頼" in prompts[0].text


# ---- コンパクション要約の除外 ----

def test_compact_summary_not_counted_as_user_turn(tmp_path):
    proj = tmp_path / "proj"
    _write(proj, "s1.jsonl", [
        _user("s1", "2026-07-19T09:00:00+00:00", "依頼A"),
        _user("s1", "2026-07-19T09:30:00+00:00",
              "This session is being continued from a previous conversation..." * 200,
              isCompactSummary=True),
        _user("s1", "2026-07-19T10:00:00+00:00", "依頼B"),
    ])
    s = scan_sessions(tmp_path, DS, DE)[0]
    assert s.user_turns == 2  # コンパクション要約は発話に数えない


def test_compact_summary_excluded_from_mining(tmp_path):
    proj = tmp_path / "proj"
    _write(proj, "s1.jsonl", [
        _user("s1", "2026-07-19T09:00:00+00:00", "普通の依頼文です"),
        _user("s1", "2026-07-19T09:30:00+00:00",
              "This session is being continued..." * 500, isCompactSummary=True),
    ])
    prompts = scan_user_prompts(tmp_path, DS, DE)
    assert [p.text for p in prompts] == ["普通の依頼文です"]  # 数十KBの要約を混ぜない


# ---- 深夜跨ぎセッションの翌日フラグメントを細切れ扱いしない ----

def test_after_midnight_continuation_not_counted_as_fragment(tmp_path):
    proj = tmp_path / "proj"
    # 23:30に本体、翌00:40にassistant継続のみ（user発話なし）
    _write(proj, "s1.jsonl", [
        _user("s1", "2026-07-19T23:30:00+00:00", "夜遅くの依頼"),
        _assistant("s1", "2026-07-20T00:40:00+00:00", "msg_Z", 50, [{"type": "text", "text": "続き"}]),
    ])
    day2 = scan_sessions(tmp_path, DE, datetime(2026, 7, 21, tzinfo=timezone.utc))
    # 翌日側にはuser発話ゼロの継続断片しかない → セッションとして数えない
    assert day2 == []
