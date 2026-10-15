import json
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from kaizenlog.guard_watch import watch_once
from kaizenlog.aiwork import UserPrompt, detect_retry_chains

_LIVE_NOW = datetime(2026, 9, 6, 0, 5, tzinfo=timezone.utc).timestamp()


def _jsonl(path: Path, rows: list[dict], *, newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(row) for row in rows)
    path.write_text(body + ("\n" if newline else ""), encoding="utf-8")


def _codex_user(session_id: str, text: str, second: int) -> dict:
    return {
        "type": "event_msg",
        "timestamp": f"2026-09-06T00:00:{second:02d}+00:00",
        "payload": {"type": "user_message", "message": text, "session_id": session_id},
    }


def test_watch_once_keeps_partial_tail_and_notifies_only_after_a_same_session_chain(tmp_path: Path):
    codex_dir = tmp_path / "codex"
    log = codex_dir / "2026" / "09" / "06" / "rollout-a.jsonl"
    first = _codex_user("session-a", "Please repair this exact failing test now", 1)
    second = _codex_user("session-a", "Please repair this exact failing test now", 2)
    third = _codex_user("session-a", "Please repair this exact failing test now", 3)
    _jsonl(log, [], newline=False)

    notifications: list[tuple[str, str]] = []
    kwargs = {
        "codex_sessions_dir": codex_dir,
        "browser_export_dir": tmp_path / "browser",
        "state_root": tmp_path / "state",
        "memory_dir": tmp_path / "memory",
        "retry_threshold": 3,
        "notify_enabled": True,
        "notifier": lambda title, message: notifications.append((title, message)) or True,
    }
    # Initial discovery consumes no history. Subsequent complete rows are live.
    assert watch_once(**kwargs) == []
    _jsonl(log, [first, second], newline=False)
    assert watch_once(**kwargs, now=_LIVE_NOW) == []
    assert notifications == []

    with log.open("a", encoding="utf-8") as f:
        f.write("\n" + json.dumps(third) + "\n")
    events = watch_once(**kwargs, now=_LIVE_NOW)

    assert len(events) == 1
    assert events[0]["session_id"] == "codex:session-a"
    assert events[0]["est_tokens"] is None
    assert events[0]["est_usd"] is None
    assert len(notifications) == 1
    assert (tmp_path / "memory" / "live_episodes.jsonl").is_file()
    assert not (tmp_path / "memory" / "stats").exists()
    assert "Please repair" not in (tmp_path / "state" / "guard-watch.json").read_text(encoding="utf-8")


def test_watch_once_never_crosses_browser_conversations_or_records_when_notify_is_off(tmp_path: Path):
    browser_dir = tmp_path / "browser"
    path = browser_dir / "2026-09-06.jsonl"
    rows = []
    for conversation_id in ("one", "two"):
        for second in (1, 2):
            rows.append(
                {
                    "ts": f"2026-09-06T00:00:{second:02d}+00:00",
                    "site": "chatgpt.com",
                    "conversation_id": conversation_id,
                    "role": "user",
                    "text": "Same browser request should stay separate",
                }
            )
    _jsonl(path, rows)

    assert watch_once(
        codex_sessions_dir=tmp_path / "codex",
        browser_export_dir=browser_dir,
        state_root=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        retry_threshold=2,
        notify_enabled=False,
    ) == []
    assert not (tmp_path / "memory" / "live_episodes.jsonl").exists()


def test_watch_once_resets_a_same_size_replaced_file_and_retries_failed_notification(tmp_path: Path):
    codex_dir = tmp_path / "codex"
    log = codex_dir / "2026" / "09" / "06" / "rollout-a.jsonl"
    _jsonl(log, [])
    base = {
        "codex_sessions_dir": codex_dir,
        "browser_export_dir": tmp_path / "browser",
        "state_root": tmp_path / "state",
        "memory_dir": tmp_path / "memory",
        "retry_threshold": 2,
        "notify_enabled": True,
        "cooldown_seconds": 0,
        "now": _LIVE_NOW,
    }
    assert watch_once(**base) == []
    rows = [_codex_user("s", "same size replacement request", 1), _codex_user("s", "same size replacement request", 2)]
    _jsonl(log, rows)
    assert watch_once(**base, notifier=lambda *_: (_ for _ in ()).throw(RuntimeError("toast"))) == []
    # Replacing bytes at the same path and length is rotation, not a completed cursor.
    _jsonl(log, rows)
    calls: list[str] = []
    delivered = watch_once(**base, notifier=lambda _title, message: calls.append(message) or True)
    assert len(delivered) == 1
    assert len(calls) == 1


def test_watch_once_skips_an_oversized_unterminated_line_without_blocking_later_rows(tmp_path: Path):
    codex_dir = tmp_path / "codex"
    log = codex_dir / "2026" / "09" / "06" / "rollout-a.jsonl"
    _jsonl(log, [])
    kwargs = {
        "codex_sessions_dir": codex_dir,
        "browser_export_dir": tmp_path / "browser",
        "state_root": tmp_path / "state",
        "memory_dir": tmp_path / "memory",
        "retry_threshold": 2,
        "notify_enabled": False,
    }
    watch_once(**kwargs)
    with log.open("ab") as f:
        f.write(b"x" * (600 * 1024))
        f.write(b"\n" + json.dumps(_codex_user("s", "later complete event", 4)).encode("utf-8") + b"\n")
    assert watch_once(**kwargs) == []
    assert watch_once(**kwargs) == []
    state = json.loads((tmp_path / "state" / "guard-watch.json").read_text(encoding="utf-8"))
    assert next(iter(state["files"].values()))["offset"] > 600 * 1024


def test_watch_once_consumes_old_history_without_a_notification(tmp_path: Path):
    codex_dir = tmp_path / "codex"
    log = codex_dir / "2026" / "09" / "06" / "rollout-a.jsonl"
    _jsonl(log, [])
    kwargs = {
        "codex_sessions_dir": codex_dir,
        "browser_export_dir": tmp_path / "browser",
        "state_root": tmp_path / "state",
        "memory_dir": tmp_path / "memory",
        "retry_threshold": 2,
        "notify_enabled": True,
        "now": _LIVE_NOW + 7200,
    }
    watch_once(**kwargs)
    _jsonl(log, [_codex_user("s", "old historical retry", 1), _codex_user("s", "old historical retry", 2)])
    assert watch_once(**kwargs, notifier=lambda *_: (_ for _ in ()).throw(AssertionError("must stay quiet"))) == []


def test_concurrent_once_runs_deliver_one_notification(tmp_path: Path):
    codex_dir = tmp_path / "codex"
    log = codex_dir / "2026" / "09" / "06" / "rollout-a.jsonl"
    _jsonl(log, [])
    kwargs = {
        "codex_sessions_dir": codex_dir,
        "browser_export_dir": tmp_path / "browser",
        "state_root": tmp_path / "state",
        "memory_dir": tmp_path / "memory",
        "retry_threshold": 2,
        "notify_enabled": True,
        "now": _LIVE_NOW,
    }
    watch_once(**kwargs)
    _jsonl(log, [_codex_user("s", "concurrent retry request", 1), _codex_user("s", "concurrent retry request", 2)])
    calls: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: watch_once(**kwargs, notifier=lambda _t, msg: calls.append(msg) or True), range(2)))
    assert sum(len(result) for result in results) == 1
    assert len(calls) == 1


def test_watch_once_uses_the_nightly_near_match_algorithm_across_two_runs(tmp_path: Path):
    codex_dir = tmp_path / "codex"
    log = codex_dir / "2026" / "09" / "06" / "rollout-a.jsonl"
    _jsonl(log, [])
    first = "Please repair failing test now immediately"
    second = "Please repair failing test now promptly"
    first_ts = datetime(2026, 9, 6, 0, 1, tzinfo=timezone.utc)
    second_ts = datetime(2026, 9, 6, 0, 2, tzinfo=timezone.utc)
    nightly = detect_retry_chains(
        [UserPrompt(first_ts, "repo", first, "codex"), UserPrompt(second_ts, "repo", second, "codex")]
    )
    assert [chain.length for chain in nightly] == [2]
    kwargs = {
        "codex_sessions_dir": codex_dir,
        "browser_export_dir": tmp_path / "browser",
        "state_root": tmp_path / "state",
        "memory_dir": tmp_path / "memory",
        "retry_threshold": 2,
        "notify_enabled": True,
        "now": _LIVE_NOW,
    }
    watch_once(**kwargs)
    _jsonl(log, [_codex_user("s", first, 1)])
    assert watch_once(**kwargs) == []
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_codex_user("s", second, 2)) + "\n")
    calls: list[str] = []
    assert len(watch_once(**kwargs, notifier=lambda _t, message: calls.append(message) or True)) == 1
    assert len(calls) == 1
    state = (tmp_path / "state" / "guard-watch.json").read_text(encoding="utf-8")
    assert first not in state and second not in state


def test_default_notify_failure_is_not_a_delivery_and_remains_retryable(tmp_path, monkeypatch):
    log = tmp_path / 'codex' / 'rollout-a.jsonl'
    _jsonl(log, [_codex_user('a', 'Please fix this failing parser safely', second) for second in (1, 2, 3)])
    kwargs = dict(codex_sessions_dir=log.parent, browser_export_dir=tmp_path / 'browser',
                  state_root=tmp_path / 'state', memory_dir=tmp_path / 'memory',
                  retry_threshold=3, notify_enabled=True, now=_LIVE_NOW)
    monkeypatch.setattr('kaizenlog.notify.notify', lambda *_a, **_kw: False)
    assert watch_once(**kwargs) == []
    assert not (tmp_path / 'memory' / 'live_episodes.jsonl').exists()
    monkeypatch.setattr('kaizenlog.notify.notify', lambda *_a, **_kw: None)
    assert watch_once(**kwargs) == []
    monkeypatch.setattr('kaizenlog.notify.notify', lambda *_a, **_kw: True)
    assert len(watch_once(**kwargs)) == 1


def test_browser_nightly_and_live_keep_distinct_conversations_separate(tmp_path):
    from kaizenlog.aiwork_browser import BrowserAIAdapter
    from kaizenlog.aiwork import collect_ai_telemetry
    browser = tmp_path / 'browser'
    rows = [dict(ts=f'2026-09-06T00:00:0{index}+00:00', site='chatgpt.com',
                 conversation_id=conversation, role='user', text='Please repair this exact failing test now')
            for index, conversation in enumerate(('one', 'two', 'one'), 1)]
    _jsonl(browser / '2026-09-06.jsonl', rows)
    start = datetime(2026, 9, 6, tzinfo=timezone.utc)
    end = datetime(2026, 9, 7, tzinfo=timezone.utc)
    _, prompts, _ = collect_ai_telemetry([BrowserAIAdapter(browser)], start, end)
    assert [c.length for c in detect_retry_chains(prompts)] == [2]
    assert watch_once(codex_sessions_dir=tmp_path / 'codex', browser_export_dir=browser,
                      state_root=tmp_path / 'state', memory_dir=tmp_path / 'memory',
                      retry_threshold=3, notify_enabled=True, now=_LIVE_NOW,
                      notifier=lambda *_: True) == []


def test_codex_nightly_and_live_exclude_internal_and_wrapper_prompts(tmp_path):
    from kaizenlog.aiwork_codex import CodexAdapter
    from kaizenlog.aiwork import collect_ai_telemetry
    codex = tmp_path / 'codex'
    path = codex / '2026' / '09' / '06' / 'rollout-a.jsonl'
    text = '<environment_context>workspace instructions</environment_context>'
    _jsonl(path, [_codex_user('a', text, n) for n in (1, 2, 3)])
    start = datetime(2026, 9, 6, tzinfo=timezone.utc)
    end = datetime(2026, 9, 7, tzinfo=timezone.utc)
    _, prompts, _ = collect_ai_telemetry([CodexAdapter(codex)], start, end)
    assert prompts == []
    assert watch_once(codex_sessions_dir=codex, browser_export_dir=tmp_path / 'browser',
                      state_root=tmp_path / 'state', memory_dir=tmp_path / 'memory',
                      retry_threshold=3, notify_enabled=True, now=_LIVE_NOW,
                      notifier=lambda *_: True) == []
