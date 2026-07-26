"""第12弾: レビュー残件 R1–R3。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from kaizenlog.config import Config
from kaizenlog.memory import (
    MemoryEntry,
    append_entries,
    compute_action_stats,
    load_entries,
    open_actions_in_window,
)
from kaizenlog.notify import notify
from kaizenlog.runlog import load_runs


def _cfg(vault: Path) -> Config:
    return Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        stats_dir=".kaizenlog/stats",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
    )


# ---- R1 ----

def test_today_window_includes_same_day_proposal():
    today = date(2026, 7, 26)
    entries = [
        MemoryEntry(
            id="KZN-20260726-001",
            date="2026-07-26",
            action="当日提案",
            status="proposed",
        ),
        MemoryEntry(
            id="KZN-20260725-001",
            date="2026-07-25",
            action="昨日提案",
            status="proposed",
        ),
        MemoryEntry(
            id="KZN-20260718-001",
            date="2026-07-18",
            action="8日前",
            status="proposed",
        ),
    ]
    open_ids = {e.id for e in open_actions_in_window(entries, today)}
    assert "KZN-20260726-001" in open_ids
    assert "KZN-20260725-001" in open_ids
    assert "KZN-20260718-001" not in open_ids  # today-8


def test_compute_action_stats_window_unchanged_excludes_today():
    """表示窓は当日を含むが、統計窓は従来どおり target-1 まで。"""
    today = date(2026, 7, 26)
    entries = [
        MemoryEntry(
            id="KZN-20260726-001",
            date="2026-07-26",
            action="当日",
            status="proposed",
        ),
        MemoryEntry(
            id="KZN-20260725-001",
            date="2026-07-25",
            action="昨日",
            status="done",
            done_date="2026-07-26",
        ),
    ]
    stats = compute_action_stats(entries, today)
    assert stats.proposed == 1  # 昨日のみ
    assert stats.done == 1
    assert open_actions_in_window(entries, today)  # 当日 proposed は表示側に出る


# ---- R2 ----

def test_second_done_does_not_overwrite_done_date(tmp_path, capsys):
    import kaizenlog.cli as cli_mod

    vault = tmp_path / "v"
    vault.mkdir()
    cfg = _cfg(vault)
    first_day = date(2026, 7, 20)
    second_day = date(2026, 7, 26)
    append_entries(
        cfg.memory_path,
        [
            MemoryEntry(
                id="KZN-20260719-001",
                date="2026-07-19",
                action="x",
                status="proposed",
            )
        ],
    )
    assert cli_mod.cmd_done(cfg, "KZN-20260719-001", first_day) == 0
    e1 = load_entries(cfg.memory_path)[-1]
    assert e1.status == "done" and e1.done_date == "2026-07-20"

    code = cli_mod.cmd_done(cfg, "KZN-20260719-001", second_day)
    assert code == 0
    out = capsys.readouterr().out
    assert "既に消化済み" in out
    assert "2026-07-20" in out
    e2 = load_entries(cfg.memory_path)[-1]
    # 後勝ちでも 2 回目の追記が無いので done_date は最初のまま
    assert e2.done_date == "2026-07-20"
    assert e2.status == "done"


# ---- R3 ----

def test_non_windows_notify_returns_none_and_no_failed_log(tmp_path, monkeypatch):
    from kaizenlog.cli import _notify

    monkeypatch.setattr("kaizenlog.notify.sys.platform", "linux")
    assert notify("t", "m") is None

    vault = tmp_path / "v"
    vault.mkdir()
    cfg = _cfg(vault)
    monkeypatch.setattr("kaizenlog.cli.notify", lambda *a, **k: None)
    assert _notify(cfg, "t", "m") is None
    assert load_runs(cfg.logs_path) == []


def test_windows_notify_failure_still_logs(tmp_path, monkeypatch):
    from kaizenlog.cli import _notify

    vault = tmp_path / "v"
    vault.mkdir()
    cfg = _cfg(vault)
    monkeypatch.setattr("kaizenlog.cli.notify", lambda *a, **k: False)
    assert _notify(cfg, "fail", "msg") is False
    runs = load_runs(cfg.logs_path)
    assert any(r.get("notify_failed") for r in runs)
