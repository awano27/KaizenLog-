"""Explain unclassified names and append explicitly requested classification rules."""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from datetime import date, timedelta
from math import isfinite
from pathlib import Path

from .classifier import Classifier
from .file_lock import file_lock
from .privacy_filter import is_private_block
from .vault import atomic_write_bytes


@dataclass(frozen=True)
class UnknownCandidate:
    kind: str
    name: str
    minutes: float


def unknown_candidates(stats_list, rules, *, as_of: date, days: int = 14,
                       min_minutes: float = 120, private_title_patterns=(), redactor=None):
    if days < 1 or not isfinite(min_minutes) or min_minutes < 0:
        raise ValueError('日数と時間閾値が不正です。')
    by_day = {}
    for item in stats_list:
        if not isinstance(item, dict):
            continue
        raw_day = item.get('day')
        try:
            day = date.fromisoformat(raw_day)
        except (ValueError, TypeError):
            continue
        if raw_day == day.isoformat() and as_of - timedelta(days=days - 1) <= day <= as_of:
            by_day[day] = item
    classifier = Classifier(rules)
    totals = {}
    for item in by_day.values():
        for kind, key in (('app', 'by_app'), ('site', 'by_site')):
            mapping = item.get(key)
            if not isinstance(mapping, dict):
                continue
            for name, minutes in mapping.items():
                if (not isinstance(name, str) or not name.strip()
                        or isinstance(minutes, bool) or not isinstance(minutes, (float, int))
                        or not isfinite(minutes) or minutes < 0):
                    continue
                if is_private_block(title=name, private_title_patterns=private_title_patterns):
                    continue
                if any(rule.matches(name) for rule in classifier.rules):
                    continue
                totals[kind, name] = totals.get((kind, name), 0) + minutes
    return sorted((UnknownCandidate(kind, redactor(name) if redactor else name, minutes)
                   for (kind, name), minutes in totals.items() if minutes >= min_minutes),
                  key=lambda c: (-c.minutes, c.kind, c.name))


def _append_payload(original: bytes, *, name: str, pattern: str, ai: bool) -> bytes:
    data = tomllib.loads(original.decode('utf-8'))
    cats = data.get('categories', {})
    if not isinstance(cats, dict) or not isinstance(cats.get('rules', []), list):
        raise ValueError('categories.rules の形式が不正です。')
    for rule in cats.get('rules', []):
        if (isinstance(rule, dict) and rule.get('name') == name
                and pattern in rule.get('patterns', []) and bool(rule.get('ai', False)) == ai):
            return original
    nl = '\r\n' if b'\r\n' in original else '\n'
    suffix = nl + '[[categories.rules]]' + nl
    suffix += 'name = ' + json.dumps(name, ensure_ascii=False) + nl
    suffix += 'patterns = [' + json.dumps(pattern, ensure_ascii=False) + ']' + nl
    suffix += 'ai = ' + ('true' if ai else 'false') + nl
    updated = original + suffix.encode('utf-8')
    tomllib.loads(updated.decode('utf-8'))
    return updated


def append_rule(path: Path, *, name: str, pattern: str, ai: bool = False,
                write: bool = False) -> str:
    if not isinstance(name, str) or not name.strip() or not isinstance(pattern, str) or not pattern:
        raise ValueError('カテゴリ名と正規表現を指定してください。')
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as error:
        raise ValueError('分類ルールの正規表現が不正です。') from error
    path = Path(path)
    original = path.read_bytes()
    updated = _append_payload(original, name=name, pattern=pattern, ai=ai)
    if write and updated != original:
        with file_lock(path.with_name(path.name + '.rules.lock')):
            if path.read_bytes() != original:
                raise ValueError('確認中に設定が変更されました。差分を再確認してください。')
            atomic_write_bytes(path, updated)
    return updated[len(original):].decode('utf-8')
