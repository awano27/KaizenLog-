"""イベントをカテゴリ（AI作業・開発・ブラウジング…）に分類する。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .collector import ActivityEvent

OTHER_CATEGORY = "その他"


@dataclass
class Rule:
    name: str
    patterns: list[re.Pattern]
    ai: bool = False

    def matches(self, text: str) -> bool:
        return any(p.search(text) for p in self.patterns)


@dataclass
class ClassifiedEvent:
    event: ActivityEvent
    category: str
    ai: bool
    matched_tool: str | None = None  # AI作業の場合、どのツールにマッチしたか


class Classifier:
    def __init__(self, rule_dicts: list[dict]):
        self.rules: list[Rule] = []
        for rd in rule_dicts:
            patterns = [re.compile(p, re.IGNORECASE) for p in rd.get("patterns", [])]
            self.rules.append(Rule(name=rd["name"], patterns=patterns, ai=bool(rd.get("ai", False))))

    def classify(self, event: ActivityEvent) -> ClassifiedEvent:
        text = f"{event.app} | {event.title}"
        for rule in self.rules:
            for pattern in rule.patterns:
                m = pattern.search(text)
                if m:
                    tool = m.group(0).lower() if rule.ai else None
                    return ClassifiedEvent(event=event, category=rule.name, ai=rule.ai, matched_tool=tool)
        return ClassifiedEvent(event=event, category=OTHER_CATEGORY, ai=False)

    def classify_all(self, events: list[ActivityEvent]) -> list[ClassifiedEvent]:
        return [self.classify(e) for e in events]
