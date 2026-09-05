import json

import pytest

from kaizenlog import cli
from kaizenlog.memory import MemoryEntry, append_entries, load_entries
from kaizenlog.vault import ACTIVITY_MARKER, DailyNoteStore
from tests.test_advise_integration import DAY, VALID_GENERATED, _config


@pytest.mark.parametrize("tail", [b"", b"\n", b"\r\n", b"\r"])
def test_append_preserves_readable_final_record_and_existing_bytes(tmp_path, tail):
    original = b'{"id":"KZN-20260720-001","date":"2026-07-20","action":"old"}' + tail
    path = tmp_path / "suggestions.jsonl"
    path.write_bytes(original)
    append_entries(tmp_path, [MemoryEntry("KZN-20260721-001", "2026-07-21", "new")])
    assert path.read_bytes().startswith(original)
    assert [entry.id for entry in load_entries(tmp_path)] == ["KZN-20260720-001", "KZN-20260721-001"]


def test_advise_after_partial_tail_saves_readable_id_and_reuses_it(monkeypatch, tmp_path):
    cfg = _config(tmp_path)
    cfg.aiwork.enabled = False
    store = DailyNoteStore(cfg.daily_notes_path)
    store.write_section(DAY, ACTIVITY_MARKER, "## Activity Log\n\n**合計**: 10m")
    cfg.memory_path.mkdir(parents=True)
    path = cfg.memory_path / "suggestions.jsonl"
    original = b'{"id":"incomplete"'
    path.write_bytes(original)
    monkeypatch.setattr(cli, "generate_advice", lambda *a, **k: VALID_GENERATED)
    for _ in range(2):
        cli.cmd_advise(cfg, DAY)
        assert [entry.id for entry in load_entries(cfg.memory_path)] == ["KZN-20260721-001", "KZN-20260721-002"]
    assert path.read_bytes().startswith(original + b"\n")
    assert "KZN-20260721-001" in store.read(DAY)


def test_empty_append_does_not_change_incomplete_tail(tmp_path):
    path = tmp_path / "suggestions.jsonl"
    path.write_bytes(b'{"partial"')
    append_entries(tmp_path, [])
    assert path.read_bytes() == b'{"partial"'


def test_first_append_starts_with_json_without_blank_record(tmp_path):
    append_entries(tmp_path, [MemoryEntry("KZN-20260721-001", "2026-07-21", "new")])
    lines = (tmp_path / "suggestions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "KZN-20260721-001"
