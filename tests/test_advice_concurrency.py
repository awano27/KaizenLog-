import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from kaizenlog import cli
from kaizenlog.advisor import AdvisorError
from kaizenlog.memory import MemoryEntry, append_entries, load_entries
from kaizenlog.vault import ACTIVITY_MARKER, ADVICE_MARKER, DailyNoteStore
from tests.test_advise_integration import DAY, VALID_GENERATED, _config


def _setup(tmp_path):
    cfg = _config(tmp_path)
    cfg.aiwork.enabled = False
    store = DailyNoteStore(cfg.daily_notes_path)
    store.write_section(DAY, ACTIVITY_MARKER, "## Activity Log\n**合計**: 10m")
    return cfg, store


def test_late_generation_does_not_replace_a_concurrently_completed_action(monkeypatch, tmp_path):
    cfg, store = _setup(tmp_path)
    started = False

    def generate(*args, **kwargs):
        nonlocal started
        if not started:
            started = True
            cli.cmd_advise(cfg, DAY)
            assert cli.cmd_done(cfg, "KZN-20260721-001", DAY) == 0
            return VALID_GENERATED.replace("始業時に集中枠", "終了時に新しい枠")
        return VALID_GENERATED

    monkeypatch.setattr(cli, "generate_advice", generate)
    cli.cmd_advise(cfg, DAY)
    entries = {e.id: e for e in load_entries(cfg.memory_path)}
    assert entries["KZN-20260721-001"].status == "done"
    assert "始業時に集中枠" in entries["KZN-20260721-001"].action
    assert any("終了時に新しい枠" in e.action and e.id != "KZN-20260721-001" for e in entries.values())


def test_user_completion_during_generation_is_recorded_before_replacement(monkeypatch, tmp_path):
    cfg, store = _setup(tmp_path)
    action_id = "KZN-20260721-001"
    append_entries(cfg.memory_path, [MemoryEntry(action_id, DAY.isoformat(), "existing")])
    store.write_section(DAY, ADVICE_MARKER, f"- [ ] {action_id}: existing")

    def generate(*args, **kwargs):
        store.write_section(DAY, ADVICE_MARKER, f"- [x] {action_id}: existing")
        return VALID_GENERATED

    monkeypatch.setattr(cli, "generate_advice", generate)
    cli.cmd_advise(cfg, DAY)
    assert {e.id: e for e in load_entries(cfg.memory_path)}[action_id].status == "done"


def test_rollback_does_not_remove_unexpected_ledger_update(monkeypatch, tmp_path):
    cfg, store = _setup(tmp_path)
    original_note = store.path_for(DAY).read_bytes()
    external = MemoryEntry("KZN-20260721-999", DAY.isoformat(), "independent update")

    def fail_after_external_append(memory_dir, entries):
        append_entries(memory_dir, [external])
        raise OSError("injected independent writer then save failure")

    monkeypatch.setattr(cli, "append_entries", fail_after_external_append)
    with pytest.raises(AdvisorError, match="suggestions"):
        cli._save_advice_with_entries(store, DAY, "new", cfg.memory_path, [MemoryEntry("KZN-20260721-001", DAY.isoformat(), "ours")])
    assert [e.id for e in load_entries(cfg.memory_path)] == ["KZN-20260721-999"]
    assert store.path_for(DAY).read_bytes() == original_note


def test_memory_lock_blocks_other_process_and_is_released(tmp_path):
    from kaizenlog.memory import memory_lock

    code = """import sys
from pathlib import Path
from kaizenlog.memory import memory_lock
try:
    with memory_lock(Path(sys.argv[1]), timeout=0.15):
        print('acquired')
except TimeoutError:
    print('busy')
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), PYTHONDONTWRITEBYTECODE="1")
    def attempt():
        return subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=5, env=env, check=True).stdout.strip()
    with memory_lock(tmp_path):
        assert attempt() == "busy"
        with memory_lock(tmp_path):
            pass
    assert attempt() == "acquired"


def test_save_conflict_preserves_note_edit_after_our_write(monkeypatch, tmp_path):
    cfg, store = _setup(tmp_path)
    original_write = store.write_section
    def edit_after_write(*args, **kwargs):
        path = original_write(*args, **kwargs)
        path.write_bytes(path.read_bytes() + b"\nuser edit\n")
        return path
    monkeypatch.setattr(store, "write_section", edit_after_write)
    with pytest.raises(AdvisorError):
        cli._save_advice_with_entries(store, DAY, "new", cfg.memory_path, [MemoryEntry("KZN-20260721-001", DAY.isoformat(), "ours")])
    assert store.path_for(DAY).read_bytes().endswith(b"user edit\n")
    assert load_entries(cfg.memory_path) == []
