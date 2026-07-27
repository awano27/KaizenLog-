"""分類済みイベントを集計し、デイリーノート用のMarkdownセクションを生成する。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo

from .classifier import ClassifiedEvent
from .focus import FOCUS_MIN_MINUTES, InputStats


@dataclass
class Block:
    """同一カテゴリ・同一アプリの連続作業ブロック。"""

    start: datetime
    end: datetime
    category: str
    app: str
    titles: list[str] = field(default_factory=list)
    ai: bool = False
    tool: str | None = None

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass
class DailySummary:
    day: date
    total_minutes: float
    by_category: dict[str, float]  # 分
    by_app: dict[str, float]
    blocks: list[Block]
    ai_tool_minutes: dict[str, float]
    # 後方互換のためフィールド名は維持する。値は会話セッション数ではなく、
    # AI関連と分類された前景画面のactivity block数。
    ai_sessions: int
    context_switches: int  # カテゴリの切り替え回数
    by_site: dict[str, float] = field(default_factory=dict)  # ドメイン別（aw-watcher-web導入時のみ）

    @property
    def ai_activity_blocks(self) -> int:
        """AI関連画面のactivity block数（会話数・往復数ではない）。"""
        return self.ai_sessions


def _sorted_events(classified: list[ClassifiedEvent]) -> list[ClassifiedEvent]:
    """開始時刻順に並べる。集計・ブロック化は入力順に依存してはならない。"""
    return sorted(classified, key=lambda ce: (ce.event.start, ce.event.end))


def build_blocks(
    classified: list[ClassifiedEvent], gap_minutes: float = 5.0
) -> list[Block]:
    """連続するイベントをブロックにまとめる。gap_minutes以上空いたら別ブロック。"""
    blocks: list[Block] = []
    gap = timedelta(minutes=gap_minutes)
    for ce in _sorted_events(classified):
        e = ce.event
        prev = blocks[-1] if blocks else None
        if (
            prev is not None
            and prev.category == ce.category
            and prev.app == e.app
            and e.start - prev.end <= gap
        ):
            prev.end = max(prev.end, e.end)
            if e.title and e.title not in prev.titles:
                prev.titles.append(e.title)
        else:
            blocks.append(
                Block(
                    start=e.start,
                    end=e.end,
                    category=ce.category,
                    app=e.app,
                    titles=[e.title] if e.title else [],
                    ai=ce.ai,
                    tool=ce.matched_tool,
                )
            )
    return blocks


def summarize(
    day: date, classified: list[ClassifiedEvent], gap_minutes: float = 5.0
) -> DailySummary:
    by_category: dict[str, float] = defaultdict(float)
    by_app: dict[str, float] = defaultdict(float)
    by_site: dict[str, float] = defaultdict(float)
    ai_tool_minutes: dict[str, float] = defaultdict(float)

    # 重複区間は先着イベント優先でクリップし、同じ時間を二重計上しない
    cursor: datetime | None = None
    for ce in _sorted_events(classified):
        e = ce.event
        start = e.start if cursor is None else max(e.start, cursor)
        cursor = e.end if cursor is None else max(cursor, e.end)
        minutes = (e.end - start).total_seconds() / 60
        if minutes <= 0:
            continue
        by_category[ce.category] += minutes
        by_app[e.app] += minutes
        if e.domain:
            by_site[e.domain] += minutes
        if ce.ai and ce.matched_tool:
            ai_tool_minutes[ce.matched_tool] += minutes

    blocks = build_blocks(classified, gap_minutes)
    ai_sessions = sum(1 for b in blocks if b.ai)

    context_switches = 0
    prev_cat: str | None = None
    for b in blocks:
        if prev_cat is not None and b.category != prev_cat:
            context_switches += 1
        prev_cat = b.category

    total = sum(by_category.values())
    return DailySummary(
        day=day,
        total_minutes=total,
        by_category=dict(by_category),
        by_app=dict(by_app),
        blocks=blocks,
        ai_tool_minutes=dict(ai_tool_minutes),
        ai_sessions=ai_sessions,
        context_switches=context_switches,
        by_site=dict(by_site),
    )


def _fmt_minutes(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _fmt_site_minutes(minutes: float) -> str:
    """少量のサイト観測を「0分」と誤表示しない。"""
    if 0 < minutes < 1:
        return "<1m"
    return _fmt_minutes(minutes)


def _fmt_time(dt: datetime, tz: tzinfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def _markdown_table_cell(value: object) -> str:
    """Markdown 表セル用に一行化し、| と制御文字を無害化する。"""
    text = " ".join(str(value).split())
    return text.replace("|", "\\|")


def render_markdown(
    summary: DailySummary,
    tz: tzinfo,
    min_block_minutes: float = 3.0,
    max_timeline_rows: int = 60,
    input_stats: InputStats | None = None,
) -> str:
    """デイリーノートに埋め込むアクティビティログのMarkdownを生成する。"""
    lines: list[str] = []
    lines.append("## 📊 Activity Log")
    lines.append("")

    if summary.total_minutes <= 0:
        # 収集成功/watcher停止は断定しない（ここでは0分という事実だけ）
        lines.append("ActivityWatchから対象日のアクティブ時間を0分取得しました。")
        lines.append(
            "PCを使わなかった場合は正常です。"
            "使用した場合は `kaizenlog doctor` でwatcherと対象日を確認してください。"
        )
        lines.append("")
        return "\n".join(lines)

    lines.append(f"**合計アクティブ時間**: {_fmt_minutes(summary.total_minutes)}"
                 f" / コンテキストスイッチ: {summary.context_switches}回")
    lines.append("")

    if input_stats is not None:
        lines.append(
            f"**集中ブロック**: {len(input_stats.focus_blocks)}回 / "
            f"合計 {_fmt_minutes(input_stats.focus_minutes)}"
            f"（{FOCUS_MIN_MINUTES:.0f}分以上入力が続いた区間）"
            f" / キー入力 {input_stats.keypresses:,}回"
        )
        lines.append("")

    # カテゴリ別サマリー
    lines.append("### カテゴリ別")
    lines.append("")
    lines.append("| カテゴリ | 時間 | 割合 |")
    lines.append("| --- | ---: | ---: |")
    for cat, minutes in sorted(summary.by_category.items(), key=lambda x: -x[1]):
        pct = minutes / summary.total_minutes * 100
        lines.append(
            f"| {_markdown_table_cell(cat)} | {_fmt_minutes(minutes)} | {pct:.0f}% |"
        )
    lines.append("")

    # サイト別（aw-watcher-web 導入時のみ）
    if summary.by_site:
        lines.append("### 🌐 サイト別（watcher取得分・上位10）")
        lines.append("")
        lines.append("| サイト | 時間 |")
        lines.append("| --- | ---: |")
        for site, minutes in sorted(summary.by_site.items(), key=lambda x: -x[1])[:10]:
            lines.append(
                f"| {_markdown_table_cell(site)} | {_fmt_site_minutes(minutes)} |"
            )
        lines.append("")
        lines.append("※ watcherが取得できた部分だけで、ブラウザ時間の完全な内訳ではありません。")
        lines.append("")

    # AI作業の詳細
    if summary.ai_tool_minutes:
        lines.append("### 🤖 AI作業の内訳（画面分類による推定）")
        lines.append("")
        lines.append(
            "AI関連画面の前景ブロック数（推定）: "
            f"{summary.ai_activity_blocks}回（会話数・往復数ではありません）"
        )
        lines.append("")
        lines.append("| ツール | 時間 |")
        lines.append("| --- | ---: |")
        for tool, minutes in sorted(summary.ai_tool_minutes.items(), key=lambda x: -x[1]):
            lines.append(
                f"| {_markdown_table_cell(tool)} | {_fmt_minutes(minutes)} |"
            )
        lines.append("")

    # タイムライン（主要ブロックのみ）— 件数は実配列から決定論的に算出
    total_blocks = len(summary.blocks)
    eligible = [b for b in summary.blocks if b.minutes >= min_block_minutes]
    eligible_blocks = len(eligible)
    min_label = (
        f"{int(min_block_minutes)}分"
        if float(min_block_minutes).is_integer()
        else f"{min_block_minutes}分"
    )
    rows = eligible
    overflow_omitted = 0
    if eligible_blocks > max_timeline_rows:
        rows = sorted(eligible, key=lambda b: -b.minutes)[:max_timeline_rows]
        rows.sort(key=lambda b: b.start)
        overflow_omitted = eligible_blocks - max_timeline_rows
    shown_blocks = len(rows)
    if rows:
        lines.append("### タイムライン")
        lines.append("")
        lines.append(f"{min_label}以上の画面ブロックを時刻順に表示。")
        if overflow_omitted > 0:
            lines.append(
                f"全{total_blocks}件中、{min_label}以上は{eligible_blocks}件です。"
                f"長時間の上位{shown_blocks}件を時刻順に表示し、"
                f"対象内の{overflow_omitted}件を省略しています。"
            )
        lines.append("")
        lines.append("| 時刻 | 時間 | カテゴリ | アプリ | 内容 |")
        lines.append("| --- | ---: | --- | --- | --- |")
        for b in rows:
            title = b.titles[0] if b.titles else ""
            if len(title) > 60:
                title = title[:57] + "..."
            lines.append(
                f"| {_fmt_time(b.start, tz)}-{_fmt_time(b.end, tz)} "
                f"| {_fmt_minutes(b.minutes)} "
                f"| {_markdown_table_cell(b.category)} "
                f"| {_markdown_table_cell(b.app)} "
                f"| {_markdown_table_cell(title)} |"
            )
        lines.append("")

    return "\n".join(lines)
