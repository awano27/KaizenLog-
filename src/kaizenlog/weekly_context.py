"""週次事前集約: weekly-kaizen が一次データとして読む決定論 Markdown。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from .experiments import (
    detect_regressions,
    format_effect_size,
    load_experiments,
    target_met,
)
from .memory import compute_action_stats, load_entries
from .stats import load_stats


def monday_of(d: date) -> date:
    """月曜始まりの週の初日。"""
    return d - timedelta(days=d.weekday())


def parse_iso_week(week: str) -> date:
    """YYYY-Www → その週の月曜日。"""
    s = week.strip().upper()
    if "-W" not in s:
        raise ValueError(f"週指定は YYYY-Www 形式です: {week!r}")
    year_s, _, w_s = s.partition("-W")
    year = int(year_s)
    w = int(w_s)
    # ISO 週: その年の第 w 週の月曜
    return date.fromisocalendar(year, w, 1)


def render_weekly_context(
    stats_dir: Path,
    memory_dir: Path,
    experiments_dir: Path,
    week_start: date,
) -> str:
    """対象週（月曜始まり7日）の集約 Markdown。LLM 不使用。"""
    days = [week_start + timedelta(days=i) for i in range(7)]
    week_label = f"{week_start.isocalendar()[0]}-W{week_start.isocalendar()[1]:02d}"
    lines: list[str] = [
        f"# 週次コンテキスト {week_label}",
        f"対象: {days[0].isoformat()} 〜 {days[-1].isoformat()}（月曜始まり）",
        "",
        "## 日別カテゴリと合計",
        "",
    ]

    week_cat: dict[str, float] = {}
    ai_rows: list[str] = []
    for d in days:
        loaded = load_stats(stats_dir, 1, d)
        if not loaded:
            lines.append(f"- {d.isoformat()}: 記録なし")
            continue
        s = loaded[0]
        by_cat = s.get("by_category") if isinstance(s.get("by_category"), dict) else {}
        total = s.get("total_minutes", 0)
        cat_bits = ", ".join(
            f"{k} {float(v):.0f}分"
            for k, v in sorted(by_cat.items(), key=lambda x: -float(x[1]))[:5]
        ) or "（カテゴリなし）"
        lines.append(f"- {d.isoformat()}: 合計 {float(total):.0f}分 / {cat_bits}")
        for k, v in by_cat.items():
            week_cat[k] = week_cat.get(k, 0.0) + float(v)
        ai = s.get("ai") if isinstance(s.get("ai"), dict) else {}
        cost = ai.get("est_cost_usd")
        cost_s = f"${float(cost):.2f}" if isinstance(cost, (int, float)) else "-"
        sources = ai.get("sources") if isinstance(ai.get("sources"), dict) else {}
        src_s = ", ".join(f"{n}:{b.get('sessions', 0)}" for n, b in sources.items()) or "-"
        ai_rows.append(
            f"| {d.isoformat()} | {ai.get('sessions', 0)} | {ai.get('fragmented', 0)} | "
            f"{ai.get('tool_errors', 0)} | {ai.get('interruptions', 0)} | "
            f"{ai.get('retry_chains', 0)} | {cost_s} | {src_s} |"
        )

    lines.append("")
    lines.append("### 週合計（カテゴリ）")
    if week_cat:
        for k, v in sorted(week_cat.items(), key=lambda x: -x[1]):
            lines.append(f"- {k}: {v:.0f}分")
    else:
        lines.append("- （データなし）")

    lines.extend(
        [
            "",
            "## AIテレメトリ週次推移",
            "",
            "| 日 | セッション | 細切れ | エラー | 中断 | リトライ | 推定コスト | ソース |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    if ai_rows:
        lines.extend(ai_rows)
    else:
        lines.append("| （記録なし） |  |  |  |  |  |  |  |")

    # アクション実績（superseded 除外は compute_action_stats 側）
    entries = load_entries(memory_dir)
    # 週末日を「今日」相当にして窓内集計: 週内提案は end+1 を today として window 14 で
    # より正確には週内 date の proposed/done を直接集計
    week_start_s = days[0].isoformat()
    week_end_s = days[-1].isoformat()
    week_entries = [
        e
        for e in entries
        if e.status != "superseded" and week_start_s <= e.date <= week_end_s
    ]
    proposed = len(week_entries)
    done = sum(1 for e in week_entries if e.status == "done")
    judged = sum(1 for e in week_entries if e.verdict in ("pass", "fail"))
    passed = sum(1 for e in week_entries if e.verdict == "pass")
    open_list = [e for e in week_entries if e.status == "proposed"]
    done_rate = f"{round(100 * done / proposed)}%" if proposed else "-"
    pass_rate = f"{round(100 * passed / judged)}%" if judged else "-"
    lines.extend(
        [
            "",
            "## アクション実績",
            "",
            f"- 週の提案: {proposed}件 / 消化: {done}件（{done_rate}） / "
            f"判定: {judged}件 / PASS: {passed}件（{pass_rate}）",
            "",
            "### 未完了",
        ]
    )
    if open_list:
        for e in sorted(open_list, key=lambda x: x.id):
            lines.append(f"- {e.id}: {e.action[:80]}")
    else:
        lines.append("- （なし）")

    # 実験
    experiments = load_experiments(experiments_dir)
    lines.extend(["", "## 実験サマリー", ""])
    running = [e for e in experiments if e.status == "running"]
    expired = [e for e in experiments if e.status == "expired"]
    adopted = [e for e in experiments if e.status == "adopted"]
    for exp in running:
        es = format_effect_size(exp)
        es_part = f" / {es}" if es else ""
        if exp.measurements:
            last_d = max(exp.measurements)
            lines.append(
                f"- running 「{exp.title}」: 直近 {last_d} = {exp.measurements[last_d]:g}"
                f"{es_part}"
            )
        else:
            lines.append(f"- running 「{exp.title}」: 実測なし{es_part}")
    for exp in expired:
        es = format_effect_size(exp)
        es_part = f" / {es}" if es else ""
        if not exp.measurements:
            lines.append(f"- expired 「{exp.title}」: 実測なし（達成率 -）{es_part}")
            continue
        hits = sum(
            1
            for v in exp.measurements.values()
            if target_met(v, exp.target_op, exp.target_value)
        )
        n = len(exp.measurements)
        lines.append(
            f"- expired 「{exp.title}」: 達成率 {hits}/{n}"
            f"（{round(100 * hits / n)}%）{es_part}"
        )
    regs = detect_regressions(adopted, window=7, as_of=days[-1])
    if regs:
        for exp in regs:
            lines.append(f"- ⚠ 退行: adopted 「{exp.title}」")
    elif adopted:
        lines.append(f"- adopted: {len(adopted)}件（直近7日の退行なし）")

    lines.append(
        "- 注意: PC前景のみ計測。カテゴリ時間の減少はデバイス移行（風船効果）の可能性あり。"
    )
    lines.append("")
    return "\n".join(lines)
