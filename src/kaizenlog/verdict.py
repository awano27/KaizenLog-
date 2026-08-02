"""PASS 条件の機械判定: 提案アクションを翌日の実測で ✅/❌ する。

LLM は PASS: を「指標 演算子 数値」で書く。generate が翌日に
compute_metric / target_met で判定し、Memory と前日ノートへ書き戻す。
自由文の PASS は人間判定のまま（parse が None）。
"""

from __future__ import annotations

import math
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .aiwork import AISession
from .experiments import (
    METRIC_DESCRIPTIONS,
    ExperimentError,
    compute_metric,
    metric_from_stats,
    parse_target,
    target_met,
)
from .focus import InputStats
from .memory import ACTIONS_HANDOFF_DAYS, ID_PATTERN, MemoryEntry
from .report import DailySummary
from .stats import load_stats
from .vault import ACTIONS_MARKER, ADVICE_MARKER

# プレースホルダキー自体は指標名として採用しない
_PLACEHOLDER_METRICS = {
    "category_minutes:<カテゴリ名>",
    "site_minutes:<ドメイン>",
}

# 機械構文らしい PASS 値（契約検証用）
_MACHINE_PASS_RE = re.compile(r"^\S+\s*(<=|>=|<|>|==?)\s*[\d.]+$")

# 判定 suffix（行末）の置換用
_VERDICT_SUFFIX_RE = re.compile(r"｜判定:.*$")

# ACTIONS の既存転記タグ（行末の括弧内）だけを置換する。
# 行末の空白と改行はキャプチャして、ノートの改行・末尾空白を保持する。
_ACTIONS_VERDICT_TAG_RE = re.compile(
    r"（\d{1,2}/\d{1,2}提案(?:・判定 [^\r\n]*?)?）"
    r"(?P<trailing>[ \t]*)(?P<newline>\r\n|\n|\r|$)"
)


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
    if metric.startswith("prompt_cluster:"):
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
    # レンダラが付けた日本語注記（…）/(...) を除去してから機械構文を判定
    segment = strip_pass_annotation(segment)
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


def strip_pass_annotation(pass_value: str) -> str:
    """PASS 値末尾の全角/半角括弧注記を除去する。

    ネスト・二重注記（LLM 自前注記＋レンダラ注記）も、末尾の括弧ペアが
    無くなるまで繰り返し剥がす。
    """
    s = pass_value.strip()

    def _strip_trailing_pair(text: str, open_ch: str, close_ch: str) -> str:
        if not text.endswith(close_ch):
            return text
        depth = 0
        for i in range(len(text) - 1, -1, -1):
            ch = text[i]
            if ch == close_ch:
                depth += 1
            elif ch == open_ch:
                depth -= 1
                if depth == 0:
                    return text[:i].rstrip()
        return text

    # 二重注記: （A）（B）や （A（B）） を順に落とす
    while True:
        prev = s
        s = _strip_trailing_pair(s, "（", "）")
        s = _strip_trailing_pair(s, "(", ")")
        if s == prev:
            break
    return s.strip()


def looks_like_machine_pass(pass_value: str) -> bool:
    """契約検証用: PASS 値が機械構文らしいか（指標の既知性は別判定）。"""
    return bool(_MACHINE_PASS_RE.match(strip_pass_annotation(pass_value)))


def judge_entries(
    entries: list[MemoryEntry],
    proposal_day: date,
    summary: DailySummary,
    cc_sessions: list[AISession],
    input_stats: InputStats | None,
    judged_day: date,
    retry_chains: int | None = None,
    known_categories: set[str] | frozenset[str] | None = None,
    *,
    today: date | None = None,
) -> list[MemoryEntry]:
    """提案日のエントリを判定し、差分 MemoryEntry だけを返す。

    再実行で同じ verdict/stage なら空（JSONL 増殖防止）。status/done_date は保持。
    compute_metric が None の行はスキップ（watcher 未導入など）。

    today を渡した場合、測定日当日は provisional、翌日以降は confirmed。
    省略時は既存呼び出しの後方互換として confirmed。
    """
    proposal = proposal_day.isoformat()
    judged = judged_day.isoformat()
    out: list[MemoryEntry] = []
    for entry in entries:
        if entry.date != proposal:
            continue
        # 同日再 advise で撤回された行は判定しない（統計・通知の汚染防止）
        if entry.status not in ("proposed", "done"):
            continue
        parsed = parse_pass_condition(entry.action)
        if parsed is None:
            continue
        metric, op, target_value = parsed
        value = compute_metric(
            metric,
            summary,
            cc_sessions,
            input_stats,
            retry_chains=retry_chains,
            known_categories=known_categories,
        )
        if value is None:
            continue
        met = target_met(value, op, target_value)
        verdict = "pass" if met else "fail"
        calculated_stage = (
            "confirmed"
            if today is None or judged_day < today
            else "provisional"
        )
        # 既に確定した同じID・測定日の行は、途中値の再判定で降格させない。
        stage = (
            "confirmed"
            if entry.verdict and entry.verdict_stage == "confirmed"
            else calculated_stage
        )
        # 同一なら差分なし（浮動小数は isclose）
        if (
            entry.verdict == verdict
            and entry.verdict_date == judged
            and entry.verdict_value is not None
            and math.isclose(entry.verdict_value, value, rel_tol=1e-9, abs_tol=1e-9)
            and entry.verdict_stage == stage
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
                skip_reason=entry.skip_reason,
                verdict_stage=stage,
            )
        )
    return out


@dataclass
class BackfillResult:
    judged: list[MemoryEntry]
    judged_count: int
    skipped_no_stats: int
    skipped_unsupported: int
    skipped_none: int

    @property
    def skipped_total(self) -> int:
        return self.skipped_no_stats + self.skipped_unsupported + self.skipped_none

    def log_line(self) -> str:
        return (
            f"verdict backfill: judged {self.judged_count}, "
            f"skipped {self.skipped_total} "
            f"(no-stats {self.skipped_no_stats}, "
            f"unsupported {self.skipped_unsupported}, "
            f"none {self.skipped_none})"
        )


def measure_day_for_entry(entry: MemoryEntry) -> date | None:
    """判定に使う測定日（未判定行向け）。

    done_date あり → done_date + 1日（行動の効果を測る）。
    無し → 提案日 + 1日（提案の妥当性）。不正日付は None。

    既に verdict_date がある行の再判定では、backfill_verdicts が
    verdict_date を優先する（一度決めた測定日を動かさない）。
    """
    if entry.done_date:
        try:
            return date.fromisoformat(entry.done_date) + timedelta(days=1)
        except ValueError:
            pass
    try:
        return date.fromisoformat(entry.date) + timedelta(days=1)
    except ValueError:
        return None


# stage 遷移表（第36弾 §A4 / 第37弾 §Z2）。表外遷移は追記せず警告して当該IDのみスキップ。
# キー: (現在 stage | None=未判定, 次 stage) → 許可
ALLOWED_VERDICT_STAGE_TRANSITIONS: frozenset[tuple[str | None, str]] = frozenset(
    {
        (None, "provisional"),
        (None, "confirmed"),
        ("provisional", "provisional"),
        ("provisional", "confirmed"),
        ("confirmed", "confirmed"),
    }
)


def is_allowed_verdict_stage_transition(
    from_stage: str | None, to_stage: str
) -> bool:
    """遷移表内の stage 変化なら True。from_stage は未判定時 None。"""
    return (from_stage, to_stage) in ALLOWED_VERDICT_STAGE_TRANSITIONS


def filter_allowed_stage_updates(
    current_by_id: dict[str, MemoryEntry],
    candidates: list[MemoryEntry],
    *,
    warn: Callable[[str], None] | None = None,
) -> list[MemoryEntry]:
    """表外 stage 遷移の候補を落とし、許可された更新だけ返す。

    同一IDの最新行が confirmed なのに provisional へ落ちる等は
    推測で補正せず、台帳へ追記しない（当該IDのみスキップ）。
    """
    kept: list[MemoryEntry] = []
    for cand in candidates:
        cur = current_by_id.get(cand.id)
        if cur is not None and cur.verdict in ("pass", "fail"):
            from_stage: str | None = cur.verdict_stage
        else:
            from_stage = None
        to_stage = cand.verdict_stage
        if not is_allowed_verdict_stage_transition(from_stage, to_stage):
            msg = (
                f"⚠️  stage遷移が表外のためスキップ: {cand.id} "
                f"{from_stage!r} → {to_stage!r}"
            )
            if warn is not None:
                warn(msg)
            else:
                print(msg, file=sys.stderr)
            continue
        kept.append(cand)
    return kept


def backfill_verdicts(
    entries: list[MemoryEntry],
    stats_dir: Path,
    as_of: date,
    *,
    window_days: int = ACTIONS_HANDOFF_DAYS,
    known_categories: set[str] | frozenset[str] | None = None,
) -> BackfillResult:
    """verdict 未設定かつ提案日が as_of から window 内のエントリを後追い判定。

    実測は保存済み stats の metric_from_stats のみ。
    ai_avg_turns は v2 キー / 導出 / v1 projects 近似で復元可能。不能時はスキップ。
    測定日の stats が無ければスキップ（次回 generate で再試行）。

    既に verdict_date がある行は、その日を測定日として固定する
    （done_date 後付けで測定日がずれて confirmed 昇格が永久に起きない退行を防ぐ）。
    done_date による再割り当ては verdict 未設定の行に限る。
    """
    window_start = (as_of - timedelta(days=window_days)).isoformat()
    # as_of 当日提案は測定日が明日になるので対象外（window は提案日）
    window_end = (as_of - timedelta(days=1)).isoformat()
    result = BackfillResult([], 0, 0, 0, 0)
    # stats キャッシュ（day iso → dict | None missing）
    stats_cache: dict[str, dict | None] = {}

    def load_day(d: date) -> dict | None:
        key = d.isoformat()
        if key not in stats_cache:
            loaded = load_stats(stats_dir, 1, d)
            stats_cache[key] = loaded[0] if loaded else None
        return stats_cache[key]

    for entry in entries:
        if (
            entry.verdict in ("pass", "fail")
            and entry.verdict_stage == "confirmed"
        ):
            continue
        if entry.status not in ("proposed", "done"):
            continue
        if not entry.date or not (window_start <= entry.date <= window_end):
            continue
        parsed = parse_pass_condition(entry.action)
        if parsed is None:
            continue
        metric, op, target_value = parsed
        # 一度決めた測定日は動かさない。done_date 再割当は未判定行のみ。
        measure_day: date | None = None
        if entry.verdict_date:
            try:
                measure_day = date.fromisoformat(entry.verdict_date)
            except ValueError:
                measure_day = None
        if measure_day is None:
            measure_day = measure_day_for_entry(entry)
        if measure_day is None or measure_day > as_of:
            # 測定日が未来ならまだ測れない
            continue
        stats = load_day(measure_day)
        if stats is None:
            result.skipped_no_stats += 1
            continue
        value = metric_from_stats(metric, stats, known_categories=known_categories)
        if value is None:
            # focus_*/input は input キー欠落、ai_avg_turns は復元不能日、未知カテゴリ
            if (
                metric == "ai_avg_turns"
                or metric == "ai_output_tokens"
                or metric.startswith("focus_")
                or metric == "input_keypresses"
            ):
                result.skipped_unsupported += 1
            elif metric.startswith("category_minutes:") and known_categories is not None:
                cat = metric.split(":", 1)[1].strip()
                if cat not in known_categories:
                    result.skipped_none += 1
                else:
                    result.skipped_unsupported += 1
            else:
                result.skipped_unsupported += 1
            continue
        met = target_met(value, op, target_value)
        verdict = "pass" if met else "fail"
        judged_day = measure_day.isoformat()
        stage = "confirmed" if measure_day < as_of else "provisional"
        # 表外遷移（例: confirmed→provisional）は追記しない
        from_stage = (
            entry.verdict_stage
            if entry.verdict in ("pass", "fail")
            else None
        )
        if not is_allowed_verdict_stage_transition(from_stage, stage):
            print(
                f"⚠️  stage遷移が表外のためスキップ: {entry.id} "
                f"{from_stage!r} → {stage!r}",
                file=sys.stderr,
            )
            continue
        if (
            entry.verdict == verdict
            and entry.verdict_date == judged_day
            and entry.verdict_value is not None
            and math.isclose(entry.verdict_value, value, rel_tol=1e-9, abs_tol=1e-9)
            and entry.verdict_stage == stage
        ):
            continue
        result.judged.append(
            MemoryEntry(
                id=entry.id,
                date=entry.date,
                action=entry.action,
                status=entry.status,
                done_date=entry.done_date,
                verdict=verdict,
                verdict_value=value,
                verdict_date=judged_day,
                skip_reason=entry.skip_reason,
                verdict_stage=stage,
            )
        )
        result.judged_count += 1
    return result


def format_verdict_suffix(entry: MemoryEntry) -> str:
    """ノート ADVICE 行末の判定 suffix。FAIL は目標との残距離を示す。"""
    icon = "✅" if entry.verdict == "pass" else "❌"
    val = f"{entry.verdict_value:g}" if entry.verdict_value is not None else "?"
    parsed = parse_pass_condition(entry.action)
    if entry.verdict_stage == "provisional" and entry.verdict in ("pass", "fail"):
        if parsed is None or entry.verdict_value is None:
            return f"｜判定: ⏳ 集計中（途中値{val}）"
        _metric, op, target = parsed
        target_value = float(target)
        if op in ("<=", "<"):
            target_text = f"{target_value:g}以下"
        elif op in (">=", ">"):
            target_text = f"{target_value:g}以上"
        else:
            target_text = f"{target_value:g}"
        try:
            judged_day = date.fromisoformat(entry.verdict_date or "")
            judged_label = f"{judged_day.month}/{judged_day.day}"
        except ValueError:
            judged_label = "判定日不明"
        return (
            f"｜判定: ⏳ 集計中（途中値{val}・目標{target_text}・"
            f"{judged_label}の日締め後に確定）"
        )
    if (
        parsed is None
        or entry.verdict_value is None
        or entry.verdict not in ("pass", "fail")
    ):
        return f"｜判定: {icon}（実測 {val}）"
    _metric, op, target = parsed
    measured = float(entry.verdict_value)
    t = float(target)
    if entry.verdict == "pass":
        return f"｜判定: ✅ 実測{measured:g}（目標{t:g}）"
    # FAIL: 目標との差（「あと」）
    if op in ("<=", "<"):
        dist = measured - t  # 超過分を減らす
    elif op in (">=", ">"):
        dist = t - measured  # 不足分を足す
    else:
        dist = abs(measured - t)
    if dist < 0:
        dist = abs(dist)
    near_thr = abs(t) * 0.1 if t != 0 else 0.1
    if dist <= near_thr:
        return f"｜判定: ❌ あと一歩（実測{measured:g}/目標{t:g}）"
    return f"｜判定: ❌ 実測{measured:g}（目標{t:g}・あと{dist:g}）"


def format_action_verdict_tag(entry: MemoryEntry) -> str:
    """📌 ACTIONS 行の括弧内タグを、stageを含めて決定論で返す。"""
    try:
        proposal_day = date.fromisoformat(entry.date)
        proposal_label = f"{proposal_day.month}/{proposal_day.day}"
    except ValueError:
        proposal_label = entry.date
    if entry.verdict not in ("pass", "fail"):
        return f"{proposal_label}提案"
    val = f"{entry.verdict_value:g}" if entry.verdict_value is not None else "?"
    if entry.verdict_stage == "provisional":
        try:
            judged_day = date.fromisoformat(entry.verdict_date or "")
            judged_label = f"{judged_day.month}/{judged_day.day}"
        except ValueError:
            judged_label = "判定日不明"
        return (
            f"{proposal_label}提案・判定 ⏳ 集計中・途中値{val}・"
            f"{judged_label}の日締め後に確定"
        )
    icon = "✅" if entry.verdict == "pass" else "❌"
    return f"{proposal_label}提案・判定 {icon} 実測{val}"


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


def apply_verdicts_to_actions_note(
    content: str, judged: list[MemoryEntry]
) -> str | None:
    """既存 ACTIONS 区間の判定タグだけを、判定結果に合わせて更新する。

    候補上限や現在の status では再抽出せず、既存区間に実在する同一IDの
    行だけを対象にする。マーカー外と、チェック状態・アクション本文・
    改行コードはそのまま保持し、対象行に既存の転記タグが無い場合は
    fail-closed で変更しない。
    """
    by_id = {
        entry.id: entry
        for entry in judged
        if entry.verdict in ("pass", "fail")
    }
    if not by_id:
        return None

    start_tag = f"<!-- {ACTIONS_MARKER}:start -->"
    end_tag = f"<!-- {ACTIONS_MARKER}:end -->"
    start_idx = content.find(start_tag)
    end_idx = content.find(end_tag, start_idx + len(start_tag)) if start_idx >= 0 else -1
    if start_idx < 0 or end_idx < 0:
        return None

    body_start = start_idx + len(start_tag)
    body = content[body_start:end_idx]
    changed = False
    new_lines: list[str] = []
    for line in body.splitlines(keepends=True):
        id_match = ID_PATTERN.search(line)
        if not id_match or id_match.group(0) not in by_id:
            new_lines.append(line)
            continue
        tag_match = _ACTIONS_VERDICT_TAG_RE.search(line)
        if tag_match is None:
            new_lines.append(line)
            continue
        entry = by_id[id_match.group(0)]
        replacement = (
            f"（{format_action_verdict_tag(entry)}）"
            f"{tag_match.group('trailing')}{tag_match.group('newline')}"
        )
        new_line = line[:tag_match.start()] + replacement + line[tag_match.end():]
        if new_line != line:
            changed = True
        new_lines.append(new_line)

    if not changed:
        return None
    return content[:body_start] + "".join(new_lines) + content[end_idx:]
