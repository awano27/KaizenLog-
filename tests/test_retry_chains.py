"""リトライ連鎖検出と stats / metric / patterns 配線。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.aiwork import UserPrompt, detect_retry_chains, render_aiwork_markdown
from kaizenlog.experiments import compute_metric, metric_from_stats
from kaizenlog.patterns import detect_ai_friction
from kaizenlog.report import DailySummary
from kaizenlog.stats import build_stats, load_stats, write_stats


TZ = timezone.utc
BASE = datetime(2026, 7, 20, 10, 0, tzinfo=TZ)


def _p(text: str, minutes: float = 0, project: str = "proj") -> UserPrompt:
    return UserPrompt(
        timestamp=BASE + timedelta(minutes=minutes),
        project=project,
        text=text,
    )


def test_detect_retry_chains_window_boundary():
    # 30分ちょうどは連結
    prompts = [
        _p("同じ依頼をしてください", 0),
        _p("同じ依頼をしてください", 30),
    ]
    chains = detect_retry_chains(prompts, window_minutes=30)
    assert len(chains) == 1 and chains[0].length == 2

    # 31分は分断
    prompts31 = [
        _p("同じ依頼をしてください", 0),
        _p("同じ依頼をしてください", 31),
    ]
    assert detect_retry_chains(prompts31, window_minutes=30) == []


def test_detect_retry_chains_similarity_and_project():
    # 低類似度は連結しない
    low = [_p("完全に別の依頼A", 0), _p("zzzz totally different request", 5)]
    assert detect_retry_chains(low, similarity=0.85) == []

    # 別プロジェクトは連結しない
    cross = [
        _p("同じ依頼をしてください", 0, "a"),
        _p("同じ依頼をしてください", 5, "b"),
    ]
    assert detect_retry_chains(cross) == []


def test_detect_retry_chains_normalize_and_triple():
    # パス・数値差を吸収して連結
    prompts = [
        _p("C:\\develop\\app\\main.py のバグを直して", 0),
        _p("C:\\develop\\app\\util.py のバグを直して", 10),
        _p("/home/u/app/main.py のバグを直して", 20),
    ]
    chains = detect_retry_chains(prompts)
    assert len(chains) == 1
    assert chains[0].length == 3

    # 単発は返らない
    assert detect_retry_chains([_p("一度だけ")]) == []


def test_stats_roundtrip_retry_chains(tmp_path):
    summary = DailySummary(
        day=date(2026, 7, 20),
        total_minutes=10.0,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
    )
    chains = detect_retry_chains([
        _p("fix it please", 0, "ai-news"),
        _p("fix it please", 5, "ai-news"),
    ])
    path = write_stats(
        tmp_path, date(2026, 7, 20), summary, [], retry_chains=chains
    )
    loaded = load_stats(tmp_path, days=1, end_day=date(2026, 7, 20))[0]
    assert loaded["ai"]["retry_chains"] == 1
    assert loaded["ai"]["retry_prompts"] == 2
    assert loaded["ai"]["projects"]["ai-news"]["retry_chains"] == 1
    # 旧形式（フィールド無し）は metric_from_stats が None
    assert metric_from_stats("ai_retry_chains", {"ai": {"sessions": 1}}) is None
    assert metric_from_stats("ai_retry_chains", loaded) == 1.0
    assert path.is_file()


def test_compute_metric_ai_retry_chains():
    summary = DailySummary(
        day=date(2026, 7, 20),
        total_minutes=1.0,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
    )
    assert compute_metric("ai_retry_chains", summary, [], retry_chains=None) is None
    assert compute_metric("ai_retry_chains", summary, [], retry_chains=3) == 3.0


def test_evidence_f5_includes_retry_when_present():
    base = {
        "version": 1,
        "day": "2026-07-21",
        "total_minutes": 120.0,
        "context_switches": 10,
        "ai_activity_blocks": 1,
        "by_category": {"開発": 100.0},
        "by_app": {},
        "by_site": {},
        "blocks": [],
        "ai": {
            "sessions": 2,
            "fragmented": 1,
            "tool_errors": 0,
            "interruptions": 0,
            "projects": {},
        },
    }
    without = build_advice_evidence(base).markdown
    assert "リトライ連鎖" not in without

    base["ai"]["retry_chains"] = 4
    with_retry = build_advice_evidence(base).markdown
    assert "リトライ連鎖 4回" in with_retry


def test_patterns_retry_friction_and_not_fragmented_alone():
    days = [date(2026, 7, d) for d in range(1, 5)]

    def day_stats(d, **proj):
        return {
            "day": d.isoformat(),
            "by_app": {},
            "blocks": [],
            "ai": {
                "sessions": 2,
                "fragmented": 1,
                "tool_errors": 0,
                "interruptions": 0,
                "retry_chains": proj.get("day_retry", 0),
                "projects": {
                    "vault": {
                        "sessions": 2,
                        "turns": 3,
                        "errors": 0,
                        "fragmented": 2,
                        "retry_chains": proj.get("retry", 0),
                    }
                },
            },
        }

    alone = [day_stats(d, retry=0) for d in days]
    assert detect_ai_friction(alone) == []

    with_retry = [day_stats(d, retry=1, day_retry=1) for d in days]
    out = detect_ai_friction(with_retry)
    assert len(out) == 1
    assert "リトライ連鎖計4回" in out[0].evidence


def test_render_aiwork_includes_retry_count():
    from kaizenlog.aiwork import AISession

    start = BASE
    sessions = [
        AISession(session_id="a", project="p", start=start, end=start, user_turns=3),
    ]
    md = render_aiwork_markdown(sessions, ZoneInfo("UTC"), retry_chain_count=2)
    assert "リトライ連鎖: 2回" in md
    assert "2往復以下" in md
