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
        "AIとの仕事を、実測で調教する。",
        "やり直しのムダを測る",
        "効果の高い依頼方法を見つける",
        "学んだルールを次のAIへ渡す",
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
    expected = {
        "./assets/readme/hero.svg",
        "./assets/readme/section-start.svg",
        "./assets/readme/section-loop.svg",
        "./assets/readme/workflow.svg",
    }

    assert len(sources) == len(expected)
    assert set(sources) == expected
    for source in sources:
        assert (ROOT / unquote(source)).resolve().is_file()


def test_readme_enforces_current_runtime_and_coach_boundaries():
    text = README.read_text(encoding="utf-8")
    for required in (
        "`kaizenlog run`は`generate`と`advise`を順に実行します。",
        '`backend = "none"`の状態で`advise`を呼ぶと、LLM生成を行わずエラーとして終了します。',
        "日々のLLM提案は現在の契約どおり1〜3件です。",
        "設定したendpointへ送信する。endpointはローカルまたはリモートの場合がある",
        "通常の`coach`は提案ファイルとdiffを作成します。管理対象のcoach区間へ書き込むのは`coach --apply <proposal-file>`だけです。",
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
    marker = "### Next / Planned — M365 Copilot改善アシスト"
    assert marker in text
    section = text.split(marker, 1)[1].split("\n---", 1)[0]

    for required in (
        "> **現在未実装です。**",
        "会話の自動取得、Microsoft Graph／テナント連携、自動送信、カスタム指示の自動変更にはまだ対応していません。",
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
