from datetime import date, datetime, timedelta, timezone

from kaizenlog.classifier import Classifier
from kaizenlog.collector import ActivityEvent
from kaizenlog.config import DEFAULT_RULES
from kaizenlog.report import build_blocks, render_markdown, summarize
from kaizenlog.vault import (
    ACTIVITY_MARKER,
    DailyNoteStore,
    extract_section,
    upsert_section,
)

TZ = timezone.utc


def _events():
    base = datetime(2026, 7, 5, 9, tzinfo=TZ)
    specs = [
        (0, 30, "Code.exe", "main.py - Visual Studio Code"),
        (30, 20, "chrome.exe", "Claude"),
        (50, 10, "chrome.exe", "Claude"),
        (65, 30, "Code.exe", "main.py - Visual Studio Code"),
        (95, 15, "chrome.exe", "YouTube"),
    ]
    out = []
    for offset, dur, app, title in specs:
        start = base + timedelta(minutes=offset)
        out.append(ActivityEvent(start=start, end=start + timedelta(minutes=dur), app=app, title=title))
    return out


def _summary():
    classified = Classifier(DEFAULT_RULES).classify_all(_events())
    return summarize(date(2026, 7, 5), classified, gap_minutes=5.0)


def test_summarize_totals_and_ai():
    s = _summary()
    assert round(s.total_minutes) == 105
    assert round(s.by_category["AI作業"]) == 30
    assert s.ai_sessions == 1  # 30分+10分は5分ギャップ以内なので1セッション
    assert s.context_switches == 3


def test_blocks_merge_within_gap():
    classified = Classifier(DEFAULT_RULES).classify_all(_events())
    blocks = build_blocks(classified, gap_minutes=5.0)
    assert len(blocks) == 4  # Claudeの2イベントが1ブロックに統合される


def test_render_markdown_contains_sections():
    md = render_markdown(_summary(), TZ)
    assert "## 📊 Activity Log" in md
    assert "### カテゴリ別" in md
    assert "🤖 AI作業の内訳" in md
    assert "claude" in md
    assert "### タイムライン" in md


def test_summarize_overlapping_events_not_double_counted():
    start = datetime(2026, 7, 5, 9, tzinfo=TZ)
    events = [
        ActivityEvent(start=start, end=start + timedelta(minutes=60),
                      app="Code.exe", title="main.py - Visual Studio Code"),
        ActivityEvent(start=start, end=start + timedelta(minutes=60),
                      app="Code.exe", title="main.py - Visual Studio Code"),
    ]
    classified = Classifier(DEFAULT_RULES).classify_all(events)
    s = summarize(date(2026, 7, 5), classified)
    assert round(s.total_minutes) == 60


def test_summarize_partial_overlap_clipped():
    start = datetime(2026, 7, 5, 9, tzinfo=TZ)
    events = [
        ActivityEvent(start=start, end=start + timedelta(minutes=60),
                      app="Code.exe", title="main.py"),
        ActivityEvent(start=start + timedelta(minutes=30),
                      end=start + timedelta(minutes=90),
                      app="chrome.exe", title="docs"),
    ]
    classified = Classifier(DEFAULT_RULES).classify_all(events)
    s = summarize(date(2026, 7, 5), classified)
    assert round(s.total_minutes) == 90  # 和集合。重複30分は先着イベントに帰属
    assert round(sum(s.by_category.values())) == 90


def test_summarize_and_blocks_order_independent():
    classified = Classifier(DEFAULT_RULES).classify_all(list(reversed(_events())))
    s = summarize(date(2026, 7, 5), classified, gap_minutes=5.0)
    assert round(s.total_minutes) == 105
    blocks = build_blocks(classified, gap_minutes=5.0)
    assert len(blocks) == 4
    assert all(a.start <= b.start for a, b in zip(blocks, blocks[1:]))


def test_upsert_section_idempotent():
    original = "---\ndate: 2026-07-05\n---\n\n## メモ\n手書きの内容\n"
    v1 = upsert_section(original, ACTIVITY_MARKER, "## 📊 Activity Log\n\nv1")
    assert "手書きの内容" in v1 and "v1" in v1
    v2 = upsert_section(v1, ACTIVITY_MARKER, "## 📊 Activity Log\n\nv2")
    assert "v2" in v2 and "v1" not in v2
    assert "手書きの内容" in v2
    assert v2.count("kaizenlog:activity:start") == 1


def test_extract_section_roundtrip():
    content = upsert_section("", ACTIVITY_MARKER, "hello world")
    assert extract_section(content, ACTIVITY_MARKER) == "hello world"
    assert extract_section(content, "kaizenlog:advice") is None


def test_daily_note_store_creates_and_updates(tmp_path):
    store = DailyNoteStore(tmp_path / "01 Daily Notes")
    day = date(2026, 7, 5)
    p = store.write_section(day, ACTIVITY_MARKER, "## 📊 Activity Log\n\nfirst")
    assert p.is_file()
    content = p.read_text(encoding="utf-8")
    assert content.startswith("---\ndate: 2026-07-05")
    store.write_section(day, ACTIVITY_MARKER, "## 📊 Activity Log\n\nsecond")
    content = p.read_text(encoding="utf-8")
    assert "second" in content and "first" not in content
