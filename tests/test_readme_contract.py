from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
LOCAL_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


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
    for raw in LOCAL_LINK_RE.findall(text):
        target = raw.strip().split(maxsplit=1)[0]
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path_text = unquote(target.split("#", 1)[0])
        if path_text and not (ROOT / path_text).resolve().exists():
            missing.append(target)
    assert missing == []


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
