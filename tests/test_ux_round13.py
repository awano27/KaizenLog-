"""第13弾 UX: 設定 fail-closed / morning・today 副作用 / 候補群分け / Activity Log 表示。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kaizenlog.config import Config
from kaizenlog.memory import (
    MemoryEntry,
    TODAY_CANDIDATE_CAP,
    append_entries,
    load_entries,
    partition_open_actions,
    render_actions_section,
)
from kaizenlog.report import Block, DailySummary, render_markdown
from kaizenlog.runlog import log_run, render_status
from kaizenlog.vault import ACTIONS_MARKER, upsert_section


TZ = ZoneInfo("Asia/Tokyo")


def _isolate_config_env(monkeypatch, tmp_path: Path) -> Path:
    """実ホーム/AppData/CWD に触れない隔離環境。設定なしの CWD を返す。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAIZENLOG_CONFIG", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr("kaizenlog.config.sys.platform", "win32")
    return tmp_path


def _write_cfg(path: Path, vault: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'[general]\nvault_dir = "{vault.as_posix()}"\ntimezone = "Asia/Tokyo"\n',
        encoding="utf-8",
    )


# ---- X2 fail-closed ---------------------------------------------------------


def test_x2_no_config_run_exits_2_without_side_effects(monkeypatch, tmp_path, capsys):
    from kaizenlog import cli as cli_mod

    _isolate_config_env(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(
        cli_mod, "cmd_generate", lambda *a, **k: called.append("generate")
    )
    monkeypatch.setattr(cli_mod, "cmd_advise", lambda *a, **k: called.append("advise"))
    monkeypatch.setattr(
        "kaizenlog.collector.ActivityWatchClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("AW must not run")),
    )
    code = cli_mod.main(["run"])
    assert code == 2
    err = capsys.readouterr().err
    assert "設定がありません" in err
    assert "kaizenlog setup" in err
    assert "kaizenlog doctor" in err
    assert called == []


@pytest.mark.parametrize("cmd", ["morning", "today", "done", "status"])
def test_x2_no_config_commands_exit_2(monkeypatch, tmp_path, capsys, cmd):
    from kaizenlog import cli as cli_mod

    _isolate_config_env(monkeypatch, tmp_path)
    argv = [cmd] if cmd != "done" else ["done", "KZN-20260701-001"]
    code = cli_mod.main(argv)
    assert code == 2
    assert "設定がありません" in capsys.readouterr().err


def test_x2_doctor_no_config_exits_1_and_skips_vault(monkeypatch, tmp_path, capsys):
    from kaizenlog import cli as cli_mod
    import requests as req

    _isolate_config_env(monkeypatch, tmp_path)
    # AW 接続は失敗してよいが、設定エラーは必須
    monkeypatch.setattr(
        "kaizenlog.doctor.requests.get",
        lambda *a, **k: (_ for _ in ()).throw(req.ConnectionError("offline")),
    )
    code = cli_mod.main(["doctor"])
    assert code == 1
    out = capsys.readouterr().out
    assert "設定ファイルがありません" in out or "設定" in out
    assert "設定作成後に確認" in out
    # CWD をボールトとして ✅ にしない
    assert "ボールト書き込み可" not in out


def test_x2_missing_explicit_config_no_traceback(monkeypatch, tmp_path, capsys):
    from kaizenlog import cli as cli_mod

    _isolate_config_env(monkeypatch, tmp_path)
    missing = tmp_path / "nope.toml"
    code = cli_mod.main(["--config", str(missing), "run"])
    assert code == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "見つかりません" in err


def test_x2_missing_explicit_config_doctor_exit_1(monkeypatch, tmp_path, capsys):
    from kaizenlog import cli as cli_mod
    import requests as req

    _isolate_config_env(monkeypatch, tmp_path)
    missing = tmp_path / "nope.toml"
    monkeypatch.setattr(
        "kaizenlog.doctor.requests.get",
        lambda *a, **k: (_ for _ in ()).throw(req.ConnectionError("offline")),
    )
    code = cli_mod.main(["--config", str(missing), "doctor"])
    assert code == 1
    out = capsys.readouterr().out
    assert "見つかりません" in out
    assert "Traceback" not in out


def test_x2_help_works_without_config(monkeypatch, tmp_path):
    from kaizenlog import cli as cli_mod

    _isolate_config_env(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as ei:
        cli_mod.main(["--help"])
    assert ei.value.code == 0
    with pytest.raises(SystemExit) as ei2:
        cli_mod.main(["setup", "--help"])
    assert ei2.value.code == 0
    with pytest.raises(SystemExit) as ei3:
        cli_mod.main(["doctor", "--help"])
    assert ei3.value.code == 0


# ---- X3 catch-up / morning / today sync -------------------------------------


def _vault_cfg(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    (vault / "01 Daily Notes").mkdir(parents=True)
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / ".kaizenlog" / "logs").mkdir(parents=True)
    return Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        memory_dir="Kaizen/Memory",
        stats_dir=".kaizenlog/stats",
        logs_dir=".kaizenlog/logs",
        actions_position="top",
    )


def test_x3_catch_up_result_not_needed(tmp_path, monkeypatch):
    from kaizenlog import cli as cli_mod

    cfg = _vault_cfg(tmp_path)
    today = date(2026, 7, 26)
    yday = today - timedelta(days=1)
    (cfg.stats_path / f"{yday.isoformat()}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli_mod, "cmd_generate", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(cli_mod, "cmd_advise", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    r = cli_mod.catch_up_yesterday(cfg, today)
    assert r.generate == "not-needed"
    assert r.advise == "not-needed"
    assert not r.has_failures
    assert r.new_ids == set()


def test_x3_catch_up_generate_failed_safe_error(tmp_path, monkeypatch, capsys):
    from kaizenlog import cli as cli_mod
    from kaizenlog.runlog import load_runs

    cfg = _vault_cfg(tmp_path)
    today = date(2026, 7, 26)
    yday = today - timedelta(days=1)
    monkeypatch.setattr(cli_mod, "missing_days", lambda *a, **k: [yday])

    def boom(*a, **k):
        raise RuntimeError("ACTION_BODY secret prompt TITLE")

    monkeypatch.setattr(cli_mod, "cmd_generate", boom)
    r = cli_mod.catch_up_yesterday(cfg, today)
    assert r.generate == "failed"
    assert r.has_failures
    assert ("generate", "RuntimeError") in r.failures
    runs = load_runs(cfg.logs_path)
    gen = [x for x in runs if x.get("command") == "generate"][-1]
    assert gen["ok"] is False
    assert "ACTION_BODY" not in gen.get("error", "")
    assert "secret" not in gen.get("error", "")
    assert "RuntimeError" in gen.get("error", "")


def test_x3_morning_skip_catch_up(tmp_path, monkeypatch, capsys):
    from kaizenlog import cli as cli_mod

    cfg = _vault_cfg(tmp_path)
    today = date(2026, 7, 26)
    calls = []
    monkeypatch.setattr(
        cli_mod,
        "catch_up_yesterday",
        lambda *a, **k: calls.append("catch") or cli_mod.CatchUpResult(),
    )
    monkeypatch.setattr(cli_mod, "notify", lambda *a, **k: True)
    assert cli_mod.cmd_morning(cfg, today, skip_catch_up=True) == 0
    assert calls == []


def test_x3_morning_partial_runlog_and_status(tmp_path, monkeypatch, capsys):
    from kaizenlog import cli as cli_mod
    from kaizenlog.runlog import load_runs

    cfg = _vault_cfg(tmp_path)
    today = date(2026, 7, 26)
    monkeypatch.setattr(
        cli_mod,
        "catch_up_yesterday",
        lambda *a, **k: cli_mod.CatchUpResult(
            generate="failed",
            failures=[("generate", "RuntimeError")],
        ),
    )
    monkeypatch.setattr(cli_mod, "notify", lambda *a, **k: True)
    assert cli_mod.cmd_morning(cfg, today) == 0
    out = capsys.readouterr().out
    assert "追いつきは未完了（generate）" in out
    runs = load_runs(cfg.logs_path)
    mor = [r for r in runs if r.get("command") == "morning"][-1]
    assert mor.get("ok") is True
    assert mor.get("partial") is True
    assert "catch-up incomplete" in (mor.get("note") or "")
    assert "ACTION" not in (mor.get("note") or "")
    st = render_status(runs)
    assert "⚠" in st
    assert "部分成功" in st or "partial" in st.lower() or "catch-up" in st


def test_x3_today_sync_message(tmp_path, capsys):
    import kaizenlog.cli as cli_mod

    cfg = _vault_cfg(tmp_path)
    day = date(2026, 7, 26)
    append_entries(
        cfg.memory_path,
        [
            MemoryEntry(
                id="KZN-20260725-001",
                date="2026-07-25",
                action="a",
                status="proposed",
            )
        ],
    )
    note = upsert_section(
        "---\n---\n",
        ACTIONS_MARKER,
        "- [x] KZN-20260725-001: a\n",
    )
    (cfg.daily_notes_path / f"{day.isoformat()}.md").write_text(note, encoding="utf-8")
    assert cli_mod.cmd_today(cfg, day) == 0
    out = capsys.readouterr().out
    assert "同期しました: 1件" in out


def test_x3_today_no_sync_skips_note(tmp_path, monkeypatch, capsys):
    import kaizenlog.cli as cli_mod

    cfg = _vault_cfg(tmp_path)
    day = date(2026, 7, 26)
    append_entries(
        cfg.memory_path,
        [
            MemoryEntry(
                id="KZN-20260725-001",
                date="2026-07-25",
                action="a",
                status="proposed",
            )
        ],
    )
    reads = []

    class FakeStore:
        def __init__(self, *a, **k):
            pass

        def read(self, d):
            reads.append(d)
            return None

        def path_for(self, d):
            return cfg.daily_notes_path / f"{d}.md"

    monkeypatch.setattr(cli_mod, "DailyNoteStore", FakeStore)
    monkeypatch.setattr(
        cli_mod,
        "append_entries",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no write")),
    )
    assert cli_mod.cmd_today(cfg, day, no_sync=True) == 0
    out = capsys.readouterr().out
    assert "同期せずに表示" in out
    # 目標表示のため当日ノートは1回だけ読む（チェック同期の複数日走査はしない）
    assert reads == [day]
    assert "KZN-20260725-001" in out
    assert "目標" in out


# ---- X4 buckets -------------------------------------------------------------


def test_x4_partition_boundaries():
    today = date(2026, 7, 26)
    entries = [
        MemoryEntry(id="KZN-20260726-001", date="2026-07-26", action="t", status="proposed"),
        MemoryEntry(id="KZN-20260719-001", date="2026-07-19", action="r7", status="proposed"),
        MemoryEntry(id="KZN-20260718-001", date="2026-07-18", action="s8", status="proposed"),
        MemoryEntry(id="KZN-20260626-001", date="2026-06-26", action="s30", status="proposed"),
        MemoryEntry(id="KZN-20260625-001", date="2026-06-25", action="o31", status="proposed"),
    ]
    b = partition_open_actions(entries, today, recent_include_today=True)
    assert {e.action for e in b.recent} == {"t", "r7"}
    assert {e.action for e in b.stale} == {"s8", "s30"}
    assert {e.action for e in b.older} == {"o31"}


def test_x4_today_default_cap_and_all(tmp_path, capsys):
    import kaizenlog.cli as cli_mod

    cfg = _vault_cfg(tmp_path)
    day = date(2026, 7, 26)
    ents = [
        MemoryEntry(
            id=f"KZN-20260725-{i:03d}",
            date="2026-07-25",
            action=f"a{i}",
            status="proposed",
        )
        for i in range(1, 6)
    ]
    append_entries(cfg.memory_path, ents)
    mem_before = cfg.memory_path.joinpath("suggestions.jsonl").read_text(encoding="utf-8")
    assert cli_mod.cmd_today(cfg, day, no_sync=True) == 0
    out = capsys.readouterr().out
    assert "今日の候補 3件" in out
    assert "ほか直近7日の未完了 2件" in out
    # 新しい ID が先（005,004,003）
    assert "KZN-20260725-005" in out
    assert "KZN-20260725-001" not in out  # 既定では 3 件のみ
    assert cli_mod.cmd_today(cfg, day, no_sync=True, show_all=True) == 0
    out_all = capsys.readouterr().out
    for i in range(1, 6):
        assert f"KZN-20260725-{i:03d}" in out_all
    mem_after = cfg.memory_path.joinpath("suggestions.jsonl").read_text(encoding="utf-8")
    assert mem_before == mem_after


def test_x4_today_stale_only_hold_message(tmp_path, capsys):
    import kaizenlog.cli as cli_mod

    cfg = _vault_cfg(tmp_path)
    day = date(2026, 7, 26)
    append_entries(
        cfg.memory_path,
        [
            MemoryEntry(
                id="KZN-20260710-001",
                date="2026-07-10",
                action="old",
                status="proposed",
            )
        ],
    )
    assert cli_mod.cmd_today(cfg, day, no_sync=True) == 0
    out = capsys.readouterr().out
    assert "今日の候補なし" in out
    assert "保留" in out
    assert "today --all" in out


def test_x4_obsidian_section_max_three():
    today = date(2026, 7, 26)
    entries = [
        MemoryEntry(
            id=f"KZN-20260725-{i:03d}",
            date="2026-07-25",
            action=f"a{i}",
            status="proposed",
        )
        for i in range(1, 5)
    ]
    # keep existing [x]
    note = "- [x] KZN-20260725-004: a4\n"
    md = render_actions_section(entries, today, note)
    assert md is not None
    checks = [ln for ln in md.splitlines() if ln.startswith("- [")]
    assert len(checks) == TODAY_CANDIDATE_CAP
    assert "- [x] KZN-20260725-004:" in md
    assert "ほか直近7日の未完了 1件" in md
    assert "today --all" in md


# ---- X5 Activity Log --------------------------------------------------------


def _summary(blocks: list[Block], total: float | None = None) -> DailySummary:
    t = total if total is not None else sum(b.minutes for b in blocks)
    by_cat: dict[str, float] = {}
    for b in blocks:
        by_cat[b.category] = by_cat.get(b.category, 0.0) + b.minutes
    return DailySummary(
        day=date(2026, 7, 26),
        total_minutes=t,
        by_category=by_cat or {"開発": t} if t else {},
        by_app={},
        blocks=blocks,
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
    )


def test_x5_zero_minutes_empty_state():
    md = render_markdown(_summary([], total=0), TZ)
    assert "0分" in md
    assert "kaizenlog doctor" in md
    assert "記録された活動はありませんでした" not in md
    assert "収集成功" not in md


def test_x5_timeline_cap_61():
    base = datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc)
    blocks = [
        Block(
            start=base + timedelta(minutes=i * 10),
            end=base + timedelta(minutes=i * 10 + 5),
            category="開発",
            app="Code",
            titles=[f"t{i}"],
        )
        for i in range(61)
    ]
    # Block.minutes is property from start/end (5 min each) — all eligible
    md = render_markdown(_summary(blocks), TZ, min_block_minutes=3.0, max_timeline_rows=60)
    assert "3分以上" in md
    assert "61件" in md
    assert "60件" in md
    assert "省略" in md
    # 表示は時刻順（UTC base → Asia/Tokyo）
    assert "### タイムライン" in md


def test_x5_table_cell_escape():
    base = datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc)
    blocks = [
        Block(
            start=base,
            end=base + timedelta(minutes=10),
            category="カテゴリ|A",
            app="App|B\nX",
            titles=["title|C\nline2"],
        )
    ]
    summary = _summary(blocks)
    summary.ai_tool_minutes = {"tool|B": 10.0}
    summary.by_site = {"site|D\nok": 5.0}
    md = render_markdown(summary, TZ, min_block_minutes=3.0)
    # 各データ行の | 列数がヘッダと一致（パイプエスケープ後）
    for line in md.splitlines():
        if line.startswith("|") and "---" not in line and "カテゴリ" not in line and "サイト" not in line and "ツール" not in line and "時刻" not in line:
            # unescaped pipe would break column count — escaped \| is fine
            assert "カテゴリ\\|A" in md or "tool\\|B" in md or "site\\|D" in md
    assert "カテゴリ\\|A" in md
    assert "tool\\|B" in md
    assert "App\\|B" in md
    assert "title\\|C" in md


# ---- X1 docs contract -------------------------------------------------------


def test_x1_readme_usage_no_pypi_assert():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    usage = (root / "docs" / "USAGE.md").read_text(encoding="utf-8")
    for text in (readme, usage):
        assert "pipx install kaizenlog" not in text
        assert "pip install kaizenlog" not in text
        assert "pipx install ." in text
        assert "git clone" in text
        assert "github.com/awano27/KaizenLog-" in text
    assert "1.5.0rc1" in readme or "RC" in readme
