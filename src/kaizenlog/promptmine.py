"""プロンプト資産化: Claude Codeへの依頼文から「繰り返しパターン」を発掘する。

似た依頼を何度も打っている＝テンプレ化・スキル化で複利が効く場所。
標準ライブラリのみ（difflib）で類似クラスタリングを行い、候補をレポートする。
発掘結果は /kaizen-autopilot がスキル生成の入力として使う。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .aiwork import UserPrompt

DEFAULT_SIMILARITY = 0.6
DEFAULT_MIN_COUNT = 3


@dataclass
class PromptCluster:
    representative: str  # 正規化済みの代表文（比較用）
    example: str  # 生の例文（表示用）
    count: int = 0
    projects: set[str] = field(default_factory=set)
    days: set[str] = field(default_factory=set)


def normalize(text: str) -> str:
    """比較用に依頼文を正規化する。数値・パス・空白の違いを吸収する。"""
    t = text.lower()
    t = re.sub(r"[a-z]:[\\/][^\s]+|/[^\s]+/[^\s]+", "<path>", t)  # ファイルパス
    t = re.sub(r"https?://\S+", "<url>", t)
    t = re.sub(r"\d+", "#", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:200]


def cluster_prompts(
    prompts: list[UserPrompt], similarity: float = DEFAULT_SIMILARITY
) -> list[PromptCluster]:
    """貪欲法で類似プロンプトをクラスタにまとめる。"""
    clusters: list[PromptCluster] = []
    for p in prompts:
        norm = normalize(p.text)
        if not norm:
            continue
        best: PromptCluster | None = None
        best_ratio = 0.0
        for c in clusters:
            ratio = SequenceMatcher(None, norm, c.representative).ratio()
            if ratio >= similarity and ratio > best_ratio:
                best, best_ratio = c, ratio
        if best is None:
            best = PromptCluster(representative=norm, example=p.text)
            clusters.append(best)
        best.count += 1
        best.projects.add(p.project)
        best.days.add(p.timestamp.date().isoformat())
    return clusters


def _suggestion(c: PromptCluster) -> str:
    if c.count >= 5 and len(c.days) >= 3:
        return "スキル化を強く推奨（.claude/skills/ に切り出せば1コマンドになる）"
    if len(c.days) >= 2:
        return "テンプレ化候補（頻出の前提・制約をCLAUDE.mdかテンプレに固定する）"
    return "同日内の反復。依頼をまとめて1タスクとして渡すことを検討"


def render_prompt_report(
    prompts: list[UserPrompt],
    days: int,
    min_count: int = DEFAULT_MIN_COUNT,
    max_clusters: int = 10,
) -> str:
    header = f"# 💬 プロンプト資産化レポート（過去{days}日・依頼{len(prompts)}件）\n"
    if not prompts:
        return header + (
            "\nClaude Codeの依頼が見つかりませんでした。"
            "`[aiwork] claude_projects_dir` の設定を確認してください。\n"
        )
    clusters = [c for c in cluster_prompts(prompts) if c.count >= min_count]
    clusters.sort(key=lambda c: -c.count)
    if not clusters:
        return header + f"\n{min_count}回以上繰り返された依頼パターンはありません。\n"

    lines = [header]
    lines.append(f"{min_count}回以上繰り返された依頼: {len(clusters)}パターン\n")
    for i, c in enumerate(clusters[:max_clusters], 1):
        example = c.example.replace("\n", " ")
        if len(example) > 100:
            example = example[:97] + "..."
        lines.append(f"## {i}. {c.count}回 / {len(c.days)}日 / {', '.join(sorted(c.projects))}")
        lines.append("")
        lines.append(f"> {example}")
        lines.append("")
        lines.append(f"- 提案: {_suggestion(c)}")
        lines.append("")
    if len(clusters) > max_clusters:
        lines.append(f"（他 {len(clusters) - max_clusters} パターン省略）")
    return "\n".join(lines)
