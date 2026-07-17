"""繰り返しパターンの検出（自己実装カイゼンの候補出し）。

蓄積された日次統計JSONから、決定的なルールで「自動化・改善の候補」を検出する。
検出結果は /kaizen-autopilot スキル（Claude Codeエージェント）への入力になり、
エージェントが実際の自動化コードの実装を提案する。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

MIN_DAYS_WITH_DATA = 3


@dataclass
class PatternCandidate:
    kind: str  # "time_sink" | "routine" | "ai_friction"
    title: str
    evidence: str
    suggestion: str


def _threshold(days_with_data: int) -> int:
    """「習慣」とみなす最低日数: データのある日の半分以上（最低3日）。"""
    return max(MIN_DAYS_WITH_DATA, (days_with_data + 1) // 2)


def _dedupe_by_day(stats: list[dict]) -> list[dict]:
    """同一日のstatsを1件に統合する（後勝ち）。件数＝日数の前提を守る。"""
    by_day: dict[str, dict] = {}
    for i, entry in enumerate(stats):
        by_day[str(entry.get("day") or f"__no_day_{i}")] = entry
    return list(by_day.values())


def detect_time_sinks(
    stats: list[dict], min_minutes: float = 30.0
) -> list[PatternCandidate]:
    """毎日min_minutes以上使っているアプリ（時間泥棒/主要作業の候補）。"""
    stats = _dedupe_by_day(stats)
    days_over: dict[str, list[float]] = defaultdict(list)
    app_category: dict[str, str] = {}
    for day in stats:
        for app, minutes in day.get("by_app", {}).items():
            if minutes >= min_minutes:
                days_over[app].append(minutes)
        for b in day.get("blocks", []):
            app_category.setdefault(b.get("app", ""), b.get("category", ""))

    out = []
    need = _threshold(len(stats))
    for app, values in sorted(days_over.items(), key=lambda x: -sum(x[1])):
        if len(values) < need:
            continue
        avg = sum(values) / len(values)
        category = app_category.get(app, "")
        out.append(
            PatternCandidate(
                kind="time_sink",
                title=f"{app}（{category}）に毎日約{avg:.0f}分",
                evidence=f"{len(stats)}日中{len(values)}日で{min_minutes:.0f}分以上使用",
                suggestion=(
                    "エンタメ系なら時間帯を固定する実験を、作業系なら定型部分の"
                    "スクリプト化・AIエージェント化を検討"
                ),
            )
        )
    return out


def detect_routines(
    stats: list[dict], min_block_minutes: float = 15.0
) -> list[PatternCandidate]:
    """ほぼ毎日、同じ時間帯に発生する同一アプリの作業ブロック（定時ルーチン）。"""
    stats = _dedupe_by_day(stats)
    occurrences: dict[tuple[str, int], list[tuple[str, float, str]]] = defaultdict(list)
    for day in stats:
        seen_keys = set()
        for b in day.get("blocks", []):
            if b.get("minutes", 0) < min_block_minutes:
                continue
            try:
                hour = datetime.fromisoformat(b["start"]).hour
            except (KeyError, ValueError):
                continue
            key = (b.get("app", ""), hour)
            if key in seen_keys:  # 同日同時間帯は1回だけ数える
                continue
            seen_keys.add(key)
            occurrences[key].append(
                (day["day"], b.get("minutes", 0), b.get("title", ""))
            )

    out = []
    need = _threshold(len(stats))
    for (app, hour), occ in sorted(occurrences.items(), key=lambda x: -len(x[1])):
        if len(occ) < need:
            continue
        avg = sum(m for _, m, _ in occ) / len(occ)
        sample_title = next((t for _, _, t in occ if t), "")
        title_hint = f"「{sample_title}」" if sample_title else ""
        out.append(
            PatternCandidate(
                kind="routine",
                title=f"毎日{hour}時台に {app} で約{avg:.0f}分の定型作業{title_hint}",
                evidence=f"{len(stats)}日中{len(occ)}日で発生",
                suggestion=(
                    "内容が定型なら自動化の第一候補。スクリプト化、Claude Codeスキル化、"
                    "またはスケジュール実行への置き換えを検討"
                ),
            )
        )
    return out


def detect_ai_friction(stats: list[dict]) -> list[PatternCandidate]:
    """特定プロジェクトでAI作業の摩擦（細切れ・エラー）が慢性化していないか。"""
    stats = _dedupe_by_day(stats)
    fragmented_days: dict[str, int] = defaultdict(int)
    error_total: dict[str, int] = defaultdict(int)
    session_total: dict[str, int] = defaultdict(int)
    for day in stats:
        for project, p in day.get("ai", {}).get("projects", {}).items():
            session_total[project] += p.get("sessions", 0)
            error_total[project] += p.get("errors", 0)
            if p.get("fragmented", 0) > 0:
                fragmented_days[project] += 1

    out = []
    need = _threshold(len(stats))
    for project in sorted(session_total, key=lambda p: -session_total[p]):
        reasons = []
        if fragmented_days[project] >= need:
            reasons.append(f"細切れセッションが{fragmented_days[project]}日発生")
        if error_total[project] >= 5:
            reasons.append(f"ツールエラー計{error_total[project]}回")
        if not reasons:
            continue
        out.append(
            PatternCandidate(
                kind="ai_friction",
                title=f"プロジェクト {project} でAI作業の摩擦が慢性化",
                evidence="、".join(reasons) + f"（{len(stats)}日間）",
                suggestion=(
                    "そのリポジトリのCLAUDE.mdにビルド/テスト手順・前提を明文化する、"
                    "頻出依頼をスキル化する、細切れ依頼をタスク単位にまとめる"
                ),
            )
        )
    return out


def detect_all(stats: list[dict]) -> list[PatternCandidate]:
    return detect_time_sinks(stats) + detect_routines(stats) + detect_ai_friction(stats)


def render_patterns_markdown(stats: list[dict]) -> str:
    stats = _dedupe_by_day(stats)
    header = f"# 🔁 繰り返しパターン検出レポート（{len(stats)}日分のデータ）\n"
    if len(stats) < MIN_DAYS_WITH_DATA:
        return (
            header
            + f"\nデータが不足しています（{len(stats)}日分 / 最低{MIN_DAYS_WITH_DATA}日必要）。"
            "kaizenlog generate を数日実行してから再試行してください。\n"
        )
    candidates = detect_all(stats)
    if not candidates:
        return header + "\n検出された繰り返しパターンはありません。\n"

    kind_labels = {
        "time_sink": "## ⏳ 毎日の時間泥棒/主要作業",
        "routine": "## 🕘 定時ルーチン（自動化の第一候補）",
        "ai_friction": "## 🤖 AI作業の慢性的な摩擦",
    }
    lines = [header]
    for kind, label in kind_labels.items():
        group = [c for c in candidates if c.kind == kind]
        if not group:
            continue
        lines.append(label)
        lines.append("")
        for c in group:
            lines.append(f"- **{c.title}**")
            lines.append(f"  - 根拠: {c.evidence}")
            lines.append(f"  - 提案: {c.suggestion}")
        lines.append("")
    return "\n".join(lines)
