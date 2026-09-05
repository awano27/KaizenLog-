"""Kaizen実験ループ: 改善提案を「検証可能な実験」として追跡する。

実験は `03 Areas/Kaizen Experiments/` 配下のノート（frontmatter付きMarkdown）。
毎晩の `kaizenlog generate` が対象日の実測値を計算してMeasurementsテーブルに
自動追記し、期限が来た実験は status: expired にして週次レビューの判定に回す。
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import median

from .aiwork import AISession
from .focus import InputStats
from .report import DailySummary
from .vault import atomic_write_text, extract_section, upsert_section

MEASUREMENTS_MARKER = "kaizenlog:measurements"

# adopted 後も deadline からこの日数以内は定着監視のため計測を続ける
_ADOPTED_MONITOR_DAYS = 30

# 実験で追跡できる指標。値は (説明, 計算関数名) ではなく直接計算する。
# ai_* は Claude Code / Codex 等アダプタ合算。単一製品名だけの説明にしない。
# 注記ラベルは括弧前で切るため、説明文にネスト括弧を入れない（・ で補足）。
METRIC_DESCRIPTIONS = {
    "context_switches": "コンテキストスイッチ回数",
    "context_switches_per_hour": "1時間あたりのカテゴリ変更回数",
    "total_active_minutes": "合計アクティブ時間（分）",
    "ai_tool_errors_per_session": "AI CLIセッション1回あたりのツールエラー回数",
    "ai_activity_blocks": "AIツールの画面アクティビティブロック数",
    "ai_sessions": "AIツールの画面アクティビティブロック数（旧名・互換用）",
    "ai_cc_sessions": "AI CLIセッション数・Claude Code/Codex合算",
    "ai_fragmented_sessions": "2往復以下の細切れAI CLIセッション数",
    "ai_retry_chains": "リトライ連鎖数（30分以内のほぼ同文の再依頼）",
    "ai_tool_errors": "AI CLI合算のツールエラー回数",
    "loop_tax_episodes": "ループ税エピソード数・ai.loop_tax.episode_count",
    "ai_interruptions": "AI CLI合算のユーザー中断・拒否回数",
    "ai_avg_turns": "AI CLIセッションの平均往復数",
    "ai_output_tokens": "AI CLI合算の応答トークン量",
    "category_minutes:<カテゴリ名>": "指定カテゴリの時間（分）例: category_minutes:エンタメ",
    "site_minutes:<ドメイン>": "指定サイトの時間（分）例: site_minutes:youtube.com（要 aw-watcher-web）",
    "focus_blocks": "集中ブロック数",
    "focus_minutes": "集中ブロックの合計時間（分）",
    "input_keypresses": "1日のキー入力数",
    "prompt_cluster:<slug>": (
        "指定クラスタに類似する生依頼の件数/日"
        "（frontmatter の cluster_id: PRM-... を優先。"
        "無ければ cluster_rep。スキル化後の効果測定用）"
    ),
}

_TARGET_RE = re.compile(r"^(<=|>=|<|>|==?)\s*([\d.]+)$")


def metric_display_label(metric: str) -> str | None:
    """PASS 注記用の短い日本語ラベル。未知指標は None。

    注記自体が全角（…）で囲まれるため、ラベル内に括弧を入れない
    （ネストすると strip_pass_annotation / parse_pass_condition が壊れる）。
    """
    if metric in METRIC_DESCRIPTIONS and "<" not in metric:
        return re.split(r"[（(]", METRIC_DESCRIPTIONS[metric], 1)[0].strip()
    if metric.startswith("category_minutes:"):
        cat = metric.split(":", 1)[1].strip()
        return f"{cat}の時間・分" if cat else None
    if metric.startswith("site_minutes:"):
        site = metric.split(":", 1)[1].strip()
        return f"{site}の時間・分" if site else None
    return None


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
    # prompt_cluster 計測用: 正規化済み代表文（機密を含む生文を書かないこと）
    cluster_rep: str | None = None
    # 台帳 ID（PRM-...）。あれば cluster_rep より優先
    cluster_id: str | None = None
    # frontmatter `date`（実験開始日）。不正・欠落は None
    start: date | None = None


class ExperimentError(ValueError):
    pass


def parse_target(target: str) -> tuple[str, float]:
    m = _TARGET_RE.match(target.strip())
    if not m:
        raise ExperimentError(
            f"target の形式が不正です: {target!r}（例: \"<= 15\", \">= 120\"）"
        )
    op = "==" if m.group(1) == "=" else m.group(1)
    try:
        value = float(m.group(2))
    except ValueError as e:
        # "1.2.3" のような値は正規表現を通るがfloat化できない。生のValueErrorの
        # まま漏らすと呼び出し側のexcept ExperimentErrorを素通りして夜間実行が落ちる
        raise ExperimentError(f"target の数値が不正です: {target!r}") from e
    return op, value


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
    retry_chains: int | None = None,
    known_categories: set[str] | frozenset[str] | None = None,
) -> float | None:
    """対象日の指標値を計算する。未知の指標・データ不足はNone。

    known_categories:
      category_minutes で既知集合に無い名前は None（偽PASS防止）。
      None（省略）は後方互換で従来どおり 0.0 フォールバック。
      既知名で当日0分は 0.0 が正当。
    site_minutes:
      欠損は常に 0.0（サイトブロック成功時の0分判定が正当。カテゴリとの非対称）。
    """
    if metric in ("focus_blocks", "focus_minutes", "input_keypresses"):
        if input_stats is None:
            return None  # aw-watcher-input 未導入の日は計測不能
        if metric == "focus_blocks":
            return float(len(input_stats.focus_blocks))
        if metric == "focus_minutes":
            return round(input_stats.focus_minutes, 1)
        return float(input_stats.keypresses)
    if metric.startswith("site_minutes:"):
        # 欠損=0.0 を維持（ブロック成功で0分になった既知ドメインの判定が正当）
        site = metric.split(":", 1)[1].strip().lower()
        return round(summary.by_site.get(site, 0.0), 1)
    if metric == "context_switches":
        return float(summary.context_switches)
    if metric == "context_switches_per_hour":
        # 分母下限: 稼働 < 60 分は未計測（0埋めすると低稼働日が恒久偽PASS）
        if summary.total_minutes < 60:
            return None
        return round(float(summary.context_switches) / summary.total_minutes * 60.0, 1)
    if metric == "total_active_minutes":
        return round(summary.total_minutes, 1)
    if metric in ("ai_activity_blocks", "ai_sessions"):
        return float(summary.ai_activity_blocks)
    if metric == "ai_cc_sessions":
        return float(len(cc_sessions))
    if metric == "ai_fragmented_sessions":
        return float(sum(1 for s in cc_sessions if s.is_fragmented))
    if metric == "ai_retry_chains":
        # 未配線（aiwork 無効など）は未計測。0 は「計測したが連鎖なし」
        if retry_chains is None:
            return None
        return float(retry_chains)
    if metric == "ai_tool_errors":
        return float(sum(s.tool_errors for s in cc_sessions))
    if metric == "ai_tool_errors_per_session":
        if not cc_sessions:
            return None
        total_err = float(sum(s.tool_errors for s in cc_sessions))
        return round(total_err / len(cc_sessions), 1)
    if metric == "ai_interruptions":
        return float(sum(s.interruptions for s in cc_sessions))
    if metric == "ai_avg_turns":
        if not cc_sessions:
            return 0.0
        return round(sum(s.user_turns for s in cc_sessions) / len(cc_sessions), 1)
    if metric == "ai_output_tokens":
        return float(sum(int(s.output_tokens or 0) for s in cc_sessions))
    if metric.startswith("category_minutes:"):
        category = metric.split(":", 1)[1].strip()
        # 未知カテゴリを 0.0 にすると PASS: cat <= N が恒久偽PASSになる
        if known_categories is not None and category not in known_categories:
            return None
        return round(summary.by_category.get(category, 0.0), 1)
    return None


def metric_from_stats(
    metric: str,
    stats: dict,
    known_categories: set[str] | frozenset[str] | None = None,
) -> float | None:
    """日次統計 JSON（stats.write_stats 形式）から指標値を復元する。

    対応:
      context_switches, total_active_minutes,
      ai_activity_blocks / ai_sessions（トップレベルまたは互換）,
      ai_cc_sessions ← ai.sessions,
      ai_fragmented_sessions ← ai.fragmented,
      ai_retry_chains ← ai.retry_chains（旧 stats は None）,
      ai_tool_errors ← ai.tool_errors,
      ai_interruptions ← ai.interruptions,
      ai_avg_turns ← ai.avg_turns / turns_total÷sessions / v1 projects turns 近似,
      ai_output_tokens ← ai.output_tokens（v2。v1 は None）,
      category_minutes:<名> ← by_category,
      site_minutes:<域> ← by_site,
      focus_blocks / focus_minutes / input_keypresses ← input（保存時のみ）

    キー欠損は version 分岐せず None/フォールバック（v1 互換）。
    """
    if not isinstance(stats, dict):
        return None
    ai = stats.get("ai") if isinstance(stats.get("ai"), dict) else {}
    inp = stats.get("input") if isinstance(stats.get("input"), dict) else {}

    if metric == "context_switches":
        v = stats.get("context_switches")
        return float(v) if isinstance(v, (int, float)) else None
    if metric == "context_switches_per_hour":
        cs = stats.get("context_switches")
        mins = stats.get("total_minutes")
        if not isinstance(cs, (int, float)) or not isinstance(mins, (int, float)):
            return None
        if float(mins) < 60:
            return None
        return round(float(cs) / float(mins) * 60.0, 1)
    if metric == "total_active_minutes":
        v = stats.get("total_minutes")
        return float(v) if isinstance(v, (int, float)) else None
    if metric in ("ai_activity_blocks", "ai_sessions"):
        v = stats.get("ai_activity_blocks", stats.get("ai_sessions"))
        return float(v) if isinstance(v, (int, float)) else None
    if metric == "ai_cc_sessions":
        v = ai.get("sessions")
        return float(v) if isinstance(v, (int, float)) else None
    if metric == "ai_fragmented_sessions":
        v = ai.get("fragmented")
        return float(v) if isinstance(v, (int, float)) else None
    if metric == "ai_retry_chains":
        v = ai.get("retry_chains")
        return float(v) if isinstance(v, (int, float)) else None
    if metric == "ai_tool_errors":
        v = ai.get("tool_errors")
        return float(v) if isinstance(v, (int, float)) else None
    if metric == "ai_tool_errors_per_session":
        errs = ai.get("tool_errors")
        sessions = ai.get("sessions")
        if not isinstance(errs, (int, float)) or not isinstance(sessions, (int, float)):
            return None
        if float(sessions) <= 0:
            return None
        return round(float(errs) / float(sessions), 1)
    if metric == "loop_tax_episodes":
        # loop_tax 欠落日は測定不能（None）。episode_count=0 は計測済み
        lt = ai.get("loop_tax")
        if not isinstance(lt, dict):
            return None
        v = lt.get("episode_count")
        return float(v) if isinstance(v, (int, float)) else None
    if metric == "ai_interruptions":
        v = ai.get("interruptions")
        return float(v) if isinstance(v, (int, float)) else None
    if metric == "ai_avg_turns":
        # 1) v2 直接キー
        v = ai.get("avg_turns")
        if isinstance(v, (int, float)):
            return float(v)
        # 2) turns_total ÷ sessions
        turns = ai.get("turns_total")
        sessions = ai.get("sessions")
        if (
            isinstance(turns, (int, float))
            and isinstance(sessions, (int, float))
            and float(sessions) > 0
        ):
            return round(float(turns) / float(sessions), 1)
        if isinstance(sessions, (int, float)) and float(sessions) == 0:
            return 0.0
        # 3) v1 近似: projects[*].turns 合計 ÷ sessions
        projects = ai.get("projects")
        if isinstance(projects, dict) and isinstance(sessions, (int, float)):
            if float(sessions) <= 0:
                return 0.0
            tsum = 0.0
            for p in projects.values():
                if isinstance(p, dict) and isinstance(p.get("turns"), (int, float)):
                    tsum += float(p["turns"])
            return round(tsum / float(sessions), 1)
        return None
    if metric == "ai_output_tokens":
        v = ai.get("output_tokens")
        return float(v) if isinstance(v, (int, float)) else None
    if metric.startswith("category_minutes:"):
        cat = metric.split(":", 1)[1].strip()
        # 未知カテゴリは未計測（None）。既知名でキー欠落のみ 0.0
        if known_categories is not None and cat not in known_categories:
            return None
        by_cat = stats.get("by_category") if isinstance(stats.get("by_category"), dict) else {}
        v = by_cat.get(cat)
        return float(v) if isinstance(v, (int, float)) else 0.0
    if metric.startswith("site_minutes:"):
        # 欠損=0.0 維持（サイトブロック成功の0分判定。category との非対称）
        site = metric.split(":", 1)[1].strip().lower()
        by_site = stats.get("by_site") if isinstance(stats.get("by_site"), dict) else {}
        # キーが小文字で保存されている想定。大小混在も拾う
        v = by_site.get(site)
        if v is None:
            for k, val in by_site.items():
                if str(k).lower() == site:
                    v = val
                    break
        return float(v) if isinstance(v, (int, float)) else 0.0
    if metric == "focus_blocks":
        if not inp:
            return None
        v = inp.get("focus_blocks")
        return float(v) if isinstance(v, (int, float)) else None
    if metric == "focus_minutes":
        if not inp:
            return None
        v = inp.get("focus_minutes")
        return float(v) if isinstance(v, (int, float)) else None
    if metric == "input_keypresses":
        if not inp:
            return None
        v = inp.get("keypresses")
        return float(v) if isinstance(v, (int, float)) else None
    return None


def baseline_median_from_stats(
    stats_list: list[dict], metric: str, min_days: int = 3
) -> float | None:
    """開始前の日次統計から中央値 baseline を求める。日数不足は None。"""
    values: list[float] = []
    for s in stats_list:
        v = metric_from_stats(metric, s)
        if v is not None:
            values.append(float(v))
    if len(values) < min_days:
        return None
    return float(median(values))


def weekday_baseline(
    metric: str, day: date, stats_list: list[dict]
) -> float | None:
    """同曜日の metric 中央値（呼び出し側が開始前28日分などを渡す）。

    day と同じ weekday のサンプルが 2 日未満なら None。
    """
    target_wd = day.weekday()
    values: list[float] = []
    for s in stats_list:
        raw = s.get("day")
        if not raw:
            continue
        try:
            d = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if d.weekday() != target_wd:
            continue
        v = metric_from_stats(metric, s)
        if v is not None:
            values.append(float(v))
    if len(values) < 2:
        return None
    return float(median(values))


def _effect_size_from_values(
    baseline: float | None, measurements: Sequence[float]
) -> float | None:
    """baseline と実測群から変化率(%)。baseline が None/0 または空なら None。"""
    if baseline is None or baseline == 0 or not math.isfinite(baseline):
        return None
    if not measurements or not all(math.isfinite(value) for value in measurements):
        return None
    med = float(median(list(measurements)))
    effect = (med - float(baseline)) / float(baseline) * 100.0
    return round(effect, 1) if math.isfinite(effect) else None


def effect_size(exp: Experiment) -> float | None:
    """実測中央値 vs baseline の変化率（%）。baseline が None/0 または実測なしは None。"""
    return _effect_size_from_values(exp.baseline, list(exp.measurements.values()))


def format_effect_size(exp: Experiment) -> str | None:
    """表示用 `効果量 -32%`。算出不能なら None。"""
    es = effect_size(exp)
    if es is None:
        return None
    # 符号付き（正は +）
    if es > 0:
        return f"効果量 +{es:g}%"
    return f"効果量 {es:g}%"


def should_measure_experiment(exp: Experiment, day: date) -> bool:
    """generate が実測を追記する対象か。running 全件 + adopted は期限後30日以内のみ。"""
    if exp.status == "running":
        return True
    if exp.status == "adopted" and exp.deadline is not None:
        return day <= exp.deadline + timedelta(days=_ADOPTED_MONITOR_DAYS)
    return False


def detect_regressions(
    experiments: list[Experiment],
    window: int = 7,
    as_of: date | None = None,
) -> list[Experiment]:
    """adopted 実験のうち、直近 window 日の実測が3点以上かつ過半数が未達なら退行。"""
    as_of = as_of or date.today()
    start = as_of - timedelta(days=window - 1)
    out: list[Experiment] = []
    for exp in experiments:
        if exp.status != "adopted":
            continue
        recent = [
            (d, v) for d, v in exp.measurements.items()
            if start <= d <= as_of
        ]
        if len(recent) < 3:
            continue
        misses = sum(
            1 for _, v in recent
            if not target_met(v, exp.target_op, exp.target_value)
        )
        # 過半数（ちょうど半分は退行としない）
        if misses * 2 > len(recent):
            out.append(exp)
    return out


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
    # マーカー区間があればそこだけを読む。ノート全体を対象にすると、
    # Notes欄などに手書きした日付始まりのテーブル行まで実測値として
    # 吸い込み、次回のupsertで自動テーブルに混入してしまう。
    section = extract_section(content, MEASUREMENTS_MARKER)
    target = section if section is not None else content
    out: dict[date, float] = {}
    for m in re.finditer(
        r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([\d.\-]+)\s*\|", target, re.MULTILINE
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
        except (OSError, UnicodeDecodeError):
            # 他エディタ由来の非UTF-8ノート等は読める形式ではないためスキップ
            # （クラッシュさせると夜間実行全体が止まる）
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
                if not math.isfinite(baseline):
                    baseline = None
            except ValueError:
                pass
        start = None
        if fields.get("date"):
            try:
                start = date.fromisoformat(fields["date"][:10])
            except ValueError:
                start = None
        cluster_rep = fields.get("cluster_rep") or None
        if cluster_rep is not None:
            cluster_rep = cluster_rep.strip() or None
        cluster_id = fields.get("cluster_id") or None
        if cluster_id is not None:
            cluster_id = cluster_id.strip() or None
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
                cluster_rep=cluster_rep,
                cluster_id=cluster_id,
                start=start,
            )
        )
    return experiments


def record_measurement(
    exp: Experiment,
    day: date,
    value: float,
    *,
    weekday_baselines: dict[date, float | None] | None = None,
) -> bool:
    """実測値を実験ノートのMeasurementsテーブルにupsertする。

    baselineが未設定なら最初の実測値で埋める。期限を過ぎた実験は
    status: expired に更新する。目標達成ならTrueを返す。

    テーブルは4列（日付|値|目標達成|同曜日基準）。旧3列ノートの読込は
    `_parse_measurements` が先頭2列だけ見るため互換。
    """
    content = exp.path.read_text(encoding="utf-8")

    exp.measurements[day] = value
    met = target_met(value, exp.target_op, exp.target_value)
    wb_map = weekday_baselines or {}

    rows = [
        "| 日付 | 値 | 目標達成 | 同曜日基準 |",
        "| --- | ---: | :-: | ---: |",
    ]
    for d in sorted(exp.measurements):
        v = exp.measurements[d]
        mark = "✅" if target_met(v, exp.target_op, exp.target_value) else "❌"
        v_str = f"{v:g}"
        wb = wb_map.get(d)
        wb_str = f"{wb:g}" if wb is not None else "-"
        rows.append(f"| {d.isoformat()} | {v_str} | {mark} | {wb_str} |")
    section = "## Measurements（自動計測）\n\n" + "\n".join(rows)
    content = upsert_section(content, MEASUREMENTS_MARKER, section)

    if exp.baseline is None:
        exp.baseline = value
        content = _set_frontmatter_field(content, "baseline", f"{value:g}")

    if exp.status == "running" and exp.deadline and day > exp.deadline:
        exp.status = "expired"
        content = _set_frontmatter_field(content, "status", "expired")

    atomic_write_text(exp.path, content)
    return met


EXPERIMENT_TEMPLATE = """\
---
title: "{title}"
date: {today}
tags: [type/kaizen-experiment]
status: running
metric: {metric}
target: "{target}"
baseline: {baseline}
deadline: {deadline}
---

# 🧪 {title}

## Hypothesis（仮説）

{hypothesis}

<!-- kaizenlog:measurements:start -->
## Measurements（自動計測）

`kaizenlog generate` が毎晩ここに実測値を追記します。
<!-- kaizenlog:measurements:end -->

## Notes

- 開始曜日・週内の仕事量変動が交絡し得る。同曜日基準列と効果量で判断する
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
    baseline: float | None = None,
) -> Path:
    parse_target(target)  # バリデーション
    experiments_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title).strip()
    path = experiments_dir / f"EXP {today.isoformat()} {safe_title}.md"
    if path.exists():
        raise ExperimentError(f"同名の実験が既に存在します: {path}")
    # 開始前 baseline を渡されたときだけ数値を書く（空欄は従来どおり後で埋める）
    baseline_field = f"{baseline:g}" if baseline is not None else ""
    atomic_write_text(
        path,
        EXPERIMENT_TEMPLATE.format(
            title=title,
            today=today.isoformat(),
            metric=metric,
            target=target,
            baseline=baseline_field,
            deadline=deadline.isoformat(),
            hypothesis=hypothesis or "（なぜこの変更が効くと考えるか）",
        ),
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
        es = format_effect_size(e)
        es_part = f"、{es}" if es else ""
        lines.append(
            f"- 「{e.title}」[{e.status}] 目標: {target}"
            f"（{baseline}直近: {points}{es_part}、期限: {e.deadline or '未設定'}）"
        )
    lines.append(
        "※ PC前景のみ計測。カテゴリ時間の減少はスマホ等への移行（風船効果）の可能性を排除できない。"
    )
    return "\n".join(lines)


def experiment_day_progress(
    start: date | None,
    deadline: date | None,
    today: date,
) -> tuple[int, int] | None:
    """n/N 日目を返す。start か deadline 欠落時は None。

    開始日当日 = 1/N、deadline 当日 = N/N、期限超過は N/N のまま。
    """
    if start is None or deadline is None:
        return None
    total = (deadline - start).days + 1
    if total < 1:
        total = 1
    n = (today - start).days + 1
    if n < 1:
        n = 1
    if n > total:
        n = total
    return n, total


def _fmt_metric_value(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def build_experiments_section(
    experiments: Sequence[Experiment],
    abtests: Sequence["AbtestExperiment"],
    *,
    today: date,
) -> str | None:
    """進行中実験の1行カルテ。running 0件なら None（読み取り専用）。"""
    lines_body: list[str] = []

    for exp in experiments:
        if exp.status != "running":
            continue
        progress = experiment_day_progress(exp.start, exp.deadline, today)
        if progress is None:
            continue
        n, total = progress
        today_val = exp.measurements.get(today)
        if today_val is None:
            today_part = "今日の値: 未測"
        else:
            today_part = f"今日の値: {_fmt_metric_value(today_val)}"
        if exp.baseline is None:
            base_part = "開始前基準線: 未記録"
        else:
            base_part = f"開始前基準線: {_fmt_metric_value(exp.baseline)}"
        target_part = (
            f"目標条件: {exp.metric} {exp.target_op} "
            f"{_fmt_metric_value(exp.target_value)}"
        )
        metric_part = f"（metric: {exp.metric}）"
        lines_body.append(
            f"- {exp.title} — {n}/{total}日目｜{today_part}{metric_part}"
            f"｜{base_part}｜{target_part}"
        )

    for ab in abtests:
        if ab.status != "running":
            continue
        progress = experiment_day_progress(ab.start, ab.deadline, today)
        if progress is None:
            continue
        n, total = progress
        ab_id = getattr(ab, "id", None) or "abtest"
        lines_body.append(
            f"- abtest #{ab_id} — {n}/{total}日目｜status: running"
            f"（AI利用日 {int(ab.sample_ai_days)}日"
            f" / 非利用日 {int(ab.sample_non_ai_days)}日）"
        )

    if not lines_body:
        return None
    header = f"## 🧪 進行中の実験（{len(lines_body)}件）"
    return header + "\n" + "\n".join(lines_body) + "\n"


# ---- abtest（パーソナル METR） ----

ABTEST_TYPE = "abtest"
ABTEST_METRIC = "category_minutes:開発"
_MIN_NON_AI_DAYS = 3
_PREDICT_RE = re.compile(r"^[+]?\s*(-?\d+(?:\.\d+)?)\s*%?$")


@dataclass
class AbtestExperiment:
    path: Path
    id: str
    status: str  # running | finished | invalid
    start: date
    deadline: date
    predict_pct: float
    felt_pct: float | None = None
    measured_pct: float | None = None
    invalid_reason: str | None = None
    sample_ai_days: int = 0
    sample_non_ai_days: int = 0
    card_path: str | None = None


def parse_predict_pct(raw: str) -> float:
    m = _PREDICT_RE.match((raw or "").strip())
    if not m:
        raise ExperimentError(f"予測値は +N または +N% 形式です: {raw!r}")
    return float(m.group(1))


def is_ai_day(stats: dict) -> bool:
    """stats v2 の api_calls > 0（internal 除外後の集計済み値）。"""
    ai = stats.get("ai") if isinstance(stats.get("ai"), dict) else {}
    try:
        return int(ai.get("api_calls") or 0) > 0
    except (TypeError, ValueError):
        return False


def create_abtest(
    experiments_dir: Path,
    *,
    today: date,
    predict_pct: float,
    days: int = 28,
) -> Path:
    experiments_dir = Path(experiments_dir)
    experiments_dir.mkdir(parents=True, exist_ok=True)
    abtest_id = f"ABT-{today.strftime('%Y%m%d')}"
    # 同日複数: 連番
    n = 1
    while (experiments_dir / f"ABTEST {abtest_id}-{n:02d}.md").exists():
        n += 1
    abtest_id = f"{abtest_id}-{n:02d}"
    deadline = today + timedelta(days=max(1, int(days)) - 1)
    path = experiments_dir / f"ABTEST {abtest_id}.md"
    content = (
        "---\n"
        f'title: "abtest {abtest_id}"\n'
        f"date: {today.isoformat()}\n"
        f"type: {ABTEST_TYPE}\n"
        "status: running\n"
        f"abtest_id: {abtest_id}\n"
        f"predict_pct: {predict_pct:g}\n"
        f"deadline: {deadline.isoformat()}\n"
        f"metric: {ABTEST_METRIC}\n"
        'target: ">= 0"\n'
        "---\n\n"
        f"# 📊 abtest {abtest_id}\n\n"
        f"予測: {predict_pct:+g}%（開発カテゴリ・AI利用日 vs 非利用日）\n"
        f"期間: {today.isoformat()} 〜 {deadline.isoformat()}\n\n"
        "<!-- kaizenlog:measurements:start -->\n"
        "## Measurements\n\n"
        "`kaizenlog abtest finish` で確定します。\n"
        "<!-- kaizenlog:measurements:end -->\n"
    )
    atomic_write_text(path, content)
    return path


def load_abtests(experiments_dir: Path) -> list[AbtestExperiment]:
    if not experiments_dir.is_dir():
        return []
    out: list[AbtestExperiment] = []
    for path in sorted(Path(experiments_dir).glob("ABTEST *.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fields, ok = _parse_frontmatter(content)
        if not ok or fields.get("type") != ABTEST_TYPE:
            continue
        try:
            start = date.fromisoformat(fields.get("date", "")[:10])
        except ValueError:
            continue
        try:
            deadline = date.fromisoformat(fields.get("deadline", "")[:10])
        except ValueError:
            deadline = start + timedelta(days=27)
        try:
            predict = float(fields.get("predict_pct", "0"))
        except ValueError:
            predict = 0.0
        felt = None
        if fields.get("felt_pct"):
            try:
                felt = float(fields["felt_pct"])
            except ValueError:
                felt = None
        measured = None
        if fields.get("measured_pct"):
            try:
                measured = float(fields["measured_pct"])
            except ValueError:
                measured = None
        try:
            ai_n = int(fields.get("sample_ai_days") or 0)
        except ValueError:
            ai_n = 0
        try:
            non_n = int(fields.get("sample_non_ai_days") or 0)
        except ValueError:
            non_n = 0
        out.append(
            AbtestExperiment(
                path=path,
                id=fields.get("abtest_id") or path.stem,
                status=fields.get("status", "running"),
                start=start,
                deadline=deadline,
                predict_pct=predict,
                felt_pct=felt,
                measured_pct=measured,
                invalid_reason=fields.get("invalid_reason") or None,
                sample_ai_days=ai_n,
                sample_non_ai_days=non_n,
                card_path=fields.get("card_path") or None,
            )
        )
    return out


def compute_abtest_effect(
    stats_list: list[dict],
    *,
    start: date,
    end: date,
    metric: str = ABTEST_METRIC,
    pre_stats: list[dict] | None = None,
) -> tuple[float | None, int, int, str | None]:
    """AI日/非AI日を分割し同曜日正規化の効果量(%)を返す。

    戻り値: (measured_pct | None, ai_days, non_ai_days, invalid_reason | None)

    正規化: value / weekday_baseline * 100 の無次元 index。
    baseline が None/0 の日が1日でもあれば混在を避けて不成立。
    """
    pre = list(pre_stats or [])
    ai_vals: list[float] = []
    non_vals: list[float] = []
    missing_baseline = 0
    for s in stats_list:
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
        wb = weekday_baseline(metric, d, pre) if pre else None
        if wb is None or wb == 0:
            missing_baseline += 1
            continue
        # 無次元 index: value / baseline * 100
        adj = float(v) / float(wb) * 100.0
        if is_ai_day(s):
            ai_vals.append(adj)
        else:
            non_vals.append(adj)
    if missing_baseline > 0:
        return (
            None,
            len(ai_vals),
            len(non_vals),
            f"実測不成立(同曜日baseline不足: {missing_baseline}日)",
        )
    ai_n, non_n = len(ai_vals), len(non_vals)
    if non_n < _MIN_NON_AI_DAYS:
        return None, ai_n, non_n, f"実測不成立(サンプル不足: {non_n}日)"
    if ai_n < 1:
        return None, ai_n, non_n, "実測不成立(サンプル不足: AI日0日)"
    # 非AI群中央値を baseline、AI群を measurements として effect_size 式を再利用
    non_med = float(median(non_vals))
    if non_med == 0:
        return None, ai_n, non_n, "実測不成立(非AI基準が0)"
    effect = _effect_size_from_values(non_med, ai_vals)
    return effect, ai_n, non_n, None


def _upsert_fm(content: str, key: str, value: str) -> str:
    if re.search(rf"(?m)^{re.escape(key)}\s*:", content):
        return _set_frontmatter_field(content, key, value)
    return _insert_frontmatter_field(content, key, value)


def finish_abtest(
    exp: AbtestExperiment,
    *,
    felt_pct: float,
    card_rel_path: str | None,
    measured_pct: float | None,
    invalid_reason: str | None,
    sample_ai: int,
    sample_non: int,
    as_of: date,
) -> AbtestExperiment:
    """frontmatter を更新して finished/invalid にする。"""
    content = exp.path.read_text(encoding="utf-8")
    status = "invalid" if invalid_reason else "finished"
    content = _upsert_fm(content, "status", status)
    content = _upsert_fm(content, "felt_pct", f"{felt_pct:g}")
    if measured_pct is not None:
        content = _upsert_fm(content, "measured_pct", f"{measured_pct:g}")
    if invalid_reason:
        content = _upsert_fm(content, "invalid_reason", invalid_reason)
    content = _upsert_fm(content, "sample_ai_days", str(sample_ai))
    content = _upsert_fm(content, "sample_non_ai_days", str(sample_non))
    content = _upsert_fm(content, "finished_on", as_of.isoformat())
    if card_rel_path:
        content = _upsert_fm(content, "card_path", card_rel_path)
    atomic_write_text(exp.path, content)
    exp.status = status
    exp.felt_pct = felt_pct
    exp.measured_pct = measured_pct
    exp.invalid_reason = invalid_reason
    exp.sample_ai_days = sample_ai
    exp.sample_non_ai_days = sample_non
    exp.card_path = card_rel_path
    return exp


def _insert_frontmatter_field(content: str, key: str, value: str) -> str:
    """frontmatter 終了直前にフィールドを挿入。"""
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return content
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            lines.insert(i, f"{key}: {value}\n")
            return "".join(lines)
    return content


def format_abtest_journal_line(exp: AbtestExperiment) -> str:
    """日誌 Kaizen セクション用1行。"""
    pred = f"{exp.predict_pct:+g}%"
    felt = f"{exp.felt_pct:+g}%" if exp.felt_pct is not None else "—"
    if exp.invalid_reason:
        # invalid_reason は「実測不成立(...)」形式なので接頭辞の重複を避ける
        meas = exp.invalid_reason.removeprefix("実測")
    elif exp.measured_pct is not None:
        meas = f"{exp.measured_pct:+g}%"
    else:
        meas = "—"
    card = f"（カード: {exp.card_path}）" if exp.card_path else ""
    return f"📊 abtest完了: 予測{pred} / 体感{felt} / 実測{meas}{card}"
