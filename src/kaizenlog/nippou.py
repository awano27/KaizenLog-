"""日報ドラフトの自動生成。

活動ログから提出用の日報下書きを作る。2モード:
- 決定的モード: 統計JSONから事実ベースの箇条書きを組み立てる（LLM不要・0秒）
- LLMモード:   活動ログ＋計画を渡して自然な文章に仕上げる
"""

from __future__ import annotations

import re
from datetime import datetime, tzinfo

from .advisor import generate_text
from .config import LLMConfig

NIPPOU_MARKER_HEADING = "## 📝 日報ドラフト"

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


def generate_nippou_llm(
    cfg: LLMConfig, activity_md: str, intent: str | None, redactor=None
) -> str:
    prompt = build_nippou_prompt(activity_md, intent)
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


def generate_nippou_deterministic(
    stats: dict, tz: tzinfo, intent: str | None = None, min_block_minutes: float = 15.0
) -> str:
    """統計JSONから事実ベースの日報ドラフトを組み立てる。"""
    lines = [NIPPOU_MARKER_HEADING, ""]

    lines.append("【本日の業務】")
    blocks = [
        b for b in stats.get("blocks", [])
        if b.get("minutes", 0) >= min_block_minutes and not _is_private(b)
    ]
    blocks.sort(key=lambda b: -b.get("minutes", 0))
    for b in blocks[:6]:
        try:
            hour = datetime.fromisoformat(b["start"]).astimezone(tz).hour
            when = "午前" if hour < 12 else "午後"
        except (KeyError, ValueError):
            when = ""
        title = b.get("title") or b.get("app", "")
        lines.append(f"- {when}: {title}（{b.get('category', '')}、約{_fmt_minutes(b.get('minutes', 0))}）")
    if len(blocks) == 0:
        lines.append("- （15分以上の作業ブロックなし）")
    lines.append("")

    checked = _checked_tasks(intent)
    lines.append("【成果・進捗】")
    if checked:
        lines.extend(f"- {t} を完了" for t in checked[:3])
    else:
        # 合計にも私的時間（エンタメ等）を含めない。【本日の業務】から除外した
        # 時間を合計だけに足すと、提出用日報の作業時間が水増しされる
        private = sum(
            m for cat, m in stats.get("by_category", {}).items()
            if cat in _PRIVATE_CATEGORIES
        )
        total = max(0.0, stats.get("total_minutes", 0) - private)
        lines.append(f"- 合計 {_fmt_minutes(total)} の作業を実施")
    ai = stats.get("ai", {})
    if ai.get("sessions", 0) > 0:
        lines.append(f"- AIエージェント（Claude Code）を{ai['sessions']}セッション活用")
    lines.append("")

    lines.append("【明日の予定】")
    unchecked = _unchecked_tasks(intent)
    if unchecked:
        lines.extend(f"- {t}" for t in unchecked[:3])
    else:
        lines.append("- 引き続き上記対応")
    lines.append("")
    return "\n".join(lines)
