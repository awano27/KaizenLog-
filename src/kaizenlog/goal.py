"""日誌の「今日の目標」区間。

所有権: このマーカー区間を書き換えるのは `kaizenlog goal` コマンド
（と morning のプレースホルダのみ）。generate / advise は読むだけ
（手書き編集も保持される）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .vault import (
    ACTIONS_MARKER,
    GOAL_MARKER,
    DailyNoteStore,
    default_frontmatter,
    extract_section,
    upsert_section,
    atomic_write_text,
    _end_tag,
)

# 🎯 今日の目標: <文言> [@カテゴリ]
_GOAL_LINE_RE = re.compile(
    r"^(?:🎯\s*)?今日の目標\s*[:：]\s*(.+?)\s*$",
    re.MULTILINE,
)
_AT_CATEGORY_RE = re.compile(r"\s*@([^\s@]+)\s*$")
# 達成度: NN%（自己申告） または 達成度: NN%
_ACHIEVED_RE = re.compile(
    r"^達成度\s*[:：]\s*(\d{1,3})\s*%(?:\s*（自己申告）)?\s*$",
    re.MULTILINE,
)

GOAL_PLACEHOLDER = "🎯 今日の目標: （未設定 — kaizenlog goal \"...\" で登録）\n"


@dataclass(frozen=True)
class DayGoal:
    """1日の作業目標（構造化）。"""

    text: str  # @カテゴリを除いた目標文言
    category: str | None = None  # config カテゴリと一致した場合のみ
    raw_line: str = ""  # 区間内の元行（表示用）
    achieved: int | None = None  # 0-100 自己申告。未申告は None


def parse_goal_text(
    body: str,
    known_categories: set[str] | frozenset[str] | None = None,
) -> DayGoal | None:
    """目標区間の本文をパース。空なら None。

    @カテゴリは任意。known_categories と完全一致すれば category に載せる。
    不一致でも警告せず、@以降を text から外して category=None にする
    （不一致カテゴリは構造化しないが、表示上 @ 付きで残したい場合は raw_line を使う）。
    達成度行があれば 0-100 にクランプして achieved に載せる。
    """
    if not body or not body.strip():
        return None

    achieved: int | None = None
    m_ach = _ACHIEVED_RE.search(body)
    if m_ach:
        try:
            n = int(m_ach.group(1))
            if 0 <= n <= 100:
                achieved = n
        except ValueError:
            achieved = None

    # プレースホルダは「未設定」扱い
    if "（未設定" in body and "kaizenlog goal" in body:
        return None

    m = _GOAL_LINE_RE.search(body.strip())
    if not m:
        # 1行だけの自由文も許容（マーカー内の主文）
        line = body.strip().splitlines()[0].strip()
        if not line:
            return None
        text = line
        # 先頭の絵文字ラベルを剥がす
        text = re.sub(r"^🎯\s*", "", text)
        text = re.sub(r"^今日の目標\s*[:：]\s*", "", text).strip()
    else:
        text = m.group(1).strip()
        line = m.group(0).strip()

    category: str | None = None
    at = _AT_CATEGORY_RE.search(text)
    if at:
        cand = at.group(1).strip()
        text_core = text[: at.start()].strip()
        cats = set(known_categories or ())
        if cand in cats:
            category = cand
            text = text_core
        else:
            # 不一致: 構造化せず text から @ を外す（断定しない・警告しない）
            text = text_core
    if not text:
        return None
    return DayGoal(
        text=text,
        category=category,
        raw_line=line if m else body.strip().splitlines()[0].strip(),
        achieved=achieved,
    )


def format_goal_section(
    text: str,
    category: str | None = None,
    *,
    achieved: int | None = None,
) -> str:
    """目標区間の中身（マーカー除く）を生成。"""
    body = (text or "").strip()
    if category:
        body = f"{body} @{category}"
    lines = [f"🎯 今日の目標: {body}"]
    if achieved is not None:
        n = max(0, min(100, int(achieved)))
        lines.append(f"達成度: {n}%（自己申告）")
    return "\n".join(lines) + "\n"


def read_goal(
    content: str | None,
    known_categories: set[str] | frozenset[str] | None = None,
) -> DayGoal | None:
    """ノート全文から目標を読む。generate/advise は読むだけ。"""
    if not content:
        return None
    section = extract_section(content, GOAL_MARKER)
    if section is None:
        return None
    return parse_goal_text(section, known_categories)


def upsert_goal_in_content(content: str, section_md: str) -> str:
    """目標マーカー区間を upsert。

    区間が無ければ 📌 アクション区間（ACTIONS_MARKER）の直後に挿入。
    アクション区間も無ければ frontmatter 直後（top）。
    """
    # 既存 goal があれば位置固定で置換
    start_tag = f"<!-- {GOAL_MARKER}:start -->"
    if start_tag in content:
        return upsert_section(content, GOAL_MARKER, section_md, position="top")

    actions_end = _end_tag(ACTIONS_MARKER)
    idx = content.find(actions_end)
    if idx != -1:
        insert_at = idx + len(actions_end)
        wrapped = (
            f"<!-- {GOAL_MARKER}:start -->\n"
            f"{section_md.rstrip()}\n"
            f"<!-- {GOAL_MARKER}:end -->"
        )
        before = content[:insert_at].rstrip("\n")
        after = content[insert_at:].lstrip("\n")
        parts = [before, wrapped]
        if after:
            parts.append(after)
        return "\n\n".join(parts) + ("\n" if not content.endswith("\n") and not after else "")

    return upsert_section(content, GOAL_MARKER, section_md, position="top")


def write_goal(
    daily_notes_dir: Path,
    day: date,
    text: str,
    *,
    category: str | None = None,
    known_categories: set[str] | frozenset[str] | None = None,
    achieved: int | None = None,
) -> tuple[Path, DayGoal]:
    """goal コマンド専用の書き込み。"""
    # text 内の @カテゴリも解釈（CLI 引数と二重指定なら text 側を優先解釈）
    combined = text if not category else f"{text} @{category}"
    parsed = parse_goal_text(
        f"🎯 今日の目標: {combined}",
        known_categories,
    )
    if parsed is None:
        raise ValueError("目標文が空です")
    # CLI の --achieved を優先（parse には含めない）
    if achieved is not None:
        n = int(achieved)
        if not (0 <= n <= 100):
            raise ValueError("達成度は 0〜100 の整数で指定してください")
        parsed = DayGoal(
            text=parsed.text,
            category=parsed.category,
            raw_line=parsed.raw_line,
            achieved=n,
        )
    section = format_goal_section(
        parsed.text, parsed.category, achieved=parsed.achieved
    )
    store = DailyNoteStore(daily_notes_dir)
    store.dir.mkdir(parents=True, exist_ok=True)
    path = store.path_for(day)
    content = store.read(day)
    if content is None:
        content = default_frontmatter(day) + "\n"
    updated = upsert_goal_in_content(content, section)
    atomic_write_text(path, updated)
    return path, parsed


def write_goal_achieved(
    daily_notes_dir: Path,
    day: date,
    achieved: int,
    *,
    known_categories: set[str] | frozenset[str] | None = None,
) -> tuple[Path, DayGoal]:
    """既存目標に達成度だけ追記。目標未設定ならエラー。"""
    n = int(achieved)
    if not (0 <= n <= 100):
        raise ValueError("達成度は 0〜100 の整数で指定してください")
    store = DailyNoteStore(daily_notes_dir)
    content = store.read(day)
    existing = read_goal(content, known_categories)
    if existing is None or not existing.text.strip():
        raise ValueError(
            '目標が未設定です。先に `kaizenlog goal "..."` で登録してください'
        )
    section = format_goal_section(
        existing.text, existing.category, achieved=n
    )
    store.dir.mkdir(parents=True, exist_ok=True)
    path = store.path_for(day)
    if content is None:
        content = default_frontmatter(day) + "\n"
    updated = upsert_goal_in_content(content, section)
    atomic_write_text(path, updated)
    return path, DayGoal(
        text=existing.text,
        category=existing.category,
        raw_line=existing.raw_line,
        achieved=n,
    )


def ensure_goal_placeholder(daily_notes_dir: Path, day: date) -> Path | None:
    """GOAL 区間が無ければプレースホルダを書く。既存があれば触らない。

    戻り値: 書いたパス。既存あり / 失敗時は None。
    """
    store = DailyNoteStore(daily_notes_dir)
    store.dir.mkdir(parents=True, exist_ok=True)
    path = store.path_for(day)
    content = store.read(day)
    if content is None:
        content = default_frontmatter(day) + "\n"
    if extract_section(content, GOAL_MARKER) is not None:
        return None
    updated = upsert_goal_in_content(content, GOAL_PLACEHOLDER)
    atomic_write_text(path, updated)
    return path


def goal_stats_fields(
    day_goal: DayGoal | None,
    redactor=None,
) -> tuple[str | None, str | None, int | None]:
    """stats 保存用の (goal_text, goal_category, goal_achieved)。

    原文をボールト外（stats/LLM）に広げないため、text は redact 適用後のみ
    返す。cli の generate から呼ばれる唯一の経路であり、redact の適用漏れは
    ここのテストで検知する。goal_achieved は後方互換の追加キー。
    """
    if day_goal is None:
        return None, None, None
    text = day_goal.text
    if redactor is not None:
        text = redactor(text)
    ach = day_goal.achieved
    if ach is not None:
        ach = max(0, min(100, int(ach)))
    return text, day_goal.category, ach
