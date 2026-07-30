"""申し送りROI台帳: agent-context レッスンの家賃と効果を対照する。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Sequence

from .aiwork import AISession, UserPrompt
from .memory import load_entries
from .promptledger import find_matching_entry, load_prompt_ledger
from .stats import load_stats
from .verdict import parse_pass_condition
from .experiments import metric_from_stats, target_met

HANDOFF_LEDGER = "handoff_ledger.jsonl"
STATUSES = frozenset({"active", "suppressed", "promoted"})
KINDS = frozenset({"prm", "kzn", "retry", "toolerr"})


@dataclass
class HandoffLesson:
    """生成時の1レッスン（安定ID付き）。"""

    lesson_id: str
    kind: str
    ref_id: str
    text: str  # セクションに書く本文行


@dataclass
class HandoffLedgerEntry:
    lesson_id: str
    target: str  # 絶対パス文字列
    first_injected: str  # YYYY-MM-DD
    kind: str
    ref_id: str
    status: str = "active"  # active|suppressed|promoted


@dataclass
class HandoffROIRow:
    lesson: HandoffLesson
    target: str
    first_injected: str | None
    status: str
    rent_tokens: int | None  # 概算 tok
    rent_sessions: int | None  # None = 不明
    rent_display: str
    effect_display: str
    effect_good: bool | None  # True=効いている, False=効果なし, None=不明/計測中
    suppress_candidate: bool = False
    promote_candidate: bool = False


def _ledger_path(memory_dir: Path) -> Path:
    return Path(memory_dir) / HANDOFF_LEDGER


def load_handoff_ledger(memory_dir: Path) -> list[HandoffLedgerEntry]:
    """last-wins で読む。キーは (lesson_id, target)。"""
    path = _ledger_path(memory_dir)
    if not path.is_file():
        return []
    by_key: dict[tuple[str, str], HandoffLedgerEntry] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict) or "lesson_id" not in d:
            continue
        status = str(d.get("status") or "active")
        if status not in STATUSES:
            status = "active"
        kind = str(d.get("kind") or "retry")
        if kind not in KINDS:
            kind = "retry"
        target = str(d.get("target") or "")
        lid = str(d["lesson_id"])
        entry = HandoffLedgerEntry(
            lesson_id=lid,
            target=target,
            first_injected=str(d.get("first_injected") or ""),
            kind=kind,
            ref_id=str(d.get("ref_id") or ""),
            status=status,
        )
        by_key[(lid, _norm_target(target))] = entry
    return sorted(by_key.values(), key=lambda e: (e.lesson_id, e.target))


def append_handoff_ledger(
    memory_dir: Path, entries: list[HandoffLedgerEntry]
) -> None:
    if not entries:
        return
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    with open(_ledger_path(memory_dir), "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")


def _norm_target(target: str | Path) -> str:
    try:
        return str(Path(target).expanduser().resolve()).lower()
    except OSError:
        return str(target).replace("\\", "/").lower()


def abs_target(target: str | Path) -> str:
    try:
        return str(Path(target).expanduser().resolve())
    except OSError:
        return str(Path(target).expanduser())


def project_name_for_target(target: str | Path) -> str:
    """AISession.project 照合用: target の親ディレクトリ basename。"""
    p = Path(target).expanduser()
    # CLAUDE.md の親 = リポジトリ root 想定
    return p.parent.name or p.name


def sessions_for_target(
    sessions: Sequence[AISession], target: str | Path
) -> list[AISession]:
    name = project_name_for_target(target).lower()
    return [s for s in sessions if (s.project or "").lower() == name]


def record_lessons_on_apply(
    memory_dir: Path,
    *,
    target: str | Path,
    lessons: list[HandoffLesson],
    as_of: date,
) -> list[HandoffLedgerEntry]:
    """初回出現の (lesson_id, target) だけ first_injected を追記。"""
    existing = load_handoff_ledger(memory_dir)
    known = {(e.lesson_id, _norm_target(e.target)) for e in existing}
    t_abs = abs_target(target)
    t_key = _norm_target(t_abs)
    to_add: list[HandoffLedgerEntry] = []
    for les in lessons:
        if (les.lesson_id, t_key) in known:
            continue
        to_add.append(
            HandoffLedgerEntry(
                lesson_id=les.lesson_id,
                target=t_abs,
                first_injected=as_of.isoformat(),
                kind=les.kind,
                ref_id=les.ref_id,
                status="active",
            )
        )
        known.add((les.lesson_id, t_key))
    append_handoff_ledger(memory_dir, to_add)
    return to_add


def set_lesson_status(
    memory_dir: Path,
    lesson_id: str,
    status: str,
    *,
    target: str | Path | None = None,
    as_of: date | None = None,
) -> list[HandoffLedgerEntry]:
    """status を追記更新。target 指定時はその target のみ、省略時は全 target。"""
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    existing = load_handoff_ledger(memory_dir)
    hits = [e for e in existing if e.lesson_id == lesson_id]
    if target is not None:
        tk = _norm_target(target)
        hits = [e for e in hits if _norm_target(e.target) == tk]
    if not hits:
        raise KeyError(lesson_id)
    updated: list[HandoffLedgerEntry] = []
    for e in hits:
        updated.append(
            HandoffLedgerEntry(
                lesson_id=e.lesson_id,
                target=e.target,
                first_injected=e.first_injected,
                kind=e.kind,
                ref_id=e.ref_id,
                status=status,
            )
        )
    append_handoff_ledger(memory_dir, updated)
    return updated


def suppressed_ids_for_target(
    ledger: list[HandoffLedgerEntry], target: str | Path
) -> set[str]:
    tk = _norm_target(target)
    out: set[str] = set()
    for e in ledger:
        if _norm_target(e.target) != tk:
            continue
        if e.status in ("suppressed", "promoted"):
            out.add(e.lesson_id)
    return out


def promoted_lesson_ids(ledger: list[HandoffLedgerEntry]) -> set[str]:
    return {e.lesson_id for e in ledger if e.status == "promoted"}


def approx_tokens(text: str) -> int:
    """概算トークン = 文字数 // 4。"""
    return max(0, len(text or "") // 4)


def compute_rent(
    text: str,
    sessions: Sequence[AISession],
    target: str | Path,
) -> tuple[int | None, int | None, str]:
    """戻り値: (tok, sess_count|None, display)。

    一致セッション0件 → 家賃「不明」(fail-closed)。金額換算しない。
    """
    tok = approx_tokens(text)
    matched = sessions_for_target(sessions, target)
    if not matched:
        return tok, None, "不明"
    n = len(matched)
    rent = tok * n
    return tok, n, f"~{tok} tok × {n} sess = {rent} tok·sess"


def _first_injected_for(
    ledger: list[HandoffLedgerEntry], lesson_id: str, target: str | Path
) -> str | None:
    tk = _norm_target(target)
    for e in ledger:
        if e.lesson_id == lesson_id and _norm_target(e.target) == tk:
            return e.first_injected or None
    return None


def _status_for(
    ledger: list[HandoffLedgerEntry], lesson_id: str, target: str | Path
) -> str:
    tk = _norm_target(target)
    for e in ledger:
        if e.lesson_id == lesson_id and _norm_target(e.target) == tk:
            return e.status
    return "active"


def _measure_sess_metric(
    kind: str,
    matched: Sequence[AISession],
    b0: date,
    b1: date,
    a0: date,
    a1: date,
) -> tuple[str, bool | None]:
    """retry/toolerr の前窓 vs 後窓。

    AISession に retry_chains が無いため、retry は is_fragmented を摩擦プロキシにする。
    toolerr は tool_errors 合計。
    """

    def agg(start: date, end: date) -> int | None:
        total = 0
        any_s = False
        for s in matched:
            d = s.start.date()
            if not (start <= d <= end):
                continue
            any_s = True
            if kind == "retry":
                total += 1 if s.is_fragmented else 0
            else:
                total += int(s.tool_errors or 0)
        return total if any_s else None

    before = agg(b0, b1)
    after = agg(a0, a1)
    if before is None or after is None:
        return "不明", None
    if after < before:
        return f"効いている({before}→{after})", True
    return f"効果なし({before}→{after})", False


def measure_effect(
    lesson: HandoffLesson,
    *,
    target: str | Path,
    first_injected: str | None,
    as_of: date,
    prompts: Sequence[UserPrompt],
    sessions: Sequence[AISession],
    memory_dir: Path,
    stats_dir: Path,
) -> tuple[str, bool | None]:
    """戻り値: (display, good|None)。fail-closed。"""
    if not first_injected:
        return "計測中(未注入)", None
    try:
        fi = date.fromisoformat(first_injected[:10])
    except ValueError:
        return "不明", None
    after_days = (as_of - fi).days + 1  # fi 当日を1日目
    if after_days < 30:
        return f"計測中({after_days}/30日)", None

    before_start = fi - timedelta(days=30)
    after_end = fi + timedelta(days=29)  # [fi, fi+29] = 30日

    matched_sess = sessions_for_target(sessions, target)

    if lesson.kind == "prm":
        ledger = load_prompt_ledger(memory_dir)
        entry = next((e for e in ledger if e.id == lesson.ref_id), None)
        if entry is None:
            return "不明", None

        before_n = 0
        after_n = 0
        for p in prompts:
            d = p.timestamp.date()
            if find_matching_entry([entry], p.text) is None:
                continue
            if before_start <= d < fi:
                before_n += 1
            elif fi <= d <= after_end:
                after_n += 1
        if after_n < before_n:
            return f"効いている({before_n}→{after_n})", True
        return f"効果なし({before_n}→{after_n})", False

    if lesson.kind == "kzn":
        entries = load_entries(memory_dir)
        kzn = next((e for e in entries if e.id == lesson.ref_id), None)
        if kzn is None:
            return "不明", None
        parsed = parse_pass_condition(kzn.action)
        if not parsed:
            return "不明", None
        metric, op, thr = parsed

        def violate_rate(start: date, end: date) -> tuple[int, int] | None:
            stats = load_stats(stats_dir, days=(end - start).days + 1, end_day=end)
            meas = 0
            viol = 0
            for s in stats:
                raw = s.get("day")
                if not raw:
                    continue
                try:
                    d = date.fromisoformat(str(raw)[:10])
                except ValueError:
                    continue
                if not (start <= d <= end):
                    continue
                v = metric_from_stats(metric, s)
                if v is None:
                    continue
                meas += 1
                if not target_met(float(v), op, thr):
                    viol += 1
            if meas < 3:
                return None
            return viol, meas

        b = violate_rate(before_start, fi - timedelta(days=1))
        a = violate_rate(fi, after_end)
        if b is None or a is None:
            return "不明", None
        if a[0] < b[0]:
            return f"効いている(違反{b[0]}/{b[1]}→{a[0]}/{a[1]})", True
        return f"効果なし(違反{b[0]}/{b[1]}→{a[0]}/{a[1]})", False

    if lesson.kind in ("retry", "toolerr"):
        if not matched_sess:
            return "不明", None
        return _measure_sess_metric(
            lesson.kind,
            matched_sess,
            before_start,
            fi - timedelta(days=1),
            fi,
            after_end,
        )

    return "不明", None


def build_roi_rows(
    *,
    target: str | Path,
    lessons: list[HandoffLesson],
    ledger: list[HandoffLedgerEntry],
    sessions: Sequence[AISession],
    prompts: Sequence[UserPrompt],
    memory_dir: Path,
    stats_dir: Path,
    as_of: date,
    redactor: Callable[[str], str] | None = None,
) -> list[HandoffROIRow]:
    t_abs = abs_target(target)
    rows: list[HandoffROIRow] = []

    for les in lessons:
        fi = _first_injected_for(ledger, les.lesson_id, t_abs)
        status = _status_for(ledger, les.lesson_id, t_abs)
        tok, sess_n, rent_disp = compute_rent(les.text, sessions, t_abs)
        eff_disp, good = measure_effect(
            les,
            target=t_abs,
            first_injected=fi,
            as_of=as_of,
            prompts=prompts,
            sessions=sessions,
            memory_dir=memory_dir,
            stats_dir=stats_dir,
        )
        suppress_cand = False
        if good is False and fi:
            try:
                fi_d = date.fromisoformat(fi[:10])
                if (as_of - fi_d).days >= 30:
                    suppress_cand = True
            except ValueError:
                pass
        text = les.text
        if redactor:
            text = redactor(text)
        if len(text) > 60:
            text = text[:57] + "..."
        rows.append(
            HandoffROIRow(
                lesson=HandoffLesson(
                    lesson_id=les.lesson_id,
                    kind=les.kind,
                    ref_id=les.ref_id,
                    text=text,
                ),
                target=t_abs,
                first_injected=fi,
                status=status,
                rent_tokens=tok,
                rent_sessions=sess_n,
                rent_display=rent_disp,
                effect_display=eff_disp,
                effect_good=good,
                suppress_candidate=suppress_cand,
            )
        )
    return rows


def mark_promote_candidates(rows_by_target: dict[str, list[HandoffROIRow]]) -> None:
    """2+ target で effect_good の lesson_id に promote_candidate を立てる。"""
    good_targets: dict[str, set[str]] = {}
    for t, rows in rows_by_target.items():
        for r in rows:
            if r.effect_good is True:
                good_targets.setdefault(r.lesson.lesson_id, set()).add(t)
    multi = {lid for lid, ts in good_targets.items() if len(ts) >= 2}
    for rows in rows_by_target.values():
        for r in rows:
            if r.lesson.lesson_id in multi:
                r.promote_candidate = True


def format_roi_table(rows: list[HandoffROIRow]) -> str:
    if not rows:
        return "申し送りROI: （レッスンなし）"
    lines = [
        "| lesson_id | 行 | 家賃 | 効果 | status | メモ |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        memo = []
        if r.suppress_candidate:
            memo.append("→ 抑制候補")
        if r.promote_candidate:
            memo.append("→ 昇格候補")
        body = r.lesson.text.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {r.lesson.lesson_id} | {body} | {r.rent_display} | "
            f"{r.effect_display} | {r.status} | {' '.join(memo) or '-'} |"
        )
    lines.append("")
    lines.append(
        "注: 家賃は概算トークン(文字数/4)×対象リポジトリ30日セッション数。"
        "金額換算はしない。セッション帰属不能時は家賃「不明」。"
    )
    return "\n".join(lines)


def format_weekly_handoff_roi_section(
    rows: list[HandoffROIRow],
    ledger: list[HandoffLedgerEntry],
) -> str | None:
    """台帳も rows も空なら None。"""
    if not ledger and not rows:
        return None
    lines = ["## 申し送りROI", ""]
    if rows:
        def rent_key(r: HandoffROIRow) -> int:
            if r.rent_sessions is None or r.rent_tokens is None:
                return -1
            return r.rent_tokens * r.rent_sessions

        top = max(rows, key=rent_key)
        lines.append(
            f"- 最高家賃: {top.lesson.lesson_id} {top.rent_display}"
            f" — {top.lesson.text[:40]}"
        )
    else:
        lines.append("- 最高家賃: （レッスンなし）")
    supp = sum(1 for r in rows if r.suppress_candidate)
    supp_led = sum(1 for e in ledger if e.status == "suppressed")
    prom = sum(1 for e in ledger if e.status == "promoted")
    lines.append(f"- 抑制候補: {supp}件 / 台帳suppressed: {supp_led}件")
    lines.append(f"- promoted: {prom}件")
    return "\n".join(lines)


def inject_promoted_lesson(
    global_target: Path,
    lesson: HandoffLesson,
    *,
    as_of: date,
) -> None:
    """global_target の agent-context 区間へ昇格レッスンを追記。"""
    from .vault import (
        AGENT_CONTEXT_MARKER,
        atomic_write_text,
        extract_section,
        read_text_preserve_newlines,
        upsert_section,
    )

    target = Path(global_target).expanduser()
    if target.is_file():
        content = read_text_preserve_newlines(target)
    else:
        content = ""
    body = extract_section(content, AGENT_CONTEXT_MARKER) or ""
    if not body.strip():
        body = (
            "このセクションは KaizenLog が実測データから自動生成(再実行で上書き)。"
            f"手動メモはマーカーの外へ。生成日: {as_of.isoformat()}\n"
        )
    marker_line = f"<!-- hnd-promoted:{lesson.lesson_id} -->"
    if marker_line in body or lesson.lesson_id in body:
        # 既にあれば status 更新のみ（本文はそのまま）
        pass
    else:
        block = (
            f"\n### グローバル昇格レッスン\n"
            f"{marker_line}\n"
            f"- {lesson.lesson_id}: {lesson.text}\n"
        )
        if "### グローバル昇格レッスン" in body:
            # 見出しが既にある場合は見出し直後ではなく末尾へ
            body = body.rstrip() + f"\n{marker_line}\n- {lesson.lesson_id}: {lesson.text}\n"
        else:
            body = body.rstrip() + block
    updated = upsert_section(
        content, AGENT_CONTEXT_MARKER, body.rstrip() + "\n", position="bottom"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, updated)
