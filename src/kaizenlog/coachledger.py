"""コーチ効果検証台帳: 適用した助言を機械採点し、FAIL ならロールバック提案する。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from typing import Callable, Sequence

from .experiments import metric_from_stats, weekday_baseline
from .stats import load_stats
from .vault import atomic_write_text

COACH_LEDGER = "coach_ledger.jsonl"
WATCH_METRICS = ["ai_retry_chains", "ai_tool_errors", "loop_tax_episodes"]
STATUSES = frozenset(
    {"watching", "pass", "fail", "rolled_back", "superseded", "unmeasurable"}
)
_JUDGE_DAYS = 7
_MIN_MEASURABLE = 3
_PRE_BASELINE_DAYS = 28


@dataclass
class CoachLedgerEntry:
    id: str
    applied_on: str  # YYYY-MM-DD
    proposal_file: str  # memory_path からの相対
    targets: list[str] = field(default_factory=list)
    evidence: list = field(default_factory=list)
    watch_metrics: list[str] = field(default_factory=list)
    status: str = "watching"
    verdict_date: str | None = None
    verdict_detail: str | None = None
    # ロールバック照合用（旧行は欠落可 → 提案ファイルから復元）
    append_md: str | None = None


def _ledger_path(memory_dir: Path) -> Path:
    return Path(memory_dir) / COACH_LEDGER


def next_cch_id(existing: list[CoachLedgerEntry], day: date, offset: int = 0) -> str:
    prefix = f"CCH-{day.strftime('%Y%m%d')}-"
    used = {
        int(e.id.rsplit("-", 1)[1])
        for e in existing
        if e.id.startswith(prefix) and e.id.rsplit("-", 1)[-1].isdigit()
    }
    n = 1 + offset
    while n in used:
        n += 1
    return f"{prefix}{n:03d}"


def load_coach_ledger(memory_dir: Path) -> list[CoachLedgerEntry]:
    """追記型 JSONL を後勝ちで読み、ID 昇順で返す。"""
    path = _ledger_path(memory_dir)
    if not path.is_file():
        return []
    by_id: dict[str, CoachLedgerEntry] = {}
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
        if not isinstance(d, dict) or "id" not in d:
            continue
        status = str(d.get("status") or "watching")
        if status not in STATUSES:
            status = "watching"
        targets = d.get("targets") or []
        if not isinstance(targets, list):
            targets = [str(targets)]
        metrics = d.get("watch_metrics") or list(WATCH_METRICS)
        if not isinstance(metrics, list):
            metrics = list(WATCH_METRICS)
        evidence = d.get("evidence") if isinstance(d.get("evidence"), list) else []
        append_md = d.get("append_md")
        if append_md is not None:
            append_md = str(append_md)
        by_id[str(d["id"])] = CoachLedgerEntry(
            id=str(d["id"]),
            applied_on=str(d.get("applied_on") or ""),
            proposal_file=str(d.get("proposal_file") or ""),
            targets=[str(t) for t in targets],
            evidence=evidence,
            watch_metrics=[str(m) for m in metrics],
            status=status,
            verdict_date=(
                str(d["verdict_date"]) if d.get("verdict_date") is not None else None
            ),
            verdict_detail=(
                str(d["verdict_detail"])
                if d.get("verdict_detail") is not None
                else None
            ),
            append_md=append_md,
        )
    return sorted(by_id.values(), key=lambda e: e.id)


def append_coach_ledger(
    memory_dir: Path, entries: list[CoachLedgerEntry]
) -> None:
    if not entries:
        return
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    with open(_ledger_path(memory_dir), "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")


def _rel_to_memory(memory_dir: Path, path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(memory_dir).resolve())).replace(
            "\\", "/"
        )
    except ValueError:
        return str(path).replace("\\", "/")


def record_coach_application(
    memory_dir: Path,
    *,
    as_of: date,
    proposal_path: Path,
    targets: list[Path],
    evidence: list,
    append_md: str,
) -> CoachLedgerEntry:
    """適用成功後に watching を追記。既存 watching は superseded。"""
    existing = load_coach_ledger(memory_dir)
    to_append: list[CoachLedgerEntry] = []
    for e in existing:
        if e.status == "watching":
            to_append.append(
                CoachLedgerEntry(
                    id=e.id,
                    applied_on=e.applied_on,
                    proposal_file=e.proposal_file,
                    targets=list(e.targets),
                    evidence=list(e.evidence),
                    watch_metrics=list(e.watch_metrics),
                    status="superseded",
                    verdict_date=as_of.isoformat(),
                    verdict_detail="後続の coach 適用により上書き",
                    append_md=e.append_md,
                )
            )
    new_id = next_cch_id(existing + to_append, as_of)
    entry = CoachLedgerEntry(
        id=new_id,
        applied_on=as_of.isoformat(),
        proposal_file=_rel_to_memory(memory_dir, proposal_path),
        targets=[str(t) for t in targets],
        evidence=list(evidence or []),
        watch_metrics=list(WATCH_METRICS),
        status="watching",
        append_md=append_md,
    )
    to_append.append(entry)
    append_coach_ledger(memory_dir, to_append)
    return entry


def _judge_metric(
    metric: str,
    applied_on: date,
    as_of: date,
    stats_dir: Path,
) -> tuple[str | None, float | None]:
    """metric 単位: ('pass'|'fail'|None, median_index)。

    None = 測定可能日不足で判定不能。
    """
    post_start = applied_on
    post_end = applied_on + timedelta(days=_JUDGE_DAYS - 1)
    # 判定窓は applied_on から7日。as_of が窓完了日以降であることが前提
    pre_end = applied_on - timedelta(days=1)
    pre_stats = load_stats(stats_dir, days=_PRE_BASELINE_DAYS, end_day=pre_end)
    post_stats = load_stats(
        stats_dir,
        days=(post_end - post_start).days + 1,
        end_day=post_end,
    )
    indices: list[float] = []
    for s in post_stats:
        raw = s.get("day")
        if not raw:
            continue
        try:
            d = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if not (post_start <= d <= post_end):
            continue
        v = metric_from_stats(metric, s)
        if v is None:
            continue
        wb = weekday_baseline(metric, d, pre_stats)
        if wb is None or wb == 0:
            continue
        indices.append(float(v) / float(wb) * 100.0)
    if len(indices) < _MIN_MEASURABLE:
        return None, None
    med = float(median(indices))
    return ("pass" if med <= 100.0 else "fail"), med


def judge_coach_entries(
    memory_dir: Path,
    stats_dir: Path,
    *,
    as_of: date,
    redactor: Callable[[str], str] | None = None,
) -> list[CoachLedgerEntry]:
    """watching かつ applied_on+7日 到達を一発判定。結果エントリを追記して返す。"""
    existing = load_coach_ledger(memory_dir)
    results: list[CoachLedgerEntry] = []
    for e in existing:
        if e.status != "watching":
            continue
        if not e.applied_on:
            continue
        try:
            applied = date.fromisoformat(e.applied_on[:10])
        except ValueError:
            continue
        if as_of < applied + timedelta(days=_JUDGE_DAYS - 1):
            continue
        metrics = e.watch_metrics or list(WATCH_METRICS)
        judged: list[tuple[str, str, float]] = []  # metric, pass/fail, median
        for m in metrics:
            verdict, med = _judge_metric(m, applied, as_of, stats_dir)
            if verdict is None or med is None:
                continue
            judged.append((m, verdict, med))
        if not judged:
            detail = "測定可能日不足のため判定不能"
            if redactor:
                detail = redactor(detail)
            updated = CoachLedgerEntry(
                id=e.id,
                applied_on=e.applied_on,
                proposal_file=e.proposal_file,
                targets=list(e.targets),
                evidence=list(e.evidence),
                watch_metrics=list(metrics),
                status="unmeasurable",
                verdict_date=as_of.isoformat(),
                verdict_detail=detail,
                append_md=e.append_md,
            )
            results.append(updated)
            continue
        fails = sum(1 for _, v, _ in judged if v == "fail")
        overall = "fail" if fails * 2 > len(judged) else "pass"
        detail = " / ".join(
            f"{m} 中央値{med:g}" for m, _, med in judged
        )
        if redactor:
            detail = redactor(detail)
        updated = CoachLedgerEntry(
            id=e.id,
            applied_on=e.applied_on,
            proposal_file=e.proposal_file,
            targets=list(e.targets),
            evidence=list(e.evidence),
            watch_metrics=list(metrics),
            status=overall,
            verdict_date=as_of.isoformat(),
            verdict_detail=detail,
            append_md=e.append_md,
        )
        results.append(updated)
    append_coach_ledger(memory_dir, results)
    return results


def rollback_path(memory_dir: Path, as_of: date, ledger_id: str) -> Path:
    return Path(memory_dir) / "coach" / f"{as_of.isoformat()}-rollback-{ledger_id}.md"


def generate_rollback_proposal(
    memory_dir: Path,
    entry: CoachLedgerEntry,
    *,
    as_of: date,
) -> Path | None:
    """FAIL エントリ用ロールバック提案。同一 id があれば None（再生成しない）。"""
    path = rollback_path(memory_dir, as_of, entry.id)
    # 既存探索: 任意日付の rollback ファイル
    coach_dir = Path(memory_dir) / "coach"
    if coach_dir.is_dir():
        for p in coach_dir.glob(f"*-rollback-{entry.id}.md"):
            return None
    append_md = entry.append_md or ""
    if not append_md and entry.proposal_file:
        prop = Path(memory_dir) / entry.proposal_file
        if prop.is_file():
            try:
                from .coach import _parse_proposal_text
                from .vault import read_text_preserve_newlines

                _, append_md = _parse_proposal_text(
                    read_text_preserve_newlines(prop)
                )
            except Exception:
                append_md = ""
    content = (
        "---\n"
        f"date: {as_of.isoformat()}\n"
        "kind: rollback\n"
        f"ledger_id: {entry.id}\n"
        "applied: false\n"
        "---\n\n"
        f"# コーチロールバック提案 {entry.id}\n\n"
        f"判定: FAIL（{entry.verdict_detail or ''}）\n\n"
        "## 除去対象\n\n"
        f"{append_md.rstrip()}\n\n"
        "## 判定根拠\n\n"
        f"{entry.verdict_detail or '（詳細なし）'}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content)
    return path


def mark_rolled_back(
    memory_dir: Path, ledger_id: str, *, as_of: date
) -> CoachLedgerEntry | None:
    existing = load_coach_ledger(memory_dir)
    src = next((e for e in existing if e.id == ledger_id), None)
    if src is None:
        return None
    updated = CoachLedgerEntry(
        id=src.id,
        applied_on=src.applied_on,
        proposal_file=src.proposal_file,
        targets=list(src.targets),
        evidence=list(src.evidence),
        watch_metrics=list(src.watch_metrics),
        status="rolled_back",
        verdict_date=as_of.isoformat(),
        verdict_detail=src.verdict_detail,
        append_md=src.append_md,
    )
    append_coach_ledger(memory_dir, [updated])
    return updated


def format_coach_status_line(entries: list[CoachLedgerEntry]) -> str | None:
    if not entries:
        return None
    watching = sum(1 for e in entries if e.status == "watching")
    judged = [e for e in entries if e.status in ("pass", "fail")]
    if watching == 0 and not judged:
        return None
    if judged:
        latest = sorted(
            judged, key=lambda e: e.verdict_date or e.applied_on or "", reverse=True
        )[0]
        return (
            f"🎓 コーチ: 監視中{watching}件 / 直近判定 "
            f"{latest.status.upper()}({latest.id})"
        )
    return f"🎓 コーチ: 監視中{watching}件"


def format_coach_weekly_section(entries: list[CoachLedgerEntry]) -> str | None:
    wins = sum(1 for e in entries if e.status == "pass")
    losses = sum(1 for e in entries if e.status == "fail")
    watching = sum(1 for e in entries if e.status == "watching")
    if wins + losses == 0 and watching == 0:
        return None
    total = wins + losses
    if total:
        pct = round(100 * wins / total)
        head = f"🎓 コーチ勝率: {wins}勝{losses}敗({pct}%)・監視中{watching}件"
    else:
        head = f"🎓 コーチ勝率: 判定なし・監視中{watching}件"
    lines = ["## 🎓 コーチ勝率", "", f"- {head}"]
    fails = [
        e
        for e in entries
        if e.status == "fail"
    ]
    fails.sort(key=lambda e: e.verdict_date or "", reverse=True)
    if fails:
        f0 = fails[0]
        lines.append(f"- 直近FAIL: {f0.id} — {f0.verdict_detail or ''}")
    return "\n".join(lines)


def format_f18_lines(entries: Sequence[CoachLedgerEntry]) -> list[str]:
    watching = sum(1 for e in entries if e.status == "watching")
    judged = [e for e in entries if e.status in ("pass", "fail")]
    if watching == 0 and not judged:
        return []
    if judged:
        latest = sorted(
            judged, key=lambda e: e.verdict_date or "", reverse=True
        )[0]
        return [
            f"- [F18] コーチ: 直近判定 {latest.status.upper()}({latest.id})"
            f" / 監視中{watching}件"
        ]
    return [f"- [F18] コーチ: 監視中{watching}件"]
