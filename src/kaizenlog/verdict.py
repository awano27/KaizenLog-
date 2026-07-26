"""PASS 条件の機械判定: 提案アクションを翌日の実測で ✅/❌ する。

LLM は PASS: を「指標 演算子 数値」で書く。generate が翌日に
compute_metric / target_met で判定し、Memory と前日ノートへ書き戻す。
自由文の PASS は人間判定のまま（parse が None）。
"""

from __future__ import annotations

import math
import re
from datetime import date

from .aiwork import AISession
from .experiments import (
    METRIC_DESCRIPTIONS,
    ExperimentError,
    compute_metric,
    parse_target,
    target_met,
)
from .focus import InputStats
from .memory import ID_PATTERN, MemoryEntry
from .report import DailySummary
from .vault import ADVICE_MARKER

# プレースホルダキー自体は指標名として採用しない
_PLACEHOLDER_METRICS = {
    "category_minutes:<カテゴリ名>",
    "site_minutes:<ドメイン>",
}

# 機械構文らしい PASS 値（契約検証用）
_MACHINE_PASS_RE = re.compile(r"^\S+\s*(<=|>=|<|>|==?)\s*[\d.]+$")

# 判定 suffix（行末）の置換用
_VERDICT_SUFFIX_RE = re.compile(r"｜判定:.*$")


def is_known_metric(metric: str) -> bool:
    """実験指標として認識できる名前か。

    固定キー（プレースホルダ除く）または category_minutes:/site_minutes: ＋
    空白を含まない非空サフィックス（日本語カテゴリ可）。
    """
    if not metric or metric in _PLACEHOLDER_METRICS:
        return False
    if metric in METRIC_DESCRIPTIONS:
        return True
    if metric.startswith("category_minutes:"):
        suffix = metric.split(":", 1)[1]
        # プレースホルダ文字列そのもの・空白入りは不可
        return bool(suffix) and " " not in suffix and "\t" not in suffix and "<" not in suffix
    if metric.startswith("site_minutes:"):
        suffix = metric.split(":", 1)[1]
        return bool(suffix) and " " not in suffix and "\t" not in suffix and "<" not in suffix
    return False


def parse_pass_condition(action_text: str) -> tuple[str, str, float] | None:
    """アクション文から機械可読な PASS 条件を取り出す。

    区切りは全角｜と半角|の両方。FAIL: 以降は見ない。
    成功時は (metric, op, value)。自由文・未知指標は None。
    """
    if not action_text:
        return None
    idx = action_text.find("PASS:")
    if idx < 0:
        return None
    rest = action_text[idx + len("PASS:"):]
    fail_idx = rest.find("FAIL:")
    if fail_idx >= 0:
        rest = rest[:fail_idx]
    # パイプ区切りの残りセグメントを落とす
    for sep in ("｜", "|"):
        pipe = rest.find(sep)
        if pipe >= 0:
            rest = rest[:pipe]
    segment = rest.strip(" ｜|/\t")
    m = re.match(r"^(\S+)\s*(<=|>=|<|>|==?)\s*([\d.]+)\s*$", segment)
    if not m:
        return None
    metric = m.group(1)
    if not is_known_metric(metric):
        return None
    try:
        op, value = parse_target(f"{m.group(2)} {m.group(3)}")
    except ExperimentError:
        return None
    return metric, op, value


def looks_like_machine_pass(pass_value: str) -> bool:
    """契約検証用: PASS 値が機械構文らしいか（指標の既知性は別判定）。"""
    return bool(_MACHINE_PASS_RE.match(pass_value.strip()))


def judge_entries(
    entries: list[MemoryEntry],
    proposal_day: date,
    summary: DailySummary,
    cc_sessions: list[AISession],
    input_stats: InputStats | None,
    judged_day: date,
    retry_chains: int | None = None,
) -> list[MemoryEntry]:
    """提案日のエントリを判定し、差分 MemoryEntry だけを返す。

    再実行で同じ verdict なら空（JSONL 増殖防止）。status/done_date は保持。
    compute_metric が None の行はスキップ（watcher 未導入など）。
    """
    proposal = proposal_day.isoformat()
    judged = judged_day.isoformat()
    out: list[MemoryEntry] = []
    for entry in entries:
        if entry.date != proposal:
            continue
        parsed = parse_pass_condition(entry.action)
        if parsed is None:
            continue
        metric, op, target_value = parsed
        value = compute_metric(
            metric, summary, cc_sessions, input_stats, retry_chains=retry_chains
        )
        if value is None:
            continue
        met = target_met(value, op, target_value)
        verdict = "pass" if met else "fail"
        # 同一なら差分なし（浮動小数は isclose）
        if (
            entry.verdict == verdict
            and entry.verdict_date == judged
            and entry.verdict_value is not None
            and math.isclose(entry.verdict_value, value, rel_tol=1e-9, abs_tol=1e-9)
        ):
            continue
        out.append(
            MemoryEntry(
                id=entry.id,
                date=entry.date,
                action=entry.action,
                status=entry.status,
                done_date=entry.done_date,
                verdict=verdict,
                verdict_value=value,
                verdict_date=judged,
            )
        )
    return out


def format_verdict_suffix(entry: MemoryEntry) -> str:
    icon = "✅" if entry.verdict == "pass" else "❌"
    val = f"{entry.verdict_value:g}" if entry.verdict_value is not None else "?"
    return f"｜判定: {icon}（実測 {val}）"


def apply_verdicts_to_advice_note(
    content: str, judged: list[MemoryEntry]
) -> str | None:
    """前日ノートの ADVICE 区間内だけに判定 suffix を付与する。

    区間外の同一 KZN 行には触れない。既存 ｜判定: は置換（冪等）。
    変更が無ければ None。
    """
    by_id = {e.id: e for e in judged if e.verdict}
    if not by_id:
        return None
    start_tag = f"<!-- {ADVICE_MARKER}:start -->"
    end_tag = f"<!-- {ADVICE_MARKER}:end -->"
    start_idx = content.find(start_tag)
    end_idx = content.find(end_tag)
    if start_idx < 0 or end_idx < 0 or end_idx < start_idx:
        return None
    body_start = start_idx + len(start_tag)
    body = content[body_start:end_idx]
    # 先頭改行を保つため splitlines で再構成
    lines = body.splitlines()
    # body が "\n...\n" のとき splitlines は端の空行を落とすことがあるので
    # 行単位で置換し、元の前後空白は簡易に復元する
    changed = False
    new_lines: list[str] = []
    for line in lines:
        id_match = ID_PATTERN.search(line)
        if not id_match or id_match.group(0) not in by_id:
            new_lines.append(line)
            continue
        entry = by_id[id_match.group(0)]
        suffix = format_verdict_suffix(entry)
        base = _VERDICT_SUFFIX_RE.sub("", line.rstrip())
        new_line = base + suffix
        if new_line != line:
            changed = True
        new_lines.append(new_line)
    if not changed:
        return None
    # 先頭・末尾にちょうど1つの改行を保証（splitlines の空要素に重ねて増殖させない）
    new_body = "\n".join(new_lines)
    if not new_body.startswith("\n"):
        new_body = "\n" + new_body
    if not new_body.endswith("\n"):
        new_body = new_body + "\n"
    return content[:body_start] + new_body + content[end_idx:]
