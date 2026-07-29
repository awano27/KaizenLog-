"""プロンプト資産の記憶層（クラスタ台帳）。

promptmine の毎回ゼロ再計算と非対称を解消する。Kaizen Memory と同じ
追記型後勝ち JSONL / 安定 ID（PRM-YYYYMMDD-NNN）。

representative は [privacy] redact_patterns 適用後に保存する。
理由: 依頼文の逐語はボールト同期で外部へ漏れ得るため、日誌原文主義の
意図的例外（第18弾 title レダクトと同じ方針）。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from .promptmine import DEFAULT_SIMILARITY, PromptCluster, normalize

LEDGER_FILE = "prompt_clusters.jsonl"
ID_PATTERN = re.compile(r"PRM-(\d{8})-(\d{3,})")
STATUSES = frozenset({"new", "skilled", "dismissed"})


@dataclass
class PromptLedgerEntry:
    id: str
    representative: str  # 正規化＋redact 済み
    count_total: int
    days_seen: int
    first_seen: str  # YYYY-MM-DD
    last_seen: str  # YYYY-MM-DD
    status: str = "new"  # new | skilled | dismissed
    skill_name: str | None = None
    # skilled/dismissed にした日（旧 JSONL は欠落 → None）
    marked_on: str | None = None


def _ledger_path(memory_dir: Path) -> Path:
    return Path(memory_dir) / LEDGER_FILE


def load_prompt_ledger(memory_dir: Path) -> list[PromptLedgerEntry]:
    """追記型 JSONL を後勝ちで読み、ID 昇順で返す。"""
    path = _ledger_path(memory_dir)
    if not path.is_file():
        return []
    by_id: dict[str, PromptLedgerEntry] = {}
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
        status = str(d.get("status") or "new")
        if status not in STATUSES:
            status = "new"
        skill = d.get("skill_name")
        if skill is not None:
            skill = str(skill).strip() or None
        marked_on = d.get("marked_on")
        if marked_on is not None:
            marked_on = str(marked_on).strip() or None
        try:
            count_total = int(d.get("count_total") or 0)
        except (TypeError, ValueError):
            count_total = 0
        try:
            days_seen = int(d.get("days_seen") or 0)
        except (TypeError, ValueError):
            days_seen = 0
        by_id[str(d["id"])] = PromptLedgerEntry(
            id=str(d["id"]),
            representative=str(d.get("representative") or ""),
            count_total=max(0, count_total),
            days_seen=max(0, days_seen),
            first_seen=str(d.get("first_seen") or ""),
            last_seen=str(d.get("last_seen") or ""),
            status=status,
            skill_name=skill,
            marked_on=marked_on,
        )
    return sorted(by_id.values(), key=lambda e: e.id)


def append_prompt_ledger(
    memory_dir: Path, entries: list[PromptLedgerEntry]
) -> None:
    if not entries:
        return
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    with open(_ledger_path(memory_dir), "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")


def next_prm_id(existing: list[PromptLedgerEntry], day: date, offset: int = 0) -> str:
    prefix = f"PRM-{day.strftime('%Y%m%d')}-"
    used = {
        int(e.id.rsplit("-", 1)[1])
        for e in existing
        if e.id.startswith(prefix) and e.id.rsplit("-", 1)[-1].isdigit()
    }
    n = 1 + offset
    while n in used:
        n += 1
    return f"{prefix}{n:03d}"


def similarity_ratio(a: str, b: str) -> float:
    """代表文同士の類似度。normalize は比較時に行う（冪等なので既存の
    正規化済み台帳データにもそのまま効く）。"""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def find_matching_entry(
    entries: list[PromptLedgerEntry],
    representative: str,
    *,
    similarity: float = DEFAULT_SIMILARITY,
) -> PromptLedgerEntry | None:
    """台帳エントリと代表文を類似度照合。最良一致を返す。"""
    rep = (representative or "").strip()
    if not rep:
        return None
    best: PromptLedgerEntry | None = None
    best_ratio = 0.0
    for e in entries:
        ratio = similarity_ratio(rep, e.representative)
        if ratio >= similarity and ratio > best_ratio:
            best, best_ratio = e, ratio
    return best


def _redact_display(
    raw_text: str, redactor: Callable[[str], str] | None
) -> str:
    """生文に redact を適用した「読める」代表文を返す。

    保存するのはこちら（週次レビュー等の表示用）。照合用の正規化は
    similarity_ratio が比較時に行うため、ここでは normalize しない。
    """
    t = " ".join((raw_text or "").split())
    if not t:
        return ""
    if redactor is not None:
        t = redactor(t)
    return t.strip()


def upsert_clusters(
    memory_dir: Path,
    clusters: list[PromptCluster],
    *,
    as_of: date,
    redactor: Callable[[str], str] | None = None,
    similarity: float = DEFAULT_SIMILARITY,
) -> list[PromptLedgerEntry]:
    """発掘クラスタを台帳へ upsert。戻り値は今回扱ったエントリ（最新状態）。

    count/days は max 更新（加算ではない）: 日別の内訳を保存していないため、
    加算すると重なり合うスキャン窓や同日再実行で二重計上する。max なら
    「これまで観測した最大の窓内出現数」という一貫した意味で冪等になる。
    代表文はより出現数の多い方へ更新。照合は promptmine と同じ正規化+閾値。
    """
    existing = load_prompt_ledger(memory_dir)
    by_id: dict[str, PromptLedgerEntry] = {e.id: e for e in existing}
    as_of_s = as_of.isoformat()
    minted: list[PromptLedgerEntry] = list(by_id.values())
    new_offset = 0
    touched: list[PromptLedgerEntry] = []
    to_append: list[PromptLedgerEntry] = []

    for c in clusters:
        # 生 example を redact（読める形のまま保存。正規化は照合時）
        source = (c.example or c.representative or "").strip()
        if not source:
            continue
        rep = _redact_display(source, redactor)
        if not rep:
            continue
        days = sorted(d for d in c.days if d)
        day_count = len(days) if days else (1 if c.count else 0)
        first = days[0] if days else as_of_s
        last = days[-1] if days else as_of_s
        match = find_matching_entry(list(by_id.values()), rep, similarity=similarity)
        if match is None:
            new_id = next_prm_id(minted, as_of, offset=new_offset)
            new_offset += 1
            entry = PromptLedgerEntry(
                id=new_id,
                representative=rep,
                count_total=max(0, int(c.count)),
                days_seen=day_count,
                first_seen=first,
                last_seen=last,
                status="new",
                skill_name=None,
                marked_on=None,
            )
            by_id[entry.id] = entry
            minted.append(entry)
            to_append.append(entry)
            touched.append(entry)
            continue

        # 合流: count/days は max（同日再実行で加算しない＝冪等）
        new_count = max(match.count_total, int(c.count))
        new_days = max(match.days_seen, day_count)
        new_first = match.first_seen or first
        if first and (not new_first or first < new_first):
            new_first = first
        new_last = match.last_seen or last
        if last and (not new_last or last > new_last):
            new_last = last
        # より出現数の多い方の代表文
        new_rep = rep if int(c.count) >= match.count_total else match.representative
        # 変化がなければ追記スキップ（完全冪等）
        if (
            new_count == match.count_total
            and new_days == match.days_seen
            and new_first == match.first_seen
            and new_last == match.last_seen
            and new_rep == match.representative
        ):
            touched.append(match)
            continue
        updated = PromptLedgerEntry(
            id=match.id,
            representative=new_rep,
            count_total=new_count,
            days_seen=new_days,
            first_seen=new_first,
            last_seen=new_last,
            status=match.status,
            skill_name=match.skill_name,
            marked_on=match.marked_on,
        )
        by_id[updated.id] = updated
        to_append.append(updated)
        touched.append(updated)

    append_prompt_ledger(memory_dir, to_append)
    return touched


def resolve_prm_id(
    query: str, entries: list[PromptLedgerEntry]
) -> PromptLedgerEntry | list[PromptLedgerEntry] | None:
    """mark 用 ID 解決（done と同じ: 完全一致 → サフィックス → 曖昧はリスト）。

    サフィックスは new を優先（done と同じ開いた項目優先）。new にヒットが
    無ければ既処理も含めて解決する（skilled→dismissed 等の訂正用途）。
    """
    q = (query or "").strip()
    if not q:
        return None
    exact = [e for e in entries if e.id == q]
    if exact:
        return exact[-1]
    open_hits = [e for e in entries if e.status == "new" and e.id.endswith(q)]
    if len(open_hits) == 1:
        return open_hits[0]
    if len(open_hits) > 1:
        return open_hits
    all_hits = [e for e in entries if e.id.endswith(q)]
    if len(all_hits) == 1:
        return all_hits[0]
    if len(all_hits) > 1:
        return all_hits
    return None


def mark_prompt_entry(
    entry: PromptLedgerEntry,
    status: str,
    *,
    skill_name: str | None = None,
    marked_on: str | date | None = None,
) -> PromptLedgerEntry:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    skill = None
    if status == "skilled":
        skill = (skill_name or "").strip() or None
        if not skill:
            raise ValueError("skilled には skill_name が必要です")
    if marked_on is None:
        marked_s = date.today().isoformat()
    elif isinstance(marked_on, date):
        marked_s = marked_on.isoformat()
    else:
        marked_s = str(marked_on).strip() or date.today().isoformat()
    return PromptLedgerEntry(
        id=entry.id,
        representative=entry.representative,
        count_total=entry.count_total,
        days_seen=entry.days_seen,
        first_seen=entry.first_seen,
        last_seen=entry.last_seen,
        status=status,
        skill_name=skill if status == "skilled" else None,
        marked_on=marked_s if status in ("skilled", "dismissed") else None,
    )


def format_ledger_line(entry: PromptLedgerEntry, example: str | None = None) -> str:
    """表示1行。cp932 でも落ちにくい基本文字のみ。"""
    body = (example or entry.representative or "").replace("\n", " ").strip()
    if len(body) > 80:
        body = body[:77] + "..."
    return (
        f"{entry.id} [{entry.status}] "
        f"{entry.count_total}回/{entry.days_seen}日: {body}"
    )


def ledger_status_counts(entries: list[PromptLedgerEntry]) -> dict[str, int]:
    counts = {"new": 0, "skilled": 0, "dismissed": 0}
    for e in entries:
        if e.status in counts:
            counts[e.status] += 1
    return counts


def representative_for_cluster_id(
    memory_dir: Path, cluster_id: str
) -> str | None:
    """cluster_id から台帳の代表文を解決。無ければ None。"""
    cid = (cluster_id or "").strip()
    if not cid:
        return None
    for e in load_prompt_ledger(memory_dir):
        if e.id == cid and e.representative:
            return e.representative
    return None
