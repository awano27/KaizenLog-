"""第46弾 + 第47弾残件: 私的タイトル伏せ・重複圧縮・セッション入出力・日報成果ベース。"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.aiwork import (
    AISession,
    finalize_session_io_digest,
    render_aiwork_markdown,
    session_digests_for_stats,
)
from kaizenlog.aiwork_browser import BrowserAIAdapter
from kaizenlog.aiwork_codex import CodexAdapter, _SessionAccum
from kaizenlog.nippou import generate_nippou_deterministic, _project_work_lines
from kaizenlog.privacy_filter import is_private_block
from kaizenlog.report import Block, DailySummary, render_markdown, _fmt_minutes

TZ = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 8, 2)
UTC = timezone.utc


def _block(
    h: int,
    m: float,
    cat: str,
    app: str = "app.exe",
    title: str = "t",
    *,
    ai: bool = False,
) -> Block:
    start = datetime(2026, 8, 2, h, 0, tzinfo=TZ)
    end = start + timedelta(minutes=m)
    return Block(start, end, cat, app, [title], ai=ai)


def _summary(blocks: list[Block], total: float | None = None) -> DailySummary:
    t = total if total is not None else sum(b.minutes for b in blocks)
    by_cat: dict[str, float] = {}
    for b in blocks:
        by_cat[b.category] = by_cat.get(b.category, 0.0) + b.minutes
    return DailySummary(
        day=DAY,
        total_minutes=t,
        by_category=by_cat,
        by_app={},
        blocks=blocks,
        ai_tool_minutes={},
        ai_sessions=sum(1 for b in blocks if b.ai),
        context_switches=0,
        by_site={},
    )


def test_f1_private_title_hidden_counts_unchanged():
    blocks = [
        _block(17, 5, "エンタメ", "brave.exe", "【超涼しい】材料費たったの1,500円で自作クーラー"),
        _block(10, 30, "開発", "Code.exe", "main.py"),
    ]
    s = _summary(blocks, total=35.0)
    md = render_markdown(s, TZ, min_block_minutes=3.0, hide_private_titles=True)
    # 第48弾 §D3: 私的はタイムラインで（私的）集計1行
    assert "（私的）" in md
    assert "自作クーラー" not in md
    assert "エンタメ" in md
    # カテゴリ表の分数は不変
    assert "| エンタメ |" in md
    assert re.search(r"\| エンタメ \|[^|]*\| *5m *\|", md) or "| 5m |" in md


def test_f1_hide_private_false_shows_title():
    blocks = [
        _block(17, 5, "エンタメ", "brave.exe", "【超涼しい】材料費クーラー"),
    ]
    s = _summary(blocks, total=5.0)
    md = render_markdown(s, TZ, min_block_minutes=3.0, hide_private_titles=False)
    assert "材料費クーラー" in md
    assert "（私的・非表示）" not in md


def test_f2_collapse_three_or_more():
    blocks = [
        _block(11, 5, "AI作業", "claude.exe", "KaizenLog-: 実施しました、確認して", ai=True),
        _block(12, 5, "AI作業", "claude.exe", "KaizenLog-: 実施しました、確認して", ai=True),
        _block(13, 5, "AI作業", "claude.exe", "KaizenLog-: 実施しました、確認して", ai=True),
        _block(14, 5, "AI作業", "claude.exe", "KaizenLog-: 実施しました、確認して", ai=True),
    ]
    # shift minutes so consecutive hours
    for i, b in enumerate(blocks):
        b.start = datetime(2026, 8, 2, 11, i * 10, tzinfo=TZ)
        b.end = b.start + timedelta(minutes=5)
    s = _summary(blocks, total=20.0)
    md = render_markdown(s, TZ, min_block_minutes=3.0)
    assert "(4回)" in md
    assert "計" in md
    # only one data row with that content in timeline (plus maybe fragments none)
    content_rows = [
        ln for ln in md.splitlines()
        if "実施しました" in ln and ln.startswith("|")
    ]
    assert len(content_rows) == 1


def test_f2_two_rows_not_collapsed():
    blocks = [
        _block(11, 5, "AI作業", "claude.exe", "same title here long enough", ai=True),
        _block(11, 5, "AI作業", "claude.exe", "same title here long enough", ai=True),
    ]
    blocks[1].start = blocks[0].start + timedelta(minutes=10)
    blocks[1].end = blocks[1].start + timedelta(minutes=5)
    s = _summary(blocks, total=10.0)
    md = render_markdown(s, TZ, min_block_minutes=3.0)
    assert "2回" not in md
    rows = [ln for ln in md.splitlines() if "same title" in ln]
    assert len(rows) == 2


def test_f2_coverage_unchanged_after_collapse():
    blocks = [
        _block(11, 5, "開発", "Code.exe", "dup content xxx"),
        _block(11, 5, "開発", "Code.exe", "dup content xxx"),
        _block(11, 5, "開発", "Code.exe", "dup content xxx"),
    ]
    for i, b in enumerate(blocks):
        b.start = datetime(2026, 8, 2, 11, i * 10, tzinfo=TZ)
        b.end = b.start + timedelta(minutes=5)
    s = _summary(blocks, total=15.0)
    md = render_markdown(s, TZ, min_block_minutes=3.0)
    # 3行が1行に圧縮されても、表が説明する時間は 15分=100% のまま変わらない
    assert "計15m (3回)" in md, "3行の連続重複が圧縮されていない"
    cov = re.search(r"この表は合計 ([^ ]+) の (\d+)% を説明", md)
    assert cov, "被覆率フッタが出ていない"
    assert cov.group(2) == "100", f"圧縮で時間が失われた: {cov.group(0)}"
    # 圧縮行が1本だけで、元の3行は消えている
    assert md.count("dup content xxx") == 1


def test_f3_prompts_digest_max_three():
    s = AISession(
        session_id="s1",
        project="p",
        start=datetime(2026, 8, 2, 10, tzinfo=TZ),
        end=datetime(2026, 8, 2, 11, tzinfo=TZ),
        user_turns=5,
    )
    s._user_prompts_raw = [
        "first prompt text long enough",
        "second",
        "third",
        "fourth",
        "fifth last prompt",
    ]
    finalize_session_io_digest(s)
    assert len(s.prompts_digest) == 3
    assert s.prompts_digest[0].startswith("first")
    assert s.prompts_digest[-1].startswith("fifth")
    assert all(len(p) <= 60 for p in s.prompts_digest)


def test_f3_files_basename_only():
    s = AISession(
        session_id="s1",
        project="p",
        start=datetime(2026, 8, 2, 10, tzinfo=TZ),
        end=datetime(2026, 8, 2, 11, tzinfo=TZ),
    )
    from kaizenlog.aiwork import _note_tool_use

    _note_tool_use(s, "Edit", {"file_path": r"C:\develop\KaizenLog\nippou.py"})
    _note_tool_use(s, "Write", {"path": r"C:\x\report.py"})
    _note_tool_use(s, "Edit", {"file_path": r"C:\develop\KaizenLog\nippou.py"})  # dup
    finalize_session_io_digest(s)
    assert s.files_touched == ["nippou.py", "report.py"]
    assert not any("\\" in f or "/" in f for f in s.files_touched)


def test_f3_commands_head_only():
    s = AISession(
        session_id="s1",
        project="p",
        start=datetime(2026, 8, 2, 10, tzinfo=TZ),
        end=datetime(2026, 8, 2, 11, tzinfo=TZ),
    )
    from kaizenlog.aiwork import _note_tool_use

    _note_tool_use(s, "Bash", {"command": "pytest tests/test_x.py -q"})
    _note_tool_use(s, "Bash", {"command": "git status --short"})
    _note_tool_use(s, "Bash", {"command": "pytest tests/y.py"})
    finalize_session_io_digest(s)
    assert "pytest" in s.commands_run
    assert "git" in s.commands_run
    assert not any("tests/" in c or "--" in c for c in s.commands_run)


def test_f3_redact_in_digests():
    s = AISession(
        session_id="s1",
        project="SECRET_PROJ",
        start=datetime(2026, 8, 2, 10, tzinfo=TZ),
        end=datetime(2026, 8, 2, 12, tzinfo=TZ),
        user_turns=3,
        title="do SECRET_TOKEN work now please",
    )
    s._user_prompts_raw = ["please fix SECRET_TOKEN in the code carefully"]
    s._files_order = ["secret_file.py"]
    digests = session_digests_for_stats(
        [s],
        "2026-08-02",
        redactor=lambda t: t.replace("SECRET_TOKEN", "[R]").replace("SECRET_PROJ", "[P]"),
    )
    assert digests
    d = digests[0]
    assert "SECRET_TOKEN" not in d["title"]
    assert "SECRET_TOKEN" not in " ".join(d["prompts_digest"])
    assert "[R]" in " ".join(d["prompts_digest"]), "redact 後の置換文字が入っていない"
    # 仕込んだ全フィールドを検証する（保存されるものは漏れなく redact を通す）
    assert "SECRET_TOKEN" not in json.dumps(d, ensure_ascii=False)
    assert d["files_touched"] == ["secret_file.py"]  # basename のみ・パスは入らない
    # project は既存仕様どおり redact 対象外（工数側は第45弾で別途 redact 済み）
    assert d["project"] == "SECRET_PROJ"


def test_f3_session_details_top3_and_toggle():
    # 往復が十分な4件: 4件目は詳細に出ない（tiny除外ではない境界）
    sessions = []
    for i, turns in enumerate((15, 12, 10, 8)):
        s = AISession(
            session_id=f"s{i}",
            project=f"proj{i}",
            start=datetime(2026, 8, 2, 10 + i, tzinfo=TZ),
            end=datetime(2026, 8, 2, 11 + i, tzinfo=TZ),
            user_turns=turns,
            edits=i + 1,
        )
        s._user_prompts_raw = [f"prompt number {i} long enough text"]
        s._files_order = [f"file{i}.py"]
        sessions.append(s)
    md = render_aiwork_markdown(sessions, TZ, session_details=True)
    assert "主なセッションの中身" in md
    detail = md.split("#### 主なセッションの中身", 1)[1]
    assert "proj0" in detail and "proj1" in detail and "proj2" in detail
    assert "proj3" not in detail  # only top 3 by turns in detail section
    md_off = render_aiwork_markdown(sessions, TZ, session_details=False)
    assert "主なセッションの中身" not in md_off


def test_f3_no_sessions_no_details_section():
    s = AISession(
        session_id="tiny",
        project="p",
        start=datetime(2026, 8, 2, 10, tzinfo=TZ),
        end=datetime(2026, 8, 2, 10, 1, tzinfo=TZ),
        user_turns=0,
        edits=0,
    )
    md = render_aiwork_markdown([s], TZ, session_details=True)
    # tiny session omitted from table; no details
    assert "主なセッションの中身" not in md


def test_f4_nippou_effort_based_no_prompt_fragments():
    stats = {
        "day": "2026-08-02",
        "total_minutes": 200.0,
        "goal_text": "アプリを完成させる",
        "by_category": {"AI作業": 60.0, "開発": 100.0},
        "blocks": [],
        "effort": {
            "minutes": {
                "KaizenLog-": 128.0,
                "ai-news-site": 31.0,
                "（調査・共通）": 115.0,
                "（未分類）": 40.0,
                "（私的）": 50.0,
            },
            "total_minutes": 364.0,
        },
        "ai": {
            "session_digests": [
                {
                    "project": "KaizenLog-",
                    "title": "みて想定通り価値のある日誌ができているか評価して改善案を提案してください",
                    "user_turns": 15,
                    "edits": 46,
                    "tools_total": 50,
                    "source": "claude-code",
                    "tests_run": True,
                    "files_touched": ["nippou.py", "report.py", "cli.py"],
                    "prompts_digest": ["長いプロンプト断片は出さないこと"],
                }
            ]
        },
        "outcome_git": [
            {"repo_label": "KaizenLog-", "commits": 11, "insertions": 1, "deletions": 0}
        ],
    }
    md = generate_nippou_deterministic(stats, TZ)
    assert "本日の業務" in md
    assert "KaizenLog-" in md
    # 第48弾 §B2: subject が無いとき prompts_digest をテーマに（ファイル名羅列ではない）
    assert "長いプロンプト断片は出さないこと" in md
    assert "nippou.py" not in md
    assert "コミット11件" in md
    assert "評価して改善案" not in md  # title 断片は出さない
    assert "エンタメ" not in md
    assert "私的" not in md
    assert "調査・未分類" in md


def test_f4_fallback_without_effort():
    stats = {
        "day": "2026-08-02",
        "total_minutes": 100.0,
        "by_category": {"AI作業": 50.0},
        "blocks": [],
        "ai": {
            "session_digests": [
                {
                    "project": "Foo",
                    "title": "x",
                    "user_turns": 5,
                    "edits": 3,
                    "tools_total": 10,
                    "source": "claude-code",
                    "files_touched": ["a.py"],
                }
            ]
        },
    }
    lines = _project_work_lines(stats)
    assert lines
    assert "Foo" in lines[0]
    assert "セッション" in lines[0]


def test_privacy_filter_shared():
    assert is_private_block(category="エンタメ", title="x")
    assert is_private_block(category="ブラウジング", title="YouTube - music")
    assert not is_private_block(category="開発", title="main.py")


# ---- §G2 / §G3 / §G4 (第47弾) ---------------------------------------------


def test_g2_collapse_split_by_fragment():
    """A, A, 細切れ, A → A×3 融合しない（前2は2行、後1は単独）。"""
    # eligible: 3 × 5min same content; frag: 1min under threshold between 2nd and 3rd
    a1 = _block(11, 5, "開発", "Code.exe", "same-dup-content-zzz")
    a2 = _block(11, 5, "開発", "Code.exe", "same-dup-content-zzz")
    frag = _block(11, 1, "開発", "Code.exe", "tiny frag row")
    a3 = _block(11, 5, "開発", "Code.exe", "same-dup-content-zzz")
    a1.start = datetime(2026, 8, 2, 11, 0, tzinfo=TZ)
    a1.end = a1.start + timedelta(minutes=5)
    a2.start = datetime(2026, 8, 2, 11, 10, tzinfo=TZ)
    a2.end = a2.start + timedelta(minutes=5)
    frag.start = datetime(2026, 8, 2, 11, 16, tzinfo=TZ)
    frag.end = frag.start + timedelta(minutes=1)
    a3.start = datetime(2026, 8, 2, 11, 20, tzinfo=TZ)
    a3.end = a3.start + timedelta(minutes=5)
    # pad with 2 more same content after a continuous run to verify pure collapse still works elsewhere
    blocks = [a1, a2, frag, a3]
    s = _summary(blocks, total=16.0)
    md = render_markdown(s, TZ, min_block_minutes=3.0)
    # 第48弾 §D1 で細切れを表外へ出したため、細切れは圧縮の境界にならない。
    # 第47弾 §G2 の「細切れが挟まれば分割」は要求として失効し、
    # 現仕様では表に載る3件が1行へ融合するのが正しい挙動。
    # （両方を通す `in (1, 3)` は退行を検出できないため値を1つに固定する）
    content_rows = [
        ln for ln in md.splitlines()
        if "same-dup-content-zzz" in ln and ln.startswith("|")
    ]
    assert len(content_rows) == 1, content_rows
    assert "(3回)" in md, "表に載る3件が圧縮されていない"
    # 細切れ自体は表外のサマリ行で説明される
    assert "細切れ" in md


def test_g2_collapse_aaa_still_collapses():
    """間に何も挟まない A,A,A は従来どおり1行に圧縮。"""
    blocks = []
    for i in range(3):
        b = _block(11, 5, "開発", "Code.exe", "pure-aaa-content")
        b.start = datetime(2026, 8, 2, 11, i * 10, tzinfo=TZ)
        b.end = b.start + timedelta(minutes=5)
        blocks.append(b)
    s = _summary(blocks, total=15.0)
    md = render_markdown(s, TZ, min_block_minutes=3.0)
    assert "計15m (3回)" in md
    assert md.count("pure-aaa-content") == 1


def test_g2_overflow_gap_breaks_collapse():
    """省略行が間に入ると、残った同一内容でも融合しない。"""
    # 5 blocks: A(10), B(3), A(10), A(10), A(10) — max_timeline_rows=3 keeps top-3 by minutes = all A
    # time order of shown: A1, A2, A3 but B omitted between A1 and A2
    blocks = []
    times = [0, 10, 20, 30, 40]
    mins_list = [10.0, 3.0, 10.0, 10.0, 10.0]
    titles = ["overflow-same", "other-short", "overflow-same", "overflow-same", "overflow-same"]
    for t, m, title in zip(times, mins_list, titles):
        b = _block(11, m, "開発", "Code.exe", title)
        b.start = datetime(2026, 8, 2, 11, t, tzinfo=TZ)
        b.end = b.start + timedelta(minutes=m)
        blocks.append(b)
    s = _summary(blocks, total=43.0)
    md = render_markdown(s, TZ, min_block_minutes=3.0, max_timeline_rows=3)
    assert "省略" in md
    # B is omitted; A1 |gap| A2,A3 → A1 alone, A2+A3 only 2 so no collapse
    assert "(3回)" not in md
    assert "(4回)" not in md
    same_rows = [
        ln for ln in md.splitlines()
        if "overflow-same" in ln and ln.startswith("|")
    ]
    assert len(same_rows) == 3, same_rows


def test_g3_private_before_truncate():
    """70字目付近に youtube があるタイトルも私的伏せになる。"""
    # 60字切詰め後には youtube が残らない長さ
    prefix = "a" * 65
    title = prefix + " youtube music video long"
    assert len(title) > 70
    assert "youtube" not in title[:60].lower()
    blocks = [_block(17, 5, "ブラウジング", "brave.exe", title)]
    s = _summary(blocks, total=5.0)
    md = render_markdown(s, TZ, min_block_minutes=3.0, hide_private_titles=True)
    assert "（私的）" in md
    assert "youtube" not in md.lower()
    assert "a" * 20 not in md  # 原文のプレフィックスも出ない


def test_g4_system_wrapper_excluded_from_digest():
    """<task-notification> 等は prompts_digest に入らない（往復にも数えない）。"""
    acc = _SessionAccum(session_id="s-sys", project="p")
    acc.start = datetime(2026, 8, 2, 10, tzinfo=UTC)
    acc.end = datetime(2026, 8, 2, 11, tzinfo=UTC)
    acc.touched = True
    acc.note_user_message("<task-notification>background done</task-notification>")
    acc.note_user_message("本物の依頼テキストです。修正してください。")
    acc.note_user_message("<command-message>slash</command-message>")
    session = acc.to_session()
    assert session is not None
    assert session.user_turns == 1
    assert len(session.prompts_digest) == 1
    assert "本物の依頼" in session.prompts_digest[0]
    assert "task-notification" not in " ".join(session.prompts_digest)
    assert "command-message" not in " ".join(session.prompts_digest)


def test_g1_codex_session_io_digests(tmp_path: Path):
    """Codex 由来で prompts_digest / files_touched / commands_run が埋まる。"""
    day_dir = tmp_path / "2026" / "08" / "02"
    day_dir.mkdir(parents=True)
    path = day_dir / "rollout-2026-08-02T10-00-00-io.jsonl"
    records = [
        {
            "type": "session_meta",
            "timestamp": "2026-08-02T01:00:00.000Z",
            "payload": {
                "session_id": "codex-io-1",
                "cwd": "C:/develop/KaizenLog-",
                "id": "codex-io-1",
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-08-02T01:00:05.000Z",
            "payload": {
                "type": "user_message",
                "message": "Please fix report.py collapse boundaries carefully.",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-02T01:00:10.000Z",
            "payload": {
                "type": "function_call",
                "name": "apply_patch",
                "arguments": json.dumps({"path": "C:/develop/KaizenLog-/src/kaizenlog/report.py"}),
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-02T01:00:12.000Z",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": json.dumps({"command": "pytest tests/test_round46_session_io.py -q"}),
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-08-02T01:00:20.000Z",
            "payload": {
                "type": "user_message",
                "message": "続けて browser 側も直して。",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-02T01:00:25.000Z",
            "payload": {
                "type": "function_call",
                "name": "Edit",
                "arguments": {"file_path": r"C:\develop\KaizenLog-\src\kaizenlog\aiwork_browser.py"},
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    day_start = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    day_end = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
    sessions = CodexAdapter(tmp_path).scan_sessions(day_start, day_end)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.source == "codex"
    assert s.user_turns == 2
    assert s.prompts_digest, "prompts_digest が空"
    assert any("report.py" in p or "fix" in p for p in s.prompts_digest)
    assert "report.py" in s.files_touched
    assert "aiwork_browser.py" in s.files_touched
    assert "pytest" in s.commands_run
    # 集計は従来どおり（edits は apply_patch + Edit）
    assert s.edits >= 2


def test_g1_browser_prompts_saved_vs_unsaved(tmp_path: Path):
    """本文保存モードでは prompts_digest が入り、未保存では空。"""
    day = "2026-08-02"
    saved = [
        {
            "ts": "2026-08-02T10:00:00+00:00",
            "site": "chatgpt.com",
            "conversation_id": "saved1",
            "role": "user",
            "char_count": 30,
            "text": "スキーマを設計してマイグレーションまで書いて",
        },
        {
            "ts": "2026-08-02T10:05:00+00:00",
            "site": "chatgpt.com",
            "conversation_id": "saved1",
            "role": "assistant",
            "char_count": 100,
        },
    ]
    unsaved = [
        {
            "ts": "2026-08-02T11:00:00+00:00",
            "site": "claude.ai",
            "conversation_id": "unsaved1",
            "role": "user",
            "char_count": 40,
        },
        {
            "ts": "2026-08-02T11:01:00+00:00",
            "site": "claude.ai",
            "conversation_id": "unsaved1",
            "role": "assistant",
            "char_count": 50,
        },
    ]
    (tmp_path / f"{day}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in saved + unsaved) + "\n",
        encoding="utf-8",
    )
    day_start = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    day_end = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
    sessions = BrowserAIAdapter(tmp_path).scan_sessions(day_start, day_end)
    by_src = {s.source: s for s in sessions}
    assert by_src["chatgpt-web"].prompts_digest
    assert "スキーマ" in by_src["chatgpt-web"].prompts_digest[0]
    assert by_src["chatgpt-web"].files_touched == []
    assert by_src["chatgpt-web"].commands_run == []
    assert by_src["claude-web"].title == "（本文未保存）"
    assert by_src["claude-web"].prompts_digest == []


def test_g1_codex_apply_patch_files_extracted():
    """Codex の apply_patch はパッチ本文からファイル名を拾う（file_path キーが無い）。"""
    from kaizenlog.aiwork import _note_tool_use

    s = AISession(
        session_id="cx",
        project="p",
        start=datetime(2026, 8, 2, 10, tzinfo=TZ),
        end=datetime(2026, 8, 2, 11, tzinfo=TZ),
    )
    patch = (
        "*** Begin Patch\n"
        + r"*** Update File: C:\develop\KaizenLog\PLAN.md" + "\n"
        + "@@\n-old\n+new\n"
        + r"*** Add File: C:\develop\KaizenLog\src\kaizenlog\newmod.py" + "\n"
        + "+x = 1\n"
        + "*** End Patch\n"
    )
    _note_tool_use(s, "apply_patch", patch)
    finalize_session_io_digest(s)
    assert s.files_touched == ["PLAN.md", "newmod.py"]
    assert not any("\\" in f or "/" in f for f in s.files_touched)
    # 編集回数は 1 呼び出し = 1（既存集計を変えない）
    assert s.edits == 1


def test_g1_patch_text_without_marker_is_ignored():
    from kaizenlog.aiwork import _basenames_from_patch_text

    assert _basenames_from_patch_text("just a normal string") == []
    assert _basenames_from_patch_text({"command": "pytest -q"}) == []


def test_g1_codex_tests_run_not_regressed():
    """arguments を dict にパースしても Codex の pytest 検出が落ちない（退行ガード）。"""
    acc = _SessionAccum(session_id="cx2", project="p")
    acc.start = datetime(2026, 8, 2, 10, tzinfo=UTC)
    acc.end = datetime(2026, 8, 2, 11, tzinfo=UTC)

    # 旧: 素の文字列で渡っていた経路
    acc_str = _SessionAccum(session_id="cx3", project="p")
    acc_str.note_tool("shell_command", "pytest -q tests/")
    assert acc_str.tests_run is True

    # 新: JSON パース後の dict で渡る経路（第47弾で追加されたもの）
    acc.note_tool("shell_command", {"command": "pytest -q tests/"})
    assert acc.tests_run is True, "dict 経路で tests_run が検出されない（退行）"

    # テストでないコマンドでは立たない
    acc_other = _SessionAccum(session_id="cx4", project="p")
    acc_other.note_tool("shell_command", {"command": "git status"})
    assert acc_other.tests_run is False


def test_g1_tests_run_not_triggered_by_patch_body():
    """apply_patch のパッチ本文に pytest の語があるだけでは tests_run を立てない。"""
    acc = _SessionAccum(session_id="pt", project="p")
    patch = (
        "*** Begin Patch\n"
        + r"*** Update File: C:\dev\proj\tests\test_pytest_helpers.py" + "\n"
        + "+def test_x():\n+    assert pytest\n"
        + "*** End Patch\n"
    )
    acc.note_tool("apply_patch", patch)
    assert acc.tests_run is False, "編集系ツールで tests_run が誤検知している"
    # ファイル名は拾えている（機能は維持）
    assert acc._files_order == ["test_pytest_helpers.py"]

    # コマンド実行系なら従来どおり立つ
    run = _SessionAccum(session_id="pt2", project="p")
    run.note_tool("exec", {"command": "pytest -q"})
    assert run.tests_run is True
