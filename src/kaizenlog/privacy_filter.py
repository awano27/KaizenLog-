"""私的コンテンツ判定（日誌タイムライン・日報で共有）。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

PRIVATE_CATEGORIES: tuple[str, ...] = ("エンタメ",)

# ブラウザ経由の私的コンテンツ（分類上「ブラウジング」になるもの）
PRIVATE_TITLE_RE = re.compile(
    r"youtube|netflix|spotify|twitter|reddit|tiktok|niconico|ニコニコ|prime video",
    re.IGNORECASE,
)


def is_private_block(
    category: str | Mapping[str, Any] | None = None,
    title: str | None = None,
    app: str | None = None,
) -> bool:
    """エンタメカテゴリ、または私的タイトル/アプリなら True。

    第1引数に block dict を渡す旧呼び出し（nippou）も受け付ける。
    """
    if isinstance(category, Mapping):
        block = category
        category = str(block.get("category") or "")
        title = str(block.get("title") or title or "")
        app = str(block.get("app") or app or "")
    cat = str(category or "")
    if cat in PRIVATE_CATEGORIES:
        return True
    return bool(PRIVATE_TITLE_RE.search(f"{title or ''} {app or ''}"))
