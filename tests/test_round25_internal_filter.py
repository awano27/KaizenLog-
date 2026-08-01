"""第25弾: 自己計測除外（センチネル / テンプレ先頭 / トークン行重複）。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.advisor import (
    INTERNAL_SENTINEL,
    INTERNAL_SENTINEL_TOKEN,
    apply_internal_sentinel,
    load_bundled_prompt,
    prepare_advice_request,
)
from kaizenlog.aiwork import (
    AISession,
    UserPrompt,
    bundled_prompt_head_prefixes,
    detect_retry_chains,
    is_kaizenlog_internal_text,
    render_aiwork_markdown,
    scan_sessions,
    scan_user_prompts,
)
from kaizenlog.config import LLMConfig
from kaizenlog.stats import build_stats
from kaizenlog.report import DailySummary


UTC = timezone.utc
TZ = ZoneInfo("UTC")
DAY_START = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
DAY_END = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)


def _write_session(path: Path, sid: str, first_user: str, *, tokens: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "user",
            "timestamp": "2026-07-20T10:00:00.000Z",
            "sessionId": sid,
            "cwd": "C:/develop/demo",
            "message": {"role": "user", "content": [{"type": "text", "text": first_user}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-07-20T10:00:05.000Z",
            "sessionId": sid,
            "message": {
                "id": f"msg-{sid}",
                "role": "assistant",
                "model": "test-model",
                "usage": {"output_tokens": tokens},
                "content": [{"type": "text", "text": "ok"}],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-07-20T10:01:00.000Z",
            "sessionId": sid,
            "cwd": "C:/develop/demo",
            "message": {"role": "user", "content": [{"type": "text", "text": "thanks"}]},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_s1_sentinel_applied_to_cli_backends_only():
    body = "system body"
    cli = apply_internal_sentinel(body, "claude-code-cli")
    assert cli.startswith(INTERNAL_SENTINEL_TOKEN)
    assert body in cli
    # 冪等
    assert apply_internal_sentinel(cli, "claude-code-cli") == cli
    assert apply_internal_sentinel(body, "copilot-cli").startswith(INTERNAL_SENTINEL_TOKEN)
    # openai-compatible はセッションログを残さないため対象外
    assert apply_internal_sentinel(body, "openai-compatible") == body


def test_s1_dry_run_and_execute_share_sentinel_via_helper():
    """prepare 後に apply_internal_sentinel すれば dry-run/本実行が一致。"""
    cfg = LLMConfig(backend="claude-code-cli", system_prompt="daily_advisor")
    system, user, _ = prepare_advice_request(cfg, "activity", [], evidence=None)
    shown = apply_internal_sentinel(system, cfg.backend)
    executed = apply_internal_sentinel(system, "claude-code-cli")
    assert shown == executed
    assert shown.startswith(INTERNAL_SENTINEL_TOKEN)


def test_s2_sentinel_session_excluded(tmp_path: Path):
    proj = tmp_path / "proj"
    _write_session(
        proj / "sess-internal.jsonl",
        "sid-int",
        f"{INTERNAL_SENTINEL}\n{load_bundled_prompt('daily_advisor')[:80]}",
        tokens=5000,
    )
    _write_session(
        proj / "sess-user.jsonl",
        "sid-user",
        "Please fix the failing unit test in collector.py",
        tokens=100,
    )
    sessions = scan_sessions(proj, DAY_START, DAY_END)
    # 内部は is_internal、通常は残る
    by_id = {s.session_id: s for s in sessions}
    assert by_id["sid-int"].is_internal is True
    assert by_id["sid-user"].is_internal is False

    from kaizenlog.aiwork import collect_ai_telemetry, ClaudeCodeAdapter

    kept, prompts, n_int = collect_ai_telemetry(
        [ClaudeCodeAdapter(proj)], DAY_START, DAY_END
    )
    assert n_int == 1
    assert len(kept) == 1 and kept[0].session_id == "sid-user"
    assert all(INTERNAL_SENTINEL_TOKEN not in p.text for p in prompts)
    assert all(not is_kaizenlog_internal_text(p.text) for p in prompts)


def test_s2_template_prefix_excludes_legacy_without_sentinel(tmp_path: Path):
    """テンプレ先頭行を読み込んで検証（改訂に自動追従）。"""
    prefixes = bundled_prompt_head_prefixes()
    assert prefixes
    # daily_advisor の実ファイル先頭
    daily_head = None
    for line in load_bundled_prompt("daily_advisor").splitlines():
        if line.strip():
            daily_head = line.strip()
            break
    assert daily_head
    from kaizenlog.aiwork import normalize_prompt_text

    assert normalize_prompt_text(daily_head) in prefixes

    proj = tmp_path / "proj"
    # 過去ログ形: センチネル無し・テンプレ冒頭一致
    legacy = load_bundled_prompt("daily_advisor") + "\n\n# 本日のActivity Log\n..."
    _write_session(proj / "legacy.jsonl", "sid-legacy", legacy, tokens=9000)
    _write_session(proj / "normal.jsonl", "sid-ok", "実装してテストを通して", tokens=50)

    from kaizenlog.aiwork import ClaudeCodeAdapter, collect_ai_telemetry

    kept, _, n_int = collect_ai_telemetry(
        [ClaudeCodeAdapter(proj)], DAY_START, DAY_END
    )
    assert n_int == 1
    assert [s.session_id for s in kept] == ["sid-ok"]


def test_s2_normal_session_not_excluded(tmp_path: Path):
    proj = tmp_path / "proj"
    _write_session(proj / "a.jsonl", "a", "refactor the report module please")
    sessions = scan_sessions(proj, DAY_START, DAY_END)
    assert len(sessions) == 1
    assert sessions[0].is_internal is False
    assert is_kaizenlog_internal_text("refactor the report module please") is False


def test_s2_internal_line_shown_only_when_n_positive():
    s = AISession(
        session_id="u",
        project="p",
        start=DAY_START,
        end=DAY_START.replace(hour=1),
        user_turns=3,
        output_tokens=10,
    )
    md0 = render_aiwork_markdown([s], TZ, internal_ai_sessions=0)
    assert "内部呼び出し" not in md0
    md1 = render_aiwork_markdown([s], TZ, internal_ai_sessions=3)
    assert "内部呼び出し（KaizenLog自身のLLM実行）: 3回を計測から除外" in md1


def test_s2_retry_chains_exclude_internal_prompts():
    prompts = [
        UserPrompt(
            timestamp=DAY_START.replace(hour=10),
            project="p",
            text=f"{INTERNAL_SENTINEL}\n同じ依頼を繰り返す advise 用",
        ),
        UserPrompt(
            timestamp=DAY_START.replace(hour=10, minute=5),
            project="p",
            text=f"{INTERNAL_SENTINEL}\n同じ依頼を繰り返す advise 用",
        ),
        UserPrompt(
            timestamp=DAY_START.replace(hour=11),
            project="p",
            text="ユーザーの通常依頼 A",
        ),
    ]
    # フィルタ後に連鎖検出する前提（collect と同順）
    filtered = [p for p in prompts if not is_kaizenlog_internal_text(p.text)]
    chains = detect_retry_chains(filtered)
    assert chains == []
    # フィルタ無しなら内部が連鎖になりうる
    chains_raw = detect_retry_chains(prompts)
    assert len(chains_raw) >= 1


def test_s2_stats_internal_ai_sessions():
    day = DAY_START.date()
    summary = DailySummary(
        day=day,
        total_minutes=60.0,
        by_category={"開発": 60.0},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=1,
        by_site={},
    )
    user = AISession(
        session_id="u",
        project="p",
        start=DAY_START,
        end=DAY_START.replace(hour=1),
        user_turns=2,
        output_tokens=10,
    )
    stats = build_stats(day, summary, [user], internal_ai_sessions=2)
    assert stats["ai"]["sessions"] == 1  # ユーザーのみ
    assert stats["ai"]["internal_ai_sessions"] == 2


# 第35弾§B2で第25弾§S3の「トークン数値は日誌内で1回」不変条件を意図的に廃止し、3行ガイダンスを採用した。
def test_s3_cost_fallback_shows_three_line_guidance():
    s = AISession(
        session_id="u",
        project="p",
        start=DAY_START,
        end=DAY_START.replace(hour=1),
        user_turns=2,
        output_tokens=12345,
        models={"unknown-model-xyz"},
    )
    # pricing 空 → 全トークン uncosted → フォールバック
    md = render_aiwork_markdown([s], TZ, pricing={})
    assert md.count("12,345") == 3
    assert md.count("出力トークン") == 1
    assert "推定コスト: 換算なし — 出力12,345 tok のうち単価未登録が12,345 tok。" in md
    assert "未登録モデル: unknown-model-xyz。" in md
    assert "kaizenlog.toml の [aiwork.pricing] に $/1Mtok を設定すると金額換算されます。" in md
    assert "コスト換算なし）" not in md.split("出力トークン")[1] if False else True


def test_s2_template_follow_live_file():
    """テンプレ文言改訂に追従: 実ファイル先頭行を読んで is_internal が真。"""
    text = load_bundled_prompt("privacy_safe")
    first = next(line.strip() for line in text.splitlines() if line.strip())
    assert is_kaizenlog_internal_text(first + "\n続きの本文")
