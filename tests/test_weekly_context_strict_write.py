"""Automated weekly reviews must detect failure to persist their input."""
from __future__ import annotations

import pytest

from kaizenlog import cli
from kaizenlog.config import AIWorkConfig, Config
from kaizenlog.runlog import load_runs
from kaizenlog.vault import WEEKLY_CONTEXT_MARKER, extract_section


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    cfg = Config(vault_dir=tmp_path / "vault", aiwork=AIWorkConfig(enabled=False))
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: cfg)

    def unexpected_telemetry(*args, **kwargs):
        pytest.fail("Synthetic weekly test must not collect real telemetry")

    monkeypatch.setattr(cli, "available_adapters", unexpected_telemetry)
    return cfg


def test_strict_write_reports_failure_and_preserves_existing_note(
    isolated_config, monkeypatch, capsys
):
    cfg = isolated_config
    note = cfg.daily_notes_path / "Weekly Reviews" / "2026-W30.md"
    note.parent.mkdir(parents=True)
    note.write_text("Existing review\n", encoding="utf-8")

    def fail_write(*args, **kwargs):
        raise OSError("synthetic permission failure")

    monkeypatch.setattr("kaizenlog.weekly_context.write_weekly_context", fail_write)
    result = cli.main([
        "weekly-context", "--week", "2026-W30", "--write", "--strict-write"
    ])

    assert result == 1
    assert note.read_text(encoding="utf-8") == "Existing review\n"
    captured = capsys.readouterr()
    assert "2026-07-20" in captured.out
    assert "synthetic permission failure" in captured.err
    assert any(r["command"] == "weekly-context" and not r["ok"]
               for r in load_runs(cfg.logs_path))


def test_strict_write_success_persists_selected_week(isolated_config):
    cfg = isolated_config
    assert cli.main([
        "weekly-context", "--week", "2026-W30", "--write", "--strict-write"
    ]) == 0
    note = cfg.daily_notes_path / "Weekly Reviews" / "2026-W30.md"
    section = extract_section(note.read_text(encoding="utf-8"), WEEKLY_CONTEXT_MARKER)
    assert section is not None
    assert "2026-07-20 〜 2026-07-26" in section
    assert any(r["command"] == "weekly-context" and r["ok"]
               for r in load_runs(cfg.logs_path))


def test_strict_write_requires_write_before_collecting_data(isolated_config, monkeypatch, capsys):
    def unexpected_render(*args, **kwargs):
        pytest.fail("Invalid option combination must stop before rendering")

    monkeypatch.setattr("kaizenlog.weekly_context.render_weekly_context", unexpected_render)
    assert cli.main(["weekly-context", "--strict-write", "--week", "2026-W30"]) == 1
    assert "--write" in capsys.readouterr().err
