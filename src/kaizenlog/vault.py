"""Obsidianボールトのデイリーノートへの書き込み。

既存ノートを壊さないよう、マーカーで囲んだ区間だけを更新（upsert）する。
マーカー外の既存bytes（末尾空白・改行コード含む）は変更しない。
"""

from __future__ import annotations

import os
import re
import time
from datetime import date
from pathlib import Path

_FOOTNOTE_DEF_RE = re.compile(r"^\[\^(\d+)\]:\s*(.*)$")
_FOOTNOTE_REF_RE = re.compile(r"\[\^(\d+)\]")

# Obsidian 等が .md を掴んでいるときの WinError 5 / PermissionError 対策
_REPLACE_RETRIES = 8
_REPLACE_BASE_SLEEP_SEC = 0.05


def _os_replace_with_retry(tmp: Path, path: Path) -> None:
    """os.replace を短い間隔で再試行する（Windows ファイルロック耐性）。"""
    last: BaseException | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last = e
        except OSError as e:
            # WinError 5 は環境により OSError のこともある
            winerr = getattr(e, "winerror", None)
            if winerr == 5 or e.errno in (13, 11):  # EACCES / EAGAIN
                last = e
            else:
                raise
        time.sleep(_REPLACE_BASE_SLEEP_SEC * (attempt + 1))
    assert last is not None
    # 失敗時は .tmp を残して原因調査しやすくする（成功時は replace で消える）
    raise last


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """一時ファイルへbytesを書いて os.replace で置き換える。"""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    _os_replace_with_retry(tmp, path)


def atomic_write_text(path: Path, content: str, *, newline: str | None = None) -> None:
    """一時ファイルに書いてから os.replace で置き換えるアトミック書き込み。

    newline=None の既定は newline=\"\"（変換なし）。明示指定時はその newline で書く。
    既存呼び出しの既定動作は「渡した content をそのまま書く」。
    PermissionError / WinError 5 時は短い sleep のうえ再試行する。
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    # newline=\"\" は改行変換なし。None も変換なし（content 内の改行をそのまま）。
    nl = "" if newline is None else newline
    with open(tmp, "w", encoding="utf-8", newline=nl) as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    _os_replace_with_retry(tmp, path)


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
GOAL_MARKER = "kaizenlog:goal"  # 今日の作業目標（所有: goal コマンド + morning プレースホルダ。generate/advise は読取）
WEEKLY_CONTEXT_MARKER = "kaizenlog:weekly-context"  # 週次スコアカード（決定論）
AGENT_CONTEXT_MARKER = "kaizenlog:agent-context"  # handoff が CLAUDE.md/AGENTS.md へ注入
COACH_MARKER = "kaizenlog:coach"  # coach --apply が追記する調教区間
DIGEST_MARKER = "kaizenlog:digest"  # 冒頭30秒サマリ（決定論・generate/advise が所有）
RESUME_MARKER = "kaizenlog:resume"  # きのうの続きから（決定論・generate が翌日ノートへ）
DECISION_MARKER = "kaizenlog:decision"  # 朝決算カード（決定論・morning/generate）
EFFORT_MARKER = "kaizenlog:effort"  # 工数のつけ先（決定論・generate が所有）
EXPERIMENTS_MARKER = "kaizenlog:experiments"  # 進行中の実験1行（決定論・generate が所有）
REFLECT_MARKER = "kaizenlog:reflect"  # 夜の内省3問（決定論・generate が所有）
MONTHLY_MARKER = "kaizenlog:monthly"  # 月次実績（決定論・monthly が所有）
NIPPOU_MARKER = "kaizenlog:nippou"  # 日報ドラフト
FOOTNOTES_MARKER = "kaizenlog:footnotes"  # 免責注釈の集約

# 日誌区間の正準順序（未知マーカー・手書き・frontmatter は触らない）
SECTION_ORDER: tuple[str, ...] = (
    DIGEST_MARKER,
    RESUME_MARKER,
    DECISION_MARKER,
    GOAL_MARKER,
    ACTIONS_MARKER,
    ADVICE_MARKER,
    NIPPOU_MARKER,
    EFFORT_MARKER,
    EXPERIMENTS_MARKER,
    WEEKLY_CONTEXT_MARKER,
    ACTIVITY_MARKER,
    REFLECT_MARKER,
    COACH_MARKER,
    FOOTNOTES_MARKER,
)


def _start_tag(marker: str) -> str:
    return f"<!-- {marker}:start -->"


def _end_tag(marker: str) -> str:
    return f"<!-- {marker}:end -->"


def _find_section_span(content: str, marker: str) -> tuple[int, int] | None:
    """マーカー区間の [start_tag 開始, end_tag 終了直後) を返す。無ければ None。"""
    start_tag, end_tag = _start_tag(marker), _end_tag(marker)
    start_idx = content.find(start_tag)
    if start_idx < 0:
        return None
    end_idx = content.find(end_tag, start_idx + len(start_tag))
    if end_idx < 0:
        return None
    return start_idx, end_idx + len(end_tag)


def reorder_sections(content: str) -> str:
    """既知マーカー区間だけを SECTION_ORDER 順に並べ替える。

    - frontmatter / 未知マーカー / 手書き本文の**内容**は変えない・複製しない
    - 既知区間は「最初の既知区間があった位置」に正準順でまとめて置き直す
    - 区間の間にあった非空白テキストは ordered ブロック直後に残す
    - べき等（2回適用で差分ゼロ）
    """
    if not content:
        return content
    nl = detect_newline(content)
    found: list[tuple[int, int, str, str]] = []
    for marker in SECTION_ORDER:
        span = _find_section_span(content, marker)
        if span is None:
            continue
        s, e = span
        found.append((s, e, marker, content[s:e]))
    if not found:
        return content
    found.sort(key=lambda x: x[0])

    by_marker = {m: b for _s, _e, m, b in found}
    ordered_blocks = [by_marker[m] for m in SECTION_ORDER if m in by_marker]
    ordered_blob = (nl + nl).join(ordered_blocks)

    first_s = found[0][0]
    last_e = found[-1][1]
    prefix = content[:first_s]
    suffix = content[last_e:]

    # 区間の間の手書き（空白のみは捨てて、セパレータは ordered_blob 側）
    hand_bits: list[str] = []
    for i in range(len(found) - 1):
        gap = content[found[i][1] : found[i + 1][0]]
        if gap.strip():
            # 前後の余分な空行を1つに
            hand_bits.append(gap.strip("\r\n") + nl)

    mid = ""
    if hand_bits:
        mid = nl + nl.join(hand_bits)
        if not mid.endswith(nl):
            mid += nl

    # prefix が区間直前で空行無しなら区切りを足す
    pieces = [prefix]
    if prefix and not prefix.endswith(nl) and not prefix.endswith("\n"):
        pieces.append(nl)
    if prefix and not (prefix.endswith(nl + nl) or prefix.endswith("\n\n")):
        if prefix.endswith(nl) or prefix.endswith("\n"):
            pieces.append(nl)
    pieces.append(ordered_blob)
    if mid:
        if not ordered_blob.endswith(nl):
            pieces.append(nl)
        pieces.append(nl)
        pieces.append(mid.lstrip("\r\n") if mid.startswith(nl) else mid)
    if suffix:
        if not ("".join(pieces)).endswith(nl) and not suffix.startswith("\n") and not suffix.startswith("\r"):
            pieces.append(nl)
        pieces.append(suffix)
    return "".join(pieces)


def consolidate_disclaimers(content: str, *, max_inline: int = 1) -> str:
    """各既知区間の ※ 始まり行を最大 max_inline 本残し、残りを footnotes へ。

    脚注ブロックは毎回ゼロから再構築する:
    - 本文に参照が残る注のみ保持（orphan 定義は破棄）
    - 同一文面は1定義に統合して複数参照で共有
    - 番号は 1 から振り直し
    べき等（同一ノートに2回適用しても本文・脚注が変化しない）。
    """
    if not content or max_inline < 0:
        return content
    nl = detect_newline(content)

    existing_defs: dict[int, str] = {}
    existing_fn = extract_section(content, FOOTNOTES_MARKER)
    if existing_fn:
        for line in existing_fn.splitlines():
            m = _FOOTNOTE_DEF_RE.match(line.strip())
            if m and m.group(2).strip():
                existing_defs[int(m.group(1))] = m.group(2).strip()

    # 初出順のユニーク注釈文面 → 最終番号は 1 始まり
    ref_texts: list[str] = []
    text_to_num: dict[str, int] = {}

    def _ref_for(text: str) -> str:
        key = text.strip()
        if key not in text_to_num:
            text_to_num[key] = len(ref_texts) + 1
            ref_texts.append(key)
        return f"[^{text_to_num[key]}]"

    def _rewrite_refs(line: str) -> str:
        def repl(m: re.Match[str]) -> str:
            n = int(m.group(1))
            text = existing_defs.get(n)
            if text is None:
                # 定義の無い参照は破棄（汚染ノートの orphan 参照掃除）
                return ""
            return _ref_for(text)

        return _FOOTNOTE_REF_RE.sub(repl, line)

    def _process_body(body: str) -> str:
        lines = body.splitlines()
        out: list[str] = []
        seen = 0
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("※"):
                note = stripped[1:].lstrip()
                # T5: 空の ※ は脚注化しない（空定義は再読込で落ち冪等性が壊れる）
                if not note:
                    out.append(line)
                    continue
                seen += 1
                if seen <= max_inline:
                    out.append(line)
                else:
                    indent = line[: len(line) - len(line.lstrip())]
                    out.append(f"{indent}{_ref_for(note)}")
            else:
                out.append(_rewrite_refs(line))
        return "\n".join(out)

    updated = content
    for marker in SECTION_ORDER:
        if marker == FOOTNOTES_MARKER:
            continue
        span = _find_section_span(updated, marker)
        if span is None:
            continue
        s, e = span
        block = updated[s:e]
        start_tag, end_tag = _start_tag(marker), _end_tag(marker)
        inner_start = len(start_tag)
        rest = block[inner_start:]
        if rest.startswith("\r\n"):
            head_nl, rest = "\r\n", rest[2:]
        elif rest.startswith("\n"):
            head_nl, rest = "\n", rest[1:]
        else:
            head_nl = ""
        if not rest.endswith(end_tag):
            continue
        body = rest[: -len(end_tag)]
        body_stripped = body
        trailing = ""
        if body_stripped.endswith("\r\n"):
            trailing = "\r\n"
            body_stripped = body_stripped[:-2]
        elif body_stripped.endswith("\n"):
            trailing = "\n"
            body_stripped = body_stripped[:-1]
        new_body = _process_body(body_stripped.replace("\r\n", "\n"))
        if nl == "\r\n":
            new_body = new_body.replace("\n", "\r\n")
        new_block = f"{start_tag}{head_nl}{new_body}{trailing}{end_tag}"
        updated = updated[:s] + new_block + updated[e:]

    if not ref_texts:
        # 参照が無ければ footnotes 区間ごと除去（孤児定義の掃除）
        span = _find_section_span(updated, FOOTNOTES_MARKER)
        if span is None:
            return updated
        s, e = span
        before = updated[:s].rstrip("\r\n")
        after = updated[e:].lstrip("\r\n")
        if before and after:
            return before + nl + nl + after
        return before + (nl if before and not before.endswith("\n") else "") + after

    fn_lines = ["## 注釈", ""]
    for i, text in enumerate(ref_texts, 1):
        fn_lines.append(f"[^{i}]: {text}")
    fn_body = "\n".join(fn_lines) + "\n"
    return upsert_section(updated, FOOTNOTES_MARKER, fn_body, position="bottom")


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
