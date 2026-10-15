from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from kaizenlog.config import Config, load_config, write_daily_note_override
from kaizenlog.note_paths import (
    DailyNoteResolutionError,
    resolve_daily_note,
)


DAY = date(2026, 10, 15)


def _cfg(tmp_path: Path, **kwargs) -> Config:
    return Config(vault_dir=tmp_path, daily_notes_dir="日次 ノート", **kwargs)


def test_missing_resolution_is_read_only_and_uses_configured_format(tmp_path: Path):
    cfg = _cfg(tmp_path, daily_note_format="YYYYMMDD")

    result = resolve_daily_note(cfg, DAY)
    expected = tmp_path / "日次 ノート" / "20261015.md"

    assert result.status == "missing"
    assert result.path == expected
    assert result.selected_by == "configured"
    assert not expected.exists()
    assert not expected.with_name(expected.name + ".lock").exists()
    assert not (tmp_path / "日次 ノート").exists()


def test_override_is_authoritative_even_when_the_file_is_missing(tmp_path: Path):
    cfg = _cfg(tmp_path, daily_note_overrides={DAY.isoformat(): "special/手書き.md"})

    result = resolve_daily_note(cfg, DAY)

    assert result.status == "missing"
    assert result.path == tmp_path / "日次 ノート" / "special" / "手書き.md"
    assert result.selected_by == "override"


def test_year_month_day_format_uses_nested_daily_folder(tmp_path: Path):
    cfg = _cfg(tmp_path, daily_note_format="YYYY/MM/DD")

    result = resolve_daily_note(cfg, date(2024, 2, 29))

    assert result.status == "missing"
    assert result.path == tmp_path / "日次 ノート" / "2024" / "02" / "29.md"


def test_multiple_known_existing_formats_are_a_conflict(tmp_path: Path):
    cfg = _cfg(tmp_path)
    notes = tmp_path / "日次 ノート"
    notes.mkdir(parents=True)
    (notes / "2026-10-15.md").write_text("iso", encoding="utf-8")
    (notes / "20261015.md").write_text("compact", encoding="utf-8")

    result = resolve_daily_note(cfg, DAY)

    assert result.status == "conflict"
    assert result.path is None
    assert "複数" in (result.reason or "")


def test_only_non_configured_existing_format_is_a_conflict(tmp_path: Path):
    cfg = _cfg(tmp_path, daily_note_format="YYYYMMDD")
    notes = tmp_path / "日次 ノート"
    notes.mkdir(parents=True)
    (notes / "2026-10-15.md").write_text("old", encoding="utf-8")

    result = resolve_daily_note(cfg, DAY)

    assert result.status == "conflict"
    assert result.path is None
    assert "形式" in (result.reason or "")


@pytest.mark.parametrize(
    "override",
    [
        "../outside.md",
        "/absolute.md",
        "C:/absolute.md",
        "CON.md",
        "foo:bar.md",
        "foo\x00bar.md",
        "foo.md ",
        "nested/..\\..\\outside.md",
        "without-extension.txt",
    ],
)
def test_unsafe_override_is_unsupported(tmp_path: Path, override: str):
    cfg = _cfg(tmp_path, daily_note_overrides={DAY.isoformat(): override})

    result = resolve_daily_note(cfg, DAY)

    assert result.status == "unsupported"
    assert result.path is None
    with pytest.raises(DailyNoteResolutionError):
        from kaizenlog.vault import DailyNoteStore

        DailyNoteStore(cfg).path_for(DAY)


def test_override_writer_preserves_unrelated_config_bytes(tmp_path: Path):
    config = tmp_path / "config.toml"
    original = (
        "# keep this comment\r\n"
        "[general]\r\n"
        f'vault_dir = "{tmp_path.as_posix()}"\r\n'
        'daily_notes_dir = "日次 ノート"\r\n'
        "\r\n"
        "[privacy]\r\n"
        'replacement = "[MASK]"  \r\n'
    ).encode("utf-8")
    config.write_bytes(original)

    write_daily_note_override(
        config,
        DAY,
        "special/手書き.md",
        expected_bytes=original,
    )
    updated = config.read_bytes()
    loaded = load_config(str(config))

    assert b"# keep this comment\r\n" in updated
    assert b'replacement = "[MASK]"  \r\n' in updated
    assert loaded.daily_note_overrides == {DAY.isoformat(): "special/手書き.md"}


def test_override_writer_preserves_crlf_inline_comment_and_hash_in_quoted_path(tmp_path: Path):
    config = tmp_path / "config.toml"
    original = (
        "[general]\r\n"
        f'vault_dir = "{tmp_path.as_posix()}"\r\n'
        'daily_notes_dir = "日次 # ノート" # keep\r\n'
        "\r\n"
        "[daily_notes.overrides]\r\n"
        '"2026-10-15" = "old#name.md" # preserve this\r\n'
        "\r\n"
        "[privacy]\r\n"
        'replacement = "[MASK]"\r\n'
    ).encode("utf-8")
    config.write_bytes(original)

    write_daily_note_override(
        config,
        DAY,
        "special/new#name.md",
        expected_bytes=original,
    )

    updated = config.read_bytes()
    assert b'"2026-10-15" = "special/new#name.md" # preserve this\r\n' in updated
    assert 'daily_notes_dir = "日次 # ノート" # keep\r\n'.encode("utf-8") in updated
    assert b"[privacy]\r\n" in updated


def test_concurrent_override_writers_using_same_expected_bytes_have_one_winner(tmp_path: Path):
    import threading

    config = tmp_path / "config.toml"
    config.write_text(
        f'[general]\nvault_dir = "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )
    expected = config.read_bytes()
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def attempt(value: str):
        barrier.wait()
        try:
            write_daily_note_override(config, DAY, value, expected_bytes=expected)
        except ValueError:
            outcomes.append("conflict")
        else:
            outcomes.append(value)

    threads = [
        threading.Thread(target=attempt, args=("one.md",)),
        threading.Thread(target=attempt, args=("two.md",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) in (["conflict", "one.md"], ["conflict", "two.md"])
    loaded = load_config(str(config))
    assert loaded.daily_note_overrides[DAY.isoformat()] in {"one.md", "two.md"}


def test_override_writer_rejects_stale_config_bytes(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        f'[general]\nvault_dir = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="変更"):
        write_daily_note_override(
            config,
            DAY,
            "2026-10-15.md",
            expected_bytes=b"stale",
        )


def test_write_section_explicit_none_requires_missing_note(tmp_path: Path):
    from kaizenlog.vault import DailyNoteStore, GOAL_MARKER

    cfg = _cfg(tmp_path)
    store = DailyNoteStore(cfg)
    store.write_section(DAY, GOAL_MARKER, "first", expected_bytes=None)
    with pytest.raises(ValueError, match="変更"):
        store.write_section(DAY, GOAL_MARKER, "second", expected_bytes=None)
    store.write_section(DAY, GOAL_MARKER, "second")
    assert "second" in store.path_for(DAY).read_text(encoding="utf-8")


def test_writer_stops_when_resolution_changes_while_waiting_for_note_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from contextlib import contextmanager
    import kaizenlog.vault as vault
    from kaizenlog.vault import DailyNoteStore, GOAL_MARKER

    cfg = _cfg(tmp_path)
    store = DailyNoteStore(cfg)
    original_lock = vault.locked_read_modify_write

    @contextmanager
    def mutate_config_after_lock(path: Path):
        with original_lock(path):
            cfg.daily_note_format = "YYYYMMDD"
            yield path

    monkeypatch.setattr(vault, "locked_read_modify_write", mutate_config_after_lock)
    with pytest.raises(RuntimeError, match="解決結果"):
        store.write_section(DAY, GOAL_MARKER, "must not write")
    assert not (cfg.daily_notes_path / "2026-10-15.md").exists()
    assert not (cfg.daily_notes_path / "20261015.md").exists()


@pytest.mark.parametrize(
    "daily_notes_dir",
    ["bad:dir", "CON", "nested/..", "folder. ", "folder\x00name"],
)
def test_unsafe_configured_daily_directory_is_unsupported_without_side_effects(
    tmp_path: Path, daily_notes_dir: str,
):
    cfg = Config(vault_dir=tmp_path, daily_notes_dir=daily_notes_dir)

    result = resolve_daily_note(cfg, DAY)

    assert result.status == "unsupported"
    assert result.path is None
    assert not list(tmp_path.iterdir())


def test_missing_known_candidate_under_outward_parent_symlink_is_unsupported(tmp_path: Path):
    cfg = _cfg(tmp_path, daily_note_format="YYYY/MM/DD")
    notes = tmp_path / "日次 ノート"
    outside = tmp_path / "outside"
    notes.mkdir()
    outside.mkdir()
    try:
        (notes / "2026").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    result = resolve_daily_note(cfg, DAY)

    assert result.status == "unsupported"
    assert result.path is None
    assert not (outside / "10" / "15.md").exists()
    assert not (outside / "10" / "15.md.lock").exists()


def test_missing_known_candidate_is_checked_for_containment_without_real_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    import kaizenlog.note_paths as note_paths

    cfg = _cfg(tmp_path, daily_note_format="YYYY/MM/DD")
    candidate = tmp_path / "日次 ノート" / "2026" / "10" / "15.md"
    checked: list[Path] = []
    original_inside = note_paths._inside

    def detect(path: Path, root: Path) -> bool:
        checked.append(path)
        if path == candidate:
            return False
        return original_inside(path, root)

    monkeypatch.setattr(note_paths, "_inside", detect)
    result = resolve_daily_note(cfg, DAY)

    assert result.status == "unsupported"
    assert result.path is None
    assert candidate in checked
