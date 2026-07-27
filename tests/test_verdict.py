"""PASS/FAIL 自動判定（A1）と朝のアクション転記（A2）のテスト。"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from kaizenlog.advisor import advice_contract_errors
from kaizenlog.advice_evidence import AdviceEvidence
from kaizenlog.config import Config
from kaizenlog.memory import (
    MemoryEntry,
    append_entries,
    load_entries,
    render_actions_section,
    update_statuses_from_note,
)
from kaizenlog.report import DailySummary
from kaizenlog.vault import (
    ACTIONS_MARKER,
    ADVICE_MARKER,
    ACTIVITY_MARKER,
    DailyNoteStore,
    extract_section,
    upsert_section,
)
from kaizenlog.verdict import (
    apply_verdicts_to_advice_note,
    is_known_metric,
    judge_entries,
    parse_pass_condition,
)


def _summary(switches: float = 35.0, **kw) -> DailySummary:
    return DailySummary(
        day=date(2026, 7, 25),
        total_minutes=kw.get("total_minutes", 120.0),
        by_category=kw.get("by_category", {"エンタメ": 20.0, "開発": 100.0}),
        by_app={},
        blocks=[],
        ai_tool_minutes=0.0,
        ai_sessions=0,
        context_switches=int(switches),
        by_site=kw.get("by_site", {"youtube.com": 15.0}),
    )


# ---- parse_pass_condition ----

def test_parse_pass_fullwidth_and_halfwidth_pipe():
    a = "やる｜PASS: context_switches <= 40｜FAIL: context_switches > 40"
    b = "やる|PASS: context_switches <= 40|FAIL: context_switches > 40"
    assert parse_pass_condition(a) == ("context_switches", "<=", 40.0)
    assert parse_pass_condition(b) == ("context_switches", "<=", 40.0)


def test_parse_pass_category_and_site_metrics():
    assert parse_pass_condition(
        "x｜PASS: category_minutes:エンタメ <= 30｜FAIL: 31"
    ) == ("category_minutes:エンタメ", "<=", 30.0)
    assert parse_pass_condition(
        "x｜PASS: site_minutes:YouTube.com <= 10｜FAIL: 11"
    ) == ("site_minutes:YouTube.com", "<=", 10.0)


def test_parse_pass_unknown_and_freeform_and_ignores_fail_numbers():
    assert parse_pass_condition("x｜PASS: pomodoro_count <= 4｜FAIL: 5") is None
    assert parse_pass_condition("x｜PASS: 集中ブロック2回以上｜FAIL: 1回") is None
    # FAIL 側の数値だけを見ない
    assert parse_pass_condition("x｜PASS: 自由文｜FAIL: context_switches <= 40") is None


def test_is_known_metric():
    assert is_known_metric("context_switches")
    assert is_known_metric("category_minutes:エンタメ")
    assert not is_known_metric("category_minutes:<カテゴリ名>")
    assert not is_known_metric("pomodoro_count")
    assert not is_known_metric("category_minutes:")


# ---- judge_entries ----

def test_judge_entries_pass_fail_skip_none_idempotent():
    proposal = date(2026, 7, 24)
    judged_day = date(2026, 7, 25)
    entries = [
        MemoryEntry(
            id="KZN-20260724-001",
            date="2026-07-24",
            action="a｜PASS: context_switches <= 40｜FAIL: >40",
        ),
        MemoryEntry(
            id="KZN-20260724-002",
            date="2026-07-24",
            action="b｜PASS: context_switches <= 20｜FAIL: >20",
        ),
        MemoryEntry(
            id="KZN-20260724-003",
            date="2026-07-24",
            action="c｜PASS: focus_blocks >= 1｜FAIL: 0",
        ),
        MemoryEntry(
            id="KZN-20260723-001",
            date="2026-07-23",
            action="old｜PASS: context_switches <= 40｜FAIL: x",
        ),
    ]
    summary = _summary(35)
    # focus_blocks は input_stats=None でスキップ
    out = judge_entries(entries, proposal, summary, [], None, judged_day)
    by_id = {e.id: e for e in out}
    assert by_id["KZN-20260724-001"].verdict == "pass"
    assert by_id["KZN-20260724-001"].verdict_value == 35.0
    assert by_id["KZN-20260724-001"].verdict_date == "2026-07-25"
    assert by_id["KZN-20260724-002"].verdict == "fail"
    assert "KZN-20260724-003" not in by_id
    assert "KZN-20260723-001" not in by_id

    # 同一 verdict の再判定は差分なし
    again = judge_entries(out + entries, proposal, summary, [], None, judged_day)
    # out has the judged ones; merge for second pass
    merged = {e.id: e for e in entries}
    merged.update({e.id: e for e in out})
    again = judge_entries(list(merged.values()), proposal, summary, [], None, judged_day)
    assert again == []

    # status / done_date 保持
    done_entry = MemoryEntry(
        id="KZN-20260724-001",
        date="2026-07-24",
        action="a｜PASS: context_switches <= 40｜FAIL: x",
        status="done",
        done_date="2026-07-25",
    )
    j = judge_entries([done_entry], proposal, summary, [], None, judged_day)
    assert j[0].status == "done" and j[0].done_date == "2026-07-25"


# ---- memory 互換 ----

def test_memory_legacy_jsonl_and_verdict_preserved_on_done(tmp_path):
    path = tmp_path / "suggestions.jsonl"
    path.write_text(
        json.dumps(
            {"id": "KZN-20260724-001", "date": "2026-07-24", "action": "x", "status": "proposed"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = load_entries(tmp_path)
    assert loaded[0].verdict is None and loaded[0].verdict_value is None

    with_verdict = MemoryEntry(
        id="KZN-20260724-001",
        date="2026-07-24",
        action="x",
        status="proposed",
        verdict="pass",
        verdict_value=10.0,
        verdict_date="2026-07-25",
    )
    note = "- [x] KZN-20260724-001: x\n"
    updates = update_statuses_from_note(note, [with_verdict], date(2026, 7, 26))
    assert updates[0].status == "done"
    assert updates[0].verdict == "pass"
    assert updates[0].verdict_value == 10.0


# ---- ノート書き戻し ----

def test_apply_verdicts_idempotent_and_skips_outside_advice():
    entry = MemoryEntry(
        id="KZN-20260724-001",
        date="2026-07-24",
        action="a｜PASS: context_switches <= 40｜FAIL: x",
        verdict="pass",
        verdict_value=35.0,
        verdict_date="2026-07-25",
    )
    outside = "- [ ] KZN-20260724-001: outside never touch\n"
    advice = (
        "## Kaizen\n"
        "- [ ] KZN-20260724-001: a｜PASS: context_switches <= 40｜FAIL: x\n"
    )
    content = outside + upsert_section("", ADVICE_MARKER, advice)
    updated = apply_verdicts_to_advice_note(content, [entry])
    assert updated is not None
    # 区間外は未変更
    assert outside.strip() in updated
    sec = extract_section(updated, ADVICE_MARKER)
    assert sec and "｜判定:" in sec and "実測35" in sec
    # 冪等
    again = apply_verdicts_to_advice_note(updated, [entry])
    assert again is None


def test_apply_verdicts_does_not_accumulate_blank_lines():
    """判定書き込みを複数回しても ADVICE 区間の空行数が増えない（F1）。"""
    content = upsert_section(
        "---\ndate: 2026-07-24\n---\n",
        ADVICE_MARKER,
        "## Kaizen\n- [ ] KZN-20260724-001: a｜PASS: context_switches <= 40｜FAIL: x\n",
    )
    blank_counts: list[int] = []
    for value in (30.0, 35.0, 40.0):
        entry = MemoryEntry(
            id="KZN-20260724-001",
            date="2026-07-24",
            action="a｜PASS: context_switches <= 40｜FAIL: x",
            verdict="pass" if value <= 40 else "fail",
            verdict_value=value,
            verdict_date="2026-07-25",
        )
        updated = apply_verdicts_to_advice_note(content, [entry])
        assert updated is not None
        content = updated
        section = extract_section(content, ADVICE_MARKER) or ""
        # extract_section は strip するため raw body の空行を直接数える
        start = content.find(f"<!-- {ADVICE_MARKER}:start -->")
        end = content.find(f"<!-- {ADVICE_MARKER}:end -->")
        body = content[start + len(f"<!-- {ADVICE_MARKER}:start -->"):end]
        blank_counts.append(sum(1 for ln in body.splitlines() if ln.strip() == ""))
    assert blank_counts[0] == blank_counts[1] == blank_counts[2]


# ---- advice_contract_errors ----

def test_advice_contract_machine_pass_known_and_unknown():
    base = """### 今日の改善提案
1. [F1] 根拠→提案

### 明日の最小アクション
- [ ] [F1] 行動｜PASS: {pass_cond}｜FAIL: 0回

### AI作業の改善
- [F1] ok
"""
    # freeform with number still ok without evidence
    free = base.format(pass_cond="集中ブロック2回以上")
    assert not any("指標名" in e for e in advice_contract_errors(free))

    good = base.format(pass_cond="context_switches <= 40")
    assert not any("指標名" in e for e in advice_contract_errors(good))

    bad = base.format(pass_cond="pomodoro_count <= 4")
    errs = advice_contract_errors(bad)
    assert any("指標名が使用可能な指標にありません" in e for e in errs)

    numberless = base.format(pass_cond="うまくできた")
    assert any("数値条件" in e for e in advice_contract_errors(numberless))


# ---- render_actions_section ----

def test_render_actions_section_window_check_verdict():
    target = date(2026, 7, 25)
    entries = [
        MemoryEntry(id="KZN-20260724-001", date="2026-07-24", action="昨日", status="proposed"),
        MemoryEntry(id="KZN-20260718-001", date="2026-07-18", action="8日前", status="proposed"),
        MemoryEntry(id="KZN-20260720-001", date="2026-07-20", action="done", status="done"),
        MemoryEntry(
            id="KZN-20260722-001",
            date="2026-07-22",
            action="判定済",
            status="proposed",
            verdict="fail",
            verdict_value=52.0,
        ),
    ]
    assert render_actions_section(entries, target) is not None
    md = render_actions_section(entries, target)
    assert "KZN-20260724-001" in md
    assert "KZN-20260722-001" in md
    assert "判定 ❌ 実測52" in md
    # window: target-7 = 7/18 〜 target-1 = 7/24 → 7/18 は含む
    assert "KZN-20260718-001" in md
    assert "KZN-20260720-001" not in md  # done 除外

    assert render_actions_section([], target) is None

    note = "- [x] KZN-20260724-001: 昨日\n"
    md2 = render_actions_section(entries, target, note)
    assert "- [x] KZN-20260724-001:" in md2


# ---- CLI 統合 ----

def test_generate_verdict_and_actions_handoff(tmp_path, monkeypatch):
    from kaizenlog import cli as cli_mod

    vault = tmp_path / "vault"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    memory = vault / "Kaizen" / "Memory"
    memory.mkdir(parents=True)
    stats = vault / ".kaizenlog" / "stats"
    stats.mkdir(parents=True)
    exp = vault / "03 Areas" / "Kaizen Experiments"
    exp.mkdir(parents=True)

    proposal = date(2026, 7, 24)
    today = date(2026, 7, 25)

    # 前日の Memory + ノート
    action = "集中｜PASS: context_switches <= 40｜FAIL: context_switches > 40"
    append_entries(
        memory,
        [MemoryEntry(id="KZN-20260724-001", date="2026-07-24", action=action)],
    )
    advice_body = f"- [ ] KZN-20260724-001: {action}\n"
    prev = upsert_section(
        f"---\ndate: {proposal.isoformat()}\n---\n\nhandwritten\n",
        ADVICE_MARKER,
        advice_body,
    )
    # 区間外の同一 ID
    prev = prev + "\n- [ ] KZN-20260724-001: outside\n"
    (daily / f"{proposal.isoformat()}.md").write_text(prev, encoding="utf-8")

    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        experiments_dir="03 Areas/Kaizen Experiments",
        stats_dir=".kaizenlog/stats",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
    )
    cfg.aiwork.enabled = False

    # AW をモック
    monkeypatch.setattr(
        cli_mod,
        "collect_day",
        lambda *a, **k: ([], True),
    )
    monkeypatch.setattr(cli_mod, "collect_input", lambda *a, **k: None)

    class FakeSummary:
        day = today
        total_minutes = 100.0
        by_category = {"開発": 100.0}
        by_app = {}
        blocks = []
        ai_tool_minutes = 0.0
        ai_sessions = 0
        context_switches = 30
        by_site = {}

        @property
        def ai_activity_blocks(self):
            return 0

    monkeypatch.setattr(
        cli_mod,
        "summarize",
        lambda *a, **k: FakeSummary(),
    )
    monkeypatch.setattr(
        cli_mod,
        "render_markdown",
        lambda *a, **k: "### カテゴリ別\n| a | b |\n",
    )
    monkeypatch.setattr(cli_mod.Classifier, "classify_all", lambda self, e: [])
    monkeypatch.setattr(
        cli_mod,
        "ActivityWatchClient",
        lambda url: MagicMock(),
    )

    # 「今日」を固定して handoff を許可
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 25, 12, 0, tzinfo=tz or ZoneInfo("Asia/Tokyo"))

    monkeypatch.setattr(cli_mod, "datetime", FixedDateTime)

    cli_mod.cmd_generate(cfg, today)

    # Memory に verdict
    final = {e.id: e for e in load_entries(cfg.memory_path)}
    assert final["KZN-20260724-001"].verdict == "pass"
    assert final["KZN-20260724-001"].verdict_value == 30.0

    # 前日ノート: ADVICE 内に判定、外は触らない
    prev_text = (daily / f"{proposal.isoformat()}.md").read_text(encoding="utf-8")
    assert "handwritten" in prev_text
    advice = extract_section(prev_text, ADVICE_MARKER)
    assert "｜判定:" in advice and "実測30" in advice
    assert prev_text.count("｜判定:") == 1
    assert "- [ ] KZN-20260724-001: outside" in prev_text

    # 再実行で suffix 増殖しない
    cli_mod.cmd_generate(cfg, today)
    prev2 = (daily / f"{proposal.isoformat()}.md").read_text(encoding="utf-8")
    assert prev2.count("｜判定:") == 1

    # 翌日ノートに actions
    next_day = today + __import__("datetime").timedelta(days=1)
    next_path = daily / f"{next_day.isoformat()}.md"
    assert next_path.is_file()
    assert extract_section(next_path.read_text(encoding="utf-8"), ACTIONS_MARKER)


def test_backfill_day_does_not_write_actions(tmp_path, monkeypatch):
    from kaizenlog import cli as cli_mod
    from datetime import timedelta

    vault = tmp_path / "vault"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    memory = vault / "Kaizen" / "Memory"
    memory.mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / "03 Areas" / "Kaizen Experiments").mkdir(parents=True)

    past = date(2026, 7, 10)
    append_entries(
        memory,
        [
            MemoryEntry(
                id="KZN-20260709-001",
                date="2026-07-09",
                action="x｜PASS: context_switches <= 40｜FAIL: y",
            )
        ],
    )
    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        experiments_dir="03 Areas/Kaizen Experiments",
        stats_dir=".kaizenlog/stats",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
    )
    cfg.aiwork.enabled = False

    monkeypatch.setattr(cli_mod, "collect_day", lambda *a, **k: ([], True))
    monkeypatch.setattr(cli_mod, "collect_input", lambda *a, **k: None)

    class FakeSummary:
        day = past
        total_minutes = 10.0
        by_category = {}
        by_app = {}
        blocks = []
        ai_tool_minutes = 0.0
        ai_sessions = 0
        context_switches = 5
        by_site = {}

        @property
        def ai_activity_blocks(self):
            return 0

    monkeypatch.setattr(cli_mod, "summarize", lambda *a, **k: FakeSummary())
    monkeypatch.setattr(cli_mod, "render_markdown", lambda *a, **k: "log")
    monkeypatch.setattr(cli_mod.Classifier, "classify_all", lambda self, e: [])
    monkeypatch.setattr(cli_mod, "ActivityWatchClient", lambda url: MagicMock())

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 25, 12, 0, tzinfo=tz or ZoneInfo("Asia/Tokyo"))

    monkeypatch.setattr(cli_mod, "datetime", FixedDateTime)

    cli_mod.cmd_generate(cfg, past)
    target = past + timedelta(days=1)
    assert not (daily / f"{target.isoformat()}.md").exists()


def test_advise_dry_run_does_not_write_actions(tmp_path, monkeypatch):
    from kaizenlog import cli as cli_mod

    vault = tmp_path / "vault"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    memory = vault / "Kaizen" / "Memory"
    memory.mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)

    day = date(2026, 7, 25)
    content = upsert_section(
        f"---\ndate: {day.isoformat()}\n---\n",
        ACTIVITY_MARKER,
        "### カテゴリ別\n|x|y|\n",
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
    # dry_run should return before any write to next day
    result = cli_mod.cmd_advise(cfg, day, dry_run=True)
    assert result is None
    next_day = date(2026, 7, 26)
    assert not (daily / f"{next_day.isoformat()}.md").exists()
