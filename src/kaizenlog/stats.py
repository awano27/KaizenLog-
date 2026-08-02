"""日次統計のJSON永続化。

Markdownの日誌とは別に、機械可読な統計を `.kaizenlog/stats/YYYY-MM-DD.json` に
蓄積する。パターン検出（patterns.py）や外部ツールがここから履歴を読む。
ドットフォルダなのでObsidianのファイルツリーには表示されない。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .aiwork import (
    AISession,
    LoopTaxSummary,
    RetryChain,
    estimate_sessions_cost,
    loop_tax_to_stats_dict,
    _normalize_screen_tool_minutes,
    prompt_length_observation,
    session_digests_for_stats,
)
from .focus import InputStats
from .report import DailySummary
from .vault import atomic_write_text


def _round_minutes(d: dict[str, float]) -> dict[str, float]:
    return {k: round(v, 1) for k, v in d.items()}


def _source_name(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else "unknown"


def activity_fingerprint(activity_md: str) -> str:
    """Activityセクションと統計が同じ生成結果か確認する指紋。"""
    canonical = activity_md.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_stats(
    day: date,
    summary: DailySummary,
    cc_sessions: list[AISession],
    input_stats: InputStats | None = None,
    activity_md: str | None = None,
    retry_chains: list[RetryChain] | None = None,
    afk_watcher_available: bool | None = None,
    pricing: dict[str, float] | None = None,
    title_redactor: Callable[[str], str] | None = None,
    goal_text: str | None = None,
    goal_category: str | None = None,
    goal_achieved: int | None = None,
    internal_ai_sessions: int = 0,
    loop_tax_summary: LoopTaxSummary | None = None,
    outcome_git: list[dict] | None = None,
    screenpipe: Mapping[str, Any] | None = None,
    effort: Mapping[str, Any] | None = None,
) -> dict:
    projects: dict[str, dict] = {}
    for s in cc_sessions:
        p = projects.setdefault(
            s.project,
            {
                "sessions": 0,
                "turns": 0,
                "errors": 0,
                "fragmented": 0,
                "retry_chains": 0,
                "interruptions": 0,
            },
        )
        p["sessions"] += 1
        p["turns"] += s.user_turns
        p["errors"] += s.tool_errors
        p["fragmented"] += 1 if s.is_fragmented else 0
        p["interruptions"] = p.get("interruptions", 0) + s.interruptions

    chains = retry_chains or []
    for chain in chains:
        p = projects.setdefault(
            chain.project,
            {
                "sessions": 0,
                "turns": 0,
                "errors": 0,
                "fragmented": 0,
                "retry_chains": 0,
                "interruptions": 0,
            },
        )
        p["retry_chains"] = p.get("retry_chains", 0) + 1

    # ソース別内訳（合算値は互換のため ai 直下に維持）
    sources: dict[str, dict] = {}
    for s in cc_sessions:
        src = _source_name(s.source)
        bucket = sources.setdefault(
            src,
            {
                "sessions": 0,
                "fragmented": 0,
                "tool_errors": 0,
                "interruptions": 0,
                "retry_chains": 0,
            },
        )
        bucket["sessions"] += 1
        bucket["fragmented"] += 1 if s.is_fragmented else 0
        bucket["tool_errors"] += s.tool_errors
        bucket["interruptions"] += s.interruptions
    est_cost, uncosted, cost_by_src = estimate_sessions_cost(cc_sessions, pricing)
    for src, cbucket in cost_by_src.items():
        bucket = sources.setdefault(
            src,
            {
                "sessions": 0,
                "fragmented": 0,
                "tool_errors": 0,
                "interruptions": 0,
                "retry_chains": 0,
            },
        )
        bucket["est_cost_usd"] = round(float(cbucket.get("est_cost_usd", 0.0)), 4)
        bucket["uncosted_tokens"] = int(cbucket.get("uncosted_tokens", 0))
    # リトライ連鎖はプロンプト側の source が無い場合があるため、
    # チェーン先頭プロンプトの source があれば割当、無ければ合算のみ
    for chain in chains:
        src = _source_name(chain.prompts[0].source) if chain.prompts else "unknown"
        bucket = sources.setdefault(
            src,
            {
                "sessions": 0,
                "fragmented": 0,
                "tool_errors": 0,
                "interruptions": 0,
                "retry_chains": 0,
            },
        )
        bucket["retry_chains"] += 1

    # v2: セッション横断のテレメトリ集約（週次・実験・baseline 用）
    n_sess = len(cc_sessions)
    turns_total = sum(int(s.user_turns or 0) for s in cc_sessions)
    output_tokens = sum(int(s.output_tokens or 0) for s in cc_sessions)
    api_calls = sum(int(s.api_calls or 0) for s in cc_sessions)
    tool_total: Counter[str] = Counter()
    models_set: set[str] = set()
    for s in cc_sessions:
        if s.tool_counts:
            tool_total.update(s.tool_counts)
        if s.models:
            models_set.update(str(m) for m in s.models if m)
    avg_turns = round(turns_total / n_sess, 1) if n_sess else None
    # 上位5ツール（合算回数）
    tool_counts = {name: int(cnt) for name, cnt in tool_total.most_common(5)}

    stats = {
        # v2: ai に turns/tokens/tools/models を追加。読込側はキー欠損を許容（分岐しない）
        "version": 2,
        "day": day.isoformat(),
        "total_minutes": round(summary.total_minutes, 1),
        "context_switches": summary.context_switches,
        # ai_sessions / ai_activity_blocks はともに画面イベントをまとめたブロック数であり、
        # 会話セッション数ではない。旧名は互換のため維持する。
        "ai_sessions": summary.ai_sessions,
        "ai_activity_blocks": summary.ai_activity_blocks,
        "by_category": _round_minutes(summary.by_category),
        "by_app": _round_minutes(summary.by_app),
        "by_site": _round_minutes(summary.by_site),
        "ai_screen_tool_minutes": _round_minutes(
            _normalize_screen_tool_minutes(summary.ai_tool_minutes)
        ),
        "blocks": [
            {
                "start": b.start.isoformat(),
                "end": b.end.isoformat(),
                "category": b.category,
                "app": b.app,
                "minutes": round(b.minutes, 1),
                "title": (b.titles[0][:60] if b.titles else ""),
            }
            for b in summary.blocks
        ],
        "ai": {
            "sessions": n_sess,
            "internal_ai_sessions": int(internal_ai_sessions or 0),
            "fragmented": sum(1 for s in cc_sessions if s.is_fragmented),
            "tool_errors": sum(s.tool_errors for s in cc_sessions),
            "interruptions": sum(s.interruptions for s in cc_sessions),
            # リトライ連鎖（摩擦の一次指標）。旧 stats には無い → 読み手は 0/欠落を許容
            "retry_chains": len(chains),
            "retry_prompts": sum(c.length for c in chains),
            "projects": projects,
            "sources": sources,
            # output tokens ベースの概算（input/cache 未計上）。旧 stats は欠落可
            "est_cost_usd": est_cost,
            "uncosted_tokens": uncosted,
            # v2 テレメトリ（キー欠損時は v1 と同様に無視）
            "turns_total": turns_total,
            "avg_turns": avg_turns,
            "output_tokens": output_tokens,
            "api_calls": api_calls,
            "tool_counts": tool_counts,
            "models": sorted(models_set),
            # 週次摩擦ワースト用（title は redact して保存。retry_touch は連鎖関与）
            "session_digests": session_digests_for_stats(
                cc_sessions,
                day.isoformat(),
                redactor=title_redactor,
                retry_chains=chains,
            ),
        },
    }
    if loop_tax_summary is not None:
        stats["ai"]["loop_tax"] = loop_tax_to_stats_dict(
            loop_tax_summary, redactor=title_redactor
        )
    # ブラウザ AI: source 接尾辞 `-web` で判定（tools_measurable 非依存）。
    # 命名規約: chatgpt-web / claude-web / gemini-web 等。トークン系キーとは分離。
    web_sessions = [
        s
        for s in cc_sessions
        if str(getattr(s, "source", "") or "").endswith("-web")
    ]
    if web_sessions:
        stats["ai"]["web_sessions"] = len(web_sessions)
        stats["ai"]["web_user_turns"] = sum(s.user_turns for s in web_sessions)
        stats["ai"]["web_assistant_chars"] = sum(
            int(s.assistant_chars or 0) for s in web_sessions
        )
    obs = prompt_length_observation(cc_sessions)
    if obs:
        stats["ai"]["prompt_length_observation"] = obs
    if input_stats is not None:
        stats["input"] = {
            "keypresses": input_stats.keypresses,
            "clicks": input_stats.clicks,
            "active_input_minutes": input_stats.active_input_minutes,
            "focus_blocks": len(input_stats.focus_blocks),
            "focus_minutes": round(input_stats.focus_minutes, 1),
        }
    if activity_md is not None:
        stats["activity_sha256"] = activity_fingerprint(activity_md)
    # AFK 欠測フラグ。旧 stats はキー無し → 読み手は true（従来挙動）とみなす。
    # 判定・実験計測からの除外は本キー導入後も行わない（注記のみ。将来判断）。
    if afk_watcher_available is not None:
        stats["afk_watcher_available"] = bool(afk_watcher_available)
    # §C1: コミット統計（subjects は呼び出し側で redact 済みを渡す）
    if outcome_git is not None:
        stats["outcome_git"] = list(outcome_git)
    # screenpipe は件数のみ（本文は保存しない）
    if screenpipe is not None:
        stats["screenpipe"] = {
            "queried_blocks": int(screenpipe.get("queried_blocks") or 0),
            "filled_blocks": int(screenpipe.get("filled_blocks") or 0),
        }
    if effort is not None:
        stats["effort"] = dict(effort)
    # 今日の目標（redact 適用後の文言のみ保存。generate がノートから読む）
    if goal_text:
        stats["goal_text"] = str(goal_text)
        if goal_category:
            stats["goal_category"] = str(goal_category)
    # 自己申告達成度（0-100）。後方互換の追加キーのみ。
    if goal_achieved is not None:
        try:
            n = int(goal_achieved)
            if 0 <= n <= 100:
                stats["goal_achieved"] = n
        except (TypeError, ValueError):
            pass
    return stats


def write_stats(
    stats_dir: Path,
    day: date,
    summary: DailySummary,
    cc_sessions: list[AISession],
    input_stats: InputStats | None = None,
    activity_md: str | None = None,
    retry_chains: list[RetryChain] | None = None,
    afk_watcher_available: bool | None = None,
    pricing: dict[str, float] | None = None,
    title_redactor: Callable[[str], str] | None = None,
    goal_text: str | None = None,
    goal_category: str | None = None,
    goal_achieved: int | None = None,
    internal_ai_sessions: int = 0,
    loop_tax_summary: LoopTaxSummary | None = None,
    outcome_git: list[dict] | None = None,
    screenpipe: Mapping[str, Any] | None = None,
    effort: Mapping[str, Any] | None = None,
) -> Path:
    stats_dir.mkdir(parents=True, exist_ok=True)
    path = stats_dir / f"{day.isoformat()}.json"
    atomic_write_text(
        path,
        json.dumps(
            build_stats(
                day,
                summary,
                cc_sessions,
                input_stats,
                activity_md,
                retry_chains,
                afk_watcher_available=afk_watcher_available,
                title_redactor=title_redactor,
                pricing=pricing,
                goal_text=goal_text,
                goal_category=goal_category,
                goal_achieved=goal_achieved,
                internal_ai_sessions=internal_ai_sessions,
                loop_tax_summary=loop_tax_summary,
                outcome_git=outcome_git,
                screenpipe=screenpipe,
                effort=effort,
            ),
            ensure_ascii=False,
            indent=1,
        ),
    )
    return path


def missing_days(stats_dir: Path, end_day: date, lookback: int) -> list[date]:
    """end_dayより前のlookback日間で、統計が存在しない日を古い順に返す（欠損日の補完用）。"""
    stats_dir = Path(stats_dir)
    return [
        d
        for i in range(lookback, 0, -1)
        for d in [end_day - timedelta(days=i)]
        if not (stats_dir / f"{d.isoformat()}.json").is_file()
    ]


def load_stats(stats_dir: Path, days: int, end_day: date) -> list[dict]:
    """end_dayを含む直近days日分の統計を古い順に返す（存在する日のみ）。"""
    out = []
    for i in range(days - 1, -1, -1):
        path = stats_dir / f"{(end_day - timedelta(days=i)).isoformat()}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # ValueError は JSONDecodeError と UnicodeDecodeError（部分書き込みで
            # 生じる不正UTF-8）の両方を覆う。1日分の破損で patterns/block を落とさない
            continue
        if isinstance(data, dict) and "day" in data:
            out.append(data)
    return out
