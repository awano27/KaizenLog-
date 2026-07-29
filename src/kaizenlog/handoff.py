"""エージェント申し送り: 実測教訓を CLAUDE.md / AGENTS.md へ冪等注入する。"""

from __future__ import annotations

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


def _retry_trend_block(stats_list: list[dict], as_of: date) -> str:
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


def _tool_errors_block(stats_list: list[dict]) -> str:
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


def _fail_actions_block(memory_dir: Path, as_of: date) -> str:
    entries = load_entries(memory_dir)
    lines = consecutive_fail_actions(entries, as_of, n=2)
    if not lines:
        return "(計測なし)"
    return "\n".join(f"- {ln}" for ln in lines)


def _skilled_wait_block(
    memory_dir: Path,
    redactor=None,
) -> str:
    ledger = load_prompt_ledger(memory_dir)
    new_ents = [e for e in ledger if e.status == "new"]
    if not new_ents:
        return "(計測なし)"
    top = sorted(new_ents, key=lambda e: (-e.count_total, e.id))[:3]
    lines = []
    for e in top:
        rep = _redact_display(e.representative, redactor)
        if len(rep) > 60:
            rep = rep[:57] + "..."
        lines.append(f"- {e.id} ({e.count_total}回): {rep or '（代表文なし）'}")
    return "\n".join(lines)


def build_agent_context_section(
    *,
    stats_dir: Path,
    memory_dir: Path,
    as_of: date | None = None,
    redactor=None,
) -> str:
    """handoff マーカー区間の本文（マーカー自体は含めない）。"""
    as_of = as_of or date.today()
    stats_list = load_stats(Path(stats_dir), days=30, end_day=as_of)
    header = (
        "このセクションは KaizenLog が実測データから自動生成(再実行で上書き)。"
        f"手動メモはマーカーの外へ。生成日: {as_of.isoformat()}"
    )
    blocks = [
        header,
        "",
        "### リトライ傾向",
        _retry_trend_block(stats_list, as_of),
        "",
        "### 頻出ツールエラー",
        _tool_errors_block(stats_list),
        "",
        "### 連続FAIL中のKZN施策",
        _fail_actions_block(Path(memory_dir), as_of),
        "",
        "### skilled化待ちPRMクラスタ",
        _skilled_wait_block(Path(memory_dir), redactor=redactor),
        "",
    ]
    return "\n".join(blocks).rstrip() + "\n"


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
