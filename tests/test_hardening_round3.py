"""第3弾: baseline / 退行 / 縮退保存 / command wrapper 除外。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kaizenlog.advisor import AdviceContractError
from kaizenlog.advice_evidence import AdviceEvidence
from kaizenlog.aiwork import _is_command_wrapper, scan_sessions
from kaizenlog.config import Config, LLMConfig
from kaizenlog.experiments import (
    Experiment,
    baseline_median_from_stats,
    create_experiment,
    detect_regressions,
    load_experiments,
    metric_from_stats,
    record_measurement,
    should_measure_experiment,
)
from kaizenlog.vault import ADVICE_MARKER, extract_section


# ---- A4 ----

def test_metric_from_stats_mappings():
    stats = {
        "total_minutes": 200.5,
        "context_switches": 42,
        "ai_activity_blocks": 7,
        "by_category": {"エンタメ": 30.0},
        "by_site": {"YouTube.com": 12.0},
        "ai": {
            "sessions": 3,
            "fragmented": 1,
            "tool_errors": 2,
            "interruptions": 4,
        },
        "input": {
            "keypresses": 1000,
            "focus_blocks": 2,
            "focus_minutes": 55.5,
        },
    }
    assert metric_from_stats("context_switches", stats) == 42.0
    assert metric_from_stats("total_active_minutes", stats) == 200.5
    assert metric_from_stats("ai_activity_blocks", stats) == 7.0
    assert metric_from_stats("ai_cc_sessions", stats) == 3.0
    assert metric_from_stats("ai_fragmented_sessions", stats) == 1.0
    assert metric_from_stats("ai_tool_errors", stats) == 2.0
    assert metric_from_stats("ai_interruptions", stats) == 4.0
    assert metric_from_stats("category_minutes:エンタメ", stats) == 30.0
    assert metric_from_stats("site_minutes:youtube.com", stats) == 12.0
    assert metric_from_stats("focus_blocks", stats) == 2.0
    assert metric_from_stats("focus_minutes", stats) == 55.5
    assert metric_from_stats("input_keypresses", stats) == 1000.0
    assert metric_from_stats("ai_avg_turns", stats) is None
    assert metric_from_stats("focus_blocks", {"day": "x"}) is None


def test_create_experiment_with_baseline(tmp_path):
    path = create_experiment(
        tmp_path, "BL", "context_switches", "<= 40",
        today=date(2026, 7, 25), deadline=date(2026, 8, 8),
        baseline=42.5,
    )
    text = path.read_text(encoding="utf-8")
    assert "baseline: 42.5" in text
    e = load_experiments(tmp_path)[0]
    assert e.baseline == 42.5


def test_baseline_median_requires_three_days():
    days = [
        {"context_switches": 10},
        {"context_switches": 20},
    ]
    assert baseline_median_from_stats(days, "context_switches") is None
    days.append({"context_switches": 30})
    assert baseline_median_from_stats(days, "context_switches") == 20.0


# ---- A5 ----

def test_should_measure_adopted_within_monitor_window():
    deadline = date(2026, 7, 1)
    exp = Experiment(
        path=Path("x.md"), title="t", status="adopted",
        metric="context_switches", target_op="<=", target_value=10,
        deadline=deadline,
    )
    assert should_measure_experiment(exp, date(2026, 7, 31)) is True
    assert should_measure_experiment(exp, date(2026, 8, 1)) is False
    exp_no_dl = Experiment(
        path=Path("x.md"), title="t", status="adopted",
        metric="context_switches", target_op="<=", target_value=10,
        deadline=None,
    )
    assert should_measure_experiment(exp_no_dl, date(2026, 7, 10)) is False
    running = Experiment(
        path=Path("x.md"), title="t", status="running",
        metric="context_switches", target_op="<=", target_value=10,
    )
    assert should_measure_experiment(running, date(2026, 7, 10)) is True


def test_detect_regressions_boundaries():
    as_of = date(2026, 7, 25)
    # 2点のみ → 検知しない
    two = Experiment(
        path=Path("a.md"), title="two", status="adopted",
        metric="context_switches", target_op="<=", target_value=10,
        deadline=date(2026, 7, 1),
        measurements={
            date(2026, 7, 24): 20.0,
            date(2026, 7, 25): 21.0,
        },
    )
    assert detect_regressions([two], window=7, as_of=as_of) == []

    # 3点中2点未達 → 検知
    three = Experiment(
        path=Path("b.md"), title="reg", status="adopted",
        metric="context_switches", target_op="<=", target_value=10,
        deadline=date(2026, 7, 1),
        measurements={
            date(2026, 7, 23): 5.0,   # pass
            date(2026, 7, 24): 20.0,  # fail
            date(2026, 7, 25): 21.0,  # fail
        },
    )
    assert detect_regressions([three], window=7, as_of=as_of) == [three]

    # running は対象外
    three.status = "running"
    assert detect_regressions([three], window=7, as_of=as_of) == []


# ---- L2 ----

def test_cmd_advise_saves_degraded_section_and_reraises(tmp_path, monkeypatch):
    from kaizenlog import cli as cli_mod
    from kaizenlog.vault import upsert_section, ACTIVITY_MARKER

    vault = tmp_path / "vault"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    day = date(2026, 7, 25)
    content = upsert_section(
        f"---\ndate: {day.isoformat()}\n---\n",
        ACTIVITY_MARKER,
        "### カテゴリ別\n|a|b|\n",
    )
    (daily / f"{day.isoformat()}.md").write_text(content, encoding="utf-8")
    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        stats_dir=".kaizenlog/stats",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
    )

    def boom(*a, **k):
        raise AdviceContractError("保存条件を満たしませんでした:\n- 見出し不足")

    monkeypatch.setattr(cli_mod, "generate_advice", boom)
    # evidence with markdown
    fake_ev = MagicMock()
    fake_ev.markdown = "### 確定事実\n- 合計100分"
    monkeypatch.setattr(
        cli_mod,
        "build_advice_evidence",
        lambda *a, **k: fake_ev,
    )
    monkeypatch.setattr(cli_mod, "load_stats", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "activity_fingerprint", lambda x: "x")

    with pytest.raises(AdviceContractError):
        cli_mod.cmd_advise(cfg, day)

    note = (daily / f"{day.isoformat()}.md").read_text(encoding="utf-8")
    section = extract_section(note, ADVICE_MARKER)
    assert section is not None
    assert "出力契約を満たさず" in section
    assert "確定事実" in section
    assert "- [ ]" not in section
    assert "KZN-" not in section


def test_degraded_section_omits_facts_when_no_evidence():
    from kaizenlog.cli import _degraded_advice_section

    text = _degraded_advice_section(None)
    assert "出力契約を満たさず" in text
    assert "確定事実サマリー" in text
    # 事実本文は付かない
    assert text.count("\n\n") <= 2 or "###" not in text


def test_normal_advise_overwrites_degraded(tmp_path, monkeypatch):
    from kaizenlog import cli as cli_mod
    from kaizenlog.vault import upsert_section, ACTIVITY_MARKER

    vault = tmp_path / "vault"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    day = date(2026, 7, 25)
    degraded = cli_mod._degraded_advice_section(None)
    content = upsert_section(
        f"---\ndate: {day.isoformat()}\n---\n",
        ACTIVITY_MARKER,
        "### カテゴリ別\n|a|b|\n",
    )
    content = upsert_section(content, ADVICE_MARKER, degraded)
    (daily / f"{day.isoformat()}.md").write_text(content, encoding="utf-8")
    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        stats_dir=".kaizenlog/stats",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
    )
    good = """## 🚀 Kaizen（AIからの改善提案）

### 今日の改善提案
1. ok

### 明日の最小アクション
- [ ] 行動

### AI作業の改善
- ok
"""
    monkeypatch.setattr(cli_mod, "generate_advice", lambda *a, **k: good)
    monkeypatch.setattr(cli_mod, "render_reader_advice", lambda md, ev: md)
    monkeypatch.setattr(cli_mod, "build_advice_evidence", lambda *a, **k: MagicMock(markdown=""))
    monkeypatch.setattr(cli_mod, "load_stats", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "activity_fingerprint", lambda x: "x")
    monkeypatch.setattr(cli_mod, "_write_actions_handoff", lambda *a, **k: None)

    cli_mod.cmd_advise(cfg, day)
    section = extract_section(
        (daily / f"{day.isoformat()}.md").read_text(encoding="utf-8"), ADVICE_MARKER
    )
    assert "出力契約を満たさず" not in section
    assert "明日の最小アクション" in section


# ---- L3 ----

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(__import__("json").dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_command_wrapper_not_counted_in_user_turns(tmp_path):
    assert _is_command_wrapper("<command-name>foo</command-name>")
    assert _is_command_wrapper("<local-command-stdout>x</local-command-stdout>")
    assert not _is_command_wrapper("普通の依頼です")

    ds = datetime(2026, 7, 20, tzinfo=timezone.utc)
    de = datetime(2026, 7, 21, tzinfo=timezone.utc)
    path = tmp_path / "proj" / "s.jsonl"
    ts = "2026-07-20T10:00:00.000Z"
    records = [
        {
            "type": "user", "sessionId": "s1", "timestamp": ts,
            "message": {"content": [{"type": "text", "text": "<command-name>/help</command-name>"}]},
        },
        {
            "type": "user", "sessionId": "s1", "timestamp": "2026-07-20T10:01:00.000Z",
            "message": {"content": [{"type": "text", "text": "<local-command-stdout>ok</local-command-stdout>"}]},
        },
        {
            "type": "user", "sessionId": "s1", "timestamp": "2026-07-20T10:02:00.000Z",
            "message": {"content": [{"type": "text", "text": "本物の依頼"}]},
        },
        {
            "type": "assistant", "sessionId": "s1", "timestamp": "2026-07-20T10:03:00.000Z",
            "message": {"id": "m1", "model": "x", "usage": {"output_tokens": 1}, "content": []},
        },
    ]
    _write_jsonl(path, records)
    # touch mtime so not skipped
    path.touch()
    sessions = scan_sessions(tmp_path, ds, de)
    assert len(sessions) == 1
    assert sessions[0].user_turns == 1


def test_command_only_session_excluded(tmp_path):
    ds = datetime(2026, 7, 20, tzinfo=timezone.utc)
    de = datetime(2026, 7, 21, tzinfo=timezone.utc)
    path = tmp_path / "proj" / "s2.jsonl"
    records = [
        {
            "type": "user", "sessionId": "s2", "timestamp": "2026-07-20T10:00:00.000Z",
            "message": {"content": [{"type": "text", "text": "<command-name>/status</command-name>"}]},
        },
        {
            "type": "assistant", "sessionId": "s2", "timestamp": "2026-07-20T10:01:00.000Z",
            "message": {"id": "m2", "model": "x", "usage": {"output_tokens": 1}, "content": []},
        },
    ]
    _write_jsonl(path, records)
    path.touch()
    assert scan_sessions(tmp_path, ds, de) == []


def test_generate_advice_prints_repair_message(monkeypatch, capsys):
    from kaizenlog.advisor import generate_advice
    from kaizenlog.advice_evidence import build_advice_evidence
    from tests.test_advice_evidence import CURRENT, HISTORY, VALID_ADVICE_JSON

    calls = {"n": 0}

    def fake_text(cfg, system, user):
        calls["n"] += 1
        if calls["n"] == 1:
            return "invalid not json"
        return VALID_ADVICE_JSON

    monkeypatch.setattr("kaizenlog.advisor.generate_text", fake_text)
    out = generate_advice(
        LLMConfig(), "log", [], evidence=build_advice_evidence(CURRENT, HISTORY)
    )
    captured = capsys.readouterr().out
    assert "出力契約違反を検出" in captured
    assert out.outcome == "repaired"
    assert "🚀 Kaizen" in out.markdown
    assert "### 明日の最小アクション" in out.markdown
