"""日次改善提案へ渡す、機械可読統計由来の根拠と測定限界。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from math import isfinite
from statistics import median
import re
from typing import Any

from .collector import _is_browser_app

_MIN_BASELINE_DAYS = 3
_MIN_BASELINE_ACTIVE_MINUTES = 60.0
# short_record / 計測欠測検知の共通閾値（aiwork へは呼び出し側から渡す）
SHORT_RECORD_MIN_MINUTES = 120.0
_SCREEN_TOOL_WEB_SOURCES = {
    "chatgpt": ("chatgpt-web",),
    "claude": ("claude-web",),
    "gemini": ("gemini-web",),
}


@dataclass(frozen=True)
class AdviceEvidence:
    """プロンプト本文と、検証に使う測定可否を同じ生成結果として保持する。"""

    markdown: str
    fact_ids: frozenset[str]
    ai_conversation_metrics_available: bool
    entertainment_observed: bool
    reader_summary: str
    reader_notes: tuple[str, ...]
    max_actions: int
    previous_day_available: bool
    browser_sample_sufficient: bool
    # None = 入口ガード未適用（後方互換）。非 None なら集合外は契約違反
    known_categories: frozenset[str] | None = None
    # 提案日当日に観測されたサイト。None = ガード未適用
    observed_sites: frozenset[str] | None = None
    afk_watcher_available: bool = True
    # PASS 入口: 環境で計測可能な指標だけ許可（永久未判定の偽保存を防ぐ）
    input_metrics_available: bool = False  # focus_blocks / focus_minutes / input_keypresses
    structured_ai_metrics_available: bool = False  # ai_cc_sessions 等（画面ブロック ai_activity は別）
    site_metrics_available: bool = False  # by_site 統計が有効（web watcher 経路）
    # 指標 → 挑戦性検査用ベースライン（直近履歴の中央値。無い指標は入口で検査しない）
    metric_baselines: Mapping[str, float] | None = None
    # 当日 total_minutes（生カウントPASS入口ガード用。欠落は None）
    total_minutes: float | None = None


def _evidence(
    lines: list[str],
    *,
    ai_conversation_metrics_available: bool = False,
    entertainment_observed: bool = False,
    reader_summary: str = "当日の確定統計がないため、作業状況を評価できません。",
    reader_notes: tuple[str, ...] = ("先にActivity Logを生成してください。",),
    max_actions: int = 1,
    previous_day_available: bool = False,
    browser_sample_sufficient: bool = False,
    known_categories: frozenset[str] | None = None,
    observed_sites: frozenset[str] | None = None,
    afk_watcher_available: bool = True,
    input_metrics_available: bool = False,
    structured_ai_metrics_available: bool = False,
    site_metrics_available: bool = False,
    metric_baselines: Mapping[str, float] | None = None,
    total_minutes: float | None = None,
) -> AdviceEvidence:
    markdown = "\n".join(lines)
    return AdviceEvidence(
        markdown=markdown,
        fact_ids=frozenset(re.findall(r"\[F\d+\]", markdown)),
        ai_conversation_metrics_available=ai_conversation_metrics_available,
        entertainment_observed=entertainment_observed,
        reader_summary=reader_summary,
        reader_notes=reader_notes,
        max_actions=max_actions,
        previous_day_available=previous_day_available,
        browser_sample_sufficient=browser_sample_sufficient,
        known_categories=known_categories,
        observed_sites=observed_sites,
        afk_watcher_available=afk_watcher_available,
        input_metrics_available=input_metrics_available,
        structured_ai_metrics_available=structured_ai_metrics_available,
        site_metrics_available=site_metrics_available,
        metric_baselines=metric_baselines,
        total_minutes=total_minutes,
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number if isfinite(number) else 0.0
    return 0.0


def _valid_nonnegative_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and isfinite(float(value))
        and float(value) >= 0
    )


def _valid_number_mapping(value: object) -> bool:
    return isinstance(value, Mapping) and all(
        _valid_nonnegative_number(item) for item in value.values()
    )


def _valid_count_fields(value: object, fields: tuple[str, ...]) -> bool:
    return isinstance(value, Mapping) and all(
        field in value and _valid_nonnegative_number(value[field])
        for field in fields
    )


def _fmt(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _fmt_duration_ja(minutes: float) -> str:
    """Activity Log と同型の「N時間M分」。結論用。"""
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}時間{m}分" if h else f"{m}分"


def _known_browser_minutes(by_app: Mapping[str, Any]) -> float:
    return sum(
        _number(minutes)
        for app, minutes in by_app.items()
        if _is_browser_app(str(app))
    )


def _switch_rate(stats: Mapping[str, Any]) -> float | None:
    if not (
        _valid_nonnegative_number(stats.get("total_minutes"))
        and _valid_nonnegative_number(stats.get("context_switches"))
    ):
        return None
    total = float(stats["total_minutes"])
    if total <= 0:
        return None
    return float(stats["context_switches"]) / total * 60


def _parse_hour(value: object, timezone: tzinfo | None) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone).hour if timezone and parsed.tzinfo else parsed.hour
    except ValueError:
        return None


def _transition_fact(blocks: list[object], timezone: tzinfo | None) -> str:
    transitions: Counter[tuple[str, str]] = Counter()
    hours: Counter[int] = Counter()
    previous: str | None = None
    for value in blocks:
        block = _mapping(value)
        category = block.get("category")
        if not isinstance(category, str) or not category:
            continue
        if previous is not None and category != previous:
            transitions[(previous, category)] += 1
            hour = _parse_hour(block.get("start"), timezone)
            if hour is not None:
                hours[hour] += 1
        previous = category

    if not transitions:
        return "- [F9] activity block列からカテゴリ遷移パターンを算出できない。"

    pairs = "、".join(
        f"{source}→{target} {count}回"
        for (source, target), count in transitions.most_common(3)
    )
    peaks = "、".join(f"{hour:02d}時台 {count}回" for hour, count in hours.most_common(3))
    suffix = f"。遷移が多い時間帯: {peaks}" if peaks else ""
    return f"- [F9] 上位カテゴリ遷移: {pairs}{suffix}。"


def _previous_day_available(
    stats: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]] | None,
) -> bool:
    try:
        previous_day = date.fromisoformat(str(stats.get("day"))) - timedelta(days=1)
    except ValueError:
        return False
    return any(
        item.get("day") == previous_day.isoformat()
        and _valid_nonnegative_number(item.get("total_minutes"))
        and float(item["total_minutes"]) >= _MIN_BASELINE_ACTIVE_MINUTES
        for item in (history or [])
        if isinstance(item, Mapping)
    )


def _previous_day_stats(
    stats: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]] | None,
) -> Mapping[str, Any] | None:
    try:
        previous_day = date.fromisoformat(str(stats.get("day"))) - timedelta(days=1)
    except ValueError:
        return None
    key = previous_day.isoformat()
    for item in history or []:
        if isinstance(item, Mapping) and item.get("day") == key:
            return item
    return None


_BASELINE_SIMPLE_METRICS = (
    "context_switches",
    "context_switches_per_hour",
    "total_active_minutes",
    "ai_activity_blocks",
    "ai_cc_sessions",
    "ai_fragmented_sessions",
    "ai_retry_chains",
    "ai_tool_errors",
    "ai_tool_errors_per_session",
    "ai_interruptions",
    "ai_avg_turns",
    "ai_output_tokens",
    "focus_blocks",
    "focus_minutes",
    "input_keypresses",
)


def _strict_simple_metric_source(metric: str, stats: Mapping[str, Any]) -> bool:
    """``metric_from_stats`` が読む生値が厳密な非負有限数かを確認する。"""
    if metric == "context_switches":
        return _valid_nonnegative_number(stats.get("context_switches"))
    if metric == "context_switches_per_hour":
        # 分子・分母とも必要。分母下限は metric_from_stats 側で None になる。
        return (
            _valid_nonnegative_number(stats.get("context_switches"))
            and _valid_nonnegative_number(stats.get("total_minutes"))
        )
    if metric == "total_active_minutes":
        return _valid_nonnegative_number(stats.get("total_minutes"))
    if metric == "ai_activity_blocks":
        value = (
            stats.get("ai_activity_blocks")
            if "ai_activity_blocks" in stats
            else stats.get("ai_sessions")
        )
        return _valid_nonnegative_number(value)

    input_stats = stats.get("input")
    input_keys = {
        "focus_blocks": "focus_blocks",
        "focus_minutes": "focus_minutes",
        "input_keypresses": "keypresses",
    }
    if metric in input_keys:
        return (
            isinstance(input_stats, Mapping)
            and _valid_nonnegative_number(input_stats.get(input_keys[metric]))
        )

    ai = stats.get("ai")
    if not isinstance(ai, Mapping):
        return False
    ai_keys = {
        "ai_cc_sessions": "sessions",
        "ai_fragmented_sessions": "fragmented",
        "ai_retry_chains": "retry_chains",
        "ai_tool_errors": "tool_errors",
        "ai_interruptions": "interruptions",
        "ai_output_tokens": "output_tokens",
    }
    if metric in ai_keys:
        return _valid_nonnegative_number(ai.get(ai_keys[metric]))
    if metric == "ai_tool_errors_per_session":
        return (
            _valid_nonnegative_number(ai.get("tool_errors"))
            and _valid_nonnegative_number(ai.get("sessions"))
            and float(ai.get("sessions")) > 0
        )
    if metric == "ai_avg_turns":
        if "avg_turns" in ai:
            return _valid_nonnegative_number(ai.get("avg_turns"))
        sessions = ai.get("sessions")
        if not _valid_nonnegative_number(sessions):
            return False
        if "turns_total" in ai:
            return _valid_nonnegative_number(ai.get("turns_total"))
        projects = ai.get("projects")
        return isinstance(projects, Mapping) and bool(projects) and all(
            isinstance(project, Mapping)
            and _valid_nonnegative_number(project.get("turns"))
            for project in projects.values()
        )

    return False


def _unique_prior_history(
    history: Sequence[Mapping[str, Any]] | None, current_day: date
) -> list[tuple[date, Mapping[str, Any]]]:
    """同日重複を丸ごと除外した、日付昇順の過去統計を返す。"""
    by_day: dict[date, Mapping[str, Any]] = {}
    duplicate_days: set[date] = set()
    for item in history or ():
        if not isinstance(item, Mapping):
            continue
        try:
            item_day = date.fromisoformat(str(item.get("day")))
        except (TypeError, ValueError):
            continue
        if item_day >= current_day:
            continue
        if item_day in by_day:
            duplicate_days.add(item_day)
            continue
        by_day[item_day] = item
    return [
        (item_day, item)
        for item_day, item in sorted(by_day.items())
        if item_day not in duplicate_days
    ]


def _metric_baselines_from_history(
    stats: Mapping[str, Any], history: Sequence[Mapping[str, Any]] | None
) -> dict[str, float]:
    """有効な直近履歴3日以上の中央値だけをPASS検査用に返す。"""
    from .experiments import metric_from_stats

    try:
        current_day = date.fromisoformat(str(stats.get("day")))
    except (TypeError, ValueError):
        return {}

    valid_history = _unique_prior_history(history, current_day)

    category_metrics: set[str] = set()
    site_metrics: set[str] = set()
    for _, item in valid_history:
        by_category = item.get("by_category")
        if isinstance(by_category, Mapping):
            category_metrics.update(f"category_minutes:{name}" for name in by_category)
        by_site = item.get("by_site")
        if isinstance(by_site, Mapping):
            site_metrics.update(f"site_minutes:{str(name).lower()}" for name in by_site)
    metrics = [
        *_BASELINE_SIMPLE_METRICS,
        *sorted(category_metrics),
        *sorted(site_metrics),
    ]

    out: dict[str, float] = {}
    for metric in metrics:
        values_by_day: dict[date, float] = {}
        for item_day, item in valid_history:
            if metric.startswith("category_minutes:"):
                mapping = item.get("by_category")
                key = metric.split(":", 1)[1]
                if not isinstance(mapping, Mapping) or key not in mapping:
                    continue
                raw_value = mapping[key]
            elif metric.startswith("site_minutes:"):
                mapping = item.get("by_site")
                key = metric.split(":", 1)[1]
                if not isinstance(mapping, Mapping):
                    continue
                raw_value = next(
                    (value for name, value in mapping.items() if str(name).lower() == key),
                    None,
                )
            else:
                if not _strict_simple_metric_source(metric, item):
                    continue
                raw_value = None
            if metric.startswith(("category_minutes:", "site_minutes:")) and not _valid_nonnegative_number(raw_value):
                continue
            value = metric_from_stats(metric, dict(item))
            if not _valid_nonnegative_number(value):
                continue
            values_by_day[item_day] = float(value)
        if len(values_by_day) >= _MIN_BASELINE_DAYS:
            out[metric] = float(median(values_by_day.values()))
    return out


def _tool_error_concentration_sentence(stats: Mapping[str, Any]) -> str | None:
    """ツールエラーの過半が単一プロジェクトに集中していれば1文。"""
    ai = stats.get("ai")
    if not isinstance(ai, Mapping):
        return None
    total = ai.get("tool_errors")
    if not _valid_nonnegative_number(total) or float(total) <= 0:
        return None
    total_f = float(total)
    projects = ai.get("projects")
    if not isinstance(projects, Mapping) or not projects:
        return None
    best_name = ""
    best_err = -1.0
    for name, bucket in projects.items():
        if not isinstance(bucket, Mapping):
            continue
        err = bucket.get("errors")
        if not _valid_nonnegative_number(err):
            return None
        if float(err) > best_err:
            best_err = float(err)
            best_name = str(name)
    if best_err <= 0 or best_err <= total_f * 0.5:
        return None
    # プロジェクト名は表セルと同様に一行化（改行で結論を壊さない）
    safe = " ".join(best_name.split())
    percentage = best_err / total_f * 100
    return (
        f"ツールエラー{int(total_f)}回中{int(best_err)}回（{percentage:.0f}%）が"
        f"『{safe}』に集中しています。"
    )


def _goal_category_minutes(
    stats: Mapping[str, Any], category: str
) -> float | None:
    by_cat = stats.get("by_category")
    if not isinstance(by_cat, Mapping):
        return None
    v = by_cat.get(category)
    if isinstance(v, (int, float)) and isfinite(float(v)):
        return float(v)
    return None


def _count_goal_days(
    history: Sequence[Mapping[str, Any]] | None,
    current: Mapping[str, Any] | None,
    *,
    window: int = 7,
) -> tuple[int, int]:
    """直近 window 日（当日含む）の目標記入日数。戻り値 (記入日数, 窓日数)。"""
    days: list[Mapping[str, Any]] = []
    if history:
        days.extend(list(history)[-window:])
    if current is not None:
        # history に当日が含まれる実装もあるため day で重複除去
        cur_day = current.get("day")
        days = [d for d in days if d.get("day") != cur_day]
        days.append(current)
    days = days[-window:]
    n_window = len(days) if days else window
    n_goal = sum(1 for d in days if isinstance(d.get("goal_text"), str) and d.get("goal_text").strip())
    return n_goal, max(n_window, 1)


def _reader_history_with_current(
    stats: Mapping[str, Any], history: Sequence[Mapping[str, Any]] | None
) -> list[tuple[date, Mapping[str, Any]]]:
    """現在日より前の有効な履歴と、末尾の現在統計を日付順にする。"""
    try:
        current_day = date.fromisoformat(str(stats.get("day")))
    except ValueError:
        return []
    return [*_unique_prior_history(history, current_day), (current_day, stats)]


def _ai_work_trend_sentence(
    stats: Mapping[str, Any], history: Sequence[Mapping[str, Any]] | None
) -> str | None:
    records = _reader_history_with_current(stats, history)
    ai_minutes: list[float] = []
    for _, item in records:
        by_category = item.get("by_category")
        if not isinstance(by_category, Mapping):
            return None
        value = by_category.get("AI作業")
        if not _valid_nonnegative_number(value):
            return None
        ai_minutes.append(_number(value))
    if any(later <= earlier for earlier, later in zip(ai_minutes, ai_minutes[1:])):
        return None
    increase_count = sum(
        later > earlier for earlier, later in zip(ai_minutes, ai_minutes[1:])
    )
    if increase_count < 3:
        return None
    current = ai_minutes[-1]
    is_adjacent = all(
        later_day == earlier_day + timedelta(days=1)
        for (earlier_day, _), (later_day, _) in zip(records, records[1:])
    )
    qualifier = (
        f"{increase_count}日連続で増加"
        if is_adjacent
        else f"記録のある{len(records)}日で単調増加"
    )
    return f"AI作業は{qualifier}し、本日 {_fmt_duration_ja(current)}が記録されています。"


def _recorded_day_total_is_maximum(
    stats: Mapping[str, Any], history: Sequence[Mapping[str, Any]] | None
) -> int | None:
    records = _reader_history_with_current(stats, history)
    if len(records) < 3:
        return None
    totals: list[float] = []
    for _, item in records:
        value = item.get("total_minutes")
        if not _valid_nonnegative_number(value):
            return None
        totals.append(_number(value))
    return len(records) if totals[-1] == max(totals) else None


def _build_reader_summary(
    *,
    total_minutes: float,
    short_record: bool,
    stats: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]] | None,
    by_category: Mapping[str, Any],
    category_stats_valid: bool,
    entertainment_minutes: float | None,
    previous_day_available: bool,
    measurement_gap: tuple[int, int] | None = None,
) -> str:
    """「今日の結論」用の決定論文（最大3文）。数字は evidence 確定事実のみ。"""
    total_hm = _fmt_duration_ja(total_minutes)
    if short_record:
        # §B2: 画面計測が薄いが AI CLI ログがある日は欠測疑いへ接続
        if measurement_gap is not None:
            n_sess, _err = measurement_gap
            return (
                f"本日の画面記録は合計{total_hm}のため評価できません。"
                f"AI CLI ログには{n_sess}セッションの記録があり、"
                "画面側の計測欠測が疑われます。"
            )
        return (
            f"本日の記録は合計{total_hm}のため、"
            "1日の働き方を評価するにはデータ不足です。"
        )
    ai_trend = _ai_work_trend_sentence(stats, history)
    parts: list[str] = [ai_trend or f"本日は合計{total_hm}の作業が記録されています。"]
    # 目標カテゴリ実測（断定なし・goal_category がある日のみ）
    goal_cat = stats.get("goal_category")
    if isinstance(goal_cat, str) and goal_cat.strip() and len(parts) < 3:
        mins = _goal_category_minutes(stats, goal_cat.strip())
        if mins is not None:
            parts.append(
                f"目標カテゴリ『{goal_cat.strip()}』は{_fmt(mins)}分が記録されています。"
            )
    # 優先1: AI摩擦の集中
    friction = _tool_error_concentration_sentence(stats)
    if friction and len(parts) < 3:
        parts.append(friction)
    # 優先2: カテゴリ特徴（断定禁止・記録調）
    if len(parts) < 3 and category_stats_valid and by_category:
        top = max(
            ((str(k), float(v)) for k, v in by_category.items() if isinstance(v, (int, float))),
            key=lambda x: x[1],
            default=None,
        )
        if top and top[1] > 0:
            maximum_days = _recorded_day_total_is_maximum(stats, history)
            if maximum_days is not None:
                parts.append(
                    f"合計 {total_hm} は記録のある直近{maximum_days}日で最長です。"
                )
            else:
                parts.append(
                    f"カテゴリ別では「{top[0]}」が最多（{_fmt(top[1])}分）でした。"
                )
        if (
            len(parts) < 3
            and entertainment_minutes is not None
            and total_minutes > 0
            and entertainment_minutes > total_minutes * 0.2
        ):
            parts.append(
                f"エンタメカテゴリが{_fmt(entertainment_minutes)}分"
                f"（合計の{_fmt(entertainment_minutes / total_minutes * 100)}%）"
                "記録されています。"
            )
    # 優先3: 前日比（合計 or AIセッションのどちらか1つ）
    if len(parts) < 3 and previous_day_available:
        prev = _previous_day_stats(stats, history)
        if prev is not None:
            prev_total = prev.get("total_minutes")
            if (
                isinstance(prev_total, (int, float))
                and isfinite(float(prev_total))
            ):
                delta = total_minutes - float(prev_total)
                if abs(delta) >= 1.0:
                    direction = "増加" if delta > 0 else "減少"
                    parts.append(
                        f"合計時間は前日比{_fmt(abs(delta))}分{direction}が"
                        "記録されています。"
                    )
                else:
                    ai = stats.get("ai") if isinstance(stats.get("ai"), Mapping) else {}
                    prev_ai = prev.get("ai") if isinstance(prev.get("ai"), Mapping) else {}
                    cur_s = ai.get("sessions") if isinstance(ai, Mapping) else None
                    prev_s = (
                        prev_ai.get("sessions") if isinstance(prev_ai, Mapping) else None
                    )
                    if (
                        isinstance(cur_s, (int, float))
                        and isinstance(prev_s, (int, float))
                        and int(cur_s) != int(prev_s)
                    ):
                        d = int(cur_s) - int(prev_s)
                        direction = "増加" if d > 0 else "減少"
                        parts.append(
                            f"AI CLIセッションは前日比{abs(d)}回{direction}が"
                            "記録されています。"
                        )
    return " ".join(parts[:3])


def build_advice_evidence(
    stats: Mapping[str, Any] | None,
    history: Sequence[Mapping[str, Any]] | None = None,
    *,
    timezone: tzinfo | None = None,
    source_status: str = "unverified",
    known_categories: Sequence[str] | frozenset[str] | None = None,
    action_stats: Any | None = None,
    decay_events: Sequence[Any] | None = None,
    coach_entries: Sequence[Any] | None = None,
    lifecycle_notes: Sequence[str] | None = None,
    screenpipe_lines: Sequence[str] | None = None,
) -> AdviceEvidence:
    """LLMがログの意味を取り違えないための根拠コンテキストを作る。

    Markdownの日誌は人間向けに丸め・省略されるため、改善提案では統計JSONを
    優先する。Fは確定事実、Lは測定限界を示す安定した参照IDである。
    """
    lines = [
        "# 分析用の確定事実と測定限界（ログ本文より優先）",
        "",
        "## 測定限界",
        "- [L1] activity blockは同一カテゴリ・アプリの前景画面イベントをまとめた区間であり、"
        "会話セッション、メッセージ、AIとの往復回数ではない。",
        "- [L2] コンテキストスイッチは連続するactivity block間のカテゴリ変更回数であり、"
        "通知・割り込みの発生や生産性低下を直接証明しない。",
        "- [L3] 「ブラウジング」はブラウザ上の作業時間であり、私用・娯楽を意味しない。"
        "エンタメ扱いにはエンタメカテゴリ等の直接根拠が必要。",
        "- [L4] 日誌のタイムラインは短いブロックを省略し、件数上限を超えると長いブロックだけを"
        "表示する。表示行だけで一日の時間帯傾向を断定しない。",
        "- [L5] サイト別時間はブラウザwatcherが取得できた範囲だけで、統計では0.1分単位に"
        "丸められる。既知ブラウザの前景時間に対するカバレッジを必ず併記する。",
        "- [L6] AIの発話数・往復数・エラー・中断を判断できるのは、明示されたAIテレメトリ"
        "（Claude Code / Codex CLI 等）が存在する場合だけ。画面滞在時間から推定しない。",
        "- [L7] 過去中央値は60分以上記録された日が3日以上ある場合だけ示す。"
        "通常範囲からの差であり、良し悪しや因果を証明しない。",
        (
            "- [L8] 当日統計とActivity Logの指紋は一致済み。"
            if source_status == "verified"
            else "- [L8] 当日統計は旧形式でActivity Logとの同一生成結果か未検証。矛盾時は断定しない。"
            if source_status == "unverified"
            else "- [L8] 当日統計とActivity Logの指紋が不一致。統計値を確定事実として使わない。"
            if source_status == "mismatch"
            else "- [L8] 当日統計がなく、Activity Logとの整合性を検証できない。"
        ),
        "- [L13] 測定対象はこのPCの前景アクティビティのみ。"
        "スマホ・他デバイス・離席中の行動は含まれない。"
        "数値の減少はデバイス移行の可能性を排除できない。",
        "",
        "## 確定事実",
    ]

    invalid_stats = bool(stats) and not (
        _valid_nonnegative_number(stats.get("total_minutes"))
        and _valid_nonnegative_number(stats.get("context_switches"))
    )
    cats = (
        frozenset(str(c) for c in known_categories)
        if known_categories is not None
        else None
    )
    if not stats or invalid_stats:
        reason = (
            "当日統計とActivity Logの指紋が不一致"
            if source_status == "mismatch"
            else "当日統計が不正または不完全"
            if invalid_stats
            else "機械可読な当日統計が無い"
        )
        lines.extend([
            f"- [F0] {reason}ため、ログ本文に明記された値はこのIDで引用し、それ以外は未測定。",
            "- [F4] AI関連画面アクティビティの機械可読統計なし。時間・ブロック数は測定不能。",
            "- [F5] 構造化AIテレメトリの機械可読統計なし。発話数・往復数・品質は測定不能。",
        ])
        for de in (decay_events or []):
            detail = getattr(de, "detail", None) or ""
            if detail:
                lines.append(f"- [F17] 風化: {detail}")
        try:
            from .coachledger import format_f18_lines

            lines.extend(format_f18_lines(list(coach_entries or [])))
        except Exception:
            pass
        return _evidence(lines, known_categories=cats)

    blocks_value = stats.get("blocks")
    blocks_valid = isinstance(blocks_value, list)
    blocks = blocks_value if blocks_valid else []
    total_minutes = _number(stats.get("total_minutes"))
    context_switches = int(_number(stats.get("context_switches")))
    switch_rate = _switch_rate(stats)
    rate_text = f" / 1時間あたり {_fmt(switch_rate)}回" if switch_rate is not None else ""
    block_count_text = f"{len(blocks)}件" if blocks_valid else "測定不能"
    # 旧 stats はキー欠損 → true（従来どおり正常値扱い）
    afk_ok = stats.get("afk_watcher_available")
    afk_watcher_available = True if afk_ok is None else bool(afk_ok)
    f1_note = (
        ""
        if afk_watcher_available
        else "（AFK未計測のため離席時間を含む可能性）"
    )
    lines.append(
        f"- [F1] 合計アクティブ時間 {_fmt(total_minutes)}分{f1_note} / "
        f"activity block {block_count_text} / カテゴリ変更 {context_switches}回{rate_text}。"
    )

    # 今日の目標（stats に redact 済みで保存されたもののみ。達成断定はしない）
    goal_text = stats.get("goal_text")
    if isinstance(goal_text, str) and goal_text.strip():
        # F14〜F16: F11 は依頼長さ層別が既に使用しているため衝突しない番号を使う
        lines.append(f"- [F14] 今日の目標: {goal_text.strip()}")
        goal_cat = stats.get("goal_category")
        if isinstance(goal_cat, str) and goal_cat.strip():
            gm = _goal_category_minutes(stats, goal_cat.strip())
            if gm is not None:
                lines.append(
                    f"- [F15] 目標カテゴリ『{goal_cat.strip()}』の実測: {_fmt(gm)}分"
                )
        raw_ach = stats.get("goal_achieved")
        if isinstance(raw_ach, (int, float)):
            n = max(0, min(100, int(raw_ach)))
            lines.append(f"- [F14b] 目標達成度（自己申告）: {n}%")
        n_goal, n_win = _count_goal_days(history, stats, window=7)
        lines.append(f"- [F16] 目標記入: {n_win}日中{n_goal}日")

    by_category_value = stats.get("by_category")
    category_stats_valid = _valid_number_mapping(by_category_value)
    by_category = _mapping(by_category_value) if category_stats_valid else {}
    category_rows = sorted(
        ((str(name), _number(minutes)) for name, minutes in by_category.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    if category_rows:
        rendered = "、".join(f"{name} {_fmt(minutes)}分" for name, minutes in category_rows)
        lines.append(f"- [F2] カテゴリ別実測: {rendered}。")
    else:
        lines.append("- [F2] カテゴリ別統計なし。時間配分は測定不能。")

    input_value = stats.get("input")
    input_fields = ("focus_blocks", "focus_minutes", "active_input_minutes")
    input_stats = _mapping(input_value)
    focus_blocks: int | None = None
    focus_minutes: float | None = None
    if _valid_count_fields(input_value, input_fields):
        focus_blocks = int(_number(input_stats.get("focus_blocks")))
        focus_minutes = _number(input_stats.get("focus_minutes"))
        active_input = _number(input_stats.get("active_input_minutes"))
        lines.append(
            f"- [F3] 入力watcher実測: ログ定義の集中ブロック {focus_blocks}回 / "
            f"合計 {_fmt(focus_minutes)}分 / 入力あり {_fmt(active_input)}分。"
        )
    else:
        lines.append("- [F3] 入力watcher統計なし。集中ブロック数・時間は測定不能。")

    ai_blocks_value = stats.get("ai_activity_blocks")
    ai_activity_blocks: int | None
    if _valid_nonnegative_number(ai_blocks_value):
        ai_activity_blocks = int(float(ai_blocks_value))
    elif "ai_activity_blocks" not in stats and blocks_valid:
        ai_activity_blocks = sum(
            1 for block in blocks
            if _mapping(block).get("category") == "AI作業"
        )
    else:
        ai_activity_blocks = None
    ai_minutes = _number(by_category.get("AI作業")) if category_stats_valid else None
    ai_minutes_text = f"{_fmt(ai_minutes)}分" if ai_minutes is not None else "時間は測定不能"
    ai_blocks_text = (
        f"{ai_activity_blocks}ブロック"
        if ai_activity_blocks is not None else "ブロック数は測定不能"
    )
    lines.append(
        f"- [F4] AI関連画面アクティビティ（分類推定）: {ai_minutes_text} / "
        f"{ai_blocks_text}。画面ブロックはAI会話セッション数・往復数ではない。"
    )

    ai_value = stats.get("ai")
    ai_telemetry = _mapping(ai_value)
    ai_fields = ("sessions", "fragmented", "tool_errors", "interruptions")
    ai_stats_valid = _valid_count_fields(ai_value, ai_fields)
    telemetry_sessions = int(_number(ai_telemetry.get("sessions"))) if ai_stats_valid else 0
    fragmented = int(_number(ai_telemetry.get("fragmented"))) if ai_stats_valid else 0
    tool_errors = int(_number(ai_telemetry.get("tool_errors"))) if ai_stats_valid else 0
    interruptions = int(_number(ai_telemetry.get("interruptions"))) if ai_stats_valid else 0
    if ai_stats_valid and telemetry_sessions:
        # retry_chains は新フィールド。旧 stats では欠落 → 文言に含めない
        retry_raw = ai_telemetry.get("retry_chains")
        retry_part = ""
        if isinstance(retry_raw, (int, float)) and not isinstance(retry_raw, bool):
            retry_part = f" / リトライ連鎖 {int(retry_raw)}回"
        # ソース内訳（ai.sources）があれば付記
        sources = ai_telemetry.get("sources")
        source_part = ""
        if isinstance(sources, dict) and sources:
            bits = []
            for name, bucket in sorted(sources.items()):
                if isinstance(bucket, dict):
                    n = bucket.get("sessions")
                    if isinstance(n, (int, float)):
                        bits.append(f"{name} {int(n)}回")
            if bits:
                source_part = f"（内訳: {' / '.join(bits)}）"
        cost_part = ""
        cost_raw = ai_telemetry.get("est_cost_usd")
        if isinstance(cost_raw, (int, float)) and not isinstance(cost_raw, bool):
            uncosted = ai_telemetry.get("uncosted_tokens")
            out_tok = ai_telemetry.get("output_tokens")
            uncosted_n = (
                int(uncosted)
                if isinstance(uncosted, (int, float)) and not isinstance(uncosted, bool)
                else 0
            )
            out_n = (
                int(out_tok)
                if isinstance(out_tok, (int, float)) and not isinstance(out_tok, bool)
                else 0
            )
            costed_n = max(0, out_n - uncosted_n)
            # 対象外が計上分を上回る日は額を出さない（無意味な $ 表示を避ける）
            if out_n > 0 and uncosted_n > costed_n:
                cost_part = (
                    f" / 出力トークン {out_n:,}"
                    "（モデル単価不明分が大半のためコスト換算なし）"
                )
            else:
                u_part = f"・対象外 {uncosted_n:,} tok" if uncosted_n else ""
                cost_part = f" / 推定コスト ${float(cost_raw):.2f}（output のみ{u_part}）"
        lines.append(
            f"- [F5] 構造化AIテレメトリ: セッション {telemetry_sessions}回{source_part} / "
            f"2往復以下 {fragmented}回 / ツールエラー {tool_errors}回 / "
            f"中断・拒否 {interruptions}回{retry_part}{cost_part}。"
        )
    elif ai_stats_valid:
        lines.append(
            "- [F5] 統計に記録された構造化AIテレメトリは0件（利用ゼロとは限らない）。"
            "明示されたAIテレメトリ（Claude Code / Codex CLI 等）以外の"
            "会話の発話数・往復数は判断不能。"
        )
    else:
        lines.append(
            "- [F5] 構造化AIテレメトリ欄なし。"
            "明示されたAIテレメトリ（Claude Code / Codex CLI 等）の範囲外の"
            "発話数・往復数・品質は判断不能。"
        )
    # 依頼長さ層別（決定論観察。因果断定禁止。LLM が粒度提案する際の根拠）
    if ai_stats_valid:
        plo = ai_telemetry.get("prompt_length_observation")
        if isinstance(plo, str) and plo.strip():
            lines.append(f"- [F11] {plo.strip()}")

    by_site_value = stats.get("by_site")
    site_stats_valid = _valid_number_mapping(by_site_value)
    by_site = _mapping(by_site_value) if site_stats_valid else {}
    positive_sites = [
        (str(site), _number(minutes))
        for site, minutes in by_site.items()
        if _number(minutes) > 0
    ]
    positive_sites.sort(key=lambda item: item[1], reverse=True)
    site_observed = sum(minutes for _, minutes in positive_sites)
    browser_category_minutes = (
        _number(by_category.get("ブラウジング")) if category_stats_valid else None
    )
    by_app_value = stats.get("by_app")
    app_stats_valid = _valid_number_mapping(by_app_value)
    browser_foreground = (
        _known_browser_minutes(_mapping(by_app_value)) if app_stats_valid else None
    )
    if browser_foreground is not None and browser_foreground > 0:
        coverage = site_observed / browser_foreground * 100
        if coverage <= 100:
            coverage_text = (
                f"既知ブラウザ前景 {_fmt(browser_foreground)}分に対する概算URL観測率 "
                f"{_fmt(coverage)}%"
            )
        else:
            coverage_text = (
                f"既知ブラウザ前景 {_fmt(browser_foreground)}分。丸め後のサイト合計が"
                "前景時間を超えるためURL観測率は算出不能"
            )
    elif browser_foreground is not None:
        coverage_text = "既知ブラウザ前景時間が無いためURL観測率は算出不能"
    else:
        coverage_text = "既知ブラウザ前景時間は測定不能"

    browser_category_text = (
        f"{_fmt(browser_category_minutes)}分"
        if browser_category_minutes is not None else "測定不能"
    )

    if positive_sites:
        rendered = "、".join(
            f"{site} {_fmt(minutes)}分" for site, minutes in positive_sites[:10]
        )
        lines.append(
            f"- [F6] ブラウジングカテゴリ {browser_category_text} / "
            f"サイト観測合計 {_fmt(site_observed)}分 / {coverage_text}。"
            f"観測サイト（上位）: {rendered}。未観測部分の用途は不明。"
        )
    elif (
        (browser_category_minutes is not None and browser_category_minutes > 0)
        or (browser_foreground is not None and browser_foreground > 0)
    ):
        lines.append(
            f"- [F6] ブラウジングカテゴリ {browser_category_text} / "
            f"{coverage_text}。サイト別の用途は不明。"
        )
    elif not (category_stats_valid and app_stats_valid and site_stats_valid):
        lines.append(
            f"- [F6] ブラウジングカテゴリ {browser_category_text} / {coverage_text}。"
            "サイト別の用途は測定不能。"
        )
    else:
        lines.append("- [F6] ブラウジングカテゴリ・既知ブラウザ前景時間の実測なし。")

    entertainment_minutes = (
        _number(by_category.get("エンタメ")) if category_stats_valid else None
    )
    entertainment_observed = bool(
        entertainment_minutes is not None and entertainment_minutes > 0
    )
    if entertainment_observed:
        lines.append(f"- [F7] エンタメカテゴリ {_fmt(entertainment_minutes)}分。")
    elif entertainment_minutes is not None:
        lines.append("- [F7] エンタメカテゴリとして計上された時間はない。娯楽利用を示す定量根拠なし。")
    else:
        lines.append("- [F7] エンタメカテゴリ統計なし。娯楽利用の有無は測定不能。")

    prior = [
        item for item in (history or [])
        if (
            isinstance(item, Mapping)
            and item.get("day") != stats.get("day")
            and _number(item.get("total_minutes")) >= _MIN_BASELINE_ACTIVE_MINUTES
            and _switch_rate(item) is not None
        )
    ]
    prior_rates = [rate for item in prior if (rate := _switch_rate(item)) is not None]
    prior_totals = [
        _number(item.get("total_minutes"))
        for item in prior
        if _number(item.get("total_minutes")) > 0
    ]
    if len(prior_rates) >= _MIN_BASELINE_DAYS and switch_rate is not None:
        median_rate = median(prior_rates)
        comparison = (
            f"中央値比 {(switch_rate - median_rate) / median_rate * 100:+.0f}%"
            if median_rate > 0
            else "中央値が0のため比率算出不能"
        )
        total_text = (
            f" / アクティブ時間中央値 {_fmt(median(prior_totals))}分"
            if prior_totals else ""
        )
        lines.append(
            f"- [F8] 比較可能な過去{len(prior_rates)}日中央値: カテゴリ変更 "
            f"{_fmt(median_rate)}回/時{total_text}。当日は {_fmt(switch_rate)}回/時 "
            f"（{comparison}）。"
        )
    else:
        lines.append("- [F8] 比較可能な過去統計が不足しているため、通常範囲との比較は不能。")

    lines.append(_transition_fact(blocks, timezone))

    # 計測可否（F10 と入口ガードで同じ判定源を使う）
    # F10 が履歴から帯だけ出し、当日 watcher 停止時も focus_*/ai_* を推奨すると
    # モデルが PASS 条件に書いて入口ガード(advice_format)が契約エラー→L2縮退する。
    # 入口ガードと同じフラグで帯を抑止し対称にする。
    input_metrics_available = _valid_count_fields(
        stats.get("input"), ("focus_blocks", "focus_minutes", "active_input_minutes")
    )
    structured_ai_metrics_available = ai_stats_valid
    by_site_val_early = stats.get("by_site")
    site_metrics_available = _valid_number_mapping(by_site_val_early)

    # F10: 推奨PASS帯（履歴窓の中央値×0.85〜0.95）。ラベルは実窓長と一致させる
    _F10_HISTORY_WINDOW = 14  # 上限。短い history ではそのまま全件
    band_parts: list[str] = []
    history_list = list(history or [])
    f10_window = history_list[-_F10_HISTORY_WINDOW:]
    f10_window_days = len(f10_window)
    # 主要指標: context_switches / 上位カテゴリ / focus / ai_sessions
    def _hist_values(key_fn) -> list[float]:
        vals: list[float] = []
        for h in f10_window:
            if not isinstance(h, dict):
                continue
            v = key_fn(h)
            if isinstance(v, (int, float)) and isfinite(float(v)):
                vals.append(float(v))
        return vals

    cs_vals = _hist_values(lambda h: h.get("context_switches"))
    if len(cs_vals) >= 3:
        m = float(median(cs_vals))
        band_parts.append(
            f"context_switches {m * 0.85:.0f}〜{m * 0.95:.0f}"
        )
    by_cat_hist: dict[str, list[float]] = {}
    for h in f10_window:
        if not isinstance(h, dict):
            continue
        bc = h.get("by_category")
        if isinstance(bc, dict):
            for k, v in bc.items():
                if isinstance(v, (int, float)):
                    by_cat_hist.setdefault(str(k), []).append(float(v))
    # 当日上位カテゴリ優先
    top_cats = sorted(
        ((str(k), float(v)) for k, v in by_category.items() if isinstance(v, (int, float))),
        key=lambda x: -x[1],
    )[:3]
    for cat, _ in top_cats:
        vals = by_cat_hist.get(cat) or []
        if len(vals) >= 3:
            m = float(median(vals))
            band_parts.append(
                f"category_minutes:{cat} {m * 0.85:.0f}〜{m * 0.95:.0f}分"
            )
    # focus_*/ai_* 帯は当日計測可能時のみ（入口ガードと対称）
    if input_metrics_available:
        focus_vals = _hist_values(
            lambda h: (h.get("input") or {}).get("focus_blocks")
            if isinstance(h.get("input"), dict)
            else None
        )
        if len(focus_vals) >= 3:
            m = float(median(focus_vals))
            band_parts.append(f"focus_blocks {m * 0.85:.1f}〜{m * 0.95:.1f}")
    if structured_ai_metrics_available:
        ai_sess_vals = _hist_values(
            lambda h: (h.get("ai") or {}).get("sessions")
            if isinstance(h.get("ai"), dict)
            else None
        )
        if len(ai_sess_vals) >= 3:
            m = float(median(ai_sess_vals))
            band_parts.append(f"ai_cc_sessions {m * 0.85:.1f}〜{m * 0.95:.1f}")
        # ai_tool_errors_per_session: 入口ガードと同じ structured_ai ゲート
        err_per_sess_vals: list[float] = []
        for h in f10_window:
            if not isinstance(h, dict):
                continue
            from .experiments import metric_from_stats as _mfs

            v = _mfs("ai_tool_errors_per_session", h)
            if isinstance(v, (int, float)) and isfinite(float(v)):
                err_per_sess_vals.append(float(v))
        if len(err_per_sess_vals) >= 3:
            m = float(median(err_per_sess_vals))
            band_parts.append(
                f"ai_tool_errors_per_session {m * 0.85:.1f}〜{m * 0.95:.1f}"
            )
    # context_switches_per_hour: cs と total_minutes が両方あり測定可能日3日以上
    cph_vals: list[float] = []
    for h in f10_window:
        if not isinstance(h, dict):
            continue
        from .experiments import metric_from_stats as _mfs

        v = _mfs("context_switches_per_hour", h)
        if isinstance(v, (int, float)) and isfinite(float(v)):
            cph_vals.append(float(v))
    if len(cph_vals) >= 3:
        m = float(median(cph_vals))
        band_parts.append(
            f"context_switches_per_hour {m * 0.85:.1f}〜{m * 0.95:.1f}"
        )
    # site 帯を将来足す場合も site_metrics_available を掛けること（現状は未使用）
    if band_parts and f10_window_days > 0:
        lines.append(
            f"- [F10] 推奨PASS帯（過去{f10_window_days}日中央値×0.85〜0.95）: "
            + " / ".join(band_parts)
            + "。推奨帯はガイドであり、行動内容に合わせて外れてよい。"
        )

    short_record = total_minutes < SHORT_RECORD_MIN_MINUTES
    previous_day_available = _previous_day_available(stats, history)
    browser_sample_sufficient = bool(
        browser_category_minutes is not None and browser_category_minutes >= 30
    )
    max_actions = 1 if short_record else 3
    # 適応投与・未消化バックログ・short_record は min で合成
    # （dosing は proposed≥6、backlog は done=0 等でより早く1件に絞る）
    if action_stats is not None:
        try:
            from .memory import backlog_generation_cap, dosing_max_actions

            max_actions = min(
                max_actions,
                dosing_max_actions(action_stats),
                backlog_generation_cap(action_stats),
            )
        except Exception:
            pass

    # §Z3: 構造化 AI CLI セッション数（web を除く）。日誌行と F19 で同じ母数。
    cli_session_count = 0
    if ai_stats_valid:
        sources = ai_telemetry.get("sources")
        if isinstance(sources, Mapping) and sources:
            for name, bucket in sources.items():
                nkey = str(name)
                if nkey.endswith("-web") or nkey == "web":
                    continue
                if isinstance(bucket, Mapping):
                    n = bucket.get("sessions")
                    if isinstance(n, (int, float)) and not isinstance(n, bool):
                        cli_session_count += int(n)
        else:
            # sources 無しの旧 stats は sessions 合算を CLI とみなす
            cli_session_count = int(telemetry_sessions)

    # 画面計測が薄いのに AI CLI ログがある → 欠測疑い
    measurement_gap: tuple[int, int] | None = None
    if short_record and ai_stats_valid and cli_session_count >= 1:
        measurement_gap = (int(cli_session_count), int(tool_errors))
        lines.append(
            f"- [F19] 画面計測は{_fmt(total_minutes)}分だが AI CLI ログには"
            f"{cli_session_count}セッション・ツールエラー{tool_errors}回がある。"
            "画面側の計測欠測の疑いがある。"
        )

    reader_summary = _build_reader_summary(
        total_minutes=total_minutes,
        short_record=short_record,
        stats=stats,
        history=history,
        by_category=by_category,
        category_stats_valid=category_stats_valid,
        entertainment_minutes=entertainment_minutes,
        previous_day_available=previous_day_available,
        measurement_gap=measurement_gap,
    )

    reader_notes: list[str] = []
    if not (ai_stats_valid and telemetry_sessions > 0):
        ai_time = f"{_fmt(ai_minutes)}分" if ai_minutes is not None else "一定時間"
        reader_notes.append(
            f"AI関連画面は{ai_time}記録されていますが、会話回数や回答品質は"
            "計測できないため、画面切り替えだけからAIの使い方を評価しません。"
        )
    else:
        screen_minutes = stats.get("ai_screen_tool_minutes")
        source_buckets = ai_telemetry.get("sources")

        def _has_web_session(expected_sources: Sequence[str]) -> bool:
            if not isinstance(source_buckets, Mapping):
                return False
            for source in expected_sources:
                bucket = source_buckets.get(source)
                if not isinstance(bucket, Mapping):
                    continue
                sessions = bucket.get("sessions")
                if _valid_nonnegative_number(sessions) and _number(sessions) > 0:
                    return True
            return False

        unlogged_minutes = 0.0
        if isinstance(screen_minutes, Mapping):
            for tool, minutes in screen_minutes.items():
                if not _valid_nonnegative_number(minutes):
                    continue
                expected_sources = _SCREEN_TOOL_WEB_SOURCES.get(str(tool), ())
                if not _has_web_session(expected_sources):
                    unlogged_minutes += _number(minutes)
        if unlogged_minutes >= 30:
            reader_notes.append(
                "セッションログを取得できないAI画面が"
                f"{_fmt(unlogged_minutes)}分記録されています。🧠の数値はCLI・拡張由来のみで、"
                "AI利用全体の質ではありません。"
            )
    if (
        browser_category_minutes is not None
        and 0 < browser_category_minutes < 30
    ):
        reader_notes.append(
            f"ブラウザ利用は{_fmt(browser_category_minutes)}分と短いため、"
            "URL watcherの設定改善は現時点では優先しません。"
        )
    if not previous_day_available:
        reader_notes.append(
            "比較可能な前日の記録がないため、前日比ではなく絶対値で翌日の合否を判定します。"
        )
    if not afk_watcher_available:
        reader_notes.append(
            "AFKウォッチャーが無いため、合計時間は離席を含む可能性があります。"
        )
    if short_record:
        thr = int(SHORT_RECORD_MIN_MINUTES) if float(SHORT_RECORD_MIN_MINUTES).is_integer() else SHORT_RECORD_MIN_MINUTES
        lines.append(
            f"- [L9] 当日の記録が{thr}分未満で評価材料が少ない。問題を作らず、"
            "改善提案は維持行動を最大1件だけにする。"
        )
    if not previous_day_available:
        lines.append(
            "- [L10] 比較可能な前日統計がない。前日比のPASS/FAILは禁止し、"
            "単独で判定できる絶対値を使う。"
        )
    if not browser_sample_sufficient:
        lines.append(
            "- [L11] ブラウジング実測が30分未満。URL watcher設定を改善課題として"
            "優先しない。"
        )
    if not afk_watcher_available:
        # 判定・実験からの除外は行わない（注記のみ。スコープは将来判断）
        lines.append(
            "- [L12] AFK未計測のため合計時間・カテゴリ時間は過大の可能性"
            "（離席時間を含む）。"
        )

    by_site_val = by_site_val_early
    observed_sites = (
        frozenset(str(k).lower() for k in by_site_val)
        if isinstance(by_site_val, Mapping)
        else frozenset()
    )
    # input_metrics_available / structured_ai_metrics_available / site_metrics_available
    # は F10 直前で計算済み（入口ガードと同じ判定源）
    metric_baselines = _metric_baselines_from_history(stats, history)

    # F17: 風化した改善（直近7日イベント。達成断定・自動再オープンはしない）
    for de in (decay_events or []):
        detail = getattr(de, "detail", None) or ""
        if detail:
            lines.append(f"- [F17] 風化: {detail}")

    # F18: コーチ効果判定
    try:
        from .coachledger import format_f18_lines

        lines.extend(format_f18_lines(list(coach_entries or [])))
    except Exception:
        pass

    # D4: 摩擦ワーストセッションの 依頼/成果 digest（redact 済みのみ）
    if ai_stats_valid:
        digests = ai_telemetry.get("session_digests")
        if isinstance(digests, list) and digests:
            from .aiwork import top_friction_sessions

            for i, d in enumerate(top_friction_sessions(digests, limit=2), 1):
                if not isinstance(d, dict):
                    continue
                prompts = d.get("prompts_digest") if isinstance(d.get("prompts_digest"), list) else []
                first = ""
                if prompts:
                    first = str(prompts[0] or "")[:80]
                elif d.get("title"):
                    first = str(d.get("title") or "")[:80]
                reply = str(d.get("last_reply_digest") or "")[:120]
                proj = str(d.get("project") or "—")
                if not first and not reply:
                    continue
                req = f"依頼「{first}」" if first else "依頼（記録なし）"
                res = f"成果「{reply}」" if reply else "成果（記録なし）"
                lines.append(
                    f"- [F20] 摩擦セッション{i}（{proj}）: {req} / {res}"
                )

    # §S6: screenpipe 画面観測（参考・推定）。redact 済み行のみ受け取る
    if screenpipe_lines:
        block: list[str] = ["", "## screenpipe画面観測（参考・推定）"]
        total_chars = 0
        for raw in list(screenpipe_lines)[:3]:
            line = str(raw or "").strip()
            if not line:
                continue
            if total_chars + len(line) > 600:
                break
            block.append(f"- {line}")
            total_chars += len(line)
        if len(block) > 2:
            lines.extend(block)

    # 寿命管理の読者向け1行（空なら足さない。第35弾の空許容と両立）
    notes_out = list(reader_notes)
    for note in lifecycle_notes or ():
        text = str(note or "").strip()
        if text:
            notes_out.append(text)

    return _evidence(
        lines,
        ai_conversation_metrics_available=ai_stats_valid and telemetry_sessions > 0,
        entertainment_observed=entertainment_observed,
        reader_summary=reader_summary,
        reader_notes=tuple(notes_out),
        max_actions=max_actions,
        previous_day_available=previous_day_available,
        browser_sample_sufficient=browser_sample_sufficient,
        known_categories=cats,
        observed_sites=observed_sites,
        afk_watcher_available=afk_watcher_available,
        input_metrics_available=input_metrics_available,
        structured_ai_metrics_available=structured_ai_metrics_available,
        site_metrics_available=site_metrics_available,
        metric_baselines=metric_baselines or None,
        total_minutes=float(total_minutes) if isinstance(total_minutes, (int, float)) else None,
    )
