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
    # T3: SECRET を 120 字境界を跨ぐ位置に置く（先頭配置だと truncate-first 変異が生存する）
    secret = ("x" * 110) + "SECRET_TOKEN" + ("y" * 50)
    s._last_assistant_raw = secret
    finalize_session_io_digest(
        s, redactor=lambda t: t.replace("SECRET_TOKEN", "[R]")
    )
    assert s.last_reply_digest is not None
    assert "SECRET_TOKEN" not in s.last_reply_digest
    assert "SECRET_T" not in s.last_reply_digest  # 境界部分漏れも禁止
    assert len(s.last_reply_digest) <= 120
    # redact が先: 置換後に切詰め。truncate-first なら SECRET_T が残る


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
    # T4: 7セッション入力で 依頼 行がちょうど 5
    sessions = []
    for i in range(7):
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
    assert detail.count("- 依頼:") == 5
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
        "goal_text": "仕上げる",
        "goal_achieved": 70,
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
    # T4 / R3: F20・F22 が citable fact_ids に入る（F14b は正規表現外で失格。
    # F21 は並行作業のパーセンタイル指示行が使用中のため達成度は F22）
    assert "[F20]" in ev.fact_ids
    assert "[F22]" in ev.fact_ids
    assert "[F14b]" not in ev.markdown


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


# ---------------------------------------------------------------------------
# 第50弾: レビュー残件 R1–R6 / T1–T5
# ---------------------------------------------------------------------------


def test_r1_patch_stats_preserves_redacted_goal_text(tmp_path: Path):
    """R1: goal --achieved が redact 済み goal_text を生に戻さない。"""
    from kaizenlog.cli import _patch_stats_goal_achieved
    from kaizenlog.config import Config
    from kaizenlog.goal import DayGoal

    vault = tmp_path / "vault"
    stats_dir = vault / "stats"
    stats_dir.mkdir(parents=True)
    day = DAY
    (stats_dir / f"{day.isoformat()}.json").write_text(
        json.dumps(
            {
                "day": day.isoformat(),
                "goal_text": "[REDACTED] の提案書",
                "goal_category": "執筆・ノート",
                "total_minutes": 10.0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = Config(vault_dir=vault, stats_dir="stats")
    _patch_stats_goal_achieved(
        cfg,
        day,
        DayGoal(text="ACME-SECRET の提案書", category="執筆・ノート", achieved=55),
    )
    data = json.loads((stats_dir / f"{day.isoformat()}.json").read_text(encoding="utf-8"))
    assert data["goal_text"] == "[REDACTED] の提案書"
    assert data["goal_achieved"] == 55
    assert "ACME-SECRET" not in data["goal_text"]
    # [F14] にも生値が流れない
    stats = {
        "version": 2,
        "day": day.isoformat(),
        "total_minutes": 100.0,
        "context_switches": 1,
        "by_category": {"執筆・ノート": 10.0},
        "by_app": {},
        "by_site": {},
        "blocks": [],
        "ai": {"sessions": 0, "fragmented": 0, "tool_errors": 0, "interruptions": 0},
        "goal_text": data["goal_text"],
        "goal_achieved": data["goal_achieved"],
    }
    ev = build_advice_evidence(stats)
    assert "ACME-SECRET" not in ev.markdown
    assert "[REDACTED]" in ev.markdown
    assert "[F22]" in ev.fact_ids


def test_r2_prompt_digest_redact_before_truncate_80():
    """R2/T3: 依頼 digests は redact→80字。境界跨ぎトークンを漏らさない。"""
    s = AISession(
        session_id="s1",
        project="p",
        start=datetime(2026, 8, 2, 10, tzinfo=TZ),
        end=datetime(2026, 8, 2, 11, tzinfo=TZ),
        user_turns=2,
    )
    # SECRET_TOKEN が 80 字境界を跨ぐ
    raw = ("a" * 70) + "SECRET_TOKEN" + ("b" * 20)
    s._user_prompts_raw = [raw]
    finalize_session_io_digest(
        s, redactor=lambda t: t.replace("SECRET_TOKEN", "[R]")
    )
    assert s.prompts_digest
    p0 = s.prompts_digest[0]
    # redact 後 93 字の入力なので、80字キャップならちょうど 80（60字への退行を検知）
    assert len(p0) == 80
    assert "SECRET_TOKEN" not in p0
    assert "SECRET_T" not in p0
    digests = session_digests_for_stats(
        [s], DAY.isoformat(), redactor=lambda t: t.replace("SECRET_TOKEN", "[R]")
    )
    assert "SECRET_T" not in json.dumps(digests, ensure_ascii=False)
    md = render_aiwork_markdown(
        [s],
        TZ,
        session_details=True,
        redactor=lambda t: t.replace("SECRET_TOKEN", "[R]"),
    )
    assert "SECRET_T" not in md
    assert "SECRET_TOKEN" not in md


def test_r4_digest_note_achieved_beats_stale_stats(tmp_path: Path):
    """R4: goal A→achieved 80→goal B の後、digest は B に旧達成度を付けない。"""
    from kaizenlog.cli import _write_digest_for_day
    from kaizenlog.config import Config
    from kaizenlog.vault import DailyNoteStore, DIGEST_MARKER

    vault = tmp_path / "v"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    stats_dir = vault / "stats"
    stats_dir.mkdir()
    logs = vault / "logs"
    logs.mkdir()
    mem = vault / "mem"
    mem.mkdir()
    cfg = Config(
        vault_dir=vault,
        daily_notes_dir="notes",
        stats_dir="stats",
        logs_dir="logs",
        memory_dir="mem",
    )
    day = DAY
    # ノートは新目標 B・達成度なし。stats は旧 80%
    write_goal(notes, day, "目標B", known_categories=KNOWN)
    write_stats(
        stats_dir,
        day,
        _summary(),
        [],
        activity_md="### activity\n",
        goal_text="目標A",
        goal_achieved=80,
    )
    store = DailyNoteStore(notes)
    dig_stats = json.loads(
        (stats_dir / f"{day.isoformat()}.json").read_text(encoding="utf-8")
    )
    dig_stats["source_status"] = "verified"
    dig_stats["activity_sha256"] = dig_stats.get("activity_sha256") or "x"
    ok = _write_digest_for_day(
        cfg,
        store,
        day,
        source_status="verified",
        current_stats=dig_stats,
        entries=[],
        redactor=None,
        log_skips=False,
    )
    assert ok
    body = extract_section(store.read(day) or "", DIGEST_MARKER) or ""
    assert "目標B" in body or "目標: 目標B" in body
    assert "80%" not in body
    assert "未申告" in body


def test_r4_digest_stats_fallback_without_goal_section(tmp_path: Path):
    """R4: ノートに GOAL 区間が無い日は stats の目標/達成度に fallback する。"""
    from kaizenlog.cli import _write_digest_for_day
    from kaizenlog.config import Config
    from kaizenlog.vault import DailyNoteStore, DIGEST_MARKER

    vault = tmp_path / "v"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    stats_dir = vault / "stats"
    stats_dir.mkdir()
    (vault / "logs").mkdir()
    (vault / "mem").mkdir()
    cfg = Config(
        vault_dir=vault,
        daily_notes_dir="notes",
        stats_dir="stats",
        logs_dir="logs",
        memory_dir="mem",
    )
    day = DAY
    # GOAL 区間なし。stats のみに目標A・達成度80
    write_stats(
        stats_dir,
        day,
        _summary(),
        [],
        activity_md="### activity\n",
        goal_text="目標A",
        goal_achieved=80,
    )
    store = DailyNoteStore(notes)
    dig_stats = json.loads(
        (stats_dir / f"{day.isoformat()}.json").read_text(encoding="utf-8")
    )
    dig_stats["source_status"] = "verified"
    dig_stats["activity_sha256"] = dig_stats.get("activity_sha256") or "x"
    ok = _write_digest_for_day(
        cfg,
        store,
        day,
        source_status="verified",
        current_stats=dig_stats,
        entries=[],
        redactor=None,
        log_skips=False,
    )
    assert ok
    body = extract_section(store.read(day) or "", DIGEST_MARKER) or ""
    assert "目標A" in body
    assert "80%（自己申告）" in body


def test_r5_codex_agent_message_fills_last_reply(tmp_path: Path):
    """R5: event_msg/agent_message でも last_reply_digest が埋まる。"""
    from kaizenlog.aiwork_codex import CodexAdapter

    day = "2026-08-02"
    sess_dir = tmp_path / "sessions" / "2026" / "08" / "02"
    sess_dir.mkdir(parents=True)
    lines = [
        {
            "timestamp": "2026-08-02T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "sess-r5", "cwd": str(tmp_path / "proj")},
        },
        {
            "timestamp": "2026-08-02T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "please fix the bug now"},
        },
        {
            "timestamp": "2026-08-02T10:00:05Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "I fixed the bug by rewriting the fingerprint sync",
            },
        },
    ]
    path = sess_dir / "rollout-r5.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in lines) + "\n",
        encoding="utf-8",
    )
    start = datetime(2026, 8, 2, 0, 0, tzinfo=ZoneInfo("UTC"))
    end = datetime(2026, 8, 3, 0, 0, tzinfo=ZoneInfo("UTC"))
    sessions = CodexAdapter(tmp_path / "sessions").scan_sessions(start, end)
    assert sessions, "session should be found"
    s = sessions[0]
    assert s.last_reply_digest
    assert "fingerprint" in (s.last_reply_digest or "")
    assert "I fixed" in (s._last_assistant_raw or "")


def test_r6_shared_goal_category_minutes():
    from kaizenlog.stats import goal_category_minutes
    from kaizenlog.advice_evidence import _goal_category_minutes

    stats = {"by_category": {"開発": 42.0, "AI作業": 10.0}}
    assert goal_category_minutes(stats, "開発") == 42.0
    assert _goal_category_minutes(stats, "開発") == 42.0
    assert goal_category_minutes(stats, None) is None
    assert goal_category_minutes(stats, "存在しない") is None


def test_t5_empty_disclaimer_idempotent():
    """T5: 素の「※」だけの行を含む本文でも consolidate が冪等。"""
    body = "## Activity\n※ first real note\n※\n※ second real note\n"
    content = upsert_section("", ACTIVITY_MARKER, body, position="bottom")
    once = consolidate_disclaimers(content, max_inline=1)
    twice = consolidate_disclaimers(once, max_inline=1)
    assert once == twice
    # 脚注は空定義を持たない
    fn = extract_section(once, FOOTNOTES_MARKER) or ""
    for line in fn.splitlines():
        if line.startswith("[^") and "]:" in line:
            assert line.split("]:", 1)[1].strip(), "empty footnote def"


def test_t1_cmd_generate_resync_and_goal_achieved(tmp_path: Path, monkeypatch):
    """T1: cmd_generate 統合 — resync と goal_achieved 保存が生きている。"""
    from unittest.mock import MagicMock, patch

    from kaizenlog.cli import cmd_generate
    from kaizenlog.config import Config, AIWorkConfig
    from kaizenlog.report import DailySummary
    from kaizenlog.vault import DailyNoteStore, ACTIVITY_MARKER as AM

    vault = tmp_path / "vault"
    for d in ("notes", "stats", "mem", "logs", "exp"):
        (vault / d).mkdir(parents=True)
    cfg = Config(
        vault_dir=vault,
        daily_notes_dir="notes",
        stats_dir="stats",
        memory_dir="mem",
        logs_dir="logs",
        experiments_dir="exp",
        aiwork=AIWorkConfig(enabled=False),
        auto_backfill_days=0,
    )
    day = DAY
    write_goal(
        vault / "notes",
        day,
        "今日の目標テキスト @開発",
        known_categories=KNOWN,
        achieved=66,
    )
    # consolidate で変わる activity（※ が2本）
    activity_md = (
        "## Activity Log\n"
        "※ first disclaimer about measurement limits\n"
        "※ second disclaimer about causality\n"
        "| 10:00 | 開発 | Code | main.py | 30m |\n"
    )
    summary = DailySummary(
        day=day,
        total_minutes=30.0,
        by_category={"開発": 30.0},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
        by_site={},
    )
    with (
        patch("kaizenlog.cli.collect_day", return_value=([], True)),
        patch("kaizenlog.cli.collect_input", return_value=None),
        patch("kaizenlog.cli.summarize", return_value=summary),
        patch("kaizenlog.cli.render_markdown", return_value=activity_md),
        patch("kaizenlog.cli.available_adapters", return_value=[]),
        patch("kaizenlog.cli.ActivityWatchClient"),
        patch("kaizenlog.cli.Classifier") as Cls,
        patch("kaizenlog.decay.run_decay_detection", return_value=[]),
    ):
        Cls.return_value.classify_all.return_value = []
        cmd_generate(cfg, day)

    store = DailyNoteStore(vault / "notes")
    note = store.read(day) or ""
    act = extract_section(note, AM) or ""
    stats_path = vault / "stats" / f"{day.isoformat()}.json"
    data = json.loads(stats_path.read_text(encoding="utf-8"))
    # (a) resync が生きている: finalize 後本文と一致
    assert data["activity_sha256"] == activity_fingerprint(act)
    # (b) goal_achieved が stats に保存される
    assert data.get("goal_achieved") == 66


def test_t2_main_run_auto_nippou_gate(tmp_path: Path, monkeypatch):
    """T2: main(run) で auto_write が効く / false と advise 単独では書かない。"""
    from unittest.mock import patch

    from kaizenlog.cli import main
    from kaizenlog.vault import NIPPOU_MARKER, DailyNoteStore

    def _setup(auto_write: bool) -> tuple[Path, Path, date]:
        vault = tmp_path / f"v_{auto_write}"
        for d in ("notes", ".kaizenlog/stats", "mem", "logs", "exp"):
            (vault / d).mkdir(parents=True, exist_ok=True)
        day = DAY
        write_stats(
            vault / ".kaizenlog" / "stats",
            day,
            _summary(),
            [],
            goal_text="x",
        )
        store = DailyNoteStore(vault / "notes")
        store.path_for(day).write_text(
            f"---\ndate: {day.isoformat()}\n---\n\n# d\n",
            encoding="utf-8",
        )
        cfg_path = vault / "config.toml"
        cfg_path.write_text(
            "\n".join(
                [
                    "[general]",
                    f'vault_dir = "{vault.as_posix()}"',
                    'daily_notes_dir = "notes"',
                    'stats_dir = ".kaizenlog/stats"',
                    'logs_dir = "logs"',
                    'memory_dir = "mem"',
                    'experiments_dir = "exp"',
                    "auto_backfill_days = 0",
                    "",
                    "[aiwork]",
                    "enabled = false",
                    "",
                    "[llm]",
                    'backend = "none"',
                    "",
                    "[nippou]",
                    f"auto_write = {'true' if auto_write else 'false'}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return vault, cfg_path, day

    vault, cfg_path, day = _setup(True)
    with (
        patch("kaizenlog.cli.cmd_generate", return_value=None),
        patch("kaizenlog.cli.cmd_advise", return_value=None),
        patch("kaizenlog.cli.missing_days", return_value=[]),
        patch("kaizenlog.cli.catch_up_yesterday") as cu,
    ):
        from kaizenlog.cli import CatchUpResult

        cu.return_value = CatchUpResult(generate="not-needed", advise="skipped")
        rc = main(
            [
                "--config",
                str(cfg_path),
                "run",
                "--date",
                day.isoformat(),
            ]
        )
    assert rc == 0
    store = DailyNoteStore(vault / "notes")
    assert extract_section(store.read(day) or "", NIPPOU_MARKER) is not None

    # auto_write=false → 書かない
    vault2, cfg2, day2 = _setup(False)
    with (
        patch("kaizenlog.cli.cmd_generate", return_value=None),
        patch("kaizenlog.cli.cmd_advise", return_value=None),
        patch("kaizenlog.cli.missing_days", return_value=[]),
        patch("kaizenlog.cli.catch_up_yesterday") as cu2,
    ):
        from kaizenlog.cli import CatchUpResult

        cu2.return_value = CatchUpResult(generate="not-needed", advise="skipped")
        rc2 = main(["--config", str(cfg2), "run", "--date", day2.isoformat()])
    assert rc2 == 0
    store2 = DailyNoteStore(vault2 / "notes")
    assert extract_section(store2.read(day2) or "", NIPPOU_MARKER) is None

    # advise 単独では書かない
    vault3, cfg3, day3 = _setup(True)
    with patch("kaizenlog.cli.cmd_advise", return_value=None):
        rc3 = main(["--config", str(cfg3), "advise", "--date", day3.isoformat()])
    assert rc3 == 0
    store3 = DailyNoteStore(vault3 / "notes")
    assert extract_section(store3.read(day3) or "", NIPPOU_MARKER) is None

