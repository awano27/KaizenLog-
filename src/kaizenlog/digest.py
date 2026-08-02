"""冒頭30秒サマリ（kaizenlog:digest）。決定論のみ・LLM文禁止。"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import date, tzinfo
from typing import Any, Callable

from .aiwork import top_friction_sessions
from .memory import MemoryEntry
from .report import _fmt_minutes
from .vault import (
    ACTIONS_MARKER,
    ADVICE_MARKER,
    WEEKLY_CONTEXT_MARKER,
)

# digest 自身が生成する固定文言のみ評価語検査対象（外部由来は行ごとスキップ）
_BANNED_EVAL = ("良い", "悪い", "改善")


def _has_banned(text: str) -> bool:
    return any(b in text for b in _BANNED_EVAL)


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
) -> str | None:
    """当日 verified stats から決定論サマリを組み立てる。

    順序: 稼働 → AI作業 → 目標 → 成果 → 摩擦ワースト → 提案件数 → 今日の1手 → 詳細

    redactor が無いとき: 外部由来文字列を含む行(摩擦・目標・今日の1手)は
    行ごとスキップ。stats 由来の決定論行は出す（素通し redact は禁止）。

    目標行はユーザー自由記述のため評価語自己検査の対象外。
    目標以外（摩擦 title 等）に評価語があれば digest 全体を None にする。

    stats 由来行が1本も立たない場合は None（区間を新設しない）。
    """
    if not isinstance(stats, Mapping):
        return None
    if stats.get("source_status") != "verified":
        if "source_status" in stats:
            return None
        if not isinstance(stats.get("activity_sha256"), str):
            return None

    lines: list[str] = ["## ⏱ 30秒サマリ", ""]
    stats_derived = 0  # 稼働・AI作業など stats 由来行数

    # 稼働合計
    total = stats.get("total_minutes")
    if isinstance(total, (int, float)):
        lines.append(f"- 稼働: {_fmt_minutes(float(total))}")
        stats_derived += 1

    # AI作業分
    by_cat = stats.get("by_category")
    if isinstance(by_cat, Mapping):
        ai_min = by_cat.get("AI作業")
        if isinstance(ai_min, (int, float)):
            lines.append(f"- AI作業: {_fmt_minutes(float(ai_min))}")
            stats_derived += 1

    # §B2: stats 由来がゼロなら entries だけでは区間を新設しない
    if stats_derived == 0:
        return None

    # 目標: ユーザー自由記述（評価語検査の対象外・redactor は通す）。LLM 評価文ではない。
    if goal_text and str(goal_text).strip() and redactor is not None:
        g = redactor(str(goal_text).strip())
        if g:
            lines.append(f"- 目標: {g}")

    # 成果（outcome_git: ラベルは basename のみ・決定論）
    if commit_stats:
        parts = []
        for s in commit_stats:
            label = getattr(s, "repo_label", None) or "repo"
            commits = int(getattr(s, "commits", 0) or 0)
            ins = int(getattr(s, "insertions", 0) or 0)
            dels = int(getattr(s, "deletions", 0) or 0)
            parts.append(f"{label} {commits}コミット +{ins}/-{dels}行")
        if parts:
            lines.append(f"- 成果: {'; '.join(parts)}")

    # 摩擦ワースト（外部 title/project — redactor 無しなら行ごとスキップ）
    # 評価語を含む場合は行ごと落とさず digest 全体を None（目標以外は厳格）
    friction_line: str | None = None
    ai = stats.get("ai") if isinstance(stats.get("ai"), Mapping) else {}
    digests = ai.get("session_digests") if isinstance(ai, Mapping) else None
    if isinstance(digests, list) and digests and redactor is not None:
        worst = top_friction_sessions(digests, limit=1)
        if worst:
            d0 = worst[0]
            title = redactor(str(d0.get("title") or "—"))
            project = redactor(str(d0.get("project") or "—"))
            friction_line = f"- 摩擦ワースト: {project}「{title}」"
            if _has_banned(title) or _has_banned(project):
                # 目標以外の評価語 → digest 全体を落とす
                return None
            lines.append(friction_line)

    # 今日の提案 / 未完了（entries 由来だが ID 件数のみ・決定論）
    today_s = today.isoformat()
    proposed_today = sum(
        1 for e in entries if e.date == today_s and e.status == "proposed"
    )
    # proposed は TERMINAL に含まれない（冗長条件を置かない）
    open_entries = [e for e in entries if e.status == "proposed"]
    open_all = len(open_entries)
    lines.append(f"- 今日の提案: {proposed_today}件 / 未完了: {open_all}件")

    # 今日の1手（外部行動文 — redactor 無しならスキップ。評価語検査対象外）
    if open_entries and redactor is not None:
        latest = sorted(open_entries, key=lambda e: e.id, reverse=True)[0]
        body = " ".join((latest.action or "").split())
        # §G3: 上限40字（40字ちょうどは無変換、超は39字+…）
        snippet = body if len(body) <= 40 else body[:39] + "…"
        snippet = redactor(snippet)
        if snippet:
            lines.append(f"- 今日の1手: {latest.id} {snippet}")

    # 内部リンク
    markers = set(existing_markers or ())
    link_bits: list[str] = []
    if ADVICE_MARKER in markers:
        link_bits.append("🚀提案")
    if WEEKLY_CONTEXT_MARKER in markers:
        link_bits.append("📊週次")
    if ACTIONS_MARKER in markers:
        link_bits.append("📌アクション")
    if link_bits:
        lines.append("- 詳細: " + " / ".join(link_bits))

    body_lines = [ln for ln in lines if ln.startswith("- ")]
    if not body_lines:
        return None
    return "\n".join(lines) + "\n"
