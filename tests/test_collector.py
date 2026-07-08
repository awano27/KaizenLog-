from datetime import datetime, timezone

from kaizenlog.collector import active_intervals, clip_to_active


def _ts(hour, minute=0):
    return datetime(2026, 7, 5, hour, minute, tzinfo=timezone.utc)


def _ev(start, duration_s, data):
    return {"timestamp": start.isoformat(), "duration": duration_s, "data": data}


def test_active_intervals_extracts_not_afk():
    afk = [
        _ev(_ts(9), 3600, {"status": "not-afk"}),
        _ev(_ts(10), 1800, {"status": "afk"}),
        _ev(_ts(10, 30), 1800, {"status": "not-afk"}),
    ]
    intervals = active_intervals(afk)
    assert intervals == [(_ts(9), _ts(10)), (_ts(10, 30), _ts(11))]


def test_clip_removes_afk_time():
    # 9:00-11:00までブラウザが開きっぱなしだが、10:00-10:30はAFK
    window = [_ev(_ts(9), 7200, {"app": "chrome.exe", "title": "docs"})]
    intervals = [(_ts(9), _ts(10)), (_ts(10, 30), _ts(11))]
    events = clip_to_active(window, intervals)
    assert len(events) == 2
    assert events[0].start == _ts(9) and events[0].end == _ts(10)
    assert events[1].start == _ts(10, 30) and events[1].end == _ts(11)
    total_min = sum(e.duration.total_seconds() for e in events) / 60
    assert total_min == 90


def test_clip_handles_no_overlap():
    window = [_ev(_ts(8), 1800, {"app": "code.exe", "title": "main.py"})]
    intervals = [(_ts(9), _ts(10))]
    assert clip_to_active(window, intervals) == []
