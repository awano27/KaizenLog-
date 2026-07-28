"""第9弾: 総点検10件の回帰テスト。"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advice_format import render_advice_markdown, validate_advice
from kaizenlog.advisor import (
    AdviceContractError,
    AdvisorError,
    prepare_advice_request,
    requires_daily_contract,
)
from kaizenlog.config import Config, ConfigError, LLMConfig
from kaizenlog.memory import (
    MemoryEntry,
    assign_action_ids,
    compute_action_stats,
    update_statuses_from_note,
)
from kaizenlog.privacy import make_redactor
from kaizenlog.setup import validate_hhmm
from kaizenlog.verdict import parse_pass_condition, strip_pass_annotation
from tests.test_advice_evidence import CURRENT, HISTORY
from tests.test_advice_format import _evidence, _valid_data


# ---- G1 ----

def test_non_daily_prompt_skips_reader_rewrite(tmp_path, monkeypatch):
    """カスタム system_prompt の本文が render_reader_advice で消えない。"""
    import kaizenlog.cli as cli_mod
    from kaizenlog.vault import ADVICE_MARKER, ACTIVITY_MARKER, extract_section, upsert_section

    vault = tmp_path / "vault"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    day = date(2026, 7, 21)
    note = upsert_section("---\ndate: x\n---\n", ACTIVITY_MARKER, "### カテゴリ別\n|a|b|\n")
    (daily / f"{day.isoformat()}.md").write_text(note, encoding="utf-8")

    custom_body = "## 週次レビュー\n\n### 今週の学び\n- 深い振り返り本文XYZ\n"
    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        stats_dir=".kaizenlog/stats",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
        llm=LLMConfig(backend="none", system_prompt="weekly_review"),
    )
    assert not requires_daily_contract(cfg.llm)

    monkeypatch.setattr(
        cli_mod, "generate_advice", lambda *a, **k: f"## 🚀 Kaizen（AIからの改善提案）\n\n{custom_body}"
    )
    # render_reader_advice をモックしない — 実経路で呼ばれないこと／呼ばれても本文が残ること
    called = []
    real_render = cli_mod.render_reader_advice

    def track_render(md, ev):
        called.append(1)
        return real_render(md, ev)

    monkeypatch.setattr(cli_mod, "render_reader_advice", track_render)
    monkeypatch.setattr(
        cli_mod, "build_advice_evidence", lambda *a, **k: MagicMock(markdown="", reader_summary="", reader_notes=[])
    )
    monkeypatch.setattr(cli_mod, "load_stats", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "activity_fingerprint", lambda x: "x")
    monkeypatch.setattr(cli_mod, "_write_actions_handoff", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "make_redactor", lambda *a, **k: None)

    cli_mod.cmd_advise(cfg, day)
    section = extract_section(
        (daily / f"{day.isoformat()}.md").read_text(encoding="utf-8"), ADVICE_MARKER
    )
    assert called == []  # requires_daily_contract が偽なら呼ばない
    assert "深い振り返り本文XYZ" in section
    assert "今日の結論" not in section


# ---- G2 ----

def test_g2_notification_action_is_contract_error_not_renderer_bug():
    """『通知』を含む action → AdviceContractError（修復対象）。JSON 契約層。"""
    data = _valid_data()
    data["actions"][0]["action"] = "通知を切って集中する"
    errs = validate_advice(data, _evidence())
    assert any("通知" in e for e in errs)
    # 意味違反は render 前に落ち、renderer bug にしない
    with pytest.raises(AdviceContractError):
        render_advice_markdown(data, _evidence())


def test_g2_pass_label_does_not_false_positive_ai_optimize():
    """ai_avg_turns のレンダラ注記（Claude/往復を含む）が誤爆しない。"""
    data = _valid_data()
    data["actions"][0]["pass"] = "ai_avg_turns <= 3"
    data["actions"][0]["fail"] = "4以上"
    # ラベルに Claude / 往復 が含まれる指標 — 形状検査込みで render 成功
    md = render_advice_markdown(data, _evidence())
    assert "PASS: ai_avg_turns <= 3" in md


def test_g2_structural_break_still_renderer_bug():
    """構造破壊は _assert_render_shape → AdvisorError(renderer bug)。"""
    from kaizenlog.advice_format import _assert_render_shape

    broken = "これは見出しもチェックも無い壊れた出力"
    with pytest.raises(AdvisorError, match="renderer bug"):
        _assert_render_shape(broken, n_actions=1)


# ---- G3 ----

def test_privacy_safe_with_redactor_prepares_ok():
    """privacy_safe + redact_patterns で prepare が成功する。"""
    from kaizenlog.advisor import resolve_system_prompt

    cfg = LLMConfig(backend="none", system_prompt="privacy_safe")
    evidence = build_advice_evidence(CURRENT, HISTORY)
    redactor = make_redactor([r"秘密プロジェクト名"], "[REDACTED]")
    system, prompt, _ = prepare_advice_request(
        cfg, "Activity", [], redactor=redactor, evidence=evidence
    )
    assert '"proposals"' in system
    assert '"actions"' in system
    # 過剰マスクで JSON キーが消えたら失敗
    nuke = make_redactor([r'"proposals"'], "XX")
    with pytest.raises(AdvisorError, match="制御トークン"):
        prepare_advice_request(cfg, "Activity", [], redactor=nuke, evidence=evidence)


# ---- G4 ----

def test_handoff_checkbox_detected_on_intermediate_day():
    """中間日ノートの 📌 だけでチェック → done 化。"""
    entries = [
        MemoryEntry(
            id="KZN-20260720-001",
            date="2026-07-20",
            action="古い提案",
            status="proposed",
        )
    ]
    # 提案日 7/20、転記先 7/23 でチェック（advise 日は 7/26 想定）
    note = "- [x] KZN-20260720-001: 古い提案（7/20提案）\n"
    updates = update_statuses_from_note(note, entries, date(2026, 7, 26))
    assert len(updates) == 1
    assert updates[0].status == "done"
    assert updates[0].id == "KZN-20260720-001"


# ---- G5 ----

def test_setup_proposes_morning_when_daily_exists(tmp_path, monkeypatch):
    """Daily 登録済み・Morning 未登録 → Morning 登録が提案・実行される。"""
    from kaizenlog.setup import SetupOptions, run_setup

    vault = tmp_path / "v"
    vault.mkdir()
    cfg = tmp_path / "c.toml"
    registered = []

    def fake_is_registered(name="KaizenLog Daily"):
        return name == "KaizenLog Daily"

    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_llm",
        lambda **k: __import__("kaizenlog.setup_detect", fromlist=["LlmDetection"]).LlmDetection(
            None, None, None, "none", None
        ),
    )
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_activitywatch",
        lambda url: __import__("kaizenlog.setup_detect", fromlist=["AwDetection"]).AwDetection(True, None),
    )
    monkeypatch.setattr("kaizenlog.setup_detect.is_task_registered", fake_is_registered)
    monkeypatch.setattr(
        "kaizenlog.setup.register_daily_task",
        lambda time=None, kaizenlog_exe=None, morning_time="": registered.append(
            (time, morning_time)
        )
        or True,
    )
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda cfg, p=None: ("ok", False))

    code = run_setup(
        SetupOptions(
            config_path=cfg,
            vault=vault,
            yes=True,
            skip_aw=True,
            skip_skills=True,
            register_task=True,
            time="21:30",
            morning_time="08:30",
        ),
        ui=__import__("kaizenlog.setup", fromlist=["ConsoleUI"]).ConsoleUI(),
    )
    assert code == 0
    # 日次は既存のため time=None、朝だけ登録
    assert registered == [(None, "08:30")]


# ---- G6 ----

def test_double_annotation_normalized_and_parses():
    """LLM 自前注記 → レンダ単一注記 → parse 可能。"""
    data = _valid_data()
    data["actions"] = [data["actions"][0]]
    data["proposals"] = [data["proposals"][0]]
    data["actions"][0]["pass"] = "context_switches <= 40（コンテキストスイッチ回数）"
    data["actions"][0]["fail"] = "41回以上"
    md = render_advice_markdown(data, _evidence())
    # 二重括弧にならない
    assert md.count("（コンテキストスイッチ回数）") == 1
    assert "（コンテキストスイッチ回数）（" not in md
    line = [ln for ln in md.splitlines() if "PASS:" in ln][0]
    assert parse_pass_condition(line) == ("context_switches", "<=", 40.0)
    # ループ strip も
    nested = "context_switches <= 40（A）（B）"
    assert strip_pass_annotation(nested) == "context_switches <= 40"


# ---- G7 ----

def test_assign_action_ids_no_duplicate_on_reorder():
    """新アクション順 [新規C, 既存A] でも同一 KZN が2行に付かない。"""
    day = date(2026, 7, 21)
    existing = [
        MemoryEntry(
            id="KZN-20260721-001",
            date="2026-07-21",
            action="既存アクションA｜PASS: x｜FAIL: y",
            status="proposed",
        )
    ]
    md = (
        "### 明日の最小アクション\n"
        "- [ ] 新規アクションC｜PASS: a｜FAIL: b\n"
        "- [ ] 既存アクションA｜PASS: x｜FAIL: y\n"
    )
    out, entries = assign_action_ids(md, day, existing)
    ids = []
    for ln in out.splitlines():
        if "KZN-" in ln:
            m = __import__("re").search(r"KZN-\d{8}-\d+", ln)
            assert m
            ids.append(m.group(0))
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert "KZN-20260721-001" in ids


# ---- G8 ----

def test_superseded_excluded_from_judge_stats_notify():
    from kaizenlog.cli import build_morning_notification
    from kaizenlog.report import DailySummary
    from kaizenlog.verdict import judge_entries

    day = date(2026, 7, 21)
    entries = [
        MemoryEntry(
            id="KZN-20260720-001",
            date="2026-07-20",
            action="x｜PASS: context_switches <= 40｜FAIL: 41",
            status="superseded",
        ),
        MemoryEntry(
            id="KZN-20260720-002",
            date="2026-07-20",
            action="y｜PASS: context_switches <= 40｜FAIL: 41",
            status="proposed",
        ),
    ]
    summary = DailySummary(
        day=day,
        total_minutes=100.0,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=10,
        by_site={},
    )
    judged = judge_entries(entries, date(2026, 7, 20), summary, [], None, day)
    assert all(j.id != "KZN-20260720-001" for j in judged)
    assert any(j.id == "KZN-20260720-002" for j in judged)

    stats = compute_action_stats(entries, date(2026, 7, 21))
    assert stats.proposed == 1  # superseded 除外

    # 判定付き superseded がトーストに乗らない
    with_verdict = [
        MemoryEntry(
            id="KZN-20260720-001",
            date="2026-07-20",
            action="x",
            status="superseded",
            verdict="pass",
            verdict_date="2026-07-20",
            verdict_value=1.0,
        ),
        MemoryEntry(
            id="KZN-20260720-002",
            date="2026-07-20",
            action="y",
            status="proposed",
            verdict="fail",
            verdict_date="2026-07-20",
            verdict_value=50.0,
        ),
    ]
    msg = build_morning_notification(with_verdict, date(2026, 7, 21))
    assert msg is not None
    assert "✅0" in msg
    assert "実行済み" in msg  # proposed FAIL is not counted as done-fail


# ---- G9 ----

def test_validate_hhmm():
    assert validate_hhmm("8:30") == "08:30"
    assert validate_hhmm("21:30") == "21:30"
    with pytest.raises(ConfigError):
        validate_hhmm("25:00")
    with pytest.raises(ConfigError):
        validate_hhmm("ab:cd")
    with pytest.raises(ConfigError):
        validate_hhmm("")
