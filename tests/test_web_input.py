"""aw-watcher-web（URL粒度）と aw-watcher-input（集中ブロック）対応のテスト。"""

from datetime import date, datetime, timedelta, timezone

from kaizenlog.classifier import Classifier
from kaizenlog.collector import ActivityEvent, enrich_with_web
from kaizenlog.config import DEFAULT_RULES
from kaizenlog.experiments import compute_metric
from kaizenlog.focus import InputStats, compute_input_stats
from kaizenlog.report import summarize

T0 = datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc)


def _min(n):
    return T0 + timedelta(minutes=n)


def _raw(start_min, duration_min, data):
    return {
        "timestamp": _min(start_min).isoformat(),
        "duration": duration_min * 60,
        "data": data,
    }


# ---- aw-watcher-web: タブ情報の合成 ----

def test_enrich_splits_browser_event_by_tabs():
    events = [ActivityEvent(_min(0), _min(30), "chrome.exe", "Google Chrome")]
    web = [
        _raw(0, 10, {"url": "https://www.youtube.com/watch?v=x", "title": "動画"}),
        _raw(10, 20, {"url": "https://github.com/awano27/KaizenLog-", "title": "GitHub"}),
    ]
    out = enrich_with_web(events, web)
    assert len(out) == 2
    assert out[0].domain == "youtube.com"  # www. は除去される
    assert out[0].title == "動画"
    assert out[1].domain == "github.com"
    assert (out[1].end - out[0].start) == timedelta(minutes=30)


def test_enrich_keeps_uncovered_time_as_original():
    events = [ActivityEvent(_min(0), _min(30), "chrome.exe", "Google Chrome")]
    web = [_raw(10, 10, {"url": "https://example.com/", "title": "Example"})]
    out = enrich_with_web(events, web)
    # タブ情報の無い前後はURLなしの元イベントとして残る
    assert [(e.url == "", e.start, e.end) for e in out] == [
        (True, _min(0), _min(10)),
        (False, _min(10), _min(20)),
        (True, _min(20), _min(30)),
    ]


def test_enrich_does_not_touch_non_browser():
    events = [ActivityEvent(_min(0), _min(30), "Code.exe", "main.py")]
    web = [_raw(0, 30, {"url": "https://example.com/", "title": "Example"})]
    out = enrich_with_web(events, web)
    assert out == events


def test_domain_of_event_without_url_is_empty():
    assert ActivityEvent(_min(0), _min(1), "Code.exe", "x").domain == ""


# ---- 分類: ドメインで判定・エンタメがブラウジングに勝つ ----

def test_youtube_in_browser_is_entertainment_not_browsing():
    c = Classifier(DEFAULT_RULES)
    ev = ActivityEvent(_min(0), _min(10), "chrome.exe", "動画タイトル",
                       url="https://www.youtube.com/watch?v=x")
    assert c.classify(ev).category == "エンタメ"


def test_plain_browser_time_is_browsing():
    c = Classifier(DEFAULT_RULES)
    ev = ActivityEvent(_min(0), _min(10), "chrome.exe", "ニュース記事",
                       url="https://example-news.com/article")
    assert c.classify(ev).category == "ブラウジング"


def test_domain_rule_matches():
    rules = [{"name": "調査", "patterns": [r"stackoverflow\.com"]}] + DEFAULT_RULES
    c = Classifier(rules)
    ev = ActivityEvent(_min(0), _min(10), "firefox.exe", "python - How to ...",
                       url="https://stackoverflow.com/questions/1")
    assert c.classify(ev).category == "調査"


# ---- 集計: サイト別 ----

def test_summarize_aggregates_by_site():
    c = Classifier(DEFAULT_RULES)
    events = [
        ActivityEvent(_min(0), _min(10), "chrome.exe", "動画",
                      url="https://youtube.com/watch"),
        ActivityEvent(_min(10), _min(15), "chrome.exe", "GitHub",
                      url="https://github.com/x"),
        ActivityEvent(_min(15), _min(20), "Code.exe", "main.py"),
    ]
    summary = summarize(date(2026, 7, 19), c.classify_all(events))
    assert summary.by_site == {"youtube.com": 10.0, "github.com": 5.0}


# ---- aw-watcher-input: 集中ブロック ----

def _input(start_min, duration_min, presses=0, clicks=0, dx=0, dy=0):
    return _raw(start_min, duration_min,
                {"presses": presses, "clicks": clicks, "deltaX": dx, "deltaY": dy})


def test_focus_block_detected_when_input_sustained():
    # 30分連続入力 → 集中ブロック1つ
    raw = [_input(i, 1, presses=10) for i in range(30)]
    stats = compute_input_stats(raw)
    assert len(stats.focus_blocks) == 1
    assert stats.focus_blocks[0].minutes == 30.0
    assert stats.keypresses == 300


def test_short_input_runs_are_not_focus_blocks():
    # 10分だけ → 25分未満なのでブロックなし（入力時間には計上）
    raw = [_input(i, 1, presses=5) for i in range(10)]
    stats = compute_input_stats(raw)
    assert stats.focus_blocks == []
    assert stats.active_input_minutes == 10.0


def test_zero_input_heartbeats_split_runs():
    # 15分入力 → 10分無入力（全ゼロのハートビート）→ 15分入力
    raw = ([_input(i, 1, presses=5) for i in range(15)]
           + [_input(15, 10)]  # 無入力区間
           + [_input(25 + i, 1, presses=5) for i in range(15)])
    stats = compute_input_stats(raw)
    # どちらの区間も25分未満なので集中ブロックにはならない
    assert stats.focus_blocks == []
    assert stats.active_input_minutes == 30.0


def test_small_gaps_do_not_split_focus_block():
    # 2分の途切れは同一ブロック扱い（max_gap=3分）
    raw = ([_input(i, 1, presses=5) for i in range(15)]
           + [_input(17 + i, 1, clicks=2) for i in range(15)])
    stats = compute_input_stats(raw)
    assert len(stats.focus_blocks) == 1
    assert stats.focus_blocks[0].minutes == 32.0


def test_compute_input_stats_empty():
    stats = compute_input_stats([])
    assert stats.keypresses == 0
    assert stats.focus_blocks == []


# ---- 実験指標 ----

def _summary_with_site(minutes_by_site):
    c = Classifier(DEFAULT_RULES)
    events = []
    cursor = 0
    for site, minutes in minutes_by_site.items():
        events.append(ActivityEvent(_min(cursor), _min(cursor + minutes),
                                    "chrome.exe", site, url=f"https://{site}/"))
        cursor += minutes
    return summarize(date(2026, 7, 19), c.classify_all(events))


def test_metric_site_minutes():
    summary = _summary_with_site({"youtube.com": 45})
    assert compute_metric("site_minutes:youtube.com", summary, []) == 45.0
    assert compute_metric("site_minutes:netflix.com", summary, []) == 0.0


def test_metric_focus_requires_input_stats():
    summary = _summary_with_site({})
    assert compute_metric("focus_blocks", summary, []) is None  # watcher未導入
    raw = [_input(i, 1, presses=10) for i in range(30)]
    stats = compute_input_stats(raw)
    assert compute_metric("focus_blocks", summary, [], stats) == 1.0
    assert compute_metric("focus_minutes", summary, [], stats) == 30.0
    assert compute_metric("input_keypresses", summary, [], stats) == 300.0
