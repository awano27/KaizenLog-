"""冒頭30秒サマリ（kaizenlog:digest）。決定論のみ・LLM文禁止。"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from datetime import date, tzinfo
from typing import Any, Callable

from .aiwork import top_friction_sessions
from .baseline import baseline, format_with_baseline
from .memory import MemoryEntry
from .report import _fmt_minutes
from .vault import (
    ACTIONS_MARKER,
    ADVICE_MARKER,
    NIPPOU_MARKER,
    WEEKLY_CONTEXT_MARKER,
)

# digest 自身が生成する固定文言のみ評価語検査対象（外部由来は行ごとスキップ）
_BANNED_EVAL = ("良い", "悪い", "改善")
_ROUND_RE = re.compile(r"\s*\(round\s+\d+\)\s*", re.IGNORECASE)


def _has_banned(text: str) -> bool:
    return any(b in text for b in _BANNED_EVAL)


def _truncate_subject(subj: str, limit: int = 80) -> str:
    s = " ".join(str(subj).split())
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


def _effort_top_projects(
    stats: Mapping[str, Any],
    *,
    limit: int = 2,
) -> list[tuple[str, float]]:
    effort = stats.get("effort")
    if not isinstance(effort, Mapping):
        return []
    mins = effort.get("minutes")
    if not isinstance(mins, Mapping):
        return []
    skip = {
        "（私的）",
        "（AI・汎用相談）",
        "（未分類）",
        "（調査・共通）",
        "（ほか）",
    }
    items = [
        (str(k), float(v or 0))
        for k, v in mins.items()
        if str(k) not in skip and float(v or 0) > 0
    ]
    items.sort(key=lambda x: (-x[1], x[0]))
    return items[:limit]


def _commits_by_label(stats: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    og = stats.get("outcome_git")
    if not isinstance(og, list):
        return out
    for item in og:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("repo_label") or "")
        if label:
            out[label] = out.get(label, 0) + int(item.get("commits") or 0)
    return out


def _friction_what_happened(d: Mapping[str, Any]) -> str:
    """タイトルではなく「何が起きたか」を数値で。"""
    err = int(d.get("tool_errors") or 0)
    tools = d.get("tools_total")
    edits = d.get("edits")
    bits: list[str] = []
    source = str(d.get("source") or "").strip()
    project = str(d.get("project") or "").strip()
    head = project or "—"
    if source and source not in head:
        head = f"{head} ({source})" if head != "—" else source
    if isinstance(tools, (int, float)) and float(tools) > 0:
        bits.append(f"ツールエラー{err}/{int(tools)}回")
    elif err > 0:
        bits.append(f"ツールエラー{err}回")
    if isinstance(edits, (int, float)):
        bits.append(f"変更{int(edits)}件で終了")
    if not bits:
        bits.append("摩擦あり")
    return f"{head} " + " — ".join(bits)


def build_digest(
    stats: Mapping[str, Any] | None,
    entries: Sequence[MemoryEntry],
    *,
    today: date,
    tz: tzinfo | None = None,
    redactor: Callable[[str], str] | None = None,
    existing_markers: Collection[str] | None = None,
    goal_text: str | None = None,
    commit_stats: Sequence[Any] | None = None,
    stats_history: Sequence[Mapping[str, Any]] | None = None,
) -> str | None:
    """当日 verified stats から決定論サマリを組み立てる。

    順序: 稼働(+基準線) → 目標 → 手を動かした先 → 摩擦 → 今日の1手 → 詳細

    redactor が無いとき:
      - 目標・今日の1手の自由文は行ごとスキップ
      - 摩擦は数値部分のみ出す（タイトルは付けない）
    目標以外に評価語があれば digest 全体を None にする。
    stats 由来行が1本も立たない場合は None。
    """
    if not isinstance(stats, Mapping):
        return None
    if stats.get("source_status") != "verified":
        if "source_status" in stats:
            return None
        if not isinstance(stats.get("activity_sha256"), str):
            return None

    lines: list[str] = ["## ⏱ 30秒サマリ", ""]
    stats_derived = 0
    history = [h for h in (stats_history or []) if isinstance(h, Mapping)]
    # 当日を除く
    today_s = today.isoformat()
    prior = [h for h in history if str(h.get("day") or "") != today_s]

    # 稼働 + AI作業 + 基準線
    total = stats.get("total_minutes")
    ai_min = None
    by_cat = stats.get("by_category")
    if isinstance(by_cat, Mapping):
        raw_ai = by_cat.get("AI作業")
        if isinstance(raw_ai, (int, float)):
            ai_min = float(raw_ai)
    if isinstance(total, (int, float)):
        total_f = float(total)
        work_bits = [f"稼働 {_fmt_minutes(total_f)}"]
        if ai_min is not None and total_f > 0:
            pct = int(round(ai_min / total_f * 100))
            work_bits[0] = (
                f"稼働 {_fmt_minutes(total_f)}"
                f"（AI作業 {_fmt_minutes(ai_min)}・{pct}%）"
            )
        elif ai_min is not None:
            work_bits[0] = (
                f"稼働 {_fmt_minutes(total_f)}（AI作業 {_fmt_minutes(ai_min)}）"
            )
        med, label = baseline(prior, "total_minutes", today_value=total_f)
        if med is not None and label:
            work_bits.append(f"直近7日中央値 {_fmt_minutes(med)} の {label}")
            lines.append("- " + " / ".join(work_bits))
        else:
            lines.append("- " + work_bits[0])
        stats_derived += 1
    elif ai_min is not None:
        lines.append(f"- AI作業: {_fmt_minutes(ai_min)}")
        stats_derived += 1

    if stats_derived == 0:
        return None

    # 目標（redactor 必須 — 自由記述）
    if goal_text and str(goal_text).strip() and redactor is not None:
        g = redactor(str(goal_text).strip())
        if g:
            lines.append(f"- 目標: {g}")
    elif goal_text and str(goal_text).strip() and redactor is None:
        # redact 無効設定（patterns=[] → redactor=None）でも目標は出す
        # 素通しは「秘匿パターン無し」の意味。評価語検査は目標対象外。
        lines.append(f"- 目標: {str(goal_text).strip()}")

    # 手を動かした先（effort 上位2 + コミット数）
    tops = _effort_top_projects(stats, limit=2)
    commits = _commits_by_label(stats)
    # commit_stats 引数があればマージ
    if commit_stats:
        for s in commit_stats:
            label = getattr(s, "repo_label", None) or "repo"
            n = int(getattr(s, "commits", 0) or 0)
            if n:
                commits[str(label)] = commits.get(str(label), 0) + n
    if tops:
        parts: list[str] = []
        for name, mins in tops:
            c_n = commits.get(name, 0)
            if c_n:
                parts.append(f"{name} {_fmt_minutes(mins)}（コミット{c_n}件）")
            else:
                parts.append(f"{name} {_fmt_minutes(mins)}")
        lines.append("- 手を動かした先: " + "・".join(parts))
        stats_derived += 1
    elif commits:
        # effort 無しでも outcome_git があれば成果行
        parts = [f"{lab} コミット{n}件" for lab, n in list(commits.items())[:2]]
        if parts:
            lines.append("- 手を動かした先: " + "・".join(parts))

    # 今日いちばんの摩擦（数値は redactor 無しでも出す）
    ai = stats.get("ai") if isinstance(stats.get("ai"), Mapping) else {}
    digests = ai.get("session_digests") if isinstance(ai, Mapping) else None
    if isinstance(digests, list) and digests:
        worst = top_friction_sessions(digests, limit=1)
        if worst:
            d0 = worst[0]
            what = _friction_what_happened(d0)
            if _has_banned(what):
                return None
            if redactor is not None:
                title = redactor(str(d0.get("title") or "").strip())
                if title and not _has_banned(title):
                    # タイトルは末尾に短く
                    t = title if len(title) <= 24 else title[:23] + "…"
                    what = f"{what}「{t}」"
                elif title and _has_banned(title):
                    return None
            lines.append(f"- 今日いちばんの摩擦: {what}")

    # 今日の1手（1件・自由文のため redactor 必須）
    open_entries = [e for e in entries if e.status == "proposed"]
    if open_entries and redactor is not None:
        latest = sorted(open_entries, key=lambda e: e.id, reverse=True)[0]
        body = " ".join((latest.action or "").split())
        snippet = body if len(body) <= 40 else body[:39] + "…"
        snippet = redactor(snippet)
        if snippet:
            lines.append(f"- 今日の1手: {latest.id} {snippet}")

    # 内部リンク
    markers = set(existing_markers or ())
    link_bits: list[str] = []
    if ADVICE_MARKER in markers:
        link_bits.append("🚀提案")
    if ACTIONS_MARKER in markers:
        link_bits.append("📌アクション")
    if NIPPOU_MARKER in markers:
        link_bits.append("📝日報")
    if WEEKLY_CONTEXT_MARKER in markers:
        link_bits.append("📊週次")
    if link_bits:
        lines.append("- 詳細: " + " / ".join(link_bits))

    body_lines = [ln for ln in lines if ln.startswith("- ")]
    if not body_lines:
        return None
    return "\n".join(lines) + "\n"
