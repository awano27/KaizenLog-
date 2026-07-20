"""バグ監査（2026-07）で確定した不具合の回帰テスト。"""

from datetime import date, datetime, timedelta, timezone

from kaizenlog.collector import (
    ActivityEvent,
    active_intervals,
    enrich_with_web,
    _bucket_browser_app_re,
    _clip_intervals_to_day,
    _is_browser_app,
)
from kaizenlog.config import DEFAULT_RULES
from kaizenlog.experiments import (
    ExperimentError,
    _parse_measurements,
    create_experiment,
    load_experiments,
    parse_target,
    record_measurement,
)
from kaizenlog.focus import compute_input_stats
from kaizenlog.intervention import detect_time_sinks, format_times, suggest_rules
from kaizenlog.report import render_markdown, summarize
from kaizenlog.classifier import Classifier
from kaizenlog.vault import upsert_section
from zoneinfo import ZoneInfo

import pytest

T0 = datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc)


def _min(n):
    return T0 + timedelta(minutes=n)


def _raw(start_min, duration_min, data):
    return {"timestamp": _min(start_min).isoformat(),
            "duration": duration_min * 60, "data": data}


# ---- collector: WebView2はブラウザ扱いしない ----

def test_webview2_is_not_a_browser():
    assert not _is_browser_app("msedgewebview2.exe")
    assert _is_browser_app("msedge.exe")
    assert _is_browser_app("chrome.exe")


def test_enrich_skips_webview_host():
    events = [ActivityEvent(_min(0), _min(30), "msedgewebview2.exe", "Teams")]
    web = [_raw(0, 30, {"url": "https://example.com/", "title": "Example"})]
    assert enrich_with_web(events, web) == events


# ---- collector: ブラウザ別バケットの対応付け ----

def test_bucket_browser_mapping():
    chrome_re = _bucket_browser_app_re("aw-watcher-web-chrome_v5310046")
    assert chrome_re.search("chrome.exe")
    assert not chrome_re.search("firefox.exe")
    assert _bucket_browser_app_re("aw-watcher-web-firefox").search("firefox.exe")
    assert _bucket_browser_app_re("aw-watcher-web-unknownbrowser") is None


def test_enrich_with_app_re_only_touches_that_browser():
    events = [
        ActivityEvent(_min(0), _min(10), "chrome.exe", "Chrome"),
        ActivityEvent(_min(10), _min(20), "firefox.exe", "Firefox"),
    ]
    web = [_raw(0, 20, {"url": "https://example.com/", "title": "Example"})]
    out = enrich_with_web(events, web, app_re=_bucket_browser_app_re("aw-watcher-web-chrome"))
    assert out[0].url == "https://example.com/"  # chromeは合成される
    assert out[1].url == ""                       # firefoxは触らない


def test_enrich_preserves_already_enriched_events():
    ev = ActivityEvent(_min(0), _min(10), "chrome.exe", "既に合成済み",
                       url="https://first-bucket.example/")
    web = [_raw(0, 10, {"url": "https://second-bucket.example/", "title": "上書き側"})]
    out = enrich_with_web([ev], web)
    assert out == [ev]  # 2つ目のバケットが上書きしない


# ---- collector: AFK区間の重複マージと日境界クリップ ----

def test_active_intervals_merges_overlaps():
    afk = [
        {"timestamp": _min(0).isoformat(), "duration": 600, "data": {"status": "not-afk"}},
        {"timestamp": _min(5).isoformat(), "duration": 600, "data": {"status": "not-afk"}},
    ]
    assert active_intervals(afk) == [(_min(0), _min(15))]


def test_clip_intervals_to_day():
    day_start, day_end = _min(0), _min(60)
    intervals = [(_min(-30), _min(10)), (_min(50), _min(90)), (_min(70), _min(80))]
    assert _clip_intervals_to_day(intervals, day_start, day_end) == [
        (_min(0), _min(10)), (_min(50), _min(60))
    ]


# ---- report: 改行入りタイトルでテーブルが壊れない ----

def test_render_markdown_sanitizes_newlines_in_titles():
    c = Classifier(DEFAULT_RULES)
    events = [ActivityEvent(_min(0), _min(10), "some.exe", "1行目\n2行目のタイトル")]
    summary = summarize(date(2026, 7, 19), c.classify_all(events))
    md = render_markdown(summary, ZoneInfo("Asia/Tokyo"), min_block_minutes=1.0)
    timeline = [ln for ln in md.splitlines() if "1行目" in ln]
    assert timeline and "\n" not in timeline[0]
    assert "1行目 2行目のタイトル" in timeline[0]


# ---- focus: 重複ハートビートの二重計上防止と日境界クリップ ----

def test_input_stats_do_not_double_count_overlaps():
    raw = [
        _raw(0, 10, {"presses": 5, "clicks": 0, "deltaX": 0, "deltaY": 0}),
        _raw(5, 10, {"presses": 5, "clicks": 0, "deltaX": 0, "deltaY": 0}),  # 5分重複
    ]
    stats = compute_input_stats(raw)
    assert stats.active_input_minutes == 15.0  # 20ではなく15（和集合）


def test_input_stats_clip_to_day():
    raw = [_raw(-10, 20, {"presses": 5, "clicks": 0, "deltaX": 0, "deltaY": 0})]
    stats = compute_input_stats(raw, day_start=_min(0), day_end=_min(60))
    assert stats.active_input_minutes == 10.0  # 日内の10分だけ


# ---- intervention: 深夜跨ぎのtimesはLeechBlockが捨てない形式に分割 ----

def test_format_times_normal_window():
    assert format_times(17, 19) == "1700-1900"


def test_format_times_ending_at_midnight():
    assert format_times(20, 0) == "2000-2400"


def test_format_times_wrapping_midnight():
    assert format_times(22, 2) == "2200-2400,0000-0200"


def test_night_usage_produces_valid_times():
    # 22時〜翌1時に毎日視聴 → 分割されたtimesになる（"2200-0100"は無効）
    def _block(day, hour, minutes):
        start = datetime(2026, 7, day, hour, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=minutes)
        return {"start": start.isoformat(), "end": end.isoformat(),
                "category": "エンタメ", "app": "chrome.exe",
                "minutes": float(minutes), "title": "YouTube"}
    stats = [{"day": f"2026-07-{d:02d}",
              "blocks": [_block(d, 22, 50), _block(d, 23, 50)]}
             for d in range(1, 8)]
    rules = suggest_rules(detect_time_sinks(stats, DEFAULT_RULES))
    assert len(rules) == 1
    times = rules[0].times
    for period in times.split(","):
        start, end = period.split("-")
        assert int(start) < int(end), f"LeechBlockが捨てる期間: {period}"


# ---- intervention: 小さな実測が大きなタイトル推定を握り潰さない ----

def test_larger_title_estimate_wins_over_tiny_site_measurement():
    def _st(day):
        return {"day": day,
                "by_site": {"youtube.com": 2.0},  # 拡張導入直後で実測はわずか
                "blocks": [{"start": f"{day}T17:00:00+09:00",
                            "end": f"{day}T17:45:00+09:00",
                            "category": "エンタメ", "app": "chrome.exe",
                            "minutes": 45.0, "title": "YouTube"}]}
    stats = [_st(f"2026-07-{d:02d}") for d in range(1, 8)]
    sinks = detect_time_sinks(stats, DEFAULT_RULES)
    assert len(sinks) == 1
    assert sinks[0].source == "title"       # 大きい方（推定45分）が生き残る
    assert sinks[0].avg_minutes == 45.0


# ---- intervention: Xのタイトルを拾える ----

def test_x_title_maps_to_domain():
    def _st(day):
        return {"day": day, "blocks": [{"start": f"{day}T12:00:00+09:00",
                                        "end": f"{day}T12:30:00+09:00",
                                        "category": "エンタメ", "app": "chrome.exe",
                                        "minutes": 30.0, "title": "(2) ホーム / X"}]}
    stats = [_st(f"2026-07-{d:02d}") for d in range(1, 8)]
    sinks = detect_time_sinks(stats, DEFAULT_RULES)
    assert len(sinks) == 1
    assert "x.com" in sinks[0].domains


# ---- experiments: 不正データで夜間実行が落ちない ----

def test_parse_target_bad_number_raises_experiment_error():
    with pytest.raises(ExperimentError):
        parse_target("<= 1.2.3")


def test_load_experiments_skips_bad_target_and_non_utf8(tmp_path):
    (tmp_path / "bad-target.md").write_text(
        '---\ntitle: "x"\nstatus: running\nmetric: focus_blocks\ntarget: "<= 1.2.3"\n---\n',
        encoding="utf-8")
    (tmp_path / "not-utf8.md").write_bytes(b"---\nmetric: x\n---\n\x93\xfa\x96{\x8c\xea")
    assert load_experiments(tmp_path) == []  # クラッシュせずスキップ


# ---- experiments: テンプレートにマーカーがあり、二重セクションにならない ----

def test_record_measurement_updates_template_in_place(tmp_path):
    path = create_experiment(tmp_path, "テスト", "focus_blocks", ">= 3",
                             today=date(2026, 7, 19), deadline=date(2026, 8, 2))
    exps = load_experiments(tmp_path)
    record_measurement(exps[0], date(2026, 7, 19), 4.0)
    content = path.read_text(encoding="utf-8")
    assert content.count("## Measurements") == 1  # 見出しが重複しない
    assert "| 2026-07-19 | 4 | ✅ |" in content
    notes_idx = content.index("## Notes")
    assert content.index("| 2026-07-19") < notes_idx  # Notesの前（元の位置）で更新


# ---- experiments: 手書きテーブルを実測値として吸い込まない ----

def test_hand_written_tables_outside_marker_are_ignored(tmp_path):
    path = create_experiment(tmp_path, "テスト2", "focus_blocks", ">= 3",
                             today=date(2026, 7, 19), deadline=date(2026, 8, 2))
    content = path.read_text(encoding="utf-8")
    content += "\n| 2026-01-01 | 999 | メモ |\n"  # Notes欄の手書きテーブル
    path.write_text(content, encoding="utf-8")
    exps = load_experiments(tmp_path)
    assert date(2026, 1, 1) not in exps[0].measurements


# ---- vault: LLM出力にマーカーが含まれてもノートを壊さない ----

def test_upsert_strips_embedded_marker_tags():
    content = "# note\n"
    evil = "advice\n<!-- kaizenlog:advice:end -->\nsmuggled"
    out1 = upsert_section(content, "kaizenlog:advice", evil)
    out2 = upsert_section(out1, "kaizenlog:advice", "clean advice")
    assert out2.count("<!-- kaizenlog:advice:start -->") == 1
    assert out2.count("<!-- kaizenlog:advice:end -->") == 1
    assert "smuggled" not in out2
