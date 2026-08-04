"""Graph Engineering: short journal insights and bounded action durations."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
import math
from pathlib import Path

import pytest

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.advice_format import render_advice_markdown, validate_advice
from kaizenlog.advisor import render_reader_advice
from kaizenlog.memory import MemoryEntry, assign_action_ids
from kaizenlog.digest import build_digest
from tests.test_advice_evidence import CURRENT, HISTORY


def _evidence():
    return build_advice_evidence(CURRENT, HISTORY)


def _advice_data(*, estimated_minutes=10):
    return {
        "plan_review": None,
        "proposals": [{
            "fact_ids": ["F3"],
            "interpretation": "開始条件を固定して集中枠の再現性を試す",
            "proposal": "始業時に集中枠を予定へ入れる",
            "next_metric": "集中ブロック回数",
        }],
        "actions": [{
            "fact_ids": ["F3"],
            "trigger": "始業の直後",
            "action": "集中枠を予定へ一件入れる",
            "estimated_minutes": estimated_minutes,
            "pass": "focus_blocks >= 2",
            "fail": "1回以下",
            "mechanism": "枠を先に置くと開始が遅れにくいと考える",
            "falsifier": "focus_blocks が前日より減った場合",
        }],
        "ai_review": [{
            "fact_ids": ["F5"],
            "text": "会話の往復数は測定不能なので品質の良否は断定しない",
        }],
    }


@pytest.mark.parametrize("value", [None, 4, 16, True, "10", 10.0])
def test_validate_rejects_missing_or_non_integer_5_to_15_estimated_minutes(value):
    data = _advice_data(estimated_minutes=value)
    if value is None:
        del data["actions"][0]["estimated_minutes"]

    errors = validate_advice(data, _evidence())

    assert any(
        "actions[1].estimated_minutes" in error
        and "5〜15" in error
        and "整数" in error
        for error in errors
    )


@pytest.mark.parametrize("value", [5, 15])
def test_validate_accepts_integer_estimated_minutes_at_bounds(value):
    assert validate_advice(_advice_data(estimated_minutes=value), _evidence()) == []


def test_rendered_duration_is_once_and_is_preserved_in_memory_action():
    markdown = render_advice_markdown(_advice_data(estimated_minutes=10), _evidence())

    assert markdown.count("（目安10分）") == 1
    with_ids, entries = assign_action_ids(markdown, date(2026, 8, 2), [])
    assert "（目安10分）" in with_ids
    assert entries[0].action.count("（目安10分）") == 1


def test_action_shows_at_most_three_safe_reader_evidence_labels():
    data = _advice_data(estimated_minutes=10)
    data["proposals"][0]["fact_ids"] = ["F1", "F3", "F5", "F8"]
    data["actions"][0]["fact_ids"] = ["F1", "F3", "F5", "F8"]

    markdown = render_advice_markdown(data, _evidence())

    assert (
        "    - 根拠: 稼働・切替の確定統計 / 入力watcher実測 / "
        "構造化AIテレメトリ"
    ) in markdown
    assert "通常範囲との比較" not in markdown
    assert "[F1]" not in markdown


def test_reader_insight_hides_internal_candidate_ids():
    evidence = replace(
        _evidence(),
        insight_candidates=(
            ("C1", "リトライ連鎖は2件が観測されています。"),
            ("C2", "集中ブロックは3件が観測されています。"),
        ),
    )
    data = _advice_data(estimated_minutes=10)
    data["insight_selection"] = [
        {"candidate_id": "C1"},
        {"candidate_id": "C2", "connector": "一方で"},
    ]

    markdown = render_advice_markdown(data, evidence)

    assert "リトライ連鎖は2件が観測されています。" in markdown
    assert "一方で、集中ブロックは3件が観測されています。" in markdown
    assert "[C1]" not in markdown
    assert "[C2]" not in markdown


def test_reader_advice_preserves_safe_evidence_labels_to_final_journal():
    internal = render_advice_markdown(_advice_data(estimated_minutes=10), _evidence())

    reader = render_reader_advice(internal, _evidence())

    assert "    - 根拠: 入力watcher実測" in reader
    assert "[F3]" not in reader


def test_digest_exposes_direct_waste_ai_measurement_and_tomorrow_focus():
    today = date(2026, 8, 2)
    digest = build_digest(
        {
            "source_status": "verified",
            "total_minutes": 180,
            "by_category": {"AI作業": 60, "エンタメ": 35, "ブラウジング": 80},
            "ai": {},
        },
        [MemoryEntry("KZN-20260802-001", today.isoformat(), "始業→集中する｜PASS: focus_blocks >= 2｜FAIL: 1")],
        today=today,
        redactor=lambda text: text,
    )

    assert digest is not None
    assert "ムダ上位: エンタメ 35m（直接計測）" in digest
    assert "AI作業の質: 測定不能（構造化AIログなし）" in digest
    assert "明日のフォーカス: KZN-20260802-001 始業 → 集中する" in digest
    assert "｜PASS:" not in digest
    assert "ブラウジング" not in digest


def test_digest_labels_prior_day_proposal_as_today_focus():
    digest = build_digest(
        {"source_status": "verified", "total_minutes": 10, "by_category": {"AI作業": 0}},
        [MemoryEntry("KZN-20260801-001", "2026-08-01", "朝→集中する｜PASS: focus_blocks >= 2｜FAIL: 1")],
        today=date(2026, 8, 2),
        redactor=lambda text: text,
    )

    assert digest is not None
    assert "今日の1手: KZN-20260801-001 朝 → 集中する" in digest


def test_digest_friction_is_redacted_and_calls_it_ai_quality_proxy():
    digest = build_digest(
        {
            "source_status": "verified",
            "total_minutes": 60,
            "by_category": {"AI作業": 60},
            "ai": {"session_digests": [{
                "title": "secret project title",
                "project": "journal",
                "source": "Codex",
                "tool_errors": 2,
                "tools_total": 4,
                "edits": 1,
            }]},
        },
        [],
        today=date(2026, 8, 2),
        redactor=lambda text: "[REDACTED]" if "secret" in text else text,
    )

    assert digest is not None
    assert "AI作業の質:" in digest
    assert "今日いちばんの摩擦" in digest
    assert "secret" not in digest


def test_digest_reports_no_major_friction_when_structured_sessions_score_zero():
    digest = build_digest(
        {
            "source_status": "verified",
            "total_minutes": 60,
            "by_category": {"AI作業": 60},
            "ai": {"session_digests": [{"tool_errors": 0, "interruptions": 0}]},
        },
        [],
        today=date(2026, 8, 2),
        redactor=lambda text: text,
    )

    assert digest is not None
    assert "AI作業の質: 大きな摩擦なし（摩擦の代理指標）" in digest


@pytest.mark.parametrize("entertainment", [True, math.nan, math.inf, -math.inf, -1])
def test_digest_fails_closed_for_invalid_entertainment_minutes(entertainment):
    digest = build_digest(
        {
            "source_status": "verified",
            "total_minutes": 60,
            "by_category": {"AI作業": 0, "エンタメ": entertainment},
        },
        [],
        today=date(2026, 8, 2),
    )

    assert digest is not None
    assert "ムダ上位: 測定不能（エンタメカテゴリ値が不正）" in digest


def test_digest_focus_ignores_future_proposals():
    today = date(2026, 8, 2)
    stats = {"source_status": "verified", "total_minutes": 60}
    future = MemoryEntry("KZN-20260803-001", "2026-08-03", "未来→実行する")
    today_entry = MemoryEntry("KZN-20260802-001", today.isoformat(), "今日→実行する")

    future_only = build_digest(stats, [future], today=today, redactor=lambda text: text)
    current_and_future = build_digest(
        stats, [future, today_entry], today=today, redactor=lambda text: text
    )

    assert future_only is not None and "フォーカス:" not in future_only and "今日の1手:" not in future_only
    assert current_and_future is not None
    assert "明日のフォーカス: KZN-20260802-001 今日 → 実行する" in current_and_future
    assert "KZN-20260803-001" not in current_and_future


def test_daily_prompts_show_complete_validator_action_keys():
    prompt_dir = Path(__file__).parents[1] / "src" / "kaizenlog" / "prompts"
    required = ("estimated_minutes", "mechanism", "falsifier")

    for name in ("daily_advisor.md", "privacy_safe.md"):
        prompt = (prompt_dir / name).read_text(encoding="utf-8")
        assert all(f'"{field}"' in prompt for field in required), name


def test_improvement_graph_is_a_valid_closed_provenance_contract():
    path = Path(__file__).parents[1] / ".kaizenlog" / "improvement_graph.json"
    graph = json.loads(path.read_text(encoding="utf-8"))

    assert isinstance(graph, dict)
    node_types = set(graph["allowed_node_types"])
    edge_types = set(graph["allowed_edge_types"])
    nodes = graph["nodes"]
    edges = graph["edges"]
    assert nodes
    assert edges
    assert len({node["id"] for node in nodes}) == len(nodes)
    assert len({edge["id"] for edge in edges}) == len(edges)
    node_ids = {node["id"] for node in nodes}
    for item in [*nodes, *edges]:
        provenance = item["provenance"]
        assert provenance["sources"]
        assert provenance["step"]
        assert provenance["timestamp"]
    assert all(node["type"] in node_types for node in nodes)
    assert all(edge["type"] in edge_types for edge in edges)
    assert all(edge["source"] in node_ids and edge["target"] in node_ids for edge in edges)
    triples = {(edge["source"], edge["type"], edge["target"]) for edge in edges}
    assert {
        ("D-PRACTICAL-LOOP-001", "improves", "G-JOURNAL-30SEC-001"),
        ("D-PRACTICAL-LOOP-001", "improves", "G-ADVICE-DURATION-001"),
        ("C-PRACTICAL-LOOP-001", "evaluated-as", "T-FULL-REGRESSION-003"),
        ("E-FIXTURE-OUTPUT-001", "supports", "G-JOURNAL-30SEC-001"),
        ("E-FIXTURE-OUTPUT-001", "supports", "G-ADVICE-DURATION-001"),
    } <= triples
    loop = graph["loop"]
    assert loop["critique_revise_used"] <= loop["critique_revise_limit"]
