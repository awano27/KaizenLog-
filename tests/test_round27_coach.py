"""第27弾 §D: コパイロット調教パック。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from kaizenlog.advisor import AdvisorError, INTERNAL_SENTINEL_TOKEN, load_bundled_prompt
from kaizenlog.coach import (
    apply_proposal,
    parse_coach_json,
    proposal_diff_text,
    run_coach_llm,
    save_proposal,
)
from kaizenlog.config import Config, LLMConfig
from kaizenlog.vault import COACH_MARKER, extract_section


def test_d4_sentinel_in_coach_prompt():
    text = load_bundled_prompt("coach")
    assert text.lstrip().startswith(INTERNAL_SENTINEL_TOKEN) or INTERNAL_SENTINEL_TOKEN in text[:80]


def test_d4_json_contract_accept_and_reject():
    good = """{
      "claude_md_append": "- a\\n- b\\n- c",
      "evidence": [{"fact_id": "F1", "value": "3"}]
    }"""
    data = parse_coach_json(good)
    assert "claude_md_append" in data
    with pytest.raises(AdvisorError):
        parse_coach_json("just free text without json")
    with pytest.raises(AdvisorError):
        parse_coach_json('{"claude_md_append": "one line only", "evidence": [{"a":1}]}')


def test_d4_retry_on_bad_then_ok():
    cfg = Config(llm=LLMConfig(backend="none", retries=1))
    calls = {"n": 0}

    def gen(_cfg, _sys, _user):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json"
        return (
            '{"claude_md_append": "- rule1\\n- rule2\\n- rule3",'
            ' "evidence": [{"fact_id": "F", "value": "1"}]}'
        )

    data = run_coach_llm(cfg, "# ctx", generate_fn=gen)
    assert calls["n"] == 2
    assert "rule1" in data["claude_md_append"]


def test_d4_apply_idempotent_and_double_reject(tmp_path: Path):
    mem = tmp_path / "mem"
    prop = save_proposal(
        mem,
        as_of=date(2026, 7, 29),
        append_md="- 短い依頼を避ける\n- リトライ前に原因を書く\n- テストを先に",
        evidence=[{"fact_id": "F", "value": "1"}],
    )
    target = tmp_path / "CLAUDE.md"
    target.write_text("# 手書き方針\n\n変えないで\n", encoding="utf-8")
    apply_proposal(prop, [target])
    text1 = target.read_text(encoding="utf-8")
    assert "変えないで" in text1
    assert extract_section(text1, COACH_MARKER) is not None
    assert "短い依頼" in (extract_section(text1, COACH_MARKER) or "")
    # 二重適用拒否
    with pytest.raises(AdvisorError):
        apply_proposal(prop, [target])
    # 冪等: 区間内容は変わらない
    text2 = target.read_text(encoding="utf-8")
    assert extract_section(text1, COACH_MARKER) == extract_section(text2, COACH_MARKER)


def test_d4_diff_text():
    d = proposal_diff_text("- a\n- b\n- c")
    assert "+" in d
    assert COACH_MARKER in d
