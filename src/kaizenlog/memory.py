"""Kaizen Memory: 提案の記録と追跡。

改善提案を「言いっぱなし」にしないための記憶層。各アクションに安定ID
（KZN-YYYYMMDD-NNN）を付与し、JSONL（<vault>/Kaizen/Memory/suggestions.jsonl）に
記録する。翌日以降のデイリーノートのチェックボックス状態からdoneを検出し、
LLMには「未完了・提案済み・完了済み」の要約を渡して重複提案を防ぐ。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from numbers import Real
from pathlib import Path
from typing import Any

MEMORY_FILE = "suggestions.jsonl"
# 終端 status（寿命管理）。現役分母・📌・判定対象から除外する。
TERMINAL_STATUSES = frozenset({"unmeasurable", "graduated", "retired"})
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
    # proposed | done | superseded | skipped | unmeasurable | graduated | retired
    status: str = "proposed"
    done_date: str | None = None
    # 翌日 generate による PASS 機械判定（旧 JSONL には無い → None）
    verdict: str | None = None  # pass | fail
    verdict_value: float | None = None
    verdict_date: str | None = None  # 判定日 YYYY-MM-DD
    skip_reason: str | None = None  # status=skipped の理由（旧 JSONL は欠落 → None）
    verdict_stage: str = "confirmed"  # provisional | confirmed（旧 JSONL の既定は confirmed）
    # 寿命管理（旧 JSONL は欠落 → None）。終了扱いは達成を意味しない。
    closed_reason: str | None = None
    closed_date: str | None = None


def _memory_file(memory_dir: Path) -> Path:
    return Path(memory_dir) / MEMORY_FILE


def _normalize_verdict_stage(raw: object, *, key_present: bool) -> str:
    """判定 stage を後方互換かつ fail-closed に正規化する。"""
    if not key_present:
        return "confirmed"
    if raw in ("provisional", "confirmed"):
        return raw
    return "provisional"


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
        verdict_stage = _normalize_verdict_stage(
            d.get("verdict_stage"), key_present="verdict_stage" in d
        )
        closed_reason = d.get("closed_reason")
        if closed_reason is not None:
            closed_reason = str(closed_reason) or None
        closed_date = d.get("closed_date")
        if closed_date is not None:
            closed_date = str(closed_date) or None
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
            verdict_stage=verdict_stage,
            closed_reason=closed_reason,
            closed_date=closed_date,
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
        # §B6: 終端 status のみ再開不可（CLI と揃える）。skipped/superseded は再開可
        if entry.status in TERMINAL_STATUSES:
            continue
        if mark in ("x", "X"):
            if entry.status == "done":
                continue
            # done 化しても verdict 系は消さない（判定結果を失わない）
            updated.append(
                replace(
                    entry,
                    status="done",
                    done_date=done_date.isoformat(),
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
                replace(
                    entry,
                    status="skipped",
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
        if e.verdict in ("pass", "fail") and e.verdict_stage == "confirmed":
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


def backlog_generation_cap(stats: ActionStats) -> int:
    """未消化バックログ時の advise 件数上限（1 or 3）。

    dosing_max_actions（proposed≥6 ゲート）より早く絞る。
    display_cap の強制1条件と揃える（generation_cap / display_cap の同時超過を防ぐ）。
    - proposed≥1 かつ done==0 → 1
    - done_rate<0.4 かつ proposed≥3 → 1
    - それ以外 → 3（short_record / dosing と min 合成する側の責務）
    """
    if stats.proposed >= 1 and stats.done == 0:
        return 1
    if (
        stats.done_rate is not None
        and stats.done_rate < _DOSING_DONE_RATE
        and stats.proposed >= 3
    ):
        return 1
    return 3


# 📌 主面の表示上限ハードキャップ（generation_cap / TODAY_CANDIDATE_CAP とは別）
_DISPLAY_CAP_HARD_MAX = 3


def resolve_display_cap(
    stats: ActionStats,
    *,
    max_candidates: int | None = None,
) -> int:
    """📌 チェックボックス本文の表示上限（display_cap）。

    generation_cap（evidence.max_actions / dosing_max_actions）とは独立。
    - max_candidates > 0 ならそれを上限3で clamp（morning の1件指定など）
    - それ以外の既定は 1
    - 低消化時は強制1（dosing の proposed≥6 より早く絞る）:
      proposed≥1 かつ done==0、または done_rate<0.4 かつ proposed≥3
    """
    if max_candidates is not None and max_candidates > 0:
        base = min(_DISPLAY_CAP_HARD_MAX, int(max_candidates))
    else:
        base = 1
    # 未チェック続き・低消化: 主面は常に1件（「今日の1手」）
    if stats.proposed >= 1 and stats.done == 0:
        return 1
    if (
        stats.done_rate is not None
        and stats.done_rate < _DOSING_DONE_RATE
        and stats.proposed >= 3
    ):
        return 1
    return min(_DISPLAY_CAP_HARD_MAX, max(1, base))


def order_still_open_for_display(entries: Sequence[MemoryEntry]) -> list[MemoryEntry]:
    """still_open の表示順（台帳非破壊）。

    1. 実行可能優先（provisional でも confirmed-fail でもない）
    2. confirmed-fail
    3. provisional（集計中が1件枠を独占しない）
    同一帯内は入力順を維持（呼び出し側が ID 降順）。
    """
    preferred: list[MemoryEntry] = []
    confirmed_fail: list[MemoryEntry] = []
    provisional: list[MemoryEntry] = []
    for e in entries:
        if e.verdict_stage == "provisional":
            provisional.append(e)
        elif e.verdict == "fail" and e.verdict_stage == "confirmed":
            confirmed_fail.append(e)
        else:
            preferred.append(e)
    return preferred + confirmed_fail + provisional


def split_action_candidates(
    entries: Sequence[MemoryEntry],
    checked_ids: set[str],
) -> tuple[list[MemoryEntry], list[MemoryEntry]]:
    """実行候補と、判定済みPASSの観測候補を分離する。"""
    confirmed_pass = [
        e
        for e in entries
        if e.verdict == "pass"
        and e.verdict_stage == "confirmed"
    ]
    monitoring = [e for e in confirmed_pass if e.id not in checked_ids]
    actionable = [e for e in entries if e not in confirmed_pass]
    return order_still_open_for_display(actionable), monitoring


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
        # superseded と卒業3状態は連続を切らない（対象外）
        if e.status == "superseded" or e.status in TERMINAL_STATUSES:
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
        if (
            e.verdict not in ("pass", "fail")
            or e.verdict_stage != "confirmed"
        ):
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


def metric_behavior_rates(
    entries: list[MemoryEntry],
    today: date,
    *,
    window_days: int = 30,
    min_judged: int = 1,
) -> list[tuple[str, int, int]]:
    """指標別の挙動PASS/判定数（実行の有無は問わない・confirmed のみ）。

    対象: status in (proposed, done) かつ verdict in (pass, fail)
    かつ verdict_stage == confirmed。
    superseded/skipped/unmeasurable/graduated/retired と provisional は除外。
    戻り値: (metric, passed, judged) を判定数降順。
    """
    from .verdict import parse_pass_condition

    start = (today - timedelta(days=window_days)).isoformat()
    end = today.isoformat()
    tallies: dict[str, list[int]] = {}
    for e in entries:
        if e.status not in ("proposed", "done"):
            continue
        if (
            e.verdict not in ("pass", "fail")
            or e.verdict_stage != "confirmed"
        ):
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
        and e.verdict_stage == "confirmed"
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
        and e.verdict_stage == "confirmed"
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
    """status コマンド用の1行サマリ。数値は compute_action_stats と同じ。文言のみ平文。"""
    label = f"📈 Kaizen実績（直近{stats.window_days}日）"
    if stats.proposed == 0 and stats.skipped == 0:
        return f"{label}: まだ提案がありません"
    skip_part = f" / スキップ {stats.skipped}件" if stats.skipped else ""
    # §R2: 内部用語（消化 / 実行済みPASS / 未実行のままPASS到達）を出さない
    undone_part = (
        f" / うちチェックなしで指標達成 {stats.undone_passed}件"
        if stats.undone_passed > 0
        else ""
    )
    streak_part = ""
    if streaks is not None and (streaks.current > 0 or streaks.best > 0):
        streak_part = f" / 🔥{streaks.current}日（最長{streaks.best}）"
    achieved_part = f"チェック済みで指標達成 {stats.done_passed}件"
    if stats.pass_rate is not None:
        achieved_part += f"（{_pct_label(stats.pass_rate)}）"
    return (
        f"{label}: 提案 {stats.proposed}件 / チェック完了 {stats.done}件"
        f"（{_pct_label(stats.done_rate)}）{skip_part}"
        f" / {achieved_part}{undone_part}{streak_part}"
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


_ACTION_MINUTES_HINT_RE = re.compile(
    r"(?:上限|目安|以内|約)?\s*(\d+)\s*分|(?:（|\()(\d+)\s*秒(?:）|\))"
)


def _estimate_action_minutes_hint(action: str) -> str | None:
    """行動文から所要の目安を拾う（無ければ None）。"""
    text = action or ""
    m = _ACTION_MINUTES_HINT_RE.search(text)
    if not m:
        return None
    if m.group(1):
        return f"{int(m.group(1))}分"
    if m.group(2):
        sec = int(m.group(2))
        if sec < 60:
            return f"{sec}秒"
        return f"{max(1, round(sec / 60))}分"
    return None


def format_today_action_line(
    entry: MemoryEntry, *, reader_friendly: bool = True
) -> str:
    """today 一覧の1行。ID は done へコピペできる完全形。

    reader_friendly=True（既定）: ｜PASS: 以降を落とした平文（日誌📌とトーン揃え）。
    False: 台帳原文（デバッグ・機械構文確認用）。
    """
    try:
        d = date.fromisoformat(entry.date)
        md = f"{d.month}/{d.day}"
    except ValueError:
        md = entry.date
    if entry.verdict_stage == "provisional" and entry.verdict in ("pass", "fail"):
        v = "⏳暫定"
    elif entry.verdict == "pass":
        v = "✅PASS"
    elif entry.verdict == "fail":
        v = "❌FAIL"
    else:
        v = "     "
    # 本文は1行に圧縮（読者向けは機械 PASS を前面に出さない）
    if reader_friendly:
        action = " ".join(humanize_action_body(entry.action).split())
    else:
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
    return replace(
        entry,
        status="done",
        done_date=done_date.isoformat(),
        skip_reason=None,
    )


def mark_entry_skipped(
    entry: MemoryEntry, *, reason: str | None = None
) -> MemoryEntry:
    """status=skipped の差分エントリ。"""
    return replace(
        entry,
        status="skipped",
        skip_reason=(reason or "").strip() or None,
    )


def _execution_label(entry: MemoryEntry) -> str:
    if entry.status == "done":
        return "実行済み"
    if entry.status == "skipped":
        return "スキップ"
    if entry.status == "unmeasurable":
        return "判定不能で終了"
    if entry.status == "graduated":
        return "指標継続"
    if entry.status == "retired":
        return "期限切れ"
    return "未実行"


def graduate_entries(
    entries: list[MemoryEntry],
    today: date,
    *,
    stats_dir: Path,
    known_categories: set[str] | frozenset[str] | None = None,
) -> list[MemoryEntry]:
    """寿命管理: unmeasurable / graduated / retired の差分エントリを返す。

    評価順: unmeasurable → graduated → retired（同一実行で両方にしない）。
    追記型・冪等（既に終端なら出さない）。

    retired: age≥3 かつ proposed のまま・graduated 未達（§E）。
    closed_reason で未測定 / 未チェック達成 / 指標未達などを区別する。

    known_categories: category_minutes の偽0.0 卒業を防ぐ（backfill と同じ）。
    測定可能日は提案日より後かつ **当日未満**（当日は集計途中のため含めない）。
    """
    from .experiments import metric_from_stats, target_met
    from .stats import load_stats
    from .verdict import parse_pass_condition

    out: list[MemoryEntry] = []
    # 測定可能日探索用に十分な stats を1回読む
    stats_list = load_stats(stats_dir, days=45, end_day=today)
    stats_by_day = {
        str(s.get("day")): s
        for s in stats_list
        if isinstance(s, dict) and s.get("day")
    }

    for e in entries:
        if e.status != "proposed":
            continue
        try:
            prop = date.fromisoformat(e.date)
        except ValueError:
            continue
        age = (today - prop).days
        if age < 0:
            continue
        parsed = parse_pass_condition(e.action)

        # 自由文PASS（機械構文なし）は3日で unmeasurable
        if parsed is None:
            if age >= 3:
                out.append(
                    replace(
                        e,
                        status="unmeasurable",
                        closed_reason="no_machine_pass",
                        closed_date=today.isoformat(),
                    )
                )
            continue

        metric, op, target = parsed
        # graduated: 提案日より後・当日未満の測定可能日の直近2日が両方 PASS
        measurable: list[tuple[str, bool]] = []
        d = prop + timedelta(days=1)
        while d < today:  # 当日を含めない（provisional 原則と揃える）
            s = stats_by_day.get(d.isoformat())
            if s is not None:
                v = metric_from_stats(
                    metric, s, known_categories=known_categories
                )
                if v is not None:
                    measurable.append(
                        (d.isoformat(), target_met(float(v), op, float(target)))
                    )
            d += timedelta(days=1)
        if len(measurable) >= 2 and measurable[-1][1] and measurable[-2][1]:
            out.append(
                replace(
                    e,
                    status="graduated",
                    closed_reason="metric_sustained",
                    closed_date=today.isoformat(),
                )
            )
            continue

        # retired (§E): 3日以上 proposed のまま = 未チェック、かつ graduated 未達
        # closed_reason で「実行されなかった / 指標が動かない / チェックなし達成」を区別
        if age >= 3:
            any_pass = any(ok for _d, ok in measurable)
            any_fail = any(not ok for _d, ok in measurable)
            if not measurable:
                reason = "unchecked_no_measurement"
            elif any_pass and not any_fail:
                # 測定できた日はすべて PASS なのに未チェック → 行動と指標の因果が弱い
                reason = "unchecked_metric_ok_no_check"
            elif any_pass and any_fail:
                reason = "unchecked_metric_mixed"
            else:
                reason = "unchecked_metric_unmet"
            out.append(
                replace(
                    e,
                    status="retired",
                    closed_reason=reason,
                    closed_date=today.isoformat(),
                )
            )
    return out


def format_lifecycle_reader_notes(
    graduated: list[MemoryEntry], *, today: date
) -> list[str]:
    """計測上の注意用の固定文面。closed_date == today のみ。達成断定禁止。"""
    from .verdict import parse_pass_condition

    today_s = today.isoformat()
    notes: list[str] = []
    retired_n = 0
    for e in graduated:
        if e.closed_date != today_s:
            continue
        if e.status == "unmeasurable":
            notes.append(
                f"{e.id} は自動判定できるPASS条件が無いまま3日経過したため"
                f"終了扱いにしました。同じ狙いは判定可能な指標で出し直します。"
            )
        elif e.status == "graduated":
            parsed = parse_pass_condition(e.action)
            if parsed:
                metric, op, target = parsed
                cond = f"{metric} {op} {target:g}"
            else:
                cond = "条件"
            notes.append(
                f"{e.id} の PASS条件（{cond}）は測定できた直近2日とも"
                f"満たされていました（実行の有無は問いません）。📌からは外します。"
            )
            # 実験昇格は副産物1行（自動起票しない）。--title 必須。
            title = " ".join((e.action or "").split())[:30] or e.id
            if parsed:
                metric, op, target = parsed
                notes.append(
                    f"14日の実験として追跡するなら: kaizenlog experiment new "
                    f'--title "{title}" --metric {metric} '
                    f'--target "{op} {target:g}" --days 14'
                )
        elif e.status == "retired":
            retired_n += 1
            reason = e.closed_reason or "expired"
            why = {
                "unchecked_no_measurement": "測定日が足りず効果を判定できないまま未チェックだった",
                "unchecked_metric_ok_no_check": "指標は達していたがチェックされなかった（行動と指標の因果が弱い可能性）",
                "unchecked_metric_mixed": "指標が安定せず未チェックのまま過ぎた",
                "unchecked_metric_unmet": "未チェックのまま指標も目標に届かなかった",
                "expired": "期限切れ",
            }.get(reason, reason)
            notes.append(
                f"{e.id} を退役しました: {why}（終了扱いは達成を意味しません）。"
            )
    if retired_n and not any("退役" in n for n in notes):
        notes.append(
            f"提案を退役した件が {retired_n} 件あります"
            f"（終了扱いは達成を意味しません）。"
        )
    return notes


# 同時に「proposed のまま」にしてよい上限（§E・advise 新規を止める）
MAX_ACTIVE_PROPOSED = 3


def count_open_proposed(entries: Sequence[MemoryEntry]) -> int:
    """終端以外の status=proposed 件数（同時アクティブ提案数）。"""
    n = 0
    for e in entries:
        if e.status == "proposed":
            n += 1
    return n


def causal_mismatch_metrics(entries: Sequence[MemoryEntry]) -> frozenset[str]:
    """チェックなしで PASS した提案の指標名集合（同じ指標の新規提案を抑制する）。

    - proposed のまま confirmed pass
    - または done だが判定が実行前（_execution_aligned_verdict が False）
    """
    from .verdict import parse_pass_condition

    found: set[str] = set()
    for e in entries:
        parsed = parse_pass_condition(e.action)
        if parsed is None:
            continue
        metric = parsed[0]
        if (
            e.status == "proposed"
            and e.verdict == "pass"
            and e.verdict_stage == "confirmed"
        ):
            found.add(metric)
            continue
        if (
            e.status == "done"
            and e.verdict == "pass"
            and e.verdict_stage == "confirmed"
            and not _execution_aligned_verdict(e)
        ):
            found.add(metric)
    return frozenset(found)


def _verdict_block_line(entry: MemoryEntry) -> str:
    from .verdict import parse_pass_condition

    if entry.verdict_stage == "provisional" and entry.verdict in ("pass", "fail"):
        icon = f"⏳暫定{'PASS' if entry.verdict == 'pass' else 'FAIL'}"
    else:
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
    # 適応投与 / 未消化バックログ: システムが件数を制限する前提の理由説明
    # （決定論は evidence.max_actions = min(short, dosing, backlog)）
    if backlog_generation_cap(stats) == 1 and not (
        stats.proposed >= _DOSING_MIN_PROPOSED
        and stats.done_rate is not None
        and stats.done_rate < _DOSING_DONE_RATE
    ):
        # dosing 未発動でも done=0 等で1件制限（ACTION-UX P1）
        lines.append(
            "⚠️ 未チェックの提案が溜まっているため、システムが件数を1件に制限する。"
            "新規は最も小さく始められる1件のみ。既存の未完了を繰り返さない。"
            "action 文は「いつ→何をする→どう確認するか」が1行で分かる形にすること。"
        )
    elif (
        stats.proposed >= _DOSING_MIN_PROPOSED
        and stats.done_rate is not None
        and stats.done_rate < _DOSING_DONE_RATE
    ):
        lines.append(
            "⚠️ 消化率が低いため、システムが件数を1件に制限する。"
            "「今日の改善提案」と「明日の最小アクション」は最も小さく始められる1件にすること。"
            "action 文は「いつ→何をする→どう確認するか」が1行で分かる形にすること。"
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

    # 直近の判定（最大3日・新しい順）。終端 status は LLM プロンプトへ流さない
    verdict_lines: list[str] = []
    for delta in range(1, 4):
        d = (today - timedelta(days=delta)).isoformat()
        day_entries = [
            e
            for e in entries
            if e.verdict in ("pass", "fail")
            and e.verdict_date == d
            and e.status not in TERMINAL_STATUSES
        ]
        day_entries.sort(key=lambda e: e.id, reverse=True)
        for e in day_entries:
            verdict_lines.append(_verdict_block_line(e))
    if verdict_lines:
        lines.append("## 直近の判定（最大3日・新しい順）")
        lines.extend(verdict_lines)

    # 指標別PASS実績（実行済みトラック）
    mpr = metric_pass_rates(entries, today, window_days=30, min_judged=3)
    if mpr:
        lines.append("## 指標別PASS実績（直近30日）")
        for metric, passed, judged in mpr[:6]:
            trend = "✅傾向" if passed * 2 >= judged else "❌傾向"
            lines.append(f"- {metric} {passed}/{judged} {trend}")

    # 実行の有無は問わない指標の挙動（§B1）。達成断定ラベルは使わない。
    mbr = metric_behavior_rates(entries, today, window_days=30, min_judged=1)
    if mbr:
        lines.append("## 実行の有無は問わない指標の挙動（直近30日）")
        for metric, passed, judged in mbr[:6]:
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
    unmeasurable = [
        e
        for e in entries
        if e.status == "unmeasurable" and e.date >= d30
    ]
    unmeasurable.sort(key=lambda e: e.id, reverse=True)

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
    if unmeasurable:
        lines.append("## 判定不能で終了した提案（同種を繰り返さない）")
        for e in unmeasurable[:5]:
            lines.append(
                f"- {e.id}: {e.action[:80]} ｜ 理由: "
                f"{e.closed_reason or 'no_machine_pass'}"
            )
    return "\n".join(lines)


def _stats_by_day(
    stats_history: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    if not stats_history:
        return out
    for item in stats_history:
        if not isinstance(item, Mapping):
            continue
        day_key = item.get("day")
        if isinstance(day_key, str) and day_key:
            out[day_key] = item
    return out


def _denominator_shortfall_note(
    entry: MemoryEntry,
    stats_by_day: dict[str, Mapping[str, Any]],
) -> str | None:
    """分母不足で判定できないとき「判定不成立」注記を返す。失敗アイコンは付けない。

    分子キー欠落は分母不足と断定しない（注記を出さず従来表記へ落とす）。
    注記は「分子キーが存在し、かつ分母が下限未満」のときだけ。
    """
    if entry.verdict in ("pass", "fail"):
        return None
    from .verdict import measure_day_for_entry, parse_pass_condition

    parsed = parse_pass_condition(entry.action)
    if parsed is None:
        return None
    metric, _op, _t = parsed
    if not (metric.endswith("_per_hour") or metric.endswith("_per_session")):
        return None
    measure_day = measure_day_for_entry(entry)
    if measure_day is None:
        return None
    day_stats = stats_by_day.get(measure_day.isoformat())
    if day_stats is None:
        return None
    if metric.endswith("_per_hour"):
        # 分子: context_switches キーが無いなら注記しない
        if "context_switches" not in day_stats:
            return None
        if not isinstance(day_stats.get("context_switches"), (int, float)):
            return None
        mins = day_stats.get("total_minutes")
        if not isinstance(mins, (int, float)):
            return None
        if float(mins) >= 60:
            return None  # 分母は足りている（別理由で None なら従来表記）
        return (
            f"判定不成立・稼働{float(mins):g}分/必要60分"
            "・分母不足"
        )

    # *_per_session: 分子 tool_errors キーが必要、分母 sessions が 0 以下
    ai = day_stats.get("ai") if isinstance(day_stats.get("ai"), Mapping) else None
    if not isinstance(ai, Mapping):
        return None
    if "tool_errors" not in ai or not isinstance(ai.get("tool_errors"), (int, float)):
        return None
    sessions = ai.get("sessions")
    if not isinstance(sessions, (int, float)):
        return None
    if float(sessions) > 0:
        return None
    return (
        "判定不成立・AIセッション"
        f"{int(sessions) if float(sessions) == int(sessions) else sessions}件/必要1件"
        "・分母不足"
    )


@dataclass(frozen=True)
class MetricObservation:
    day: date
    value: float
    met: bool


@dataclass(frozen=True)
class PostVerdictTrajectory:
    metric: str
    op: str
    target: float
    observations: tuple[MetricObservation, ...]


def _post_verdict_trajectory(
    entry: MemoryEntry,
    target_day: date,
    stats_by_day: dict[str, Mapping[str, Any]],
) -> PostVerdictTrajectory | None:
    if entry.verdict not in ("pass", "fail"):
        return None
    if entry.verdict_stage != "confirmed" or not entry.verdict_date:
        return None
    from .experiments import metric_from_stats, target_met
    from .verdict import parse_pass_condition

    parsed = parse_pass_condition(entry.action)
    if parsed is None:
        return None
    metric, op, target = parsed
    try:
        start = date.fromisoformat(entry.verdict_date) + timedelta(days=1)
    except ValueError:
        return None
    end = target_day - timedelta(days=1)
    observations: list[MetricObservation] = []
    current = start
    while current <= end:
        day_stats = stats_by_day.get(current.isoformat())
        if day_stats is not None:
            value = metric_from_stats(metric, dict(day_stats))
            if value is not None:
                observations.append(
                    MetricObservation(
                        day=current,
                        value=float(value),
                        met=target_met(float(value), op, float(target)),
                    )
                )
        current += timedelta(days=1)
    if not observations:
        return None
    return PostVerdictTrajectory(
        metric=metric,
        op=op,
        target=float(target),
        observations=tuple(observations[-5:]),
    )


def _post_verdict_trajectory_lines(
    entry: MemoryEntry,
    target_day: date,
    stats_by_day: dict[str, Mapping[str, Any]],
) -> list[str]:
    trajectory = _post_verdict_trajectory(entry, target_day, stats_by_day)
    if trajectory is None:
        return []
    observations = trajectory.observations
    chain = " → ".join(
        f"{point.day.month}/{point.day.day} {point.value:g} "
        f"{'✅' if point.met else '❌'}"
        for point in observations
    )
    met_count = sum(point.met for point in observations)
    return [
        f"  └ 判定後の実測: {chain}",
        f"     (測定できた{len(observations)}日のうち"
        f"{met_count}日達成・{len(observations)-met_count}日未達。"
        "実行の有無は問わない指標の挙動です)",
    ]


_PASS_SPLIT_RE = re.compile(r"[｜|]\s*PASS\s*:", re.IGNORECASE)
_FAIL_SPLIT_RE = re.compile(r"[｜|]\s*FAIL\s*:", re.IGNORECASE)
_OP_JA = {"<=": "以下", ">=": "以上", "<": "未満", ">": "超", "==": "", "=": ""}
_ARROW_RE = re.compile(r"\s*→\s*")


def humanize_action_body(action: str) -> str:
    """台帳 action から行動文だけを取り、→ 前後に半角スペースを入れる。

    ｜PASS: 以降は落とす。機械構文が無い自由文はそのまま返す。
    """
    text = (action or "").strip()
    if not text:
        return ""
    m = _PASS_SPLIT_RE.search(text)
    body = text[: m.start()] if m else text
    body = body.strip(" ｜|\t")
    return _ARROW_RE.sub(" → ", body)


def _pass_segment(action: str) -> str | None:
    m = _PASS_SPLIT_RE.search(action or "")
    if not m:
        return None
    rest = action[m.end() :]
    fm = _FAIL_SPLIT_RE.search(rest)
    if fm:
        rest = rest[: fm.start()]
    return rest.strip(" ｜|\t")


def _annotation_label(pass_segment: str) -> str | None:
    """PASS 部の （…） / (...) 注記をラベルとして返す。"""
    for open_ch, close_ch in (("（", "）"), ("(", ")")):
        if close_ch not in pass_segment or open_ch not in pass_segment:
            continue
        end = pass_segment.rfind(close_ch)
        start = pass_segment.rfind(open_ch, 0, end)
        if start >= 0 and end > start:
            label = pass_segment[start + 1 : end].strip()
            if label:
                return label
    return None


def format_effect_metric_clause(action: str) -> str | None:
    """効果指標の本体（括弧タグなし）。機械構文が無ければ None。"""
    from .experiments import metric_display_label
    from .verdict import parse_pass_condition, strip_pass_annotation

    seg = _pass_segment(action)
    if seg is None:
        return None
    parsed = parse_pass_condition(action)
    if parsed is not None:
        metric, op, value = parsed
        label = _annotation_label(seg) or metric_display_label(metric) or metric
        op_ja = _OP_JA.get(op, op)
        val_s = f"{value:g}"
        if op_ja:
            return f"{label} を {val_s} {op_ja} に"
        return f"{label} を {val_s} に"
    # 自由文 PASS（機械構文ではない）
    core = strip_pass_annotation(seg).strip()
    if not core:
        return None
    return core


def format_action_display_lines(
    entry_id: str,
    action: str,
    *,
    mark: str = " ",
    tag: str,
) -> list[str]:
    """📌 用の平文化行。PASS 無しは1行、有りは2行。"""
    body = humanize_action_body(action)
    effect = format_effect_metric_clause(action)
    if effect is None:
        return [f"- [{mark}] {entry_id}: {body}（{tag}）"]
    return [
        f"- [{mark}] {entry_id}: {body}",
        f"    - 効果指標: {effect}（{tag}）",
    ]


def humanize_advice_markdown_actions(advice_md: str) -> str:
    """ADVICE 読者向け本文のチェックボックス行を平文化する。

    assign_action_ids の**後**・ノート書き込みの**前**に呼ぶこと。
    台帳へ渡す action 文字列（機械構文）は触らない。
    mechanism / falsifier / なぜ / 明日見る数字 のサブ行はそのまま保持。
    ｜PASS: を含まない自由文行は無変換。
    """
    lines = advice_md.splitlines()
    out: list[str] = []
    in_action_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            in_action_section = (
                ACTION_SECTION.removeprefix("### ") in stripped
                or LEGACY_ACTION_SECTION.removeprefix("### ") in stripped
            )
            out.append(line)
            continue
        if not in_action_section:
            out.append(line)
            continue
        m = _CHECKBOX_RE.match(line)
        if not m:
            # サブ行（効果指標以外の既存注記）はそのまま
            out.append(line)
            continue
        mark = m.group(2)
        rest = m.group(4).strip()
        if not _PASS_SPLIT_RE.search(rest):
            out.append(line)
            continue
        entry_id: str | None = None
        action = rest
        id_m = ID_PATTERN.match(rest)
        if id_m is not None:
            entry_id = id_m.group(0)
            action = rest[id_m.end() :].lstrip(": ").strip()
        else:
            # "KZN-…: …" 以外の位置に ID がある場合は本文全体を action 扱い
            id_any = ID_PATTERN.search(rest)
            if id_any is not None and rest[id_any.end() :].lstrip().startswith(":"):
                entry_id = id_any.group(0)
                action = rest[id_any.end() :].lstrip(": ").strip()
        effect = format_effect_metric_clause(action)
        body = humanize_action_body(action)
        if entry_id:
            out.append(f"- [{mark}] {entry_id}: {body}")
        else:
            out.append(f"- [{mark}] {body}")
        if effect is not None:
            out.append(f"    - 効果指標: {effect}")
    return "\n".join(out)


# ACTIONS 区間の旧サマリ行（第41弾 §A2 以前）
_SUMMARY_LOW_RE = re.compile(
    r"^今週の消化\s*(\d+)件（提案\s*(\d+)件）"
    r"(?:\s*/\s*スキップ\s*(\d+)件)?"
    r"(?:\s*/\s*実行済みPASS\s*(\d+)件(?:（[^）]*）)?)?"
    r"(?:（未実行のままPASS到達\s*(\d+)件[：:][^）]*）)?"
    r"\s*$"
)
_SUMMARY_RATE_RE = re.compile(
    r"^直近(\d+)日:\s*消化率\s*([0-9.]+%|-)\s*（(\d+)件中(\d+)件）"
    r"(?:\s*/\s*スキップ\s*(\d+)件)?"
    r"(?:\s*/\s*実行済みPASS\s*(\d+)件(?:（[^）]*）)?)?"
    r"(?:（未実行のままPASS到達\s*(\d+)件[：:][^）]*）)?"
    r"\s*$"
)
# 判定タグ: 行末の「（…提案…）」
_TRAILING_PROPOSAL_TAG_RE = re.compile(r"（([^）]*提案[^）]*)）\s*$")


_UNDONE_PASS_RE = re.compile(r"未実行のままPASS到達\s*(\d+)件")
_SKIP_IN_LINE_RE = re.compile(r"スキップ\s*(\d+)件")


def _humanize_actions_summary_line(line: str) -> str | None:
    """旧サマリ行を §A2 平文へ。失敗時 None（無変換）。"""
    text = line.strip()
    undone_m = _UNDONE_PASS_RE.search(text)
    undone = int(undone_m.group(1)) if undone_m else 0
    skip_m = _SKIP_IN_LINE_RE.search(text)
    skipped = int(skip_m.group(1)) if skip_m else 0

    m = _SUMMARY_LOW_RE.match(text)
    if m:
        done, proposed = int(m.group(1)), int(m.group(2))
        out = f"今週は{proposed}件提案し、チェック完了は{done}件。"
        if skipped:
            out += f"スキップは{skipped}件。"
        if undone:
            out += (
                f"うち{undone}件はチェックなしで指標が目標に達しています"
                f"（習慣化するなら下の「達成済み」からチェック）。"
            )
        return out
    m = _SUMMARY_RATE_RE.match(text)
    if m:
        window = int(m.group(1))
        rate = m.group(2)
        proposed, done = int(m.group(3)), int(m.group(4))
        out = (
            f"直近{window}日は{proposed}件提案し、"
            f"チェック完了は{done}件（完了率 {rate}）。"
        )
        if skipped:
            out += f"スキップは{skipped}件。"
        if undone:
            out += (
                f"うち{undone}件はチェックなしで指標が目標に達しています"
                f"（習慣化するなら下の「達成済み」からチェック）。"
            )
        return out
    return None


def _humanize_actions_checkbox_line(line: str) -> list[str] | None:
    """ACTIONS の機械構文チェック行を2行平文へ。失敗時 None。

    判定タグ（行末の「（…提案…）」）は効果指標行末尾に保持する。
    """
    m = _CHECKBOX_RE.match(line)
    if not m:
        return None
    mark = m.group(2)
    rest = m.group(4).strip()
    if not _PASS_SPLIT_RE.search(rest):
        return None
    id_m = ID_PATTERN.match(rest)
    if id_m is None:
        return None
    entry_id = id_m.group(0)
    action = rest[id_m.end() :].lstrip(": ").strip()
    tag = ""
    tag_m = _TRAILING_PROPOSAL_TAG_RE.search(action)
    if tag_m is not None:
        # FAIL 以降に付くタグのみ採用（PASS 注記の括弧と混同しない）
        fail_m = _FAIL_SPLIT_RE.search(action)
        if fail_m is not None and tag_m.start() >= fail_m.end():
            tag = tag_m.group(1)
            action = action[: tag_m.start()].rstrip()
        else:
            # タグ位置が想定外 → 無変換
            return None
    effect = format_effect_metric_clause(action)
    if effect is None:
        return None
    body = humanize_action_body(action)
    if tag:
        return [
            f"- [{mark}] {entry_id}: {body}",
            f"    - 効果指標: {effect}（{tag}）",
        ]
    return [
        f"- [{mark}] {entry_id}: {body}",
        f"    - 効果指標: {effect}",
    ]


def humanize_actions_section_markdown(actions_md: str) -> str:
    """📌 ACTIONS 区間本文の旧形式行を平文化する（冪等・不明行は無変換）。"""
    lines = actions_md.splitlines()
    out: list[str] = []
    for line in lines:
        # サマリ行
        if "消化" in line or "実行済みPASS" in line or "未実行のままPASS到達" in line:
            repl = _humanize_actions_summary_line(line)
            if repl is not None:
                out.append(repl)
                continue
        # チェックボックス機械構文
        if _CHECKBOX_RE.match(line) and _PASS_SPLIT_RE.search(line):
            repl_lines = _humanize_actions_checkbox_line(line)
            if repl_lines is not None:
                out.extend(repl_lines)
                continue
        out.append(line)
    return "\n".join(out)


def _split_action_trigger(body: str) -> tuple[str | None, str]:
    """行動文を「いつ」と「やる」に分ける。自由文はそのまま保持する。"""
    if " → " not in body:
        return None, body
    trigger, action = body.split(" → ", 1)
    return trigger.strip() or None, action.strip()


def _metric_scope_note(
    metric: str,
    latest_stats: Mapping[str, Any] | None,
) -> str | None:
    """日次集計の因果解釈を過剰にしないための限定注記。"""
    if metric == "ai_avg_turns":
        ai = latest_stats.get("ai") if latest_stats else None
        sessions = ai.get("sessions") if isinstance(ai, Mapping) else None
        count = (
            f"{int(sessions)}セッション。"
            if isinstance(sessions, Real)
            and not isinstance(sessions, bool)
            and math.isfinite(sessions)
            else ""
        )
        return f"全AI {count}特定AIツール単独の効果は判定できません"
    if metric == "context_switches_per_hour":
        return "日全体の観測値。特定の実施区間だけの効果は判定できません"
    return None


def _goal_monitoring_lines(note_content: str | None) -> list[str]:
    """既存GOALマーカーを読むだけで日次目標を表示する。"""
    from .goal import read_goal

    goal = read_goal(note_content)
    lines = ["## 🎯 日次目標", ""]
    if goal is None:
        return lines + ['- 未設定: `kaizenlog goal "今日達成したい成果"`']
    label = re.sub(r"^(?:🎯\s*)?今日の目標\s*[:：]\s*", "", goal.raw_line).strip()
    achieved = f"{goal.achieved}%（自己申告）" if goal.achieved is not None else "未入力"
    return lines + [f"- 目標: {label}", f"  - 達成度: {achieved}"]


def _action_card_lines(
    entry: MemoryEntry,
    mark: str,
    target_day: date,
    stats_by_day: dict[str, Mapping[str, Any]],
    *,
    thin_coverage: bool = False,
) -> list[str]:
    """今日実施する1件を、完了操作と測定状態に分けて表示する。"""
    from .verdict import format_action_verdict_tag, parse_pass_condition

    lines = [f"- [{mark}] {entry.id}:"]
    trigger, action = _split_action_trigger(humanize_action_body(entry.action))
    if trigger:
        lines.append(f"  - いつ: {trigger}")
    lines.append(f"  - やる: {action}")
    lines.append(f"  - 完了条件: 今日の予定分を実施して `kaizenlog done {entry.id}`")
    if effect := format_effect_metric_clause(entry.action):
        lines.append(f"  - 効果目標: {effect}")
        lines.append(f"  - 効果指標: {effect}")
    shortfall = _denominator_shortfall_note(entry, stats_by_day)
    if shortfall:
        lines.append(f"  - 測定: 未判定（集計待ち・{shortfall}）")
    else:
        lines.append(
            f"  - 測定: {format_action_verdict_tag(entry, thin_coverage=thin_coverage)}"
        )
    parsed = parse_pass_condition(entry.action)
    if parsed is not None:
        metric, _op, _target = parsed
        latest_stats = stats_by_day.get(target_day.isoformat())
        if scope := _metric_scope_note(metric, latest_stats):
            lines.append(f"  - 因果の範囲: {scope}")
    if entry.verdict == "fail" and entry.verdict_stage == "confirmed":
        lines.extend(_post_verdict_trajectory_lines(entry, target_day, stats_by_day))
    return lines


def _monitoring_card_lines(
    entry: MemoryEntry,
    target_day: date,
    stats_by_day: dict[str, Mapping[str, Any]],
) -> list[str]:
    """confirmed PASS後の指標だけを、実行カードと混ぜずに表示する。"""
    trajectory = _post_verdict_trajectory(entry, target_day, stats_by_day)
    if trajectory is None:
        return []
    latest = trajectory.observations[-1]
    met_count = sum(point.met for point in trajectory.observations)
    total = len(trajectory.observations)
    lines = [f"- {entry.id}"]
    lines.append(
        f"  - 最新: {latest.day.month}/{latest.day.day} {latest.value:g} "
        f"{'✅' if latest.met else '❌'}"
    )
    lines.append(
        f"  - 直近{total}日: {met_count}/{total}達成・未達{total - met_count}日"
        f"（目標 {trajectory.op} {trajectory.target:g}）"
    )
    latest_stats = stats_by_day.get(latest.day.isoformat())
    if scope := _metric_scope_note(trajectory.metric, latest_stats):
        lines.append(f"  - 集計範囲: {scope}")
    if not latest.met:
        lines.append("  - ⚠ 最新観測が目標未達です")
    return lines


def _status_and_all_lines(
    stats: ActionStats,
    buckets: OpenActionBuckets,
    actionable: Sequence[MemoryEntry],
    shown: Sequence[MemoryEntry],
    monitoring: Sequence[MemoryEntry],
    *,
    streaks: Streaks,
    monitoring_shown: int = 2,
) -> list[str]:
    """週次の投与状況と省略した一覧への導線を最後に置く。"""
    lines = ["## 🗂 状況・全件", ""]
    if stats.proposed > 0 or stats.skipped > 0:
        if stats.done == 0 and stats.proposed > 0:
            summary = f"今週の提案は{stats.proposed}件（未チェックの実験が残っています）。"
        elif (
            stats.done_rate is not None
            and stats.done_rate < _DOSING_DONE_RATE
            and stats.proposed >= _DOSING_MIN_PROPOSED
        ):
            summary = f"今週は{stats.proposed}件提案し、チェック完了は{stats.done}件。"
        else:
            rate = _pct_label(stats.done_rate) if stats.done_rate is not None else "—"
            summary = (
                f"直近{stats.window_days}日は{stats.proposed}件提案し、"
                f"チェック完了は{stats.done}件（完了率 {rate}）。"
            )
        if stats.skipped:
            summary += f"スキップは{stats.skipped}件。"
        if monitoring:
            summary += (
                f"うち{len(monitoring)}件はチェックなしで指標が目標に達しています"
                f"（指標は達成済み {len(monitoring)}件）。"
            )
        lines.append(summary)

    if streaks.current >= 2:
        lines.append(f"🔥 連続{streaks.current}日")
    elif streaks.broken_yesterday and streaks.best > 0:
        lines.append(f"今日から再スタート（過去最長 {streaks.best}日）")

    rest_recent = max(0, len(actionable) - len(shown))
    lines.append(
        f"ほか直近7日の未完了 {rest_recent}件"
        f" / 8〜30日前 {len(buckets.stale)}件"
        f" / 31日以上 {len(buckets.older)}件"
    )
    monitoring_extra = max(0, len(monitoring) - monitoring_shown)
    if monitoring_extra:
        lines.append(f"ほか効果モニタリング {monitoring_extra}件")
    lines.append("全件表示: `kaizenlog today --all`")
    return lines


def render_actions_section(
    entries: list[MemoryEntry],
    target_day: date,
    note_content: str | None = None,
    stats_history: Sequence[Mapping[str, Any]] | None = None,
    *,
    max_candidates: int | None = None,
) -> str | None:
    """翌日ノートの実行・効果観測・目標を分離したACTIONS区間を返す。"""
    buckets = partition_open_actions(entries, target_day, recent_include_today=False)
    if buckets.total == 0:
        return None

    checked_ids: set[str] = set()
    if note_content:
        for line in note_content.splitlines():
            match = _CHECKBOX_RE.match(line)
            if not match or match.group(2) not in ("x", "X"):
                continue
            id_match = ID_PATTERN.search(match.group(4))
            if id_match:
                checked_ids.add(id_match.group(0))

    stats = compute_action_stats(entries, target_day)
    streaks = compute_streaks(entries, target_day)
    candidate_cap = resolve_display_cap(stats, max_candidates=max_candidates)
    stats_by_day = _stats_by_day(stats_history)
    actionable, monitoring = split_action_candidates(buckets.recent, checked_ids)
    shown = actionable[:candidate_cap]

    def _thin_coverage_for(entry: MemoryEntry) -> bool:
        if entry.verdict in ("pass", "fail"):
            return False
        from .verdict import (
            _is_raw_count_metric,
            _prior_totals_from_history,
            _thin_measurement_coverage,
            measure_day_for_entry,
            parse_pass_condition,
        )

        parsed = parse_pass_condition(entry.action)
        if parsed is None or not _is_raw_count_metric(parsed[0]):
            return False
        measure_day = measure_day_for_entry(entry)
        if measure_day is None:
            return False
        day_stats = stats_by_day.get(measure_day.isoformat())
        if day_stats is None:
            return False
        total_minutes = day_stats.get("total_minutes")
        day_total = float(total_minutes) if isinstance(total_minutes, (int, float)) else None
        prior = _prior_totals_from_history([dict(h) for h in stats_by_day.values()], measure_day)
        return _thin_measurement_coverage(day_total, prior)

    lines = [f"## 📌 今日やること（{len(shown)}件）", ""]
    if shown:
        for entry in shown:
            mark = "x" if entry.id in checked_ids else " "
            lines.extend(
                _action_card_lines(
                    entry,
                    mark,
                    target_day,
                    stats_by_day,
                    thin_coverage=_thin_coverage_for(entry),
                )
            )
    else:
        lines.append("- 今日の実行候補はありません")

    lines.extend(["", "## 📈 効果モニタリング（今日やることではない）", ""])
    renderable_monitoring = [
        (entry, card)
        for entry in monitoring
        if (card := _monitoring_card_lines(entry, target_day, stats_by_day))
    ]
    shown_monitoring = renderable_monitoring[:2]
    for _entry, card in shown_monitoring:
        lines.extend(card)
    if not shown_monitoring:
        lines.append("- 確認できる効果モニタリングはありません")

    lines.extend([""])
    lines.extend(_goal_monitoring_lines(note_content))
    lines.extend([""])
    lines.extend(
        _status_and_all_lines(
            stats,
            buckets,
            actionable,
            shown,
            monitoring,
            streaks=streaks,
            monitoring_shown=len(shown_monitoring),
        )
    )
    return "\n".join(lines)
