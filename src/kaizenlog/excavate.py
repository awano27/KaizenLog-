"""過去ログ発掘監査: ディスク上の AI セッションを読み取り専用で集計する。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .aiwork import (
    AISession,
    RetryChain,
    UserPrompt,
    available_adapters,
    collect_ai_telemetry,
    compute_loop_tax,
    detect_retry_chains,
    estimate_sessions_cost,
    max_loop_episode,
    retry_chain_excerpts,
)
from .config import Config
from .privacy import make_redactor
from .vault import atomic_write_text, read_text_preserve_newlines, upsert_section

EXCAVATE_MARKER = "kaizenlog:excavate"


@dataclass
class DayBucket:
    day: date
    episode_count: int = 0
    wasted_tokens: int | None = None  # None = 不明
    est_cost_usd: float | None = None
    tool_errors: int = 0
    sessions: int = 0
    output_tokens: int = 0


@dataclass
class ExcavateReport:
    start: date
    end: date
    session_count: int
    output_tokens: int
    est_cost_usd: float | None  # 不明は None
    loop_episodes: int
    loop_wasted_tokens: int | None
    loop_cost_usd: float | None
    retry_chain_count: int
    tool_errors: int
    worst_days: list[DayBucket] = field(default_factory=list)
    worst_excerpt: str | None = None
    adapters_used: list[str] = field(default_factory=list)
    usd_jpy: float | None = None

    @property
    def period_label(self) -> str:
        return f"{self.start.isoformat()} 〜 {self.end.isoformat()}"


def run_excavate(
    cfg: Config,
    *,
    days: int = 90,
    as_of: date | None = None,
    redactor: Callable[[str], str] | None = None,
) -> ExcavateReport:
    """期間全体を1パス走査して発掘レポートを返す。stats/日誌は書かない。"""
    if days < 1:
        raise ValueError("days は 1 以上")
    as_of = as_of or datetime.now(ZoneInfo(cfg.timezone)).date()
    end = as_of
    start = end - timedelta(days=days - 1)
    tz = ZoneInfo(cfg.timezone)
    start_dt = datetime.combine(start, time.min, tzinfo=tz)
    end_dt = datetime.combine(end, time.min, tzinfo=tz) + timedelta(days=1)

    adapters = available_adapters(cfg) if cfg.aiwork.enabled else []
    if not adapters:
        raise FileNotFoundError(
            "テレメトリソースが見つかりません"
            "（確認: claude_projects_dir / codex_sessions_dir）"
        )
    adapter_names: list[str] = []
    for a in adapters:
        n = getattr(a, "name", None)
        if isinstance(n, str) and n:
            adapter_names.append(n)
        else:
            adapter_names.append(type(a).__name__)
    sessions, prompts, _internal = collect_ai_telemetry(adapters, start_dt, end_dt)
    # internal は collect が除外済み
    pricing = cfg.aiwork.pricing or None
    chains = detect_retry_chains(prompts)

    # 日別セッション集計
    by_day_sess: dict[date, list[AISession]] = {}
    for s in sessions:
        d = s.start.astimezone(tz).date()
        if start <= d <= end:
            by_day_sess.setdefault(d, []).append(s)

    # チェーンを先頭プロンプトのローカル日付へ帰属
    by_day_chains: dict[date, list[RetryChain]] = {}
    for c in chains:
        if not c.prompts:
            continue
        d = c.prompts[0].timestamp.astimezone(tz).date()
        if start <= d <= end:
            by_day_chains.setdefault(d, []).append(c)

    day_buckets: list[DayBucket] = []
    total_loop_eps = 0
    total_loop_tok: int | None = 0
    total_loop_cost: float | None = 0.0
    any_loop_tok_unknown = False
    any_loop_cost_unknown = False
    total_tool_err = 0
    worst_excerpt: str | None = None
    worst_ep_len = -1

    # 全セッションをトークン帰属に使い、日別に会計
    for d in sorted(by_day_chains.keys() | by_day_sess.keys()):
        day_chains = by_day_chains.get(d, [])
        day_sess = by_day_sess.get(d, [])
        tax = compute_loop_tax(day_chains, sessions, pricing=pricing)
        tok = tax.total_wasted_tokens
        cost = tax.est_cost_usd
        if tax.episode_count > 0:
            if tok is None:
                any_loop_tok_unknown = True
            else:
                total_loop_tok = int(total_loop_tok or 0) + int(tok)
            if cost is None:
                any_loop_cost_unknown = True
            else:
                total_loop_cost = float(total_loop_cost or 0.0) + float(cost)
        total_loop_eps += tax.episode_count
        err = sum(int(s.tool_errors or 0) for s in day_sess)
        total_tool_err += err
        out_tok = sum(int(s.output_tokens or 0) for s in day_sess)
        day_buckets.append(
            DayBucket(
                day=d,
                episode_count=tax.episode_count,
                wasted_tokens=tok if tax.episode_count else 0,
                est_cost_usd=cost if tax.episode_count else 0.0,
                tool_errors=err,
                sessions=len(day_sess),
                output_tokens=out_tok,
            )
        )
        # 最悪エピソード抜粋
        ep = max_loop_episode(tax)
        if ep is not None and ep.chain.length > worst_ep_len:
            worst_ep_len = ep.chain.length
            excerpts = retry_chain_excerpts(
                [ep.chain], redactor=redactor, max_chains=1
            )
            worst_excerpt = excerpts[0] if excerpts else None

    if total_loop_eps == 0:
        total_loop_tok = 0
        total_loop_cost = 0.0
    else:
        if any_loop_tok_unknown:
            total_loop_tok = None
        if any_loop_cost_unknown:
            total_loop_cost = None

    # 期間セッションコスト（モデル不明混在 → 不明）
    est_cost, uncosted, _ = estimate_sessions_cost(sessions, pricing)
    period_cost: float | None
    if uncosted > 0:
        period_cost = None  # fail-closed: 不明混在
    else:
        period_cost = float(est_cost)

    # 最悪ループ日 Top5: エピソード数→浪費tokens
    def _day_rank(b: DayBucket):
        wt = b.wasted_tokens if b.wasted_tokens is not None else -1
        return (b.episode_count, wt)

    worst_days = sorted(
        [b for b in day_buckets if b.episode_count > 0],
        key=_day_rank,
        reverse=True,
    )[:5]

    return ExcavateReport(
        start=start,
        end=end,
        session_count=len(sessions),
        output_tokens=sum(int(s.output_tokens or 0) for s in sessions),
        est_cost_usd=period_cost,
        loop_episodes=total_loop_eps,
        loop_wasted_tokens=total_loop_tok,
        loop_cost_usd=total_loop_cost,
        retry_chain_count=len(chains),
        tool_errors=total_tool_err,
        worst_days=worst_days,
        worst_excerpt=worst_excerpt,
        adapters_used=adapter_names,
        usd_jpy=getattr(cfg.aiwork, "usd_jpy", None),
    )


def format_excavate_report(report: ExcavateReport) -> str:
    """端末・Markdown 共通サマリ。"""
    lines = [
        f"# 発掘監査 {report.period_label}",
        "",
        f"ソース: {', '.join(report.adapters_used) or '（なし）'}",
        f"セッション: {report.session_count} / 出力トークン: {report.output_tokens:,}",
    ]
    if report.est_cost_usd is None:
        lines.append("推定コスト: 不明（モデル単価不明混在）")
    else:
        money = f"${report.est_cost_usd:.2f}"
        if report.usd_jpy and report.usd_jpy > 0:
            money += f"（¥{int(round(report.est_cost_usd * report.usd_jpy))}）"
        lines.append(f"推定コスト: {money}")

    lines.append("")
    lines.append("## 空転税（ループ税・期間合計）")
    lines.append(f"エピソード: {report.loop_episodes}")
    if report.loop_wasted_tokens is None:
        lines.append("浪費トークン: 不明")
    else:
        lines.append(f"浪費トークン: {report.loop_wasted_tokens:,}")
    if report.loop_cost_usd is None:
        lines.append("空転税: 不明")
    else:
        lm = f"${report.loop_cost_usd:.2f}"
        if report.usd_jpy and report.usd_jpy > 0:
            lm += f"（¥{int(round(report.loop_cost_usd * report.usd_jpy))}）"
        lines.append(f"空転税: {lm}")
    lines.append(f"リトライ連鎖: {report.retry_chain_count}")
    lines.append(f"ツールエラー: {report.tool_errors}")

    lines.append("")
    lines.append("## 最悪ループ日 Top5")
    if not report.worst_days:
        lines.append("- （計測なし）")
    else:
        for b in report.worst_days:
            tok = "不明" if b.wasted_tokens is None else f"{b.wasted_tokens}"
            lines.append(
                f"- {b.day.isoformat()}: {b.episode_count}エピソード / "
                f"浪費tokens {tok}"
            )
    if report.worst_excerpt:
        lines.append("")
        lines.append("## 最悪エピソード抜粋")
        lines.append(f"- {report.worst_excerpt}")
    lines.append("")
    return "\n".join(lines)


def write_excavate_report(path: Path, body_md: str) -> Path:
    """マーカー区間として冪等書き込み。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        content = read_text_preserve_newlines(path)
    else:
        content = ""
    updated = upsert_section(content, EXCAVATE_MARKER, body_md, position="bottom")
    atomic_write_text(path, updated)
    return path
