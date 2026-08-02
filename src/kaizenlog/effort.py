"""工数のつけ先配分（決定論・純関数）。

セッションの「開始-最終」を作業時間とみなさない。
AI作業ブロックのみを短い適合セッションへ帰属させる。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from .report import (
    Block,
    SessionSpan,
    _fmt_minutes,
    _overlap_minutes,
    _tool_class_matches,
)

# パス上の汎用ディレクトリ（子なら親をプロジェクト名にする）
_GENERIC_DIR_NAMES = frozenset(
    {
        ".venv",
        "venv",
        "src",
        "docs",
        "tests",
        "test",
        "node_modules",
        "dist",
        "build",
        "out",
        "target",
        "bin",
        "lib",
        "include",
        "__pycache__",
        ".git",
        ".github",
        "scripts",
        "assets",
        "public",
        "static",
        "vendor",
        "pkg",
        "cmd",
        "internal",
        "packages",
        "apps",
        "services",
    }
)

# リポジトリ名ではない汎用チャット project
_GENERIC_CHAT_PROJECTS = frozenset(
    {
        "",
        "—",
        "-",
        "chatgpt",
        "gemini",
        "claude",
        "claude-code",
        "codex",
        "openai",
        "unknown",
        "browser",
        "web",
    }
)

BUCKET_PRIVATE = "（私的）"
BUCKET_RESEARCH = "（調査・共通）"
BUCKET_AI_GENERAL = "（AI・汎用相談）"
BUCKET_UNCLASSIFIED = "（未分類）"

# 固定バケット（プロジェクト名ではないため redact 対象外）
_FIXED_BUCKETS = frozenset(
    {BUCKET_PRIVATE, BUCKET_RESEARCH, BUCKET_AI_GENERAL, BUCKET_UNCLASSIFIED}
)

EVIDENCE_AI = "AIセッション突合"
EVIDENCE_BROWSE = "ブラウジング"
EVIDENCE_PRIVATE = "エンタメ分類"
EVIDENCE_PATH = "ウィンドウパス"
EVIDENCE_PATH_MISS = "パス不明"
EVIDENCE_AI_MISS = "セッション不明"
EVIDENCE_OTHER = "その他"


@dataclass
class EffortReport:
    minutes: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, dict[str, float]] = field(default_factory=dict)
    unclassified_apps: list[tuple[str, float]] = field(default_factory=list)
    total_minutes: float = 0.0

    def to_stats_dict(self) -> dict[str, Any]:
        return {
            "minutes": {k: round(v, 2) for k, v in sorted(self.minutes.items())},
            "evidence": {
                k: {ek: round(ev, 2) for ek, ev in sorted(v.items())}
                for k, v in sorted(self.evidence.items())
            },
            "unclassified_apps": [
                [name, round(mins, 2)] for name, mins in self.unclassified_apps
            ],
            "total_minutes": round(self.total_minutes, 2),
        }


def _is_generic_chat_project(project: str) -> bool:
    p = (project or "").strip()
    if p.lower() in _GENERIC_CHAT_PROJECTS:
        return True
    # パス区切りや拡張子が無く、短い英単語のみなら汎用寄り
    if "/" in p or "\\" in p or p.endswith("-"):
        return False
    return False


def _project_from_span(span: SessionSpan) -> str:
    proj = getattr(span, "project", None)
    if isinstance(proj, str) and proj.strip():
        return proj.strip()
    # label は "project: title" 形式
    label = str(getattr(span, "label", "") or "")
    if ": " in label:
        return label.split(": ", 1)[0].strip()
    return label.strip()


def _shortest_matching_span(
    block: Block, spans: Sequence[SessionSpan]
) -> SessionSpan | None:
    """ツール適合かつ重なるセッションのうち、区間が最も短いものを選ぶ。"""
    thr = min(2.0, float(block.minutes) * 0.5) if block.minutes > 0 else 0.0
    best: SessionSpan | None = None
    best_len: float | None = None
    for sp in spans:
        if not _tool_class_matches(block.tool, sp.tool_class):
            continue
        ov = _overlap_minutes(block.start, block.end, sp.start, sp.end)
        if ov < thr and thr > 0:
            continue
        if ov <= 0:
            continue
        length = max(0.0, (sp.end - sp.start).total_seconds() / 60.0)
        if best is None or length < (best_len or 1e18):
            best = sp
            best_len = length
    return best


def _looks_like_filename(name: str) -> bool:
    """`index.html` のようなファイル名をプロジェクト名として採用しないための判定。

    ルート直下にリポジトリがある構成（`<root>/<repo>/<file>`）では、子要素が
    ファイル名になりうる。拡張子付き（末尾が `.` + 英数字1〜5文字）なら除外する。
    `KaizenLog-` `gotouchi-ai-v2` のようなディレクトリ名は誤判定しない。
    """
    return bool(re.search(r"\.[A-Za-z0-9]{1,5}$", name or ""))


def extract_project_from_title(
    title: str,
    project_roots: Sequence[str],
) -> str | None:
    """ウィンドウタイトルからプロジェクト名を抽出。無ければ None。"""
    text = title or ""
    if not text or not project_roots:
        return None
    # 正規化: バックスラッシュ統一
    norm = text.replace("\\", "/")
    for root in project_roots:
        r = str(root or "").strip().replace("\\", "/").rstrip("/")
        if not r:
            continue
        # 大文字小文字を無視してルート以降を探す
        pattern = re.compile(
            re.escape(r) + r"/([^/\s\"'`|]+)/([^/\s\"'`|]+)",
            re.IGNORECASE,
        )
        m = pattern.search(norm)
        if not m:
            # root/name only
            pattern2 = re.compile(
                re.escape(r) + r"/([^/\s\"'`|]+)",
                re.IGNORECASE,
            )
            m2 = pattern2.search(norm)
            if m2:
                name = m2.group(1)
                if name.lower() not in _GENERIC_DIR_NAMES and not _looks_like_filename(
                    name
                ):
                    return name
            continue
        parent, child = m.group(1), m.group(2)
        if child.lower() in _GENERIC_DIR_NAMES or _looks_like_filename(child):
            return parent if not _looks_like_filename(parent) else None
        return child
    return None


def allocate_effort(
    blocks: Sequence[Block | Mapping[str, Any]],
    spans: Sequence[SessionSpan],
    *,
    tz: tzinfo | None = None,  # 将来用・現状ブロック時刻は既に aware 想定
    self_paths: Sequence[str] = (),
    project_roots: Sequence[str] | None = None,
    private_categories: Sequence[str] | None = None,
    redactor: Callable[[str], str] | None = None,
) -> EffortReport:
    """ブロック列とセッションスパンから工数を配分する。

    redactor: つけ先名（プロジェクト名・リポジトリ名）に適用する秘匿関数。
      工数表と stats の双方に出るため、日誌の他の箇所と同じ [privacy]
      redact_patterns を必ず通す（未指定なら素通し）。
    """
    roots = list(project_roots) if project_roots is not None else ["C:/develop"]
    private = set(private_categories or ("エンタメ",))
    minutes: dict[str, float] = defaultdict(float)
    evidence: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    unclass_apps: dict[str, float] = defaultdict(float)
    total = 0.0

    for raw in blocks:
        if isinstance(raw, Mapping):
            cat = str(raw.get("category") or "")
            app = str(raw.get("app") or "")
            mins = float(raw.get("minutes") or 0.0)
            titles = [str(raw.get("title") or "")]
            tool = raw.get("tool")
            # reconstruct minimal block-like for AI matching when full Block given
            start = raw.get("start")
            end = raw.get("end")
            if isinstance(start, str):
                try:
                    start = datetime.fromisoformat(start)
                except ValueError:
                    start = None
            if isinstance(end, str):
                try:
                    end = datetime.fromisoformat(end)
                except ValueError:
                    end = None
            block = Block(
                start=start or datetime.min,
                end=end or datetime.min,
                category=cat,
                app=app,
                titles=titles,
                ai=(cat == "AI作業"),
                tool=str(tool) if tool else None,
            )
            # override minutes if start/end invalid
            if mins <= 0 and start and end and end > start:
                mins = (end - start).total_seconds() / 60.0
            elif mins <= 0:
                mins = float(block.minutes) if start and end else 0.0
        else:
            block = raw
            cat = block.category
            app = block.app
            mins = float(block.minutes)
            titles = list(block.titles or [])

        if mins <= 0:
            continue
        total += mins

        # 1. 私的
        if cat in private:
            bucket = BUCKET_PRIVATE
            ev = EVIDENCE_PRIVATE
        # 2. AI作業
        elif cat == "AI作業":
            span = _shortest_matching_span(block, spans)
            if span is None:
                bucket = BUCKET_AI_GENERAL
                ev = EVIDENCE_AI_MISS
            else:
                proj = _project_from_span(span)
                if _is_generic_chat_project(proj):
                    bucket = BUCKET_AI_GENERAL
                    ev = EVIDENCE_AI_MISS
                else:
                    bucket = proj
                    ev = EVIDENCE_AI
        # 3. 開発 / 執筆
        elif cat in ("開発", "執筆・ノート"):
            title = " ".join(titles)
            # self_paths に含まれるパスは自プロダクト寄りだが、名前抽出は roots 基準
            name = extract_project_from_title(title, roots)
            if name:
                bucket = name
                ev = EVIDENCE_PATH
            else:
                bucket = BUCKET_UNCLASSIFIED
                ev = EVIDENCE_PATH_MISS
                unclass_apps[app or "unknown"] += mins
        # 4. ブラウジング
        elif cat == "ブラウジング":
            bucket = BUCKET_RESEARCH
            ev = EVIDENCE_BROWSE
        # 5. その他
        else:
            bucket = BUCKET_UNCLASSIFIED
            ev = EVIDENCE_OTHER
            unclass_apps[app or "unknown"] += mins

        # つけ先名は日誌と stats の双方に出るため、ここで一度だけ redact する
        # （固定バケット名「（未分類）」等は対象外＝素通しでよい）
        if redactor is not None and bucket not in _FIXED_BUCKETS:
            bucket = redactor(bucket) or bucket
        minutes[bucket] += mins
        evidence[bucket][ev] += mins

    unclass_list = sorted(unclass_apps.items(), key=lambda x: (-x[1], x[0]))[:5]
    return EffortReport(
        minutes=dict(minutes),
        evidence={k: dict(v) for k, v in evidence.items()},
        unclassified_apps=unclass_list,
        total_minutes=total,
    )


def _primary_evidence(ev: Mapping[str, float]) -> str:
    if not ev:
        return "—"
    return max(ev.items(), key=lambda x: (x[1], x[0]))[0]


def render_effort_markdown(
    report: EffortReport,
    *,
    min_display_minutes: float = 1.0,
    private_label: str = BUCKET_PRIVATE,
) -> str:
    """日誌用「⏱ 工数のつけ先」Markdown。"""
    if report.total_minutes <= 0 or not report.minutes:
        return (
            "## ⏱ 工数のつけ先\n\n"
            "記録された作業時間がありません。\n"
        )

    # 業務先 → 私的は最後
    items = [(k, v) for k, v in report.minutes.items() if v > 0]
    work = [(k, v) for k, v in items if k != private_label]
    priv = [(k, v) for k, v in items if k == private_label]

    # min_display 未満は「ほか」にまとめる（私的以外）
    shown: list[tuple[str, float]] = []
    other = 0.0
    for k, v in sorted(work, key=lambda x: (-x[1], x[0])):
        if v < min_display_minutes:
            other += v
        else:
            shown.append((k, v))
    if other >= min_display_minutes:
        shown.append(("（ほか）", other))
    shown.extend(sorted(priv, key=lambda x: (-x[1], x[0])))

    total = report.total_minutes
    lines = [
        "## ⏱ 工数のつけ先",
        "",
        "| つけ先 | 時間 | 割合 | 根拠 |",
        "| --- | ---: | ---: | --- |",
    ]
    for name, mins in shown:
        pct = mins / total * 100 if total else 0.0
        if name == "（ほか）":
            ev = "少額合算"
        else:
            ev = _primary_evidence(report.evidence.get(name, {}))
        lines.append(
            f"| {name} | {_fmt_minutes(mins)} | {pct:.0f}% | {ev} |"
        )
    lines.append("")

    if report.unclassified_apps:
        # 表示: WindowsTerminal.exe 22分 / explorer.exe 18分 / ほか 38分
        parts: list[str] = []
        for i, (app, mins) in enumerate(report.unclassified_apps):
            if i < 2:
                parts.append(f"{app} {_fmt_minutes_plain(mins)}")
        unclass_total = report.minutes.get(BUCKET_UNCLASSIFIED, 0.0)
        shown_apps = sum(m for _, m in report.unclassified_apps[:2])
        leftover = max(0.0, unclass_total - shown_apps)
        if leftover > 0.5:
            parts.append(f"ほか {_fmt_minutes_plain(leftover)}")
        if parts:
            lines.append("未分類の内訳: " + " / ".join(parts))
            lines.append("")

    lines.append(
        "※ 画面の前景アプリからの推定です。工数入力の下書きとして使い、"
        "実際の申告は本人が判断してください。"
    )
    unclass = report.minutes.get(BUCKET_UNCLASSIFIED, 0.0)
    if total > 0 and unclass / total > 0.30:
        lines.append(
            "※ 未分類が全体の30%を超えています。"
            "`[effort] project_roots` の設定で改善できます。"
        )
    lines.append("")
    return "\n".join(lines)


def _fmt_minutes_plain(minutes: float) -> str:
    """内訳用: 22分 / 1時間10分。"""
    m = int(round(minutes))
    if m < 60:
        return f"{m}分"
    h, mm = divmod(m, 60)
    if mm == 0:
        return f"{h}時間"
    return f"{h}時間{mm}分"
