from datetime import datetime, timedelta, timezone

from kaizenlog.classifier import Classifier, OTHER_CATEGORY
from kaizenlog.collector import ActivityEvent
from kaizenlog.config import DEFAULT_RULES


def _event(app, title):
    start = datetime(2026, 7, 5, 9, tzinfo=timezone.utc)
    return ActivityEvent(start=start, end=start + timedelta(minutes=10), app=app, title=title)


def test_ai_tools_detected():
    c = Classifier(DEFAULT_RULES)
    ce = c.classify(_event("chrome.exe", "Claude - my prompt"))
    assert ce.category == "AI作業"
    assert ce.ai is True
    assert ce.matched_tool == "claude"


def test_ai_takes_priority_over_browser():
    # ブラウザ内のChatGPTは「ブラウジング」ではなく「AI作業」になる
    c = Classifier(DEFAULT_RULES)
    ce = c.classify(_event("msedge.exe", "ChatGPT"))
    assert ce.category == "AI作業"


def test_dev_tools():
    c = Classifier(DEFAULT_RULES)
    assert c.classify(_event("Code.exe", "cli.py - Visual Studio Code")).category == "開発"
    ce = c.classify(_event("WindowsTerminal.exe", "PowerShell"))
    assert ce.category == "開発"


def test_unmatched_is_other():
    c = Classifier(DEFAULT_RULES)
    ce = c.classify(_event("some-random.exe", "hello"))
    assert ce.category == OTHER_CATEGORY
    assert ce.ai is False


def test_user_rules_take_priority():
    rules = [{"name": "自社ツール", "patterns": ["mytool"]}] + DEFAULT_RULES
    c = Classifier(rules)
    assert c.classify(_event("mytool.exe", "chrome")).category == "自社ツール"
