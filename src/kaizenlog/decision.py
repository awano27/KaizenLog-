"""朝決算カード（kaizenlog:decision）。決定論のみ・LLM 不関与。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from .memory import (
    ID_PATTERN,
    MemoryEntry,
    _CHECKBOX_RE,
    _SKIP_REASON_RE,
    partition_open_actions,
    resolve_display_cap,
    split_action_candidates,
    compute_action_stats,
)
from .verdict import parse_pass_condition

_ADOPT_RE = re.compile(r"採用")
_SKIP_RE = re.compile(r"見送り")
_ALT_RE = re.compile(r"別案")
_CONTENT_RE = re.compile(r"[｜|]\s*内容\s*[:：]\s*(.+)$")
_YESTERDAY_LINE_RE = re.compile(
    r"昨日の確定判定:\s*(KZN-\d{8}-\d+)\s*(✅PASS|❌FAIL|⏳暫定\S*)?\s*(.*)$"
)
_QUESTION_RE = re.compile(r"\*\*問い:\s*(KZN-\d{8}-\d+)")


def format_verdict_brief(entry: MemoryEntry) -> str:
    """確定判定1行（✅/❌ + 実測/目標）。"""
    mark = ""
    if entry.verdict == "pass":
        mark = "✅PASS"
    elif entry.verdict == "fail":
        mark = "❌FAIL"
    else:
        mark = "未判定"
    parsed = parse_pass_condition(entry.action or "")
    detail = ""
    if parsed is not None:
        metric, op, target = parsed
        if entry.verdict_value is not None:
            detail = f"（{metric} 実測 {entry.verdict_value:g} / 目標 {op} {target:g}）"
        else:
            detail = f"（{metric} 目標 {op} {target:g}）"
    return f"{entry.id} {mark}{detail}".rstrip()


def yesterday_confirmed_entries(
    entries: Sequence[MemoryEntry], today: date
) -> list[MemoryEntry]:
    y = (today - timedelta(days=1)).isoformat()
    out: list[MemoryEntry] = []
    for e in entries:
        if e.verdict not in ("pass", "fail"):
            continue
        if (e.verdict_stage or "confirmed") != "confirmed":
            continue
        if e.verdict_date == y:
            out.append(e)
    out.sort(key=lambda e: e.id)
    return out


def skip_counts_last_days(
    entries: Sequence[MemoryEntry],
    *,
    today: date,
    days: int = 7,
    memory_dir: Any | None = None,
) -> dict[str, int]:
    """decision.choice==skip の直近 days 日カウント。

    memory_dir があれば JSONL 全追記行を走査（同一IDの複数 skip を数える）。
    無ければ後勝ち entries 上の decision のみ（最大1）。
    """
    start = today - timedelta(days=days - 1)
    counts: dict[str, int] = {}

    # ID×日付で一意化してから数える（重複追記行の水増し耐性）
    seen_pairs: set[tuple[str, str]] = set()

    def _add(kid: str, d_raw: str) -> None:
        try:
            dd = date.fromisoformat(d_raw[:10])
        except ValueError:
            return
        if not (start <= dd <= today):
            return
        key = (kid, d_raw[:10])
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        counts[kid] = counts.get(kid, 0) + 1

    if memory_dir is not None:
        from .memory import MEMORY_FILE
        from pathlib import Path
        import json

        path = Path(memory_dir) / MEMORY_FILE
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict) or "id" not in d:
                    continue
                dec = d.get("decision")
                if not isinstance(dec, dict) or dec.get("choice") != "skip":
                    continue
                d_raw = dec.get("date")
                if not isinstance(d_raw, str):
                    continue
                _add(str(d["id"]), d_raw)
            return counts

    for e in entries:
        dec = e.decision
        if not isinstance(dec, dict) or dec.get("choice") != "skip":
            continue
        d_raw = dec.get("date")
        if not isinstance(d_raw, str):
            continue
        _add(e.id, d_raw)
    return counts


def select_decision_question_entry(
    entries: Sequence[MemoryEntry],
    today: date,
    *,
    note_content: str | None = None,
    memory_dir: Any | None = None,
) -> MemoryEntry | None:
    """朝の📌1件と同じ選定 + skip 2回フィルタで次点へ。"""
    buckets = partition_open_actions(list(entries), today, recent_include_today=False)
    if buckets.total == 0:
        return None
    checked_ids: set[str] = set()
    if note_content:
        for line in note_content.splitlines():
            match = _CHECKBOX_RE.match(line)
            if not match or match.group(2) not in ("x", "X"):
                continue
            id_match = ID_PATTERN.search(match.group(4))
            if id_match:
                checked_ids.add(id_match.group(0))
    stats = compute_action_stats(list(entries), today)
    cap = resolve_display_cap(stats, max_candidates=5)
    actionable, _mon = split_action_candidates(buckets.recent, checked_ids)
    skip_map = skip_counts_last_days(
        entries, today=today, days=7, memory_dir=memory_dir
    )
    for entry in actionable[: max(cap, 5)]:
        if skip_map.get(entry.id, 0) >= 2:
            continue
        return entry
    return None


def build_morning_decision_section(
    entries: Sequence[MemoryEntry],
    today: date,
    *,
    note_content: str | None = None,
    existing_section: str | None = None,
    memory_dir: Any | None = None,
) -> str | None:
    """朝の意思決定カード。判定も候補も無ければ None。

    既存区間にチェック/手書きがあればそれを優先保持（再描画で消さない）。
    """
    if existing_section and _has_user_decision_marks(existing_section):
        return existing_section if existing_section.endswith("\n") else existing_section + "\n"

    confirmed = yesterday_confirmed_entries(entries, today)
    question = select_decision_question_entry(
        entries, today, note_content=note_content, memory_dir=memory_dir
    )
    if not confirmed and question is None:
        return None

    lines = ["## ⚖ 今日の意思決定（1件・朝に確定）", ""]
    if confirmed:
        # 代表1件（最新 ID 優先で末尾）
        top = confirmed[-1]
        lines.append(f"昨日の確定判定: {format_verdict_brief(top)}")
        lines.append("")
    if question is not None:
        lines.append(f"**問い: {question.id} を今日も実行するか**")
        lines.append("- [ ] 採用（今日実行する）")
        lines.append("- [ ] 見送り｜理由: ＿＿")
        lines.append("- [ ] 別案でいく｜内容: ＿＿")
    elif confirmed:
        # 判定のみある日: 問いなしでもカードは出す（判定表示のみ）
        pass
    else:
        return None
    return "\n".join(lines) + "\n"


def _has_user_decision_marks(section: str) -> bool:
    """チェック済み or 理由/内容の非空手書きがあれば True。"""
    for line in section.splitlines():
        m = _CHECKBOX_RE.match(line)
        if not m:
            continue
        if m.group(2) in ("x", "X"):
            return True
        body = m.group(4)
        rm = _SKIP_REASON_RE.search(body)
        if rm and rm.group(1).strip() and rm.group(1).strip() not in ("＿＿", "_", "—"):
            return True
        cm = _CONTENT_RE.search(body)
        if cm and cm.group(1).strip() and cm.group(1).strip() not in ("＿＿", "_", "—"):
            return True
    return False


def parse_decision_choice(
    section: str | None,
) -> dict[str, Any] | None:
    """3択のうち [x] を読み choice/reason を返す。未記入は None。"""
    if not section:
        return None
    choice: str | None = None
    reason: str | None = None
    for line in section.splitlines():
        m = _CHECKBOX_RE.match(line)
        if not m or m.group(2) not in ("x", "X"):
            continue
        body = m.group(4)
        if _ADOPT_RE.search(body):
            choice = "adopt"
            reason = None
            break
        if _SKIP_RE.search(body):
            choice = "skip"
            rm = _SKIP_REASON_RE.search(body)
            if rm:
                r = rm.group(1).strip()
                if r and r not in ("＿＿", "_"):
                    reason = r
            break
        if _ALT_RE.search(body):
            choice = "alternative"
            cm = _CONTENT_RE.search(body)
            if cm:
                r = cm.group(1).strip()
                if r and r not in ("＿＿", "_"):
                    reason = r
            break
    if choice is None:
        return None
    return {"choice": choice, "reason": reason}


def parse_decision_question_id(section: str | None) -> str | None:
    if not section:
        return None
    m = _QUESTION_RE.search(section)
    if m:
        return m.group(1)
    return None


def choice_label(choice: str) -> str:
    return {
        "adopt": "採用",
        "skip": "見送り",
        "alternative": "別案",
    }.get(choice, choice)


def build_settlement_block(
    *,
    choice: str,
    metric: str | None,
    observed: float | None,
    median7: float | None,
) -> str:
    """決算小節（因果断定なし）。"""
    lines = ["### ⚖ 今日の決算", f"- 朝の決定: {choice_label(choice)}"]
    if metric is None:
        lines.append("- 観測: 未判定（指標を解釈できません）")
    elif observed is None:
        lines.append("- 観測: 未判定（分母不足）")
    elif median7 is not None:
        lines.append(
            f"- 観測: {metric} 当日実測 {_fmt_num(observed)}"
            f"（7日中央値 {_fmt_num(median7)}）。決定との因果は断定しません。"
        )
    else:
        lines.append(
            f"- 観測: {metric} 当日実測 {_fmt_num(observed)}。"
            f"決定との因果は断定しません。"
        )
    lines.append("- 判定への接続: 明朝の backfill で確定します")
    return "\n".join(lines) + "\n"


def _fmt_num(v: float) -> str:
    if float(v).is_integer():
        return str(int(v))
    return f"{v:g}"


def strip_settlement(section: str) -> str:
    """既存の決算小節を除いた朝パートを返す。"""
    idx = section.find("### ⚖ 今日の決算")
    if idx < 0:
        return section.rstrip() + "\n"
    return section[:idx].rstrip() + "\n"


def recompose_decision_section(
    morning_body: str,
    settlement: str,
) -> str:
    base = strip_settlement(morning_body).rstrip()
    return base + "\n\n" + settlement.lstrip()


def median_metric_from_history(
    metric: str,
    stats_history: Sequence[Mapping[str, Any]],
    *,
    today: date,
) -> float | None:
    from statistics import median

    from .experiments import metric_from_stats

    vals: list[float] = []
    today_s = today.isoformat()
    for h in stats_history:
        if not isinstance(h, Mapping):
            continue
        if str(h.get("day") or "") == today_s:
            continue
        v = metric_from_stats(metric, dict(h))
        if v is not None:
            vals.append(float(v))
    if len(vals) < 3:
        return None
    window = vals[-7:] if len(vals) > 7 else vals
    return float(median(window))
