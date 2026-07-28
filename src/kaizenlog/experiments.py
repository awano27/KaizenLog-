"""Kaizen実験ループ: 改善提案を「検証可能な実験」として追跡する。

実験は `03 Areas/Kaizen Experiments/` 配下のノート（frontmatter付きMarkdown）。
毎晩の `kaizenlog generate` が対象日の実測値を計算してMeasurementsテーブルに
自動追記し、期限が来た実験は status: expired にして週次レビューの判定に回す。
"""

from __future__ import annotations

import math
import re
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
    "total_active_minutes": "合計アクティブ時間（分）",
    "ai_activity_blocks": "AIツールの画面アクティビティブロック数",
    "ai_sessions": "AIツールの画面アクティビティブロック数（旧名・互換用）",
    "ai_cc_sessions": "AI CLIセッション数・Claude Code/Codex合算",
    "ai_fragmented_sessions": "2往復以下の細切れAI CLIセッション数",
    "ai_retry_chains": "リトライ連鎖数（30分以内のほぼ同文の再依頼）",
    "ai_tool_errors": "AI CLI合算のツールエラー回数",
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


def effect_size(exp: Experiment) -> float | None:
    """実測中央値 vs baseline の変化率（%）。baseline が None/0 または実測なしは None。"""
    if exp.baseline is None or exp.baseline == 0:
        return None
    if not exp.measurements:
        return None
    med = float(median(exp.measurements.values()))
    return round((med - float(exp.baseline)) / float(exp.baseline) * 100.0, 1)


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
    path.write_text(
        EXPERIMENT_TEMPLATE.format(
            title=title,
            today=today.isoformat(),
            metric=metric,
            target=target,
            baseline=baseline_field,
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
