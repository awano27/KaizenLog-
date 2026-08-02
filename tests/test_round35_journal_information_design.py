"""第35弾 Phase 1: 日誌の情報量・source分離・画面AIの計測境界。"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from math import inf, nan
from types import MappingProxyType
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from kaizenlog.advice_evidence import (
    AdviceEvidence,
    _build_reader_summary,
    _reader_history_with_current,
    build_advice_evidence,
)
from kaizenlog.advisor import AdviceContractError, _baseline_repair_hint, render_reader_advice
from kaizenlog.advice_format import validate_advice
from kaizenlog.aiwork import (
    AISession,
    RetryChain,
    UserPrompt,
    compute_loop_tax,
    estimate_sessions_cost,
    format_loop_tax_line,
    render_aiwork_markdown,
    retry_chain_excerpts,
)
from kaizenlog.report import Block, DailySummary, render_change_table, render_markdown
from kaizenlog.stats import build_stats
from kaizenlog.memory import assign_action_ids
from kaizenlog.verdict import parse_pass_condition
from tests.test_advice_format import _valid_data
from kaizenlog.config import Config
from kaizenlog.vault import (
    ACTIVITY_MARKER,
    ADVICE_MARKER,
    DailyNoteStore,
    extract_section,
    upsert_section,
)


TZ = timezone.utc
T0 = datetime(2026, 8, 1, 9, 0, tzinfo=TZ)


def _session(
    session_id: str,
    *,
    source: str = "claude-code",
    errors: int = 0,
    interruptions: int = 0,
    measurable: bool = True,
) -> AISession:
    return AISession(
        session_id=session_id,
        project="repo",
        start=T0,
        end=T0 + timedelta(minutes=20),
        user_turns=3,
        tool_counts=Counter({"Read": 1}),
        tool_errors=errors,
        interruptions=interruptions,
        output_tokens=100,
        models={"claude-sonnet-4"},
        source=source,
        tools_measurable=measurable,
    )


def _chain(text: str, *, project: str = "repo", source: str = "claude-code") -> RetryChain:
    prompt = UserPrompt(T0, project, text, source=source)
    return RetryChain(project, [prompt, prompt])


def _summary(**overrides: object) -> DailySummary:
    values: dict[str, object] = {
        "day": date(2026, 8, 1),
        "total_minutes": 180.0,
        "by_category": {"開発": 180.0},
        "by_app": {},
        "blocks": [],
        "ai_tool_minutes": {},
        "ai_sessions": 0,
        "context_switches": 4,
        "by_site": {},
    }
    values.update(overrides)
    return DailySummary(**values)


def _stats(
    *,
    sessions: int = 1,
    screen_minutes: dict[str, float] | None = None,
    web_sources: tuple[str, ...] = (),
) -> dict:
    stats = {
        "day": "2026-08-01",
        "total_minutes": 180.0,
        "context_switches": 4,
        "ai_activity_blocks": 0,
        "by_category": {"開発": 180.0},
        "by_app": {},
        "by_site": {},
        "blocks": [],
        "ai": {
            "sessions": sessions,
            "fragmented": 0,
            "tool_errors": 0,
            "interruptions": 0,
            "sources": {source: {"sessions": 1} for source in web_sources},
        },
    }
    if screen_minutes is not None:
        stats["ai_screen_tool_minutes"] = screen_minutes
    return stats


def test_a1_reader_notes_are_omitted_only_when_empty() -> None:
    base = dict(
        markdown="",
        fact_ids=frozenset(),
        ai_conversation_metrics_available=True,
        entertainment_observed=False,
        reader_summary="要約です。",
        max_actions=1,
        previous_day_available=True,
        browser_sample_sufficient=True,
    )
    advice = "### 明日の最小アクション\n- [ ] 一つ試す"

    without_notes = render_reader_advice(advice, AdviceEvidence(reader_notes=(), **base))
    with_notes = render_reader_advice(advice, AdviceEvidence(reader_notes=("AFK未計測です。",), **base))

    assert "### 計測上の注意" not in without_notes
    assert "### 計測上の注意\n\nAFK未計測です。" in with_notes


@pytest.mark.parametrize(
    "advice",
    ["", "### 明日の最小アクション\n本文だけ", "### 明日の最小アクション\n- [ ] [F1]", "### 明日の最小アクション\n- [ ] [F1] [F2]"],
)
def test_a1_reader_advice_rejects_empty_action_extraction(advice: str) -> None:
    evidence = AdviceEvidence(
        markdown="",
        fact_ids=frozenset(),
        ai_conversation_metrics_available=True,
        entertainment_observed=False,
        reader_summary="要約です。",
        reader_notes=(),
        max_actions=1,
        previous_day_available=True,
        browser_sample_sufficient=True,
    )

    with pytest.raises(AdviceContractError, match="最小アクション"):
        render_reader_advice(advice, evidence)


def test_a1_reader_advice_skips_only_fact_id_actions_when_a_real_action_remains() -> None:
    evidence = AdviceEvidence(
        markdown="", fact_ids=frozenset(), ai_conversation_metrics_available=True,
        entertainment_observed=False, reader_summary="要約です。", reader_notes=(),
        max_actions=1, previous_day_available=True, browser_sample_sufficient=True,
    )
    rendered = render_reader_advice(
        "### 明日の最小アクション\n- [ ] [F1]\n- [ ] 維持する",
        evidence,
    )

    assert "- [ ] 維持する" in rendered
    assert "- [ ] \n" not in rendered


def test_a1_cmd_advise_reader_contract_error_saves_degraded_section_and_reraises(tmp_path, monkeypatch) -> None:
    from kaizenlog import cli as cli_mod

    vault = tmp_path / "vault"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    day = date(2026, 8, 1)
    content = upsert_section("---\n---\n", ACTIVITY_MARKER, "### カテゴリ別\n|a|b|\n")
    (daily / f"{day.isoformat()}.md").write_text(content, encoding="utf-8")
    cfg = Config(
        vault_dir=vault, timezone="Asia/Tokyo", daily_notes_dir="01 Daily Notes",
        stats_dir=".kaizenlog/stats", memory_dir="Kaizen/Memory", logs_dir=".kaizenlog/logs",
    )
    evidence = MagicMock(markdown="### 確定事実\n- 合計100分")
    health: list[dict] = []
    monkeypatch.setattr(cli_mod, "generate_advice", lambda *args, **kwargs: "### 明日の最小アクション\n- [ ] [F1]")
    monkeypatch.setattr(cli_mod, "build_advice_evidence", lambda *args, **kwargs: evidence)
    monkeypatch.setattr(cli_mod, "load_stats", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli_mod, "requires_daily_contract", lambda *args, **kwargs: True)
    monkeypatch.setattr(cli_mod, "_safe_log_advise_health", lambda *args, **kwargs: health.append(kwargs))

    with pytest.raises(AdviceContractError, match="最小アクション"):
        cli_mod.cmd_advise(cfg, day)

    note = (daily / f"{day.isoformat()}.md").read_text(encoding="utf-8")
    assert "出力契約を満たさず" in (extract_section(note, ADVICE_MARKER) or "")
    assert health and health[-1]["outcome"] == "degraded"


def test_a1_retry_chain_excerpts_dedupe_after_redaction_and_before_limit() -> None:
    chains = [
        _chain("秘密   の依頼", project="one"),
        _chain("秘密 の依頼", project="one"),
        _chain("別の依頼", project="one"),
        _chain("さらに別の依頼", project="one"),
        _chain("", project="empty"),
    ]

    excerpts = retry_chain_excerpts(
        chains,
        redactor=lambda text: text.replace("秘密", "[REDACTED]"),
        max_chains=3,
    )

    assert excerpts == [
        "連鎖起点（one）: [REDACTED] の依頼 ×2件",
        "連鎖起点（one）: 別の依頼",
        "連鎖起点（one）: さらに別の依頼",
    ]
    assert retry_chain_excerpts([_chain("", project="empty")]) == [
        "連鎖起点（empty）: （依頼本文が無いため省略）"
    ]
    assert retry_chain_excerpts([_chain("", project="empty"), _chain("", project="empty")]) == [
        "連鎖起点（empty）: （依頼本文が無いため省略） ×2件"
    ]
    assert retry_chain_excerpts([_chain("x", project="line\nbreak|pipe")]) == [
        "連鎖起点（line break\\|pipe）: x"
    ]


def test_a2_tool_metrics_expand_per_source_only_for_multiple_measurable_sources() -> None:
    codex = _session("codex", source="codex", errors=4, interruptions=2)
    claude = _session("claude", source="claude-code", errors=1, interruptions=1)
    browser = _session("browser", source="chatgpt-web", errors=99, interruptions=99, measurable=False)
    chains = [_chain("a", source="codex"), _chain("b", source="claude-code")]

    expanded = render_aiwork_markdown(
        [codex, claude, browser], TZ, retry_chain_count=2, retry_chains=chains
    )
    line = next(line for line in expanded.splitlines() if line.startswith("ツールエラー:"))
    assert line == (
        "ツールエラー: 5回（codex 4 / claude-code 1。codexは文字列判定・過大計上の可能性）"
        " / ユーザー中断・拒否: 3回（codex 2 / claude-code 1）"
        " / リトライ連鎖: 2回（codex 1 / claude-code 1） / 出力トークン: 300"
    )

    single_source = render_aiwork_markdown([claude], TZ, retry_chain_count=1)
    single_line = next(line for line in single_source.splitlines() if line.startswith("ツールエラー:"))
    assert single_line == "ツールエラー: 1回 / ユーザー中断・拒否: 1回 / リトライ連鎖: 1回 / 出力トークン: 100"

    for unknown_source in ("", "unrecognized-source"):
        unknown = _session("unknown", source=unknown_source, errors=9, interruptions=4)
        unseparated = render_aiwork_markdown([codex, claude, unknown], TZ, retry_chain_count=1)
        unknown_line = next(line for line in unseparated.splitlines() if line.startswith("ツールエラー:"))
        assert unknown_line == (
            "ツールエラー: 14回（codexは文字列判定・過大計上の可能性）"
            " / ユーザー中断・拒否: 7回 / リトライ連鎖: 1回 / 出力トークン: 300"
        )


def test_a3_screen_tool_coverage_lists_all_positive_unlogged_tools() -> None:
    markdown = render_aiwork_markdown(
        [_session("web", source="chatgpt-web", measurable=False)],
        TZ,
        screen_tool_minutes={"chatgpt": 71.7, "claude": 34.8, "openai": 30.0, "gemini": 29.9},
    )

    assert "計測範囲: セッションログのある AI CLI / ブラウザ拡張のみが対象です。" in markdown
    assert "claude（ブラウザ/デスクトップ）" in markdown
    assert "openai（ブラウザ/デスクトップ）" in markdown
    assert "gemini（ブラウザ/デスクトップ）" in markdown
    assert "はログが無く" in markdown
    assert "chatgpt 71.7分" not in markdown
    assert "%" not in markdown


def test_a3_build_stats_persists_rounded_screen_tool_minutes_without_new_argument() -> None:
    stats = build_stats(
        date(2026, 8, 1),
        _summary(ai_tool_minutes={"chatgpt": 73.04, "claude": 29.96}),
        [],
    )

    assert stats["ai_screen_tool_minutes"] == {"chatgpt": 73.0, "claude": 30.0}


def test_a3_screen_tool_minutes_accept_only_finite_nonnegative_mapping_values() -> None:
    screen_minutes = MappingProxyType(
        {"safe": 12.5, 7: 1.25, "zero": 0.0, "nan": nan, "inf": inf, "bool": True, "list": []}
    )
    stats = build_stats(date(2026, 8, 1), _summary(ai_tool_minutes=screen_minutes), [])
    markdown = render_aiwork_markdown([_session("s")], TZ, screen_tool_minutes=screen_minutes)
    non_mapping_markdown = render_aiwork_markdown([_session("s2")], TZ, screen_tool_minutes=["bad"])

    assert stats["ai_screen_tool_minutes"] == {"safe": 12.5, "7": 1.2, "zero": 0.0}
    # 第39弾: _fmt_minutes 統一・1分未満除外（1.25→1m、12.5→12m）
    assert "safe（ブラウザ/デスクトップ）" in markdown
    assert "はログが無く" in markdown
    assert "nan" not in markdown
    assert "nan" not in markdown and "inf" not in markdown and "list" not in markdown
    assert "画面計測のAI作業のうち" not in non_mapping_markdown


def test_a3_stats_keeps_unknown_sources_honest_and_ai_sessions_alias() -> None:
    unknown_session = _session("unknown", source="", errors=3)
    unknown_chain = _chain("retry", source="")
    explicit_session = _session("codex", source="codex", errors=2)
    stats = build_stats(
        date(2026, 8, 1),
        _summary(ai_sessions=7),
        [unknown_session, explicit_session],
        retry_chains=[unknown_chain],
    )

    assert stats["ai_sessions"] == 7
    assert stats["ai_activity_blocks"] == 7
    assert stats["ai"]["sessions"] == 2
    assert stats["ai"]["sources"]["unknown"]["sessions"] == 1
    assert stats["ai"]["sources"]["unknown"]["retry_chains"] == 1
    assert stats["ai"]["sources"]["codex"]["sessions"] == 1
    assert "claude-code" not in stats["ai"]["sources"]


def test_a3_reader_note_uses_screen_stats_only_when_existing_telemetry_note_does_not_apply() -> None:
    new_note = "セッションログを取得できないAI画面が73分記録されています。🧠の数値はCLI・拡張由来のみで、AI利用全体の質ではありません。"

    with_screen = build_advice_evidence(_stats(screen_minutes={"chatgpt": 73.0}), [])
    legacy = build_advice_evidence(_stats(), [])
    matching_web = build_advice_evidence(
        _stats(screen_minutes={"chatgpt": 73.0}, web_sources=("chatgpt-web",)), []
    )
    existing_note_wins = build_advice_evidence(_stats(sessions=0, screen_minutes={"chatgpt": 73.0}), [])

    assert new_note in with_screen.reader_notes
    assert new_note not in legacy.reader_notes
    assert new_note not in matching_web.reader_notes
    assert new_note not in existing_note_wins.reader_notes
    assert any("会話回数や回答品質は計測できない" in note for note in existing_note_wins.reader_notes)


def test_a3_reader_note_requires_a_valid_positive_web_session_and_30_minute_boundary() -> None:
    note_30 = "セッションログを取得できないAI画面が30分記録されています。🧠の数値はCLI・拡張由来のみで、AI利用全体の質ではありません。"

    under = build_advice_evidence(_stats(screen_minutes={"chatgpt": 29.9}), [])
    at_boundary = build_advice_evidence(_stats(screen_minutes={"chatgpt": 30.0}), [])
    combined_boundary = build_advice_evidence(
        _stats(screen_minutes={"chatgpt": 10.0, "claude": 20.0}), []
    )
    invalid_sources = []
    for bucket in ({}, {"sessions": 0}, {"sessions": True}, {"sessions": "1"}, 1):
        stats = _stats(screen_minutes={"chatgpt": 30.0})
        stats["ai"]["sources"] = {"chatgpt-web": bucket}
        invalid_sources.append(build_advice_evidence(stats, []))

    assert note_30 not in under.reader_notes
    assert note_30 in at_boundary.reader_notes
    assert note_30 in combined_boundary.reader_notes
    assert all(note_30 in evidence.reader_notes for evidence in invalid_sources)


def test_b1_friction_worst_is_redacted_and_precedes_the_session_table() -> None:
    first = _session("first")
    first.title = "first session"
    worst = _session("worst", errors=4, interruptions=2)
    worst.project = "secret|project"
    worst.source = "secret|cli"
    worst.title = "secret|request"
    last = _session("last")
    last.title = "last session"

    markdown = render_aiwork_markdown(
        [first, worst, last],
        TZ,
        redactor=lambda text: text.replace("secret", "[REDACTED]"),
    )

    lines = markdown.splitlines()
    warning_index = next(i for i, line in enumerate(lines) if line.startswith("⚠ 本日の摩擦ワースト:"))
    table_index = lines.index("| 開始-最終 | プロジェクト | 内容 | 往復 | ツール | エラー | 中断 | 変更 |")
    assert warning_index < table_index
    assert lines[warning_index] == "⚠ 本日の摩擦ワースト: [REDACTED]\\|project ([REDACTED]\\|cli)「[REDACTED]\\|request」"
    # 第39弾 §E5: ツール実行総数がある場合は率を併記
    assert "摩擦14" in lines[warning_index + 1]
    assert "ツールエラー4" in lines[warning_index + 1]
    assert "中断2×5" in lines[warning_index + 1]
    assert lines[warning_index + 2] == "   ※ 摩擦はスコア順位であり、AIの良し悪しの判定ではありません。"
    assert "| first session |" in markdown
    assert "| last session |" in markdown


def test_b1_friction_is_omitted_at_zero_and_uses_dash_for_empty_title() -> None:
    quiet = _session("quiet")
    quiet.title = ""
    friction = _session("friction", errors=1)
    friction.title = ""

    no_friction = render_aiwork_markdown([quiet], TZ)
    markdown = render_aiwork_markdown([quiet, friction], TZ)

    assert "⚠ 本日の摩擦ワースト:" not in no_friction
    assert "⚠ 本日の摩擦ワースト: repo (claude-code)「—」" in markdown


def test_b2_uncosted_majority_explains_tokens_and_stable_unknown_models() -> None:
    registered = _session("registered")
    unknown = _session("unknown")
    unknown.output_tokens = 200
    unknown.models = {"zeta-unknown", "alpha-unknown"}

    markdown = render_aiwork_markdown([registered, unknown], TZ)

    assert "推定コスト(下限): 換算なし — 出力300 tok のうち単価未登録が200 tok。" in markdown
    assert "未登録モデル: alpha-unknown, zeta-unknown。" in markdown
    assert "kaizenlog.toml の [aiwork.pricing] に $/1Mtok を設定すると金額換算されます。" in markdown


def test_b2_all_registered_models_keep_existing_cost_display() -> None:
    markdown = render_aiwork_markdown([_session("registered")], TZ)

    assert "推定コスト(下限): $0.00（output tokens ベース概算、対象外 0 tok。input/cache 未計上）" in markdown
    assert "推定コスト(下限): 換算なし" not in markdown
    assert "未登録モデル:" not in markdown


def test_b2_loop_tax_additions_require_day_output_tokens_and_cap_inconsistent_ratio() -> None:
    session = _session("retry", errors=1)
    session.output_tokens = 120
    chain = _chain("retry request")
    tax = compute_loop_tax([chain], [session])

    assert format_loop_tax_line(tax) == "💸 ループ税: $0.00（1エピソード / 120 tokens） ※エピソード間で同一セッションは1回のみ計上"

    with_day_total = format_loop_tax_line(tax, day_output_tokens=200)
    inconsistent = format_loop_tax_line(tax, day_output_tokens=100)
    rendered = render_aiwork_markdown(
        [session], TZ, retry_chain_count=1, retry_chains=[chain]
    )

    assert with_day_total.splitlines() == [
        "💸 ループ税: $0.00（1エピソード / 120 tokens） ※エピソード間で同一セッションは1回のみ計上"
        " / 当日出力 200 tok に対し 60.0%",
        "   — 最悪例: 2往復 / 浪費120 tok / ツールエラーあり / 連鎖起点（repo）: retry request",
    ]
    assert "当日出力 100 tok に対し 100.0%（入力不整合のため上限）" in inconsistent
    assert "当日出力 120 tok に対し 100.0%" in rendered


def test_b2_loop_tax_worst_example_redacts_direct_formatter_excerpt() -> None:
    session = _session("retry", errors=1)
    session.project = "secret|project"
    chain = _chain("secret retry request", project="secret|project")
    tax = compute_loop_tax([chain], [session])

    line = format_loop_tax_line(
        tax,
        day_output_tokens=100,
        redactor=lambda text: text.replace("secret", "[REDACTED]"),
    )

    assert "secret" not in line
    assert "連鎖起点（[REDACTED]\\|project）: [REDACTED] retry request" in line


def test_b2_render_passes_redactor_to_loop_tax_worst_example() -> None:
    session = _session("retry", errors=1)
    session.project = "secret|project"
    chain = _chain("secret retry request", project="secret|project")

    markdown = render_aiwork_markdown(
        [session],
        TZ,
        max_rows=0,
        retry_chain_count=1,
        retry_chains=[chain],
        redactor=lambda text: text.replace("secret", "[REDACTED]"),
    )

    assert "secret" not in markdown
    assert "最悪例:" in markdown
    assert "連鎖起点（[REDACTED]\\|project）: [REDACTED] retry request" in markdown


def test_b2_render_does_not_repeat_worst_retry_excerpt_after_loop_tax_line() -> None:
    session = _session("retry", errors=1)
    session.output_tokens = 120
    worst = RetryChain(
        project="repo",
        prompts=[
            UserPrompt(T0 + timedelta(minutes=offset), "repo", "worst request")
            for offset in (0, 1, 2)
        ],
    )
    short = _chain("short request")

    markdown = render_aiwork_markdown(
        [session],
        TZ,
        retry_chain_count=2,
        retry_chains=[worst, short],
    )

    assert markdown.count("連鎖起点（repo）: worst request") == 1
    assert "リトライ連鎖起点（repo）: short request" in markdown


def test_b1_retry_chain_excerpts_group_redacted_projects_in_first_seen_order() -> None:
    chains = [
        _chain("same request", project="secret-a"),
        _chain("same request", project="secret-b"),
        _chain("other request", project="shown"),
    ]

    excerpts = retry_chain_excerpts(
        chains,
        redactor=lambda text: "[REDACTED]" if text.startswith("secret-") else text,
    )

    assert excerpts == [
        "連鎖起点（[REDACTED]）: same request ×2件",
        "連鎖起点（shown）: other request",
    ]


def test_b1_session_titles_off_keeps_friction_lines_but_hides_title() -> None:
    session = _session("friction", errors=1)
    session.title = "secret title"

    markdown = render_aiwork_markdown([session], TZ, session_titles=False)
    lines = markdown.splitlines()
    warning_index = next(i for i, line in enumerate(lines) if line.startswith("⚠ 本日の摩擦ワースト:"))

    assert lines[warning_index] == "⚠ 本日の摩擦ワースト: repo (claude-code)「—」"
    assert lines[warning_index + 2] == "   ※ 摩擦はスコア順位であり、AIの良し悪しの判定ではありません。"
    assert "secret title" not in markdown


def test_b2_mixed_known_and_unknown_models_are_fully_uncosted() -> None:
    known_first = _session("known-first")
    known_first.models = ["claude-sonnet-4", "unknown-model"]
    unknown_first = _session("unknown-first")
    unknown_first.models = ["unknown-model", "claude-sonnet-4"]

    cost, uncosted, per_source = estimate_sessions_cost([known_first, unknown_first])
    markdown = render_aiwork_markdown([known_first, unknown_first], TZ)

    assert cost == 0.0
    assert uncosted == 200
    assert per_source["claude-code"]["uncosted_tokens"] == 200
    assert "推定コスト(下限): 換算なし — 出力200 tok のうち単価未登録が200 tok。" in markdown
    assert "未登録モデル: unknown-model。" in markdown


def test_b2_all_registered_prices_stay_costed_even_when_rates_differ() -> None:
    """単価が割れても全モデル登録済みなら換算する（上限単価・モデル順に非依存）。

    None を返すと登録済みトークンを「単価未登録」と表示することになり、
    「無い指標は無いと言う」の逆（有る指標を無いと言う）になる。
    """
    sonnet_first = _session("sonnet-first")
    sonnet_first.models = ["claude-sonnet-4", "gpt-4o"]
    gpt_first = _session("gpt-first")
    gpt_first.models = ["gpt-4o", "claude-sonnet-4"]

    cost, uncosted, per_source = estimate_sessions_cost([sonnet_first, gpt_first])
    markdown = render_aiwork_markdown([sonnet_first, gpt_first], TZ)

    assert cost > 0.0
    assert uncosted == 0
    assert per_source["claude-code"]["uncosted_tokens"] == 0
    assert "推定コスト(下限): $" in markdown
    assert "推定コスト(下限): 換算なし" not in markdown
    assert "未登録モデル:" not in markdown


def test_b2_day_output_tokens_formats_known_loop_tokens_with_commas() -> None:
    session = _session("retry", errors=1)
    session.output_tokens = 100_000
    tax = compute_loop_tax([_chain("retry request")], [session])

    legacy = format_loop_tax_line(tax)
    with_day_total = format_loop_tax_line(tax, day_output_tokens=200_000)

    assert "100000 tokens" in legacy
    assert "100,000 tokens" in with_day_total
    assert "当日出力 200,000 tok に対し 50.0%" in with_day_total


# ---- §C: Activity Log -------------------------------------------------------


def _block(hour_utc: int, minutes: float, category: str, *, offset: int = 0) -> Block:
    start = datetime(2026, 8, 1, hour_utc, offset, tzinfo=timezone.utc)
    return Block(start, start + timedelta(minutes=minutes), category, "App")


def test_c1_reports_under_threshold_blocks_in_jst_with_dynamic_threshold() -> None:
    blocks = [
        _block(15, 5, "開発"),
        _block(16, 2, "AI作業"),
        _block(16, 1, "AI作業", offset=10),
        _block(23, 2, "ブラウジング"),
        _block(22, 1, "コミュニケーション"),
        _block(21, 1, "エンタメ"),
    ]
    markdown = render_markdown(
        _summary(blocks=blocks, total_minutes=20.0),
        ZoneInfo("Asia/Tokyo"),
        min_block_minutes=3.0,
    )

    assert "3分以上の画面ブロックを時刻順に表示。" in markdown
    # 第48弾 §D1: 細切れは表ではなく3行サマリ
    assert "細切れ（3分未満）5件・7分（合計の35%）" in markdown
    assert "AI作業3分" in markdown
    assert "集中を妨げた時間帯:" in markdown
    assert "1時台" in markdown


def test_c1_shows_explanation_without_timeline_rows_and_keeps_overflow_separate() -> None:
    # 第48弾 §D1: eligible 0 でも細切れは表に載らずサマリのみ。
    all_under = render_markdown(
        _summary(blocks=[_block(0, 2, "AI作業"), _block(1, 1, "開発")], total_minutes=3.0),
        ZoneInfo("Asia/Tokyo"),
        min_block_minutes=4.0,
    )
    assert "細切れ（4分未満）2件・3分（合計の100%）" in all_under
    assert "4分以上の画面ブロックを時刻順に表示。" not in all_under
    assert "| 細切れ |" not in all_under

    capped = render_markdown(
        _summary(
            blocks=[_block(0, 5, "開発"), _block(2, 6, "開発"), _block(3, 2, "AI作業")],
            total_minutes=13.0,
        ),
        timezone.utc,
        min_block_minutes=3.0,
        max_timeline_rows=1,
    )
    lines = capped.splitlines()
    overflow = next(line for line in lines if "対象内の1件を省略" in line)
    under = next(line for line in lines if line.startswith("細切れ（"))
    assert overflow != under
    assert "細切れ（3分未満）1件・2分" in under


def test_c1_omits_under_threshold_explanation_when_every_block_is_eligible() -> None:
    markdown = render_markdown(
        _summary(blocks=[_block(0, 3, "開発"), _block(1, 4, "AI作業")], total_minutes=7.0),
        timezone.utc,
        min_block_minutes=3.0,
    )

    assert "細切れ（" not in markdown
    assert "集中を妨げた時間帯:" not in markdown


def test_c2_change_table_omits_missing_metrics_and_absent_previous_day() -> None:
    today = {
        "total_minutes": 125.0,
        "by_category": {"AI作業": 91.0},
        "ai": {"tool_errors": 3, "retry_chains": 2, "fragmented": 4},
    }
    previous = {
        "total_minutes": 100.0,
        "by_category": {"AI作業": 80.0},
        "ai": {"tool_errors": 6, "retry_chains": 1, "fragmented": 2},
    }

    table = render_change_table(today, previous)
    assert "| ツールエラー | 6 | 3 | -3 |" in table
    assert "| AI作業 | 1h20m | 1h31m | +11m |" in table
    assert "| 合計アクティブ | 1h40m | 2h05m | +25m |" in table
    assert "※ 数値の向きだけを示します。" in table
    assert "| 改善 |" not in table and "| 悪化 |" not in table
    assert render_change_table(today, None) == ""

    del previous["ai"]["retry_chains"]
    assert "リトライ連鎖" not in render_change_table(today, previous)


def test_c2_cmd_generate_uses_only_calendar_previous_stats_and_hashes_written_section(tmp_path, monkeypatch) -> None:
    from kaizenlog import cli as cli_mod

    for name in ("notes", "stats", "mem", "logs", "exp"):
        (tmp_path / name).mkdir()
    cfg = Config(
        vault_dir=tmp_path,
        daily_notes_dir="notes",
        stats_dir="stats",
        memory_dir="mem",
        logs_dir="logs",
        experiments_dir="exp",
        timezone="UTC",
    )
    cfg.aiwork.enabled = False
    day = date(2026, 8, 1)
    summary = _summary(day=day, total_minutes=125.0, by_category={"AI作業": 91.0})
    captured: dict[str, str] = {}

    load_calls: list[dict] = []

    def fake_load_stats(*args, **kwargs):
        assert args[1:] == ()
        load_calls.append(dict(kwargs))
        return [
            {"day": "2026-07-31", "total_minutes": 100.0, "by_category": {"AI作業": 80.0}, "ai": {"tool_errors": 1}},
            {"day": day.isoformat(), "total_minutes": 999.0, "by_category": {"AI作業": 999.0}, "ai": {"tool_errors": 999}},
        ]

    monkeypatch.setattr(cli_mod, "collect_day", lambda *args: ([], True))
    monkeypatch.setattr(cli_mod, "collect_input", lambda *args: None)
    monkeypatch.setattr(cli_mod, "summarize", lambda *args, **kwargs: summary)
    monkeypatch.setattr(cli_mod, "render_markdown", lambda *args, **kwargs: "### activity")
    monkeypatch.setattr(cli_mod, "load_stats", fake_load_stats)
    monkeypatch.setattr(cli_mod, "write_stats", lambda *args, **kwargs: captured.update(activity_md=kwargs["activity_md"]))
    monkeypatch.setattr(cli_mod, "load_experiments", lambda *args: [])
    monkeypatch.setattr(cli_mod, "detect_regressions", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli_mod, "judge_entries", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli_mod, "load_entries", lambda *args: [])
    monkeypatch.setattr(cli_mod, "ActivityWatchClient", MagicMock)
    monkeypatch.setattr(cli_mod, "Classifier", MagicMock())
    monkeypatch.setattr("kaizenlog.decay.run_decay_detection", lambda *args, **kwargs: [])
    monkeypatch.setattr("kaizenlog.coachledger.judge_coach_entries", lambda *args, **kwargs: [])

    cli_mod.cmd_generate(cfg, day)

    # 呼び出し列の完全一致: 前日比 days=2 + ACTIONS 用 _actions_stats_history
    from kaizenlog.memory import ACTIONS_HANDOFF_DAYS

    # 第48弾: 冒頭で基準線用 days=8、digest 用 days=1 が追加
    assert {"days": 8, "end_day": day} in load_calls  # baseline prior
    assert {"days": 2, "end_day": day} in load_calls  # 前日比
    assert {
        "days": 8,
        "end_day": day - __import__("datetime").timedelta(days=1),
    } in load_calls  # §A3 prior
    assert {
        "days": ACTIONS_HANDOFF_DAYS + 14,
        "end_day": day + __import__("datetime").timedelta(days=1),
    } in load_calls
    assert {"days": 1, "end_day": day} in load_calls  # digest 用 stats 再読込

    stored = DailyNoteStore(cfg.daily_notes_path).read(day) or ""
    section = extract_section(stored, ACTIVITY_MARKER) or ""
    assert "| 合計アクティブ | 1h40m | 2h05m | +25m |" in section
    assert captured["activity_md"] == section


def test_c2_reader_summary_uses_adjacent_ai_trend_and_recorded_day_maximum() -> None:
    stats = {
        "day": "2026-08-01",
        "total_minutes": 150.0,
        "by_category": {"AI作業": 70.0, "開発": 40.0},
        "ai": {"tool_errors": 6, "projects": {"repo": {"errors": 5}}},
    }
    history = [
        {"day": "2026-07-28", "total_minutes": 90.0, "by_category": {"AI作業": 20.0}},
        {"day": "2026-07-29", "total_minutes": 100.0, "by_category": {"AI作業": 30.0}},
        {"day": "2026-07-30", "total_minutes": 120.0, "by_category": {"AI作業": 50.0}},
        {"day": "2026-07-31", "total_minutes": 130.0, "by_category": {"AI作業": 60.0}},
    ]

    summary = _build_reader_summary(
        total_minutes=150.0,
        short_record=False,
        stats=stats,
        history=history,
        by_category=stats["by_category"],
        category_stats_valid=True,
        entertainment_minutes=None,
        previous_day_available=True,
    )

    assert summary.startswith("AI作業は4日連続で増加し、本日 1時間10分が記録されています。")
    assert "ツールエラー6回中5回（83%）が『repo』に集中しています。" in summary
    assert "合計 2時間30分 は記録のある直近5日で最長です。" in summary


def test_c2_reader_summary_marks_gapped_history_without_inventing_a_rank() -> None:
    stats = {
        "day": "2026-08-01",
        "total_minutes": 120.0,
        "by_category": {"AI作業": 70.0, "開発": 40.0},
        "ai": {},
    }
    history = [
        {"day": "2026-07-29", "total_minutes": 100.0, "by_category": {"AI作業": 30.0}},
        {"day": "2026-07-30", "total_minutes": 160.0, "by_category": {"AI作業": 50.0}},
    ]

    summary = _build_reader_summary(
        total_minutes=120.0,
        short_record=False,
        stats=stats,
        history=history,
        by_category=stats["by_category"],
        category_stats_valid=True,
        entertainment_minutes=None,
        previous_day_available=False,
    )

    assert summary.startswith("本日は合計2時間0分の作業が記録されています。")
    assert "カテゴリ別では「AI作業」が最多（70分）でした。" in summary
    assert "最長" not in summary and "番目" not in summary


def test_c2_reader_summary_excludes_duplicate_and_future_history_from_current_values() -> None:
    stats = {
        "day": "2026-08-01",
        "total_minutes": 150.0,
        "by_category": {"AI作業": 70.0, "開発": 40.0},
        "ai": {},
    }
    history = [
        {"day": "2026-07-29", "total_minutes": 100.0, "by_category": {"AI作業": 20.0}},
        {"day": "2026-07-30", "total_minutes": 120.0, "by_category": {"AI作業": 30.0}},
        {"day": "2026-07-31", "total_minutes": 130.0, "by_category": {"AI作業": 50.0}},
        {"day": "2026-08-01", "total_minutes": 999.0, "by_category": {"AI作業": 999.0}},
        {"day": "2026-08-02", "total_minutes": 999.0, "by_category": {"AI作業": 999.0}},
    ]

    summary = _build_reader_summary(
        total_minutes=150.0,
        short_record=False,
        stats=stats,
        history=history,
        by_category=stats["by_category"],
        category_stats_valid=True,
        entertainment_minutes=None,
        previous_day_available=True,
    )

    assert summary.startswith("AI作業は3日連続で増加し、本日 1時間10分が記録されています。")
    assert "合計 2時間30分 は記録のある直近4日で最長です。" in summary
    assert "999" not in summary


@pytest.mark.parametrize(
    ("values", "expected_first"),
    [
        (
            (10.0, 20.0, 30.0, 40.0, 50.0),
            "AI作業は4日連続で増加し、本日 50分が記録されています。",
        ),
        (
            (10.0, 20.0, 30.0, 40.0),
            "AI作業は3日連続で増加し、本日 40分が記録されています。",
        ),
        (
            (10.0, 20.0, 30.0),
            "本日は合計2時間30分の作業が記録されています。",
        ),
    ],
    ids=["four-increases", "three-increases", "two-increases-fallback"],
)
def test_c2_ai_trend_uses_increase_count_and_requires_three_increases(
    values: tuple[float, ...], expected_first: str
) -> None:
    current_day = date(2026, 8, 1)
    history = [
        {
            "day": (current_day - timedelta(days=len(values) - 1 - index)).isoformat(),
            "total_minutes": 100.0 + index * 10,
            "by_category": {"AI作業": value},
        }
        for index, value in enumerate(values[:-1])
    ]
    stats = {
        "day": current_day.isoformat(),
        "total_minutes": 150.0,
        "by_category": {"AI作業": values[-1], "開発": 40.0},
        "ai": {},
    }

    summary = _build_reader_summary(
        total_minutes=150.0,
        short_record=False,
        stats=stats,
        history=history,
        by_category=stats["by_category"],
        category_stats_valid=True,
        entertainment_minutes=None,
        previous_day_available=False,
    )

    assert summary.split("。", 1)[0] + "。" == expected_first


def test_c2_ai_trend_does_not_claim_monotonic_increase_for_non_monotonic_history() -> None:
    current_day = date(2026, 8, 1)
    values = (10.0, 30.0, 20.0, 40.0, 50.0)
    history = [
        {
            "day": (current_day - timedelta(days=len(values) - 1 - index)).isoformat(),
            "total_minutes": 100.0 + index * 10,
            "by_category": {"AI作業": value},
        }
        for index, value in enumerate(values[:-1])
    ]
    stats = {
        "day": current_day.isoformat(),
        "total_minutes": 150.0,
        "by_category": {"AI作業": values[-1], "開発": 40.0},
        "ai": {},
    }

    summary = _build_reader_summary(
        total_minutes=150.0,
        short_record=False,
        stats=stats,
        history=history,
        by_category=stats["by_category"],
        category_stats_valid=True,
        entertainment_minutes=None,
        previous_day_available=False,
    )

    assert summary.startswith("本日は合計2時間30分の作業が記録されています。")
    assert "単調増加" not in summary


def test_c2_ai_trend_keeps_recorded_day_wording_when_calendar_days_are_missing() -> None:
    stats = {
        "day": "2026-08-01",
        "total_minutes": 150.0,
        "by_category": {"AI作業": 40.0, "開発": 40.0},
        "ai": {},
    }
    history = [
        {"day": "2026-07-27", "total_minutes": 100.0, "by_category": {"AI作業": 10.0}},
        {"day": "2026-07-29", "total_minutes": 110.0, "by_category": {"AI作業": 20.0}},
        {"day": "2026-07-31", "total_minutes": 120.0, "by_category": {"AI作業": 30.0}},
    ]

    summary = _build_reader_summary(
        total_minutes=150.0,
        short_record=False,
        stats=stats,
        history=history,
        by_category=stats["by_category"],
        category_stats_valid=True,
        entertainment_minutes=None,
        previous_day_available=False,
    )

    assert summary.startswith("AI作業は記録のある4日で単調増加し、本日 40分が記録されています。")


def test_c2_reader_summary_uses_category_fallback_with_only_one_prior_day() -> None:
    stats = {
        "day": "2026-08-01",
        "total_minutes": 150.0,
        "by_category": {"AI作業": 70.0, "開発": 40.0},
        "ai": {},
    }
    history = [
        {"day": "2026-07-31", "total_minutes": 120.0, "by_category": {"AI作業": 50.0}},
    ]

    summary = _build_reader_summary(
        total_minutes=150.0,
        short_record=False,
        stats=stats,
        history=history,
        by_category=stats["by_category"],
        category_stats_valid=True,
        entertainment_minutes=None,
        previous_day_available=True,
    )

    assert "カテゴリ別では「AI作業」が最多（70分）でした。" in summary
    assert "最長" not in summary


def test_c2_reader_summary_keeps_current_conclusion_shape_without_history() -> None:
    stats = {
        "day": "2026-08-01",
        "total_minutes": 150.0,
        "by_category": {"AI作業": 70.0, "開発": 40.0},
        "ai": {},
    }

    summary = _build_reader_summary(
        total_minutes=150.0,
        short_record=False,
        stats=stats,
        history=[],
        by_category=stats["by_category"],
        category_stats_valid=True,
        entertainment_minutes=None,
        previous_day_available=False,
    )

    assert summary == "本日は合計2時間30分の作業が記録されています。 カテゴリ別では「AI作業」が最多（70分）でした。"


@pytest.mark.parametrize("invalid_field", ["tool_errors", "project_errors"])
@pytest.mark.parametrize("invalid_value", [nan, inf, True, -1.0], ids=["nan", "inf", "bool", "negative"])
def test_c2_tool_error_concentration_invalid_numbers_fall_back_without_exception(
    invalid_field: str, invalid_value: object
) -> None:
    ai = {
        "sessions": 1,
        "fragmented": 0,
        "tool_errors": 6,
        "interruptions": 0,
        "projects": {"repo": {"errors": 5}},
    }
    if invalid_field == "tool_errors":
        ai["tool_errors"] = invalid_value
    else:
        ai["projects"]["repo"]["errors"] = invalid_value
    stats = {
        "day": "2026-08-01",
        "total_minutes": 150.0,
        "context_switches": 4,
        "blocks": [],
        "by_category": {"AI作業": 70.0, "開発": 40.0},
        "ai": ai,
    }
    fallback = "本日は合計2時間30分の作業が記録されています。 カテゴリ別では「AI作業」が最多（70分）でした。"

    summary = _build_reader_summary(
        total_minutes=150.0,
        short_record=False,
        stats=stats,
        history=[],
        by_category=stats["by_category"],
        category_stats_valid=True,
        entertainment_minutes=None,
        previous_day_available=False,
    )
    evidence = build_advice_evidence(stats, [])

    assert summary == fallback
    assert evidence.reader_summary == fallback


# ---- §D: Kaizen ------------------------------------------------------------


def _reader_evidence() -> AdviceEvidence:
    return AdviceEvidence(
        markdown="",
        fact_ids=frozenset(),
        ai_conversation_metrics_available=True,
        entertainment_observed=False,
        reader_summary="要約です。",
        reader_notes=(),
        max_actions=3,
        previous_day_available=True,
        browser_sample_sufficient=True,
    )


def test_d1_reader_advice_restores_action_context_without_extra_action_ids() -> None:
    advice = (
        "### 今日の改善提案\n"
        "1. 手順の開始条件を固定する。参照を一つに絞る。翌日見る指標: コンテキスト切替回数\n"
        "2. 振り返りの時刻を決める。集中枠を記録する。翌日見る指標: 集中ブロック数\n\n"
        "### 明日の最小アクション\n"
        "- [ ] [F1] 始業時に参照を一つに絞る｜PASS: context_switches <= 30｜FAIL: 31\n"
        "- [ ] [F2] 終業前に集中枠を記録する｜PASS: focus_blocks >= 4｜FAIL: 3\n\n"
        "### AI作業の改善\n"
        "- [F5] 会話の往復数は測定不能なので品質の良否は断定しない\n"
    )

    rendered = render_reader_advice(advice, _reader_evidence())
    with_ids, entries = assign_action_ids(rendered, date(2026, 8, 1), [])

    assert "[F" not in rendered
    assert "- [ ] 始業時に参照を一つに絞る｜PASS: context_switches <= 30｜FAIL: 31\n    - なぜ: 手順の開始条件を固定する\n    - 明日見る数字: コンテキスト切替回数" in rendered
    assert "- [ ] 終業前に集中枠を記録する｜PASS: focus_blocks >= 4｜FAIL: 3\n    - なぜ: 振り返りの時刻を決める\n    - 明日見る数字: 集中ブロック数" in rendered
    assert "### AI作業の見立て\n\n- 会話の往復数は測定不能なので品質の良否は断定しない" in rendered
    assert len(entries) == 2
    assert all(parse_pass_condition(entry.action) is not None for entry in entries)
    assert with_ids.count("KZN-20260801-") == 2


def test_d1_reader_advice_omits_unmatched_context_and_empty_ai_review() -> None:
    advice = (
        "### 今日の改善提案\n"
        "1. 開始条件を固定する。参照を一つに絞る。翌日見る指標: コンテキスト切替回数\n"
        "対応外の行\n\n"
        "### 明日の最小アクション\n"
        "- [ ] 一つ試す｜PASS: context_switches <= 30｜FAIL: 31\n"
        "- [ ] もう一つ試す｜PASS: focus_blocks >= 4｜FAIL: 3\n\n"
        "### AI作業の改善\n"
    )

    rendered = render_reader_advice(advice, _reader_evidence())

    assert "    - なぜ: 開始条件を固定する" in rendered
    assert "- [ ] もう一つ試す｜PASS: focus_blocks >= 4｜FAIL: 3\n    - なぜ:" not in rendered
    assert "### AI作業の見立て" not in rendered


def test_d1_reader_advice_matches_context_by_proposal_number_not_compacted_order() -> None:
    advice = (
        "### 今日の改善提案\n"
        "1. 壊れた提案行\n"
        "2. 二番目の解釈。二番目の提案。翌日見る指標: 集中ブロック数\n\n"
        "### 明日の最小アクション\n"
        "- [ ] 一番目を試す｜PASS: context_switches <= 30｜FAIL: 31\n"
        "- [ ] 二番目を試す｜PASS: focus_blocks >= 4｜FAIL: 3\n"
    )

    rendered = render_reader_advice(advice, _reader_evidence())

    assert "- [ ] 一番目を試す｜PASS: context_switches <= 30｜FAIL: 31\n    - なぜ:" not in rendered
    assert "- [ ] 二番目を試す｜PASS: focus_blocks >= 4｜FAIL: 3\n    - なぜ: 二番目の解釈\n    - 明日見る数字: 集中ブロック数" in rendered


def test_d1_reader_advice_keeps_internal_interpretation_periods_without_repeating_proposal() -> None:
    advice = (
        "### 今日の改善提案\n"
        "1. 観測に句点がある。補足もある。参照を一つに絞る。翌日見る指標: コンテキスト切替回数\n\n"
        "### 明日の最小アクション\n"
        "- [ ] 明日記録する｜PASS: context_switches <= 30｜FAIL: 31\n"
    )

    rendered = render_reader_advice(advice, _reader_evidence())

    assert "    - なぜ: 観測に句点がある。補足もある" in rendered
    assert "    - なぜ: 観測に句点がある。補足もある。参照を一つに絞る" not in rendered
    assert "参照を一つに絞る" not in rendered


def test_d1_reader_advice_collapses_spaces_after_fact_id_redaction() -> None:
    advice = (
        "### 今日の改善提案\n"
        "1. 観測された [F1] 変化。試す。翌日見る指標: 指標 [F2] の値\n\n"
        "### 明日の最小アクション\n"
        "- [ ] [F3] 明日記録する｜PASS: context_switches <= 30｜FAIL: 31\n\n"
        "### AI作業の改善\n"
        "- [F4] AI [F5] 作業 [F6] の見立て\n"
    )

    rendered = render_reader_advice(advice, _reader_evidence())

    assert "    - なぜ: 観測された 変化" in rendered
    assert "    - なぜ: 観測された  変化" not in rendered
    assert "    - 明日見る数字: 指標 の値" in rendered
    assert "    - 明日見る数字: 指標  の値" not in rendered
    assert "- AI 作業 の見立て" in rendered
    assert "- AI  作業  の見立て" not in rendered


def _baseline_stats(day: str, tool_errors: object, *, category: object = 10.0, site: object = 5.0) -> dict:
    return {
        "day": day,
        "total_minutes": 180.0,
        "context_switches": 10,
        "by_category": {"開発": category},
        "by_site": {"example.com": site},
        "ai": {"sessions": 1, "fragmented": 0, "tool_errors": tool_errors, "interruptions": 0},
    }


def _single_tool_error_action(pass_value: str) -> dict:
    data = _valid_data()
    data["proposals"] = data["proposals"][:1]
    data["actions"] = [{
        "fact_ids": ["F3"], "trigger": "始業の直後", "action": "試す",
        "estimated_minutes": 10,
        "pass": pass_value, "fail": "201",
        "mechanism": "小さな一歩が継続を助けると考える",
        "falsifier": "指標が目標を外れた場合",
    }]
    return data


def test_d2_history_baselines_skip_two_days_and_use_only_history_medians() -> None:
    current = _baseline_stats("2026-08-01", 999.0, category=999.0, site=999.0)
    short_history = [
        _baseline_stats("2026-07-30", 10.0, category=10.0, site=5.0),
        _baseline_stats("2026-07-31", 30.0, category=30.0, site=15.0),
    ]
    five_day_history = [
        _baseline_stats("2026-07-27", 101.0, category=10.0, site=5.0),
        _baseline_stats("2026-07-28", 770.0, category=30.0, site=15.0),
        _baseline_stats("2026-07-29", 121.0, category=20.0, site=10.0),
        _baseline_stats("2026-07-30", 178.0, category=40.0, site=25.0),
        _baseline_stats("2026-07-31", 639.0, category=50.0, site=35.0),
        _baseline_stats("2026-08-01", 1.0, category=1.0, site=1.0),
        _baseline_stats("not-a-date", 0.0, category=0.0, site=0.0),
    ]

    short = build_advice_evidence(current, short_history)
    full = build_advice_evidence(current, five_day_history)
    full = replace(full, structured_ai_metrics_available=True)

    assert "ai_tool_errors" not in (short.metric_baselines or {})
    assert not any("緩すぎ" in error for error in validate_advice(_single_tool_error_action("ai_tool_errors <= 500"), replace(short, structured_ai_metrics_available=True)))
    assert full.metric_baselines is not None
    assert full.metric_baselines["ai_tool_errors"] == 178.0
    assert full.metric_baselines["category_minutes:開発"] == 30.0
    assert full.metric_baselines["site_minutes:example.com"] == 15.0
    assert 999.0 not in full.metric_baselines.values()
    assert any("緩すぎ" in error for error in validate_advice(_single_tool_error_action("ai_tool_errors <= 200"), full))
    assert validate_advice(_single_tool_error_action("ai_tool_errors <= 169"), full) == []


@pytest.mark.parametrize("invalid", [True, nan, inf, -1.0, None], ids=["bool", "nan", "inf", "negative", "missing"])
def test_d2_history_baselines_exclude_invalid_raw_tool_error_values(invalid: object) -> None:
    history = [
        _baseline_stats("2026-07-29", invalid),
        _baseline_stats("2026-07-30", invalid),
        _baseline_stats("2026-07-31", invalid),
    ]

    evidence = build_advice_evidence(_baseline_stats("2026-08-01", 100.0), history)

    assert "ai_tool_errors" not in (evidence.metric_baselines or {})


@pytest.mark.parametrize(
    ("metric", "route"),
    [
        ("context_switches", "top"),
        ("total_active_minutes", "total"),
        ("ai_activity_blocks", "activity"),
        ("ai_cc_sessions", "sessions"),
        ("ai_fragmented_sessions", "fragmented"),
        ("ai_retry_chains", "retry_chains"),
        ("ai_tool_errors", "tool_errors"),
        ("ai_interruptions", "interruptions"),
        ("ai_output_tokens", "output_tokens"),
        ("focus_blocks", "focus_blocks"),
        ("focus_minutes", "focus_minutes"),
        ("input_keypresses", "keypresses"),
        ("ai_avg_turns", "avg_direct"),
        ("ai_avg_turns", "avg_turns_total"),
        ("ai_avg_turns", "avg_projects"),
    ],
)
def test_d2_history_baselines_reject_boolean_for_each_simple_metric_source(metric: str, route: str) -> None:
    def stats(day: str) -> dict:
        item = {
            "day": day,
            "total_minutes": 180.0,
            "context_switches": 10.0,
            "ai_activity_blocks": 2.0,
            "ai": {
                "sessions": 2.0, "fragmented": 1.0, "retry_chains": 1.0,
                "tool_errors": 1.0, "interruptions": 1.0, "output_tokens": 10.0,
                "avg_turns": 3.0,
            },
            "input": {"focus_blocks": 2.0, "focus_minutes": 30.0, "keypresses": 100.0},
        }
        if route == "top":
            item["context_switches"] = True
        elif route == "total":
            item["total_minutes"] = True
        elif route == "activity":
            item["ai_activity_blocks"] = True
        elif route in {"sessions", "fragmented", "retry_chains", "tool_errors", "interruptions", "output_tokens"}:
            item["ai"][route] = True
        elif route in {"focus_blocks", "focus_minutes", "keypresses"}:
            item["input"][route] = True
        elif route == "avg_direct":
            item["ai"]["avg_turns"] = True
        elif route == "avg_turns_total":
            item["ai"].pop("avg_turns")
            item["ai"]["turns_total"] = True
        else:
            item["ai"].pop("avg_turns")
            item["ai"]["projects"] = {"repo": {"turns": True}}
        return item

    evidence = build_advice_evidence(
        stats("2026-08-01"),
        [stats("2026-07-29"), stats("2026-07-30"), stats("2026-07-31")],
    )

    assert metric not in (evidence.metric_baselines or {})


def test_d2_repair_hint_labels_baselines_as_history_medians() -> None:
    evidence = replace(_reader_evidence(), metric_baselines={"context_switches": 20.0})

    assert _baseline_repair_hint(evidence).startswith(
        "## ベースライン（直近履歴の中央値・PASS はこの値より挑戦的に）\n"
    )


def test_d2_input_history_builds_medians_without_ai_mapping() -> None:
    def stats(day: str, focus_blocks: float, focus_minutes: float, keypresses: float) -> dict:
        return {
            "day": day,
            "total_minutes": 180.0,
            "context_switches": 10.0,
            "input": {
                "focus_blocks": focus_blocks,
                "focus_minutes": focus_minutes,
                "keypresses": keypresses,
            },
        }

    evidence = build_advice_evidence(
        stats("2026-08-01", 9.0, 90.0, 900.0),
        [
            stats("2026-07-29", 2.0, 20.0, 200.0),
            stats("2026-07-30", 4.0, 40.0, 400.0),
            stats("2026-07-31", 6.0, 60.0, 600.0),
        ],
    )

    assert evidence.metric_baselines is not None
    assert evidence.metric_baselines["focus_blocks"] == 4.0
    assert evidence.metric_baselines["focus_minutes"] == 40.0
    assert evidence.metric_baselines["input_keypresses"] == 400.0


def test_d2_duplicate_history_days_are_excluded_and_order_independent() -> None:
    current = _baseline_stats("2026-08-01", 999.0)
    history = [
        _baseline_stats("2026-07-27", 10.0),
        _baseline_stats("2026-07-28", 20.0),
        _baseline_stats("2026-07-29", 30.0),
        _baseline_stats("2026-07-29", 300.0),
        _baseline_stats("2026-07-30", 40.0),
        _baseline_stats("2026-07-31", 50.0),
    ]

    forward = build_advice_evidence(current, history)
    reverse = build_advice_evidence(current, list(reversed(history)))
    forward_records = _reader_history_with_current(current, history)
    reverse_records = _reader_history_with_current(current, list(reversed(history)))

    assert forward.metric_baselines is not None
    assert forward.metric_baselines["ai_tool_errors"] == 30.0
    assert forward.metric_baselines == reverse.metric_baselines
    assert [day for day, _ in forward_records] == [
        date(2026, 7, 27), date(2026, 7, 28), date(2026, 7, 30),
        date(2026, 7, 31), date(2026, 8, 1),
    ]
    assert forward_records == reverse_records


def test_d2_baseline_and_repair_hint_order_are_deterministic() -> None:
    def stats(day: str) -> dict:
        return {
            "day": day,
            "total_minutes": 180.0,
            "context_switches": 10.0,
            "ai_activity_blocks": 2.0,
            "by_category": {"zeta": 20.0, "alpha": 10.0},
            "by_site": {"zeta.example": 20.0, "alpha.example": 10.0},
            "ai": {
                "sessions": 2.0, "fragmented": 1.0, "retry_chains": 1.0,
                "tool_errors": 1.0, "interruptions": 1.0, "avg_turns": 3.0,
                "output_tokens": 10.0,
            },
            "input": {"focus_blocks": 2.0, "focus_minutes": 30.0, "keypresses": 100.0},
        }

    current = stats("2026-08-01")
    history = [stats("2026-07-29"), stats("2026-07-30"), stats("2026-07-31")]
    baselines = build_advice_evidence(current, list(reversed(history))).metric_baselines
    hint = _baseline_repair_hint(
        replace(_reader_evidence(), metric_baselines={"zeta": 2.0, "alpha": 1.0, "mu": 3.0})
    )

    assert list(baselines or {}) == [
        "context_switches", "context_switches_per_hour", "total_active_minutes",
        "ai_activity_blocks",
        "ai_cc_sessions", "ai_fragmented_sessions", "ai_retry_chains",
        "ai_tool_errors", "ai_tool_errors_per_session", "ai_interruptions",
        "ai_avg_turns", "ai_output_tokens",
        "focus_blocks", "focus_minutes", "input_keypresses",
        "category_minutes:alpha", "category_minutes:zeta",
        "site_minutes:alpha.example", "site_minutes:zeta.example",
    ]
    assert hint.endswith("alpha=1 / mu=3 / zeta=2\n\n")
