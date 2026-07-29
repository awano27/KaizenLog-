"""プロンプト資産ROI: クラスタ別の再発と推定トークン・skilled削減実績。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence

from .aiwork import AISession, UserPrompt
from .promptledger import (
    PromptLedgerEntry,
    find_matching_entry,
    load_prompt_ledger,
)


@dataclass
class PromptROIRow:
    entry: PromptLedgerEntry
    recurrence_30d: int
    est_tokens: int | None  # セッション単位合算。取れなければ None
    skilled_before: int | None = None
    skilled_after: int | None = None
    skilled_saved_tokens: int | None = None
    # after窓が未完了のとき True。表示は「計測中（後N/30日）」
    skilled_pending: bool = False
    skilled_pending_days: int = 0

    @property
    def roi_score(self) -> float:
        """並べ替え用: 再発×トークン（不明は再発のみ）。"""
        tok = float(self.est_tokens or 0)
        return float(self.recurrence_30d) * (tok if tok > 0 else 1.0)

    @property
    def skilled_effect(self) -> str:
        if self.entry.status != "skilled":
            return "-"
        if self.skilled_pending:
            n = self.skilled_pending_days
            return f"計測中（後{n}/30日）"
        if self.skilled_before is None or self.skilled_after is None:
            return "計測なし"
        saved = self.skilled_before - self.skilled_after
        tok = self.skilled_saved_tokens
        if tok is None:
            return f"削減 {saved}回 / tokens不明"
        return f"削減 {saved}回 / 約{tok} tokens"


def prompt_roi_scan_start(
    entries: Sequence[PromptLedgerEntry],
    as_of: date,
    window_days: int = 30,
) -> date:
    """テレメトリ収集開始日。

    as_of - (window_days-1) と、各 skilled の marked_on - window_days の最小。
    """
    start = as_of - timedelta(days=window_days - 1)
    for e in entries:
        if e.status != "skilled" or not e.marked_on:
            continue
        try:
            marked = date.fromisoformat(e.marked_on[:10])
        except ValueError:
            continue
        candidate = marked - timedelta(days=window_days)
        if candidate < start:
            start = candidate
    return start


def _session_for_prompt(
    prompt: UserPrompt, sessions: Sequence[AISession]
) -> AISession | None:
    """同一 project かつ時刻が区間内のセッション。複数なら最も近い開始。"""
    hits = [
        s
        for s in sessions
        if s.project == prompt.project and s.start <= prompt.timestamp <= s.end
    ]
    if not hits:
        day = prompt.timestamp.date()
        hits = [
            s
            for s in sessions
            if s.project == prompt.project and s.start.date() == day
        ]
    if not hits:
        return None
    hits.sort(key=lambda s: abs((s.start - prompt.timestamp).total_seconds()))
    return hits[0]


def _match_entry(
    entries: list[PromptLedgerEntry], text: str
) -> PromptLedgerEntry | None:
    return find_matching_entry(entries, text)


def _session_tokens(
    prompts: Sequence[UserPrompt], sessions: Sequence[AISession]
) -> int | None:
    """セッション単位合算。1件も取れなければ None。"""
    sess_ids: set[str] = set()
    tokens = 0
    any_tok = False
    for p in prompts:
        s = _session_for_prompt(p, sessions)
        if s is None or s.session_id in sess_ids:
            continue
        sess_ids.add(s.session_id)
        if s.output_tokens:
            any_tok = True
            tokens += int(s.output_tokens)
    return tokens if any_tok else None


def compute_prompt_roi(
    entries: list[PromptLedgerEntry],
    prompts: Sequence[UserPrompt],
    sessions: Sequence[AISession] | None = None,
    *,
    as_of: date | None = None,
    window_days: int = 30,
) -> list[PromptROIRow]:
    """台帳エントリごとの ROI 行を ROI 降順で返す。"""
    as_of = as_of or date.today()
    start = as_of - timedelta(days=window_days - 1)
    sessions = list(sessions or [])
    window_prompts = [
        p for p in prompts if start <= p.timestamp.date() <= as_of
    ]

    matched: dict[str, list[UserPrompt]] = {e.id: [] for e in entries}
    for p in window_prompts:
        hit = _match_entry(entries, p.text)
        if hit:
            matched.setdefault(hit.id, []).append(p)

    rows: list[PromptROIRow] = []
    for e in entries:
        ps = matched.get(e.id, [])
        recurrence = len(ps)
        est = _session_tokens(ps, sessions)
        if recurrence == 0:
            est = 0

        skilled_before = skilled_after = saved_tok = None
        pending = False
        pending_days = 0
        if e.status == "skilled" and e.marked_on:
            try:
                marked = date.fromisoformat(e.marked_on[:10])
            except ValueError:
                marked = None
            if marked is not None:
                # after窓完了: as_of >= marked_on + 29日
                after_complete = as_of >= marked + timedelta(days=window_days - 1)
                pending_days = max(
                    0, min(window_days, (as_of - marked).days + 1)
                )
                if not after_complete:
                    pending = True
                else:
                    # before: [marked-30, marked) / after: [marked, marked+30)
                    before_start = marked - timedelta(days=window_days)
                    after_end_excl = marked + timedelta(days=window_days)
                    before_ps = [
                        p
                        for p in prompts
                        if before_start <= p.timestamp.date() < marked
                        and _match_entry([e], p.text)
                    ]
                    after_ps = [
                        p
                        for p in prompts
                        if marked <= p.timestamp.date() < after_end_excl
                        and _match_entry([e], p.text)
                    ]
                    skilled_before = len(before_ps)
                    skilled_after = len(after_ps)
                    bt = _session_tokens(before_ps, sessions)
                    at = _session_tokens(after_ps, sessions)
                    # 片側でも tokens 不明なら saved_tok = None（tokens不明表示）
                    if bt is None or at is None:
                        saved_tok = None
                    else:
                        saved_tok = max(0, bt - at)

        rows.append(
            PromptROIRow(
                entry=e,
                recurrence_30d=recurrence,
                est_tokens=est,
                skilled_before=skilled_before,
                skilled_after=skilled_after,
                skilled_saved_tokens=saved_tok,
                skilled_pending=pending,
                skilled_pending_days=pending_days,
            )
        )
    rows.sort(key=lambda r: (-r.roi_score, r.entry.id))
    return rows


def format_roi_table(rows: list[PromptROIRow]) -> str:
    if not rows:
        return "プロンプト資産ROI: （台帳なし）"
    lines = [
        "| PRM-ID | 再発30d | 推定tokens | status | skilled効果 |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for r in rows:
        tok = "不明" if r.est_tokens is None else str(r.est_tokens)
        lines.append(
            f"| {r.entry.id} | {r.recurrence_30d} | {tok} | "
            f"{r.entry.status} | {r.skilled_effect} |"
        )
    lines.append("")
    lines.append(
        "注: 推定tokens はプロンプトが属するセッションの output tokens を"
        "セッション単位で合算（按分なし。プロンプト単位のトークンは取得不可）。"
        "skilled効果の削減は marked_on 後30日完了後のみ確定。"
    )
    return "\n".join(lines)


def format_weekly_roi_section(rows: list[PromptROIRow], *, top_n: int = 3) -> str | None:
    """週次用小節。データ無しなら None（小節ごと省略）。"""
    if not rows:
        return None
    useful = [
        r
        for r in rows
        if r.recurrence_30d > 0
        or (
            r.entry.status == "skilled"
            and not r.skilled_pending
            and r.skilled_before is not None
        )
    ]
    if not useful:
        return None
    lines = ["## プロンプト資産ROI", ""]
    for r in useful[:top_n]:
        tok = "不明" if r.est_tokens is None else f"{r.est_tokens}"
        lines.append(
            f"- {r.entry.id}: 再発{r.recurrence_30d}回 / 推定{tok} tokens"
            f" [{r.entry.status}]"
        )
    skilled = [
        r
        for r in rows
        if r.entry.status == "skilled"
        and not r.skilled_pending
        and r.skilled_before is not None
    ]
    if skilled:
        best = skilled[0]
        lines.append(f"- skilled削減実績: {best.entry.id} → {best.skilled_effect}")
    return "\n".join(lines)


def load_roi_for_paths(
    memory_dir: Path,
    prompts: Sequence[UserPrompt],
    sessions: Sequence[AISession] | None = None,
    *,
    as_of: date | None = None,
) -> list[PromptROIRow]:
    entries = load_prompt_ledger(memory_dir)
    return compute_prompt_roi(entries, prompts, sessions, as_of=as_of)
