"""第51弾: 日誌の行動起点化（resume / 委譲 / 実験カルテ / reflect）。"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from kaizenlog.config import Config, DelegationConfig
from kaizenlog.digest import build_delegation_subsection, build_digest
from kaizenlog.experiments import (
    AbtestExperiment,
    Experiment,
    build_experiments_section,
    experiment_day_progress,
)
from kaizenlog.memory import MemoryEntry
from kaizenlog.reflect import (
    build_reflect_section,
    collect_reflect_questions,
    has_reflect_answers,
    read_reflect_answers,
)
from kaizenlog.resume import build_resume_section
from kaizenlog.vault import (
    ACTIVITY_MARKER,
    DIGEST_MARKER,
    EFFORT_MARKER,
    EXPERIMENTS_MARKER,
    FOOTNOTES_MARKER,
    GOAL_MARKER,
    REFLECT_MARKER,
    RESUME_MARKER,
    SECTION_ORDER,
    consolidate_disclaimers,
    extract_section,
    reorder_sections,
    upsert_section,
)

DAY = date(2026, 8, 2)
NEXT = date(2026, 8, 3)


def _base_stats(**kw) -> dict:
    stats = {
        "day": DAY.isoformat(),
        "source_status": "verified",
        "activity_sha256": "x",
        "total_minutes": 200.0,
        "by_category": {"開発": 100.0, "AI作業": 60.0},
        "by_app": {"Code.exe": 125.0, "chrome.exe": 40.0},
        "by_site": {},
        "blocks": [
            {
                "start": "2026-08-02T10:00:00+09:00",
                "end": "2026-08-02T11:00:00+09:00",
                "category": "開発",
                "app": "Code.exe",
                "minutes": 60.0,
                "title": "main.py",
            },
            {
                "start": "2026-08-02T21:00:00+09:00",
                "end": "2026-08-02T21:58:00+09:00",
                "category": "AI作業",
                "app": "Code.exe",
                "minutes": 58.0,
                "title": "session",
            },
        ],
        "ai": {
            "session_digests": [
                {
                    "project": "KaizenLog-",
                    "user_turns": 20,
                    "edits": 30,
                    "tests_run": True,
                    "ended_in_error": True,
                    "files_touched": ["cli.py", "digest.py", "resume.py"],
                    "commands_run": ["pytest", "git", "npm"],
                    "last_reply_digest": "テスト失敗を修正中",
                    "prompts_digest": ["テストが通らないので直して"],
                    "retry_touch": 1,
                    "start": "2026-08-02T21:00:00+09:00",
                    "end": "2026-08-02T21:58:00+09:00",
                },
                {
                    "project": "OtherProj",
                    "user_turns": 10,
                    "edits": 11,
                    "tests_run": False,
                    "ended_in_error": False,
                    "files_touched": ["a.py"],
                    "commands_run": ["python"],
                    "last_reply_digest": None,
                    "prompts_digest": ["hello"],
                    "retry_touch": 0,
                    "start": "2026-08-02T15:00:00+09:00",
                    "end": "2026-08-02T16:00:00+09:00",
                },
            ]
        },
        "outcome_git": [
            {
                "repo_label": "KaizenLog-",
                "commits": 6,
                "insertions": 100,
                "deletions": 20,
                "subjects": ["feat: resume pack"],
                "dirty": True,
            }
        ],
        "input": {
            "keypresses": 1200,
            "clicks": 80,
            "active_input_minutes": 90.0,
            "focus_blocks": 2,
            "focus_minutes": 40.0,
        },
    }
    stats.update(kw)
    return stats


# ---------------------------------------------------------------------------
# §A Resume
# ---------------------------------------------------------------------------


def test_a1_resume_from_prev_stats():
    body = build_resume_section(_base_stats())
    assert body is not None
    assert "きのうの続きから" in body
    assert "21:00–21:58 / Code.exe" in body
    assert "KaizenLog-" in body
    assert "feat: resume pack" in body
    assert "未コミット変更あり" in body
    assert "再開1手:" in body


def test_a2_commands_head_only_and_omit_missing_reply():
    body = build_resume_section(_base_stats())
    assert body is not None
    assert "よく使ったコマンド: pytest, git, npm" in body
    # OtherProj は last_reply_digest=None → 行省略
    other_block = body.split("**OtherProj**", 1)[1].split("###", 1)[0]
    assert "AI最後の返答" not in other_block
    assert "python" in other_block


def test_a3_resume_one_checked_preserved():
    first = build_resume_section(_base_stats())
    assert first is not None
    checked = first.replace("- [ ] 再開1手:", "- [x] 再開1手:")
    second = build_resume_section(_base_stats(), existing_resume=checked)
    assert second is not None
    assert "- [x] 再開1手:" in second
    assert "- [ ] 再開1手:" not in second


def test_a4_no_prev_stats_no_section():
    assert build_resume_section(None) is None
    assert build_resume_section({}) is None or "最終作業" not in (
        build_resume_section({}) or ""
    )
    # 空 stats（blocks/ai/git なし）
    empty = {"day": DAY.isoformat()}
    assert build_resume_section(empty) is None


def test_a5_dirty_none_old_stats_ok():
    stats = _base_stats()
    stats["outcome_git"] = [
        {
            "repo_label": "KaizenLog-",
            "commits": 1,
            "insertions": 1,
            "deletions": 0,
            "subjects": ["old subject"],
            # dirty キー無し（旧 stats）
        }
    ]
    body = build_resume_section(stats)
    assert body is not None
    assert "old subject" in body
    assert "未コミット" not in body


def test_a6_outside_bytes_preserved(tmp_path: Path):
    from kaizenlog.vault import DailyNoteStore

    store = DailyNoteStore(tmp_path)
    handwriting = "手書きメモ keep-me\n"
    other = upsert_section("", GOAL_MARKER, "## 🎯 目標\n- 手書き目標\n")
    content = "---\ndate: 2026-08-03\n---\n\n" + handwriting + other
    path = store.path_for(NEXT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    def _outside(text: str) -> str:
        start = f"<!-- {RESUME_MARKER}:start -->"
        end = f"<!-- {RESUME_MARKER}:end -->"
        si, ei = text.find(start), text.find(end)
        if si >= 0 and ei > si:
            return text[:si] + text[ei + len(end) :]
        return text

    # 初回挿入
    body1 = build_resume_section(_base_stats())
    assert body1 is not None
    store.write_section(NEXT, RESUME_MARKER, body1, position="top")
    before = path.read_text(encoding="utf-8")
    assert "手書きメモ keep-me" in before

    # 再書き込みで区間外バイト厳密比較
    body2 = build_resume_section(
        _base_stats(),
        existing_resume=extract_section(before, RESUME_MARKER),
    )
    assert body2 is not None
    store.write_section(NEXT, RESUME_MARKER, body2, position="top")
    after = path.read_text(encoding="utf-8")
    assert _outside(before) == _outside(after)
    assert "手書きメモ keep-me" in after
    goal = extract_section(after, GOAL_MARKER)
    assert goal is not None
    assert "手書き目標" in goal


# ---------------------------------------------------------------------------
# §B Delegation
# ---------------------------------------------------------------------------


def test_b1_delegation_appended_digest_core_unchanged():
    stats = _base_stats()
    core = build_digest(
        stats, [], today=DAY, redactor=None, editor_apps=None
    )
    assert core is not None
    assert "🤝 委譲の形" in core
    # 見出しに窓表記を埋め込まない（中央値行だけが N日 を持つ）
    assert "### 🤝 委譲の形（" not in core
    assert "### 🤝 委譲の形\n" in core or "### 🤝 委譲の形\r\n" in core
    # 30秒サマリ既存行
    assert "稼働" in core
    assert "ムダ上位" in core
    # 委譲は末尾小節
    assert core.index("30秒サマリ") < core.index("委譲の形")
    # 脚注文
    assert "手作業の直接計測ではありません" in core
    # §S1: 連結後文字列で委譲見出し直前に空行1行
    assert "\n\n### 🤝 委譲の形" in core


def test_b2_input_missing_shows_欠測():
    stats = _base_stats()
    del stats["input"]
    sub = build_delegation_subsection(stats, [], today=DAY)
    assert sub is not None
    assert "入力統計: 欠測" in sub


def test_b3_history_under_3_no_median():
    stats = _base_stats()
    hist = [
        {
            "day": "2026-07-31",
            "by_app": {"Code.exe": 100.0},
            "ai": {"session_digests": []},
        },
        {
            "day": "2026-08-01",
            "by_app": {"Code.exe": 110.0},
            "ai": {"session_digests": []},
        },
    ]
    sub = build_delegation_subsection(stats, hist, today=DAY)
    assert sub is not None
    assert "エディタ前景時間:" in sub
    # 最小3日未満は中央値非表示（「N日中央値」形式が一切出ない）
    assert "日中央値" not in sub


def test_b4_zero_denominator_no_ratio():
    stats = _base_stats()
    stats["outcome_git"] = [
        {
            "repo_label": "X",
            "commits": 0,
            "insertions": 0,
            "deletions": 0,
            "subjects": [],
        }
    ]
    # turns を 0 に
    for d in stats["ai"]["session_digests"]:
        d["user_turns"] = 0
    sub = build_delegation_subsection(stats, [], today=DAY)
    assert sub is not None
    assert "日次総計比" not in sub
    assert "往復あたり" not in sub
    assert "AI編集イベント: 41件 / コミット 0件" in sub


# ---------------------------------------------------------------------------
# §C Experiments
# ---------------------------------------------------------------------------


def test_c1_day_progress_boundaries():
    start = date(2026, 8, 1)
    deadline = date(2026, 8, 5)  # N=5
    assert experiment_day_progress(start, deadline, date(2026, 8, 1)) == (1, 5)
    assert experiment_day_progress(start, deadline, date(2026, 8, 5)) == (5, 5)
    assert experiment_day_progress(start, deadline, date(2026, 8, 10)) == (5, 5)
    assert experiment_day_progress(None, deadline, DAY) is None
    assert experiment_day_progress(start, None, DAY) is None


def test_c2_zero_running_no_section_readonly(tmp_path: Path):
    exp_path = tmp_path / "exp.md"
    exp_path.write_text(
        "---\ntitle: done\nstatus: adopted\nmetric: x\ntarget: <= 1\n"
        "date: 2026-07-01\ndeadline: 2026-07-10\n---\nbody\n",
        encoding="utf-8",
    )
    original = exp_path.read_text(encoding="utf-8")
    exps = [
        Experiment(
            path=exp_path,
            title="done",
            status="adopted",
            metric="x",
            target_op="<=",
            target_value=1.0,
            start=date(2026, 7, 1),
            deadline=date(2026, 7, 10),
        )
    ]
    assert build_experiments_section(exps, [], today=DAY) is None
    assert exp_path.read_text(encoding="utf-8") == original


def test_c2b_running_renders():
    exp = Experiment(
        path=Path("x.md"),
        title="スイッチ削減",
        status="running",
        metric="switch_per_hour",
        target_op="<=",
        target_value=5.0,
        baseline=5.6,
        start=date(2026, 7, 31),
        deadline=date(2026, 8, 4),
        measurements={DAY: 4.2},
    )
    ab = AbtestExperiment(
        path=Path("ab.md"),
        id="EXP-03",
        status="running",
        start=date(2026, 7, 23),
        deadline=date(2026, 8, 19),
        predict_pct=10.0,
        sample_ai_days=8,
        sample_non_ai_days=4,
    )
    body = build_experiments_section([exp], [ab], today=DAY)
    assert body is not None
    assert "進行中の実験（2件）" in body
    assert "3/5日目" in body
    assert "今日の値: 4.2" in body
    assert "switch_per_hour <= 5" in body
    assert "abtest #EXP-03" in body
    # start=7/23, deadline=8/19 → N=28; today=8/2 → n=11
    assert "11/28日目" in body


# ---------------------------------------------------------------------------
# §D Reflect
# ---------------------------------------------------------------------------


def test_d1_rules_priority_max3():
    stats = _base_stats()
    stats["by_site"] = {"docs.screenpi.pe": 44.0}
    # 履歴に docs が無い + tests 連続3日
    hist = []
    for i in range(1, 4):
        d = DAY - timedelta(days=i)
        hist.append(
            {
                "day": d.isoformat(),
                "by_site": {"github.com": 10.0},
                "ai": {
                    "session_digests": [
                        {
                            "tests_run": True,
                            "ended_in_error": False,
                            "prompts_digest": [],
                            "end": f"{d.isoformat()}T12:00:00+09:00",
                        }
                    ]
                },
            }
        )
    qs = collect_reflect_questions(stats, hist, today=DAY)
    assert len(qs) == 3
    assert "末尾エラー" in qs[0]
    assert "docs.screenpi.pe" in qs[1]
    assert "テスト実行を伴うセッション" in qs[2]
    body = build_reflect_section(stats, hist, today=DAY)
    assert body is not None
    assert body.count("- Q") == 3
    assert "どう決着しましたか" in body


def test_d2_answers_preserved_byte_equal(tmp_path: Path):
    """回答入りノートに対し cli の reflect 書き込み経路を呼び保持を検証。"""
    from kaizenlog.cli import _write_reflect_section
    from kaizenlog.config import Config
    from kaizenlog.vault import DailyNoteStore

    stats = _base_stats()
    body = build_reflect_section(stats, [], today=DAY)
    assert body is not None
    answered = body.replace("- A:", "- A: 手で直した", 1)
    assert has_reflect_answers(answered)

    store = DailyNoteStore(tmp_path / "01 Daily Notes")
    # 手書き + GOAL を先に置き、reflect 再書き込みで区間外が不変であることを検証
    seed = (
        "---\ndate: 2026-08-02\n---\n\n"
        "手書きメモ reflect-keep\n"
    )
    seed = upsert_section(seed, GOAL_MARKER, "## 🎯 目標\n- 手書き目標\n")
    store.path_for(DAY).parent.mkdir(parents=True, exist_ok=True)
    store.path_for(DAY).write_text(seed, encoding="utf-8")
    store.write_section(DAY, REFLECT_MARKER, answered)
    before = store.read(DAY) or ""
    before_sec = extract_section(before, REFLECT_MARKER)
    assert before_sec is not None

    def _outside(text: str) -> str:
        start = f"<!-- {REFLECT_MARKER}:start -->"
        end = f"<!-- {REFLECT_MARKER}:end -->"
        si, ei = text.find(start), text.find(end)
        if si >= 0 and ei > si:
            return text[:si] + text[ei + len(end) :]
        return text

    cfg = Config(vault_dir=tmp_path, timezone="Asia/Tokyo")
    # 実経路: 回答ありなら再生成しない
    _write_reflect_section(cfg, store, DAY, stats, [])
    after = store.read(DAY) or ""
    after_sec = extract_section(after, REFLECT_MARKER)
    assert after_sec == before_sec
    assert _outside(before) == _outside(after)
    assert "手書きメモ reflect-keep" in after
    assert "手で直した" in (after_sec or "")
    pairs = read_reflect_answers(after_sec)
    assert pairs
    assert pairs[0][1] == "手で直した"


def test_d3_no_match_no_section():
    stats = {
        "day": DAY.isoformat(),
        "by_site": {"github.com": 5.0},  # < 30min
        "ai": {
            "session_digests": [
                {
                    "tests_run": False,
                    "ended_in_error": False,
                    "prompts_digest": [],
                    "end": "2026-08-02T12:00:00+09:00",
                }
            ]
        },
    }
    assert build_reflect_section(stats, [], today=DAY) is None
    assert collect_reflect_questions(stats, [], today=DAY) == []


# ---------------------------------------------------------------------------
# §E SECTION_ORDER / idempotent finalize
# ---------------------------------------------------------------------------


def test_e_section_order_and_finalize_idempotent(tmp_path: Path):
    import re

    from kaizenlog.cli import _finalize_note_layout
    from kaizenlog.vault import DailyNoteStore

    assert SECTION_ORDER.index(DIGEST_MARKER) < SECTION_ORDER.index(RESUME_MARKER)
    assert SECTION_ORDER.index(RESUME_MARKER) < SECTION_ORDER.index(GOAL_MARKER)
    assert SECTION_ORDER.index(EFFORT_MARKER) < SECTION_ORDER.index(
        EXPERIMENTS_MARKER
    )
    assert SECTION_ORDER.index(EXPERIMENTS_MARKER) < SECTION_ORDER.index(
        "kaizenlog:weekly-context"
    )
    assert SECTION_ORDER.index(ACTIVITY_MARKER) < SECTION_ORDER.index(
        REFLECT_MARKER
    )
    assert SECTION_ORDER.index(REFLECT_MARKER) < SECTION_ORDER.index(
        "kaizenlog:coach"
    )

    digest = build_digest(_base_stats(), [], today=DAY, redactor=None)
    resume = build_resume_section(_base_stats())
    reflect = build_reflect_section(_base_stats(), [], today=DAY)
    exp = build_experiments_section(
        [
            Experiment(
                path=Path("e.md"),
                title="t",
                status="running",
                metric="m",
                target_op="<=",
                target_value=1.0,
                start=DAY,
                deadline=DAY + timedelta(days=4),
            )
        ],
        [],
        today=DAY,
    )
    assert digest and resume and reflect and exp
    # max_inline=1 超過を確実化: 委譲の ※ に加え2本目を置く → FOOTNOTES へ集約
    digest = digest.rstrip() + "\n※ 第二の注記（脚注集約検証用）\n"

    # 手書きは ACTIVITY 内へ（区間外の自由文は FOOTNOTES 挿入と reorder の
    # 相互作用で 2 回目に位置がずれうるため、冪等検証ではマーカー内に置く）
    content = "---\ndate: 2026-08-02\n---\n\n"
    content = upsert_section(
        content, ACTIVITY_MARKER, "## act\n手書きメモ keep-in-activity\n"
    )
    content = upsert_section(content, DIGEST_MARKER, digest, position="top")
    content = upsert_section(content, RESUME_MARKER, resume, position="top")
    content = upsert_section(content, EXPERIMENTS_MARKER, exp, position="top")
    content = upsert_section(content, REFLECT_MARKER, reflect, position="bottom")
    content = upsert_section(
        content, EFFORT_MARKER, "## 工数\n", position="top"
    )

    store = DailyNoteStore(tmp_path)
    store.path_for(DAY).write_text(content, encoding="utf-8")
    _finalize_note_layout(store, DAY)
    once = store.read(DAY) or ""
    _finalize_note_layout(store, DAY)
    twice = store.read(DAY) or ""
    assert once == twice
    assert "手書きメモ keep-in-activity" in twice
    assert twice.index(DIGEST_MARKER) < twice.index(RESUME_MARKER)
    assert twice.index(EFFORT_MARKER) < twice.index(EXPERIMENTS_MARKER)
    assert twice.index(ACTIVITY_MARKER) < twice.index(REFLECT_MARKER)

    dig_sec = extract_section(twice, DIGEST_MARKER) or ""
    fn_sec = extract_section(twice, FOOTNOTES_MARKER) or ""
    assert "委譲の形" in dig_sec
    assert "エディタ前景時間" in dig_sec
    assert "### 🤝 委譲の形（" not in dig_sec
    # 1本目の ※ は DIGEST に残り、2本目は FOOTNOTES 定義へ
    assert "手作業の直接計測ではありません" in dig_sec
    assert fn_sec.strip(), "FOOTNOTES 区間が空（2本目の ※ が集約されていない）"
    assert re.search(r"^\[\^\d+\]:", fn_sec, re.MULTILINE)
    assert "第二の注記（脚注集約検証用）" in fn_sec
    assert re.search(r"\[\^\d+\]", dig_sec)


def test_delegation_config_defaults():
    cfg = Config()
    assert cfg.delegation.editor_apps
    assert "Code.exe" in cfg.delegation.editor_apps
    # 未記載 toml でも発火
    text = 'timezone = "Asia/Tokyo"\nvault_dir = "."\n'
    # load_config はファイル経路なので dataclass 既定で十分
    assert DelegationConfig().editor_apps


def test_resume_unchecked_kzn():
    actions = (
        "## 📌 今日やること\n"
        "- [ ] KZN-20260801-001 何かする\n"
        "- [x] KZN-20260801-002 済\n"
        "- [ ] KZN-20260801-003 未済\n"
    )
    body = build_resume_section(
        _base_stats(), prev_actions_content=actions
    )
    assert body is not None
    assert "未チェック📌: 2件" in body
    assert "KZN-20260801-001" in body
    assert "KZN-20260801-003" in body
    assert "KZN-20260801-002" not in body
