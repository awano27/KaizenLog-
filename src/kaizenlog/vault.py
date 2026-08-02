"""Obsidianボールトのデイリーノートへの書き込み。

既存ノートを壊さないよう、マーカーで囲んだ区間だけを更新（upsert）する。
マーカー外の既存bytes（末尾空白・改行コード含む）は変更しない。
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """一時ファイルへbytesを書いて os.replace で置き換える。"""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(content)
    os.replace(tmp, path)


def atomic_write_text(path: Path, content: str, *, newline: str | None = None) -> None:
    """一時ファイルに書いてから os.replace で置き換えるアトミック書き込み。

    newline=None の既定は newline=\"\"（変換なし）。明示指定時はその newline で書く。
    既存呼び出しの既定動作は「渡した content をそのまま書く」。
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    # newline=\"\" は改行変換なし。None も変換なし（content 内の改行をそのまま）。
    nl = "" if newline is None else newline
    with open(tmp, "w", encoding="utf-8", newline=nl) as f:
        f.write(content)
    os.replace(tmp, path)


def read_text_preserve_newlines(path: Path) -> str:
    """改行変換なしで UTF-8 テキストを読む。"""
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def detect_newline(content: str) -> str:
    """主要改行コード。CRLF が（CR無し）LF 以上なら CRLF、それ以外は LF。

    既存改行が0件なら LF。
    """
    if not content:
        return "\n"
    crlf = content.count("\r\n")
    lf_only = content.count("\n") - crlf
    if crlf > 0 and crlf >= lf_only:
        return "\r\n"
    return "\n"


def _normalize_section_newlines(section_md: str, nl: str) -> str:
    """セクション本文の改行を対象ファイルの主要改行へ揃える。末尾改行は除去。"""
    body = section_md.replace("\r\n", "\n").replace("\r", "\n")
    body = body.rstrip("\n")
    if nl == "\r\n":
        body = body.replace("\n", "\r\n")
    return body


ACTIVITY_MARKER = "kaizenlog:activity"
ADVICE_MARKER = "kaizenlog:advice"
ACTIONS_MARKER = "kaizenlog:actions"  # 朝の引き継ぎ（未完了アクション転記）
GOAL_MARKER = "kaizenlog:goal"  # 今日の作業目標（所有: goal コマンドのみ。generate/advise は読取）
WEEKLY_CONTEXT_MARKER = "kaizenlog:weekly-context"  # 週次スコアカード（決定論）
AGENT_CONTEXT_MARKER = "kaizenlog:agent-context"  # handoff が CLAUDE.md/AGENTS.md へ注入
COACH_MARKER = "kaizenlog:coach"  # coach --apply が追記する調教区間
DIGEST_MARKER = "kaizenlog:digest"  # 冒頭30秒サマリ（決定論・advise が所有）


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


def _frontmatter_end(content: str) -> int:
    """frontmatter の終了位置（次のコンテンツ開始 index）。無ければ 0。

    先頭が --- で始まり、次の --- 行までを frontmatter とみなす。
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return sum(len(lines[j]) for j in range(i + 1))
    return 0


def upsert_section(
    content: str,
    marker: str,
    section_md: str,
    position: str = "bottom",
) -> str:
    """マーカー区間があれば置換、なければ追加する。

    既存 content には rstrip/lstrip/全体置換をしない。
    生成する管理区間だけを主要改行コードへ揃える。

    position:
      - \"bottom\": 末尾に追加（元bytesを完全prefixとして残す）
      - \"top\": 区間が**未存在のときだけ** frontmatter 直後（無ければ先頭）に挿入。
        既存区間がある場合は現在位置で置換する。
    """
    nl = detect_newline(content)
    start_tag, end_tag = _start_tag(marker), _end_tag(marker)
    # 中身に同じマーカーが紛れ込むと（LLMがマーカーを復唱した場合など）、
    # 次回のupsertが偽の終了タグで区間を誤認しノートを壊す。事前に除去する。
    section_md = section_md.replace(start_tag, "").replace(end_tag, "")
    body = _normalize_section_newlines(section_md, nl)
    wrapped = f"{start_tag}{nl}{body}{nl}{end_tag}"

    start_idx = content.find(start_tag)
    end_idx = content.find(end_tag)
    if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
        # 既存区間は位置を動かさず中身だけ置換（prefix/suffix bytes 不変）
        return content[:start_idx] + wrapped + content[end_idx + len(end_tag):]

    if position == "top":
        insert_at = _frontmatter_end(content)
        before = content[:insert_at]
        after = content[insert_at:]
        # before/after を strip しない。区切り改行は追加のみ。
        pieces: list[str] = []
        if before:
            pieces.append(before)
            if not before.endswith("\n") and not before.endswith("\r"):
                pieces.append(nl)
            # blank line separator if not already blank-ended
            if not (before.endswith(nl + nl) or before.endswith("\n\n")):
                if before.endswith(nl) or before.endswith("\n"):
                    pieces.append(nl)
                else:
                    pieces.append(nl)
        pieces.append(wrapped)
        if after:
            # ensure separation before after
            mid = "".join(pieces)
            if not mid.endswith(nl) and not mid.endswith("\n"):
                pieces.append(nl)
            if not after.startswith("\n") and not after.startswith("\r"):
                pieces.append(nl)
            pieces.append(after)
            return "".join(pieces)
        return "".join(pieces) + nl

    # bottom: 元 content を1バイトも変えず、後ろへ区切り＋区間を追加
    if not content:
        return wrapped + nl
    if content.endswith(nl) or content.endswith("\n") or content.endswith("\r"):
        # 既に改行で終わる → 空行セパレータ1つ + wrapped + 終端改行
        if content.endswith(nl + nl) or content.endswith("\n\n"):
            return content + wrapped + nl
        return content + nl + wrapped + nl
    # 末尾改行なし → 区切り改行を後ろへ追加するだけ（元末尾を削除しない）
    return content + nl + nl + wrapped + nl


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
        if not p.is_file():
            return None
        return read_text_preserve_newlines(p)

    def write_section(
        self,
        day: date,
        marker: str,
        section_md: str,
        position: str = "bottom",
    ) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path_for(day)
        content = self.read(day)
        if content is None:
            content = default_frontmatter(day) + "\n"
        atomic_write_text(
            p, upsert_section(content, marker, section_md, position=position)
        )
        return p
