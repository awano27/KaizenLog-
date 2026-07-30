"""第34弾: 日誌可読性と計測正直化。"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from kaizenlog.advice_evidence import _build_reader_summary, _fmt_duration_ja
from kaizenlog.aiwork import (
    AISession,
    RetryChain,
    UserPrompt,
    _is_command_wrapper,
    _is_system_wrapper,
    compute_loop_tax,
    extract_session_title,
    format_loop_tax_line,
    render_aiwork_markdown,
    retry_chain_excerpts,
    scan_sessions,
    session_title_from_text,
)
from kaizenlog.memory import MemoryEntry, render_actions_section
from kaizenlog.vault import ACTIONS_MARKER, upsert_section


TZ = timezone.utc
T0 = datetime(2026, 7, 30, 10, 0, tzinfo=TZ)


def _sess(
    sid: str,
    *,
    tokens: int = 1000,
    turns: int = 3,
    tools: int = 2,
    edits: int = 1,
    errors: int = 0,
    source: str = "claude-code",
    title: str | None = None,
    project: str = "repo",
) -> AISession:
    s = AISession(
        session_id=sid,
        project=project,
        start=T0,
        end=T0 + timedelta(minutes=30),
        user_turns=turns,
        tool_counts=Counter({"Read": tools}),
        tool_errors=errors,
        output_tokens=tokens,
        models={"claude-sonnet-4"},
        source=source,
        edits=edits,
        title=title,
    )
    return s


def _p(mins: int, *, project: str = "repo", text: str = "please fix the bug carefully") -> UserPrompt:
    return UserPrompt(
        timestamp=T0 + timedelta(minutes=mins),
        project=project,
        text=text,
    )


# ---- §A1 ----

def test_a1_system_wrapper_real_examples_and_boundary():
    samples = [
        '<task-notification> <task-id>a1d9fdd05e0</task-id>',
        '<in-app-browser-context source="ambient-foo">',
        '<scheduled-task name="daily-news-coverag',
        "<system-reminder>do not",
        "<command-name>foo</command-name>",
        "<local-command-stdout>x</local-command-stdout>",
    ]
    for s in samples:
        assert _is_system_wrapper(s), s
        assert _is_command_wrapper(s), s  # alias
        assert extract_session_title(s) is None

    # タグ形式でない < 始まりは除外しない
    plain = "< 1000円で買えるPCを調べて"
    assert not _is_system_wrapper(plain)
    got = extract_session_title(plain)
    assert got is not None
    assert "1000円" in got[0]


def test_a1_title_fallback_skips_xml_then_user():
    # extract が XML で None → 次の実発話を title にするのは _maybe_set_title 側
    # ここでは extract の段階と session_title のフォールバック前提を固定
    assert extract_session_title('<task-notification> x') is None
    t, n = extract_session_title("実際の依頼: ログを直して")
    assert "実際の依頼" in t
    assert n > 0


# ---- §A2 ----

def test_a2_loop_tax_session_dedupe_across_episodes():
    # 同一セッションが2チェーンに触れる → 旧実装なら倍増、新実装で1回
    s1 = _sess("shared", tokens=100_000)
    c1 = RetryChain(project="repo", prompts=[_p(0), _p(5)])
    c2 = RetryChain(project="repo", prompts=[_p(10), _p(15)])
    tax = compute_loop_tax([c1, c2], [s1])
    assert tax.episode_count == 2
    # per-episode は各 100k（現行維持）
    assert tax.episodes[0].wasted_tokens == 100_000
    assert tax.episodes[1].wasted_tokens == 100_000
    # 合計は一意 → 100k（倍増しない）
    assert tax.total_wasted_tokens == 100_000
    # 不変条件: total <= 関与セッション output 一意合計
    assert tax.total_wasted_tokens <= 100_000
    line = format_loop_tax_line(tax)
    assert "エピソード間で同一セッションは1回のみ計上" in line
    assert "100000 tokens" in line or "100,000" in line or "100000" in line


# ---- §A3 ----

def test_a3_codex_tool_error_note_and_money_unknown():
    s_codex = _sess("c1", errors=5, source="codex")
    s_cc = _sess("c2", errors=1, source="claude-code")
    md = render_aiwork_markdown([s_codex, s_cc], TZ)
    assert "codexは文字列判定・過大計上の可能性" in md

    md2 = render_aiwork_markdown([s_cc], TZ)
    assert "過大計上" not in md2

    # 金額不明表記
    chain = RetryChain(project="repo", prompts=[_p(0), _p(5)])
    sess = _sess("s1", tokens=1_000_000)
    sess.models = {"unknown-model-xyz"}
    tax = compute_loop_tax([chain], [sess], pricing={})
    line = format_loop_tax_line(tax)
    assert "金額不明" in line
    assert "$-.--" not in line


# ---- §B1 ----

def test_b1_action_wording_and_pass_separation_byte_safe():
    day = date(2026, 7, 30)
    handwritten = b"# Note\n\nKEEP_HANDWRITTEN\n"

    entries = [
        MemoryEntry(
            id="KZN-20260728-001",
            date="2026-07-28",
            action="do A",
            status="proposed",
            verdict="pass",
            verdict_value=1.0,
            verdict_date="2026-07-29",
        ),
        MemoryEntry(
            id="KZN-20260729-001",
            date="2026-07-29",
            action="do B",
            status="proposed",
        ),
    ]
    section = render_actions_section(entries, day, None)
    assert section is not None
    assert "実行済みPASS 0件" in section
    assert "未実行のままPASS到達" in section
    assert "チェックなしで指標が目標値に達した提案" in section
    assert "指標は達成済み（習慣化するならチェック）" in section
    assert "KZN-20260728-001" in section
    # PASS 達成済みは小見出し下、通常未完了と分離
    idx_h = section.index("指標は達成済み")
    idx_pass = section.index("KZN-20260728-001")
    idx_open = section.index("KZN-20260729-001")
    assert idx_open < idx_h < idx_pass

    # マーカー内 upsert で手書き不可侵（改行正規化後も本文保持）
    content = handwritten.decode("utf-8")
    updated = upsert_section(content, ACTIONS_MARKER, section, position="bottom")
    assert "KEEP_HANDWRITTEN" in updated
    assert updated.index("KEEP_HANDWRITTEN") < updated.index("kaizenlog:actions:start")
    assert "指標は達成済み" in updated


# ---- §B2 ----

def test_b2_path_basename_and_tiny_omit_and_retry_excerpt():
    long_path = (
        r"C:\develop\KaizenLog\docs\codex-prompts\0730_Grok_申し送りROI.md"
    )
    title = session_title_from_text(long_path, max_chars=40)
    assert "0730_Grok" in title or title.endswith(".md") or "..." in title
    assert not title.startswith(r"C:\develop")

    tiny = _sess("t1", turns=0, tools=1, edits=0, title="noise")
    real = _sess("r1", turns=4, tools=3, edits=2, title="real work")
    md = render_aiwork_markdown([tiny, real], TZ, max_rows=15)
    assert "real work" in md
    assert "短小セッション 1件" in md
    # tiny の行は出ない（時刻行に noise が載らない）
    assert "| noise |" not in md

    # リトライ起点 30字+…
    long_text = "あ" * 50
    chain = RetryChain(project="p", prompts=[_p(0, text=long_text), _p(5, text=long_text)])
    ex = retry_chain_excerpts([chain])
    assert ex
    assert ex[0].endswith("…") or "…" in ex[0]
    # 30字+ellipsis
    body = ex[0].split(": ", 1)[-1]
    assert len(body.replace("…", "")) <= 30


# ---- §B3 ----

def test_b3_conclusion_uses_hours_minutes():
    assert _fmt_duration_ja(172.9) == "2時間53分"
    assert _fmt_duration_ja(45) == "45分"
    text = _build_reader_summary(
        total_minutes=172.9,
        short_record=False,
        stats={"total_minutes": 172.9, "by_category": {}},
        history=None,
        by_category={},
        category_stats_valid=False,
        entertainment_minutes=None,
        previous_day_available=False,
    )
    assert "2時間53分" in text
    assert "172.9分" not in text


# ---- §B1 追補: 達成済み超過分の無言消失防止(レビュー検出バグの回帰) ----

def test_b1_pass_achieved_overflow_counted():
    entries = [
        MemoryEntry(
            id=f"KZN-20260729-00{i}",
            date="2026-07-29",
            action=f"act {i}",
            status="proposed",
            verdict="pass",
            verdict_value=1.0,
            verdict_date="2026-07-29",
        )
        for i in range(1, 6)
    ]
    section = render_actions_section(entries, date(2026, 7, 30), None)
    assert section is not None
    shown = sum(1 for e in entries if e.id in section)
    assert shown == 3  # 表示は上限3件
    assert "ほか達成済み 2件" in section  # 超過分が無言で消えない


# ---- §B2 追補: システムXML→実発話フォールバックのE2E(scan_sessions経由) ----

def test_b2_scan_sessions_title_fallback_e2e(tmp_path: Path):
    import json as _json

    def _rec_user(ts: str, text: str) -> dict:
        return {
            "type": "user",
            "sessionId": "s1",
            "timestamp": ts,
            "message": {"role": "user", "content": text},
        }

    records = [
        _rec_user(
            "2026-07-30T09:00:00+00:00",
            "<task-notification> <task-id>abc123</task-id>",
        ),
        _rec_user("2026-07-30T09:01:00+00:00", "READMEのヒーローを直して"),
    ]
    p = tmp_path / "-c-develop-myproj" / "s1.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("\n".join(_json.dumps(r) for r in records), encoding="utf-8")

    day_start = datetime(2026, 7, 30, 0, 0, tzinfo=TZ)
    sessions = scan_sessions(tmp_path, day_start, day_start + timedelta(days=1))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.user_turns == 1  # システムXMLは往復に数えない
    assert s.title and "task-notification" not in s.title
    assert "README" in s.title  # 次の実発話にフォールバック
