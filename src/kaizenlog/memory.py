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
# x/X = done, - = skipped (Obsidian cancelled), space = open
_CHECKBOX_RE = re.compile(r"^(\s*- \[)([ xX\-])(\]\s*)(.*)$")
_SKIP_REASON_RE = re.compile(r"[｜|]\s*理由\s*[:：]\s*(.+)$")

# 消化率が低いときの適応投与（プロンプト経由のソフト制御 + evidence max_actions）
_DOSING_MIN_PROPOSED = 6
_DOSING_DONE_RATE = 0.4  # 未満 → 1件
_DOSING_MID_RATE = 0.6  # 0.4〜0.6 → 2件、それ以上 → 3件
# 実行済みPASS率・消化率が高く「一段挑戦」してよい閾値（_DOSING の対）
_THRIVING_DONE_RATE = 0.7
_THRIVING_PASS_RATE = 0.6
# PASS 難易度較正（実行済みPASS率・判定3件以上）
_CALIBRATE_LOW = 0.30
_CALIBRATE_HIGH = 0.85
_STATS_WINDOW_DAYS = 14
# 📌 転記・done 検出の走査幅（提案日 target-N 〜 target-1）
ACTIONS_HANDOFF_DAYS = 7


@dataclass
class MemoryEntry:
    id: str
    date: str  # 提案日 YYYY-MM-DD
    action: str
    status: str = "proposed"  # proposed | done | superseded | skipped
    done_date: str | None = None
    # 翌日 generate による PASS 機械判定（旧 JSONL には無い → None）
    verdict: str | None = None  # pass | fail
    verdict_value: float | None = None
    verdict_date: str | None = None  # 判定日 YYYY-MM-DD
    skip_reason: str | None = None  # status=skipped の理由（旧 JSONL は欠落 → None）


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
        skip_reason = d.get("skip_reason")
        if skip_reason is not None:
            skip_reason = str(skip_reason) or None
        entries[d["id"]] = MemoryEntry(
            id=d["id"],
            date=d.get("date", ""),
            action=d.get("action", ""),
            status=d.get("status", "proposed"),
            done_date=d.get("done_date"),
            verdict=d.get("verdict"),
            verdict_value=verdict_value,
            verdict_date=d.get("verdict_date"),
            skip_reason=skip_reason,
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
    """ノート内の `- [x]` → done、`- [-]` → skipped を検出し差分エントリを返す。"""
    updated: list[MemoryEntry] = []
    by_id = {e.id: e for e in entries}
    for line in note_content.splitlines():
        m = _CHECKBOX_RE.match(line)
        if not m:
            continue
        mark = m.group(2)
        rest = m.group(4)
        id_match = ID_PATTERN.search(rest)
        if not id_match:
            continue
        entry = by_id.get(id_match.group(0))
        if not entry:
            continue
        if mark in ("x", "X"):
            if entry.status == "done":
                continue
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
                    skip_reason=None,
                )
            )
        elif mark == "-":
            if entry.status == "skipped":
                continue
            reason = None
            rm = _SKIP_REASON_RE.search(rest)
            if rm:
                reason = rm.group(1).strip() or None
            updated.append(
                MemoryEntry(
                    id=entry.id,
                    date=entry.date,
                    action=entry.action,
                    status="skipped",
                    done_date=entry.done_date,
                    verdict=entry.verdict,
                    verdict_value=entry.verdict_value,
                    verdict_date=entry.verdict_date,
                    skip_reason=reason,
                )
            )
    return updated


@dataclass(frozen=True)
class ActionStats:
    """提案の消化率・実行済みPASS率（北極星指標）の集計結果。

    pass_rate は **実行済みPASS率**（done_passed / done_judged）。
    未実行のまま PASS した件数は undone_passed で別掲する。
    proposed は skipped を分母から除外した件数。
    """

    window_days: int
    proposed: int  # 窓内 proposed+done（skipped / superseded 除外）
    done: int  # うち status == "done"
    judged: int  # うち verdict が pass/fail（done+undone）
    passed: int  # うち verdict == "pass"（全体・互換用）
    done_judged: int = 0
    done_passed: int = 0
    undone_judged: int = 0
    undone_passed: int = 0
    skipped: int = 0  # 窓内 skipped（分母外）

    @property
    def done_rate(self) -> float | None:
        if self.proposed == 0:
            return None
        return self.done / self.proposed

    @property
    def pass_rate(self) -> float | None:
        """実行済みPASS率（主指標）。"""
        if self.done_judged == 0:
            return None
        return self.done_passed / self.done_judged


def _execution_aligned_verdict(entry: MemoryEntry) -> bool:
    """判定が「実行後」に帰属するか（実行済みPASS率の層別用）。

    verdict は status=proposed の夜間判定で付き、後日チェックを付けても保持する
    （判定を失わない設計）。ただし verdict_date < done_date のエントリは
    「本人が動く前の測定で PASS した後に実行された」ケースで、done_passed に
    入れると pass_rate / 較正指示が過大になる。

    再判定トリガ化は意味論が複雑になるため見送り、ここでは層別のみ行う:
    - verdict_date >= done_date（または日付欠落のレガシー行）→ 実行済み側
    - verdict_date < done_date → 未実行での達成側
    """
    if entry.status != "done":
        return False
    if not entry.verdict_date or not entry.done_date:
        # 旧 JSONL や done_date 未記録は従来どおり実行済み側
        return True
    return entry.verdict_date >= entry.done_date


def compute_action_stats(
    entries: list[MemoryEntry], today: date, window_days: int = _STATS_WINDOW_DAYS
) -> ActionStats:
    """提案日が today-window 〜 today-1 のエントリを集計する。

    当日提案は実行機会がないため除外。skipped は分母から除外。
    done かつ判定済みでも verdict_date < done_date なら undone_* 側に層別する。
    """
    start = (today - timedelta(days=window_days)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    proposed = done = judged = passed = 0
    done_judged = done_passed = undone_judged = undone_passed = 0
    skipped = 0
    for e in entries:
        if not e.date or not _is_iso_date(e.date):
            continue
        if not (start <= e.date <= end):
            continue
        if e.status == "skipped":
            skipped += 1
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
            # 実行後に帰属する判定のみ done_*（それ以外は未実行達成側）
            if e.status == "done" and _execution_aligned_verdict(e):
                done_judged += 1
                if e.verdict == "pass":
                    done_passed += 1
            else:
                undone_judged += 1
                if e.verdict == "pass":
                    undone_passed += 1
    return ActionStats(
        window_days=window_days,
        proposed=proposed,
        done=done,
        judged=judged,
        passed=passed,
        done_judged=done_judged,
        done_passed=done_passed,
        undone_judged=undone_judged,
        undone_passed=undone_passed,
        skipped=skipped,
    )


def dosing_max_actions(stats: ActionStats) -> int:
    """消化率帯から決定論の max_actions（1〜3）。"""
    if (
        stats.proposed >= _DOSING_MIN_PROPOSED
        and stats.done_rate is not None
        and stats.done_rate < _DOSING_DONE_RATE
    ):
        return 1
    if (
        stats.proposed >= _DOSING_MIN_PROPOSED
        and stats.done_rate is not None
        and stats.done_rate < _DOSING_MID_RATE
    ):
        return 2
    return 3


@dataclass(frozen=True)
class Streaks:
    """消化ストリーク。

    消化ストリーク = 提案が1件以上あった日のうち、少なくとも1件を done にした
    連続日数（current / best）。提案が無い日・skipped のみの日はカウント対象外
    として連続を切らない（「記録しなかった日」でストリークを壊さないため）。
    """

    current: int
    best: int
    # 昨日は提案ありだが未消化（今日の再スタート表示用）
    broken_yesterday: bool = False


def compute_streaks(entries: list[MemoryEntry], today: date) -> Streaks:
    """消化ストリーク（current / best）を計算する。"""
    today_iso = today.isoformat()
    # 提案日ごとの status 集合
    by_day: dict[str, set[str]] = {}
    for e in entries:
        if not e.date or not _is_iso_date(e.date):
            continue
        # 破損 JSONL の遠未来 date（例 9999-01-01）を除外。
        # 旧実装は end=max(days_sorted[-1], today) の全日走査のため 1 行で数秒化していた。
        if e.date > today_iso:
            continue
        if e.status == "superseded":
            continue
        by_day.setdefault(e.date, set()).add(e.status)

    def day_outcome(d: date) -> str | None:
        """'done' = 消化あり / 'miss' = 提案あり未消化 / None = 対象外日。"""
        statuses = by_day.get(d.isoformat())
        if not statuses:
            return None
        # skipped のみ → 対象外（連続を切らない）
        non_skip = statuses - {"skipped"}
        if not non_skip:
            return None
        if "done" in statuses:
            return "done"
        # proposed のみ（未消化）
        return "miss"

    # current: today から遡って done の連続（miss で切る、None はスキップ）
    current = 0
    d = today
    for _ in range(400):
        outcome = day_outcome(d)
        if outcome is None:
            d -= timedelta(days=1)
            continue
        if outcome == "done":
            current += 1
            d -= timedelta(days=1)
            continue
        break

    # best: 提案があった日付のみ走査（空カレンダー日は outcome=None と同義で連続を切らない）
    if by_day:
        days_sorted = sorted(date.fromisoformat(s) for s in by_day)
        best = 0
        run = 0
        for day in days_sorted:
            outcome = day_outcome(day)
            if outcome is None:
                continue
            if outcome == "done":
                run += 1
                best = max(best, run)
            else:
                run = 0
    else:
        best = 0
    best = max(best, current)

    yday = today - timedelta(days=1)
    broken = day_outcome(yday) == "miss"
    return Streaks(current=current, best=best, broken_yesterday=broken)


def metric_pass_rates(
    entries: list[MemoryEntry],
    today: date,
    *,
    window_days: int = 30,
    min_judged: int = 3,
) -> list[tuple[str, int, int]]:
    """指標別の実行済みPASS/判定数（直近 window_days・判定 min_judged 以上）。

    戻り値: (metric, done_passed, done_judged) を判定数降順。
    verdict_date < done_date の事前オートPASSは compute_action_stats と同じく除外。
    """
    # 遅延 import（verdict との循環回避）
    from .verdict import parse_pass_condition

    start = (today - timedelta(days=window_days)).isoformat()
    end = today.isoformat()
    # metric -> [passed, judged]
    tallies: dict[str, list[int]] = {}
    for e in entries:
        if e.status != "done":
            continue
        if e.verdict not in ("pass", "fail"):
            continue
        # 実行前判定は実行済みPASS率に混ぜない（§L1 と同層別）
        if not _execution_aligned_verdict(e):
            continue
        if not e.date or not (start <= e.date <= end):
            continue
        parsed = parse_pass_condition(e.action)
        if not parsed:
            continue
        metric, _op, _t = parsed
        bucket = tallies.setdefault(metric, [0, 0])
        bucket[1] += 1
        if e.verdict == "pass":
            bucket[0] += 1
    rows = [
        (m, p, j)
        for m, (p, j) in tallies.items()
        if j >= min_judged
    ]
    rows.sort(key=lambda r: (-r[2], r[0]))
    return rows


# 2連続FAIL較正の判定窓（verdict_date 基準）
_CONSECUTIVE_FAIL_WINDOW_DAYS = 30


def _consecutive_metric_fails(
    entries: list[MemoryEntry], today: date, *, n: int = 2
) -> list[str]:
    """同一指標で直近 n 連続の実行済みFAILがあればその指標名を返す。

    判定日（verdict_date 優先）が today から _CONSECUTIVE_FAIL_WINDOW_DAYS 日以内
    のものだけを対象にする。窓外の古い連続FAILを永久に較正指示へ出さないため。
    """
    from .verdict import parse_pass_condition

    window_start = (today - timedelta(days=_CONSECUTIVE_FAIL_WINDOW_DAYS)).isoformat()
    today_iso = today.isoformat()

    def sort_key(e: MemoryEntry) -> str:
        return e.verdict_date or e.date or ""

    # 新しい判定から（verdict_date 優先、なければ date）。窓内のみ。
    judged = [
        e
        for e in entries
        if e.status == "done"
        and e.verdict in ("pass", "fail")
        and window_start <= sort_key(e) <= today_iso
    ]

    judged.sort(key=sort_key, reverse=True)
    by_metric: dict[str, list[str]] = {}
    for e in judged:
        parsed = parse_pass_condition(e.action)
        if not parsed:
            continue
        metric = parsed[0]
        by_metric.setdefault(metric, []).append(e.verdict or "")
    out: list[str] = []
    for metric, verdicts in by_metric.items():
        if len(verdicts) >= n and all(v == "fail" for v in verdicts[:n]):
            out.append(metric)
    return out


def consecutive_fail_actions(
    entries: list[MemoryEntry], today: date, *, n: int = 2
) -> list[str]:
    """同一指標で直近 n 連続FAILの表示行を返す（公開ラッパー）。

    形式: \"N日連続FAIL: KZN-... (metric 条件)\"
    無い場合は空リスト。
    """
    from .verdict import parse_pass_condition

    window_start = (today - timedelta(days=_CONSECUTIVE_FAIL_WINDOW_DAYS)).isoformat()
    today_iso = today.isoformat()

    def sort_key(e: MemoryEntry) -> str:
        return e.verdict_date or e.date or ""

    judged = [
        e
        for e in entries
        if e.status == "done"
        and e.verdict in ("pass", "fail")
        and window_start <= sort_key(e) <= today_iso
    ]
    judged.sort(key=sort_key, reverse=True)
    by_metric: dict[str, list[MemoryEntry]] = {}
    for e in judged:
        parsed = parse_pass_condition(e.action)
        if not parsed:
            continue
        metric = parsed[0]
        by_metric.setdefault(metric, []).append(e)
    out: list[str] = []
    for metric, ents in by_metric.items():
        if len(ents) >= n and all((e.verdict or "") == "fail" for e in ents[:n]):
            latest = ents[0]
            cond = (latest.action or metric).strip()
            out.append(f"{n}日連続FAIL: {latest.id} ({cond})")
    return out


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


def render_action_stats_line(
    stats: ActionStats, *, streaks: Streaks | None = None
) -> str:
    """status コマンド用の1行サマリ。主指標は実行済みPASS率。"""
    label = f"📈 Kaizen実績（直近{stats.window_days}日）"
    if stats.proposed == 0 and stats.skipped == 0:
        return f"{label}: まだ提案がありません"
    skip_part = f" / スキップ {stats.skipped}件" if stats.skipped else ""
    undone_part = (
        f"（未実行のままPASS到達 {stats.undone_passed}件："
        f"チェックなしで指標が目標値に達した提案）"
        if stats.undone_passed > 0
        else ""
    )
    streak_part = ""
    if streaks is not None and (streaks.current > 0 or streaks.best > 0):
        streak_part = f" / 🔥{streaks.current}日（最長{streaks.best}）"
    pass_part = f"実行済みPASS {stats.done_passed}件"
    if stats.pass_rate is not None:
        pass_part += f"（{_pct_label(stats.pass_rate)}）"
    return (
        f"{label}: 提案 {stats.proposed}件 / 消化 {stats.done}件"
        f"（{_pct_label(stats.done_rate)}）{skip_part}"
        f" / {pass_part}{undone_part}{streak_part}"
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
        skip_reason=None,
    )


def mark_entry_skipped(
    entry: MemoryEntry, *, reason: str | None = None
) -> MemoryEntry:
    """status=skipped の差分エントリ。"""
    return MemoryEntry(
        id=entry.id,
        date=entry.date,
        action=entry.action,
        status="skipped",
        done_date=entry.done_date,
        verdict=entry.verdict,
        verdict_value=entry.verdict_value,
        verdict_date=entry.verdict_date,
        skip_reason=(reason or "").strip() or None,
    )


def _execution_label(entry: MemoryEntry) -> str:
    if entry.status == "done":
        return "実行済み"
    if entry.status == "skipped":
        return "スキップ"
    return "未実行"


def _verdict_block_line(entry: MemoryEntry) -> str:
    from .verdict import parse_pass_condition

    icon = "✅PASS" if entry.verdict == "pass" else "❌FAIL"
    label = _execution_label(entry)
    action = " ".join(entry.action.split())[:60]
    cond = ""
    parsed = parse_pass_condition(entry.action)
    if parsed:
        metric, op, target = parsed
        cond = f"{metric} {op} {target:g}"
    else:
        cond = "（自由文）"
    measured = (
        f"{entry.verdict_value:g}" if entry.verdict_value is not None else "?"
    )
    return (
        f"- {entry.id} [{icon}] {label} ｜ {action} ｜ "
        f"PASS条件: {cond} ｜ 実測: {measured}"
    )


def summarize_for_prompt(
    entries: list[MemoryEntry], today: date, max_items: int = 10
) -> str:
    """LLMに渡す「過去の提案の記録」を組み立てる。無ければ空文字。

    先頭に消化率/実行済みPASS率の実績・判定還流・スキップ・適応投与/スライブ指示。
    """
    if not entries:
        return ""
    stats = compute_action_stats(entries, today)
    lines: list[str] = [
        f"## 提案の実績（直近{stats.window_days}日）",
        (
            f"提案{stats.proposed}件 / 消化率{_pct_label(stats.done_rate)}"
            + (f" / スキップ{stats.skipped}件" if stats.skipped else "")
            + f" / 実行済みPASS率{_pct_label(stats.pass_rate)}"
            + (
                f"（未実行のままPASS到達{stats.undone_passed}件）"
                if stats.undone_passed
                else ""
            )
        ),
    ]
    if stats.undone_passed >= 2:
        lines.append(
            f"⚠️ 行動せずに達成された条件が {stats.undone_passed} 件ある。"
            "目標が緩いか、指標が行動と無関係の可能性を検討する。"
        )
    # 適応投与: システムが件数を制限する前提の理由説明（決定論は evidence.max_actions）
    if (
        stats.proposed >= _DOSING_MIN_PROPOSED
        and stats.done_rate is not None
        and stats.done_rate < _DOSING_DONE_RATE
    ):
        lines.append(
            "⚠️ 消化率が低いため、システムが件数を1件に制限する。"
            "「今日の改善提案」と「明日の最小アクション」は最も小さく始められる1件にすること。"
        )
    elif (
        stats.proposed >= _DOSING_MIN_PROPOSED
        and stats.done_rate is not None
        and stats.done_rate < _DOSING_MID_RATE
    ):
        lines.append(
            "⚠️ 消化率が中程度のため、システムが件数を最大2件に制限する。"
        )
    # スライブ: 実行済みPASS率と消化率が高い
    if (
        stats.done_rate is not None
        and stats.pass_rate is not None
        and stats.done_rate >= _THRIVING_DONE_RATE
        and stats.pass_rate >= _THRIVING_PASS_RATE
        and stats.done_judged >= 1
    ):
        lines.append(
            "✅ 現状の負荷は適正（実行済みPASS率・消化率とも高い）。"
            "維持を明確に承認し、1件だけ一段挑戦的な提案をしてよい。"
        )
    # PASS 難易度較正（実行済みPASS率・判定3件以上）
    if stats.done_judged >= 3 and stats.pass_rate is not None:
        if stats.pass_rate < _CALIBRATE_LOW:
            lines.append(
                "📐 較正: 実行済みPASS率が低い。PASS条件を一段緩める（推奨帯の上限側）。"
            )
        elif stats.pass_rate > _CALIBRATE_HIGH:
            lines.append(
                "📐 較正: 実行済みPASS率が高い。一段挑戦的にしてよい（推奨帯の下限側）。"
            )
    for metric in _consecutive_metric_fails(entries, today, n=2):
        lines.append(
            f"📐 較正: 指標 {metric} が2連続FAIL。"
            "その指標の刻み幅を半分にするか、指標を変える。"
        )

    # 直近の判定（最大3日・新しい順）
    verdict_lines: list[str] = []
    for delta in range(1, 4):
        d = (today - timedelta(days=delta)).isoformat()
        day_entries = [
            e
            for e in entries
            if e.verdict in ("pass", "fail") and e.verdict_date == d
        ]
        day_entries.sort(key=lambda e: e.id, reverse=True)
        for e in day_entries:
            verdict_lines.append(_verdict_block_line(e))
    if verdict_lines:
        lines.append("## 直近の判定（最大3日・新しい順）")
        lines.extend(verdict_lines)

    # 指標別PASS実績
    mpr = metric_pass_rates(entries, today, window_days=30, min_judged=3)
    if mpr:
        lines.append("## 指標別PASS実績（直近30日）")
        for metric, passed, judged in mpr[:6]:
            trend = "✅傾向" if passed * 2 >= judged else "❌傾向"
            lines.append(f"- {metric} {passed}/{judged} {trend}")

    d30 = (today - timedelta(days=30)).isoformat()
    d7 = (today - timedelta(days=7)).isoformat()

    open_actions = [e for e in entries if e.status == "proposed" and e.date >= d30]
    recent_done = [e for e in entries if e.status == "done" and (e.done_date or "") >= d7]
    skipped = [
        e
        for e in entries
        if e.status == "skipped" and e.date >= d30
    ]
    skipped.sort(key=lambda e: e.id, reverse=True)

    if open_actions:
        lines.append(
            "## 未完了のアクション（再提案しない。"
            "例外: 実行済みFAILはより小さい一歩に分割して「（継続）」として再提案してよい）"
        )
        for e in open_actions[-max_items:]:
            lines.append(f"- {e.id}（{e.date}提案）: {e.action}")
    if recent_done:
        lines.append("## 直近7日で完了したアクション（蒸し返さない）")
        for e in recent_done[-max_items:]:
            lines.append(f"- {e.id}: {e.action}")
    if skipped:
        lines.append("## スキップされた提案（同種を繰り返さない）")
        for e in skipped[:5]:
            reason = e.skip_reason or "（理由なし）"
            lines.append(f"- {e.id}: {e.action[:80]} ｜ 理由: {reason}")
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
    streaks = compute_streaks(entries, target_day)
    if streaks.current >= 2:
        lines.append(f"🔥 連続{streaks.current}日")
    elif streaks.broken_yesterday and streaks.best > 0:
        lines.append(f"今日から再スタート（過去最長 {streaks.best}日）")
    if stats.proposed > 0 or stats.skipped > 0:
        skip_part = f" / スキップ {stats.skipped}件" if stats.skipped else ""
        undone_part = (
            f"（未実行のままPASS到達 {stats.undone_passed}件："
            f"チェックなしで指標が目標値に達した提案）"
            if stats.undone_passed
            else ""
        )
        pass_part = f"実行済みPASS {stats.done_passed}件"
        if stats.pass_rate is not None:
            pass_part += f"（{_pct_label(stats.pass_rate)}）"
        # 低調期の保護: 悪い消化率%の常時提示が記録行動を止める副作用への対策
        if (
            stats.done_rate is not None
            and stats.done_rate < _DOSING_DONE_RATE
            and stats.proposed >= _DOSING_MIN_PROPOSED
        ):
            lines.append(
                f"今週の消化 {stats.done}件"
                f"（提案 {stats.proposed}件）{skip_part}"
                f" / {pass_part}{undone_part}"
            )
        else:
            lines.append(
                f"直近{stats.window_days}日: 消化率 {_pct_label(stats.done_rate)}"
                f"（{stats.proposed}件中{stats.done}件）{skip_part}"
                f" / {pass_part}{undone_part}"
            )

    def _action_line(e: MemoryEntry, mark: str) -> str:
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
        return f"- [{mark}] {e.id}: {e.action}（{tag}）"

    # 判定✅かつ未チェックは未完了リストから分離
    pass_achieved = [
        e
        for e in buckets.recent
        if e.verdict == "pass" and e.id not in checked_ids
    ]
    still_open = [
        e
        for e in buckets.recent
        if not (e.verdict == "pass" and e.id not in checked_ids)
    ]
    # 新しい提案から最大3件（決定論。優先度推定ではない）
    shown = still_open[:TODAY_CANDIDATE_CAP]
    for e in shown:
        mark = "x" if e.id in checked_ids else " "
        lines.append(_action_line(e, mark))

    if pass_achieved:
        lines.append("### ☑ 指標は達成済み（習慣化するならチェック）")
        for e in pass_achieved[:TODAY_CANDIDATE_CAP]:
            lines.append(_action_line(e, " "))
        rest_achieved = max(0, len(pass_achieved) - TODAY_CANDIDATE_CAP)
        if rest_achieved:
            # 表示上限超過分を無言で落とさない（全件は today --all）
            lines.append(f"ほか達成済み {rest_achieved}件")

    rest_recent = max(0, len(still_open) - len(shown))
    if rest_recent or buckets.stale or buckets.older or not shown:
        if not shown and not pass_achieved:
            hold = len(buckets.stale) + len(buckets.older)
            lines.append(f"今日の候補なし。保留 {hold}件")
        lines.append(
            f"ほか直近7日の未完了 {rest_recent}件"
            f" / 8〜30日前 {len(buckets.stale)}件"
            f" / 31日以上 {len(buckets.older)}件"
        )
        lines.append("全件表示: `kaizenlog today --all`")
    return "\n".join(lines)
