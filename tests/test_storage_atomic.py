from __future__ import annotations
from kaizenlog.config import Config

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
import re
import threading
import time
from pathlib import Path

import pytest

from kaizenlog.file_lock import (
    file_lock,
    lock_path_for,
    locked_read_modify_write,
)
from kaizenlog.vault import (
    ACTIVITY_MARKER,
    ADVICE_MARKER,
    DailyNoteStore,
    atomic_write_bytes,
    atomic_write_text,
)


def test_lock_path_for_uses_one_fixed_sidecar_name(tmp_path: Path):
    target = tmp_path / "2026-10-15.md"

    assert lock_path_for(target) == tmp_path / "2026-10-15.md.lock"


def test_locked_read_modify_write_serializes_threads_and_reenters_same_thread(
    tmp_path: Path,
):
    target = tmp_path / "note.md"
    entered = threading.Event()
    nested_entered = threading.Event()
    release = threading.Event()
    acquired_second = threading.Event()

    def first_writer() -> None:
        with locked_read_modify_write(target, timeout=1.0):
            with locked_read_modify_write(target, timeout=1.0):
                nested_entered.set()
            entered.set()
            assert release.wait(1.0)

    def second_writer() -> None:
        assert entered.wait(1.0)
        with locked_read_modify_write(target, timeout=1.0):
            acquired_second.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_writer)
        second = pool.submit(second_writer)
        assert entered.wait(1.0)
        time.sleep(0.05)
        assert not acquired_second.is_set()
        release.set()
        first.result()
        second.result()
    assert nested_entered.is_set()
    assert acquired_second.is_set()


def test_lock_timeout_does_not_enter_context(tmp_path: Path):
    target = tmp_path / "note.md"
    before = "before\r\n手書き本文  \r\n".encode("utf-8")
    target.write_bytes(before)
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with file_lock(lock_path_for(target), timeout=1.0):
            entered.set()
            assert release.wait(1.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(holder)
        assert entered.wait(1.0)
        second_entered = False
        with pytest.raises(TimeoutError):
            with locked_read_modify_write(target, timeout=0.05):
                second_entered = True
        release.set()
        first.result()
    assert second_entered is False
    assert target.read_bytes() == before


def test_atomic_write_uses_unique_same_directory_temp_and_cleans_up_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "note.md"
    target.write_bytes(b"before")
    attempted: list[Path] = []

    def fail_replace(source: str | bytes | Path, destination: str | bytes | Path) -> None:
        attempted.append(Path(source))
        raise OSError("replace blocked")

    monkeypatch.setattr("kaizenlog.vault.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace blocked"):
        atomic_write_bytes(target, b"after")

    assert target.read_bytes() == b"before"
    assert len(attempted) == 1
    temp = attempted[0]
    assert temp.parent == target.parent
    assert re.fullmatch(r"\.note\.md\.\d+\.\d+\.[0-9a-f-]+\.tmp", temp.name)
    assert not temp.exists()


def _windows_deep_atomic_target(tmp_path: Path) -> Path:
    """Keep the final target below MAX_PATH while its temp sibling exceeds it."""
    target_name = "note.md"
    target_length = 220
    folder_length = target_length - len(os.fspath(tmp_path)) - 2 - len(target_name)
    assert folder_length > 20, (
        f"test root is too deep for the regression fixture: {tmp_path}"
    )
    parent = tmp_path / ("deep-" + "x" * (folder_length - len("deep-")))
    target = parent / target_name
    assert len(os.fspath(target)) == target_length
    assert len(os.fspath(target)) < 248
    parent.mkdir()
    return target


@pytest.mark.skipif(os.name != "nt", reason="covers Windows MAX_PATH filesystem behavior")
def test_atomic_write_supports_deep_windows_target_without_changing_final_path(
    tmp_path: Path,
):
    target = _windows_deep_atomic_target(tmp_path)

    atomic_write_bytes(target, b"after")

    assert target.read_bytes() == b"after"
    assert target.name == "note.md"
    assert not list(target.parent.glob(".*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="covers Windows MAX_PATH filesystem behavior")
def test_atomic_write_resolves_relative_paths_before_windows_long_path_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = _windows_deep_atomic_target(tmp_path)
    monkeypatch.chdir(target.parent)

    atomic_write_bytes(Path("note.md"), b"after")

    assert Path("note.md").read_bytes() == b"after"
    assert not list(target.parent.glob(".*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="covers Windows MAX_PATH filesystem behavior")
def test_atomic_write_cleans_deep_windows_temp_after_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = _windows_deep_atomic_target(tmp_path)
    target.write_bytes(b"before")
    attempted: list[tuple[str | bytes | Path, str | bytes | Path]] = []

    def fail_replace(
        source: str | bytes | Path, destination: str | bytes | Path
    ) -> None:
        attempted.append((source, destination))
        raise OSError("replace blocked")

    monkeypatch.setattr("kaizenlog.vault.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace blocked"):
        atomic_write_bytes(target, b"after")

    assert target.read_bytes() == b"before"
    assert len(attempted) == 1
    assert not list(target.parent.glob(".*.tmp"))


def test_atomic_write_text_preserves_explicit_newline_semantics(tmp_path: Path):
    target = tmp_path / "note.md"

    atomic_write_text(target, "a\nb\n", newline="")
    assert target.read_bytes() == b"a\nb\n"

    atomic_write_text(target, "a\nb\n", newline="\r\n")
    assert target.read_bytes() == b"a\r\nb\r\n"


def test_daily_note_store_serializes_different_markers_and_preserves_unmanaged_bytes(
    tmp_path: Path,
):
    store = DailyNoteStore(Config(vault_dir=tmp_path, daily_notes_dir=""))
    day = date(2026, 10, 15)
    target = store.path_for(day)
    original = (
        "---\r\n"
        "date: 2026-10-15\r\n"
        "---\r\n\r\n"
        "手書き本文  \r\n"
        "<!-- unknown:start -->\r\n不明な区間\r\n<!-- unknown:end -->\r\n"
    ).encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(original)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda item: store.write_section(day, item[0], item[1]),
                [
                    (ACTIVITY_MARKER, "## Activity\n活動"),
                    (ADVICE_MARKER, "## Advice\n助言"),
                ],
            )
        )

    assert results == [target, target]
    updated = target.read_bytes()
    assert "手書き本文  \r\n".encode("utf-8") in updated
    assert "<!-- unknown:start -->\r\n不明な区間\r\n<!-- unknown:end -->\r\n".encode("utf-8") in updated
    assert "<!-- kaizenlog:activity:start -->\r\n## Activity\r\n活動\r\n<!-- kaizenlog:activity:end -->".encode("utf-8") in updated
    assert "<!-- kaizenlog:advice:start -->\r\n## Advice\r\n助言\r\n<!-- kaizenlog:advice:end -->".encode("utf-8") in updated
