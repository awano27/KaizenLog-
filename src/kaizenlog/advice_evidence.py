"""日次改善提案へ渡す、機械可読統計由来の根拠と測定限界。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from math import isfinite
import re
from statistics import median
from typing import Any

from .collector import _is_browser_app

_MIN_BASELINE_DAYS = 3
_MIN_BASELINE_ACTIVE_MINUTES = 60.0


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


def build_advice_evidence(
    stats: Mapping[str, Any] | None,
    history: Sequence[Mapping[str, Any]] | None = None,
    *,
    timezone: tzinfo | None = None,
    source_status: str = "unverified",
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
        "（現在はClaude Code）が存在する場合だけ。画面滞在時間から推定しない。",
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
        "",
        "## 確定事実",
    ]

    invalid_stats = bool(stats) and not (
        _valid_nonnegative_number(stats.get("total_minutes"))
        and _valid_nonnegative_number(stats.get("context_switches"))
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
        return _evidence(lines)

    blocks_value = stats.get("blocks")
    blocks_valid = isinstance(blocks_value, list)
    blocks = blocks_value if blocks_valid else []
    total_minutes = _number(stats.get("total_minutes"))
    context_switches = int(_number(stats.get("context_switches")))
    switch_rate = _switch_rate(stats)
    rate_text = f" / 1時間あたり {_fmt(switch_rate)}回" if switch_rate is not None else ""
    block_count_text = f"{len(blocks)}件" if blocks_valid else "測定不能"
    lines.append(
        f"- [F1] 合計アクティブ時間 {_fmt(total_minutes)}分 / "
        f"activity block {block_count_text} / カテゴリ変更 {context_switches}回{rate_text}。"
    )

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
        lines.append(
            f"- [F5] Claude Codeテレメトリ: セッション {telemetry_sessions}回 / "
            f"2往復以下 {fragmented}回 / ツールエラー {tool_errors}回 / "
            f"中断・拒否 {interruptions}回{retry_part}。"
        )
    elif ai_stats_valid:
        lines.append(
            "- [F5] 統計に記録されたClaude Codeテレメトリは0件（利用ゼロとは限らない）。"
            "ChatGPT、Cursor、Copilotを含むAI会話の発話数・往復数は判断不能。"
        )
    else:
        lines.append(
            "- [F5] 構造化AIテレメトリ欄なし。AI会話の発話数・往復数・品質は判断不能。"
        )

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

    short_record = total_minutes < 120
    previous_day_available = _previous_day_available(stats, history)
    browser_sample_sufficient = bool(
        browser_category_minutes is not None and browser_category_minutes >= 30
    )
    max_actions = 1 if short_record else 3

    summary_parts = [
        (
            f"本日の記録は合計{_fmt(total_minutes)}分のため、"
            "1日の働き方を評価するにはデータ不足です。"
            if short_record
            else f"本日は合計{_fmt(total_minutes)}分の作業が記録されています。"
        )
    ]
    if focus_blocks is not None and focus_minutes is not None and focus_blocks > 0:
        summary_parts.append(
            f"一方、25分以上の集中ブロックを{focus_blocks}回"
            f"（合計{_fmt(focus_minutes)}分）確保できており、維持したい実績です。"
        )
    elif focus_blocks == 0:
        summary_parts.append(
            "25分以上の集中ブロックは記録されていませんが、単日の原因は判断できません。"
        )

    reader_notes: list[str] = []
    if not (ai_stats_valid and telemetry_sessions > 0):
        ai_time = f"{_fmt(ai_minutes)}分" if ai_minutes is not None else "一定時間"
        reader_notes.append(
            f"AI関連画面は{ai_time}記録されていますが、会話回数や回答品質は"
            "計測できないため、画面切り替えだけからAIの使い方を評価しません。"
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
    if not reader_notes:
        reader_notes.append("現時点で、追加の計測上の注意はありません。")

    if short_record:
        lines.append(
            "- [L9] 当日の記録が120分未満で評価材料が少ない。問題を作らず、"
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

    return _evidence(
        lines,
        ai_conversation_metrics_available=ai_stats_valid and telemetry_sessions > 0,
        entertainment_observed=entertainment_observed,
        reader_summary=" ".join(summary_parts),
        reader_notes=tuple(reader_notes),
        max_actions=max_actions,
        previous_day_available=previous_day_available,
        browser_sample_sufficient=browser_sample_sufficient,
    )
