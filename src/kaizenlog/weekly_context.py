"""週次事前集約: weekly-kaizen が一次データとして読む決定論 Markdown。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from .aiwork import top_friction_sessions
from .experiments import (
    detect_regressions,
    format_effect_size,
    load_experiments,
    target_met,
)
from .memory import load_entries
from .stats import load_stats
from .vault import WEEKLY_CONTEXT_MARKER, atomic_write_text, upsert_section


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


def iso_week_label(week_start: date) -> str:
    iso = week_start.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def weekly_review_path(daily_notes_dir: Path, week_start: date) -> Path:
    """Weekly Reviews/YYYY-Www.md"""
    return Path(daily_notes_dir) / "Weekly Reviews" / f"{iso_week_label(week_start)}.md"


def expired_recommendation(hits: int, n: int) -> str:
    """expired 実験の採否推奨ラベル（表示のみ。frontmatter は書かない）。

    自動で status を adopted/rejected に書き換えない理由:
    frontmatter 簡易パーサでの書換はノート破損リスクが高く、
    採否の最終決定は人間 / weekly-kaizen スキルに残す。
    """
    if n <= 0:
        return "実測不足"
    if hits * 2 > n:
        return "✅採用推奨"
    return "❌棄却推奨"


def _format_token_count(n: float) -> str:
    """123456 → 123k のような短縮。"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        # 整数寄りは k
        if abs(n - round(n / 1000) * 1000) < 50:
            return f"{int(round(n / 1000))}k"
        return f"{n / 1000:.1f}k"
    return f"{int(n)}"


def format_ai_tokens_week_line(stats_list: list[dict]) -> str:
    """AI トークン週計行。v1（output_tokens 無し）の日は分母除外。全日欠損は -。"""
    totals: list[float] = []
    for s in stats_list:
        ai = s.get("ai") if isinstance(s.get("ai"), dict) else {}
        v = ai.get("output_tokens")
        if isinstance(v, (int, float)):
            totals.append(float(v))
    if not totals:
        return "AIトークン: -"
    week_sum = sum(totals)
    day_avg = week_sum / len(totals)
    return (
        f"AIトークン: 週計 {_format_token_count(week_sum)}"
        f" / 日平均 {_format_token_count(day_avg)}"
        f"（{len(totals)}日分の v2 統計）"
    )


def render_weekly_context(
    stats_dir: Path,
    memory_dir: Path,
    experiments_dir: Path,
    week_start: date,
) -> str:
    """対象週（月曜始まり7日）の集約 Markdown。LLM 不使用。"""
    days = [week_start + timedelta(days=i) for i in range(7)]
    week_label = iso_week_label(week_start)
    lines: list[str] = [
        f"# 週次コンテキスト {week_label}",
        f"対象: {days[0].isoformat()} 〜 {days[-1].isoformat()}（月曜始まり）",
        "",
        "## 日別カテゴリと合計",
        "",
    ]

    week_cat: dict[str, float] = {}
    ai_rows: list[str] = []
    week_stats: list[dict] = []
    for d in days:
        loaded = load_stats(stats_dir, 1, d)
        if not loaded:
            lines.append(f"- {d.isoformat()}: 記録なし")
            continue
        s = loaded[0]
        week_stats.append(s)
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
    lines.append("")
    lines.append(f"- {format_ai_tokens_week_line(week_stats)}")

    # 摩擦ワースト: 日次は決定論シグナルのみ。質の LLM 判定は週次スキルへ
    # （ai_work_deep_review の観点でマーカー外に書く）
    digests: list[dict] = []
    for s in week_stats:
        ai = s.get("ai") if isinstance(s.get("ai"), dict) else {}
        raw = ai.get("session_digests")
        if isinstance(raw, list):
            for d in raw:
                if isinstance(d, dict):
                    digests.append(d)
    worst = top_friction_sessions(digests, limit=3)
    lines.extend(
        [
            "",
            "## ⚠ 摩擦ワーストセッション",
            "",
            "スコア = ツールエラー + 中断×5 + リトライ関与×5。"
            "日次は数値・内容抜粋のみ。入力/出力の質の判定は週次 LLM に委ねる。",
            "",
        ]
    )
    if not worst:
        lines.append("- （摩擦セッションなし、または session_digests 未保存）")
    else:
        for i, d in enumerate(worst, 1):
            day = d.get("day") or "?"
            proj = d.get("project") or "?"
            title = (d.get("title") or "（内容なし）").strip() or "（内容なし）"
            # 一行化
            title = " ".join(str(title).split())
            err = int(d.get("tool_errors") or 0)
            inter = int(d.get("interruptions") or 0)
            retry = int(d.get("retry_touch") or 0)
            score = err + inter * 5 + retry * 5
            ended = " / 末尾エラー" if d.get("ended_in_error") else ""
            tests = " / テスト実行あり" if d.get("tests_run") else ""
            edits = int(d.get("edits") or 0)
            lines.append(
                f"{i}. {day} 「{proj}」 score={score} "
                f"（エラー{err}・中断{inter}・変更{edits}{tests}{ended}）: {title}"
            )
    lines.append("")

    # アクション実績（superseded 除外は compute_action_stats 側）
    entries = load_entries(memory_dir)
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

    # 実験（採否は表示のみ。frontmatter status は書き換えない — 人間/スキルが最終決定）
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
            rec = expired_recommendation(0, 0)
            lines.append(
                f"- expired 「{exp.title}」: 実測なし（達成率 -）{es_part} → {rec}"
            )
            continue
        hits = sum(
            1
            for v in exp.measurements.values()
            if target_met(v, exp.target_op, exp.target_value)
        )
        n = len(exp.measurements)
        rec = expired_recommendation(hits, n)
        lines.append(
            f"- expired 「{exp.title}」: 達成率 {hits}/{n}"
            f"（{round(100 * hits / n)}%）{es_part} → {rec}"
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
    lines.append(
        "- 採否推奨は表示のみ（frontmatter の status は自動変更しない）。"
    )
    lines.append("")
    return "\n".join(lines)


def _default_weekly_frontmatter(week_start: date) -> str:
    label = iso_week_label(week_start)
    return (
        "---\n"
        f'title: "{label} Weekly Review"\n'
        f"date: {week_start.isoformat()}\n"
        "tags: [type/weekly-review]\n"
        "---\n"
    )


def write_weekly_context(
    daily_notes_dir: Path,
    body_md: str,
    week_start: date,
) -> Path:
    """Weekly Reviews ノートの weekly-context マーカー区間へ upsert（マーカー外は不変）。"""
    path = weekly_review_path(daily_notes_dir, week_start)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        content = path.read_text(encoding="utf-8")
    else:
        content = _default_weekly_frontmatter(week_start) + "\n"
    updated = upsert_section(content, WEEKLY_CONTEXT_MARKER, body_md, position="top")
    atomic_write_text(path, updated)
    return path
