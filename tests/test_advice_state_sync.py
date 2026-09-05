from datetime import timedelta

import pytest

from kaizenlog import cli
from kaizenlog.memory import MemoryEntry, append_entries, load_entries, update_statuses_from_note
from kaizenlog.vault import ACTIONS_MARKER, ACTIVITY_MARKER, ADVICE_MARKER, DailyNoteStore
from tests.test_advise_integration import DAY, VALID_GENERATED, _config


@pytest.mark.parametrize("newest,older,want", [("x", "-", "done"), ("-", "x", "skipped")])
@pytest.mark.parametrize("initial", ["proposed", "done", "skipped"])
def test_newest_explicit_state_is_stable_across_three_advise_runs(monkeypatch, tmp_path, newest, older, want, initial):
    cfg = _config(tmp_path)
    cfg.aiwork.enabled = False
    store = DailyNoteStore(cfg.daily_notes_path)
    prior = DAY - timedelta(days=1)
    action_id = "KZN-20260720-001"
    store.write_section(DAY, ACTIVITY_MARKER, "## Activity Log\n**合計**: 10m")
    store.write_section(DAY, ACTIONS_MARKER, f"- [{newest}] {action_id}: existing")
    store.write_section(prior, ADVICE_MARKER, f"- [{older}] {action_id}: existing")
    append_entries(cfg.memory_path, [MemoryEntry(action_id, prior.isoformat(), "existing", status=initial, verdict="pass")])
    monkeypatch.setattr(cli, "generate_advice", lambda *a, **k: VALID_GENERATED)
    for _ in range(3):
        cli.cmd_advise(cfg, DAY)
        entry = {e.id: e for e in load_entries(cfg.memory_path)}[action_id]
        assert entry.status == want
        assert entry.verdict == "pass"


def test_open_copy_does_not_undo_an_explicit_completion():
    entry = MemoryEntry("KZN-20260720-001", "2026-07-20", "existing", status="done")
    assert update_statuses_from_note("- [ ] KZN-20260720-001: existing", [entry], DAY) == []
