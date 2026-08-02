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
from .privacy_filter import PRIVATE_CATEGORIES as _PRIVATE_CATEGORIES
from .privacy_filter import is_private_block as _is_private

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


_LEADING_ELLIPSIS_RE = re.compile(r"^(?:\.{2,3}|…)+")
_MIN_WORK_TITLE_LEN = 8


def _display_work_title(title: str, *, max_chars: int = 48) -> str | None:
    """日報用の依頼タイトル。先頭 `...` を落とし、短文は捨て、長い文は末尾優先。"""
    t = " ".join((title or "").split()).strip()
    t = _LEADING_ELLIPSIS_RE.sub("", t).strip()
    if len(t) < _MIN_WORK_TITLE_LEN:
        return None
    if len(t) <= max_chars:
        return t
    # 先頭切りは「何をしたか」が消えるので末尾を残す
    return "…" + t[-(max_chars - 1) :]


def _project_work_lines(stats: Mapping[str, Any], *, limit: int = 5) -> list[str]:
    """業務行: effort がある日は成果ベース、無い日は旧セッション集約へフォールバック。"""
    effort = stats.get("effort") if isinstance(stats.get("effort"), Mapping) else None
    mins_map = (
        effort.get("minutes")
        if isinstance(effort, Mapping) and isinstance(effort.get("minutes"), Mapping)
        else None
    )
    if mins_map:
        return _project_work_lines_from_effort(stats, mins_map, limit=limit)
    return _project_work_lines_legacy(stats, limit=limit)


_ROUND_IN_SUBJECT_RE = re.compile(r"\s*\(round\s+\d+\)\s*", re.IGNORECASE)
# ハード切詰め済みの途中切れ "(round 4" / "(round" も落とす
_ROUND_TRAILING_RE = re.compile(r"\s*\(round\s*\d*$", re.IGNORECASE)
_CONVENTIONAL_TYPE_RE = re.compile(
    r"^(?P<type>feat|fix|docs|style|refactor|perf|test|build|ci|chore|add)"
    r"(?:\([^)]*\))?\s*:\s*",
    re.IGNORECASE,
)


def _theme_from_subject(subj: str) -> str | None:
    """conventional commit の type: 以降をテーマに。内部 (round N) は落とす。"""
    s = " ".join(str(subj or "").split()).strip()
    if not s:
        return None
    s = _ROUND_IN_SUBJECT_RE.sub(" ", s).strip()
    s = _ROUND_TRAILING_RE.sub("", s).strip()
    m = _CONVENTIONAL_TYPE_RE.match(s)
    if m:
        s = s[m.end() :].strip()
    # 末尾の切れかけ省略記号・断片は落とす
    s = s.rstrip(" …./-")
    if not s or len(s) < 4:
        return None
    return s


def _project_themes(
    stats: Mapping[str, Any],
    project: str,
) -> list[str]:
    """テーマ候補: コミット subject → prompts_digest → 空。"""
    themes: list[str] = []
    og = stats.get("outcome_git")
    if isinstance(og, list):
        for item in og:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("repo_label") or "") != project:
                continue
            subjects = item.get("subjects") if isinstance(item.get("subjects"), list) else []
            for raw in subjects:
                t = _theme_from_subject(str(raw))
                if t and t not in themes:
                    themes.append(t)
                if len(themes) >= 3:
                    return themes
    if themes:
        return themes
    # prompts_digest フォールバック
    for d in _session_digests(stats):
        if str(d.get("project") or "").strip() != project:
            continue
        for p in d.get("prompts_digest") or []:
            t = " ".join(str(p).split()).strip()
            if t and t not in themes:
                themes.append(t[:40] + ("…" if len(t) > 40 else ""))
            if len(themes) >= 2:
                return themes
    return themes


def _project_work_lines_from_effort(
    stats: Mapping[str, Any],
    mins_map: Mapping[str, Any],
    *,
    limit: int = 5,
) -> list[str]:
    """effort 時間 + テーマ（subject/prompts）+ 規模（コミット/編集/テスト）。"""
    from .effort import (
        BUCKET_AI_GENERAL,
        BUCKET_PRIVATE,
        BUCKET_RESEARCH,
        BUCKET_UNCLASSIFIED,
    )

    # digests から project 別 files/edits/tests
    by_proj: dict[str, dict[str, Any]] = {}
    for d in _session_digests(stats):
        project = str(d.get("project") or "").strip()
        if not project or project in (BUCKET_AI_GENERAL,):
            continue
        bucket = by_proj.setdefault(
            project,
            {"edits": 0, "files": [], "tests": 0},
        )
        source = str(d.get("source") or "")
        tools_ok = d.get("tools_total") is not None and not source.endswith("-web")
        if tools_ok:
            bucket["edits"] += int(d.get("edits") or 0)
        for f in d.get("files_touched") or []:
            fs = str(f).strip()
            if fs and fs not in bucket["files"]:
                bucket["files"].append(fs)
        if d.get("tests_run"):
            bucket["tests"] += 1

    # commits / ins/del by repo_label
    commits: dict[str, int] = {}
    ins_map: dict[str, int] = {}
    del_map: dict[str, int] = {}
    og = stats.get("outcome_git")
    if isinstance(og, list):
        for item in og:
            if not isinstance(item, Mapping):
                continue
            label = str(item.get("repo_label") or "")
            if label:
                commits[label] = commits.get(label, 0) + int(item.get("commits") or 0)
                ins_map[label] = ins_map.get(label, 0) + int(item.get("insertions") or 0)
                del_map[label] = del_map.get(label, 0) + int(item.get("deletions") or 0)

    skip_buckets = {
        BUCKET_PRIVATE,
        BUCKET_AI_GENERAL,
        BUCKET_UNCLASSIFIED,
        BUCKET_RESEARCH,
        "（ほか）",
    }
    work_items = [
        (str(k), float(v or 0))
        for k, v in mins_map.items()
        if str(k) not in skip_buckets and float(v or 0) > 0
    ]
    work_items.sort(key=lambda x: (-x[1], x[0]))

    lines: list[str] = []
    for project, mins in work_items[:limit]:
        meta = by_proj.get(project) or {}
        edits = int(meta.get("edits") or 0)
        c_n = commits.get(project, 0)
        t_n = int(meta.get("tests") or 0)
        themes = _project_themes(stats, project)
        scale_bits: list[str] = []
        if c_n:
            ins = ins_map.get(project, 0)
            dels = del_map.get(project, 0)
            scale_bits.append(f"コミット{c_n}件 +{ins:,}/-{dels:,}")
        if edits:
            scale_bits.append(f"{edits}編集")
        if t_n:
            scale_bits.append(f"テスト{t_n}回")
        if themes:
            theme_text = "・".join(themes[:3])
            if scale_bits:
                body = f"{theme_text}（{'、'.join(scale_bits)}）"
            else:
                body = theme_text
        else:
            # ファイル名フォールバック
            files = list(meta.get("files") or [])[:3]
            bits: list[str] = []
            if files:
                more = " ほか" if len(meta.get("files") or []) > 3 else ""
                bits.append("・".join(files) + more)
            bits.extend(scale_bits)
            body = "、".join(bits) if bits else "作業"
        lines.append(f"- {project}（{_fmt_minutes(mins)}）: {body}")

    other_mins = 0.0
    for key in (BUCKET_RESEARCH, BUCKET_UNCLASSIFIED, BUCKET_AI_GENERAL):
        other_mins += float(mins_map.get(key) or 0)
    if other_mins > 0:
        lines.append(f"- ほか 調査・未分類 {_fmt_minutes(other_mins)}")
    return lines


def _project_work_lines_legacy(stats: Mapping[str, Any], *, limit: int = 5) -> list[str]:
    """effort 欠如日: セッション数・往復数形式（プロンプト断片は出さない）。"""
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
                "files": [],
            },
        )
        bucket["sessions"] += 1
        bucket["turns"] += int(d.get("user_turns") or 0)
        tools_total = d.get("tools_total")
        source = str(d.get("source") or "")
        tools_ok = tools_total is not None and not source.endswith("-web")
        if tools_ok:
            bucket["edits"] += int(d.get("edits") or 0)
            bucket["edits_known"] = True
        for f in d.get("files_touched") or []:
            fs = str(f).strip()
            if fs and fs not in bucket["files"]:
                bucket["files"].append(fs)

    ranked = sorted(
        by_proj.items(),
        key=lambda kv: (-int(kv[1]["turns"]), -int(kv[1]["sessions"]), kv[0]),
    )
    lines: list[str] = []
    for project, data in ranked[:limit]:
        if data["edits_known"]:
            meta = (
                f"セッション{data['sessions']}回・往復{data['turns']}"
                f"・編集{data['edits']}"
            )
        else:
            meta = f"セッション{data['sessions']}回・往復{data['turns']}"
        files = data["files"][:3]
        if files:
            lines.append(
                f"- {project}: {', '.join(files)}"
                f"{' ほか' if len(data['files']) > 3 else ''}（{meta}）"
            )
        else:
            lines.append(f"- {project}: （{meta}）")
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
            # 切詰め済み subject 同士を ' / ' 連結しない（1件に絞る）
            subj = ""
            for s in subjects:
                t = _theme_from_subject(str(s)) or str(s).strip()
                t = _ROUND_TRAILING_RE.sub("", t).strip().rstrip(" …./-")
                if t:
                    subj = t
                    break
            if subj:
                lines.append(
                    f"- {label}: コミット{commits}件（+{ins}/-{dels}）"
                    f"主な内容: {subj}"
                )
            elif commits > 0:
                lines.append(f"- {label}: コミット{commits}件（+{ins}/-{dels}）")

    # テスト実行を伴うセッション
    test_n = sum(
        1 for d in _session_digests(stats) if d.get("tests_run")
    )
    if test_n > 0:
        lines.append(f"- テスト実行を伴うセッション {test_n}回")

    # 目標カテゴリ実測（カテゴリ指定がある日のみ・R6 共有ヘルパー）
    goal_cat = stats.get("goal_category")
    if isinstance(goal_cat, str) and goal_cat.strip():
        from .stats import goal_category_minutes

        raw_m = goal_category_minutes(stats, goal_cat.strip())
        if raw_m is not None:
            lines.append(
                f"- 目標カテゴリ実測: {goal_cat.strip()} {_fmt_minutes(float(raw_m))}"
            )

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
    from .screenpipe_source import extract_screen_text_excerpt, normalize_app_name

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
        excerpt = extract_screen_text_excerpt(parts[4])
        if not excerpt:
            continue
        bucket = by_app.setdefault(parts[3], {"minutes": 0.0, "excerpt": None})
        bucket["minutes"] += _parse_block_minutes(parts[1])
        if not bucket["excerpt"]:
            bucket["excerpt"] = excerpt
    if not by_app:
        return []
    app_label, data = max(
        by_app.items(), key=lambda kv: (float(kv[1]["minutes"]), kv[0])
    )
    minutes = float(data["minutes"])
    if minutes < 10.0 or not data["excerpt"]:
        return []

    app = normalize_app_name(app_label) or app_label or "AI"
    return [f"- {app}: 「{data['excerpt']}」（画面テキストより・約{_fmt_minutes(minutes)}）"]


def _truncate_action_for_tomorrow(body: str, *, limit: int = 60) -> str:
    """アクション文の切詰め。`→` があるときトリガー/動作を別々に切る。

    行頭が `…` で始まらない。
    """
    text = " ".join(str(body).split()).strip()
    if not text:
        return ""
    if "→" in text:
        left, right = text.split("→", 1)
        left = left.strip()
        right = right.strip()
        if len(left) > 20:
            left = left[:20] + "…"
        if len(right) > 36:
            right = right[:36] + "…"
        return f"{left} → {right}"
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _impression_line(stats: Mapping[str, Any]) -> str | None:
    """【所感】決定論1行。評価語なし。材料が無ければ None。"""
    digests = _session_digests(stats)
    if not digests:
        return None
    heavy = [
        d
        for d in digests
        if int(d.get("user_turns") or 0) >= 20
    ]
    if not heavy:
        # ツールエラー集中など別の事実
        total_err = 0
        ai = stats.get("ai") if isinstance(stats.get("ai"), Mapping) else {}
        if isinstance(ai, Mapping) and isinstance(ai.get("tool_errors"), (int, float)):
            total_err = int(ai.get("tool_errors") or 0)
        if total_err >= 10:
            return f"- ツールエラーが合計{total_err}回記録された"
        return None
    heavy.sort(key=lambda d: -int(d.get("user_turns") or 0))
    top = heavy[0]
    turns = int(top.get("user_turns") or 0)
    edits_top = int(top.get("edits") or 0)
    edits_all = sum(int(d.get("edits") or 0) for d in digests)
    if edits_all > 0 and edits_top > 0:
        share = int(round(edits_top / edits_all * 100))
        if share >= 10 and share % 10 == 0:
            share_txt = f"{share // 10}割"
        else:
            share_txt = f"約{share}%"
        return (
            f"- AI作業のうち往復{turns}回のセッションが1件あり、"
            f"当日の編集の{share_txt}を占めた"
        )
    return f"- AI作業のうち往復{turns}回のセッションが1件あった"


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
        ach = stats.get("goal_achieved")
        ach_part = ""
        if isinstance(ach, (int, float)):
            n = max(0, min(100, int(ach)))
            ach_part = f" ｜ 達成度: {n}%（自己申告）"
        else:
            ach_part = " ｜ 達成度: 未申告（kaizenlog goal --achieved N で記録）"
        work.append(f"- 目標: {goal.strip()}{ach_part}")
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
    # アクション節と役割分担: 日報は平文1行（KZN-ID なし）。詳細は 📌 側。
    lines.append("【明日の予定】")
    tomorrow: list[str] = []
    unchecked = _unchecked_tasks(intent)
    for t in unchecked[:2]:
        tomorrow.append(f"- {t}")
    if not tomorrow and open_kzn_actions:
        body = _truncate_action_for_tomorrow(str(open_kzn_actions[0][1]))
        if body:
            tomorrow.append(f"- {body}")
    if not tomorrow:
        tomorrow.append("- 引き続き上記対応")
    lines.extend(tomorrow)
    lines.append("")

    # ---- 【所感】----
    impression = _impression_line(stats)
    if impression:
        lines.append("【所感】")
        lines.append(impression)
        lines.append("")

    return "\n".join(lines)
