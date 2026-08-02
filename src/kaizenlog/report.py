"""分類済みイベントを集計し、デイリーノート用のMarkdownセクションを生成する。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from math import isfinite
from typing import Any

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


def _fmt_under_threshold_minutes(minutes: float) -> str:
    """細切れ集計用の丸めた分数（カテゴリ別は時間表記にしない）。"""
    return f"{int(round(minutes))}分"


def _under_threshold_lines(
    blocks: list[Block],
    *,
    total_minutes: float,
    min_block_minutes: float,
    min_label: str,
    tz: tzinfo,
) -> list[str]:
    under = [block for block in blocks if block.minutes < min_block_minutes]
    if not under:
        return []

    under_minutes = sum(block.minutes for block in under)
    ratio = under_minutes / total_minutes * 100 if total_minutes > 0 else 0.0
    lines = [
        f"表示外: {min_label}未満のブロック {len(under)}件・計"
        f"{_fmt_under_threshold_minutes(under_minutes)}（合計 {_fmt_minutes(total_minutes)} の{ratio:.0f}%）。"
    ]
    by_category: dict[str, float] = defaultdict(float)
    by_hour: dict[int, tuple[int, float]] = {}
    for block in under:
        by_category[block.category] += block.minutes
        hour = block.start.astimezone(tz).hour
        count, minutes = by_hour.get(hour, (0, 0.0))
        by_hour[hour] = (count + 1, minutes + block.minutes)
    categories = sorted(by_category.items(), key=lambda item: (-item[1], item[0]))[:4]
    lines.append(
        "内訳は "
        + " / ".join(
            f"{category} {_fmt_under_threshold_minutes(minutes)}"
            for category, minutes in categories
        )
        + "。"
    )
    hours = sorted(by_hour.items(), key=lambda item: (-item[1][1], item[0]))[:3]
    lines.append(
        "細切れが集中した時間帯: "
        + "、".join(
            f"{hour}時台 {count}件・{_fmt_under_threshold_minutes(minutes)}"
            for hour, (count, minutes) in hours
        )
        + "。"
    )
    return lines


def _stat_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _nested_stat(stats: Mapping, *keys: str) -> float | None:
    value: object = stats
    for key in keys:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return _stat_number(value)


def _fmt_change_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def render_change_table(today: Mapping, prev: Mapping | None) -> str:
    """前日との差を欠損値を補わずに表示する。"""
    if prev is None:
        return ""
    # §E6: 前日が薄い日は表を出さない
    prev_total = _nested_stat(prev, "total_minutes")
    if prev_total is not None and prev_total < 60:
        return (
            f"※ 前日は計測が薄い（合計{_fmt_minutes(prev_total)}）ため、"
            "前日比は表示しません。"
        )
    metrics = (
        ("ツールエラー", ("ai", "tool_errors"), False),
        ("リトライ連鎖", ("ai", "retry_chains"), False),
        ("2往復以下のセッション", ("ai", "fragmented"), False),
        ("AI作業", ("by_category", "AI作業"), True),
        ("合計アクティブ", ("total_minutes",), True),
    )
    rows: list[str] = []
    for label, keys, is_minutes in metrics:
        before = _nested_stat(prev, *keys)
        current = _nested_stat(today, *keys)
        if before is None or current is None:
            continue
        before_text = _fmt_minutes(before) if is_minutes else _fmt_change_number(before)
        current_text = _fmt_minutes(current) if is_minutes else _fmt_change_number(current)
        delta = current - before
        delta_text = (
            ("+" if delta >= 0 else "-") + _fmt_minutes(abs(delta))
            if is_minutes
            else f"{delta:+g}"
        )
        rows.append(f"| {label} | {before_text} | {current_text} | {delta_text} |")
    if not rows:
        return ""
    return "\n".join(
        [
            "### 前日からの変化",
            "",
            "| 指標 | 前日 | 今日 | 増減 |",
            "| --- | ---: | ---: | ---: |",
            *rows,
            "",
            "※ 数値の向きだけを示します。稼働時間が短い日は多くの指標が自動的に下がるため、改善提案の効果を示すものではありません。",
        ]
    )


@dataclass(frozen=True)
class SessionSpan:
    """タイムライン突合用の AI セッション時間帯。"""

    start: datetime
    end: datetime
    tool_class: str  # claude / codex / chatgpt / gemini / …
    label: str  # redact 済み「project: title」


def source_to_tool_class(source: str | None) -> str:
    """AISession.source → Block.tool 相当のツールクラス。"""
    s = (source or "").strip().lower()
    if not s:
        return "unknown"
    if s in ("claude-code", "claude-web") or s.startswith("claude"):
        return "claude"
    if s == "codex" or s.startswith("codex"):
        return "codex"
    if "chatgpt" in s or s in ("openai-web",):
        return "chatgpt"
    if "gemini" in s:
        return "gemini"
    if "cursor" in s:
        return "cursor"
    if s.endswith("-web"):
        return s[: -len("-web")] or "web"
    return s


def build_session_spans(
    sessions: Sequence[Any],
    *,
    redactor: Callable[[str], str] | None = None,
    title_max: int = 60,
) -> list[SessionSpan]:
    """AISession 群からタイムライン突合用スパンを作る。"""
    out: list[SessionSpan] = []
    for s in sessions:
        start = getattr(s, "start", None)
        end = getattr(s, "end", None)
        if start is None or end is None:
            continue
        project = str(getattr(s, "project", None) or "—")
        title = str(getattr(s, "title", None) or "")
        raw = f"{project}: {title}" if title else project
        if redactor is not None:
            raw = redactor(raw)
        if len(raw) > title_max:
            raw = raw[: title_max - 3] + "..."
        out.append(
            SessionSpan(
                start=start,
                end=end,
                tool_class=source_to_tool_class(getattr(s, "source", None)),
                label=raw,
            )
        )
    return out


def _overlap_minutes(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> float:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end <= start:
        return 0.0
    return (end - start).total_seconds() / 60.0


def _tool_class_matches(block_tool: str | None, span_tool: str) -> bool:
    """Block.tool とセッション tool_class の適合。tool 空なら任意可。"""
    if not block_tool or not str(block_tool).strip():
        return True
    return str(block_tool).strip().lower() == span_tool.strip().lower()


def _best_session_label(
    block: Block,
    spans: Sequence[SessionSpan],
) -> str | None:
    """AI ブロックに重なる最良スパンの label。無ければ None。"""
    if not spans or not block.ai:
        return None
    thr = min(2.0, float(block.minutes) * 0.5)
    best: tuple[float, str] | None = None
    for sp in spans:
        if not _tool_class_matches(block.tool, sp.tool_class):
            continue
        ov = _overlap_minutes(block.start, block.end, sp.start, sp.end)
        if ov < thr:
            continue
        if best is None or ov > best[0]:
            best = (ov, sp.label)
    return best[1] if best else None


def render_markdown(
    summary: DailySummary,
    tz: tzinfo,
    min_block_minutes: float = 3.0,
    max_timeline_rows: int = 60,
    input_stats: InputStats | None = None,
    session_spans: Sequence[SessionSpan] | None = None,
    screen_fills: Mapping[str, str] | None = None,
) -> str:
    """デイリーノートに埋め込むアクティビティログのMarkdownを生成する。

    screen_fills: block_key → redact 済み画面テキスト要約（未突合 AI のみ）。
    """
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

    # タイムライン（主要ブロック + 細切れ1時間バケット）— 件数は実配列から決定論
    total_blocks = len(summary.blocks)
    eligible = [b for b in summary.blocks if b.minutes >= min_block_minutes]
    under_blocks = [b for b in summary.blocks if b.minutes < min_block_minutes]
    eligible_blocks = len(eligible)
    min_label = (
        f"{int(min_block_minutes)}分"
        if float(min_block_minutes).is_integer()
        else f"{min_block_minutes}分"
    )
    under_lines = _under_threshold_lines(
        summary.blocks,
        total_minutes=summary.total_minutes,
        min_block_minutes=min_block_minutes,
        min_label=min_label,
        tz=tz,
    )
    rows = eligible
    overflow_omitted = 0
    if eligible_blocks > max_timeline_rows:
        rows = sorted(eligible, key=lambda b: -b.minutes)[:max_timeline_rows]
        rows.sort(key=lambda b: b.start)
        overflow_omitted = eligible_blocks - max_timeline_rows
    shown_blocks = len(rows)

    # §A1: 細切れを1時間バケット行として時刻順にマージ
    frag_rows = _fragment_bucket_rows(under_blocks, tz=tz)
    # eligible 行は max_timeline_rows のみ。細切れは落とさない
    table_entries: list[tuple[datetime, str, float]] = []
    # (sort_key, markdown_row, minutes)
    spans = list(session_spans or ())
    for b in rows:
        title = b.titles[0] if b.titles else ""
        if len(title) > 60:
            title = title[:57] + "..."
        # §B1: AI ブロックのみセッション突合（非AI・細切れは不変）
        if b.ai:
            matched = _best_session_label(b, spans)
            if matched is not None:
                title = matched
            else:
                fill = None
                if screen_fills:
                    from .screenpipe_source import block_fill_key

                    fill = screen_fills.get(
                        block_fill_key(b.start, b.end, b.app)
                    )
                if fill:
                    title = f"（画面テキスト: {fill}）"
                else:
                    title = f"{title}（ログなし）" if title else "（ログなし）"
        md = (
            f"| {_fmt_time(b.start, tz)}-{_fmt_time(b.end, tz)} "
            f"| {_fmt_minutes(b.minutes)} "
            f"| {_markdown_table_cell(b.category)} "
            f"| {_markdown_table_cell(b.app)} "
            f"| {_markdown_table_cell(title)} |"
        )
        table_entries.append((b.start.astimezone(tz), md, float(b.minutes)))
    for sort_key, md, mins in frag_rows:
        table_entries.append((sort_key, md, mins))
    table_entries.sort(key=lambda x: x[0])

    if table_entries or under_lines:
        lines.append("### タイムライン")
        lines.append("")
        if rows:
            lines.append(f"{min_label}以上の画面ブロックを時刻順に表示。")
        lines.extend(under_lines)
        if overflow_omitted > 0:
            lines.append(
                f"全{total_blocks}件中、{min_label}以上は{eligible_blocks}件です。"
                f"長時間の上位{shown_blocks}件を時刻順に表示し、"
                f"対象内の{overflow_omitted}件を省略しています。"
            )
        if table_entries:
            lines.append("")
            lines.append("| 時刻 | 時間 | カテゴリ | アプリ | 内容 |")
            lines.append("| --- | ---: | --- | --- | --- |")
            table_sum = 0.0
            for _sk, md, mins in table_entries:
                lines.append(md)
                table_sum += mins
            lines.append("")
            # §A2: 被覆率
            if summary.total_minutes > 0:
                pct = int(round(table_sum / summary.total_minutes * 100))
                if abs(table_sum - summary.total_minutes) > 1.0 and pct == 100:
                    lines.append(
                        f"この表は合計 {_fmt_minutes(summary.total_minutes)} の "
                        f"{pct}%（表計 {_fmt_minutes(table_sum)}）を説明しています。"
                    )
                else:
                    lines.append(
                        f"この表は合計 {_fmt_minutes(summary.total_minutes)} の "
                        f"{pct}% を説明しています。"
                    )

    return "\n".join(lines)


def _fragment_bucket_rows(
    under: list[Block],
    *,
    tz: tzinfo,
) -> list[tuple[datetime, str, float]]:
    """細切れブロックを1時間バケットの表行へ。戻り値: (sort_key, md行, 分数)。"""
    if not under:
        return []
    # hour_key -> (minutes, count, category minutes)
    buckets: dict[tuple[date, int], dict] = {}
    for block in under:
        local = block.start.astimezone(tz)
        key = (local.date(), local.hour)
        bucket = buckets.setdefault(
            key, {"minutes": 0.0, "count": 0, "cats": defaultdict(float)}
        )
        bucket["minutes"] += float(block.minutes)
        bucket["count"] += 1
        bucket["cats"][block.category] += float(block.minutes)

    out: list[tuple[datetime, str, float]] = []
    for (d, hour), data in sorted(buckets.items()):
        start = datetime(d.year, d.month, d.day, hour, 0, tzinfo=tz)
        end_label = f"{hour:02d}:59"
        start_label = f"{hour:02d}:00"
        cats_sorted = sorted(
            data["cats"].items(), key=lambda x: (-x[1], x[0])
        )
        top = cats_sorted[:3]
        bits = [
            f"{cat} {_fmt_under_threshold_minutes(mins)}"
            for cat, mins in top
        ]
        if len(cats_sorted) > 3:
            bits.append("ほか")
        content = " / ".join(bits) + f"（{data['count']}件）"
        md = (
            f"| {start_label}-{end_label} "
            f"| {_fmt_minutes(data['minutes'])} "
            f"| 細切れ | — | {_markdown_table_cell(content)} |"
        )
        out.append((start, md, float(data["minutes"])))
    return out
