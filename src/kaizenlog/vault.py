"""Obsidianボールトのデイリーノートへの書き込み。

既存ノートを壊さないよう、マーカーで囲んだ区間だけを更新（upsert）する。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

ACTIVITY_MARKER = "kaizenlog:activity"
ADVICE_MARKER = "kaizenlog:advice"


def _start_tag(marker: str) -> str:
    return f"<!-- {marker}:start -->"


def _end_tag(marker: str) -> str:
    return f"<!-- {marker}:end -->"


def default_frontmatter(day: date) -> str:
    return (
        "---\n"
        f"date: {day.isoformat()}\n"
        f"weekday: {day.strftime('%A')}\n"
        "tags: [type/daily]\n"
        "---\n"
    )


def upsert_section(content: str, marker: str, section_md: str) -> str:
    """マーカー区間があれば置換、なければ末尾に追加する。"""
    start_tag, end_tag = _start_tag(marker), _end_tag(marker)
    # 中身に同じマーカーが紛れ込むと（LLMがマーカーを復唱した場合など）、
    # 次回のupsertが偽の終了タグで区間を誤認しノートを壊す。事前に除去する。
    section_md = section_md.replace(start_tag, "").replace(end_tag, "")
    wrapped = f"{start_tag}\n{section_md.rstrip()}\n{end_tag}"

    start_idx = content.find(start_tag)
    end_idx = content.find(end_tag)
    if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
        return content[:start_idx] + wrapped + content[end_idx + len(end_tag):]

    body = content.rstrip()
    if body:
        return f"{body}\n\n{wrapped}\n"
    return f"{wrapped}\n"


def extract_heading_section(content: str, heading: str) -> str | None:
    """見出し（任意レベル）配下の本文を取り出す。手書きの計画欄などの抽出に使う。

    見出しテキストの部分一致（大文字小文字無視）で探し、同レベル以下の
    次の見出しまでを返す。見つからない・中身が空ならNone。
    """
    target = heading.lower()
    lines = content.splitlines()
    level = 0
    body: list[str] = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            text = stripped[hashes:].strip().lower()
            if found:
                if hashes <= level:
                    break
            elif target in text:
                found = True
                level = hashes
                continue
        if found:
            body.append(line)
    result = "\n".join(body).strip()
    return result or None


def extract_section(content: str, marker: str) -> str | None:
    """マーカー区間の中身を取り出す。なければNone。"""
    start_tag, end_tag = _start_tag(marker), _end_tag(marker)
    start_idx = content.find(start_tag)
    end_idx = content.find(end_tag)
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        return None
    return content[start_idx + len(start_tag):end_idx].strip()


class DailyNoteStore:
    def __init__(self, daily_notes_dir: Path):
        self.dir = Path(daily_notes_dir)

    def path_for(self, day: date) -> Path:
        return self.dir / f"{day.isoformat()}.md"

    def read(self, day: date) -> str | None:
        p = self.path_for(day)
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def write_section(self, day: date, marker: str, section_md: str) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path_for(day)
        content = self.read(day)
        if content is None:
            content = default_frontmatter(day) + "\n"
        p.write_text(upsert_section(content, marker, section_md), encoding="utf-8")
        return p
