"""第31弾: 空転ブレーカー（性能・安全契約）。"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kaizenlog.guard import (
    _read_new_lines,
    append_live_episode,
    build_hook_command,
    build_hooks_snippet,
    cleanup_old_states,
    count_live_breaker_fires,
    install_hooks_write,
    load_state,
    merge_hooks_into_settings,
    parse_hook_stdin,
    run_hook,
    save_state,
    state_dir,
    state_path,
)


def test_c1_import_lightness():
    """subprocess で import kaizenlog.guard 後の sys.modules を検査。"""
    import subprocess

    code = r"""
import sys
import kaizenlog.guard  # noqa: F401
mods = set(sys.modules)
forbidden = {"kaizenlog.cli", "kaizenlog.aiwork", "kaizenlog.promptledger"}
leaked = sorted(forbidden & mods)
print("LEAKED:" + ",".join(leaked))
sys.exit(1 if leaked else 0)
"""
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "LEAKED:" in r.stdout
    assert r.stdout.strip().endswith("LEAKED:")


def test_c1_debounce_skips_transcript_read(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    sid = "sess-debounce"
    st = load_state(sid)
    st["last_parse_ts"] = time.time()
    save_state(sid, st)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    payload = json.dumps(
        {
            "session_id": sid,
            "transcript_path": str(transcript),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "hello world test",
        }
    )
    opened = []

    real_open = open

    def spy_open(path, *a, **k):
        p = str(path)
        if "t.jsonl" in p or p.endswith("t.jsonl"):
            opened.append(p)
        return real_open(path, *a, **k)

    with patch("builtins.open", side_effect=spy_open):
        rc = run_hook(
            payload,
            settings={
                "enabled": True,
                "debounce_seconds": 30,
                "retry_threshold": 3,
                "cooldown_seconds": 0,
                "memory_dir": tmp_path / "mem",
            },
        )
    assert rc == 0
    assert opened == []  # transcript を開かない


def test_c1_incremental_read(tmp_path: Path):
    p = tmp_path / "tr.jsonl"
    line1 = json.dumps({"type": "user", "message": {"role": "user", "content": "a"}}) + "\n"
    p.write_bytes(line1.encode("utf-8"))
    lines, off = _read_new_lines(p, 0)
    assert len(lines) == 1
    assert off == len(line1.encode("utf-8"))
    # 追記
    line2 = json.dumps({"type": "user", "message": {"role": "user", "content": "b"}}) + "\n"
    with open(p, "ab") as f:
        f.write(line2.encode("utf-8"))
    lines2, off2 = _read_new_lines(p, off)
    assert len(lines2) == 1
    assert "b" in lines2[0]
    assert off2 == off + len(line2.encode("utf-8"))


def test_c1_incomplete_line_deferred(tmp_path: Path):
    p = tmp_path / "tr.jsonl"
    complete = json.dumps({"type": "x", "n": 1}) + "\n"
    incomplete = '{"type":"partial"'
    p.write_bytes((complete + incomplete).encode("utf-8"))
    lines, off = _read_new_lines(p, 0)
    assert len(lines) == 1
    # 追記して完成
    with open(p, "ab") as f:
        f.write(b', "n": 2}\n')
    lines2, _ = _read_new_lines(p, off)
    assert len(lines2) == 1
    assert json.loads(lines2[0])["n"] == 2


def test_c1_all_errors_exit_zero(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    assert run_hook("not-json{{{") == 0
    assert run_hook(json.dumps({"session_id": "s", "transcript_path": "/no/such"})) == 0
    # 破損 state
    sid = "broken"
    sp = state_path(sid)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("{broken", encoding="utf-8")
    assert run_hook(json.dumps({"session_id": sid, "prompt": "x" * 20})) == 0
    settings = {
        "enabled": True,
        "debounce_seconds": 0,
        "retry_threshold": 3,
        "cooldown_seconds": 0,
        "memory_dir": tmp_path / "m",
    }
    # effective_debounce=0 を先に焼き込み（一段目ゲート用）
    st = load_state("fire1")
    st["effective_debounce"] = 0
    save_state("fire1", st)
    with patch("kaizenlog.notify.notify", side_effect=RuntimeError("boom")):
        text = "please fix the flaky integration test carefully"
        rcs = []
        for i in range(3):
            rcs.append(
                run_hook(
                    json.dumps(
                        {
                            "session_id": "fire1",
                            "hook_event_name": "UserPromptSubmit",
                            "prompt": text,
                        }
                    ),
                    settings=settings,
                )
            )
    assert rcs == [0, 0, 0]
    err = capsys.readouterr().err
    assert err == ""


def test_c1_detection_boundaries(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    settings = {
        "enabled": True,
        "debounce_seconds": 0,
        "retry_threshold": 3,
        "tool_error_streak": 3,
        "cooldown_seconds": 300,
        "memory_dir": tmp_path / "mem",
        "pricing": {"claude-sonnet": 3.0},
    }
    text = "implement the feature with careful tests please"
    sid = "det1"
    # 2回 → 非発火
    for _ in range(2):
        run_hook(
            json.dumps(
                {
                    "session_id": sid,
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": text,
                }
            ),
            settings=settings,
        )
    out = capsys.readouterr().out
    assert "hookSpecificOutput" not in out
    # 3回目 → 発火
    run_hook(
        json.dumps(
            {
                "session_id": sid,
                "hook_event_name": "UserPromptSubmit",
                "prompt": text,
            }
        ),
        settings=settings,
    )
    out = capsys.readouterr().out
    assert "hookSpecificOutput" in out
    data = json.loads(out.strip().splitlines()[-1])
    assert "additionalContext" in data["hookSpecificOutput"]
    assert "空転ブレーカー" in data["hookSpecificOutput"]["additionalContext"]
    # クールダウン内再発火なし
    run_hook(
        json.dumps(
            {
                "session_id": sid,
                "hook_event_name": "UserPromptSubmit",
                "prompt": text,
            }
        ),
        settings=settings,
    )
    out2 = capsys.readouterr().out
    assert "hookSpecificOutput" not in out2


def test_c1_tool_error_streak(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    settings = {
        "enabled": True,
        "debounce_seconds": 0,
        "retry_threshold": 99,
        "tool_error_streak": 3,
        "cooldown_seconds": 0,
        "memory_dir": tmp_path / "mem",
    }
    tr = tmp_path / "t.jsonl"
    # 2 errors
    lines = []
    for i in range(2):
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "is_error": True, "content": "err"}
                        ],
                    },
                }
            )
        )
    # Actually tool_result as type
    lines = [
        json.dumps({"type": "tool_result", "is_error": True, "content": "e1"}),
        json.dumps({"type": "tool_result", "is_error": True, "content": "e2"}),
    ]
    tr.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run_hook(
        json.dumps(
            {
                "session_id": "te1",
                "transcript_path": str(tr),
                "hook_event_name": "Stop",
            }
        ),
        settings=settings,
    )
    assert "hookSpecificOutput" not in capsys.readouterr().out
    # 3rd
    with open(tr, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "tool_result", "is_error": True, "content": "e3"}) + "\n")
    # reset offset by using same session - state has offset, will read only new
    run_hook(
        json.dumps(
            {
                "session_id": "te1",
                "transcript_path": str(tr),
                "hook_event_name": "Stop",
            }
        ),
        settings=settings,
    )
    out = capsys.readouterr().out
    assert "hookSpecificOutput" in out
    assert "ツールエラー" in out


def test_c1_redact_and_price_fail_closed_both_sides(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))

    def redactor(t: str) -> str:
        return t.replace("SECRET", "[R]")

    text = "please handle SECRET data carefully for me"

    # 対照A: 未知モデル → 金額句なし
    sid = "red1"
    st = load_state(sid)
    st["effective_debounce"] = 0
    save_state(sid, st)
    settings_unknown = {
        "enabled": True,
        "debounce_seconds": 0,
        "retry_threshold": 3,
        "cooldown_seconds": 0,
        "memory_dir": tmp_path / "mem",
        "pricing": {},
        "redactor": redactor,
    }
    tr = tmp_path / "tok.jsonl"
    # 3 user + assistant with tokens/unknown model interleaved via state chain
    for _ in range(3):
        run_hook(
            json.dumps(
                {
                    "session_id": sid,
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": text,
                }
            ),
            settings=settings_unknown,
        )
    out = capsys.readouterr().out
    assert "SECRET" not in out
    assert "推定$" not in out

    # 対照B: 既知モデル+tokens → 推定$ が出る
    sid2 = "red2"
    st2 = load_state(sid2)
    st2["effective_debounce"] = 0
    save_state(sid2, st2)
    # transcript に assistant usage を含める
    lines = []
    for i in range(3):
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": f"2026-08-01T10:0{i}:00Z",
                    "message": {"role": "user", "content": text},
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": f"2026-08-01T10:0{i}:30Z",
                    "message": {
                        "role": "assistant",
                        "model": "claude-sonnet-4",
                        "usage": {"output_tokens": 1_000_000},
                    },
                }
            )
        )
    tr.write_text("\n".join(lines) + "\n", encoding="utf-8")
    settings_known = {
        "enabled": True,
        "debounce_seconds": 0,
        "retry_threshold": 3,
        "cooldown_seconds": 0,
        "memory_dir": tmp_path / "mem2",
        "pricing": {"claude-sonnet": 3.0},
        "redactor": redactor,
    }
    with patch("kaizenlog.notify.notify", return_value=True):
        run_hook(
            json.dumps(
                {
                    "session_id": sid2,
                    "transcript_path": str(tr),
                    "hook_event_name": "UserPromptSubmit",
                }
            ),
            settings=settings_known,
        )
    out2 = capsys.readouterr().out
    assert "推定$" in out2
    assert "SECRET" not in out2


def test_c1_install_write_backup_and_idempotent(tmp_path: Path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": "other",
                            "hooks": [
                                {"type": "command", "command": "echo other-hook"}
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    cmd = build_hook_command(python_exe="python", config_path="c.toml")
    bak = install_hooks_write(settings_path, cmd)
    assert bak.is_file()
    data1 = json.loads(settings_path.read_text(encoding="utf-8"))
    # other hook 残存
    cmds = []
    for b in data1["hooks"]["UserPromptSubmit"]:
        for h in b.get("hooks") or []:
            cmds.append(h.get("command"))
    assert "echo other-hook" in cmds
    assert any("guard" in str(c) and "--hook" in str(c) for c in cmds)
    # 冪等
    install_hooks_write(settings_path, cmd)
    data2 = json.loads(settings_path.read_text(encoding="utf-8"))
    # guard が二重でない
    guard_cmds = [
        c
        for c in [
            h.get("command")
            for b in data2["hooks"]["UserPromptSubmit"]
            for h in (b.get("hooks") or [])
        ]
        if c and "guard" in c and "--hook" in c
    ]
    assert len(guard_cmds) == 1
    # JSON 不正 → ゼロ書き込み
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    before = bad.read_bytes()
    with pytest.raises(ValueError):
        install_hooks_write(bad, cmd)
    assert bad.read_bytes() == before


def test_c1_perf_smoke_1000_lines(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    tr = tmp_path / "big.jsonl"

    def to_letters(n: int, width: int = 10) -> str:
        s = []
        for _ in range(width):
            s.append(chr(97 + (n % 26)))
            n //= 26
        return "".join(s)

    with open(tr, "w", encoding="utf-8") as f:
        for i in range(1000):
            # 共有テンプレを避け、正規化後も類似度が下がるよう十分異なる本文
            body = f"{to_letters(i)} {to_letters(i * 13 + 5)} {to_letters(i * 29 + 11)}"
            f.write(
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": f"2026-08-01T{i//3600:02d}:{(i//60)%60:02d}:{i%60:02d}Z",
                        "message": {"role": "user", "content": body},
                    }
                )
                + "\n"
            )
    # notify の PowerShell 起動を除外（性能は検知本体を測る）
    with patch("kaizenlog.notify.notify", return_value=True):
        t0 = time.perf_counter()
        rc = run_hook(
            json.dumps(
                {
                    "session_id": "perf",
                    "transcript_path": str(tr),
                    "hook_event_name": "UserPromptSubmit",
                }
            ),
            settings={
                "enabled": True,
                "debounce_seconds": 0,
                "retry_threshold": 99,
                "cooldown_seconds": 9999,
                "memory_dir": tmp_path / "m",
            },
        )
        elapsed = time.perf_counter() - t0
    assert rc == 0
    assert elapsed < 2.0


def test_b1_breaker_line_and_cleanup(tmp_path: Path, monkeypatch):
    from kaizenlog.aiwork import AISession, render_aiwork_markdown
    from zoneinfo import ZoneInfo

    mem = tmp_path / "mem"
    mem.mkdir()
    day = date(2026, 8, 10)
    # UTC 同日正午
    append_live_episode(
        mem,
        {
            "ts": "2026-08-10T12:00:00+00:00",
            "session_id": "s",
            "kind": "retry",
            "chain_len": 3,
            "est_tokens": None,
            "est_usd": None,
            "excerpt": "x",
        },
    )
    append_live_episode(
        mem,
        {
            "ts": "2026-08-09T12:00:00+00:00",
            "session_id": "s",
            "kind": "retry",
            "chain_len": 3,
            "excerpt": "y",
        },
    )
    assert count_live_breaker_fires(mem, day, tz=ZoneInfo("UTC")) == 1
    sess = [
        AISession(
            session_id="1",
            project="p",
            start=datetime(2026, 8, 10, 10, tzinfo=timezone.utc),
            end=datetime(2026, 8, 10, 11, tzinfo=timezone.utc),
            user_turns=3,
        )
    ]
    md = render_aiwork_markdown(sess, timezone.utc, breaker_fires=1)
    assert "⚡ ブレーカー発動: 1回" in md
    md0 = render_aiwork_markdown(sess, timezone.utc, breaker_fires=0)
    assert "ブレーカー" not in md0

    # cleanup: 10日超は削除、ちょうど7日は残す（mtime < cutoff のみ削除）
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    d = state_dir()
    d.mkdir(parents=True)
    import os

    old = d / "old.json"
    old.write_text("{}", encoding="utf-8")
    old_ts = time.time() - 10 * 86400
    os.utime(old, (old_ts, old_ts))
    edge = d / "edge7.json"
    edge.write_text("{}", encoding="utf-8")
    # ちょうど7日前よりわずかに新しい → 残る
    edge_ts = time.time() - 7 * 86400 + 60
    os.utime(edge, (edge_ts, edge_ts))
    n = cleanup_old_states(max_age_days=7)
    assert n >= 1
    assert not old.exists()
    assert edge.exists()


def test_b1_doctor_guard_no_mkdir(tmp_path: Path, monkeypatch):
    from kaizenlog.config import Config
    from kaizenlog.doctor import run_doctor

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la_absent"))
    monkeypatch.chdir(tmp_path)
    cfg = Config(vault_dir=tmp_path)
    (tmp_path / "01 Daily Notes").mkdir()
    guard_dir = tmp_path / "la_absent" / "kaizenlog" / "guard"
    assert not guard_dir.exists()
    report, _ = run_doctor(cfg)
    assert "未作成" in report
    assert not guard_dir.exists()


def test_r1_effective_debounce_zero_allows_chain(tmp_path: Path, monkeypatch):
    """effective_debounce=0 なら連続3回で chain_len=3。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    sid = "deb0"
    st = load_state(sid)
    st["effective_debounce"] = 0
    save_state(sid, st)
    settings = {
        "enabled": True,
        "debounce_seconds": 0,
        "retry_threshold": 99,
        "cooldown_seconds": 9999,
        "memory_dir": tmp_path / "m",
    }
    text = "retry the same careful implementation request please"
    for _ in range(3):
        run_hook(
            json.dumps(
                {
                    "session_id": sid,
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": text,
                }
            ),
            settings=settings,
        )
    st2 = load_state(sid)
    assert int(st2.get("chain_len") or 0) == 3
    assert float(st2.get("effective_debounce")) == 0.0


def test_a1_task_notification_does_not_fire_retry(tmp_path: Path, monkeypatch, capsys):
    """§A1: task-notification 連投はリトライ連鎖に数えない。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    sid = "xml-spam"
    st = load_state(sid)
    st["effective_debounce"] = 0
    save_state(sid, st)
    settings = {
        "enabled": True,
        "debounce_seconds": 0,
        "retry_threshold": 3,
        "cooldown_seconds": 0,
        "memory_dir": tmp_path / "m",
    }
    xml = '<task-notification> <task-id>a1d9fdd05e0</task-id> done'
    for _ in range(5):
        run_hook(
            json.dumps(
                {
                    "session_id": sid,
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": xml,
                }
            ),
            settings=settings,
        )
    out = capsys.readouterr().out
    assert "hookSpecificOutput" not in out
    st2 = load_state(sid)
    assert int(st2.get("chain_len") or 0) == 0


def test_r1_effective_debounce_60_blocks_early(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    sid = "deb60"
    st = load_state(sid)
    st["last_parse_ts"] = time.time() - 31  # 31秒前
    st["effective_debounce"] = 60
    save_state(sid, st)
    opened = []
    real_open = open

    def spy(path, *a, **k):
        if "t.jsonl" in str(path):
            opened.append(str(path))
        return real_open(path, *a, **k)

    tr = tmp_path / "t.jsonl"
    tr.write_text("{}\n", encoding="utf-8")
    with patch("builtins.open", side_effect=spy):
        rc = run_hook(
            json.dumps(
                {
                    "session_id": sid,
                    "transcript_path": str(tr),
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "x" * 20,
                }
            ),
            settings={"enabled": True, "debounce_seconds": 60, "memory_dir": tmp_path / "m"},
        )
    assert rc == 0
    assert opened == []


def test_r2_empty_hooks_block_preserved(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    original = {
        "hooks": {
            "UserPromptSubmit": [
                {"matcher": "keepme", "hooks": []},
            ]
        }
    }
    settings_path.write_text(
        json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    cmd = 'python -m kaizenlog.cli guard --hook'
    install_hooks_write(settings_path, cmd)
    data1 = json.loads(settings_path.read_text(encoding="utf-8"))
    matchers = [b.get("matcher") for b in data1["hooks"]["UserPromptSubmit"]]
    assert "keepme" in matchers
    empty = [
        b
        for b in data1["hooks"]["UserPromptSubmit"]
        if b.get("matcher") == "keepme"
    ]
    assert empty and empty[0].get("hooks") == []
    # 冪等
    before = settings_path.read_bytes()
    install_hooks_write(settings_path, cmd)
    data2 = json.loads(settings_path.read_text(encoding="utf-8"))
    keep = [b for b in data2["hooks"]["UserPromptSubmit"] if b.get("matcher") == "keepme"]
    assert keep and keep[0].get("hooks") == []


def test_r3_timezone_jst_boundary():
    from zoneinfo import ZoneInfo

    mem = Path  # placeholder overwritten
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        mem = Path(td)
        # UTC 2026-07-29 22:30 = JST 2026-07-30 07:30
        append_live_episode(
            mem,
            {
                "ts": "2026-07-29T22:30:00+00:00",
                "session_id": "s",
                "kind": "retry",
                "chain_len": 3,
                "excerpt": "x",
            },
        )
        jst = ZoneInfo("Asia/Tokyo")
        assert count_live_breaker_fires(mem, date(2026, 7, 30), tz=jst) == 1
        assert count_live_breaker_fires(mem, date(2026, 7, 29), tz=jst) == 0
        # naive / 不正はスキップ
        p = mem / "live_episodes.jsonl"
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-07-30T01:00:00", "kind": "retry"}) + "\n")
            f.write(json.dumps({"ts": "not-a-date", "kind": "retry"}) + "\n")
        assert count_live_breaker_fires(mem, date(2026, 7, 30), tz=jst) == 1


def test_r4_tool_error_reset_on_success(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    settings = {
        "enabled": True,
        "debounce_seconds": 0,
        "retry_threshold": 99,
        "tool_error_streak": 3,
        "cooldown_seconds": 0,
        "memory_dir": tmp_path / "mem",
    }
    sid = "tereset"
    st = load_state(sid)
    st["effective_debounce"] = 0
    save_state(sid, st)

    def user_tool_err():
        return {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "is_error": True, "content": "err"}],
            },
        }

    def user_tool_ok():
        return {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "is_error": False, "content": "ok"}],
            },
        }

    # エラー2 → 成功1 → エラー2 = 非発火
    seq = [user_tool_err(), user_tool_err(), user_tool_ok(), user_tool_err(), user_tool_err()]
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(json.dumps(r) for r in seq) + "\n", encoding="utf-8")
    with patch("kaizenlog.notify.notify", return_value=True):
        run_hook(
            json.dumps(
                {
                    "session_id": sid,
                    "transcript_path": str(tr),
                    "hook_event_name": "Stop",
                }
            ),
            settings=settings,
        )
    assert "hookSpecificOutput" not in capsys.readouterr().out
    assert int(load_state(sid).get("tool_error_streak") or 0) == 2

    # エラー3連続で発火
    sid2 = "te3"
    st = load_state(sid2)
    st["effective_debounce"] = 0
    save_state(sid2, st)
    tr2 = tmp_path / "t2.jsonl"
    tr2.write_text(
        "\n".join(json.dumps(user_tool_err()) for _ in range(3)) + "\n",
        encoding="utf-8",
    )
    with patch("kaizenlog.notify.notify", return_value=True):
        run_hook(
            json.dumps(
                {
                    "session_id": sid2,
                    "transcript_path": str(tr2),
                    "hook_event_name": "Stop",
                }
            ),
            settings=settings,
        )
    assert "ツールエラー" in capsys.readouterr().out

    # 混在レコード(エラー+成功)はエラー優先・リセットしない → その後エラー1で合計3発火
    sid3 = "temix"
    st = load_state(sid3)
    st["effective_debounce"] = 0
    save_state(sid3, st)
    mixed = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "is_error": True, "content": "e"},
                {"type": "tool_result", "is_error": False, "content": "ok"},
            ],
        },
    }
    tr3 = tmp_path / "t3.jsonl"
    rows = [user_tool_err(), user_tool_err(), mixed, user_tool_err()]
    # streak: +1 +1 +1(mixed has error) +1 = 4 → fire at 3
    tr3.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    with patch("kaizenlog.notify.notify", return_value=True):
        run_hook(
            json.dumps(
                {
                    "session_id": sid3,
                    "transcript_path": str(tr3),
                    "hook_event_name": "Stop",
                }
            ),
            settings=settings,
        )
    assert "ツールエラー" in capsys.readouterr().out


def test_r6_cmd_generate_breaker_line(tmp_path: Path):
    """cmd_generate 実配線で ⚡ 行が出る/出ない。"""
    from kaizenlog.cli import cmd_generate
    from kaizenlog.config import Config
    from kaizenlog.report import DailySummary
    from kaizenlog.vault import ACTIVITY_MARKER, DailyNoteStore, extract_section

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
    day = date(2026, 8, 10)
    for _ in range(3):
        append_live_episode(
            cfg.memory_path,
            {
                "ts": "2026-08-10T15:00:00+00:00",
                "session_id": "s",
                "kind": "retry",
                "chain_len": 3,
                "excerpt": "x",
            },
        )
    summary = DailySummary(
        day=day,
        total_minutes=30,
        by_category={"開発": 30},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
    )
    sess = [
        __import__("kaizenlog.aiwork", fromlist=["AISession"]).AISession(
            session_id="1",
            project="p",
            start=datetime(2026, 8, 10, 10, tzinfo=timezone.utc),
            end=datetime(2026, 8, 10, 11, tzinfo=timezone.utc),
            user_turns=2,
        )
    ]
    with (
        patch("kaizenlog.cli.collect_day", return_value=([], True)),
        patch("kaizenlog.cli.collect_input", return_value=None),
        patch("kaizenlog.cli.summarize", return_value=summary),
        patch("kaizenlog.cli.render_markdown", return_value="### activity\n"),
        patch("kaizenlog.cli.available_adapters", return_value=[MagicMock()]),
        patch(
            "kaizenlog.cli.collect_ai_telemetry",
            return_value=(sess, [], 0),
        ),
        patch("kaizenlog.cli.ActivityWatchClient"),
        patch("kaizenlog.cli.Classifier") as Cls,
        patch("kaizenlog.decay.run_decay_detection", return_value=[]),
        patch("kaizenlog.coachledger.judge_coach_entries", return_value=[]),
        patch("kaizenlog.cli.load_experiments", return_value=[]),
        patch("kaizenlog.cli.detect_regressions", return_value=[]),
        patch("kaizenlog.cli.judge_entries", return_value=[]),
        patch("kaizenlog.cli.load_entries", return_value=[]),
        patch("kaizenlog.cli.notify"),
    ):
        Cls.return_value.classify_all.return_value = []
        cmd_generate(cfg, day)
    note = DailyNoteStore(cfg.daily_notes_path).read(day)
    assert note is not None
    sec = extract_section(note, ACTIVITY_MARKER) or note
    assert "⚡ ブレーカー発動: 3回" in sec

    # 0件の日
    day2 = date(2026, 8, 11)
    summary2 = DailySummary(
        day=day2,
        total_minutes=10,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
    )
    with (
        patch("kaizenlog.cli.collect_day", return_value=([], True)),
        patch("kaizenlog.cli.collect_input", return_value=None),
        patch("kaizenlog.cli.summarize", return_value=summary2),
        patch("kaizenlog.cli.render_markdown", return_value="### activity\n"),
        patch("kaizenlog.cli.available_adapters", return_value=[MagicMock()]),
        patch(
            "kaizenlog.cli.collect_ai_telemetry",
            return_value=(sess, [], 0),
        ),
        patch("kaizenlog.cli.ActivityWatchClient"),
        patch("kaizenlog.cli.Classifier") as Cls2,
        patch("kaizenlog.decay.run_decay_detection", return_value=[]),
        patch("kaizenlog.coachledger.judge_coach_entries", return_value=[]),
        patch("kaizenlog.cli.load_experiments", return_value=[]),
        patch("kaizenlog.cli.detect_regressions", return_value=[]),
        patch("kaizenlog.cli.judge_entries", return_value=[]),
        patch("kaizenlog.cli.load_entries", return_value=[]),
        patch("kaizenlog.cli.notify"),
    ):
        Cls2.return_value.classify_all.return_value = []
        cmd_generate(cfg, day2)
    note2 = DailyNoteStore(cfg.daily_notes_path).read(day2)
    assert "ブレーカー" not in (note2 or "")
