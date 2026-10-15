from kaizenlog.config import Config
from datetime import date
from pathlib import Path

from kaizenlog.preflight import build_preflight_context, deliver_preflight_once
from kaizenlog.vault import DailyNoteStore
from kaizenlog.guard import build_hooks_snippet, merge_hooks_into_settings


def test_preflight_is_redacted_deterministic_and_within_conservative_context_bound(tmp_path: Path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "suggestions.jsonl").write_text(
        '{"id":"KZN-20260905-001","date":"2026-09-05","action":"SECRET ship the fixed report","status":"proposed"}\n'
        '{"id":"KZN-20260904-001","date":"2026-09-04","action":"old","status":"done","verdict":"pass","verdict_date":"2026-09-05","verdict_stage":"confirmed"}\n',
        encoding="utf-8",
    )
    notes = tmp_path / "notes"
    cfg = Config(vault_dir=notes.parent, daily_notes_dir=notes.name)
    store = DailyNoteStore(cfg)
    store.dir.mkdir()
    store.path_for(date(2026, 9, 6)).write_text(
        "<!-- kaizenlog:goal:start -->\n🎯 今日の目標: SECRET finish verification\n<!-- kaizenlog:goal:end -->\n",
        encoding="utf-8",
    )

    context = build_preflight_context(
        memory_dir=memory,
        cfg=cfg,
        today=date(2026, 9, 6),
        redactor=lambda text: text.replace("SECRET", "[REDACTED]"),
        max_chars=240,
    )

    assert "KZN-20260905-001" in context
    assert "KZN-20260904-001" in context
    assert "[REDACTED]" in context
    assert "SECRET" not in context
    assert len(context) <= 240


def test_preflight_omits_unknown_sections_and_never_fills_them_from_guesses(tmp_path: Path):
    cfg = Config(vault_dir=tmp_path, daily_notes_dir="missing-notes")
    assert build_preflight_context(
        memory_dir=tmp_path / "missing-memory",
        cfg=cfg,
        today=date(2026, 9, 6),
        max_chars=240,
    ) == ""


def test_preflight_daily_debounce_is_repo_scoped_and_does_not_suppress_failed_delivery(tmp_path: Path):
    delivered: list[dict] = []
    common = {
        "context": "redacted context",
        "today": date(2026, 9, 6),
        "state_root": tmp_path / "state",
    }
    assert not deliver_preflight_once(
        **common,
        repo_path=tmp_path / "repo-a",
        deliver=lambda _output: False,
    )
    assert deliver_preflight_once(
        **common,
        repo_path=tmp_path / "repo-a",
        deliver=lambda output: delivered.append(output) or True,
    )
    assert not deliver_preflight_once(
        **common,
        repo_path=tmp_path / "repo-a",
        deliver=lambda output: delivered.append(output) or True,
    )
    assert deliver_preflight_once(
        **common,
        repo_path=tmp_path / "repo-b",
        deliver=lambda output: delivered.append(output) or True,
    )
    assert [item["hookSpecificOutput"]["hookEventName"] for item in delivered] == [
        "SessionStart",
        "SessionStart",
    ]


def test_guard_hook_merge_adds_session_start_without_replacing_user_hooks():
    settings = {
        "hooks": {
            "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "user-hook"}]}]
        }
    }
    merged = merge_hooks_into_settings(
        settings,
        build_hooks_snippet("kaizenlog guard --hook", preflight_command="kaizenlog guard --preflight-hook"),
    )
    commands = [hook["command"] for block in merged["hooks"]["SessionStart"] for hook in block["hooks"]]
    assert commands == ["user-hook", "kaizenlog guard --preflight-hook"]
    merged_from_install_pipeline = merge_hooks_into_settings(
        settings,
        build_hooks_snippet("kaizenlog guard --hook"),
        preflight_command="kaizenlog guard --preflight-hook",
    )
    pipelined_commands = [
        hook["command"]
        for block in merged_from_install_pipeline["hooks"]["SessionStart"]
        for hook in block["hooks"]
    ]
    assert pipelined_commands == ["user-hook", "kaizenlog guard --preflight-hook"]


def test_actual_cli_preflight_hook_is_replaced_idempotently():
    from kaizenlog.guard import build_hook_command
    command = build_hook_command(python_exe='C:/Program Files/Python/python.exe', config_path='C:/cfg.toml')
    preflight = command.removesuffix('guard --hook') + 'preflight --hook'
    foreign = {'matcher': '', 'hooks': [{'type': 'command', 'command': 'user-hook'}]}
    settings = {'hooks': {'SessionStart': [foreign]}}
    snippet = build_hooks_snippet(command, preflight_command=preflight)
    first = merge_hooks_into_settings(settings, snippet)
    assert merge_hooks_into_settings(first, snippet) == first
    new = build_hooks_snippet(command, preflight_command=preflight.replace('cfg.toml', 'new.toml'))
    merged = merge_hooks_into_settings(first, new)
    commands = [h['command'] for b in merged['hooks']['SessionStart'] for h in b['hooks']]
    assert commands == ['user-hook', preflight.replace('cfg.toml', 'new.toml')]
