"""月次実績レポート（決定論）。日次 stats の effort / outcome_git と台帳を合算する。"""

from __future__ import annotations

import json
from calendar import monthrange
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .effort import BUCKET_PRIVATE, _fmt_minutes_plain
from .memory import MemoryEntry
from .report import _fmt_minutes
from .vault import MONTHLY_MARKER, DailyNoteStore, atomic_write_text, upsert_section


@dataclass
class MonthlyReport:
    year: int
    month: int
    work_days: int = 0
    total_minutes: float = 0.0
    project_minutes: dict[str, float] = field(default_factory=dict)
    project_days: dict[str, set[str]] = field(default_factory=dict)
    commits: dict[str, dict[str, int]] = field(default_factory=dict)
    proposed: int = 0
    done: int = 0
    pass_n: int = 0
    fail_n: int = 0
    undone_pass: int = 0
    days_without_effort: int = 0
    days_scanned: int = 0


def _month_days(year: int, month: int) -> list[date]:
    last = monthrange(year, month)[1]
    return [date(year, month, d) for d in range(1, last + 1)]


def load_month_stats(stats_dir: Path, year: int, month: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in _month_days(year, month):
        p = Path(stats_dir) / f"{d.isoformat()}.json"
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def aggregate_monthly(
    stats_list: Sequence[Mapping[str, Any]],
    entries: Sequence[MemoryEntry],
    *,
    year: int,
    month: int,
) -> MonthlyReport:
    rep = MonthlyReport(year=year, month=month)
    for st in stats_list:
        rep.days_scanned += 1
        day_s = str(st.get("day") or "")
        effort = st.get("effort")
        if not isinstance(effort, Mapping) or not isinstance(effort.get("minutes"), Mapping):
            rep.days_without_effort += 1
            continue
        mins_map = effort["minutes"]
        day_total = float(effort.get("total_minutes") or 0.0)
        if day_total <= 0:
            day_total = sum(float(v) for v in mins_map.values() if isinstance(v, (int, float)))
        # 業務分のみを稼働日にカウント（私的のみの日は稼働日にしない）
        work_day = False
        for name, raw in mins_map.items():
            m = float(raw or 0)
            if m <= 0:
                continue
            rep.project_minutes[str(name)] = rep.project_minutes.get(str(name), 0.0) + m
            if str(name) != BUCKET_PRIVATE:
                work_day = True
            if day_s:
                rep.project_days.setdefault(str(name), set()).add(day_s)
        if work_day:
            rep.work_days += 1
            rep.total_minutes += day_total - float(mins_map.get(BUCKET_PRIVATE) or 0)

        # outcome_git
        og = st.get("outcome_git")
        if isinstance(og, list):
            for item in og:
                if not isinstance(item, Mapping):
                    continue
                label = str(item.get("repo_label") or "repo")
                bucket = rep.commits.setdefault(
                    label, {"commits": 0, "insertions": 0, "deletions": 0}
                )
                bucket["commits"] += int(item.get("commits") or 0)
                bucket["insertions"] += int(item.get("insertions") or 0)
                bucket["deletions"] += int(item.get("deletions") or 0)

    # 台帳: 当月提案
    prefix = f"{year:04d}-{month:02d}-"
    month_entries = [e for e in entries if (e.date or "").startswith(prefix)]
    # status は後勝ち load 済み想定
    by_id: dict[str, MemoryEntry] = {}
    for e in month_entries:
        by_id[e.id] = e
    for e in by_id.values():
        rep.proposed += 1
        if e.status == "done":
            rep.done += 1
        if e.verdict == "pass" and e.verdict_stage == "confirmed":
            rep.pass_n += 1
            if e.status != "done":
                rep.undone_pass += 1
        elif e.verdict == "fail" and e.verdict_stage == "confirmed":
            rep.fail_n += 1

    # 当月に判定日があるエントリも少し見る（提案は前月でも当月判定）
    # 提案数は当月提案のみ。指標達成は当月 confirmed 全体を見ると重複し得るので
    # 上の month_entries に限定する。

    return rep


def render_monthly_markdown(rep: MonthlyReport) -> str:
    label = f"{rep.year:04d}-{rep.month:02d}"
    lines = [f"## 📅 {label} の実績", ""]
    if rep.work_days <= 0:
        lines.append("工数記録のある稼働日がありません。")
        lines.append("")
        if rep.days_without_effort:
            lines.append(
                f"※ {rep.days_without_effort}日分は工数記録がないため集計対象外です。"
            )
            lines.append("")
        return "\n".join(lines)

    avg = rep.total_minutes / rep.work_days if rep.work_days else 0.0
    lines.append(
        f"稼働 {rep.work_days}日 / 合計 {_fmt_minutes(rep.total_minutes)} / "
        f"1日平均 {_fmt_minutes(avg)}（工数記録のある日のみ）"
    )
    lines.append("")
    lines.append("### プロジェクト別")
    lines.append("")
    lines.append("| つけ先 | 時間 | 割合 | 稼働日数 |")
    lines.append("| --- | ---: | ---: | ---: |")
    total = rep.total_minutes or 1.0
    # 私的を最後
    items = sorted(
        ((k, v) for k, v in rep.project_minutes.items() if k != BUCKET_PRIVATE and v > 0),
        key=lambda x: (-x[1], x[0]),
    )
    priv = rep.project_minutes.get(BUCKET_PRIVATE, 0.0)
    for name, mins in items:
        pct = mins / total * 100
        days_n = len(rep.project_days.get(name, set()))
        lines.append(
            f"| {name} | {_fmt_minutes(mins)} | {pct:.0f}% | {days_n}日 |"
        )
    if priv > 0:
        pct = priv / (rep.total_minutes + priv) * 100 if (rep.total_minutes + priv) else 0
        days_n = len(rep.project_days.get(BUCKET_PRIVATE, set()))
        lines.append(
            f"| {BUCKET_PRIVATE} | {_fmt_minutes(priv)} | — | {days_n}日 |"
        )
    lines.append("")

    if rep.commits:
        lines.append("### 成果")
        lines.append("")
        for label_r, c in sorted(rep.commits.items(), key=lambda x: (-x[1]["commits"], x[0])):
            if c["commits"] <= 0:
                continue
            lines.append(
                f"- {label_r}: コミット {c['commits']}件"
                f"（+{c['insertions']}/-{c['deletions']}）"
            )
        lines.append("")

    lines.append("### 改善アクション")
    lines.append("")
    lines.append(
        f"- 提案 {rep.proposed}件 / チェック完了 {rep.done}件 / "
        f"指標達成 {rep.pass_n}件（うちチェックなし {rep.undone_pass}件）"
    )
    if rep.fail_n:
        lines.append(f"- 指標未達 {rep.fail_n}件")
    lines.append("")

    if rep.days_without_effort:
        lines.append(
            f"※ {rep.days_without_effort}日分は工数記録がないため集計対象外です。"
        )
        lines.append("")
    return "\n".join(lines)


def write_monthly(
    vault_monthly_dir: Path,
    year: int,
    month: int,
    body: str,
) -> Path:
    """monthly_dir/YYYY-MM.md の MONTHLY_MARKER 区間を更新。"""
    vault_monthly_dir = Path(vault_monthly_dir)
    vault_monthly_dir.mkdir(parents=True, exist_ok=True)
    path = vault_monthly_dir / f"{year:04d}-{month:02d}.md"
    if path.is_file():
        content = path.read_text(encoding="utf-8")
    else:
        content = f"# {year:04d}-{month:02d}\n\n"
    atomic_write_text(
        path, upsert_section(content, MONTHLY_MARKER, body, position="bottom")
    )
    return path
