"""夜の内省3問（kaizenlog:reflect）。決定論のみ・LLM 不関与。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, tzinfo
from typing import Any

from .report import hhmm_from_iso

_Q_RE = re.compile(r"^-\s*Q\d+\.\s*(.+?)\s*$")
_A_RE = re.compile(r"^\s+-\s*A:\s*(.*)$")


def _session_digests(stats: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    ai = stats.get("ai")
    if not isinstance(ai, Mapping):
        return []
    digests = ai.get("session_digests")
    if not isinstance(digests, list):
        return []
    return [d for d in digests if isinstance(d, Mapping)]


def _prompt_snippet(d: Mapping[str, Any], *, limit: int = 50) -> str:
    prompts = d.get("prompts_digest")
    if not isinstance(prompts, list) or not prompts:
        return ""
    raw = " ".join(str(prompts[0] or "").split())
    if not raw:
        return ""
    if len(raw) > limit:
        raw = raw[:limit]
    return raw


def _rule_unresolved(
    stats: Mapping[str, Any], *, tz: tzinfo | None = None
) -> str | None:
    digests = _session_digests(stats)
    # end 降順で最初の ended_in_error
    errored = [d for d in digests if d.get("ended_in_error")]
    if not errored:
        return None
    errored.sort(
        key=lambda d: str(d.get("end") or ""),
        reverse=True,
    )
    d0 = errored[0]
    end_hh = hhmm_from_iso(d0.get("end"), tz) or "??:??"
    snippet = _prompt_snippet(d0)
    if snippet:
        return (
            f"{end_hh} 終了のセッション（依頼: 『{snippet}』）は"
            "末尾エラーのまま終わっています。どう決着しましたか?"
        )
    return (
        f"{end_hh} 終了のセッションは末尾エラーのまま終わっています。"
        "どう決着しましたか?"
    )


def _sites_in_history(
    history: Sequence[Mapping[str, Any]], *, exclude_day: str
) -> set[str]:
    seen: set[str] = set()
    for h in history:
        if not isinstance(h, Mapping):
            continue
        if str(h.get("day") or "") == exclude_day:
            continue
        by_site = h.get("by_site")
        if not isinstance(by_site, Mapping):
            continue
        for site, mins in by_site.items():
            try:
                m = float(mins or 0)
            except (TypeError, ValueError):
                continue
            if m > 0:
                seen.add(str(site))
    return seen


def _rule_first_domain(
    stats: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    *,
    today: date,
) -> str | None:
    by_site = stats.get("by_site")
    if not isinstance(by_site, Mapping) or not by_site:
        return None
    prior = _sites_in_history(history, exclude_day=today.isoformat())
    # 30分以上・履歴に無い・分数降順で1件
    candidates: list[tuple[str, float]] = []
    for site, mins in by_site.items():
        try:
            m = float(mins or 0)
        except (TypeError, ValueError):
            continue
        if m < 30:
            continue
        name = str(site).strip()
        if not name or name in prior:
            continue
        candidates.append((name, m))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[1], x[0]))
    site, mins = candidates[0]
    mins_i = int(round(mins))
    return f"今日はじめて {site} に {mins_i}分。何を調べていましたか?"


def _day_has_tests_run(stats: Mapping[str, Any]) -> bool:
    for d in _session_digests(stats):
        if d.get("tests_run"):
            return True
    return False


def _rule_tests_streak(
    stats: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    *,
    today: date,
) -> str | None:
    if not _day_has_tests_run(stats):
        return None
    # history は古い→新しい。当日を除き、連続日を今日から遡る
    by_day: dict[str, Mapping[str, Any]] = {}
    for h in history:
        if not isinstance(h, Mapping):
            continue
        ds = str(h.get("day") or "")
        if ds:
            by_day[ds] = h
    by_day[today.isoformat()] = stats

    streak = 0
    cursor = today
    while True:
        key = cursor.isoformat()
        day_stats = by_day.get(key)
        if day_stats is None or not _day_has_tests_run(day_stats):
            break
        streak += 1
        from datetime import timedelta

        cursor = cursor - timedelta(days=1)
        if streak > 60:
            break
    if streak < 3:
        return None
    return f"テスト実行を伴うセッションが {streak}日続いています。続けますか?"


def collect_reflect_questions(
    stats: Mapping[str, Any] | None,
    stats_history: Sequence[Mapping[str, Any]] | None,
    *,
    today: date,
    limit: int = 3,
    tz: tzinfo | None = None,
) -> list[str]:
    """優先順ルールから最大 limit 問を返す。"""
    if not isinstance(stats, Mapping):
        return []
    history = [h for h in (stats_history or []) if isinstance(h, Mapping)]
    questions: list[str] = []
    for builder in (
        lambda: _rule_unresolved(stats, tz=tz),
        lambda: _rule_first_domain(stats, history, today=today),
        lambda: _rule_tests_streak(stats, history, today=today),
    ):
        q = builder()
        if q:
            questions.append(q)
        if len(questions) >= limit:
            break
    return questions


def build_reflect_section(
    stats: Mapping[str, Any] | None,
    stats_history: Sequence[Mapping[str, Any]] | None = None,
    *,
    today: date,
    tz: tzinfo | None = None,
) -> str | None:
    """データが選んだ問い区間。該当ゼロなら None。"""
    questions = collect_reflect_questions(
        stats, stats_history, today=today, tz=tz
    )
    if not questions:
        return None
    lines = [
        "## ✍️ 今日の3行（データが選んだ問い）",
        "> 1行ずつで十分。空欄のままでも構いません。",
    ]
    for i, q in enumerate(questions, start=1):
        lines.append(f"- Q{i}. {q}")
        lines.append("  - A:")
    return "\n".join(lines) + "\n"


def read_reflect_answers(content: str | None) -> list[tuple[str, str]]:
    """区間本文から (question, answer) を行パース。"""
    if not content:
        return []
    pairs: list[tuple[str, str]] = []
    current_q: str | None = None
    for line in content.splitlines():
        qm = _Q_RE.match(line)
        if qm:
            current_q = qm.group(1).strip()
            continue
        am = _A_RE.match(line)
        if am is not None and current_q is not None:
            pairs.append((current_q, am.group(1).strip()))
            current_q = None
    return pairs


def has_reflect_answers(section: str | None) -> bool:
    """`- A:` の後に非空テキストが1つでもあれば True。"""
    for _q, a in read_reflect_answers(section):
        if a:
            return True
    return False
