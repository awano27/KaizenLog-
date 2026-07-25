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
    t = re.sub(r"https?://\S+", "<url>", t)  # パス置換より先（URL内の/をパス扱いしない）
    # パス文字は明示的なASCIIに限定する。[^\s]は日本語にもマッチするため、
    # 空白のない日本語文中のパスから文末までを丸ごと飲み込み、無関係な依頼文が
    # 同一クラスタに潰れて偽の「頻出パターン」を報告してしまう
    t = re.sub(r"[a-z]:[\\/][a-z0-9_.\-\\/]+|/?[a-z0-9_.\-]+(?:/[a-z0-9_.\-]+)+",
               "<path>", t)  # ファイルパス
    t = re.sub(r"\d+", "#", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:200]


def cluster_prompts(
    prompts: list[UserPrompt], similarity: float = DEFAULT_SIMILARITY
) -> list[PromptCluster]:
    """貪欲法で類似プロンプトをクラスタにまとめる。

    貪欲法は処理順に結果が左右されるため、正規化文でソートしてから
    処理し、同じ入力集合なら並び順によらず同じクラスタを返す。
    """
    clusters: list[PromptCluster] = []
    ordered = sorted(
        ((normalize(p.text), p) for p in prompts),
        key=lambda x: (x[0], x[1].timestamp.isoformat(), x[1].text),
    )
    for norm, p in ordered:
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
        # ソース付きラベル（例: vault (codex)）でクラスタ見出しに内訳を出す
        label = p.project
        if getattr(p, "source", None) and p.source != "claude-code":
            label = f"{p.project} ({p.source})"
        best.projects.add(label)
        # タイムスタンプはUTC。UTCのまま日付を取ると日本の夕方〜深夜の依頼が
        # 別日に割れて「◯日間で反復」の判定（提案の強さ）がずれる
        best.days.add(p.timestamp.astimezone().date().isoformat())
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
            "\n構造化AIテレメトリの依頼が見つかりませんでした。"
            "`[aiwork] claude_projects_dir` / `codex_sessions_dir` を確認してください。\n"
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
