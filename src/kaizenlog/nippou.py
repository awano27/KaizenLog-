"""日報ドラフトの自動生成。

活動ログから提出用の日報下書きを作る。2モード:
- 決定的モード: 統計JSONから事実ベースの箇条書きを組み立てる（LLM不要・0秒）
- LLMモード:   活動ログ＋計画を渡して自然な文章に仕上げる
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, tzinfo
from typing import Any

from .advisor import generate_text
from .config import LLMConfig

NIPPOU_MARKER_HEADING = "## 📝 日報ドラフト"

# stats.ai.sources キー → 日報表示名
_AI_SOURCE_LABELS = {
    "claude-code": "Claude Code",
    "codex": "Codex CLI",
}


def _format_ai_agent_names(ai: dict) -> str:
    """ai.sources から表示名を組み立てる。旧形式は Claude Code 固定。"""
    sources = ai.get("sources")
    if not isinstance(sources, dict) or not sources:
        return "Claude Code"
    labels: list[str] = []
    for key in sorted(sources.keys()):
        labels.append(_AI_SOURCE_LABELS.get(str(key), str(key)))
    return " / ".join(labels) if labels else "Claude Code"

NIPPOU_SYSTEM_PROMPT = """\
あなたはユーザーの作業ログから、上司やチームに提出する日報の下書きを作るアシスタントです。

ルール:
- ログにある事実だけを書く。憶測で成果を盛らない
- 「〜を実施」「〜を完了」など簡潔なですます調・体言止めの混在で、日本の日報として自然な文体
- 分単位の細かい時刻は書かず、作業のまとまりで表現する
- エンタメ・私的なブラウジングは日報に含めない
- 合計400字以内

出力形式（この見出しをそのまま使う）:
【本日の業務】
- 主要な作業を3〜6項目

【成果・進捗】
- 完了したこと・前進したことを1〜3項目（Tasksのチェック済み項目があれば反映）

【明日の予定】
- 未完のタスク・計画から1〜3項目（材料が無ければ「引き続き上記対応」等)

【所感】
- 1〜2文（AI活用や作業の進め方で特筆すべきことがあれば）
"""


def build_nippou_prompt(activity_md: str, intent: str | None) -> str:
    parts = []
    if intent:
        parts.append(f"# 本日の計画・タスク（手書き）\n{intent}\n\n")
    parts.append(f"# 本日の作業ログ\n{activity_md}")
    return "".join(parts)


def build_nippou_facts_block(stats: Mapping[str, Any]) -> str:
    """LLM 入力用の決定論事実ブロック（プロジェクト集約+コミット subjects）。"""
    lines = ["# プロジェクト事実（決定論）"]
    for row in _project_work_lines(stats):
        lines.append(row)
    for row in _outcome_lines(stats, include_total=False):
        lines.append(row)
    if len(lines) == 1:
        lines.append("- （計測データなし）")
    return "\n".join(lines)


def generate_nippou_llm(
    cfg: LLMConfig, activity_md: str, intent: str | None, redactor=None,
    *, stats: Mapping[str, Any] | None = None,
) -> str:
    prompt = build_nippou_prompt(activity_md, intent)
    if stats is not None:
        prompt = prompt + "\n\n" + build_nippou_facts_block(stats)
    if redactor:
        prompt = redactor(prompt)  # 送信プロンプトのみマスク
    body = generate_text(cfg, NIPPOU_SYSTEM_PROMPT, prompt)
    return f"{NIPPOU_MARKER_HEADING}\n\n{body}"


# ---- 決定的モード（LLM不要） ----

_PRIVATE_CATEGORIES = ("エンタメ",)
# ブラウザ経由の私的コンテンツ（分類上「ブラウジング」になるもの）も日報から除外する
_PRIVATE_TITLE_RE = re.compile(
    r"youtube|netflix|spotify|twitter|reddit|tiktok|niconico|ニコニコ|prime video",
    re.IGNORECASE,
)


def _is_private(block: dict) -> bool:
    if block.get("category") in _PRIVATE_CATEGORIES:
        return True
    return bool(_PRIVATE_TITLE_RE.search(f"{block.get('title', '')} {block.get('app', '')}"))


def _fmt_minutes(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}時間{m}分" if h else f"{m}分"


def _checked_tasks(intent: str | None) -> list[str]:
    if not intent:
        return []
    return [
        m.group(1).strip()
        for m in re.finditer(r"^- \[x\]\s*(.+)$", intent, re.MULTILINE | re.IGNORECASE)
    ]


def _unchecked_tasks(intent: str | None) -> list[str]:
    if not intent:
        return []
    return [
        m.group(1).strip()
        for m in re.finditer(r"^- \[ \]\s*(.+)$", intent, re.MULTILINE)
    ]


def _session_digests(stats: Mapping[str, Any]) -> list[dict]:
    ai = stats.get("ai") if isinstance(stats.get("ai"), Mapping) else {}
    digests = ai.get("session_digests") if isinstance(ai, Mapping) else None
    if not isinstance(digests, list):
        return []
    out = []
    for d in digests:
        if not isinstance(d, Mapping):
            continue
        # is_internal 相当: 収集時に除外済みだが、残存フラグがあれば落とす
        if d.get("is_internal"):
            continue
        out.append(dict(d))
    return out


def _project_work_lines(stats: Mapping[str, Any], *, limit: int = 5) -> list[str]:
    """session digests を project 別に集約した業務行。"""
    by_proj: dict[str, dict[str, Any]] = {}
    for d in _session_digests(stats):
        project = str(d.get("project") or "—").strip() or "—"
        bucket = by_proj.setdefault(
            project,
            {
                "sessions": 0,
                "turns": 0,
                "edits": 0,
                "edits_known": False,
                "titles": [],
            },
        )
        bucket["sessions"] += 1
        bucket["turns"] += int(d.get("user_turns") or 0)
        # tools 計測不能（web 等）は edits を省略
        tools_total = d.get("tools_total")
        source = str(d.get("source") or "")
        tools_ok = tools_total is not None and not source.endswith("-web")
        if tools_ok:
            bucket["edits"] += int(d.get("edits") or 0)
            bucket["edits_known"] = True
        title = str(d.get("title") or "").strip()
        if title:
            bucket["titles"].append(title)

    ranked = sorted(
        by_proj.items(),
        key=lambda kv: (-int(kv[1]["turns"]), -int(kv[1]["sessions"]), kv[0]),
    )
    lines: list[str] = []
    for project, data in ranked[:limit]:
        rep = ""
        candidates = [t for t in data["titles"] if len(t) >= 8]
        if candidates:
            rep = max(candidates, key=len)
        elif data["titles"]:
            rep = max(data["titles"], key=len)
        title_part = f"「{rep}」" if rep else "「—」"
        if data["edits_known"]:
            meta = (
                f"セッション{data['sessions']}回・往復{data['turns']}"
                f"・編集{data['edits']}"
            )
        else:
            meta = f"セッション{data['sessions']}回・往復{data['turns']}"
        lines.append(f"- {project}: {title_part}（{meta}）")
    extra = len(ranked) - limit
    if extra > 0:
        lines.append(f"- ほか {extra}プロジェクト")
    return lines


def _screen_block_lines(
    stats: Mapping[str, Any],
    tz: tzinfo,
    *,
    min_block_minutes: float,
    limit: int = 3,
) -> list[str]:
    """エンタメ・私的以外かつ min 分以上のスクリーンブロック（補完）。"""
    blocks = [
        b
        for b in stats.get("blocks", [])
        if isinstance(b, Mapping)
        and float(b.get("minutes") or 0) >= min_block_minutes
        and not _is_private(b)
        and b.get("category") not in _PRIVATE_CATEGORIES
    ]
    blocks.sort(key=lambda b: -float(b.get("minutes") or 0))
    lines: list[str] = []
    for b in blocks[:limit]:
        try:
            hour = datetime.fromisoformat(str(b["start"])).astimezone(tz).hour
            when = "午前" if hour < 12 else "午後"
        except (KeyError, TypeError, ValueError):
            when = ""
        title = b.get("title") or b.get("app", "")
        prefix = f"{when}: " if when else ""
        lines.append(
            f"- {prefix}{title}（{b.get('category', '')}、"
            f"約{_fmt_minutes(float(b.get('minutes') or 0))}）"
        )
    return lines


def _outcome_lines(
    stats: Mapping[str, Any],
    *,
    include_total: bool = True,
    intent: str | None = None,
) -> list[str]:
    lines: list[str] = []
    checked = _checked_tasks(intent)
    for t in checked[:3]:
        lines.append(f"- {t} を完了")

    og = stats.get("outcome_git")
    if isinstance(og, list):
        for item in og:
            if not isinstance(item, Mapping):
                continue
            label = str(item.get("repo_label") or "repo")
            commits = int(item.get("commits") or 0)
            ins = int(item.get("insertions") or 0)
            dels = int(item.get("deletions") or 0)
            subjects = item.get("subjects") if isinstance(item.get("subjects"), list) else []
            subj_bits = [str(s).strip() for s in subjects[:2] if str(s).strip()]
            if subj_bits:
                lines.append(
                    f"- {label}: コミット{commits}件（+{ins}/-{dels}）"
                    f"主な内容: {' / '.join(subj_bits)}"
                )
            elif commits > 0:
                lines.append(f"- {label}: コミット{commits}件（+{ins}/-{dels}）")

    # テスト実行を伴うセッション
    test_n = sum(
        1 for d in _session_digests(stats) if d.get("tests_run")
    )
    if test_n > 0:
        lines.append(f"- テスト実行を伴うセッション {test_n}回")

    if include_total:
        private = sum(
            float(m)
            for cat, m in (stats.get("by_category") or {}).items()
            if cat in _PRIVATE_CATEGORIES and isinstance(m, (int, float))
        )
        total = max(0.0, float(stats.get("total_minutes") or 0) - private)
        by_cat = stats.get("by_category") if isinstance(stats.get("by_category"), Mapping) else {}
        ai_min = by_cat.get("AI作業") if isinstance(by_cat, Mapping) else None
        if isinstance(ai_min, (int, float)) and float(ai_min) > 0:
            lines.append(
                f"- 合計 {_fmt_minutes(total)} の作業"
                f"（うちAI作業 {_fmt_minutes(float(ai_min))}）"
            )
        else:
            lines.append(f"- 合計 {_fmt_minutes(total)} の作業")
    return lines


def _parse_block_minutes(text: str) -> float:
    """タイムライン表の時間セル（`6m` / `1h5m`）を分に直す。"""
    m = re.fullmatch(r"\s*(?:(\d+)h)?(?:(\d+)m)?\s*", text or "")
    if not m or not (m.group(1) or m.group(2)):
        return 0.0
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def _screenpipe_work_lines(
    stats: Mapping[str, Any],
    activity_md: str | None,
) -> list[str]:
    """digest が無い AI 画面を screenpipe 要約で補完（activity 表から抽出）。

    合計10分以上のとき最大1行。テキストは stats に保存しない設計のため
    日誌のタイムライン行 `（画面テキスト: …）` を読む。
    """
    if not activity_md or "画面テキスト" not in activity_md:
        return []
    # 画面テキストで補完された行だけを集計する（AI作業全体ではない）
    by_app: dict[str, dict[str, Any]] = {}
    for ln in activity_md.splitlines():
        if "画面テキスト:" not in ln or "|" not in ln:
            continue
        # | time | min | AI作業 | App | （画面テキスト: …） |
        parts = [p.strip() for p in ln.strip().strip("|").split("|")]
        if len(parts) < 5:
            continue
        if parts[2] != "AI作業":
            continue
        m = re.search(r"（画面テキスト:\s*(.+?)）\s*$", parts[4])
        if not m:
            continue
        bucket = by_app.setdefault(parts[3], {"minutes": 0.0, "excerpt": None})
        bucket["minutes"] += _parse_block_minutes(parts[1])
        if not bucket["excerpt"]:
            bucket["excerpt"] = m.group(1).strip()
    if not by_app:
        return []
    app_label, data = max(
        by_app.items(), key=lambda kv: (float(kv[1]["minutes"]), kv[0])
    )
    minutes = float(data["minutes"])
    if minutes < 10.0 or not data["excerpt"]:
        return []
    from .screenpipe_source import normalize_app_name

    app = normalize_app_name(app_label) or app_label or "AI"
    return [f"- {app}: 「{data['excerpt']}」（画面テキストより・約{_fmt_minutes(minutes)}）"]


def generate_nippou_deterministic(
    stats: dict,
    tz: tzinfo,
    intent: str | None = None,
    min_block_minutes: float = 15.0,
    *,
    open_kzn_actions: Sequence[tuple[str, str]] | None = None,
    activity_md: str | None = None,
) -> str:
    """統計JSONから事実ベースの日報ドラフトを組み立てる。

    open_kzn_actions: (KZN-ID, 平文化済み行動文) の未チェック上位。
    activity_md: タイムラインに載った画面テキスト補完を読むため（任意）。
    """
    lines = [NIPPOU_MARKER_HEADING, ""]

    # ---- 【本日の業務】----
    lines.append("【本日の業務】")
    work: list[str] = []
    goal = stats.get("goal_text")
    if isinstance(goal, str) and goal.strip():
        work.append(f"- 目標: {goal.strip()}")
    work.extend(_project_work_lines(stats))
    work.extend(_screenpipe_work_lines(stats, activity_md))
    # スクリーン補完（15分以上・エンタメ除外・最大3）
    work.extend(
        _screen_block_lines(stats, tz, min_block_minutes=max(15.0, min_block_minutes), limit=3)
    )
    if not work:
        work.append("- （本日の計測データがありません）")
    lines.extend(work)
    lines.append("")

    # ---- 【成果・進捗】----
    lines.append("【成果・進捗】")
    # include_total=True のため常に合計行を含む（コミット・テスト無しなら合計行のみ）
    outcomes = _outcome_lines(stats, include_total=True, intent=intent)
    lines.extend(outcomes)
    lines.append("")

    # ---- 【明日の予定】----
    lines.append("【明日の予定】")
    tomorrow: list[str] = []
    unchecked = _unchecked_tasks(intent)
    for t in unchecked[:3]:
        tomorrow.append(f"- {t}")
    for kid, body in list(open_kzn_actions or [])[:2]:
        snippet = " ".join(str(body).split())
        # §R3: 40字超は 39字 +「…」（無印切詰めをやめる）
        if len(snippet) > 40:
            snippet = snippet[:39] + "…"
        tomorrow.append(f"- {kid}: {snippet}")
    if not tomorrow:
        tomorrow.append("- 引き続き上記対応")
    lines.extend(tomorrow)
    lines.append("")
    return "\n".join(lines)
