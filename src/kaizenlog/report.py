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
from .privacy_filter import is_private_block


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
    """細切れを表に出さず、3行サマリへ畳む（第48弾 §D1）。"""
    under = [block for block in blocks if block.minutes < min_block_minutes]
    if not under:
        return []

    under_minutes = sum(block.minutes for block in under)
    ratio = under_minutes / total_minutes * 100 if total_minutes > 0 else 0.0
    by_category: dict[str, float] = defaultdict(float)
    by_hour: dict[int, tuple[int, float]] = {}
    for block in under:
        by_category[block.category] += block.minutes
        hour = block.start.astimezone(tz).hour
        count, minutes = by_hour.get(hour, (0, 0.0))
        by_hour[hour] = (count + 1, minutes + block.minutes)
    categories = sorted(by_category.items(), key=lambda item: (-item[1], item[0]))[:4]
    cat_bits = " / ".join(
        f"{category}{_fmt_under_threshold_minutes(minutes)}"
        for category, minutes in categories
    )
    lines = [
        f"細切れ（{min_label}未満）{len(under)}件・"
        f"{_fmt_under_threshold_minutes(under_minutes)}"
        f"（合計の{ratio:.0f}%）。{cat_bits}。",
    ]
    hours = sorted(by_hour.items(), key=lambda item: (-item[1][1], item[0]))[:3]
    lines.append(
        "集中を妨げた時間帯: "
        + " / ".join(
            f"{hour}時台 {count}件{_fmt_under_threshold_minutes(minutes)}"
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
    project: str = ""  # 工数帰属用（未 redact の project 名）


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
                project=project,
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
    *,
    hide_private_titles: bool = True,
    stats_history: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """デイリーノートに埋め込むアクティビティログのMarkdownを生成する。

    screen_fills: block_key → redact 済み画面テキスト要約（未突合 AI のみ）。
    hide_private_titles: 私的タイトルを（私的・非表示）にする。
    stats_history: 基準線用（当日を除く直近）。任意。
    """
    from .baseline import baseline, format_with_baseline

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

    prior = list(stats_history or [])
    sw_text = f"{summary.context_switches}回"
    med_sw, lab_sw = baseline(
        prior, "context_switches", today_value=float(summary.context_switches)
    )
    sw_disp = format_with_baseline(sw_text, med_sw, lab_sw)
    lines.append(
        f"**合計アクティブ時間**: {_fmt_minutes(summary.total_minutes)}"
        f" / コンテキストスイッチ: {sw_disp}"
    )
    lines.append("")

    if input_stats is not None:
        fb_n = len(input_stats.focus_blocks)
        fb_text = f"{fb_n}回"
        med_fb, lab_fb = baseline(
            prior, "input.focus_blocks", today_value=float(fb_n)
        )
        fb_disp = format_with_baseline(fb_text, med_fb, lab_fb)
        lines.append(
            f"**集中ブロック**: {fb_disp} / "
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

    # サイト別（aw-watcher-web 導入時のみ）— 私的は1行に畳む
    if summary.by_site:
        lines.append("### 🌐 サイト別（watcher取得分・上位10）")
        lines.append("")
        lines.append("| サイト | 時間 |")
        lines.append("| --- | ---: |")
        private_site_mins = 0.0
        public_sites: list[tuple[str, float]] = []
        for site, minutes in sorted(summary.by_site.items(), key=lambda x: -x[1]):
            if hide_private_titles and is_private_block(
                category="ブラウジング", title=str(site), app=""
            ):
                private_site_mins += float(minutes)
            else:
                public_sites.append((str(site), float(minutes)))
        shown = 0
        for site, minutes in public_sites:
            if shown >= 10:
                break
            lines.append(
                f"| {_markdown_table_cell(site)} | {_fmt_site_minutes(minutes)} |"
            )
            shown += 1
        if private_site_mins > 0 and shown < 10:
            lines.append(
                f"| （私的） | {_fmt_site_minutes(private_site_mins)} |"
            )
        lines.append("")
        lines.append(
            "※ watcherが取得できた部分だけで、ブラウザ時間の完全な内訳ではありません。"
        )
        lines.append("")

    # AI作業の内訳は Activity Log から削り、AI作業の質へ統合（§D3）
    if summary.ai_tool_minutes:
        lines.append(
            "AI作業のツール別内訳は「🤖 AI作業の質」節を参照。"
        )
        lines.append("")

    # タイムライン（3分以上の実ブロックのみ。細切れは表前サマリへ）
    total_blocks = len(summary.blocks)
    eligible = [b for b in summary.blocks if b.minutes >= min_block_minutes]
    under_blocks = [b for b in summary.blocks if b.minutes < min_block_minutes]
    under_minutes_total = sum(float(b.minutes) for b in under_blocks)
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
    shown_ids: set[int] | None = None
    if eligible_blocks > max_timeline_rows:
        kept = sorted(eligible, key=lambda b: -b.minutes)[:max_timeline_rows]
        kept.sort(key=lambda b: b.start)
        rows = kept
        shown_ids = {id(b) for b in kept}
        overflow_omitted = eligible_blocks - max_timeline_rows
    shown_blocks = len(rows)

    # 細切れは表に入れない（§D1）。被覆説明は表計 + 細切れ分で行う。
    table_entries: list[tuple[datetime, str, float]] = []
    spans = list(session_spans or ())
    merge_items: list[tuple[datetime, str, object]] = []
    scan_blocks = (
        sorted(eligible, key=lambda b: b.start) if shown_ids is not None else rows
    )
    # 私的タイムライン行は集計1行へ畳むため先に蓄積
    private_timeline_mins = 0.0
    private_timeline_count = 0
    for b in scan_blocks:
        if shown_ids is not None and id(b) not in shown_ids:
            merge_items.append((b.start.astimezone(tz), "gap", None))
            continue
        raw_title = b.titles[0] if b.titles else ""
        private = False
        if hide_private_titles and is_private_block(
            category=b.category, title=raw_title, app=b.app
        ):
            private = True
        title = raw_title
        if len(title) > 60:
            title = title[:57] + "..."
        if private:
            private_timeline_mins += float(b.minutes)
            private_timeline_count += 1
            continue
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
        merge_items.append(
            (
                b.start.astimezone(tz),
                "block",
                (
                    b.category,
                    b.app,
                    title,
                    float(b.minutes),
                    b.start,
                    b.end,
                ),
            )
        )
    merge_items.sort(key=lambda x: x[0])
    table_entries = _collapse_timeline_with_boundaries(merge_items, tz)
    if private_timeline_mins > 0:
        # 表末尾に私的集計1行（時刻キーはソート後に付与するので任意）
        sort_key = (
            table_entries[-1][0] if table_entries else datetime(2099, 1, 1, tzinfo=tz)
        )
        table_entries.append(
            (
                sort_key,
                (
                    f"| — | {_fmt_minutes(private_timeline_mins)} "
                    f"| （私的） | — | "
                    f"集計{private_timeline_count}件 |"
                ),
                private_timeline_mins,
            )
        )

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
            # 被覆率: 表計 + 細切れ。100%超は重複計上を明示し頭打ち
            if summary.total_minutes > 0:
                covered = table_sum + under_minutes_total
                raw_pct = covered / summary.total_minutes * 100
                if raw_pct > 100.0:
                    overflow_m = covered - summary.total_minutes
                    lines.append(
                        f"この表は合計 {_fmt_minutes(summary.total_minutes)} の "
                        f"100% を説明しています"
                        f"（重複計上 {_fmt_minutes(overflow_m)} を含む）。"
                    )
                else:
                    pct = int(round(raw_pct))
                    if abs(covered - summary.total_minutes) > 1.0 and pct == 100:
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


def _collapse_timeline_with_boundaries(
    items: list[tuple[datetime, str, object]],
    tz: tzinfo,
) -> list[tuple[datetime, str, float]]:
    """時刻順マージ後の並びで圧縮する。

    kind=="frag" / kind=="gap" は圧縮境界（そこで連続を打ち切る）。
    frag は表行として出力し、gap（省略行）は出力せず境界のみ。
    kind=="block" のみ同一 category/app/content が3行以上連続なら1行に圧縮。
    """
    if not items:
        return []
    out: list[tuple[datetime, str, float]] = []
    i = 0
    n = len(items)
    while i < n:
        sk, kind, payload = items[i]
        if kind == "frag":
            md, mins = payload  # type: ignore[misc]
            out.append((sk, str(md), float(mins)))
            i += 1
            continue
        if kind == "gap":
            # 省略された eligible: 表には出さないが連続圧縮は打ち切る
            i += 1
            continue
        # block run until frag/gap or content change
        cat, app, content, mins, start, end = payload  # type: ignore[misc]
        j = i + 1
        total = float(mins)
        last_end = end
        while j < n and items[j][1] == "block":
            p = items[j][2]
            cat_j, app_j, content_j, mins_j, _st_j, en_j = p  # type: ignore[misc]
            if cat_j != cat or app_j != app or content_j != content:
                break
            total += float(mins_j)
            last_end = en_j
            j += 1
        count = j - i
        if count >= 3:
            md = (
                f"| {_fmt_time(start, tz)}-{_fmt_time(last_end, tz)} "
                f"| 計{_fmt_minutes(total)} ({count}回) "
                f"| {_markdown_table_cell(cat)} "
                f"| {_markdown_table_cell(app)} "
                f"| {_markdown_table_cell(content)} |"
            )
            out.append((sk, md, total))
        else:
            for k in range(i, j):
                sk_k = items[k][0]
                cat_k, app_k, content_k, mins_k, st_k, en_k = items[k][2]  # type: ignore[misc]
                md = (
                    f"| {_fmt_time(st_k, tz)}-{_fmt_time(en_k, tz)} "
                    f"| {_fmt_minutes(float(mins_k))} "
                    f"| {_markdown_table_cell(cat_k)} "
                    f"| {_markdown_table_cell(app_k)} "
                    f"| {_markdown_table_cell(content_k)} |"
                )
                out.append((sk_k, md, float(mins_k)))
        i = j
    return out


def _collapse_consecutive_timeline_rows(
    rows: list[tuple[datetime, str, str, str, float, datetime, datetime]],
    tz: tzinfo,
) -> list[tuple[datetime, str, float]]:
    """後方互換: block のみの列を圧縮（細切れ境界なし）。"""
    items = [
        (sk, "block", (cat, app, content, mins, start, end))
        for sk, cat, app, content, mins, start, end in rows
    ]
    return _collapse_timeline_with_boundaries(items, tz)


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
