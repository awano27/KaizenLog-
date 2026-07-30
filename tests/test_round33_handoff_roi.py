"""第33弾: 申し送りROIメーター。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from kaizenlog.handoff import (
    build_agent_context_section,
    collect_handoff_lessons,
    run_handoff_for_target,
)
from kaizenlog.handoffledger import (
    HandoffLesson,
    abs_target,
    append_handoff_ledger,
    approx_tokens,
    build_roi_rows,
    compute_rent,
    format_weekly_handoff_roi_section,
    inject_promoted_lesson,
    load_handoff_ledger,
    mark_promote_candidates,
    measure_effect,
    project_name_for_target,
    record_lessons_on_apply,
    set_lesson_status,
    HandoffLedgerEntry,
)
from kaizenlog.aiwork import AISession, UserPrompt
from kaizenlog.memory import MemoryEntry, append_entries
from kaizenlog.promptledger import PromptLedgerEntry, append_prompt_ledger
from kaizenlog.vault import AGENT_CONTEXT_MARKER, extract_section
from kaizenlog.weekly_context import render_weekly_context


AS_OF = date(2026, 8, 15)
FI = date(2026, 7, 15)  # +31 days → as_of で後窓30日完了
TZ = timezone.utc


def _sess(
    project: str,
    day: date,
    *,
    tool_errors: int = 0,
    user_turns: int = 5,
    sid: str = "s1",
) -> AISession:
    start = datetime(day.year, day.month, day.day, 10, 0, tzinfo=TZ)
    return AISession(
        session_id=sid,
        project=project,
        start=start,
        end=start + timedelta(hours=1),
        user_turns=user_turns,
        tool_errors=tool_errors,
    )


def _write_stat(stats: Path, day: date, *, retry: int = 0, errors: int = 0) -> None:
    import json

    stats.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 2,
        "day": day.isoformat(),
        "total_minutes": 100,
        "context_switches": 1,
        "ai_activity_blocks": 0,
        "by_category": {},
        "by_app": {},
        "by_site": {},
        "blocks": [],
        "ai": {
            "sessions": 1,
            "retry_chains": retry,
            "tool_errors": errors,
            "api_calls": 1,
        },
    }
    (stats / f"{day.isoformat()}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def test_a1_stable_ids_and_first_injected_immutable(tmp_path: Path):
    stats = tmp_path / "stats"
    mem = tmp_path / "mem"
    mem.mkdir()
    repo = tmp_path / "myrepo"
    repo.mkdir()
    target = repo / "CLAUDE.md"
    for i in range(5):
        _write_stat(stats, AS_OF - timedelta(days=i), retry=1, errors=1)

    s1, les1 = run_handoff_for_target(
        target=target,
        stats_dir=stats,
        memory_dir=mem,
        as_of=AS_OF,
    )
    assert any(l.lesson_id == "HND-retry-trend" for l in les1)
    assert any(l.lesson_id == "HND-tool-errors" for l in les1)
    led1 = load_handoff_ledger(mem)
    fi_map = {e.lesson_id: e.first_injected for e in led1}
    assert fi_map["HND-retry-trend"] == AS_OF.isoformat()

    # 2回目: first_injected 不変・lesson_id 同一
    s2, les2 = run_handoff_for_target(
        target=target,
        stats_dir=stats,
        memory_dir=mem,
        as_of=AS_OF + timedelta(days=1),
    )
    ids1 = {l.lesson_id for l in les1}
    ids2 = {l.lesson_id for l in les2}
    assert ids1 == ids2
    led2 = load_handoff_ledger(mem)
    for e in led2:
        assert e.first_injected == fi_map[e.lesson_id]
    # 冪等: 同 as_of 再実行で byte 同一
    s3, _ = run_handoff_for_target(
        target=target, stats_dir=stats, memory_dir=mem, as_of=AS_OF + timedelta(days=1)
    )
    assert target.read_bytes() == Path(target).read_bytes()
    assert s2 == s3


def test_a1_suppress_excludes_and_unsuppress_restores(tmp_path: Path):
    stats = tmp_path / "stats"
    mem = tmp_path / "mem"
    mem.mkdir()
    repo = tmp_path / "proj"
    repo.mkdir()
    target = repo / "CLAUDE.md"
    handwritten = b"# policy\n\nKEEP_OUTSIDE\n"
    target.write_bytes(handwritten)
    for i in range(3):
        _write_stat(stats, AS_OF - timedelta(days=i), retry=2, errors=2)

    run_handoff_for_target(
        target=target, stats_dir=stats, memory_dir=mem, as_of=AS_OF
    )
    body0 = extract_section(target.read_text(encoding="utf-8"), AGENT_CONTEXT_MARKER)
    assert body0 and "リトライ" in body0

    set_lesson_status(mem, "HND-retry-trend", "suppressed", target=target)
    run_handoff_for_target(
        target=target, stats_dir=stats, memory_dir=mem, as_of=AS_OF
    )
    text1 = target.read_text(encoding="utf-8")
    body1 = extract_section(text1, AGENT_CONTEXT_MARKER)
    assert body1 is not None
    assert "### リトライ傾向" not in body1
    assert "KEEP_OUTSIDE" in text1
    # 冪等
    b1 = target.read_bytes()
    run_handoff_for_target(
        target=target, stats_dir=stats, memory_dir=mem, as_of=AS_OF
    )
    assert target.read_bytes() == b1
    # マーカー外 byte 保存
    assert target.read_bytes().startswith(handwritten)

    set_lesson_status(mem, "HND-retry-trend", "active", target=target)
    run_handoff_for_target(
        target=target, stats_dir=stats, memory_dir=mem, as_of=AS_OF
    )
    body2 = extract_section(target.read_text(encoding="utf-8"), AGENT_CONTEXT_MARKER)
    assert body2 and "### リトライ傾向" in body2


def test_a2_rent_project_match_and_unknown(tmp_path: Path):
    repo = tmp_path / "AlphaRepo"
    repo.mkdir()
    target = repo / "CLAUDE.md"
    target.write_text("# x\n", encoding="utf-8")
    assert project_name_for_target(target) == "AlphaRepo"

    text = "A" * 40  # 10 tok
    matched = [_sess("AlphaRepo", AS_OF, sid="a"), _sess("AlphaRepo", AS_OF, sid="b")]
    tok, n, disp = compute_rent(text, matched, target)
    assert tok == 10
    assert n == 2
    assert "10 tok" in disp and "2 sess" in disp and "20 tok·sess" in disp

    # 不一致 → 不明
    other = [_sess("Other", AS_OF)]
    tok2, n2, disp2 = compute_rent(text, other, target)
    assert n2 is None
    assert disp2 == "不明"
    # 空セッション → 不明
    _, n3, disp3 = compute_rent(text, [], target)
    assert n3 is None and disp3 == "不明"


def test_a3_effect_window_boundary_and_kzn_unknown(tmp_path: Path):
    mem = tmp_path / "mem"
    mem.mkdir()
    stats = tmp_path / "stats"
    stats.mkdir()
    repo = tmp_path / "r"
    repo.mkdir()
    target = repo / "CLAUDE.md"

    les = HandoffLesson(
        lesson_id="HND-tool-errors",
        kind="toolerr",
        ref_id="tool-errors",
        text="err",
    )
    # 後窓29日 = 計測中
    as_of_29 = FI + timedelta(days=28)  # after_days = 29
    disp, good = measure_effect(
        les,
        target=target,
        first_injected=FI.isoformat(),
        as_of=as_of_29,
        prompts=[],
        sessions=[],
        memory_dir=mem,
        stats_dir=stats,
    )
    assert "計測中(29/30日)" in disp
    assert good is None

    # 30日ちょうど → 判定へ（セッション無ければ不明）
    as_of_30 = FI + timedelta(days=29)
    disp2, good2 = measure_effect(
        les,
        target=target,
        first_injected=FI.isoformat(),
        as_of=as_of_30,
        prompts=[],
        sessions=[],
        memory_dir=mem,
        stats_dir=stats,
    )
    assert good2 is None
    assert "不明" in disp2 or "効いている" in disp2 or "効果なし" in disp2

    # 前後同数 = 効果なし
    before_days = [FI - timedelta(days=i) for i in range(1, 11)]
    after_days = [FI + timedelta(days=i) for i in range(0, 10)]
    sessions = []
    for i, d in enumerate(before_days):
        sessions.append(
            _sess("r", d, tool_errors=2, sid=f"b{i}")
        )
    for i, d in enumerate(after_days):
        sessions.append(
            _sess("r", d, tool_errors=2, sid=f"a{i}")
        )
    disp3, good3 = measure_effect(
        les,
        target=target,
        first_injected=FI.isoformat(),
        as_of=as_of_30,
        prompts=[],
        sessions=sessions,
        memory_dir=mem,
        stats_dir=stats,
    )
    assert good3 is False
    assert "効果なし" in disp3

    # kzn: 測定可能2日 = 不明
    append_entries(
        mem,
        [
            MemoryEntry(
                id="KZN-20260701-001",
                date="2026-07-01",
                action="do | PASS: ai_retry_chains <= 2 | FAIL: note",
                status="done",
                done_date="2026-07-01",
            )
        ],
    )
    # only 2 measurable days before and after
    for d in [FI - timedelta(days=2), FI - timedelta(days=1), FI, FI + timedelta(days=1)]:
        _write_stat(stats, d, retry=5)
    kzn = HandoffLesson(
        lesson_id="HND-kzn-KZN-20260701-001",
        kind="kzn",
        ref_id="KZN-20260701-001",
        text="kzn line",
    )
    disp4, good4 = measure_effect(
        kzn,
        target=target,
        first_injected=FI.isoformat(),
        as_of=as_of_30,
        prompts=[],
        sessions=[],
        memory_dir=mem,
        stats_dir=stats,
    )
    assert good4 is None
    assert disp4 == "不明"


def test_a3_prm_effect_good(tmp_path: Path):
    mem = tmp_path / "mem"
    mem.mkdir()
    stats = tmp_path / "stats"
    stats.mkdir()
    repo = tmp_path / "r"
    repo.mkdir()
    target = repo / "CLAUDE.md"
    append_prompt_ledger(
        mem,
        [
            PromptLedgerEntry(
                id="PRM-20260701-001",
                representative="foo bar baz",
                count_total=10,
                days_seen=5,
                first_seen="2026-07-01",
                last_seen="2026-08-01",
                status="new",
            )
        ],
    )
    les = HandoffLesson(
        lesson_id="HND-prm-PRM-20260701-001",
        kind="prm",
        ref_id="PRM-20260701-001",
        text="prm",
    )
    prompts = []
    # before: 5 matches, after: 1 match → 効いている
    for i in range(5):
        prompts.append(
            UserPrompt(
                text="foo bar baz",
                timestamp=datetime(
                    FI.year, FI.month, FI.day, 12, 0, tzinfo=TZ
                )
                - timedelta(days=i + 1),
                project="r",
            )
        )
    prompts.append(
        UserPrompt(
            text="foo bar baz",
            timestamp=datetime(FI.year, FI.month, FI.day, 12, 0, tzinfo=TZ)
            + timedelta(days=1),
            project="r",
        )
    )
    as_of_30 = FI + timedelta(days=29)
    disp, good = measure_effect(
        les,
        target=target,
        first_injected=FI.isoformat(),
        as_of=as_of_30,
        prompts=prompts,
        sessions=[],
        memory_dir=mem,
        stats_dir=stats,
    )
    assert good is True
    assert "効いている" in disp


def test_b1_promote_candidate_requires_two_targets(tmp_path: Path):
    mem = tmp_path / "mem"
    mem.mkdir()
    stats = tmp_path / "stats"
    stats.mkdir()
    r1 = tmp_path / "repo1"
    r2 = tmp_path / "repo2"
    r1.mkdir()
    r2.mkdir()
    t1 = r1 / "CLAUDE.md"
    t2 = r2 / "CLAUDE.md"
    t1.write_text("#1\n", encoding="utf-8")
    t2.write_text("#2\n", encoding="utf-8")

    les = HandoffLesson(
        lesson_id="HND-tool-errors",
        kind="toolerr",
        ref_id="tool-errors",
        text="tool errors block text here",
    )
    # 両 target に first_injected
    for t in (t1, t2):
        record_lessons_on_apply(mem, target=t, lessons=[les], as_of=FI)

    # 効果あり: before tool_errors 多、after 少
    sessions_r1 = []
    sessions_r2 = []
    for i in range(10):
        sessions_r1.append(_sess("repo1", FI - timedelta(days=i + 1), tool_errors=5, sid=f"b1{i}"))
        sessions_r1.append(_sess("repo1", FI + timedelta(days=i), tool_errors=0, sid=f"a1{i}"))
        sessions_r2.append(_sess("repo2", FI - timedelta(days=i + 1), tool_errors=5, sid=f"b2{i}"))
        sessions_r2.append(_sess("repo2", FI + timedelta(days=i), tool_errors=0, sid=f"a2{i}"))
    as_of_30 = FI + timedelta(days=29)
    ledger = load_handoff_ledger(mem)

    rows1 = build_roi_rows(
        target=t1,
        lessons=[les],
        ledger=ledger,
        sessions=sessions_r1,
        prompts=[],
        memory_dir=mem,
        stats_dir=stats,
        as_of=as_of_30,
    )
    # 1 target のみ → promote_candidate なし
    mark_promote_candidates({abs_target(t1): rows1})
    assert rows1[0].effect_good is True
    assert rows1[0].promote_candidate is False

    rows2 = build_roi_rows(
        target=t2,
        lessons=[les],
        ledger=ledger,
        sessions=sessions_r2,
        prompts=[],
        memory_dir=mem,
        stats_dir=stats,
        as_of=as_of_30,
    )
    mark_promote_candidates({abs_target(t1): rows1, abs_target(t2): rows2})
    assert rows1[0].promote_candidate is True
    assert rows2[0].promote_candidate is True


def test_b1_promote_excludes_from_targets_and_global_required(tmp_path: Path):
    stats = tmp_path / "stats"
    mem = tmp_path / "mem"
    mem.mkdir()
    repo = tmp_path / "proj"
    repo.mkdir()
    target = repo / "CLAUDE.md"
    global_t = tmp_path / "GLOBAL.md"
    for i in range(3):
        _write_stat(stats, AS_OF - timedelta(days=i), retry=1, errors=1)

    run_handoff_for_target(
        target=target, stats_dir=stats, memory_dir=mem, as_of=AS_OF
    )
    body0 = extract_section(target.read_text(encoding="utf-8"), AGENT_CONTEXT_MARKER)
    assert body0 and "ツールエラー" in body0

    set_lesson_status(mem, "HND-tool-errors", "promoted")
    les = HandoffLesson(
        lesson_id="HND-tool-errors",
        kind="toolerr",
        ref_id="tool-errors",
        text="promoted tool errors",
    )
    inject_promoted_lesson(global_t, les, as_of=AS_OF)
    gbody = extract_section(global_t.read_text(encoding="utf-8"), AGENT_CONTEXT_MARKER)
    assert gbody and "HND-tool-errors" in gbody

    run_handoff_for_target(
        target=target, stats_dir=stats, memory_dir=mem, as_of=AS_OF
    )
    body1 = extract_section(target.read_text(encoding="utf-8"), AGENT_CONTEXT_MARKER)
    assert body1 is not None
    assert "### 頻出ツールエラー" not in body1


def test_b1_redact_before_truncate():
    secret = "SECRET_TOKEN_ABCDEFGHIJKLMNOPQRSTUVWXYZ_and_more_padding_here"
    les = HandoffLesson(
        lesson_id="HND-prm-PRM-1",
        kind="prm",
        ref_id="PRM-1",
        text=f"prefix {secret} suffix and more text to exceed sixty chars easily",
    )
    from kaizenlog.handoffledger import build_roi_rows

    def redactor(s: str) -> str:
        return s.replace("SECRET_TOKEN_ABCDEFGHIJKLMNOPQRSTUVWXYZ", "[REDACTED]")

    # use empty dirs
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        mem = td_p / "m"
        mem.mkdir()
        stats = td_p / "s"
        stats.mkdir()
        t = td_p / "CLAUDE.md"
        t.write_text("x", encoding="utf-8")
        append_handoff_ledger(
            mem,
            [
                HandoffLedgerEntry(
                    lesson_id=les.lesson_id,
                    target=abs_target(t),
                    first_injected=FI.isoformat(),
                    kind="prm",
                    ref_id="PRM-1",
                    status="active",
                )
            ],
        )
        rows = build_roi_rows(
            target=t,
            lessons=[les],
            ledger=load_handoff_ledger(mem),
            sessions=[],
            prompts=[],
            memory_dir=mem,
            stats_dir=stats,
            as_of=AS_OF,
            redactor=redactor,
        )
        assert "SECRET_TOKEN" not in rows[0].lesson.text
        assert "[REDACTED]" in rows[0].lesson.text
        assert len(rows[0].lesson.text) <= 60


def test_c1_weekly_section_present_and_absent(tmp_path: Path):
    stats = tmp_path / "stats"
    mem = tmp_path / "mem"
    exp = tmp_path / "exp"
    mem.mkdir()
    exp.mkdir()
    week = date(2026, 7, 20)  # Monday
    for i in range(7):
        _write_stat(stats, week + timedelta(days=i))

    md0 = render_weekly_context(stats, mem, exp, week)
    assert "申し送りROI" not in md0

    append_handoff_ledger(
        mem,
        [
            HandoffLedgerEntry(
                lesson_id="HND-retry-trend",
                target=str(tmp_path / "x" / "CLAUDE.md"),
                first_injected="2026-07-01",
                kind="retry",
                ref_id="retry-trend",
                status="active",
            )
        ],
    )
    md1 = render_weekly_context(stats, mem, exp, week)
    assert "## 申し送りROI" in md1
    assert "抑制候補" in md1
    assert "promoted" in md1


def test_stable_prm_kzn_ids(tmp_path: Path):
    stats = tmp_path / "stats"
    mem = tmp_path / "mem"
    as_of = date(2026, 7, 29)
    _write_stat(stats, as_of, retry=2, errors=5)
    append_prompt_ledger(
        mem,
        [
            PromptLedgerEntry(
                id="PRM-20260701-001",
                representative="ニュースを要約して",
                count_total=9,
                days_seen=3,
                first_seen="2026-07-01",
                last_seen="2026-07-20",
                status="new",
            )
        ],
    )
    append_entries(
        mem,
        [
            MemoryEntry(
                id="KZN-20260728-001",
                date="2026-07-28",
                action="do | PASS: ai_retry_chains <= 2 | FAIL: note",
                status="done",
                done_date="2026-07-28",
                verdict="fail",
                verdict_value=5.0,
                verdict_date="2026-07-28",
            ),
            MemoryEntry(
                id="KZN-20260727-001",
                date="2026-07-27",
                action="do | PASS: ai_retry_chains <= 2 | FAIL: note",
                status="done",
                done_date="2026-07-27",
                verdict="fail",
                verdict_value=4.0,
                verdict_date="2026-07-27",
            ),
        ],
    )
    lessons = collect_handoff_lessons(
        stats_dir=stats, memory_dir=mem, as_of=as_of
    )
    ids = {l.lesson_id for l in lessons}
    assert "HND-prm-PRM-20260701-001" in ids
    assert any(i.startswith("HND-kzn-KZN-") for i in ids)
    # 再生成で同一
    lessons2 = collect_handoff_lessons(
        stats_dir=stats, memory_dir=mem, as_of=as_of
    )
    assert {l.lesson_id for l in lessons2} == ids


def test_approx_tokens():
    assert approx_tokens("abcd") == 1
    assert approx_tokens("") == 0
    assert approx_tokens("a" * 7) == 1
