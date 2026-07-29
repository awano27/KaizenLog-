"""改善風化センチネル: 直した問題の復活を決定論で検知する。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from .aiwork import UserPrompt, available_adapters, collect_ai_telemetry
from .config import Config
from .experiments import (
    detect_regressions,
    load_experiments,
    metric_from_stats,
    target_met,
)
from .memory import load_entries
from .promptledger import find_matching_entry, load_prompt_ledger
from .stats import load_stats
from .verdict import parse_pass_condition

DECAY_LEDGER = "decay_ledger.jsonl"
_COOLDOWN_DAYS = 30
_PRM_WINDOW = 7
_PRM_THRESHOLD = 3
_KZN_PASS_LOOKBACK = 60
_KZN_MEASURE_WINDOW = 7
_KZN_MIN_MEASURABLE = 3


@dataclass
class DecayEvent:
    date: str  # YYYY-MM-DD
    kind: str  # prm | experiment | kzn
    ref_id: str
    detail: str
    evidence: str


def load_decay_events(
    memory_dir: Path, *, window_days: int | None = None, as_of: date | None = None
) -> list[DecayEvent]:
    """台帳を読み、任意で直近 window_days に絞る。"""
    path = Path(memory_dir) / DECAY_LEDGER
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    as_of = as_of or date.today()
    start_s = None
    if window_days is not None:
        start_s = (as_of - timedelta(days=window_days - 1)).isoformat()
    out: list[DecayEvent] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict) or "ref_id" not in d:
            continue
        day = str(d.get("date") or "")
        if start_s and day and day < start_s:
            continue
        out.append(
            DecayEvent(
                date=day,
                kind=str(d.get("kind") or ""),
                ref_id=str(d.get("ref_id") or ""),
                detail=str(d.get("detail") or ""),
                evidence=str(d.get("evidence") or ""),
            )
        )
    return out


def _recent_ref_ids(memory_dir: Path, as_of: date, cooldown_days: int) -> set[str]:
    start = (as_of - timedelta(days=cooldown_days - 1)).isoformat()
    ids: set[str] = set()
    for e in load_decay_events(memory_dir):
        if e.date and e.date >= start:
            ids.add(e.ref_id)
    return ids


def append_decay_events(memory_dir: Path, events: list[DecayEvent]) -> None:
    if not events:
        return
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / DECAY_LEDGER
    with open(path, "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")


def detect_prm_decay(
    memory_dir: Path,
    prompts: Sequence[UserPrompt],
    *,
    as_of: date,
    redactor: Callable[[str], str] | None = None,
) -> list[DecayEvent]:
    """skilled PRM が直近7日で3回以上再発したら風化。"""
    ledger = load_prompt_ledger(memory_dir)
    window_start = as_of - timedelta(days=_PRM_WINDOW - 1)
    recent = [
        p for p in prompts if window_start <= p.timestamp.date() <= as_of
    ]
    events: list[DecayEvent] = []
    skilled = [e for e in ledger if e.status == "skilled" and e.marked_on]
    for entry in skilled:
        count = 0
        for p in recent:
            if find_matching_entry([entry], p.text) is not None:
                count += 1
        if count < _PRM_THRESHOLD:
            continue
        rep = entry.representative or ""
        if redactor:
            rep = redactor(rep)
        if len(rep) > 60:
            rep = rep[:57] + "..."
        events.append(
            DecayEvent(
                date=as_of.isoformat(),
                kind="prm",
                ref_id=entry.id,
                detail=f"skilled PRM が直近{_PRM_WINDOW}日で{count}回再発",
                evidence=rep,
            )
        )
    return events


def detect_experiment_decay(
    experiments_dir: Path,
    *,
    as_of: date,
    redactor: Callable[[str], str] | None = None,
) -> list[DecayEvent]:
    """detect_regressions の結果を風化イベント化（ロジック再実装禁止）。"""
    experiments = load_experiments(experiments_dir)
    regs = detect_regressions(experiments, window=7, as_of=as_of)
    events: list[DecayEvent] = []
    for exp in regs:
        recent = [
            (d, v)
            for d, v in exp.measurements.items()
            if as_of - timedelta(days=6) <= d <= as_of
        ]
        misses = sum(
            1
            for _, v in recent
            if not target_met(v, exp.target_op, exp.target_value)
        )
        title = exp.title or exp.path.stem
        if redactor:
            title = redactor(title)
        evidence = f"{exp.metric} {exp.target_op} {exp.target_value:g}"
        if redactor:
            evidence = redactor(evidence)
        events.append(
            DecayEvent(
                date=as_of.isoformat(),
                kind="experiment",
                ref_id=exp.path.stem,
                detail=(
                    f"採用実験「{title}」が退行"
                    f"（直近7日で{misses}/{len(recent)}日未達）"
                ),
                evidence=evidence,
            )
        )
    return events


def detect_kzn_decay(
    memory_dir: Path,
    stats_dir: Path,
    *,
    as_of: date,
    redactor: Callable[[str], str] | None = None,
) -> list[DecayEvent]:
    """PASS 後に直近7日の測定可能日で過半数が条件違反なら風化。"""
    entries = load_entries(memory_dir)
    pass_start = (as_of - timedelta(days=_KZN_PASS_LOOKBACK)).isoformat()
    candidates = [
        e
        for e in entries
        if e.status == "done"
        and e.verdict == "pass"
        and e.verdict_date
        and e.verdict_date >= pass_start
        and e.verdict_date <= as_of.isoformat()
    ]
    # 直近7日 stats
    stats_list = load_stats(stats_dir, days=_KZN_MEASURE_WINDOW, end_day=as_of)
    by_day = {str(s.get("day")): s for s in stats_list if s.get("day")}
    events: list[DecayEvent] = []
    seen_ids: set[str] = set()
    for e in candidates:
        if e.id in seen_ids:
            continue
        seen_ids.add(e.id)
        parsed = parse_pass_condition(e.action)
        if not parsed:
            continue
        metric, op, target = parsed
        measurable = 0
        violations = 0
        for i in range(_KZN_MEASURE_WINDOW):
            d = (as_of - timedelta(days=i)).isoformat()
            s = by_day.get(d)
            if not s:
                continue
            v = metric_from_stats(metric, s)
            if v is None:
                continue
            measurable += 1
            if not target_met(float(v), op, target):
                violations += 1
        if measurable < _KZN_MIN_MEASURABLE:
            continue
        if violations * 2 <= measurable:
            # 過半数ではない
            continue
        detail = (
            f"{e.id} の metric が再悪化"
            f"（{measurable}日中{violations}日違反）"
        )
        evidence = f"{metric} {op} {target:g}"
        if redactor:
            detail = redactor(detail)
            evidence = redactor(evidence)
        events.append(
            DecayEvent(
                date=as_of.isoformat(),
                kind="kzn",
                ref_id=e.id,
                detail=detail,
                evidence=evidence,
            )
        )
    return events


def run_decay_detection(
    cfg: Config,
    *,
    as_of: date,
    prompts: Sequence[UserPrompt] | None = None,
    redactor: Callable[[str], str] | None = None,
) -> list[DecayEvent]:
    """3検知器を実行し、クールダウン後に新規だけ台帳へ追記して返す。"""
    if prompts is None:
        prompts = []
        if cfg.aiwork.enabled:
            tz = ZoneInfo(cfg.timezone)
            end = datetime.combine(as_of, time.min, tzinfo=tz) + timedelta(days=1)
            start = end - timedelta(days=_PRM_WINDOW)
            adapters = available_adapters(cfg)
            if adapters:
                _, prompts, _ = collect_ai_telemetry(adapters, start, end)

    candidates: list[DecayEvent] = []
    candidates.extend(
        detect_prm_decay(cfg.memory_path, prompts, as_of=as_of, redactor=redactor)
    )
    candidates.extend(
        detect_experiment_decay(
            cfg.experiments_path, as_of=as_of, redactor=redactor
        )
    )
    candidates.extend(
        detect_kzn_decay(
            cfg.memory_path, cfg.stats_path, as_of=as_of, redactor=redactor
        )
    )
    cooled = _recent_ref_ids(cfg.memory_path, as_of, _COOLDOWN_DAYS)
    fresh = [e for e in candidates if e.ref_id not in cooled]
    append_decay_events(cfg.memory_path, fresh)
    return fresh


def format_decay_status_line(events: list[DecayEvent]) -> str | None:
    if not events:
        return None
    return f"⚠️ 風化した改善: {len(events)}件（直近7日）"


def format_decay_weekly_section(events: list[DecayEvent]) -> str | None:
    if not events:
        return None
    lines = ["## ⚠️ 風化した改善", ""]
    for e in events:
        lines.append(f"- [{e.kind}] {e.ref_id}: {e.detail}")
    return "\n".join(lines)


def format_f17_lines(events: list[DecayEvent]) -> list[str]:
    """advice_evidence 用 F17 行。"""
    out: list[str] = []
    for e in events:
        out.append(f"- [F17] 風化: {e.detail}")
    return out
