"""第49弾: 縮退バグ修正・目標達成度・AI入出力可視化。"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.advice_evidence import build_advice_evidence
from kaizenlog.aiwork import (
    AISession,
    finalize_session_io_digest,
    render_aiwork_markdown,
    session_digests_for_stats,
)
from kaizenlog.digest import build_digest
from kaizenlog.goal import (
    DayGoal,
    ensure_goal_placeholder,
    format_goal_section,
    goal_stats_fields,
    parse_goal_text,
    read_goal,
    write_goal,
    write_goal_achieved,
)
from kaizenlog.nippou import generate_nippou_deterministic
from kaizenlog.stats import activity_fingerprint, build_stats, write_stats
from kaizenlog.vault import (
    ACTIVITY_MARKER,
    FOOTNOTES_MARKER,
    GOAL_MARKER,
    consolidate_disclaimers,
    extract_section,
    upsert_section,
)
from kaizenlog.weekly_context import render_weekly_context
from kaizenlog.report import DailySummary

TZ = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 8, 2)
KNOWN = frozenset({"執筆・ノート", "開発", "AI作業", "エンタメ", "ブラウジング"})


def _summary(**kw) -> DailySummary:
    return DailySummary(
        day=DAY,
        total_minutes=kw.get("total", 120.0),
        by_category=kw.get("by_cat", {"開発": 80.0, "AI作業": 40.0}),
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=5,
        by_site={},
    )


# ---------------------------------------------------------------------------
# §A fingerprint / §B footnotes
# ---------------------------------------------------------------------------


def test_a1_activity_fingerprint_matches_post_consolidate():
    """finalize 相当の consolidate 後本文と fingerprint が一致する経路。"""
    body = (
        "## Activity\n"
        "※ first disclaimer about measurement\n"
        "※ second disclaimer about causality\n"
        "data line\n"
    )
    content = upsert_section("", ACTIVITY_MARKER, body, position="bottom")
    finalized = consolidate_disclaimers(content, max_inline=1)
    act = extract_section(finalized, ACTIVITY_MARKER) or ""
    fp = activity_fingerprint(act)
    # stats に post-finalize 本文を保存すれば advise 照合は verified
    stats = build_stats(DAY, _summary(), [], activity_md=act)
    assert stats["activity_sha256"] == fp
    assert stats["activity_sha256"] == activity_fingerprint(act)


def test_a2_b1_consolidate_disclaimers_idempotent():
    body = (
        "## Activity\n"
        "※ first note\n"
        "※ second note about causality\n"
        "※ third note about scope\n"
        "data\n"
    )
    content = upsert_section("", ACTIVITY_MARKER, body, position="bottom")
    once = consolidate_disclaimers(content, max_inline=1)
    twice = consolidate_disclaimers(once, max_inline=1)
    assert once == twice
    fn = extract_section(once, FOOTNOTES_MARKER) or ""
    # ユニーク文面ちょうど2件（second, third）
    defs = re.findall(r"^\[\^\d+\]:", fn, re.MULTILINE)
    assert len(defs) == 2


def test_b1_orphan_footnotes_purged_and_renumbered():
    """汚染ノート（orphan 定義多数）が次の実行で修復される。"""
    act_body = (
        "## Activity\n"
        "※ keep inline\n"
        "ref here [^13] and also [^14]\n"
    )
    content = upsert_section("", ACTIVITY_MARKER, act_body, position="bottom")
    # 汚染: [^1]..[^16] 定義だが本文参照は 13,14 のみ
    fn_lines = ["## 注釈", ""]
    for i in range(1, 17):
        fn_lines.append(f"[^{i}]: note text number {i}")
    content = upsert_section(
        content, FOOTNOTES_MARKER, "\n".join(fn_lines) + "\n", position="bottom"
    )
    out = consolidate_disclaimers(content, max_inline=1)
    fn = extract_section(out, FOOTNOTES_MARKER) or ""
    act = extract_section(out, ACTIVITY_MARKER) or ""
    # orphan は破棄、参照ありのみ 1 から
    assert "[^13]" not in act
    assert "[^1]" in act and "[^2]" in act
    defs = re.findall(r"^\[\^(\d+)\]:\s*(.+)$", fn, re.MULTILINE)
    assert len(defs) == 2
    assert defs[0][0] == "1"
    assert "note text number 13" in defs[0][1]
    assert "note text number 14" in defs[1][1]
    # 同一文面統合
    out2 = consolidate_disclaimers(out, max_inline=1)
    assert out == out2


def test_b1_same_text_shared_across_refs():
    act_body = (
        "## A\n"
        "※ keep\n"
        "※ shared disclaimer text\n"
        "※ shared disclaimer text\n"
    )
    content = upsert_section("", ACTIVITY_MARKER, act_body, position="bottom")
    out = consolidate_disclaimers(content, max_inline=1)
    fn = extract_section(out, FOOTNOTES_MARKER) or ""
    defs = re.findall(r"^\[\^\d+\]:", fn, re.MULTILINE)
    assert len(defs) == 1  # 同一文面は1定義
    act = extract_section(out, ACTIVITY_MARKER) or ""
    assert act.count("[^1]") == 2


def test_a3_resync_stats_fingerprint_after_finalize(tmp_path: Path):
    """cli の resync が finalize 後本文で fingerprint を更新する。"""
    from kaizenlog.cli import _resync_stats_activity_fingerprint
    from kaizenlog.config import Config
    from kaizenlog.vault import DailyNoteStore, consolidate_disclaimers

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    stats_dir = vault / "stats"
    stats_dir.mkdir()
    cfg = Config(vault_dir=vault, daily_notes_dir="notes", stats_dir="stats")
    store = DailyNoteStore(notes)
    day = DAY
    pre = (
        "## Activity\n"
        "※ first\n"
        "※ second causality note\n"
        "row\n"
    )
    # pre-finalize で stats を書く（旧バグ再現）
    write_stats(stats_dir, day, _summary(), [], activity_md=pre)
    content = upsert_section("", ACTIVITY_MARKER, pre, position="bottom")
    finalized = consolidate_disclaimers(content, max_inline=1)
    store.path_for(day).write_text(
        f"---\ndate: {day.isoformat()}\n---\n\n" + finalized,
        encoding="utf-8",
    )
    pre_fp = json.loads((stats_dir / f"{day.isoformat()}.json").read_text(encoding="utf-8"))[
        "activity_sha256"
    ]
    post_act = extract_section(finalized, ACTIVITY_MARKER) or ""
    post_fp = activity_fingerprint(post_act)
    assert pre_fp != post_fp  # finalize で本文が変わる前提
    _resync_stats_activity_fingerprint(cfg, store, day)
    data = json.loads((stats_dir / f"{day.isoformat()}.json").read_text(encoding="utf-8"))
    assert data["activity_sha256"] == post_fp
    # advise 照合相当
    assert data["activity_sha256"] == activity_fingerprint(post_act)


# ---------------------------------------------------------------------------
# §C goal achievement
# ---------------------------------------------------------------------------


def test_c1_goal_achieved_cli_and_stats_fields(tmp_path: Path):
    notes = tmp_path / "notes"
    path, g = write_goal(
        notes,
        DAY,
        "リリースノート @執筆・ノート",
        known_categories=KNOWN,
        achieved=75,
    )
    assert path.is_file()
    assert g.achieved == 75
    body = path.read_text(encoding="utf-8")
    assert "達成度: 75%（自己申告）" in body
    text, cat, ach = goal_stats_fields(g, None)
    assert text == "リリースノート"
    assert cat == "執筆・ノート"
    assert ach == 75
    stats = build_stats(
        DAY, _summary(by_cat={"執筆・ノート": 12.0}), [], goal_text=text, goal_category=cat, goal_achieved=ach
    )
    assert stats["goal_achieved"] == 75


def test_c1_write_goal_achieved_only(tmp_path: Path):
    notes = tmp_path / "notes"
    write_goal(notes, DAY, "仕上げる", known_categories=KNOWN)
    path, g = write_goal_achieved(notes, DAY, 40, known_categories=KNOWN)
    assert g.achieved == 40
    assert "達成度: 40%" in path.read_text(encoding="utf-8")
    # 再読
    g2 = read_goal(path.read_text(encoding="utf-8"), KNOWN)
    assert g2 is not None and g2.achieved == 40 and g2.text == "仕上げる"


def test_c2_digest_goal_achievement_line():
    stats = {
        "version": 2,
        "day": DAY.isoformat(),
        "total_minutes": 200.0,
        "context_switches": 3,
        "activity_sha256": "abc",
        "source_status": "verified",
        "by_category": {"執筆・ノート": 15.0, "AI作業": 30.0},
        "goal_text": "下書き完成",
        "goal_category": "執筆・ノート",
        "goal_achieved": 80,
        "ai": {"sessions": 0, "fragmented": 0, "tool_errors": 0, "interruptions": 0},
        "blocks": [],
    }
    body = build_digest(
        stats,
        [],
        today=DAY,
        redactor=None,
        goal_text="下書き完成",
        goal_achieved=80,
    )
    assert body is not None
    assert "達成度: 80%（自己申告）" in body
    assert "執筆・ノート" in body

    stats2 = dict(stats)
    del stats2["goal_achieved"]
    body2 = build_digest(
        stats2, [], today=DAY, redactor=None, goal_text="下書き完成", goal_achieved=None
    )
    assert body2 is not None
    assert "達成度: 未申告" in body2
    assert "kaizenlog goal --achieved" in body2


def test_c3_nippou_goal_and_category_minutes():
    stats = {
        "day": DAY.isoformat(),
        "total_minutes": 200.0,
        "goal_text": "アプリ完成",
        "goal_category": "開発",
        "goal_achieved": 60,
        "by_category": {"開発": 90.0, "AI作業": 40.0},
        "blocks": [],
        "ai": {"session_digests": []},
    }
    md = generate_nippou_deterministic(stats, TZ)
    assert "達成度: 60%（自己申告）" in md
    assert "目標カテゴリ実測: 開発" in md


def test_c4_weekly_goal_achievement_column(tmp_path: Path):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    mem = tmp_path / "mem"
    mem.mkdir()
    exp = tmp_path / "exp"
    exp.mkdir()
    week = date(2026, 7, 27)
    for i, ach in enumerate((80, None, 40)):
        d = date(2026, 7, 28 + i)
        payload = {
            "version": 2,
            "day": d.isoformat(),
            "total_minutes": 100.0,
            "context_switches": 1,
            "by_category": {"開発": 30.0},
            "goal_text": f"目標{i}",
            "goal_category": "開発",
            "ai": {},
        }
        if ach is not None:
            payload["goal_achieved"] = ach
        (stats_dir / f"{d.isoformat()}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    md = render_weekly_context(stats_dir, mem, exp, week)
    assert "達成度: 80%（自己申告）" in md
    assert "達成度: —" in md
    assert "申告あり日の平均達成度: 60%" in md  # (80+40)/2


def test_c5_prompts_allow_self_reported_achievement():
    root = Path(__file__).resolve().parents[1] / "src" / "kaizenlog" / "prompts"
    adv = (root / "daily_advisor.md").read_text(encoding="utf-8")
    priv = (root / "privacy_safe.md").read_text(encoding="utf-8")
    assert "自己申告の達成度は転記可" in adv
    assert "自己申告の達成度は転記可" in priv
    assert "AI 自身による達成/未達の断定は引き続き禁止" in adv


def test_c6_morning_placeholder(tmp_path: Path):
    notes = tmp_path / "notes"
    path = ensure_goal_placeholder(notes, DAY)
    assert path is not None
    body = path.read_text(encoding="utf-8")
    assert "未設定" in body
    assert "kaizenlog goal" in body
    # 既存があれば触らない
    assert ensure_goal_placeholder(notes, DAY) is None
    # 実 goal で上書き後はプレースホルダではない
    write_goal(notes, DAY, "本番目標", known_categories=KNOWN)
    g = read_goal(path.read_text(encoding="utf-8"), KNOWN)
    assert g is not None and g.text == "本番目標"


# ---------------------------------------------------------------------------
# §D AI I/O
# ---------------------------------------------------------------------------


def test_d1_last_reply_digest_redact_then_truncate():
    s = AISession(
        session_id="s1",
        project="p",
        start=datetime(2026, 8, 2, 10, tzinfo=TZ),
        end=datetime(2026, 8, 2, 11, tzinfo=TZ),
        user_turns=3,
    )
    secret = "SECRET_TOKEN " + ("x" * 200)
    s._last_assistant_raw = secret
    finalize_session_io_digest(
        s, redactor=lambda t: t.replace("SECRET_TOKEN", "[R]")
    )
    assert s.last_reply_digest is not None
    assert "SECRET_TOKEN" not in s.last_reply_digest
    assert s.last_reply_digest.startswith("[R]")
    assert len(s.last_reply_digest) <= 120
    # 切詰め後 redact ではないこと: redact が先なので [R] が先頭
    # （旧: 切詰め後 redact だと境界漏れしうる）


def test_d1_assistant_text_captured_from_update():
    from kaizenlog.aiwork import _update_session

    s = AISession(
        session_id="s1",
        project="p",
        start=datetime(2026, 8, 2, 10, tzinfo=TZ),
        end=datetime(2026, 8, 2, 10, tzinfo=TZ),
    )
    rec1 = {
        "type": "assistant",
        "message": {
            "id": "m1",
            "model": "claude",
            "usage": {"output_tokens": 10},
            "content": [{"type": "text", "text": "first reply"}],
        },
    }
    rec2 = {
        "type": "assistant",
        "message": {
            "id": "m2",
            "model": "claude",
            "usage": {"output_tokens": 20},
            "content": [{"type": "text", "text": "final answer here"}],
        },
    }
    ts = datetime(2026, 8, 2, 10, 1, tzinfo=TZ)
    _update_session(s, rec1, ts)
    _update_session(s, rec2, ts + timedelta(minutes=1))
    assert "final answer here" in s._last_assistant_raw
    finalize_session_io_digest(s)
    assert s.last_reply_digest is not None
    assert "final answer" in s.last_reply_digest


def test_d2_session_io_pairs_max5_and_browser_note():
    sessions = []
    for i in range(6):
        s = AISession(
            session_id=f"s{i}",
            project=f"p{i}",
            start=datetime(2026, 8, 2, 9 + i, tzinfo=TZ),
            end=datetime(2026, 8, 2, 10 + i, tzinfo=TZ),
            user_turns=20 - i,
            edits=1 if i < 4 else 0,
        )
        s._user_prompts_raw = [f"request number {i} please fix"]
        s._last_assistant_raw = f"done with task {i}"
        s._files_order = [f"f{i}.py"]
        sessions.append(s)
    # browser-like
    b = AISession(
        session_id="web1",
        project="ChatGPT",
        start=datetime(2026, 8, 2, 15, tzinfo=TZ),
        end=datetime(2026, 8, 2, 16, tzinfo=TZ),
        user_turns=5,
        edits=0,
        tools_measurable=False,
        source="chatgpt-web",
        assistant_chars=1234,
        title="browser ask",
    )
    b._user_prompts_raw = ["browser prompt text"]
    sessions.append(b)
    md = render_aiwork_markdown(sessions, TZ, session_details=True)
    assert "主なセッションの中身" in md
    detail = md.split("#### 主なセッションの中身", 1)[1]
    assert detail.count("- 依頼:") <= 5
    assert "- 成果:" in detail
    # browser 表示
    md_b = render_aiwork_markdown([b], TZ, session_details=True)
    assert "出力 1234字（本文ログなし）" in md_b


def test_d3_session_digests_include_last_reply():
    s = AISession(
        session_id="s1",
        project="p",
        start=datetime(2026, 8, 2, 10, tzinfo=TZ),
        end=datetime(2026, 8, 2, 11, tzinfo=TZ),
        user_turns=4,
        tool_errors=3,
    )
    s._user_prompts_raw = ["please implement X"]
    s._last_assistant_raw = "implemented X with tests"
    digests = session_digests_for_stats([s], DAY.isoformat())
    assert digests[0]["last_reply_digest"]
    assert "implemented X" in digests[0]["last_reply_digest"]


def test_d4_advice_evidence_friction_io_lines():
    stats = {
        "version": 2,
        "day": DAY.isoformat(),
        "total_minutes": 200.0,
        "context_switches": 5,
        "by_category": {"AI作業": 80.0, "開発": 100.0},
        "by_app": {},
        "by_site": {},
        "blocks": [],
        "ai": {
            "sessions": 2,
            "fragmented": 0,
            "tool_errors": 5,
            "interruptions": 1,
            "session_digests": [
                {
                    "project": "KaizenLog",
                    "tool_errors": 5,
                    "interruptions": 1,
                    "retry_touch": 0,
                    "prompts_digest": ["fix the fingerprint mismatch bug"],
                    "last_reply_digest": "moved write_stats after finalize",
                    "title": "fix bug",
                }
            ],
        },
    }
    ev = build_advice_evidence(stats)
    assert "[F20]" in ev.markdown
    assert "依頼「fix the fingerprint" in ev.markdown
    assert "成果「moved write_stats" in ev.markdown


# ---------------------------------------------------------------------------
# §E nippou auto_write
# ---------------------------------------------------------------------------


def test_e1_auto_write_nippou(tmp_path: Path):
    from kaizenlog.cli import _auto_write_nippou
    from kaizenlog.config import Config, NippouConfig
    from kaizenlog.vault import NIPPOU_MARKER, DailyNoteStore

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    stats_dir = vault / ".kaizenlog" / "stats"
    stats_dir.mkdir(parents=True)
    mem = vault / "mem"
    mem.mkdir()
    cfg = Config(
        vault_dir=vault,
        daily_notes_dir="notes",
        stats_dir=".kaizenlog/stats",
        memory_dir="mem",
        nippou=NippouConfig(auto_write=True),
    )
    day = DAY
    write_stats(
        stats_dir,
        day,
        _summary(),
        [],
        goal_text="完了させる",
        goal_achieved=50,
    )
    store = DailyNoteStore(notes)
    store.path_for(day).write_text(
        f"---\ndate: {day.isoformat()}\n---\n\n# day\n",
        encoding="utf-8",
    )
    assert _auto_write_nippou(cfg, day) is True
    content = store.read(day) or ""
    assert extract_section(content, NIPPOU_MARKER) is not None
    assert "目標: 完了させる" in (extract_section(content, NIPPOU_MARKER) or "")


def test_e1_nippou_config_default_on():
    from kaizenlog.config import Config, NippouConfig

    assert Config().nippou.auto_write is True
    assert NippouConfig(auto_write=False).auto_write is False


def test_c1_parse_goal_with_achieved_line():
    body = format_goal_section("テストを通す", "開発", achieved=90)
    g = parse_goal_text(body, KNOWN)
    assert g is not None
    assert g.achieved == 90
    assert g.category == "開発"
    assert g.text == "テストを通す"
