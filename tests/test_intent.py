from kaizenlog.advisor import build_prompt
from kaizenlog.vault import extract_heading_section

NOTE = """---
date: 2026-07-05
---

# 2026年07月05日（日）

## Today's Focus
- KaizenLog v0.3 を仕上げる
- AI-NEWSのスクレイパー修正

## Notes
- 打ち合わせは13時から

## Tasks
- [x] スクレイパーのバグ修正
- [ ] READMEの更新

## Reflections
-
"""


def test_extract_todays_focus():
    section = extract_heading_section(NOTE, "Today's Focus")
    assert section is not None
    assert "KaizenLog v0.3" in section
    assert "打ち合わせ" not in section  # 次の見出し以降は含まない


def test_extract_tasks_with_checkboxes():
    section = extract_heading_section(NOTE, "Tasks")
    assert section is not None
    assert "[x] スクレイパーのバグ修正" in section
    assert "Reflections" not in section


def test_extract_is_case_insensitive():
    assert extract_heading_section(NOTE, "today's focus") is not None


def test_extract_missing_heading_returns_none():
    assert extract_heading_section(NOTE, "存在しない見出し") is None


def test_extract_empty_section_returns_none():
    note = "## Today's Focus\n\n## Notes\n中身\n"
    assert extract_heading_section(note, "Today's Focus") is None


def test_extract_nested_subheadings_included():
    note = "## Plan\n### 午前\n- A\n### 午後\n- B\n## Notes\n- C\n"
    section = extract_heading_section(note, "Plan")
    assert "午前" in section and "午後" in section and "- C" not in section


def test_build_prompt_with_intent():
    prompt = build_prompt("ログ本文", [], intent="## Today's Focus\n- 計画X")
    assert "本日の計画" in prompt
    assert "計画X" in prompt
    assert prompt.index("計画X") < prompt.index("ログ本文")


def test_build_prompt_without_intent():
    prompt = build_prompt("ログ本文", [])
    assert "本日の計画" not in prompt
    assert "ログ本文" in prompt
