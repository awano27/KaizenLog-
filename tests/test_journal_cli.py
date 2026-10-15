"""CLI contract tests for the typed journal generation path."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaizenlog import cli
from kaizenlog.config import Config


def test_generate_skip_catch_up_uses_typed_journal_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg = Config(vault_dir=tmp_path)
    preview = SimpleNamespace(status="ready", target_day=date(2026, 10, 15), reason=None)
    result = SimpleNamespace(status="generated", target_day=date(2026, 10, 15), error=None)
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(cli, "preview_journal", lambda actual_cfg, day: calls.append(("preview", day)) or preview)
    monkeypatch.setattr(cli, "execute_journal", lambda actual_cfg, actual_preview: calls.append(("execute", actual_preview)) or result)

    assert cli.cmd_generate_skip_catch_up(cfg, date(2026, 10, 15)) == 0
    assert [name for name, _ in calls] == ["preview", "execute"]
    assert calls[0][1] == date(2026, 10, 15)
    assert calls[1][1] is preview


def test_generate_parser_accepts_skip_catch_up_without_journal_subcommand():
    args = cli.build_parser().parse_args(["generate", "--skip-catch-up"])

    assert args.command == "generate"
    assert args.skip_catch_up is True


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--config", "A.toml", "setup", "--yes"], "A.toml"),
        (["setup", "--config", "B.toml", "--yes"], "B.toml"),
        (["--config", "A.toml", "setup", "--config", "B.toml", "--yes"], "B.toml"),
        (["setup", "--yes"], None),
    ],
)
def test_setup_config_path_forms_reach_setup_options(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: str | None
):
    captured = []
    monkeypatch.setattr(
        "kaizenlog.setup.run_setup",
        lambda options: captured.append(options) or 0,
    )

    assert cli.main(argv) == 0
    assert len(captured) == 1
    assert captured[0].config_path == (Path(expected) if expected is not None else None)


def test_main_skip_catch_up_does_not_enter_legacy_backfill_or_advice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = Config(vault_dir=tmp_path)
    calls: list[str] = []
    preview = SimpleNamespace(status="ready", reason=None)
    result = SimpleNamespace(status="already_generated", error=None)
    monkeypatch.setattr(cli, "find_config_file", lambda _path: tmp_path / "config.toml")
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    monkeypatch.setattr(cli, "preview_journal", lambda *_: calls.append("preview") or preview)
    monkeypatch.setattr(cli, "execute_journal", lambda *_: calls.append("execute") or result)
    monkeypatch.setattr(cli, "cmd_backfill", lambda *_: pytest.fail("backfill called"))
    monkeypatch.setattr(cli, "catch_up_yesterday", lambda *_: pytest.fail("retro-advise called"))
    monkeypatch.setattr(cli, "cmd_advise", lambda *_: pytest.fail("advise called"))

    assert cli.main(["--config", str(tmp_path / "config.toml"), "generate", "--skip-catch-up"]) == 0
    assert calls == ["preview", "execute"]
