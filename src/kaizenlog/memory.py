"""Kaizen Memory: 提案の記録と追跡。

改善提案を「言いっぱなし」にしないための記憶層。各アクションに安定ID
（KZN-YYYYMMDD-NNN）を付与し、JSONL（<vault>/Kaizen/Memory/suggestions.jsonl）に
記録する。翌日以降のデイリーノートのチェックボックス状態からdoneを検出し、
LLMには「未完了・提案済み・完了済み」の要約を渡して重複提案を防ぐ。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

MEMORY_FILE = "suggestions.jsonl"
ID_PATTERN = re.compile(r"KZN-(\d{8})-(\d{3,})")  # 1000件目以降は4桁になるため下限のみ固定
ACTION_SECTION = "### 明日試すこと"
LEGACY_ACTION_SECTION = "### 明日の最小アクション"
_CHECKBOX_RE = re.compile(r"^(\s*- \[)([ xX])(\]\s*)(.*)$")

# 消化率が低いときの適応投与（プロンプト経由のソフト制御）
_DOSING_MIN_PROPOSED = 6
_DOSING_DONE_RATE = 0.4
_STATS_WINDOW_DAYS = 14
# 📌 転記・done 検出の走査幅（提案日 target-N 〜 target-1）
ACTIONS_HANDOFF_DAYS = 7


@dataclass
class MemoryEntry:
    id: str
    date: str  # 提案日 YYYY-MM-DD
    action: str
    status: str = "proposed"  # proposed | done | superseded
    done_date: str | None = None
    # 翌日 generate による PASS 機械判定（旧 JSONL には無い → None）
    verdict: str | None = None  # pass | fail
    verdict_value: float | None = None
    verdict_date: str | None = None  # 判定日 YYYY-MM-DD


def _memory_file(memory_dir: Path) -> Path:
    return Path(memory_dir) / MEMORY_FILE


def load_entries(memory_dir: Path) -> list[MemoryEntry]:
    path = _memory_file(memory_dir)
    if not path.is_file():
        return []
    entries: dict[str, MemoryEntry] = {}
    # errors="replace": 追記が中断された不正バイトでadvise全体を落とさない
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict) or "id" not in d:
            continue
        # 同一IDの後勝ち（ステータス更新は追記で表現する）
        vv = d.get("verdict_value")
        try:
            verdict_value = float(vv) if vv is not None else None
        except (TypeError, ValueError):
            verdict_value = None
        entries[d["id"]] = MemoryEntry(
            id=d["id"],
            date=d.get("date", ""),
            action=d.get("action", ""),
            status=d.get("status", "proposed"),
            done_date=d.get("done_date"),
            verdict=d.get("verdict"),
            verdict_value=verdict_value,
            verdict_date=d.get("verdict_date"),
        )
    return sorted(entries.values(), key=lambda e: e.id)


def append_entries(memory_dir: Path, entries: list[MemoryEntry]) -> None:
    if not entries:
        return
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    with open(_memory_file(memory_dir), "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")


def next_id(existing: list[MemoryEntry], day: date, offset: int = 0) -> str:
    prefix = f"KZN-{day.strftime('%Y%m%d')}-"
    used = {
        int(e.id.rsplit("-", 1)[1])
        for e in existing
        if e.id.startswith(prefix)
    }
    n = 1 + offset
    while n in used:
        n += 1
    return f"{prefix}{n:03d}"


def assign_action_ids(
    advice_md: str, day: date, existing: list[MemoryEntry]
) -> tuple[str, list[MemoryEntry]]:
    """読者向け・旧形式のアクション欄にあるチェックボックス行へIDを付与する。

    LLMはIDを書かない約束なので、ID無しの行に KZN-YYYYMMDD-NNN を挿入し、
    新規エントリのリストを返す。既にIDがある行はそのまま。
    """
    lines = advice_md.splitlines()
    new_entries: list[MemoryEntry] = []
    in_section = False
    assigned = 0
    # 同日・同文のアクションは既存IDを再利用する（adviseの再実行で重複させない）
    same_day_actions = {
        e.action: e.id
        for e in existing
        if e.date == day.isoformat() and e.status == "proposed"
    }
    reusable_same_day = [
        e for e in existing
        if e.date == day.isoformat() and e.status == "proposed"
    ]
    used_ids: set[str] = set()
    # (line_index, checkbox match, action text) — ID 未付与の行
    pending: list[tuple[int, re.Match[str], str]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            in_section = (
                ACTION_SECTION.removeprefix("### ") in stripped
                or LEGACY_ACTION_SECTION.removeprefix("### ") in stripped
            )
        if not in_section:
            continue
        m = _CHECKBOX_RE.match(line)
        if not m:
            continue
        existing_id = ID_PATTERN.search(m.group(4))
        if existing_id:
            used_ids.add(existing_id.group(0))
            continue
        text = m.group(4).strip()
        if not text:
            continue
        pending.append((i, m, text))

    # Pass 1: 全文一致の再利用を先に確定（used_ids を食い合わない）
    remaining: list[tuple[int, re.Match[str], str]] = []
    for i, m, text in pending:
        if text in same_day_actions and same_day_actions[text] not in used_ids:
            reused_id = same_day_actions[text]
            used_ids.add(reused_id)
            lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{reused_id}: {text}"
            continue
        remaining.append((i, m, text))

    # Pass 2: 汎用再利用 → 新規採番
    for i, m, text in remaining:
        reusable = next(
            (entry for entry in reusable_same_day if entry.id not in used_ids),
            None,
        )
        if reusable is not None:
            used_ids.add(reusable.id)
            lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{reusable.id}: {text}"
            new_entries.append(
                MemoryEntry(id=reusable.id, date=day.isoformat(), action=text)
            )
            continue
        new_id = next_id(existing + new_entries, day, offset=assigned)
        assigned += 1
        used_ids.add(new_id)
        lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{new_id}: {text}"
        new_entries.append(
            MemoryEntry(id=new_id, date=day.isoformat(), action=text)
        )
    new_entries.extend(
        MemoryEntry(
            id=entry.id,
            date=entry.date,
            action=entry.action,
            status="superseded",
        )
        for entry in reusable_same_day
        if entry.id not in used_ids
    )
    return "\n".join(lines), new_entries


def update_statuses_from_note(
    note_content: str, entries: list[MemoryEntry], done_date: date
) -> list[MemoryEntry]:
    """ノート内の `- [x] KZN-...` を検出し、done化した差分エントリを返す。"""
    updated: list[MemoryEntry] = []
    by_id = {e.id: e for e in entries}
    for line in note_content.splitlines():
        m = _CHECKBOX_RE.match(line)
        if not m or m.group(2) not in ("x", "X"):
            continue
        id_match = ID_PATTERN.search(m.group(4))
        if not id_match:
            continue
        entry = by_id.get(id_match.group(0))
        if entry and entry.status != "done":
            # done 化しても verdict 系は消さない（判定結果を失わない）
            updated.append(
                MemoryEntry(
                    id=entry.id,
                    date=entry.date,
                    action=entry.action,
                    status="done",
                    done_date=done_date.isoformat(),
                    verdict=entry.verdict,
                    verdict_value=entry.verdict_value,
                    verdict_date=entry.verdict_date,
                )
            )
    return updated


@dataclass(frozen=True)
class ActionStats:
    """提案の消化率・PASS率（北極星指標）の集計結果。"""

    window_days: int
    proposed: int  # 窓内に提案されたアクション数
    done: int  # うち status == "done"
    judged: int  # うち verdict が pass/fail
    passed: int  # うち verdict == "pass"

    @property
    def done_rate(self) -> float | None:
        if self.proposed == 0:
            return None
        return self.done / self.proposed

    @property
    def pass_rate(self) -> float | None:
        if self.judged == 0:
            return None
        return self.passed / self.judged


def compute_action_stats(
    entries: list[MemoryEntry], today: date, window_days: int = _STATS_WINDOW_DAYS
) -> ActionStats:
    """提案日が today-window 〜 today-1 のエントリを集計する。

    当日提案は実行機会がないため除外。不正な date は無視する。
    """
    start = (today - timedelta(days=window_days)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    proposed = done = judged = passed = 0
    for e in entries:
        if not e.date or not _is_iso_date(e.date):
            continue
        if not (start <= e.date <= end):
            continue
        # 再 advise で撤回された行は分母・判定から除外
        if e.status not in ("proposed", "done"):
            continue
        proposed += 1
        if e.status == "done":
            done += 1
        if e.verdict in ("pass", "fail"):
            judged += 1
            if e.verdict == "pass":
                passed += 1
    return ActionStats(
        window_days=window_days,
        proposed=proposed,
        done=done,
        judged=judged,
        passed=passed,
    )


def _is_iso_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _pct_label(rate: float | None) -> str:
    """整数%表示。分母0は '-'。"""
    if rate is None:
        return "-"
    return f"{round(rate * 100)}%"


def render_action_stats_line(stats: ActionStats) -> str:
    """status コマンド用の1行サマリ。"""
    label = f"📈 Kaizen実績（直近{stats.window_days}日）"
    if stats.proposed == 0:
        return f"{label}: まだ提案がありません"
    return (
        f"{label}: 提案 {stats.proposed}件 / 消化 {stats.done}件"
        f"（{_pct_label(stats.done_rate)}）/ 自動判定 {stats.judged}件"
        f" / PASS {stats.passed}件（{_pct_label(stats.pass_rate)}）"
    )


def open_actions_in_window(
    entries: list[MemoryEntry],
    target_day: date,
    window_days: int = ACTIONS_HANDOFF_DAYS,
) -> list[MemoryEntry]:
    """today 一覧用: 提案日が target_day-window 〜 target_day（当日含む）の proposed。

    表示窓は当日の advise 直後に tonight の提案を消化できるよう当日を含む。
    消化率などの統計窓（compute_action_stats: target-window 〜 target-1）とは別物。
    """
    window_start = (target_day - timedelta(days=window_days)).isoformat()
    window_end = target_day.isoformat()
    open_entries = [
        e
        for e in entries
        if e.status == "proposed" and window_start <= e.date <= window_end
    ]
    open_entries.sort(key=lambda e: e.id, reverse=True)
    return open_entries


@dataclass(frozen=True)
class OpenActionBuckets:
    """未完了 proposed の表示用群分け（Memory は変更しない）。

    recent / stale / older はいずれも新しい提案（ID 降順）から並べる。
    """

    recent: list[MemoryEntry]
    stale: list[MemoryEntry]
    older: list[MemoryEntry]

    @property
    def total(self) -> int:
        return len(self.recent) + len(self.stale) + len(self.older)


# today 既定表示の候補件数（優先度推定ではなく、新しい文脈から最大 N 件）
TODAY_CANDIDATE_CAP = 3
# stale 窓の外側境界（target - 30 〜 target - 8）
STALE_LOOKBACK_DAYS = 30


def partition_open_actions(
    entries: list[MemoryEntry],
    target_day: date,
    *,
    recent_include_today: bool = True,
) -> OpenActionBuckets:
    """未完了を recent / stale / older に分ける（表示専用・Memory 非破壊）。

    - recent: recent_include_today なら target-7〜target（today と同じ8暦日）、
      そうでなければ target-7〜target-1（Obsidian 📌 と同じ7暦日）
    - stale: target-30 〜 recent 開始の前日（8〜30日前）
    - older: target-31 日以前
    """
    if recent_include_today:
        recent_start = (target_day - timedelta(days=ACTIONS_HANDOFF_DAYS)).isoformat()
        recent_end = target_day.isoformat()
    else:
        recent_start = (target_day - timedelta(days=ACTIONS_HANDOFF_DAYS)).isoformat()
        recent_end = (target_day - timedelta(days=1)).isoformat()
    stale_start = (target_day - timedelta(days=STALE_LOOKBACK_DAYS)).isoformat()
    # recent 開始の前日 = target - 8（include_today / 否 いずれも recent 開始は target-7）
    stale_end = (target_day - timedelta(days=ACTIONS_HANDOFF_DAYS + 1)).isoformat()

    recent: list[MemoryEntry] = []
    stale: list[MemoryEntry] = []
    older: list[MemoryEntry] = []
    for e in entries:
        if e.status != "proposed" or not e.date or not _is_iso_date(e.date):
            continue
        if recent_start <= e.date <= recent_end:
            recent.append(e)
        elif stale_start <= e.date <= stale_end:
            stale.append(e)
        elif e.date < stale_start:
            older.append(e)
    # 新しい提案から（決定論: ID 降順 = 日付+連番の新しい順）
    key = lambda e: e.id  # noqa: E731
    recent.sort(key=key, reverse=True)
    stale.sort(key=key, reverse=True)
    older.sort(key=key, reverse=True)
    return OpenActionBuckets(recent=recent, stale=stale, older=older)


def format_today_action_line(entry: MemoryEntry) -> str:
    """today 一覧の1行。ID は done へコピペできる完全形。"""
    try:
        d = date.fromisoformat(entry.date)
        md = f"{d.month}/{d.day}"
    except ValueError:
        md = entry.date
    if entry.verdict == "pass":
        v = "✅PASS"
    elif entry.verdict == "fail":
        v = "❌FAIL"
    else:
        v = "     "
    # 本文は1行に圧縮
    action = " ".join(entry.action.split())
    return f"{entry.id}  [{md}]  {v}  {action}"


def resolve_action_id(
    query: str, entries: list[MemoryEntry]
) -> MemoryEntry | list[MemoryEntry] | None:
    """done 用 ID 解決。

    完全一致を最優先。部分一致は proposed の ID 末尾サフィックスのみ。
    一意に定まらなければ候補リストを返し、呼び出し側が exit 1 する
    （誤爆消化を防ぐ）。
    """
    q = (query or "").strip()
    if not q:
        return None
    # 完全一致（status 問わず最新状態を entries から）
    exact = [e for e in entries if e.id == q]
    if exact:
        # load_entries は後勝ちなので1件想定
        return exact[-1]
    proposed = [e for e in entries if e.status == "proposed"]
    suffix_hits = [e for e in proposed if e.id.endswith(q)]
    if len(suffix_hits) == 1:
        return suffix_hits[0]
    if len(suffix_hits) > 1:
        return suffix_hits
    return None


def mark_entry_done(entry: MemoryEntry, done_date: date) -> MemoryEntry:
    """status=done / done_date を付けた差分エントリ（追記型後勝ち用）。"""
    return MemoryEntry(
        id=entry.id,
        date=entry.date,
        action=entry.action,
        status="done",
        done_date=done_date.isoformat(),
        verdict=entry.verdict,
        verdict_value=entry.verdict_value,
        verdict_date=entry.verdict_date,
    )


def summarize_for_prompt(
    entries: list[MemoryEntry], today: date, max_items: int = 10
) -> str:
    """LLMに渡す「過去の提案の記録」を組み立てる。無ければ空文字。

    先頭に消化率/PASS率の実績ブロックを置き、消化率が低いときは
    提案を1件に絞る指示（適応投与）を付ける。
    """
    if not entries:
        return ""
    stats = compute_action_stats(entries, today)
    lines: list[str] = [
        f"## 提案の実績（直近{stats.window_days}日）",
        (
            f"提案{stats.proposed}件 / 消化率{_pct_label(stats.done_rate)}"
            f" / 自動判定{stats.judged}件 / PASS率{_pct_label(stats.pass_rate)}"
        ),
    ]
    if (
        stats.proposed >= _DOSING_MIN_PROPOSED
        and stats.done_rate is not None
        and stats.done_rate < _DOSING_DONE_RATE
    ):
        lines.append(
            "⚠️ 消化率が低いため、今回は「今日の改善提案」と「明日の最小アクション」"
            "を1件だけにし、最も小さく始められるものを選ぶこと。"
        )

    d30 = (today - timedelta(days=30)).isoformat()
    d7 = (today - timedelta(days=7)).isoformat()

    open_actions = [e for e in entries if e.status == "proposed" and e.date >= d30]
    recent_done = [e for e in entries if e.status == "done" and (e.done_date or "") >= d7]

    if open_actions:
        lines.append("## 未完了のアクション（再提案しない。価値があれば「（継続）」と明示して1行のみ）")
        for e in open_actions[-max_items:]:
            lines.append(f"- {e.id}（{e.date}提案）: {e.action}")
    if recent_done:
        lines.append("## 直近7日で完了したアクション（蒸し返さない）")
        for e in recent_done[-max_items:]:
            lines.append(f"- {e.id}: {e.action}")
    return "\n".join(lines)


def render_actions_section(
    entries: list[MemoryEntry],
    target_day: date,
    note_content: str | None = None,
) -> str | None:
    """翌日ノート用「今日のアクション」転記 Markdown。

    対象は proposed かつ提案日が target_day-ACTIONS_HANDOFF_DAYS 〜 target_day-1。
    チェックボックスは新しい提案から最大 TODAY_CANDIDATE_CAP 件
    （優先度推定ではなく、現在の文脈に近い候補を表示する決定論ルール）。
    0件なら None（既存セクションは消さない。ただし stale/older のみある場合は
    件数案内セクションを返す）。
    note_content に同じ KZN の [x] があればチェック状態を保持する。
    """
    buckets = partition_open_actions(
        entries, target_day, recent_include_today=False
    )
    if buckets.total == 0:
        return None

    checked_ids: set[str] = set()
    if note_content:
        for line in note_content.splitlines():
            m = _CHECKBOX_RE.match(line)
            if not m or m.group(2) not in ("x", "X"):
                continue
            id_match = ID_PATTERN.search(m.group(4))
            if id_match:
                checked_ids.add(id_match.group(0))

    lines = [
        "## 📌 今日のアクション",
        "前日までの改善提案の未完了アクション。完了したらチェック",
    ]
    # 北極星指標をノート上でも見えるようにする（CLI status と揃える）
    stats = compute_action_stats(entries, target_day)
    if stats.proposed > 0:
        lines.append(
            f"直近{stats.window_days}日: 消化率 {_pct_label(stats.done_rate)}"
            f"（{stats.proposed}件中{stats.done}件）"
            f" / 自動判定 {stats.judged}件"
            f" / PASS率 {_pct_label(stats.pass_rate)}"
        )

    # 新しい提案から最大3件（決定論。優先度推定ではない）
    shown = buckets.recent[:TODAY_CANDIDATE_CAP]
    for e in shown:
        mark = "x" if e.id in checked_ids else " "
        try:
            d = date.fromisoformat(e.date)
            md = f"{d.month}/{d.day}"
        except ValueError:
            md = e.date
        if e.verdict:
            icon = "✅" if e.verdict == "pass" else "❌"
            val = f"{e.verdict_value:g}" if e.verdict_value is not None else "?"
            tag = f"{md}提案・判定 {icon} 実測{val}"
        else:
            tag = f"{md}提案"
        lines.append(f"- [{mark}] {e.id}: {e.action}（{tag}）")

    rest_recent = max(0, len(buckets.recent) - len(shown))
    if rest_recent or buckets.stale or buckets.older or not shown:
        if not shown:
            hold = len(buckets.stale) + len(buckets.older)
            lines.append(f"今日の候補なし。保留 {hold}件")
        lines.append(
            f"ほか直近7日の未完了 {rest_recent}件"
            f" / 8〜30日前 {len(buckets.stale)}件"
            f" / 31日以上 {len(buckets.older)}件"
        )
        lines.append("全件表示: `kaizenlog today --all`")
    return "\n".join(lines)
