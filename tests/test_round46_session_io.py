"""第46弾: 私的タイトル伏せ・重複圧縮・セッション入出力・日報成果ベース。"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from kaizenlog.aiwork import (
    AISession,
    finalize_session_io_digest,
    render_aiwork_markdown,
    session_digests_for_stats,
)
from kaizenlog.nippou import generate_nippou_deterministic, _project_work_lines
from kaizenlog.privacy_filter import is_private_block
from kaizenlog.report import Block, DailySummary, render_markdown, _fmt_minutes

TZ = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 8, 2)


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
    assert "（私的・非表示）" in md
    assert "自作クーラー" not in md
    assert "エンタメ" in md
    # カテゴリ表の分数は不変
    assert "| エンタメ |" in md
    assert _fmt_minutes(5.0) in md or "5m" in md


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
    assert "4回" in md or "(4回)" in md
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
    sessions = []
    for i, turns in enumerate((15, 10, 8, 2)):
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
    assert "nippou.py" in md or "編集" in md
    assert "コミット11件" in md or "コミット 11" in md or "11件" in md
    assert "評価して改善案" not in md  # no prompt fragment
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
