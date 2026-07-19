"""LeechBlock 介入（時間泥棒検出→ルール生成）のテスト。"""

from kaizenlog.config import DEFAULT_RULES
from kaizenlog.intervention import (
    BlockRule,
    detect_time_sinks,
    render_leechblock_options,
    render_plan,
    suggest_rules,
    suggest_window,
)


def _stats(day, by_site=None, blocks=None):
    return {"day": day, "by_site": by_site or {}, "blocks": blocks or []}


def _block(start_hour, minutes, category="エンタメ", app="chrome.exe", title="YouTube"):
    start = f"2026-07-19T{start_hour:02d}:00:00+09:00"
    end_h, end_m = divmod(start_hour * 60 + minutes, 60)
    end = f"2026-07-19T{end_h:02d}:{end_m:02d}:00+09:00"
    return {"start": start, "end": end, "category": category,
            "app": app, "minutes": float(minutes), "title": title}


# ---- 検出 ----

def test_detect_from_by_site():
    stats = [_stats(f"2026-07-{d:02d}", by_site={"youtube.com": 40, "github.com": 120})
             for d in range(1, 8)]
    sinks = detect_time_sinks(stats, DEFAULT_RULES)
    assert [s.label for s in sinks] == ["youtube.com"]  # github.com は開発なので除外
    assert sinks[0].source == "site"
    assert sinks[0].avg_minutes == 40.0


def test_detect_below_threshold_is_ignored():
    stats = [_stats(f"2026-07-{d:02d}", by_site={"youtube.com": 10}) for d in range(1, 8)]
    assert detect_time_sinks(stats, DEFAULT_RULES) == []


def test_detect_falls_back_to_block_titles():
    # aw-watcher-web 未導入: by_site が無く、ブロックのタイトルから推定
    stats = [_stats(f"2026-07-{d:02d}", blocks=[_block(17, 45)]) for d in range(1, 8)]
    sinks = detect_time_sinks(stats, DEFAULT_RULES)
    assert len(sinks) == 1
    assert sinks[0].label == "youtube.com"
    assert sinks[0].source == "title"
    assert sinks[0].avg_minutes == 45.0
    assert sinks[0].hour_minutes.get(17, 0) > 0


def test_site_data_preferred_over_title_estimate():
    stats = [_stats(f"2026-07-{d:02d}",
                    by_site={"youtube.com": 30},
                    blocks=[_block(17, 45)]) for d in range(1, 8)]
    sinks = detect_time_sinks(stats, DEFAULT_RULES)
    assert len(sinks) == 1
    assert sinks[0].source == "site"


def test_detect_empty_stats():
    assert detect_time_sinks([], DEFAULT_RULES) == []


# ---- 時間帯ウィンドウ ----

def test_window_for_concentrated_hours():
    assert suggest_window({17: 40.0, 18: 20.0}) == (17, 19)


def test_no_window_for_spread_usage():
    spread = {h: 10.0 for h in range(8, 23)}  # 15時間に均等分散
    assert suggest_window(spread) is None


def test_no_window_for_empty_histogram():
    assert suggest_window({}) is None


# ---- ルール提案 ----

def test_window_rule_uses_hourly_limit():
    stats = [_stats(f"2026-07-{d:02d}", blocks=[_block(17, 45)]) for d in range(1, 8)]
    rules = suggest_rules(detect_time_sinks(stats, DEFAULT_RULES))
    assert len(rules) == 1
    r = rules[0]
    assert r.times == "1700-1800"
    assert r.limit_mins == 10 and r.limit_period == 3600
    assert r.conj_mode is True  # 時間帯 AND 上限
    assert r.metric == "category_minutes:エンタメ"  # web watcher無しのフォールバック
    assert r.target == "<= 20"  # 45分/日の半分（22.5）を5分単位に丸め


def test_daily_rule_for_site_source():
    stats = [_stats(f"2026-07-{d:02d}", by_site={"youtube.com": 60}) for d in range(1, 8)]
    rules = suggest_rules(detect_time_sinks(stats, DEFAULT_RULES))
    r = rules[0]
    assert r.times == ""
    assert r.limit_mins == 30 and r.limit_period == 86400
    assert r.conj_mode is False
    assert r.metric == "site_minutes:youtube.com"


# ---- LeechBlock 形式 ----

def test_leechblock_format():
    rules = [BlockRule(set_name="KZN: youtube.com", sites="youtube.com",
                       times="1700-1900", limit_mins=10, limit_period=3600,
                       metric="site_minutes:youtube.com", target="<= 20",
                       evidence="テスト")]
    text = render_leechblock_options(rules)
    lines = text.strip().splitlines()
    assert lines[0] == "numSets=20"  # セット20以降を使用（ユーザー領域を守る）
    assert "setName20=KZN: youtube.com" in lines
    assert "sites20=youtube.com" in lines
    assert "times20=1700-1900" in lines
    assert "limitMins20=10" in lines
    assert "limitPeriod20=3600" in lines
    assert "conjMode20=true" in lines
    assert "days20=127" in lines  # 毎日


def test_leechblock_format_multiple_sets():
    rules = [BlockRule(f"KZN: site{i}", f"site{i}.com", "", 15, 86400,
                       "category_minutes:エンタメ", "<= 15", "テスト") for i in range(3)]
    text = render_leechblock_options(rules)
    assert "numSets=22" in text
    assert "setName22=KZN: site2" in text
    assert "conjMode22=false" in text


def test_render_plan_mentions_estimate_warning():
    stats = [_stats(f"2026-07-{d:02d}", blocks=[_block(17, 45)]) for d in range(1, 8)]
    sinks = detect_time_sinks(stats, DEFAULT_RULES)
    plan = render_plan(sinks, suggest_rules(sinks))
    assert "KZN: youtube.com" in plan
    assert "aw-watcher-web" in plan  # 推定であることの注意書き
