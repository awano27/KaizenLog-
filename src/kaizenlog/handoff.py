"""エージェント申し送り: 実測教訓を CLAUDE.md / AGENTS.md へ冪等注入する。"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from .memory import consecutive_fail_actions, load_entries
from .promptledger import _redact_display, load_prompt_ledger
from .stats import load_stats
from .vault import (
    AGENT_CONTEXT_MARKER,
    atomic_write_text,
    read_text_preserve_newlines,
    upsert_section,
)
from .handoffledger import (
    HandoffLesson,
    load_handoff_ledger,
    promoted_lesson_ids,
    record_lessons_on_apply,
    suppressed_ids_for_target,
)


def _retry_trend_text(stats_list: list[dict], as_of: date) -> str:
    """リトライ傾向 1-2 行。データ無しは計測なし。"""
    if not stats_list:
        return "(計測なし)"
    total = 0
    by_day: dict[str, int] = {}
    for s in stats_list:
        ai = s.get("ai") if isinstance(s.get("ai"), dict) else {}
        n = int(ai.get("retry_chains") or 0)
        total += n
        day = str(s.get("day") or "")
        if day:
            by_day[day] = n
    if not by_day and total == 0:
        return "(計測なし)"
    d7 = (as_of - timedelta(days=6)).isoformat()
    d14 = (as_of - timedelta(days=13)).isoformat()
    recent = sum(v for d, v in by_day.items() if d >= d7)
    prev = sum(v for d, v in by_day.items() if d14 <= d < d7)
    if recent > prev:
        trend = "増"
    elif recent < prev:
        trend = "減"
    else:
        trend = "横ばい"
    return (
        f"直近30日のリトライ連鎖: 合計 {total} 件。"
        f"直近7日は {recent} 件（前週比: {trend}）。"
    )


def _tool_errors_text(stats_list: list[dict]) -> str:
    if not stats_list:
        return "(計測なし)"
    total = 0
    peak_day = ""
    peak_n = 0
    any_data = False
    for s in stats_list:
        ai = s.get("ai") if isinstance(s.get("ai"), dict) else {}
        if "tool_errors" not in ai:
            continue
        any_data = True
        n = int(ai.get("tool_errors") or 0)
        total += n
        day = str(s.get("day") or "")
        if n > peak_n:
            peak_n = n
            peak_day = day
    if not any_data:
        return "(計測なし)"
    peak_s = f"{peak_day} に {peak_n} 件" if peak_day else f"最大 {peak_n} 件/日"
    return f"ツールエラー: 30日合計 {total} 件（最多日: {peak_s}）。"


def collect_handoff_lessons(
    *,
    stats_dir: Path,
    memory_dir: Path,
    as_of: date | None = None,
    redactor=None,
    suppress_ids: set[str] | None = None,
) -> list[HandoffLesson]:
    """安定ID付きレッスン一覧を決定論生成。"""
    as_of = as_of or date.today()
    suppress_ids = suppress_ids or set()
    stats_list = load_stats(Path(stats_dir), days=30, end_day=as_of)
    lessons: list[HandoffLesson] = []

    # retry / toolerr はブロック全体で1レッスン
    if "HND-retry-trend" not in suppress_ids:
        lessons.append(
            HandoffLesson(
                lesson_id="HND-retry-trend",
                kind="retry",
                ref_id="retry-trend",
                text=_retry_trend_text(stats_list, as_of),
            )
        )
    if "HND-tool-errors" not in suppress_ids:
        lessons.append(
            HandoffLesson(
                lesson_id="HND-tool-errors",
                kind="toolerr",
                ref_id="tool-errors",
                text=_tool_errors_text(stats_list),
            )
        )

    # KZN 連続FAIL
    for line in consecutive_fail_actions(load_entries(memory_dir), as_of, n=2):
        m = re.search(r"(KZN-\d{8}-\d+)", line)
        if not m:
            continue
        kid = m.group(1)
        lid = f"HND-kzn-{kid}"
        if lid in suppress_ids:
            continue
        lessons.append(
            HandoffLesson(
                lesson_id=lid,
                kind="kzn",
                ref_id=kid,
                text=line,
            )
        )

    # skilled 待ち PRM
    ledger = load_prompt_ledger(memory_dir)
    new_ents = [e for e in ledger if e.status == "new"]
    top = sorted(new_ents, key=lambda e: (-e.count_total, e.id))[:3]
    for e in top:
        lid = f"HND-prm-{e.id}"
        if lid in suppress_ids:
            continue
        rep = _redact_display(e.representative, redactor)
        if len(rep) > 60:
            rep = rep[:57] + "..."
        lessons.append(
            HandoffLesson(
                lesson_id=lid,
                kind="prm",
                ref_id=e.id,
                text=f"{e.id} ({e.count_total}回): {rep or '（代表文なし）'}",
            )
        )
    return lessons


def build_agent_context_with_lessons(
    *,
    stats_dir: Path,
    memory_dir: Path,
    as_of: date | None = None,
    redactor=None,
    target: str | Path | None = None,
    include_promoted_exclude: bool = True,
) -> tuple[str, list[HandoffLesson]]:
    """handoff マーカー区間の本文とレッスン一覧。

    target 指定時: 台帳の suppressed/promoted を除外。
    include_promoted_exclude: promoted レッスンを各 target から除外（重複防止）。
    """
    as_of = as_of or date.today()
    suppress: set[str] = set()
    if target is not None:
        ledger = load_handoff_ledger(memory_dir)
        suppress |= suppressed_ids_for_target(ledger, target)
        if include_promoted_exclude:
            suppress |= promoted_lesson_ids(ledger)

    lessons = collect_handoff_lessons(
        stats_dir=stats_dir,
        memory_dir=memory_dir,
        as_of=as_of,
        redactor=redactor,
        suppress_ids=suppress,
    )
    header = (
        "このセクションは KaizenLog が実測データから自動生成(再実行で上書き)。"
        f"手動メモはマーカーの外へ。生成日: {as_of.isoformat()}"
    )
    by_id = {les.lesson_id: les for les in lessons}

    def block_or_none(lid: str, title: str) -> list[str]:
        les = by_id.get(lid)
        if les is None:
            return []  # suppressed またはデータなし → ブロックごと省略
        return ["", f"### {title}", les.text]

    parts: list[str] = [header]
    parts.extend(block_or_none("HND-retry-trend", "リトライ傾向"))
    parts.extend(block_or_none("HND-tool-errors", "頻出ツールエラー"))

    kzn_lines = [f"- {les.text}" for les in lessons if les.kind == "kzn"]
    # 元データ無し / 全抑制 → 見出し + (計測なし) で冪等維持
    parts.extend(["", "### 連続FAIL中のKZN施策"])
    if kzn_lines:
        parts.extend(kzn_lines)
    else:
        parts.append("(計測なし)")

    prm_lines = [f"- {les.text}" for les in lessons if les.kind == "prm"]
    parts.extend(["", "### skilled化待ちPRMクラスタ"])
    if prm_lines:
        parts.extend(prm_lines)
    else:
        parts.append("(計測なし)")

    section = "\n".join(parts).rstrip() + "\n"
    return section, lessons


def build_agent_context_section(
    *,
    stats_dir: Path,
    memory_dir: Path,
    as_of: date | None = None,
    redactor=None,
    target: str | Path | None = None,
    include_promoted_exclude: bool = True,
) -> str:
    """handoff マーカー区間の本文（後方互換: str のみ返す）。"""
    section, _ = build_agent_context_with_lessons(
        stats_dir=stats_dir,
        memory_dir=memory_dir,
        as_of=as_of,
        redactor=redactor,
        target=target,
        include_promoted_exclude=include_promoted_exclude,
    )
    return section


def apply_handoff(
    target: Path,
    section_md: str,
    *,
    dry_run: bool = False,
) -> str:
    """target ファイルへ agent-context 区間を upsert。dry_run 時は書かない。

    戻り値: 適用後（または適用予定）の全文。
    """
    target = Path(target)
    if target.is_file():
        content = read_text_preserve_newlines(target)
    else:
        content = ""
    updated = upsert_section(
        content, AGENT_CONTEXT_MARKER, section_md, position="bottom"
    )
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, updated)
    return updated


def run_handoff_for_target(
    *,
    target: Path,
    stats_dir: Path,
    memory_dir: Path,
    as_of: date,
    redactor=None,
    dry_run: bool = False,
) -> tuple[str, list[HandoffLesson]]:
    """1 target 分を生成・(非 dry_run なら)書き込み+台帳記録。"""
    section, lessons = build_agent_context_with_lessons(
        stats_dir=stats_dir,
        memory_dir=memory_dir,
        as_of=as_of,
        redactor=redactor,
        target=target,
    )
    apply_handoff(target, section, dry_run=dry_run)
    if not dry_run:
        record_lessons_on_apply(
            memory_dir, target=target, lessons=lessons, as_of=as_of
        )
    return section, lessons
