import hashlib
from datetime import date, datetime, timedelta, timezone

from kaizenlog.classifier import Classifier
from kaizenlog.collector import ActivityEvent
from kaizenlog.config import DEFAULT_RULES
from kaizenlog.patterns import (
    detect_ai_friction,
    detect_routines,
    detect_time_sinks,
    render_patterns_markdown,
)
from kaizenlog.report import summarize
from kaizenlog.stats import activity_fingerprint, build_stats, load_stats, write_stats

TZ = timezone.utc
BASE = date(2026, 7, 1)


def _day_stats(day, apps=None, blocks=None, ai_projects=None):
    return {
        "version": 1,
        "day": day.isoformat(),
        "total_minutes": 300.0,
        "context_switches": 10,
        "by_category": {},
        "by_app": apps or {},
        "blocks": blocks or [],
        "ai": {"sessions": 0, "fragmented": 0, "tool_errors": 0,
               "interruptions": 0, "projects": ai_projects or {}},
    }


def _block(day, hour, app, minutes=30.0, title=""):
    start = datetime(day.year, day.month, day.day, hour, tzinfo=TZ)
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
        "category": "ブラウジング", "app": app, "minutes": minutes, "title": title,
    }


def _days(n):
    return [BASE + timedelta(days=i) for i in range(n)]


def test_time_sink_detected_when_recurring():
    stats = [_day_stats(d, apps={"chrome.exe": 45.0}) for d in _days(6)]
    out = detect_time_sinks(stats)
    assert len(out) == 1
    assert "chrome.exe" in out[0].title
    assert "6日中6日" in out[0].evidence


def test_time_sink_not_detected_when_sporadic():
    stats = [_day_stats(d, apps={"chrome.exe": 45.0 if i < 2 else 5.0})
             for i, d in enumerate(_days(6))]
    assert detect_time_sinks(stats) == []


def test_routine_detected_same_hour():
    stats = [
        _day_stats(d, blocks=[_block(d, 9, "chrome.exe", 25.0, "AIニュースまとめ")])
        for d in _days(5)
    ]
    out = detect_routines(stats)
    assert len(out) == 1
    # 報告される時刻はUTCではなくローカル時刻（JST環境ならUTC 9時 = 18時台）
    local_hour = datetime(2026, 7, 1, 9, tzinfo=TZ).astimezone().hour
    assert f"{local_hour}時台" in out[0].title
    assert "AIニュースまとめ" in out[0].title


def test_routine_not_detected_different_hours():
    stats = [
        _day_stats(d, blocks=[_block(d, 9 + i, "chrome.exe", 25.0)])
        for i, d in enumerate(_days(5))
    ]
    assert detect_routines(stats) == []


def test_ai_friction_fragmentation_alone_is_not_friction():
    # 細切れ（2往復以下）単独は中立。摩擦シグナルなしでは検出しない。
    stats = [
        _day_stats(d, ai_projects={"ai-news": {"sessions": 3, "turns": 4,
                                               "errors": 0, "fragmented": 2,
                                               "retry_chains": 0}})
        for d in _days(4)
    ]
    assert detect_ai_friction(stats) == []


def test_ai_friction_fragmentation_with_errors():
    stats = [
        _day_stats(d, ai_projects={"ai-news": {"sessions": 3, "turns": 4,
                                               "errors": 1, "fragmented": 2,
                                               "retry_chains": 0}})
        for d in _days(4)
    ]
    out = detect_ai_friction(stats)
    assert len(out) == 1
    assert "ai-news" in out[0].title
    assert "摩擦を伴う短セッション" in out[0].evidence


def test_ai_friction_interruptions_not_cross_project():
    """他PJの中断が fragmented だけのPJを摩擦にしない（F6）。"""
    days = _days(4)
    stats = []
    for d in days:
        stats.append({
            "day": d.isoformat(),
            "by_app": {},
            "blocks": [],
            "ai": {
                "sessions": 3,
                "fragmented": 2,
                "tool_errors": 0,
                "interruptions": 1,  # 日合算（旧形式の罠）
                "projects": {
                    "proj-a": {
                        "sessions": 1, "turns": 2, "errors": 0,
                        "fragmented": 0, "retry_chains": 0, "interruptions": 1,
                    },
                    "proj-b": {
                        "sessions": 2, "turns": 2, "errors": 0,
                        "fragmented": 2, "retry_chains": 0, "interruptions": 0,
                    },
                },
            },
        })
    out = detect_ai_friction(stats)
    titles = " ".join(o.title for o in out)
    assert "proj-b" not in titles
    # 旧形式: interruptions キー無し → 0 扱い
    old = [
        _day_stats(d, ai_projects={
            "legacy": {"sessions": 2, "turns": 2, "errors": 0, "fragmented": 2},
        })
        for d in days
    ]
    # 日合算 interruptions があっても projects に無い
    for s in old:
        s["ai"]["interruptions"] = 5
    assert detect_ai_friction(old) == []


def test_ai_friction_by_errors():
    stats = [
        _day_stats(d, ai_projects={"vault": {"sessions": 1, "turns": 5,
                                             "errors": 3, "fragmented": 0}})
        for d in _days(3)
    ]
    out = detect_ai_friction(stats)
    assert len(out) == 1
    assert "エラー計9回" in out[0].evidence


def test_render_insufficient_data():
    md = render_patterns_markdown([_day_stats(BASE)])
    assert "データが不足" in md


def test_duplicate_day_stats_counted_once():
    # 同じ日のstatsが複数渡っても「3日分」と誤検出しない
    stats = [_day_stats(BASE, apps={"chrome.exe": 45.0}) for _ in range(3)]
    assert detect_time_sinks(stats) == []
    assert "1日分のデータ" in render_patterns_markdown(stats)


def test_render_with_candidates():
    stats = [_day_stats(d, apps={"YouTube.exe": 60.0},
                        blocks=[_block(d, 20, "YouTube.exe", 60.0)])
             for d in _days(5)]
    md = render_patterns_markdown(stats)
    assert "時間泥棒" in md
    assert "定時ルーチン" in md
    assert "YouTube.exe" in md


def test_stats_roundtrip(tmp_path):
    start = datetime(2026, 7, 5, 9, tzinfo=TZ)
    events = [ActivityEvent(start=start, end=start + timedelta(minutes=30),
                            app="Code.exe", title="main.py")]
    classified = Classifier(DEFAULT_RULES).classify_all(events)
    summary = summarize(date(2026, 7, 5), classified)

    activity_md = "## 📊 Activity Log\n\nexample"
    write_stats(tmp_path, date(2026, 7, 5), summary, [], activity_md=activity_md)
    loaded = load_stats(tmp_path, days=7, end_day=date(2026, 7, 8))
    assert len(loaded) == 1
    s = loaded[0]
    assert s["day"] == "2026-07-05"
    assert s["by_app"]["Code.exe"] == 30.0
    assert s["blocks"][0]["title"] == "main.py"
    assert s["ai_activity_blocks"] == 0
    assert s["activity_sha256"] == activity_fingerprint(activity_md)
    assert s["ai"]["sessions"] == 0


def test_activity_fingerprint_is_newline_style_independent():
    activity_lf = "## 📊 Activity Log\n\n**合計**: 10m\n- Code.exe"
    expected = hashlib.sha256(activity_lf.encode("utf-8")).hexdigest()

    assert activity_fingerprint(activity_lf) == expected
    assert activity_fingerprint(activity_lf.replace("\n", "\r\n")) == expected
    assert activity_fingerprint(activity_lf.replace("\n", "\r")) == expected


def test_load_stats_skips_missing_and_broken(tmp_path):
    (tmp_path / "2026-07-05.json").write_text("{broken", encoding="utf-8")
    assert load_stats(tmp_path, days=7, end_day=date(2026, 7, 8)) == []


def test_build_stats_aggregates_ai_projects():
    from kaizenlog.aiwork import AISession
    start = datetime(2026, 7, 5, 9, tzinfo=TZ)
    sessions = [
        AISession(session_id="a", project="ai-news", start=start, end=start,
                  user_turns=1, tool_errors=2),
        AISession(session_id="b", project="ai-news", start=start, end=start,
                  user_turns=5),
    ]
    classified = Classifier(DEFAULT_RULES).classify_all([])
    summary = summarize(date(2026, 7, 5), classified)
    s = build_stats(date(2026, 7, 5), summary, sessions)
    assert s["ai_activity_blocks"] == summary.ai_activity_blocks
    assert s["ai"]["projects"]["ai-news"] == {
        "sessions": 2, "turns": 6, "errors": 2, "fragmented": 1,
        "retry_chains": 0, "interruptions": 0}
