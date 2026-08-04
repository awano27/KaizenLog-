"""第54弾: 洞察候補プール + 朝決算カード。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.advice_evidence import (
    AdviceEvidence,
    build_advice_evidence,
    build_insight_candidates,
)
from kaizenlog.advice_format import (
    insight_selection_errors,
    render_advice_markdown,
    resolve_insight_lines,
    validate_advice,
)
from kaizenlog.decision import (
    build_morning_decision_section,
    build_settlement_block,
    parse_decision_choice,
    recompose_decision_section,
    select_decision_question_entry,
    strip_settlement,
    yesterday_confirmed_entries,
)
from kaizenlog.memory import MemoryEntry, append_entries, load_entries
from kaizenlog.vault import (
    DECISION_MARKER,
    SECTION_ORDER,
    RESUME_MARKER,
    GOAL_MARKER,
    extract_section,
    upsert_section,
)

DAY = date(2026, 8, 3)
TZ = ZoneInfo("Asia/Tokyo")
_BANNED = ("良い", "悪い", "多すぎ", "すべき", "改善しろ")


def _rich_stats(**kw) -> dict:
    s = {
        "day": DAY.isoformat(),
        "total_minutes": 200.0,
        "context_switches": 40,
        "by_category": {"開発": 100.0, "AI作業": 50.0},
        "by_app": {"Code.exe": 120.0},
        "by_site": {"docs.example.com": 45.0},
        "blocks": [
            {
                "start": f"{DAY.isoformat()}T10:00:00+09:00",
                "end": f"{DAY.isoformat()}T11:00:00+09:00",
                "category": "開発",
            },
            {
                "start": f"{DAY.isoformat()}T21:00:00+09:00",
                "end": f"{DAY.isoformat()}T21:40:00+09:00",
                "category": "AI作業",
            },
        ],
        "input": {
            "focus_blocks": 3,
            "focus_minutes": 60.0,
            "active_input_minutes": 90.0,
            "keypresses": 1000,
        },
        "ai": {
            "sessions": 4,
            "retry_chains": 2,
            "tool_errors": 5,
            "loop_tax": {"episode_count": 2, "est_cost_usd": 0.4},
            "session_digests": [
                {
                    "ended_in_error": True,
                    "tests_run": False,
                    "end": f"{DAY.isoformat()}T21:30:00+09:00",
                },
                {
                    "ended_in_error": False,
                    "tests_run": True,
                    "end": f"{DAY.isoformat()}T15:00:00+09:00",
                },
                {
                    "ended_in_error": False,
                    "tests_run": True,
                    "end": f"{DAY.isoformat()}T16:00:00+09:00",
                },
            ],
        },
        "outcome_git": [
            {
                "repo_label": "X",
                "commits": 4,
                "subjects": ["a"],
            }
        ],
    }
    s.update(kw)
    return s


def _hist(n: int = 5) -> list[dict]:
    out = []
    for i in range(n, 0, -1):
        d = DAY - timedelta(days=i)
        out.append(
            {
                "day": d.isoformat(),
                "total_minutes": 180.0,
                "context_switches": 30 + i,
                "by_site": {"github.com": 10.0},
                "ai": {"retry_chains": 1, "sessions": 2},
            }
        )
    return out


def _valid_advice_payload() -> dict:
    return {
        "plan_review": None,
        "proposals": [
            {
                "fact_ids": ["F1"],
                "interpretation": "作業時間がまとまって観測されています",
                "proposal": "終了前に差分を確認する",
                "next_metric": "context_switches",
            }
        ],
        "actions": [
            {
                "fact_ids": ["F1"],
                "trigger": "セッション終了時",
                "action": "git status を見る",
                "estimated_minutes": 5,
                "pass": "context_switches <= 30",
                "fail": "context_switches >= 80",
                "mechanism": "終了儀式で切替を抑える",
                "falsifier": "切替が減らない",
            }
        ],
        "ai_review": [
            {
                "fact_ids": ["F5"],
                "text": "構造化ログに摩擦の代理指標が見えます",
            }
        ],
    }


def _evidence_with_cands(
    cands: list[tuple[str, str]] | None = None,
) -> AdviceEvidence:
    if cands is None:
        cands = [
            ("C1", "リトライ連鎖は 2件が観測されています。"),
            ("C2", "集中ブロックは 3件が観測されています。"),
            ("C3", "カテゴリ変更レートは 12回/時が観測されています。"),
        ]
    return AdviceEvidence(
        markdown="# facts\n- [F1] x\n- [F5] y\n",
        fact_ids=frozenset({"[F1]", "[F5]"}),
        ai_conversation_metrics_available=True,
        entertainment_observed=False,
        reader_summary="s",
        reader_notes=(),
        max_actions=3,
        previous_day_available=True,
        browser_sample_sufficient=True,
        insight_candidates=tuple(cands),
        input_metrics_available=True,
        structured_ai_metrics_available=True,
        site_metrics_available=True,
        metric_baselines={"context_switches": 40.0},
        metric_history_values={
            "context_switches": (50.0, 45.0, 40.0, 42.0, 38.0)
        },
    )


# ---------------------------------------------------------------------------
# §T1
# ---------------------------------------------------------------------------


def test_t1_1_candidates_style_and_cap():
    cands = build_insight_candidates(_rich_stats(), _hist(7), timezone=TZ)
    assert 1 <= len(cands) <= 8
    for c in cands:
        assert c["id"].startswith("C")
        assert any(ch.isdigit() for ch in c["text"])
        for b in _BANNED:
            assert b not in c["text"]


def test_t1_2_thin_day_zero_candidates_no_section():
    thin = {"day": DAY.isoformat(), "total_minutes": 10.0, "context_switches": 1}
    cands = build_insight_candidates(thin, [], timezone=TZ)
    # switch rate may still produce 1 line; make emptier
    empty = {"day": DAY.isoformat()}
    cands0 = build_insight_candidates(empty, [], timezone=TZ)
    assert cands0 == []
    ev = _evidence_with_cands([])
    data = _valid_advice_payload()
    # no insight section when no candidates
    md = render_advice_markdown(data, ev)
    assert "事実からの洞察" not in md


def test_t1_3_selection_renders_verbatim():
    ev = _evidence_with_cands()
    data = _valid_advice_payload()
    data["insight_selection"] = [
        {"candidate_id": "C1"},
        {"candidate_id": "C2", "connector": "一方で"},
    ]
    md = render_advice_markdown(data, ev)
    assert "## 🧠 事実からの洞察" in md
    assert "リトライ連鎖は 2件が観測されています。 [C1]" in md
    assert "一方で、集中ブロックは 3件が観測されています。 [C2]" in md


def test_t1_4_unknown_candidate_id_contract_error():
    ev = _evidence_with_cands()
    data = _valid_advice_payload()
    data["insight_selection"] = [{"candidate_id": "C99"}]
    errs = insight_selection_errors(data, ev)
    assert any("存在しない候補ID" in e for e in errs)


def test_t1_5_connector_digit_contract_error():
    ev = _evidence_with_cands()
    data = _valid_advice_payload()
    data["insight_selection"] = [
        {"candidate_id": "C1", "connector": "2倍で"},
    ]
    errs = insight_selection_errors(data, ev)
    assert any("connector" in e and "数値" in e for e in errs)


def test_t1_6_degrade_on_violation():
    ev = _evidence_with_cands()
    data = _valid_advice_payload()
    data["insight_selection"] = [{"candidate_id": "C99"}]
    # 全体 validate は insight で落とさない（既存必須は満たす）
    # metric baseline があるので pass は通る
    md = render_advice_markdown(data, ev)
    assert "## 🧠 事実からの洞察" in md
    # 上位2本
    assert "[C1]" in md and "[C2]" in md
    assert "C99" not in md


def test_t1_7_existing_contract_still_blocks_digits_in_interpretation():
    ev = _evidence_with_cands()
    data = _valid_advice_payload()
    data["proposals"][0]["interpretation"] = "稼働が120分でした"
    errs = validate_advice(data, ev)
    assert errs
    assert any("interpretation" in e or "数値" in e for e in errs)


# ---------------------------------------------------------------------------
# §T2
# ---------------------------------------------------------------------------


def _entry(
    eid: str,
    *,
    verdict: str | None = None,
    verdict_date: str | None = None,
    action: str | None = None,
    status: str = "proposed",
    decision: dict | None = None,
) -> MemoryEntry:
    return MemoryEntry(
        id=eid,
        date="2026-08-01",
        action=action
        or (
            "終了時→確認（目安5分）｜PASS: ai_retry_chains <= 0｜FAIL: ai_retry_chains >= 5"
        ),
        status=status,
        verdict=verdict,
        verdict_value=4.0 if verdict == "fail" else (0.0 if verdict == "pass" else None),
        verdict_date=verdict_date,
        verdict_stage="confirmed",
        decision=decision,
    )


def test_t2_8_morning_card_renders():
    entries = [
        _entry(
            "KZN-20260801-001",
            verdict="fail",
            verdict_date="2026-08-02",
        ),
        _entry("KZN-20260802-001", status="proposed"),
    ]
    body = build_morning_decision_section(entries, DAY)
    assert body is not None
    assert "昨日の確定判定" in body
    assert "KZN-20260801-001" in body
    assert "❌FAIL" in body
    assert "問い:" in body
    assert "- [ ] 採用" in body
    assert "- [ ] 見送り｜理由:" in body


def test_t2_9_no_section_when_empty():
    assert build_morning_decision_section([], DAY) is None
    # verdict も open 候補も無し
    done = [_entry("KZN-20260801-001", status="done", verdict="pass", verdict_date="2026-07-01")]
    # done は partition で落ちる想定 — 区間なし
    body = build_morning_decision_section(done, DAY)
    assert body is None or "問い" not in (body or "")


def test_t2_10_parse_and_ledger(tmp_path: Path):
    section = (
        "## ⚖ 今日の意思決定\n\n"
        "昨日の確定判定: KZN-20260801-001 ❌FAIL\n\n"
        "**問い: KZN-20260801-001 を今日も実行するか**\n"
        "- [ ] 採用（今日実行する）\n"
        "- [x] 見送り｜理由: 優先度が低い\n"
        "- [ ] 別案でいく｜内容: ＿＿\n"
    )
    choice = parse_decision_choice(section)
    assert choice == {"choice": "skip", "reason": "優先度が低い"}

    e = _entry("KZN-20260801-001", verdict="fail", verdict_date="2026-08-02")
    mem = tmp_path / "Kaizen" / "Memory"
    append_entries(mem, [e])
    append_entries(
        mem,
        [
            replace(
                e,
                decision={
                    "choice": "skip",
                    "reason": "優先度が低い",
                    "date": DAY.isoformat(),
                },
            )
        ],
    )
    loaded = {x.id: x for x in load_entries(mem)}
    assert loaded["KZN-20260801-001"].decision["choice"] == "skip"
    assert loaded["KZN-20260801-001"].status == "proposed"  # status 不変


def test_t2_11_blank_no_ledger():
    section = (
        "**問い: KZN-20260801-001 を今日も実行するか**\n"
        "- [ ] 採用（今日実行する）\n"
        "- [ ] 見送り｜理由: ＿＿\n"
        "- [ ] 別案でいく｜内容: ＿＿\n"
    )
    assert parse_decision_choice(section) is None


def test_t2_12_settlement_preserves_handwriting():
    morning = (
        "## ⚖ 今日の意思決定（1件・朝に確定）\n\n"
        "昨日の確定判定: KZN-20260801-001 ❌FAIL（ai_retry_chains 実測 4 / 目標 <= 0）\n\n"
        "**問い: KZN-20260801-001 を今日も実行するか**\n"
        "- [ ] 採用（今日実行する）\n"
        "- [x] 見送り｜理由: 今夜は別件優先\n"
        "- [ ] 別案でいく｜内容: ＿＿\n"
    )
    settlement = build_settlement_block(
        choice="skip",
        metric="ai_retry_chains",
        observed=1.0,
        median7=2.0,
    )
    once = recompose_decision_section(morning, settlement)
    # 朝パートはバイト単位で不変（部分文字列判定では改変を検出できない）
    assert strip_settlement(once).rstrip("\n") == morning.rstrip("\n")
    assert "### ⚖ 今日の決算" in once
    assert "因果は断定しません" in once
    # 冪等: 再合成
    twice = recompose_decision_section(strip_settlement(once), settlement)
    assert "今夜は別件優先" in twice
    assert twice.count("### ⚖ 今日の決算") == 1


def test_t2_13_old_jsonl_without_decision(tmp_path: Path):
    mem = tmp_path / "Kaizen" / "Memory"
    mem.mkdir(parents=True)
    path = mem / "suggestions.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "KZN-20260801-001",
                "date": "2026-08-01",
                "action": "x｜PASS: ai_retry_chains <= 0｜FAIL: 1",
                "status": "proposed",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = load_entries(mem)
    assert len(loaded) == 1
    assert loaded[0].decision is None


def test_t2_14_skip_filter_picks_next(tmp_path: Path):
    mem = tmp_path / "m"
    e1 = _entry("KZN-20260801-001", status="proposed")
    e2 = _entry("KZN-20260801-002", status="proposed")
    append_entries(mem, [e1, e2])
    # skip を2回追記
    for d in (DAY - timedelta(days=2), DAY - timedelta(days=1)):
        append_entries(
            mem,
            [
                replace(
                    e1,
                    decision={"choice": "skip", "reason": "x", "date": d.isoformat()},
                )
            ],
        )
    entries = load_entries(mem)
    # status 不変
    assert all(e.status == "proposed" for e in entries)
    picked = select_decision_question_entry(
        entries, DAY, memory_dir=mem
    )
    assert picked is not None
    assert picked.id == "KZN-20260801-002"


def test_decision_marker_in_section_order():
    assert SECTION_ORDER.index(RESUME_MARKER) < SECTION_ORDER.index(DECISION_MARKER)
    assert SECTION_ORDER.index(DECISION_MARKER) < SECTION_ORDER.index(GOAL_MARKER)


def test_build_advice_evidence_embeds_candidates():
    ev = build_advice_evidence(
        _rich_stats(),
        _hist(5),
        timezone=TZ,
        source_status="verified",
    )
    assert ev.insight_candidates
    assert "洞察候補" in ev.markdown
    assert "[C1]" in ev.markdown or "C1" in ev.markdown
