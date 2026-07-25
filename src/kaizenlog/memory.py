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
        if text in same_day_actions:
            reused_id = same_day_actions[text]
            used_ids.add(reused_id)
            lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{reused_id}: {text}"
            continue
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


def summarize_for_prompt(
    entries: list[MemoryEntry], today: date, max_items: int = 10
) -> str:
    """LLMに渡す「過去の提案の記録」を組み立てる。無ければ空文字。"""
    if not entries:
        return ""
    d30 = (today - timedelta(days=30)).isoformat()
    d7 = (today - timedelta(days=7)).isoformat()

    open_actions = [e for e in entries if e.status == "proposed" and e.date >= d30]
    recent_done = [e for e in entries if e.status == "done" and (e.done_date or "") >= d7]

    lines: list[str] = []
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

    対象は proposed かつ提案日が target_day-7 〜 target_day-1。
    0件なら None（既存セクションは消さない）。
    note_content に同じ KZN の [x] があればチェック状態を保持する。
    """
    window_start = (target_day - timedelta(days=7)).isoformat()
    window_end = (target_day - timedelta(days=1)).isoformat()
    open_entries = [
        e for e in entries
        if e.status == "proposed" and window_start <= e.date <= window_end
    ]
    open_entries.sort(key=lambda e: e.id)
    if not open_entries:
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
    for e in open_entries:
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
    return "\n".join(lines)
