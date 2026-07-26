"""第11弾: today/done CLI とヘルスレジャー。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from kaizenlog.advisor import AdviceContractError, AdviceResult
from kaizenlog.config import Config
from kaizenlog.memory import (
    MemoryEntry,
    append_entries,
    load_entries,
    resolve_action_id,
)
from kaizenlog.runlog import (
    ADVISE_HEALTH_COMMAND,
    advise_health_warning_line,
    classify_violation_kind,
    command_duration_stats,
    consecutive_bad_advise_outcomes,
    last_advise_health,
    load_runs,
    log_advise_health,
    render_status,
)
from kaizenlog.vault import (
    ACTIONS_MARKER,
    DailyNoteStore,
    extract_section,
    upsert_section,
)


def _cfg(vault: Path) -> Config:
    return Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        stats_dir=".kaizenlog/stats",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
    )


# ---- T3 ----

def test_log_advise_health_outcomes(tmp_path):
    logs = tmp_path / "logs"
    for outcome in ("ok", "repaired", "degraded", "failed"):
        log_advise_health(
            logs,
            day=date(2026, 7, 20),
            backend="openai-compatible",
            outcome=outcome,
            duration_seconds=1.5,
            violations=["通知を切るな"] if outcome == "degraded" else None,
        )
    runs = load_runs(logs)
    health = [r for r in runs if r.get("command") == ADVISE_HEALTH_COMMAND]
    assert len(health) == 4
    assert {r["outcome"] for r in health} == {"ok", "repaired", "degraded", "failed"}
    deg = next(r for r in health if r["outcome"] == "degraded")
    # 本文ではなく種別タグ
    assert deg["violations"] == ["notification"]
    assert "切る" not in str(deg["violations"])


def test_classify_violation_has_no_body():
    assert classify_violation_kind("最小アクション1は通知を…") == "notification"
    assert classify_violation_kind("proposals と actions の件数を1対1に") == "cardinality"


def test_consecutive_bad_resets_on_ok(tmp_path):
    logs = tmp_path / "logs"
    log_advise_health(logs, day="2026-07-18", backend="x", outcome="degraded", duration_seconds=1)
    log_advise_health(logs, day="2026-07-19", backend="x", outcome="ok", duration_seconds=1)
    log_advise_health(logs, day="2026-07-20", backend="x", outcome="failed", duration_seconds=1)
    log_advise_health(logs, day="2026-07-21", backend="x", outcome="degraded", duration_seconds=1)
    runs = load_runs(logs)
    assert consecutive_bad_advise_outcomes(runs) == 2  # 最新から failed+degraded
    # ok でリセット確認: 最新が ok
    log_advise_health(logs, day="2026-07-22", backend="x", outcome="ok", duration_seconds=1)
    assert consecutive_bad_advise_outcomes(load_runs(logs)) == 0


def test_duration_stats_median_max(tmp_path):
    logs = tmp_path / "logs"
    for d, sec in ((1, 10), (2, 20), (3, 30)):
        log_advise_health(
            logs, day=f"2026-07-2{d}", backend="x", outcome="ok", duration_seconds=sec
        )
    med, mx = command_duration_stats(load_runs(logs), ADVISE_HEALTH_COMMAND)
    assert med == 20.0
    assert mx == 30.0


# ---- T4 ----

def test_doctor_error_on_two_consecutive_degraded(tmp_path):
    from kaizenlog.doctor import Check, _check_advise_health

    vault = tmp_path / "v"
    vault.mkdir()
    cfg = _cfg(vault)
    logs = cfg.logs_path
    log_advise_health(logs, day="2026-07-20", backend="x", outcome="degraded", duration_seconds=1)
    log_advise_health(logs, day="2026-07-21", backend="x", outcome="failed", duration_seconds=1)
    c = Check()
    _check_advise_health(c, cfg)
    assert c.has_error
    assert any("連続して縮退" in ln for ln in c.lines)


def test_doctor_warn_on_one_degraded(tmp_path):
    from kaizenlog.doctor import Check, _check_advise_health

    vault = tmp_path / "v"
    vault.mkdir()
    cfg = _cfg(vault)
    log_advise_health(
        cfg.logs_path, day="2026-07-21", backend="x", outcome="degraded", duration_seconds=1
    )
    c = Check()
    _check_advise_health(c, cfg)
    assert not c.has_error
    assert any("⚠️" in ln and "縮退" in ln for ln in c.lines)


def test_doctor_ok_when_healthy(tmp_path):
    from kaizenlog.doctor import Check, _check_advise_health

    vault = tmp_path / "v"
    vault.mkdir()
    cfg = _cfg(vault)
    log_advise_health(
        cfg.logs_path, day="2026-07-21", backend="x", outcome="ok", duration_seconds=1
    )
    c = Check()
    _check_advise_health(c, cfg)
    assert not c.has_error
    assert any("縮退の連続なし" in ln for ln in c.lines)


def test_health_warning_line_and_morning():
    from kaizenlog.cli import build_morning_notification

    runs = [
        {
            "ts": "2026-07-21T12:00:00+00:00",
            "command": ADVISE_HEALTH_COMMAND,
            "ok": False,
            "duration_seconds": 1,
            "date": "2026-07-20",
            "outcome": "degraded",
            "violations": ["json"],
        },
        {
            "ts": "2026-07-22T12:00:00+00:00",
            "command": ADVISE_HEALTH_COMMAND,
            "ok": False,
            "duration_seconds": 1,
            "date": "2026-07-21",
            "outcome": "failed",
            "violations": [],
        },
    ]
    line = advise_health_warning_line(runs)
    assert line is not None
    assert "2日連続" in line
    assert "json" not in line  # 違反詳細は載せない
    msg = build_morning_notification([], date(2026, 7, 22), health_line=line)
    assert msg is not None
    assert "縮退" in msg


def test_status_shows_health_and_slow_warn(tmp_path):
    logs = tmp_path / "logs"
    for i, sec in enumerate((5, 5, 5, 20), start=1):
        log_advise_health(
            logs,
            day=f"2026-07-2{i}",
            backend="ollama",
            outcome="ok",
            duration_seconds=sec,
        )
    text = render_status(load_runs(logs))
    assert "提案ヘルス" in text
    assert "実行時間が悪化" in text


# ---- T1 / T2 ----

def test_today_syncs_checkbox_then_lists(tmp_path, capsys):
    import kaizenlog.cli as cli_mod

    vault = tmp_path / "v"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    cfg = _cfg(vault)
    day = date(2026, 7, 26)
    # proposed 2件（7/25 提案 → 7/26 の窓）
    append_entries(
        cfg.memory_path,
        [
            MemoryEntry(
                id="KZN-20260725-001",
                date="2026-07-25",
                action="朝に集中枠を入れる｜PASS: x｜FAIL: y",
                status="proposed",
            ),
            MemoryEntry(
                id="KZN-20260725-002",
                date="2026-07-25",
                action="リンクをまとめる｜PASS: x｜FAIL: y",
                status="proposed",
            ),
        ],
    )
    # 中間日ノートで片方をチェック
    note = upsert_section(
        "---\ndate: x\n---\n",
        ACTIONS_MARKER,
        "- [x] KZN-20260725-001: 朝に集中枠を入れる\n"
        "- [ ] KZN-20260725-002: リンクをまとめる\n",
    )
    (daily / "2026-07-26.md").write_text(note, encoding="utf-8")

    code = cli_mod.cmd_today(cfg, day)
    assert code == 0
    out = capsys.readouterr().out
    assert "KZN-20260725-002" in out
    assert "KZN-20260725-001" not in out  # 同期で done になった
    entries = load_entries(cfg.memory_path)
    assert next(e for e in entries if e.id.endswith("001")).status == "done"


def test_today_zero_open_exits_zero(tmp_path, capsys):
    import kaizenlog.cli as cli_mod

    vault = tmp_path / "v"
    vault.mkdir()
    cfg = _cfg(vault)
    code = cli_mod.cmd_today(cfg, date(2026, 7, 26))
    assert code == 0
    assert "未完了のアクションはありません" in capsys.readouterr().out


def test_done_exact_suffix_ambiguous_and_missing(tmp_path, capsys):
    import kaizenlog.cli as cli_mod

    vault = tmp_path / "v"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    cfg = _cfg(vault)
    day = date(2026, 7, 26)
    append_entries(
        cfg.memory_path,
        [
            MemoryEntry(
                id="KZN-20260725-001",
                date="2026-07-25",
                action="A｜PASS: context_switches <= 40｜FAIL: 41",
                status="proposed",
            ),
            MemoryEntry(
                id="KZN-20260724-001",
                date="2026-07-24",
                action="B｜PASS: context_switches <= 40｜FAIL: 41",
                status="proposed",
            ),
        ],
    )
    # 曖昧
    assert cli_mod.cmd_done(cfg, "001", day) == 1
    # 該当なし
    assert cli_mod.cmd_done(cfg, "999", day) == 1
    # 完全一致 + ノート再描画
    (daily / f"{day.isoformat()}.md").write_text("---\n---\nbody\n", encoding="utf-8")
    assert cli_mod.cmd_done(cfg, "KZN-20260725-001", day) == 0
    entries = load_entries(cfg.memory_path)
    done = next(e for e in entries if e.id == "KZN-20260725-001")
    assert done.status == "done"
    assert done.done_date == day.isoformat()
    note = (daily / f"{day.isoformat()}.md").read_text(encoding="utf-8")
    sec = extract_section(note, ACTIONS_MARKER)
    assert sec is not None
    # done は一覧から外れ、残りの proposed だけが再描画される
    assert "KZN-20260725-001" not in sec
    assert "KZN-20260724-001" in sec
    out = capsys.readouterr().out
    assert "消化率" in out


def test_done_idempotent(tmp_path):
    import kaizenlog.cli as cli_mod

    vault = tmp_path / "v"
    vault.mkdir()
    cfg = _cfg(vault)
    day = date(2026, 7, 26)
    append_entries(
        cfg.memory_path,
        [
            MemoryEntry(
                id="KZN-20260725-003",
                date="2026-07-25",
                action="C",
                status="proposed",
            )
        ],
    )
    assert cli_mod.cmd_done(cfg, "KZN-20260725-003", day) == 0
    assert cli_mod.cmd_done(cfg, "KZN-20260725-003", day) == 0
    # 後勝ちで done のまま
    assert load_entries(cfg.memory_path)[-1].status == "done"


def test_today_then_done_then_today_reduces_count(tmp_path, capsys):
    """受け入れ: today → done → today で件数が減る。"""
    import kaizenlog.cli as cli_mod

    vault = tmp_path / "v"
    (vault / "01 Daily Notes").mkdir(parents=True)
    cfg = _cfg(vault)
    day = date(2026, 7, 26)
    append_entries(
        cfg.memory_path,
        [
            MemoryEntry(
                id="KZN-20260725-010",
                date="2026-07-25",
                action="消化テスト",
                status="proposed",
            )
        ],
    )
    cli_mod.cmd_today(cfg, day)
    out1 = capsys.readouterr().out
    assert "KZN-20260725-010" in out1
    cli_mod.cmd_done(cfg, "010", day)
    capsys.readouterr()
    cli_mod.cmd_today(cfg, day)
    out2 = capsys.readouterr().out
    assert "未完了のアクションはありません" in out2


def test_resolve_suffix_unique():
    entries = [
        MemoryEntry(id="KZN-20260725-001", date="2026-07-25", action="a", status="proposed"),
        MemoryEntry(id="KZN-20260725-002", date="2026-07-25", action="b", status="proposed"),
    ]
    r = resolve_action_id("002", entries)
    assert isinstance(r, MemoryEntry) and r.id.endswith("002")


def test_cmd_advise_logs_degraded_health(tmp_path, monkeypatch):
    import kaizenlog.cli as cli_mod
    from kaizenlog.vault import ACTIVITY_MARKER, upsert_section

    vault = tmp_path / "v"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    day = date(2026, 7, 21)
    note = upsert_section("---\n---\n", ACTIVITY_MARKER, "### カテゴリ別\n|a|b|\n")
    (daily / f"{day.isoformat()}.md").write_text(note, encoding="utf-8")
    cfg = _cfg(vault)

    def boom(*a, **k):
        raise AdviceContractError("契約違反\n- 通知はダメ", violations=["通知はダメ"])

    monkeypatch.setattr(cli_mod, "generate_advice", boom)
    monkeypatch.setattr(cli_mod, "build_advice_evidence", lambda *a, **k: type("E", (), {"markdown": "md"})())
    monkeypatch.setattr(cli_mod, "load_stats", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "activity_fingerprint", lambda x: "x")
    monkeypatch.setattr(cli_mod, "make_redactor", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "_write_actions_handoff", lambda *a, **k: None)

    with pytest.raises(AdviceContractError):
        cli_mod.cmd_advise(cfg, day)
    health = last_advise_health(load_runs(cfg.logs_path))
    assert health is not None
    assert health["outcome"] == "degraded"
    assert "notification" in health["violations"]


def test_cmd_advise_logs_ok_health(tmp_path, monkeypatch):
    import kaizenlog.cli as cli_mod
    from kaizenlog.vault import ACTIVITY_MARKER, upsert_section

    vault = tmp_path / "v"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    day = date(2026, 7, 21)
    note = upsert_section("---\n---\n", ACTIVITY_MARKER, "### カテゴリ別\n|a|b|\n")
    (daily / f"{day.isoformat()}.md").write_text(note, encoding="utf-8")
    cfg = _cfg(vault)
    good = (
        "## 🚀 Kaizen（AIからの改善提案）\n\n"
        "### 今日の改善提案\n1. ok\n\n"
        "### 明日の最小アクション\n- [ ] 行動｜PASS: 1回｜FAIL: 0\n\n"
        "### AI作業の改善\n- ok\n"
    )
    monkeypatch.setattr(
        cli_mod,
        "generate_advice",
        lambda *a, **k: AdviceResult(markdown=good, outcome="ok"),
    )
    monkeypatch.setattr(cli_mod, "requires_daily_contract", lambda cfg: False)
    monkeypatch.setattr(cli_mod, "build_advice_evidence", lambda *a, **k: type("E", (), {"markdown": ""})())
    monkeypatch.setattr(cli_mod, "load_stats", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "activity_fingerprint", lambda x: "x")
    monkeypatch.setattr(cli_mod, "make_redactor", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "_write_actions_handoff", lambda *a, **k: None)

    cli_mod.cmd_advise(cfg, day)
    health = last_advise_health(load_runs(cfg.logs_path))
    assert health is not None
    assert health["outcome"] == "ok"
