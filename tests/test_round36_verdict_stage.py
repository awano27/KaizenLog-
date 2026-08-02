"""第36弾: verdict_stage のスキーマと2段階判定の回帰テスト。"""
from __future__ import annotations

import json
from datetime import date, timedelta

from kaizenlog.cli import _resync_measurement_day_actions, build_morning_notification
from kaizenlog.decay import detect_kzn_decay
from kaizenlog.memory import (
    MemoryEntry,
    _verdict_block_line,
    append_entries,
    compute_action_stats,
    consecutive_fail_actions,
    format_today_action_line,
    load_entries,
    mark_entry_done,
    mark_entry_skipped,
    metric_pass_rates,
    render_actions_section,
    update_statuses_from_note,
)
from kaizenlog.report import DailySummary
from kaizenlog.stats import write_stats
from kaizenlog.verdict import (
    apply_verdicts_to_actions_note,
    backfill_verdicts,
    format_verdict_suffix,
    judge_entries,
)
from kaizenlog.vault import ACTIONS_MARKER, ADVICE_MARKER, DailyNoteStore
from kaizenlog.weekly_context import render_weekly_context


def _provisional_entry() -> MemoryEntry:
    return MemoryEntry(
        id="KZN-20260730-001",
        date="2026-07-30",
        action="x",
        status="proposed",
        verdict="pass",
        verdict_value=79.0,
        verdict_date="2026-07-31",
        verdict_stage="provisional",
    )


def _summary(context_switches: int) -> DailySummary:
    return DailySummary(
        day=date(2026, 7, 30),
        total_minutes=120.0,
        by_category={"開発": 120.0},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=context_switches,
        by_site={},
    )


def _judged_action_entry(**kwargs) -> MemoryEntry:
    values = dict(
        id="KZN-20260729-002",
        date="2026-07-29",
        action="依頼前に参照画面を閉じる｜PASS: context_switches <= 185｜FAIL: 186",
        status="proposed",
    )
    values.update(kwargs)
    return MemoryEntry(**values)


def test_memory_entry_defaults_to_confirmed_and_appends_stage(tmp_path):
    entry = MemoryEntry(id="KZN-20260730-001", date="2026-07-30", action="x")
    assert entry.verdict_stage == "confirmed"

    append_entries(tmp_path, [_provisional_entry()])
    raw = json.loads((tmp_path / "suggestions.jsonl").read_text(encoding="utf-8"))
    assert raw["verdict_stage"] == "provisional"


def test_load_entries_stage_backward_compatibility_and_fail_closed(tmp_path):
    rows = [
        {"id": "KZN-20260730-001", "date": "2026-07-30", "action": "legacy"},
        {
            "id": "KZN-20260730-002",
            "date": "2026-07-30",
            "action": "provisional",
            "verdict_stage": "provisional",
        },
        {
            "id": "KZN-20260730-003",
            "date": "2026-07-30",
            "action": "unknown",
            "verdict_stage": "future-stage",
        },
        {
            "id": "KZN-20260730-004",
            "date": "2026-07-30",
            "action": "number",
            "verdict_stage": 1,
        },
        {
            "id": "KZN-20260730-005",
            "date": "2026-07-30",
            "action": "null",
            "verdict_stage": None,
        },
    ]
    (tmp_path / "suggestions.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    loaded = {entry.id: entry for entry in load_entries(tmp_path)}
    assert loaded["KZN-20260730-001"].verdict_stage == "confirmed"
    assert loaded["KZN-20260730-002"].verdict_stage == "provisional"
    assert loaded["KZN-20260730-003"].verdict_stage == "provisional"
    assert loaded["KZN-20260730-004"].verdict_stage == "provisional"
    assert loaded["KZN-20260730-005"].verdict_stage == "provisional"


def test_update_statuses_from_note_done_preserves_provisional_stage():
    entry = _provisional_entry()
    updates = update_statuses_from_note(
        "- [x] KZN-20260730-001: x\n", [entry], date(2026, 8, 1)
    )
    assert len(updates) == 1
    assert updates[0].status == "done"
    assert updates[0].verdict_stage == "provisional"


def test_update_statuses_from_note_skipped_preserves_provisional_stage():
    entry = _provisional_entry()
    updates = update_statuses_from_note(
        "- [-] KZN-20260730-001: x\n", [entry], date(2026, 8, 1)
    )
    assert len(updates) == 1
    assert updates[0].status == "skipped"
    assert updates[0].verdict_stage == "provisional"


def test_mark_entry_done_preserves_provisional_stage():
    updated = mark_entry_done(_provisional_entry(), date(2026, 8, 1))
    assert updated.status == "done"
    assert updated.verdict_stage == "provisional"


def test_mark_entry_skipped_preserves_provisional_stage():
    updated = mark_entry_skipped(_provisional_entry(), reason="later")
    assert updated.status == "skipped"
    assert updated.verdict_stage == "provisional"


def test_judge_entries_today_controls_provisional_and_confirmed_stage():
    proposal_day = date(2026, 7, 29)
    judged_day = date(2026, 7, 30)
    entry = _judged_action_entry()

    provisional = judge_entries(
        [entry],
        proposal_day,
        _summary(79),
        [],
        None,
        judged_day,
        today=judged_day,
    )
    assert provisional[0].verdict == "pass"
    assert provisional[0].verdict_stage == "provisional"

    confirmed = judge_entries(
        [entry],
        proposal_day,
        _summary(79),
        [],
        None,
        judged_day,
        today=date(2026, 7, 31),
    )
    assert confirmed[0].verdict_stage == "confirmed"

    legacy_default = judge_entries(
        [entry], proposal_day, _summary(79), [], None, judged_day
    )
    assert legacy_default[0].verdict_stage == "confirmed"


def test_confirmed_entry_never_downgrades_when_same_measurement_is_rejudged():
    proposal_day = date(2026, 7, 29)
    judged_day = date(2026, 7, 30)
    confirmed = _judged_action_entry(
        verdict="pass",
        verdict_value=79.0,
        verdict_date=judged_day.isoformat(),
        verdict_stage="confirmed",
    )

    corrected = judge_entries(
        [confirmed],
        proposal_day,
        _summary(210),
        [],
        None,
        judged_day,
        today=judged_day,
    )
    assert len(corrected) == 1
    assert corrected[0].verdict == "fail"
    assert corrected[0].verdict_stage == "confirmed"


def test_backfill_promotes_provisional_once_and_is_idempotent(tmp_path):
    stats_dir = tmp_path / "stats"
    measure_day = date(2026, 7, 30)
    write_stats(stats_dir, measure_day, _summary(79), [])
    entry = _judged_action_entry(
        verdict="pass",
        verdict_value=79.0,
        verdict_date=measure_day.isoformat(),
        verdict_stage="provisional",
    )

    promoted = backfill_verdicts([entry], stats_dir, date(2026, 7, 31))
    assert promoted.judged_count == 1
    assert promoted.judged[0].verdict == "pass"
    assert promoted.judged[0].verdict_stage == "confirmed"

    again = backfill_verdicts(promoted.judged, stats_dir, date(2026, 7, 31))
    assert again.judged_count == 0
    assert again.judged == []


def test_backfill_on_measurement_day_keeps_value_provisional(tmp_path):
    stats_dir = tmp_path / "stats"
    measure_day = date(2026, 7, 30)
    write_stats(stats_dir, measure_day, _summary(79), [])
    entry = _judged_action_entry()

    result = backfill_verdicts([entry], stats_dir, measure_day)

    assert result.judged_count == 1
    assert result.judged[0].verdict_stage == "provisional"


def test_same_measurement_value_changes_append_provisional_rows_then_one_confirmation(
    tmp_path,
):
    proposal_day = date(2026, 7, 29)
    judged_day = date(2026, 7, 30)
    current = _judged_action_entry()
    provisional_rows: list[MemoryEntry] = []

    for value in (79, 181, 210):
        updates = judge_entries(
            [current],
            proposal_day,
            _summary(value),
            [],
            None,
            judged_day,
            today=judged_day,
        )
        assert len(updates) == 1
        assert updates[0].verdict_stage == "provisional"
        provisional_rows.extend(updates)
        current = updates[0]

    assert [entry.verdict_value for entry in provisional_rows] == [79.0, 181.0, 210.0]

    stats_dir = tmp_path / "stats"
    write_stats(stats_dir, date(2026, 7, 30), _summary(210), [])
    promoted = backfill_verdicts([current], stats_dir, date(2026, 7, 31))
    assert promoted.judged_count == 1
    assert promoted.judged[0].verdict_stage == "confirmed"


def test_provisional_and_confirmed_verdict_suffixes_are_distinct_and_stable():
    provisional_fail = _judged_action_entry(
        verdict="fail",
        verdict_value=210.0,
        verdict_date="2026-07-30",
        verdict_stage="provisional",
    )
    confirmed_pass = _judged_action_entry(
        verdict="pass",
        verdict_value=79.0,
        verdict_date="2026-07-30",
        verdict_stage="confirmed",
    )
    confirmed_fail = _judged_action_entry(
        verdict="fail",
        verdict_value=210.0,
        verdict_date="2026-07-30",
        verdict_stage="confirmed",
    )

    assert (
        format_verdict_suffix(provisional_fail)
        == "｜判定: ⏳ 集計中（途中値210・目標185以下・7/30の日締め後に確定）"
    )
    assert format_verdict_suffix(confirmed_pass) == "｜判定: ✅ 実測79（目標185）"
    assert (
        format_verdict_suffix(confirmed_fail)
        == "｜判定: ❌ 実測210（目標185・あと25）"
    )


def test_provisional_is_marked_in_action_and_prompt_lines():
    provisional_pass = _judged_action_entry(
        verdict="pass",
        verdict_value=79.0,
        verdict_date="2026-07-30",
        verdict_stage="provisional",
    )

    today_line = format_today_action_line(provisional_pass)
    prompt_line = _verdict_block_line(provisional_pass)
    actions = render_actions_section([provisional_pass], date(2026, 8, 1))

    assert "⏳暫定" in today_line
    assert "⏳PASS" not in today_line and "⏳FAIL" not in today_line
    assert "[⏳暫定PASS]" in prompt_line
    assert "⏳ 集計中" in actions
    assert "☑ 指標は達成済み" not in actions


def test_actions_resync_changes_only_existing_target_tag_and_preserves_bytes():
    target = _judged_action_entry(
        verdict="fail",
        verdict_value=210.0,
        verdict_date="2026-07-30",
        verdict_stage="confirmed",
    )
    start = f"<!-- {ACTIONS_MARKER}:start -->"
    end = f"<!-- {ACTIONS_MARKER}:end -->"
    note = (
        "outside before  \r\n"
        f"{start}\r\n"
        "## 📌 今日のアクション\r\n"
        "- [ ] KZN-20260729-001: other one（7/29提案・判定 ✅ 実測79）\r\n"
        "- [ ] KZN-20260729-003: other two（7/29提案・判定 ✅ 実測79）\r\n"
        "- [ ] KZN-20260729-004: other three（7/29提案・判定 ✅ 実測79）\r\n"
        "- [x] KZN-20260729-002: target（7/29提案・判定 ✅ 実測79）  \r\n"
        f"{end}\r\n"
        "outside after\t"
    )

    updated = apply_verdicts_to_actions_note(note, [target])

    assert updated is not None
    assert updated.startswith("outside before  \r\n" + start)
    assert updated.endswith(end + "\r\n" + "outside after\t")
    assert "- [x] KZN-20260729-002: target（7/29提案・判定 ❌ 実測210）  \r\n" in updated
    assert "KZN-20260729-001: other one（7/29提案・判定 ✅ 実測79）" in updated
    assert "KZN-20260729-003: other two（7/29提案・判定 ✅ 実測79）" in updated
    assert "KZN-20260729-004: other three（7/29提案・判定 ✅ 実測79）" in updated
    assert updated.count("実測210") == 1


def test_actions_resync_uses_measurement_day_and_does_not_create_other_notes(tmp_path):
    notes_dir = tmp_path / "notes"
    store = DailyNoteStore(notes_dir)
    measurement_day = date(2026, 7, 30)
    start = f"<!-- {ACTIONS_MARKER}:start -->"
    end = f"<!-- {ACTIONS_MARKER}:end -->"
    note = (
        "header\n"
        f"{start}\n"
        "- [ ] KZN-20260729-002: target（7/29提案・判定 ⏳ 集計中・途中値79・7/30の日締め後に確定）\n"
        f"{end}\n"
    )
    notes_dir.mkdir()
    store.path_for(measurement_day).write_text(note, encoding="utf-8", newline="")
    confirmed = _judged_action_entry(
        verdict="pass",
        verdict_value=79.0,
        verdict_date=measurement_day.isoformat(),
        verdict_stage="confirmed",
    )

    _resync_measurement_day_actions(
        None, store, [confirmed], today=date(2026, 7, 31)
    )

    assert "判定 ✅ 実測79" in store.read(measurement_day)
    assert not store.path_for(date(2026, 7, 31)).exists()


def test_actions_resync_requires_existing_marker_and_recent_measurement_day(tmp_path):
    notes_dir = tmp_path / "notes"
    store = DailyNoteStore(notes_dir)
    store.path_for(date(2026, 7, 30)).parent.mkdir()
    no_marker = "before\nKZN-20260729-002: target\nafter\n"
    store.path_for(date(2026, 7, 30)).write_text(no_marker, encoding="utf-8", newline="")
    old_day = date(2026, 7, 20)
    old_note = (
        f"<!-- {ACTIONS_MARKER}:start -->\n"
        "- [ ] KZN-20260729-005: target（7/29提案・判定 ✅ 実測79）\n"
        f"<!-- {ACTIONS_MARKER}:end -->\n"
    )
    store.path_for(old_day).write_text(old_note, encoding="utf-8", newline="")
    unmatched_day = date(2026, 7, 31)
    unmatched_note = (
        f"<!-- {ACTIONS_MARKER}:start -->\n"
        "- [ ] KZN-20260729-999: another target（7/29提案）\n"
        f"<!-- {ACTIONS_MARKER}:end -->\n"
    )
    store.path_for(unmatched_day).write_text(
        unmatched_note, encoding="utf-8", newline=""
    )
    update = _judged_action_entry(
        verdict="fail",
        verdict_value=210.0,
        verdict_date="2026-07-20",
        verdict_stage="confirmed",
        id="KZN-20260729-005",
    )
    unmatched_update = _judged_action_entry(
        verdict="fail",
        verdict_value=210.0,
        verdict_date=unmatched_day.isoformat(),
        verdict_stage="confirmed",
    )

    _resync_measurement_day_actions(
        None, store, [update, unmatched_update], today=date(2026, 8, 1)
    )

    assert store.read(date(2026, 7, 30)) == no_marker
    assert store.read(old_day) == old_note
    assert store.read(unmatched_day) == unmatched_note


def test_provisional_verdicts_are_excluded_from_all_learning_consumers(tmp_path):
    today = date(2026, 8, 1)
    action = "改善する｜PASS: context_switches <= 10｜FAIL: context_switches > 10"
    provisional_entries = [
        _judged_action_entry(
            id=f"KZN-202607{20 + day_offset:02d}-001",
            date=f"2026-07-{20 + day_offset:02d}",
            action=action,
            status="done",
            done_date=f"2026-07-{21 + day_offset:02d}",
            verdict="fail",
            verdict_value=20.0,
            verdict_date=f"2026-07-{21 + day_offset:02d}",
            verdict_stage="provisional",
        )
        for day_offset in (1, 2, 3)
    ]
    provisional_entries.append(
        _judged_action_entry(
            id="KZN-20260720-001",
            date="2026-07-20",
            action=action,
            status="done",
            done_date="2026-07-21",
            verdict="pass",
            verdict_value=5.0,
            verdict_date="2026-07-21",
            verdict_stage="provisional",
        )
    )

    stats = compute_action_stats(provisional_entries, today)
    assert stats.judged == 0
    assert stats.passed == 0
    assert stats.done_judged == 0
    assert metric_pass_rates(provisional_entries, today, min_judged=3) == []
    assert consecutive_fail_actions(provisional_entries, today) == []
    decay_memory = tmp_path / "decay-memory"
    decay_stats = tmp_path / "decay-stats"
    append_entries(decay_memory, provisional_entries)
    decay_stats.mkdir()
    for d in (date(2026, 7, 28) - timedelta(days=i) for i in range(7)):
        write_stats(decay_stats, d, _summary(20), [])
    assert detect_kzn_decay(decay_memory, decay_stats, as_of=date(2026, 7, 28)) == []

    morning_entries = [
        MemoryEntry(
            id="KZN-20260724-001",
            date="2026-07-24",
            action=action,
            status="done",
            done_date="2026-07-25",
            verdict="pass",
            verdict_value=5.0,
            verdict_date="2026-07-25",
            verdict_stage="provisional",
        ),
        MemoryEntry(
            id="KZN-20260724-002",
            date="2026-07-24",
            action=action,
            status="done",
            done_date="2026-07-25",
            verdict="fail",
            verdict_value=20.0,
            verdict_date="2026-07-25",
            verdict_stage="provisional",
        ),
    ]
    morning = build_morning_notification(morning_entries, date(2026, 7, 26))
    assert morning is not None
    assert "昨日の判定 実行済み✅0 ❌0" in morning
    assert "✅1" not in morning and "❌1" not in morning
    assert "⏳暫定" in format_today_action_line(morning_entries[0])

    stats_dir = tmp_path / "weekly-stats"
    memory_dir = tmp_path / "weekly-memory"
    experiments_dir = tmp_path / "experiments"
    stats_dir.mkdir()
    experiments_dir.mkdir()
    append_entries(memory_dir, provisional_entries)
    for d in (date(2026, 7, 20) + timedelta(days=i) for i in range(9)):
        write_stats(stats_dir, d, _summary(20), [])
    weekly_provisional = render_weekly_context(
        stats_dir, memory_dir, experiments_dir, date(2026, 7, 20)
    )
    assert "判定: 0件 / PASS: 0件" in weekly_provisional

    confirmed_entries = [
        entry.__class__(**{**entry.__dict__, "verdict_stage": "confirmed"})
        for entry in provisional_entries
    ]
    confirmed_stats = compute_action_stats(confirmed_entries, today)
    assert confirmed_stats.judged == 4
    assert confirmed_stats.passed == 1
    assert metric_pass_rates(confirmed_entries, today, min_judged=3) == [
        ("context_switches", 1, 4)
    ]
    assert consecutive_fail_actions(confirmed_entries, today)
    confirmed_decay_memory = tmp_path / "confirmed-decay-memory"
    append_entries(confirmed_decay_memory, confirmed_entries)
    assert len(
        detect_kzn_decay(
            confirmed_decay_memory, decay_stats, as_of=date(2026, 7, 28)
        )
    ) == 1

    confirmed_dir = tmp_path / "confirmed-memory"
    append_entries(confirmed_dir, confirmed_entries)
    weekly_confirmed = render_weekly_context(
        stats_dir, confirmed_dir, experiments_dir, date(2026, 7, 20)
    )
    assert "判定: 4件 / PASS: 1件" in weekly_confirmed


# --- 第37弾 Phase 0: §Z1 / §Z2 / §Z3 ---


def test_z1_done_date_does_not_move_existing_verdict_date(tmp_path):
    """provisional(verdict_date=D) + done_date=D → 測定日は D のまま confirmed 昇格。"""
    stats_dir = tmp_path / "stats"
    measure_day = date(2026, 7, 30)
    write_stats(stats_dir, measure_day, _summary(79), [])
    # 未判定時なら done_date+1=7/31 になるが、verdict_date があるので 7/30 を固定
    entry = _judged_action_entry(
        verdict="pass",
        verdict_value=79.0,
        verdict_date=measure_day.isoformat(),
        verdict_stage="provisional",
        status="done",
        done_date=measure_day.isoformat(),
    )
    # 7/31 に stats があってもそちらへ付け替えない
    write_stats(stats_dir, date(2026, 7, 31), _summary(999), [])

    promoted = backfill_verdicts([entry], stats_dir, date(2026, 7, 31))
    assert promoted.judged_count == 1
    assert promoted.judged[0].verdict_date == "2026-07-30"
    assert promoted.judged[0].verdict_value == 79.0
    assert promoted.judged[0].verdict_stage == "confirmed"


def test_z1_undone_with_done_date_still_uses_done_date_plus_one(tmp_path):
    """未判定 + done_date は従来どおり done_date+1 を測定日にする。"""
    stats_dir = tmp_path / "stats"
    # done_date=7/30 → measure=7/31
    write_stats(stats_dir, date(2026, 7, 31), _summary(50), [])
    # 提案日+1=7/30 には別値を置いて、done_date 経路を使うことだけを固定
    write_stats(stats_dir, date(2026, 7, 30), _summary(999), [])
    entry = _judged_action_entry(
        status="done",
        done_date="2026-07-30",
    )
    result = backfill_verdicts([entry], stats_dir, date(2026, 8, 1))
    assert result.judged_count == 1
    assert result.judged[0].verdict_date == "2026-07-31"
    assert result.judged[0].verdict_value == 50.0
    assert result.judged[0].verdict_stage == "confirmed"


def test_z2_confirmed_to_provisional_is_skipped_with_warning(capsys):
    """confirmed→provisional は追記せず当該IDのみスキップ・警告。他IDは継続。"""
    from kaizenlog.verdict import filter_allowed_stage_updates

    confirmed = _judged_action_entry(
        id="KZN-20260729-002",
        verdict="pass",
        verdict_value=79.0,
        verdict_date="2026-07-30",
        verdict_stage="confirmed",
    )
    other = _judged_action_entry(
        id="KZN-20260729-003",
        date="2026-07-29",
        action="y｜PASS: context_switches <= 185｜FAIL: 186",
        verdict=None,
        verdict_value=None,
        verdict_date=None,
        verdict_stage="confirmed",
    )
    # 表外: confirmed → provisional
    bad = _judged_action_entry(
        id="KZN-20260729-002",
        verdict="fail",
        verdict_value=210.0,
        verdict_date="2026-07-30",
        verdict_stage="provisional",
    )
    # 許可: 未判定 → provisional
    good = _judged_action_entry(
        id="KZN-20260729-003",
        date="2026-07-29",
        action="y｜PASS: context_switches <= 185｜FAIL: 186",
        verdict="pass",
        verdict_value=10.0,
        verdict_date="2026-07-30",
        verdict_stage="provisional",
    )
    warnings: list[str] = []
    kept = filter_allowed_stage_updates(
        {confirmed.id: confirmed, other.id: other},
        [bad, good],
        warn=warnings.append,
    )
    assert [e.id for e in kept] == ["KZN-20260729-003"]
    assert any("KZN-20260729-002" in w and "表外" in w for w in warnings)


def test_z3_console_verdict_lines_provisional_and_confirmed(tmp_path, monkeypatch, capsys):
    """cmd_generate コンソールが stage を見て ⏳途中値 / ✅実測 を出す（両側）。"""
    from unittest.mock import MagicMock
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from kaizenlog import cli as cli_mod
    from kaizenlog.config import Config

    vault = tmp_path / "vault"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    memory = vault / "Kaizen" / "Memory"
    memory.mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / "03 Areas" / "Kaizen Experiments").mkdir(parents=True)

    proposal = date(2026, 7, 24)
    today = date(2026, 7, 25)
    append_entries(
        memory,
        [
            MemoryEntry(
                id="KZN-20260724-001",
                date=proposal.isoformat(),
                action="x｜PASS: context_switches <= 40｜FAIL: y",
            )
        ],
    )
    # 前日 ADVICE 区間（判定 suffix 用）
    start = f"<!-- {ADVICE_MARKER}:start -->"
    end = f"<!-- {ADVICE_MARKER}:end -->"
    (daily / f"{proposal.isoformat()}.md").write_text(
        f"handwritten\n{start}\n- [ ] KZN-20260724-001: body\n{end}\n",
        encoding="utf-8",
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

    monkeypatch.setattr(
        cli_mod,
        "collect_day",
        lambda *a, **k: ([], True),
    )
    monkeypatch.setattr(
        cli_mod,
        "summarize",
        lambda day, classified, gap_minutes=30: DailySummary(
            day=day,
            total_minutes=120.0,
            by_category={"開発": 120.0},
            by_app={},
            blocks=[],
            ai_tool_minutes={},
            ai_sessions=0,
            context_switches=30,
            by_site={},
        ),
    )
    monkeypatch.setattr(cli_mod, "collect_input", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_mod,
        "render_markdown",
        lambda *a, **k: "### カテゴリ別\n| a | b |\n",
    )
    monkeypatch.setattr(cli_mod.Classifier, "classify_all", lambda self, e: [])
    monkeypatch.setattr(
        cli_mod, "ActivityWatchClient", lambda url: MagicMock()
    )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 25, 12, 0, tzinfo=tz or ZoneInfo("Asia/Tokyo"))

    monkeypatch.setattr(cli_mod, "datetime", FixedDateTime)

    capsys.readouterr()
    cli_mod.cmd_generate(cfg, today)
    out = capsys.readouterr().out
    # 測定日==today → provisional: ⏳ と 途中値
    assert "🧪 アクション判定: KZN-20260724-001 ⏳" in out
    assert "途中値 30" in out
    assert "実測 30" not in out

    # 翌日 backfill で confirmed 昇格（コンソールは バックフィル判定）
    class NextDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 26, 12, 0, tzinfo=tz or ZoneInfo("Asia/Tokyo"))

    monkeypatch.setattr(cli_mod, "datetime", NextDay)
    monkeypatch.setattr(
        cli_mod,
        "summarize",
        lambda day, classified, gap_minutes=30: DailySummary(
            day=day,
            total_minutes=100.0,
            by_category={"開発": 100.0},
            by_app={},
            blocks=[],
            ai_tool_minutes={},
            ai_sessions=0,
            context_switches=5,
            by_site={},
        ),
    )
    capsys.readouterr()
    cli_mod.cmd_generate(cfg, date(2026, 7, 26))
    out2 = capsys.readouterr().out
    assert "🧪 バックフィル判定: KZN-20260724-001 ✅" in out2
    assert "実測 30" in out2
    assert "途中値 30" not in out2


def test_z3_consecutive_metric_fails_provisional_excluded_confirmed_counted():
    from kaizenlog.memory import _consecutive_metric_fails

    today = date(2026, 8, 1)
    action = "改善｜PASS: context_switches <= 10｜FAIL: 11"
    provisional = [
        _judged_action_entry(
            id=f"KZN-2026072{i}-001",
            date=f"2026-07-2{i}",
            action=action,
            status="done",
            done_date=f"2026-07-2{i}",
            verdict="fail",
            verdict_value=20.0,
            verdict_date=f"2026-07-2{i}",
            verdict_stage="provisional",
        )
        for i in (1, 2)
    ]
    assert _consecutive_metric_fails(provisional, today, n=2) == []

    confirmed = [
        MemoryEntry(**{**e.__dict__, "verdict_stage": "confirmed"})
        for e in provisional
    ]
    assert _consecutive_metric_fails(confirmed, today, n=2) == ["context_switches"]


def test_z3_undone_judged_provisional_excluded_confirmed_counted():
    today = date(2026, 8, 1)
    action = "改善｜PASS: context_switches <= 10｜FAIL: 11"
    provisional = _judged_action_entry(
        id="KZN-20260720-001",
        date="2026-07-20",
        action=action,
        status="proposed",  # 未実行
        verdict="pass",
        verdict_value=5.0,
        verdict_date="2026-07-21",
        verdict_stage="provisional",
    )
    stats_p = compute_action_stats([provisional], today)
    assert stats_p.undone_judged == 0
    assert stats_p.undone_passed == 0
    assert stats_p.judged == 0

    confirmed = MemoryEntry(**{**provisional.__dict__, "verdict_stage": "confirmed"})
    stats_c = compute_action_stats([confirmed], today)
    assert stats_c.undone_judged == 1
    assert stats_c.undone_passed == 1
    assert stats_c.judged == 1
