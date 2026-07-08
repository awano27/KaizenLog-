"""プライバシーレダクション: LLMに送信する前に機密パターンをマスクする。

ウィンドウタイトルには顧客名・案件名などが含まれ得る。設定 [privacy] の
redact_patterns（正規表現）にマッチした箇所を置換してから外部LLMへ送る。
マスクは送信プロンプトにのみ適用され、ボールト内の日誌は原文のまま保持される。
"""

from __future__ import annotations

import re
from typing import Callable


class PrivacyError(ValueError):
    pass


def compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            raise PrivacyError(f"[privacy] redact_patterns の正規表現が不正です: {p!r} ({e})")
    return compiled


def make_redactor(
    patterns: list[str], replacement: str = "[REDACTED]"
) -> Callable[[str], str] | None:
    """パターンが空ならNone（レダクション無効）を返す。"""
    if not patterns:
        return None
    compiled = compile_patterns(patterns)

    def redact(text: str) -> str:
        for pattern in compiled:
            text = pattern.sub(replacement, text)
        return text

    return redact
