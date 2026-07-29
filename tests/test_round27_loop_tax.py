"""第27弾 §C: ループ税メーター。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kaizenlog.aiwork import (
    AISession,
    RetryChain,
    UserPrompt,
    compute_loop_tax,
    format_loop_tax_line,
    render_aiwork_markdown,
    resolve_output_price,
)
from kaizenlog.vault import ACTIVITY_MARKER, extract_section, upsert_section

TZ = timezone.utc
T0 = datetime(2026, 7, 29, 10, 0, tzinfo=TZ)


def _prompt(offset_min: int, text: str = "同じ依頼を繰り返す") -> UserPrompt:
    return UserPrompt(
        timestamp=T0 + timedelta(minutes=offset_min),
        project="repo",
        text=text,
    )


def _session(
    sid: str,
    start_offset: int,
    tokens: int,
    *,
    model: str = "claude-sonnet-4",
    errors: int = 0,
) -> AISession:
    start = T0 + timedelta(minutes=start_offset)
    return AISession(
        session_id=sid,
        project="repo",
        start=start,
        end=start + timedelta(minutes=20),
        user_turns=2,
        output_tokens=tokens,
        models={model},
        tool_errors=errors,
    )


def test_c5_episode_excludes_final_attempt():
    # 3試行: s1, s2 が浪費、s3 が最終 → s1+s2 の tokens
    prompts = [_prompt(0), _prompt(5), _prompt(10)]
    chain = RetryChain(project="repo", prompts=prompts)
    sessions = [
        _session("s1", 0, 1000),
        _session("s2", 5, 2000),
        _session("s3", 10, 9999),
    ]
    tax = compute_loop_tax([chain], sessions)
    assert tax.episode_count == 1
    assert tax.total_wasted_tokens == 3000
    assert tax.episodes[0].wasted_tokens == 3000


def test_c5_pricing_gpt4o_mini_not_matched_as_gpt4o():
    assert resolve_output_price("gpt-4o-mini") == 0.6
    assert resolve_output_price("gpt-4o") == 10.0
    assert resolve_output_price("chatgpt-4o-latest") == 10.0


def test_c5_usd_jpy_and_unknown_tokens():
    prompts = [_prompt(0), _prompt(5)]
    chain = RetryChain(project="repo", prompts=prompts)
    # セッション無し → tokens不明
    tax = compute_loop_tax([chain], [])
    line = format_loop_tax_line(tax)
    assert "tokens不明" in line
    assert "1エピソード" in line

    sessions = [_session("s1", 0, 1_000_000, model="claude-sonnet-4")]
    tax2 = compute_loop_tax([chain], sessions)
    line2 = format_loop_tax_line(tax2, usd_jpy=150.0)
    # 1M tokens * $3 / MTok = $3.00 → ¥450
    assert "$3.00" in line2
    assert "¥450" in line2


def test_c5_threshold_exceed_only():
    """同額は通知せず、超過のみ。比較は > 。"""
    prompts = [_prompt(0), _prompt(5)]
    chain = RetryChain(project="repo", prompts=prompts)
    sessions = [_session("s1", 0, 1_000_000, model="claude-sonnet-4")]
    tax = compute_loop_tax([chain], sessions)
    # sonnet 3.0 USD/MTok * 1M = $3.00
    assert tax.est_cost_usd == 3.0
    assert not (tax.est_cost_usd is not None and tax.est_cost_usd > 3.0)
    assert tax.est_cost_usd is not None and tax.est_cost_usd > 2.99


def test_c5_render_line_and_handwritten_inviolable():
    prompts = [_prompt(0), _prompt(5)]
    chain = RetryChain(project="repo", prompts=prompts)
    sessions = [_session("s1", 0, 500)]
    md = render_aiwork_markdown(
        sessions,
        TZ,
        retry_chain_count=1,
        retry_chains=[chain],
        pricing=None,
    )
    assert "ループ税" in md
    # マーカー外手書き不可侵
    note = "# 手書き\n手動行\n"
    section = "### 活動\n\n" + md
    updated = upsert_section(note, ACTIVITY_MARKER, section)
    assert "手動行" in updated
    assert extract_section(updated, ACTIVITY_MARKER) is not None
