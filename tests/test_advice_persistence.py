from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

import pytest

from kaizenlog import cli
from kaizenlog.advisor import AdvisorError
from kaizenlog.config import Config
from kaizenlog.memory import MemoryEntry, append_entries, load_entries
from kaizenlog.vault import (
    ACTIVITY_MARKER,
    ADVICE_MARKER,
    DailyNoteStore,
    atomic_write_bytes as vault_atomic_write_bytes,
)


DAY = date(2026, 7, 21)
EXISTING_ID = "KZN-20260721-001"
GENERATED = """## 🚀 Kaizen（AIからの改善提案）

### 今日の改善提案
1. 集中枠を固定する。
2. 調査リンクをまとめる。

### 明日の最小アクション
- [ ] 集中枠を1件入れる｜PASS: 2回以上｜FAIL: 1回以下
- [ ] 調査リンクを3件まとめる｜PASS: 遷移5回以下｜FAIL: 6回以上
"""


def _config(tmp_path: Path) -> Config:
    cfg = Config(vault_dir=tmp_path)
    cfg.llm.lookback_days = 0
    cfg.aiwork.enabled = False
    return cfg


def _seed_note(cfg: Config, *, checked_action: bool) -> DailyNoteStore:
    store = DailyNoteStore(cfg.daily_notes_path)
    store.write_section(DAY, ACTIVITY_MARKER, "## 📊 Activity Log\n\n**合計**: 10m")
    if checked_action:
        store.write_section(
            DAY,
            ADVICE_MARKER,
            "## 🚀 Kaizen\n\n### 明日の最小アクション\n"
            f"- [x] {EXISTING_ID}: 既存アクション",
        )
    return store


def _seed_checked_action_with_raw_bytes(cfg: Config) -> tuple[Path, Path]:
    store = _seed_note(cfg, checked_action=True)
    note_path = store.path_for(DAY)
    note_path.write_bytes(
        note_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n") + b"  "
    )

    append_entries(
        cfg.memory_path,
        [MemoryEntry(id=EXISTING_ID, date=DAY.isoformat(), action="既存アクション")],
    )
    ledger_path = cfg.memory_path / "suggestions.jsonl"
    ledger_path.write_bytes(
        ledger_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n") + b"  "
    )
    return note_path, ledger_path


def _generated_advice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "generate_advice", lambda *args, **kwargs: GENERATED)


def test_append_failure_before_writing_new_entries_restores_note_and_keeps_early_done_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _config(tmp_path)
    note_path, ledger_path = _seed_checked_action_with_raw_bytes(cfg)
    note_before = note_path.read_bytes()
    original_append = cli.append_entries
    ledger_after_done: bytes | None = None
    _generated_advice(monkeypatch)

    def fail_only_generated(memory_dir: Path, entries: list[MemoryEntry]) -> None:
        nonlocal ledger_after_done
        if entries[0].id == EXISTING_ID:
            original_append(memory_dir, entries)
            ledger_after_done = ledger_path.read_bytes()
            return
        raise OSError("generated ledger append failed before write")

    monkeypatch.setattr(cli, "append_entries", fail_only_generated)

    with pytest.raises(OSError, match="generated ledger append failed before write"):
        cli.cmd_advise(cfg, DAY)

    assert ledger_after_done is not None
    assert note_path.read_bytes() == note_before
    assert ledger_path.read_bytes() == ledger_after_done
    assert load_entries(cfg.memory_path)[0].status == "done"
    assert "✅ 改善提案を書き込みました" not in capsys.readouterr().out


def test_partial_generated_append_restores_exact_crlf_note_and_ledger_after_done_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _config(tmp_path)
    note_path, ledger_path = _seed_checked_action_with_raw_bytes(cfg)
    note_before = note_path.read_bytes()
    original_append = cli.append_entries
    ledger_after_done: bytes | None = None
    _generated_advice(monkeypatch)

    def partially_write_only_generated(memory_dir: Path, entries: list[MemoryEntry]) -> None:
        nonlocal ledger_after_done
        if entries[0].id == EXISTING_ID:
            original_append(memory_dir, entries)
            ledger_after_done = ledger_path.read_bytes()
            return
        original_append(memory_dir, entries[:1])
        with ledger_path.open("ab") as stream:
            stream.write(json.dumps(asdict(entries[1]), ensure_ascii=False).encode("utf-8")[:19])
        raise OSError("generated ledger append failed after partial write")

    monkeypatch.setattr(cli, "append_entries", partially_write_only_generated)

    with pytest.raises(OSError, match="generated ledger append failed after partial write"):
        cli.cmd_advise(cfg, DAY)

    assert ledger_after_done is not None
    assert b"\r\n" in note_before and note_before.endswith(b"  ")
    assert b"\r\n" in ledger_after_done and b"  " in ledger_after_done
    assert note_path.read_bytes() == note_before
    assert ledger_path.read_bytes() == ledger_after_done
    assert load_entries(cfg.memory_path)[0].status == "done"
    assert "✅ 改善提案を書き込みました" not in capsys.readouterr().out


def test_partial_append_with_no_prior_ledger_removes_new_ledger_on_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _config(tmp_path)
    store = _seed_note(cfg, checked_action=False)
    note_path = store.path_for(DAY)
    note_before = note_path.read_bytes()
    ledger_path = cfg.memory_path / "suggestions.jsonl"
    _generated_advice(monkeypatch)
    original_append = cli.append_entries

    def write_then_fail(memory_dir: Path, entries: list[MemoryEntry]) -> None:
        original_append(memory_dir, entries[:1])
        with ledger_path.open("ab") as stream:
            stream.write(json.dumps(asdict(entries[1]), ensure_ascii=False).encode("utf-8")[:19])
        raise OSError("new ledger append interrupted")

    monkeypatch.setattr(cli, "append_entries", write_then_fail)

    with pytest.raises(OSError, match="new ledger append interrupted"):
        cli.cmd_advise(cfg, DAY)

    assert note_path.read_bytes() == note_before
    assert not ledger_path.exists()


def test_ledger_restore_failure_reports_path_and_still_restores_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _config(tmp_path)
    note_path, ledger_path = _seed_checked_action_with_raw_bytes(cfg)
    note_before = note_path.read_bytes()
    original_append = cli.append_entries
    _generated_advice(monkeypatch)

    def partially_write_only_generated(memory_dir: Path, entries: list[MemoryEntry]) -> None:
        if entries[0].id == EXISTING_ID:
            original_append(memory_dir, entries)
            return
        original_append(memory_dir, entries[:1])
        with ledger_path.open("ab") as stream:
            stream.write(json.dumps(asdict(entries[1]), ensure_ascii=False).encode("utf-8")[:19])
        raise OSError("generated ledger append interrupted")

    def fail_ledger_restore(path: Path, content: bytes) -> None:
        if Path(path) == ledger_path:
            raise OSError("ledger restoration blocked")
        vault_atomic_write_bytes(path, content)

    monkeypatch.setattr(cli, "append_entries", partially_write_only_generated)
    monkeypatch.setattr(cli, "atomic_write_bytes", fail_ledger_restore, raising=False)

    with pytest.raises(AdvisorError, match="suggestions\\.jsonl") as exc_info:
        cli.cmd_advise(cfg, DAY)

    assert str(ledger_path) in str(exc_info.value)
    assert note_path.read_bytes() == note_before
    assert ledger_path.read_bytes() != b""
    assert "✅ 改善提案を書き込みました" not in capsys.readouterr().out
