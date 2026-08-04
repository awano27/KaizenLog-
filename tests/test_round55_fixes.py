"""第55弾: 第54弾レビュー残件（契約統合・スキーマ・冪等・eval）。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.advice_evidence import AdviceEvidence, build_advice_evidence
from kaizenlog.advice_format import (
    insight_selection_errors,
    render_advice_markdown,
    validate_advice,
)
from kaizenlog.advisor import (
    AdviceContractError,
    _contract_repair_prompt,
    _run_daily_pipeline,
    build_prompt,
)
from kaizenlog.cli import _write_decision_settlement
from kaizenlog.config import Config, LLMConfig
from kaizenlog.decision import skip_counts_last_days
from kaizenlog.evalharness import load_case, repo_eval_samples_dir
from kaizenlog.memory import MemoryEntry, append_entries, load_entries
from kaizenlog.vault import DECISION_MARKER, DailyNoteStore, extract_section

DAY = date(2026, 8, 3)
TZ = ZoneInfo("Asia/Tokyo")


def _cands():
    return (
        ("C1", "リトライ連鎖は 2件が観測されています。"),
        ("C2", "集中ブロックは 3件が観測されています。"),
        ("C3", "カテゴリ変更レートは 12回/時が観測されています。"),
    )


def _ev() -> AdviceEvidence:
    return AdviceEvidence(
        markdown="# facts\n- [F1] x\n- [F5] y\n## 洞察候補\n- [C1] a\n",
        fact_ids=frozenset({"[F1]", "[F5]"}),
        ai_conversation_metrics_available=True,
        entertainment_observed=False,
        reader_summary="s",
        reader_notes=(),
        max_actions=3,
        previous_day_available=True,
        browser_sample_sufficient=True,
        insight_candidates=_cands(),
        input_metrics_available=True,
        structured_ai_metrics_available=True,
        site_metrics_available=True,
        metric_baselines={"context_switches": 40.0},
        metric_history_values={
            "context_switches": (50.0, 45.0, 40.0, 42.0, 38.0)
        },
    )


def _valid_data(**kw) -> dict:
    data = {
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
            {"fact_ids": ["F5"], "text": "構造化ログに摩擦の代理指標が見えます"}
        ],
    }
    data.update(kw)
    return data


# ---------------------------------------------------------------------------
# §U1 validate 経由
# ---------------------------------------------------------------------------


def test_u1_unknown_id_via_validate():
    data = _valid_data(insight_selection=[{"candidate_id": "C99"}])
    errs = validate_advice(data, _ev())
    assert errs
    assert any("存在しない候補ID" in e for e in errs)
    # 直呼びも残す
    assert insight_selection_errors(data, _ev())


def test_u1_digit_connector_via_validate():
    data = _valid_data(
        insight_selection=[{"candidate_id": "C1", "connector": "2倍で"}]
    )
    errs = validate_advice(data, _ev())
    assert any("connector" in e and "数値" in e for e in errs)


def test_u1_three_selections_via_validate():
    data = _valid_data(
        insight_selection=[
            {"candidate_id": "C1"},
            {"candidate_id": "C2"},
            {"candidate_id": "C3"},
        ]
    )
    errs = validate_advice(data, _ev())
    assert any("最大2件" in e for e in errs)


def test_u1_repair_prompt_includes_insight_violation():
    errs = ["insight_selection[1] が存在しない候補IDを参照しています"]
    prompt = _contract_repair_prompt(_ev(), '{"proposals":[]}', errs)
    assert "insight_selection" in prompt
    assert "存在しない候補ID" in prompt


def test_u1_degrade_after_failed_repair(monkeypatch):
    """修復後も insight 違反のみ → 縮退で advise 成立。"""
    cfg = LLMConfig(backend="none", system_prompt="daily_advisor")
    good = _valid_data(insight_selection=[{"candidate_id": "C1"}])
    bad = _valid_data(insight_selection=[{"candidate_id": "C99"}])
    replies = iter(
        [
            json.dumps(bad, ensure_ascii=False),
            json.dumps(bad, ensure_ascii=False),  # 修復も失敗
        ]
    )

    def gen(_cfg, _sys, _user):
        return next(replies)

    md, report = _run_daily_pipeline(
        cfg, "system", "user", _ev(), generate_fn=gen
    )
    assert report.final_ok is True
    assert md is not None
    assert "事実からの洞察" in md
    assert "リトライ連鎖は 2件が観測されています。" in md
    assert "集中ブロックは 3件が観測されています。" in md
    assert "[C1]" not in md and "[C2]" not in md
    assert "C99" not in md


# ---------------------------------------------------------------------------
# §U2 schema in prompt
# ---------------------------------------------------------------------------


def test_u2_build_prompt_has_insight_schema_when_candidates():
    prompt = build_prompt("log", [], evidence=_ev())
    assert "insight_selection" in prompt
    assert "candidate_id" in prompt
    assert "最大2" in prompt or "最大2件" in prompt


def test_u2_build_prompt_omits_schema_without_candidates():
    ev = replace(_ev(), insight_candidates=())
    # frozen dataclass - use _evidence style
    from dataclasses import fields

    ev0 = AdviceEvidence(
        markdown="# facts\n- [F1] x\n",
        fact_ids=frozenset({"[F1]", "[F5]"}),
        ai_conversation_metrics_available=False,
        entertainment_observed=False,
        reader_summary="s",
        reader_notes=(),
        max_actions=1,
        previous_day_available=False,
        browser_sample_sufficient=False,
        insight_candidates=(),
    )
    prompt = build_prompt("log", [], evidence=ev0)
    assert "insight_selection 出力スキーマ" not in prompt


# ---------------------------------------------------------------------------
# §U3 eval case4
# ---------------------------------------------------------------------------


def test_u3_case4_loads_and_detects_violation():
    samples = repo_eval_samples_dir()
    assert samples is not None
    path = samples / "case4_insight_selection.json"
    assert path.is_file()
    case = load_case(path)
    ev = build_advice_evidence(
        case.current_stats,
        case.prior_stats,
        timezone=TZ,
        source_status=case.source_status,
        known_categories=case.known_categories,
    )
    assert ev.insight_candidates
    base = {
        "plan_review": None,
        "proposals": [
            {
                "fact_ids": list(ev.fact_ids)[:1] and ["F1"] or ["F1"],
                "interpretation": "作業のまとまりが観測されています",
                "proposal": "終了前に確認する",
                "next_metric": "context_switches",
            }
        ],
        "actions": [
            {
                "fact_ids": ["F1"],
                "trigger": "終了時",
                "action": "確認する",
                "estimated_minutes": 5,
                "pass": "context_switches <= 30",
                "fail": "context_switches >= 80",
                "mechanism": "確認で切替を抑える",
                "falsifier": "切替が増える",
            }
        ],
        "ai_review": [
            {"fact_ids": ["F5"], "text": "構造化ログがある日です"}
        ],
    }
    # fact ids may vary - use real from evidence
    fids = sorted(ev.fact_ids)
    f1 = fids[0].strip("[]") if fids else "F1"
    f5 = "F5" if "[F5]" in ev.fact_ids else f1
    base["proposals"][0]["fact_ids"] = [f1]
    base["actions"][0]["fact_ids"] = [f1]
    base["ai_review"][0]["fact_ids"] = [f5]

    # §U3: 違反ペイロードと期待値は case4 側が持つ（差し替えに追随する）
    spec = json.loads(path.read_text(encoding="utf-8"))["insight_eval"]
    assert len(ev.insight_candidates) >= 2
    for viol in spec["violations"]:
        bad = deepcopy(base)
        bad["insight_selection"] = viol["payload"]
        errs = validate_advice(bad, ev)
        assert any(viol["expect"] in e for e in errs), viol["expect"]
    ok = deepcopy(base)
    ok["insight_selection"] = spec["valid"]
    assert not [
        e for e in validate_advice(ok, ev) if "insight_selection" in e
    ]


def test_u3_case4_degrade_renders_top2():
    samples = repo_eval_samples_dir()
    path = samples / "case4_insight_selection.json"
    case = load_case(path)
    ev = build_advice_evidence(
        case.current_stats,
        case.prior_stats,
        timezone=TZ,
        source_status=case.source_status,
        known_categories=case.known_categories,
    )
    fids = sorted(ev.fact_ids)
    f1 = fids[0].strip("[]")
    f5 = "F5" if "[F5]" in ev.fact_ids else f1
    data = {
        "plan_review": None,
        "proposals": [
            {
                "fact_ids": [f1],
                "interpretation": "作業のまとまりが観測されています",
                "proposal": "終了前に確認する",
                "next_metric": "context_switches",
            }
        ],
        "actions": [
            {
                "fact_ids": [f1],
                "trigger": "終了時",
                "action": "確認する",
                "estimated_minutes": 5,
                "pass": "context_switches <= 30",
                "fail": "context_switches >= 80",
                "mechanism": "確認で切替を抑える",
                "falsifier": "切替が増える",
            }
        ],
        "ai_review": [{"fact_ids": [f5], "text": "構造化ログがある日です"}],
        "insight_selection": [{"candidate_id": "C99"}],
    }
    # may need baseline for pass challenge - if fails, skip via soft
    md = render_advice_markdown(data, ev)
    assert "事実からの洞察" in md
    top_n = json.loads(path.read_text(encoding="utf-8"))["insight_eval"]["degrade_top_n"]
    for cid, text in ev.insight_candidates[:top_n]:
        assert text in md
        assert f"[{cid}]" not in md
    assert "C99" not in md


# ---------------------------------------------------------------------------
# §U4 idempotent decision append + unique skip count
# ---------------------------------------------------------------------------


def test_u4_settlement_append_once(tmp_path: Path):
    mem = tmp_path / "Kaizen" / "Memory"
    notes = tmp_path / "01 Daily Notes"
    notes.mkdir(parents=True)
    e = MemoryEntry(
        id="KZN-20260801-001",
        date="2026-08-01",
        action="x｜PASS: ai_retry_chains <= 0｜FAIL: ai_retry_chains >= 5",
        status="proposed",
        verdict="fail",
        verdict_value=4.0,
        verdict_date="2026-08-02",
        verdict_stage="confirmed",
    )
    append_entries(mem, [e])
    section = (
        "## ⚖ 今日の意思決定\n\n"
        "**問い: KZN-20260801-001 を今日も実行するか**\n"
        "- [x] 見送り｜理由: 一度だけ\n"
        "- [ ] 採用\n"
    )
    store = DailyNoteStore(notes)
    store.write_section(DAY, DECISION_MARKER, section)
    cfg = Config(vault_dir=tmp_path, timezone="Asia/Tokyo")
    stats = {"day": DAY.isoformat(), "ai": {"retry_chains": 1}}
    for _ in range(3):
        _write_decision_settlement(cfg, store, DAY, stats)
    # raw jsonl lines with decision
    path = mem / "suggestions.jsonl"
    lines = [
        json.loads(ln)
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    dec_lines = [ln for ln in lines if isinstance(ln.get("decision"), dict)]
    assert len(dec_lines) == 1
    counts = skip_counts_last_days([], today=DAY, days=7, memory_dir=mem)
    assert counts.get("KZN-20260801-001") == 1


def test_u4_skip_count_dedupes_duplicate_rows(tmp_path: Path):
    mem = tmp_path / "m"
    mem.mkdir(parents=True)
    path = mem / "suggestions.jsonl"
    row = {
        "id": "KZN-20260801-001",
        "date": "2026-08-01",
        "action": "x",
        "status": "proposed",
        "decision": {
            "choice": "skip",
            "reason": "r",
            "date": DAY.isoformat(),
        },
    }
    # 意図的に同一 decision を3行
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for _ in range(3)) + "\n",
        encoding="utf-8",
    )
    counts = skip_counts_last_days([], today=DAY, days=7, memory_dir=mem)
    assert counts.get("KZN-20260801-001") == 1


# ---------------------------------------------------------------------------
# §U5 byte-equal morning part
# ---------------------------------------------------------------------------


def test_u5_settlement_preserves_morning_bytes():
    from kaizenlog.decision import (
        build_settlement_block,
        recompose_decision_section,
        strip_settlement,
    )

    morning = (
        "## ⚖ 今日の意思決定（1件・朝に確定）\n\n"
        "昨日の確定判定: KZN-20260801-001 ❌FAIL\n\n"
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
    assert strip_settlement(once) == strip_settlement(morning)
    # 再合成後も朝パート不変
    twice = recompose_decision_section(strip_settlement(once), settlement)
    assert strip_settlement(twice) == strip_settlement(morning)
    assert "今夜は別件優先" in strip_settlement(twice)
