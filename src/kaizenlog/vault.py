"""Obsidianボールトのデイリーノートへの書き込み。

既存ノートを壊さないよう、マーカーで囲んだ区間だけを更新（upsert）する。
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    """一時ファイルに書いてから os.replace で置き換えるアトミック書き込み。

    夜間実行中のクラッシュ・電源断で半分だけ書けたファイル（不正なUTF-8・
    壊れたJSON・欠けたノート）が残ると、以後の実行が読み込みで連鎖的に
    失敗する。os.replace は同一ボリューム内でアトミックに完了する。
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)

ACTIVITY_MARKER = "kaizenlog:activity"
ADVICE_MARKER = "kaizenlog:advice"
ACTIONS_MARKER = "kaizenlog:actions"  # 朝の引き継ぎ（未完了アクション転記）


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
    in_fence = False
    for line in lines:
        stripped = line.strip()
        # コードフェンス内の「# コメント」やObsidianの「#タグ」を見出し扱いすると、
        # そこでセクションが切れて以降のタスク行が黙って消える
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            if found:
                body.append(line)
            continue
        is_heading = (not in_fence
                      and stripped.startswith("#")
                      and stripped.lstrip("#")[:1] in (" ", "\t", ""))
        if is_heading:
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
        atomic_write_text(p, upsert_section(content, marker, section_md))
        return p
