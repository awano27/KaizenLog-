"""Kaizen実験ループ: 改善提案を「検証可能な実験」として追跡する。

実験は `03 Areas/Kaizen Experiments/` 配下のノート（frontmatter付きMarkdown）。
毎晩の `kaizenlog generate` が対象日の実測値を計算してMeasurementsテーブルに
自動追記し、期限が来た実験は status: expired にして週次レビューの判定に回す。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .aiwork import AISession
from .focus import InputStats
from .report import DailySummary
from .vault import upsert_section

MEASUREMENTS_MARKER = "kaizenlog:measurements"

# 実験で追跡できる指標。値は (説明, 計算関数名) ではなく直接計算する。
METRIC_DESCRIPTIONS = {
    "context_switches": "コンテキストスイッチ回数",
    "total_active_minutes": "合計アクティブ時間（分）",
    "ai_sessions": "AIツールの利用セッション数（画面ベース）",
    "ai_cc_sessions": "Claude Codeセッション数",
    "ai_fragmented_sessions": "2往復以下の細切れClaude Codeセッション数",
    "ai_tool_errors": "Claude Codeのツールエラー回数",
    "ai_interruptions": "Claude Codeのユーザー中断・拒否回数",
    "ai_avg_turns": "Claude Codeセッションの平均往復数",
    "category_minutes:<カテゴリ名>": "指定カテゴリの時間（分）例: category_minutes:エンタメ",
    "site_minutes:<ドメイン>": "指定サイトの時間（分）例: site_minutes:youtube.com（要 aw-watcher-web）",
    "focus_blocks": "集中ブロック数（25分以上入力が続いた区間。要 aw-watcher-input）",
    "focus_minutes": "集中ブロックの合計時間（分。要 aw-watcher-input）",
    "input_keypresses": "1日のキー入力数（要 aw-watcher-input）",
}

_TARGET_RE = re.compile(r"^(<=|>=|<|>|==?)\s*([\d.]+)$")


@dataclass
class Experiment:
    path: Path
    title: str
    status: str  # running | adopted | rejected | expired
    metric: str
    target_op: str
    target_value: float
    baseline: float | None = None
    deadline: date | None = None
    measurements: dict[date, float] = field(default_factory=dict)


class ExperimentError(ValueError):
    pass


def parse_target(target: str) -> tuple[str, float]:
    m = _TARGET_RE.match(target.strip())
    if not m:
        raise ExperimentError(
            f"target の形式が不正です: {target!r}（例: \"<= 15\", \">= 120\"）"
        )
    op = "==" if m.group(1) == "=" else m.group(1)
    return op, float(m.group(2))


def target_met(value: float, op: str, target_value: float) -> bool:
    # 0.1+0.2 != 0.3 のような浮動小数点誤差で判定を誤らないよう許容誤差付きで比較する
    close = math.isclose(value, target_value, rel_tol=1e-9, abs_tol=1e-9)
    return {
        "<=": value <= target_value or close,
        ">=": value >= target_value or close,
        "<": value < target_value and not close,
        ">": value > target_value and not close,
        "==": close,
    }[op]


def compute_metric(
    metric: str,
    summary: DailySummary,
    cc_sessions: list[AISession],
    input_stats: InputStats | None = None,
) -> float | None:
    """対象日の指標値を計算する。未知の指標・データ不足はNone。"""
    if metric in ("focus_blocks", "focus_minutes", "input_keypresses"):
        if input_stats is None:
            return None  # aw-watcher-input 未導入の日は計測不能
        if metric == "focus_blocks":
            return float(len(input_stats.focus_blocks))
        if metric == "focus_minutes":
            return round(input_stats.focus_minutes, 1)
        return float(input_stats.keypresses)
    if metric.startswith("site_minutes:"):
        site = metric.split(":", 1)[1].strip().lower()
        return round(summary.by_site.get(site, 0.0), 1)
    if metric == "context_switches":
        return float(summary.context_switches)
    if metric == "total_active_minutes":
        return round(summary.total_minutes, 1)
    if metric == "ai_sessions":
        return float(summary.ai_sessions)
    if metric == "ai_cc_sessions":
        return float(len(cc_sessions))
    if metric == "ai_fragmented_sessions":
        return float(sum(1 for s in cc_sessions if s.is_fragmented))
    if metric == "ai_tool_errors":
        return float(sum(s.tool_errors for s in cc_sessions))
    if metric == "ai_interruptions":
        return float(sum(s.interruptions for s in cc_sessions))
    if metric == "ai_avg_turns":
        if not cc_sessions:
            return 0.0
        return round(sum(s.user_turns for s in cc_sessions) / len(cc_sessions), 1)
    if metric.startswith("category_minutes:"):
        category = metric.split(":", 1)[1].strip()
        return round(summary.by_category.get(category, 0.0), 1)
    return None


# ---- frontmatter（簡易パーサ。実験ノートは単純なスカラー値のみ使う） ----

def _parse_frontmatter(content: str) -> tuple[dict[str, str], bool]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, False
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields, True
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip('"').strip("'")
    return {}, False


def _set_frontmatter_field(content: str, key: str, value: str) -> str:
    """frontmatter内の既存フィールドを書き換える（無ければ何もしない）。"""
    lines = content.splitlines(keepends=True)
    in_fm = False
    for i, line in enumerate(lines):
        if line.strip() == "---":
            if not in_fm and i == 0:
                in_fm = True
                continue
            break
        if in_fm and re.match(rf"^{re.escape(key)}\s*:", line):
            lines[i] = f"{key}: {value}\n"
            break
    return "".join(lines)


def _parse_measurements(content: str) -> dict[date, float]:
    out: dict[date, float] = {}
    for m in re.finditer(
        r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([\d.\-]+)\s*\|", content, re.MULTILINE
    ):
        try:
            out[date.fromisoformat(m.group(1))] = float(m.group(2))
        except ValueError:
            continue
    return out


def load_experiments(experiments_dir: Path) -> list[Experiment]:
    """実験ディレクトリから metric フィールドを持つノートを読み込む。"""
    if not experiments_dir.is_dir():
        return []
    experiments = []
    for path in sorted(experiments_dir.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fields, ok = _parse_frontmatter(content)
        if not ok or "metric" not in fields or "target" not in fields:
            continue
        try:
            op, value = parse_target(fields["target"])
        except ExperimentError:
            continue
        deadline = None
        if fields.get("deadline"):
            try:
                deadline = date.fromisoformat(fields["deadline"])
            except ValueError:
                pass
        baseline = None
        if fields.get("baseline"):
            try:
                baseline = float(fields["baseline"])
            except ValueError:
                pass
        experiments.append(
            Experiment(
                path=path,
                title=fields.get("title", path.stem),
                status=fields.get("status", "running"),
                metric=fields["metric"],
                target_op=op,
                target_value=value,
                baseline=baseline,
                deadline=deadline,
                measurements=_parse_measurements(content),
            )
        )
    return experiments


def record_measurement(exp: Experiment, day: date, value: float) -> bool:
    """実測値を実験ノートのMeasurementsテーブルにupsertする。

    baselineが未設定なら最初の実測値で埋める。期限を過ぎた実験は
    status: expired に更新する。目標達成ならTrueを返す。
    """
    content = exp.path.read_text(encoding="utf-8")

    exp.measurements[day] = value
    met = target_met(value, exp.target_op, exp.target_value)

    rows = ["| 日付 | 値 | 目標達成 |", "| --- | ---: | :-: |"]
    for d in sorted(exp.measurements):
        v = exp.measurements[d]
        mark = "✅" if target_met(v, exp.target_op, exp.target_value) else "❌"
        v_str = f"{v:g}"
        rows.append(f"| {d.isoformat()} | {v_str} | {mark} |")
    section = "## Measurements（自動計測）\n\n" + "\n".join(rows)
    content = upsert_section(content, MEASUREMENTS_MARKER, section)

    if exp.baseline is None:
        exp.baseline = value
        content = _set_frontmatter_field(content, "baseline", f"{value:g}")

    if exp.status == "running" and exp.deadline and day > exp.deadline:
        exp.status = "expired"
        content = _set_frontmatter_field(content, "status", "expired")

    exp.path.write_text(content, encoding="utf-8")
    return met


EXPERIMENT_TEMPLATE = """\
---
title: "{title}"
date: {today}
tags: [type/kaizen-experiment]
status: running
metric: {metric}
target: "{target}"
baseline:
deadline: {deadline}
---

# 🧪 {title}

## Hypothesis（仮説）

{hypothesis}

## Measurements（自動計測）

`kaizenlog generate` が毎晩ここに実測値を追記します。

## Notes

-
"""


def create_experiment(
    experiments_dir: Path,
    title: str,
    metric: str,
    target: str,
    today: date,
    deadline: date,
    hypothesis: str = "",
) -> Path:
    parse_target(target)  # バリデーション
    experiments_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title).strip()
    path = experiments_dir / f"EXP {today.isoformat()} {safe_title}.md"
    if path.exists():
        raise ExperimentError(f"同名の実験が既に存在します: {path}")
    path.write_text(
        EXPERIMENT_TEMPLATE.format(
            title=title,
            today=today.isoformat(),
            metric=metric,
            target=target,
            deadline=deadline.isoformat(),
            hypothesis=hypothesis or "（なぜこの変更が効くと考えるか）",
        ),
        encoding="utf-8",
    )
    return path


def render_experiments_context(experiments: list[Experiment], max_points: int = 5) -> str:
    """LLMプロンプト用に、実行中/期限切れの実験と直近の実測値を要約する。"""
    active = [e for e in experiments if e.status in ("running", "expired")]
    if not active:
        return ""
    lines = []
    for e in active:
        target = f"{e.metric} {e.target_op} {e.target_value:g}"
        recent = sorted(e.measurements)[-max_points:]
        points = ", ".join(
            f"{d.strftime('%m/%d')}={e.measurements[d]:g}" for d in recent
        ) or "実測なし"
        baseline = f"開始時 {e.baseline:g} / " if e.baseline is not None else ""
        lines.append(
            f"- 「{e.title}」[{e.status}] 目標: {target}"
            f"（{baseline}直近: {points}、期限: {e.deadline or '未設定'}）"
        )
    return "\n".join(lines)
