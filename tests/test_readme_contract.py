from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
LOCAL_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HTML_IMG_SRC_RE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def _local_targets(text: str) -> list[str]:
    return [*LOCAL_LINK_RE.findall(text), *HTML_IMG_SRC_RE.findall(text)]


def test_readme_tells_the_current_improvement_loop_story():
    text = README.read_text(encoding="utf-8")
    for required in (
        "今日の PC 作業が、毎晩そのまま日誌になる。",
        "やり直しのムダを測る",
        "効果の高い依頼方法を見つける",
        "学んだルールを次の AI へ渡す",
        "改善効果を実測する",
        "kaizenlog prompts --roi",
        "kaizenlog handoff",
        "kaizenlog coach",
        "kaizenlog abtest",
        "M365 Copilot",
        "Next / Planned",
        "現在未実装",
    ):
        assert required in text
    assert "Local Preview" not in text
    assert "実測で調教" not in text


def test_readme_local_targets_exist():
    text = README.read_text(encoding="utf-8")
    missing: list[str] = []
    for raw in _local_targets(text):
        target = raw.strip().split(maxsplit=1)[0]
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path_text = unquote(target.split("#", 1)[0])
        if path_text and not (ROOT / path_text).resolve().exists():
            missing.append(target)
    assert missing == []


def test_readme_html_images_reference_the_four_landing_assets():
    text = README.read_text(encoding="utf-8")
    sources = HTML_IMG_SRC_RE.findall(text)
    local_sources = [
        source for source in sources if not source.startswith(("http://", "https://"))
    ]
    expected = {
        "./assets/readme/hero.svg",
        "./assets/readme/section-start.svg",
        "./assets/readme/section-loop.svg",
        "./assets/readme/workflow.svg",
    }

    assert len(local_sources) == len(expected)
    assert set(local_sources) == expected
    for source in local_sources:
        assert (ROOT / unquote(source)).resolve().is_file()


def test_readme_enforces_current_runtime_and_coach_boundaries():
    text = README.read_text(encoding="utf-8")
    for required in (
        "`kaizenlog run` は `generate` と `advise` を順に実行します。",
        '`backend = "none"` の状態で `advise` を呼ぶと、LLM 生成を行わずエラーとして終了します。',
        "日々の LLM 提案は現在の契約どおり1〜3件です。",
        "設定した endpoint（ローカル／リモートどちらもありうる）",
        "通常の `coach` は提案ファイルと diff を作成します。管理対象の coach 区間へ書き込むのは `coach --apply <proposal-file>` だけです。",
    ):
        assert required in text

    for forbidden in (
        "LLMバックエンドが`none`なら、Activity Logだけを生成して提案はスキップします。",
        "runが提案をスキップします",
        "設定したローカルAPIへ送信",
        "openai-compatibleは常にローカル",
        "coachは提案を自動適用",
        "coach --applyなしで書き込む",
    ):
        assert forbidden not in text


def test_readme_keeps_m365_as_an_unimplemented_plan():
    text = README.read_text(encoding="utf-8")
    marker = "### ブラウザ AI と M365 Copilot"
    assert marker in text
    section = text.split(marker, 1)[1].split("\n---", 1)[0]

    for required in (
        "| **Next / Planned** | M365 Copilot 改善アシスト（**現在未実装**） |",
        "M365 Copilot は、会話の自動取得、Microsoft Graph／テナント連携、自動送信、カスタム指示の自動変更にまだ対応していません。",
        "customize-how-microsoft-365-copilot-responds-to-you",
        "declarative-agent-instructions",
    ):
        assert required in section

    for forbidden in (
        "M365 Copilot改善アシストは実装済みです。",
        "会話の自動取得に対応しています。",
        "Microsoft Graph／テナント連携に対応しています。",
        "自動送信に対応しています。",
        "カスタム指示の自動変更に対応しています。",
    ):
        assert forbidden not in section


def test_readme_svg_assets_are_well_formed_and_self_contained():
    for name in ("hero.svg", "section-start.svg", "section-loop.svg", "workflow.svg"):
        path = ROOT / "assets" / "readme" / name
        ET.parse(path)
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "<foreignObject",
            "<script",
            "@font-face",
            "<image",
            'href="http://',
            'href="https://',
        ):
            assert forbidden not in text


def test_readme_presents_three_ai_optional_daily_journal_stories():
    text = README.read_text(encoding="utf-8")
    marker = "## 📅 毎日の使い方"
    assert marker in text
    section = text.split(marker, 1)[1].split("\n---", 1)[0]

    for required in (
        "AI を使わない日でも",
        "朝に目標を書く → 日中は自動記録 → 夜に日誌を作る → 必要な日だけ AI 提案 → 翌日 `done`。",
        "数値と文章はすべて架空の例です。実在する利用者のデータ、導入実績、改善効果ではありません。",
        "**1. ソフトウェア開発者**",
        "6時間12分｜実装 3時間05分｜レビュー 1時間20分｜会議 50分",
        "会議後に開発へ戻るまで時間がかかった",
        "午前中にレビュー対応を終える",
        "**2. 企画・営業**",
        "5時間48分｜顧客会議 2時間10分｜提案書 1時間45分｜調査 1時間05分",
        "会議が続き、提案書の作成が夕方に偏った",
        "最初の会議までに提案書の骨子を作る",
        "**3. ライター・研究者**",
        "5時間30分｜執筆 2時間35分｜調査 1時間50分｜推敲 45分",
        "午前の調査が長引いたが、午後は執筆に集中できた",
        "調査を90分で区切り、初稿へ進む",
        "各例のカテゴリ時間は主な内訳であり、合計時間の完全な内訳ではありません。",
        "Activity Log は ActivityWatch が観測した PC 前景活動の記録です。生活全体の記録ではなく、勤務時間・目標達成・集中力・生産性を判定するものでもありません。",
        "スマートフォン、他デバイス、離席中の行動は既定では測定できません。",
        "入力 watcher から統計を取得できる場合は、25分以上入力が続いた区間を「集中ブロック」として表示します。",
        'kaizenlog goal "提案書の初稿を完成させる @執筆・ノート"',
        "## 振り返り",
        "管理マーカー区間だけを更新し、その外側の手書き本文を置換しません。",
        "# 夜 — 日誌のみ（LLM 不要）",
        "振り返りを翌日の改善提案へ反映するときだけ、設定済みの LLM バックエンドを使います。",
    ):
        assert required in section

    assert section.count("- 自動記録:") == 3
    assert section.count("- 自分の振り返り（手書き）:") == 3
    assert section.count("- 明日の目標（自分で設定）:") == 3

    role_headings = (
        "**1. ソフトウェア開発者**",
        "**2. 企画・営業**",
        "**3. ライター・研究者**",
    )
    role_blocks = []
    for index, heading in enumerate(role_headings):
        block_start = section.index(heading) + len(heading)
        block_end = (
            section.index(role_headings[index + 1], block_start)
            if index + 1 < len(role_headings)
            else section.index("</details>", block_start)
        )
        role_blocks.append(section[block_start:block_end])

    for block in role_blocks:
        assert block.count("- 自動記録:") == 1
        assert block.count("- 自分の振り返り（手書き）:") == 1
        assert block.count("- 明日の目標（自分で設定）:") == 1

    for forbidden in (
        "### こんな日誌が残ります",
        "**集中ブロック**: 3回",
        "15分以上",
        "Activity Logは勤務時間です",
        "スマートフォンの行動も記録します",
        "振り返りを自動生成します",
        "AIが明日の目標を決めます",
        "日誌の作成にはLLMが必須です",
    ):
        assert forbidden not in section
