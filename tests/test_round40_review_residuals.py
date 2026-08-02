"""第40弾: 第38/39レビュー残件 + タイムライン時間被覆。"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from kaizenlog.advice_evidence import SHORT_RECORD_MIN_MINUTES, build_advice_evidence
from kaizenlog.aiwork import AISession, render_aiwork_markdown
from kaizenlog.digest import build_digest
from kaizenlog.memory import MemoryEntry, render_actions_section
from kaizenlog.report import Block, DailySummary, render_markdown
from kaizenlog.stats import write_stats
from kaizenlog.vault import ADVICE_MARKER
from kaizenlog.memory import append_entries


TZ = ZoneInfo("Asia/Tokyo")


def _block(
    start: datetime,
    minutes: float,
    category: str = "ブラウジング",
    app: str = "chrome.exe",
) -> Block:
    end = start + timedelta(minutes=minutes)
    return Block(start=start, end=end, category=category, app=app, titles=["t"])


def _summary_with_blocks(
    day: date,
    blocks: list[Block],
    total: float | None = None,
) -> DailySummary:
    t = total if total is not None else sum(b.minutes for b in blocks)
    by_cat: dict[str, float] = {}
    for b in blocks:
        by_cat[b.category] = by_cat.get(b.category, 0.0) + b.minutes
    return DailySummary(
        day=day,
        total_minutes=t,
        by_category=by_cat or {"開発": t},
        by_app={},
        blocks=blocks,
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=10,
        by_site={},
    )


# ---------- §Z1 console backfill / judge FAIL ----------


def _z1_cfg(vault):
    from kaizenlog.config import Config

    return Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        experiments_dir="03 Areas/Kaizen Experiments",
        stats_dir=".kaizenlog/stats",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
    )


def _z1_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "01 Daily Notes").mkdir(parents=True)
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / "03 Areas" / "Kaizen Experiments").mkdir(parents=True)
    return vault


def _z1_patch_generate(monkeypatch, cli_mod, cs: int = 50):
    from datetime import datetime as dt

    from kaizenlog.report import DailySummary

    monkeypatch.setattr(cli_mod, "collect_day", lambda *a, **k: ([], True))
    monkeypatch.setattr(cli_mod, "collect_input", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_mod,
        "summarize",
        lambda day, *a, **k: DailySummary(
            day=day,
            total_minutes=200.0,
            by_category={"開発": 200.0},
            by_app={},
            blocks=[],
            ai_tool_minutes={},
            ai_sessions=0,
            context_switches=cs,
            by_site={},
        ),
    )
    monkeypatch.setattr(cli_mod, "render_markdown", lambda *a, **k: "### a\n")
    monkeypatch.setattr(cli_mod.Classifier, "classify_all", lambda self, e: [])
    monkeypatch.setattr(cli_mod, "ActivityWatchClient", lambda url: MagicMock())
    return dt


def test_z1_backfill_provisional_console(tmp_path, monkeypatch, capsys):
    """(a) backfill 経路 provisional: ⏳ と 途中値。"""
    from kaizenlog import cli as cli_mod
    from kaizenlog.report import DailySummary

    vault = _z1_vault(tmp_path)
    memory = vault / "Kaizen" / "Memory"
    prop = date(2026, 7, 28)
    measure = date(2026, 7, 29)
    # 未判定。stats は測定日分あり。as_of=測定日 → backfill provisional
    append_entries(
        memory,
        [
            MemoryEntry(
                id="KZN-20260728-BF-P",
                date=prop.isoformat(),
                action="x｜PASS: context_switches <= 100｜FAIL: 101",
            )
        ],
    )
    write_stats(
        vault / ".kaizenlog" / "stats",
        measure,
        DailySummary(
            day=measure,
            total_minutes=180.0,
            by_category={"開発": 180.0},
            by_app={},
            blocks=[],
            ai_tool_minutes={},
            ai_sessions=0,
            context_switches=50,
            by_site={},
        ),
        [],
    )
    cfg = _z1_cfg(vault)
    cfg.aiwork.enabled = False
    dt = _z1_patch_generate(monkeypatch, cli_mod, cs=10)

    # as_of == measure: 測定日当日の generate。judge は proposal_day=measure-1=prop
    # で発火し得るが、entry は prop 日付なので judge 経路。
    # backfill 専用にする: 測定日の翌日 generate ではなく、stats はあるが
    # judge の proposal 日が対象外になる day を使う。
    # → day=7/30 で proposal 7/28 の judge は day-1=7/29 のみ対象。
    # backfill は window 内の未判定を拾う。as_of=7/29 で measure==as_of → provisional。
    class DayMeasure(dt):
        @classmethod
        def now(cls, tz=None):
            return dt(2026, 7, 29, 21, 30, tzinfo=tz or ZoneInfo("Asia/Tokyo"))

    monkeypatch.setattr(cli_mod, "datetime", DayMeasure)
    # generate(measure) は judge(proposal=measure-1=prop) も走る → ⏳ は出るが
    # 経路はアクション判定。backfill 専用行を確実に取るため:
    # まず judge 済み provisional を seed し、as_of=measure で未 confirmed の
    # 別IDを backfill させる（stats のみ・verdict 無し）。
    # 実装: generate 日を measure にし、proposal 日ノートを置かない。
    # judge は advice note を読むが entry は memory から。
    # より確実: backfill のみが拾う「測定日=as_of かつ未判定」を cmd_generate で走らせ、
    # 出力に バックフィル判定 + ⏳ を要求する。
    # cmd_generate(day) の judge は proposal_day = day - 1。
    # day=measure=7/29 → judge は 7/28 を見る → 本 entry がヒットし アクション判定 ⏳。
    # これを避けるには day を measure 以外にしつつ as_of(now) を measure にする必要がある。
    # 実際 backfill の as_of は day 引数。now は provisional/confirmed 切替に使う箇所もある。
    # → day=measure, entry.date=prop, judge hits. 許容: バックフィル行 OR 判定行で ⏳/途中値。
    # 仕様は「バックフィル判定」固定なので、judge を無効化して backfill だけ通す。
    monkeypatch.setattr(
        cli_mod,
        "judge_entries",
        lambda *a, **k: [],
    )
    capsys.readouterr()
    cli_mod.cmd_generate(cfg, measure)
    out = capsys.readouterr().out
    assert "🧪 バックフィル判定: KZN-20260728-BF-P ⏳" in out
    assert "途中値" in out


def test_z1_backfill_confirmed_console(tmp_path, monkeypatch, capsys):
    """(b) backfill 経路 confirmed PASS: ✅ と 実測。"""
    from kaizenlog import cli as cli_mod
    from kaizenlog.report import DailySummary

    vault = _z1_vault(tmp_path)
    memory = vault / "Kaizen" / "Memory"
    prop = date(2026, 7, 28)
    measure = date(2026, 7, 29)
    as_of = date(2026, 7, 30)
    append_entries(
        memory,
        [
            MemoryEntry(
                id="KZN-20260728-BF-C",
                date=prop.isoformat(),
                action="x｜PASS: context_switches <= 100｜FAIL: 101",
                verdict="pass",
                verdict_value=50.0,
                verdict_date=measure.isoformat(),
                verdict_stage="provisional",
            )
        ],
    )
    write_stats(
        vault / ".kaizenlog" / "stats",
        measure,
        DailySummary(
            day=measure,
            total_minutes=180.0,
            by_category={"開発": 180.0},
            by_app={},
            blocks=[],
            ai_tool_minutes={},
            ai_sessions=0,
            context_switches=50,
            by_site={},
        ),
        [],
    )
    cfg = _z1_cfg(vault)
    cfg.aiwork.enabled = False
    dt = _z1_patch_generate(monkeypatch, cli_mod, cs=10)
    monkeypatch.setattr(cli_mod, "judge_entries", lambda *a, **k: [])

    class DayNext(dt):
        @classmethod
        def now(cls, tz=None):
            return dt(2026, 7, 30, 12, 0, tzinfo=tz or ZoneInfo("Asia/Tokyo"))

    monkeypatch.setattr(cli_mod, "datetime", DayNext)
    capsys.readouterr()
    cli_mod.cmd_generate(cfg, as_of)
    out = capsys.readouterr().out
    assert "🧪 バックフィル判定: KZN-20260728-BF-C ✅" in out
    assert "実測" in out


def test_z1_judge_confirmed_fail_console(tmp_path, monkeypatch, capsys):
    """(c) judge 経路 confirmed FAIL: ❌ と 実測。"""
    from kaizenlog import cli as cli_mod

    vault = _z1_vault(tmp_path)
    memory = vault / "Kaizen" / "Memory"
    prop = date(2026, 7, 28)
    judge_day = date(2026, 7, 29)
    append_entries(
        memory,
        [
            MemoryEntry(
                id="KZN-20260728-FAIL",
                date=prop.isoformat(),
                action="x｜PASS: context_switches <= 10｜FAIL: 11",
            )
        ],
    )
    start = f"<!-- {ADVICE_MARKER}:start -->"
    end = f"<!-- {ADVICE_MARKER}:end -->"
    (vault / "01 Daily Notes" / f"{prop.isoformat()}.md").write_text(
        f"{start}\n- [ ] KZN-20260728-FAIL: b\n{end}\n", encoding="utf-8"
    )
    cfg = _z1_cfg(vault)
    cfg.aiwork.enabled = False
    dt = _z1_patch_generate(monkeypatch, cli_mod, cs=50)

    class DayAfter(dt):
        @classmethod
        def now(cls, tz=None):
            return dt(2026, 7, 30, 12, 0, tzinfo=tz or ZoneInfo("Asia/Tokyo"))

    monkeypatch.setattr(cli_mod, "datetime", DayAfter)
    capsys.readouterr()
    cli_mod.cmd_generate(cfg, judge_day)
    out = capsys.readouterr().out
    assert "🧪 アクション判定: KZN-20260728-FAIL ❌" in out
    assert "実測 50" in out


# ---------- §Z2 trajectory revival ----------


def test_z2_regressed_pass_achieved_shown_with_cap():
    from datetime import timedelta

    target = date(2026, 8, 3)
    # 3 pass_achieved: 2 with ❌ in traj, 1 all ✅
    hist = []
    for i, v in enumerate([178, 639, 48, 10, 5]):
        d = date(2026, 7, 29) + timedelta(days=i)
        hist.append({"day": d.isoformat(), "ai": {"tool_errors": v, "sessions": 1}})

    def make(id_s: str, date_s: str, vdate: str, thr: str = "100"):
        return MemoryEntry(
            id=id_s,
            date=date_s,
            action=f"x｜PASS: ai_tool_errors <= {thr}｜FAIL: {int(thr)+1}",
            status="proposed",
            verdict="pass",
            verdict_value=1.0,
            verdict_date=vdate,
            verdict_stage="confirmed",
        )

    # e1: after 7/28 → has 639 ❌
    e1 = make("KZN-20260727-001", "2026-07-27", "2026-07-28", "100")
    e2 = make("KZN-20260727-002", "2026-07-27", "2026-07-28", "100")
    # e3 all pass with thr 10000
    e3 = make("KZN-20260728-003", "2026-07-28", "2026-07-28", "10000")
    out = render_actions_section(
        [e1, e2, e3], target, stats_history=hist
    )
    assert out is not None
    assert "指標は達成済み 3件" in out
    assert "達成済みだが指標が戻っています" in out
    assert "KZN-20260727-001" in out
    assert "KZN-20260727-002" in out
    assert "判定後の実測" in out
    # e3 should not appear as individual (no ❌)
    # uncompleted still 1-cap style
    assert "kaizenlog done" in out or "today --all" in out

    # all green
    hist_ok = [
        {"day": "2026-07-29", "ai": {"tool_errors": 1, "sessions": 1}},
        {"day": "2026-07-30", "ai": {"tool_errors": 1, "sessions": 1}},
    ]
    out2 = render_actions_section([e3], target, stats_history=hist_ok)
    assert "達成済みだが指標が戻っています" not in out2
    assert "指標は達成済み 1件" in out2


def test_z2_more_than_two_shows_extra_line():
    from datetime import timedelta

    # recent 窓 (target-7 〜 target-1) に収まる提案日にする
    target = date(2026, 8, 5)
    hist = []
    for i in range(1, 6):
        d = date(2026, 7, 30) + timedelta(days=i)
        hist.append({"day": d.isoformat(), "ai": {"tool_errors": 500, "sessions": 1}})
    entries = []
    for i in range(3):
        d = date(2026, 7, 30) + timedelta(days=i)  # 7/30, 7/31, 8/1
        entries.append(
            MemoryEntry(
                id=f"KZN-{d.strftime('%Y%m%d')}-00{i}",
                date=d.isoformat(),
                action="x｜PASS: ai_tool_errors <= 10｜FAIL: 11",
                status="proposed",
                verdict="pass",
                verdict_value=1.0,
                verdict_date=d.isoformat(),
                verdict_stage="confirmed",
            )
        )
    out = render_actions_section(entries, target, stats_history=hist)
    assert "ほか 1件も推移に未達日があります" in out


# ---------- §Z3 unified gap ----------


def test_z3_measurement_gap_cli_only_both_or_neither():
    # web only + short → neither
    stats_web = {
        "day": "2026-08-01",
        "total_minutes": 30.0,
        "context_switches": 5,
        "by_category": {"AI作業": 5.0},
        "ai": {
            "sessions": 3,
            "fragmented": 0,
            "tool_errors": 0,
            "interruptions": 0,
            "sources": {"chatgpt-web": {"sessions": 3}},
        },
        "blocks": [],
    }
    ev = build_advice_evidence(stats_web, source_status="verified")
    assert "F19" not in ev.markdown
    web_sess = [
        AISession(
            session_id=f"w{i}",
            project="p",
            start=datetime(2026, 8, 1, 10, i, tzinfo=timezone.utc),
            end=datetime(2026, 8, 1, 10, i + 1, tzinfo=timezone.utc),
            user_turns=1,
            source="chatgpt-web",
        )
        for i in range(3)
    ]
    md = render_aiwork_markdown(
        web_sess,
        timezone.utc,
        screen_total_minutes=30.0,
        measurement_gap=False,
        structured_cli_sessions=0,
    )
    assert "計測欠測の疑い" not in md

    # CLI 3 + 14 min → both
    stats_cli = {
        "day": "2026-08-01",
        "total_minutes": 14.0,
        "context_switches": 5,
        "by_category": {"AI作業": 5.0},
        "ai": {
            "sessions": 3,
            "fragmented": 0,
            "tool_errors": 10,
            "interruptions": 0,
            "sources": {"claude-code": {"sessions": 3}},
        },
        "blocks": [],
    }
    ev2 = build_advice_evidence(stats_cli, source_status="verified")
    assert "F19" in ev2.markdown
    cli_sess = [
        AISession(
            session_id=f"c{i}",
            project="p",
            start=datetime(2026, 8, 1, 10, i, tzinfo=timezone.utc),
            end=datetime(2026, 8, 1, 10, i + 1, tzinfo=timezone.utc),
            user_turns=1,
            source="claude-code",
        )
        for i in range(3)
    ]
    md2 = render_aiwork_markdown(
        cli_sess,
        timezone.utc,
        screen_total_minutes=14.0,
        measurement_gap=True,
        structured_cli_sessions=3,
    )
    assert "計測欠測の疑い" in md2
    assert "doctor" in md2

    # fat day
    stats_f = dict(stats_cli)
    stats_f["total_minutes"] = 180.0
    stats_f["by_category"] = {"開発": 150.0, "AI作業": 30.0}
    assert "F19" not in build_advice_evidence(stats_f, source_status="verified").markdown


def test_z3_threshold_literal_single_module():
    import pathlib
    import re

    root = pathlib.Path("src/kaizenlog")
    hits = []
    for p in root.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"(?<![\w.])120(?:\.0)?(?![\w.])", line) and "SHORT_RECORD" not in line:
                # allow comments about 120
                if "short_record" in line.lower() or "< 120" in line or "120分" in line:
                    hits.append(f"{p}:{i}:{line.strip()}")
    # SHORT_RECORD_MIN_MINUTES = 120.0 is the one source
    # No hardcode short_record comparisons elsewhere
    hard = [h for h in hits if "< 120" in h or "total_minutes < 120" in h]
    assert hard == [], hard
    assert SHORT_RECORD_MIN_MINUTES == 120.0


# ---------- §Z4 goal exempt ----------


def test_z4_goal_with_kaizen_word_shown():
    stats = {
        "source_status": "verified",
        "activity_sha256": "x",
        "total_minutes": 100.0,
        "by_category": {"AI作業": 10.0},
        "ai": {"session_digests": []},
    }
    body = build_digest(
        stats,
        [],
        today=date(2026, 8, 1),
        redactor=lambda s: s,
        goal_text="日誌の改善を完了する",
    )
    assert body is not None
    assert "目標: 日誌の改善を完了する" in body


def test_z4_friction_with_kaizen_word_drops_digest():
    stats = {
        "source_status": "verified",
        "activity_sha256": "x",
        "total_minutes": 100.0,
        "by_category": {"AI作業": 10.0},
        "ai": {
            "session_digests": [
                {
                    "day": "2026-08-01",
                    "project": "p",
                    "title": "改善タスク",
                    "tool_errors": 2,
                    "interruptions": 0,
                    "retry_touch": 0,
                    "friction": 2,
                }
            ]
        },
    }
    assert (
        build_digest(stats, [], today=date(2026, 8, 1), redactor=lambda s: s)
        is None
    )


def test_z4_no_redactor_skips_goal():
    stats = {
        "source_status": "verified",
        "activity_sha256": "x",
        "total_minutes": 100.0,
        "by_category": {"AI作業": 10.0},
    }
    body = build_digest(
        stats, [], today=date(2026, 8, 1), redactor=None, goal_text="改善する"
    )
    assert body is not None
    assert "目標:" not in body
    assert "稼働:" in body


# ---------- §A1 / §A2 timeline ----------


def test_a1_fragment_rows_and_sum_and_order():
    day = date(2026, 8, 2)
    base = datetime(2026, 8, 2, 8, 0, tzinfo=TZ)
    blocks = [
        _block(base, 2.0, "ブラウジング"),  # under
        _block(base + timedelta(minutes=5), 1.5, "AI作業"),  # under
        _block(base + timedelta(hours=1), 10.0, "開発"),  # eligible
        _block(base + timedelta(hours=2), 1.0, "エンタメ"),
        _block(base + timedelta(hours=2, minutes=10), 1.0, "コミュニケーション"),
        _block(base + timedelta(hours=2, minutes=20), 1.0, "執筆・ノート"),
        _block(base + timedelta(hours=2, minutes=30), 1.0, "会議"),  # 4 cats → ほか
    ]
    # total = 2+1.5+10+1*4 = 17.5
    s = _summary_with_blocks(day, blocks, total=17.5)
    md = render_markdown(s, TZ, min_block_minutes=3.0)
    assert "細切れ" in md
    assert "08:00-08:59" in md
    assert "10:00-10:59" in md
    assert "ほか" in md  # 4 categories in hour 10
    assert "この表は合計" in md and "%" in md
    # 表の時間列総和 ≈ total_minutes（±1分）— タイムラインの HH:MM- 行のみ
    import re

    mins = 0.0
    for ln in md.splitlines():
        m = re.match(
            r"\| \d{2}:\d{2}-\d{2}:\d{2} \| ([0-9.]+m|[0-9]+h[0-9]*m?) \|",
            ln,
        )
        if not m:
            continue
        t = m.group(1)
        if t.endswith("m") and "h" not in t:
            mins += float(t[:-1])
        elif "h" in t:
            h, rest = t.split("h", 1)
            mins += float(h) * 60
            if rest.endswith("m") and rest[:-1]:
                mins += float(rest[:-1])
    assert abs(mins - 17.5) <= 1.0, f"table_sum={mins} total=17.5 md=\n{md}"
    # order: 08 frag, 09 eligible, 10 frag
    i08 = md.index("08:00-08:59")
    i09 = md.index("09:00") if "09:00" in md else md.index("開発")
    i10 = md.index("10:00-10:59")
    assert i08 < i09 < i10


def test_a1_no_under_blocks_no_fragment_rows():
    day = date(2026, 8, 2)
    base = datetime(2026, 8, 2, 9, 0, tzinfo=TZ)
    blocks = [_block(base, 30.0, "開発")]
    s = _summary_with_blocks(day, blocks, total=30.0)
    md = render_markdown(s, TZ, min_block_minutes=3.0)
    assert "細切れ" not in md
    assert "表示外:" not in md
    assert "100%" in md or "この表は合計" in md


def test_a1_max_timeline_rows_keeps_fragments():
    day = date(2026, 8, 2)
    base = datetime(2026, 8, 2, 8, 0, tzinfo=TZ)
    blocks = [
        _block(base + timedelta(hours=i), 10.0, "開発") for i in range(5)
    ] + [
        _block(base + timedelta(minutes=1), 1.0, "ブラウジング")
    ]
    s = _summary_with_blocks(day, blocks, total=51.0)
    md = render_markdown(s, TZ, min_block_minutes=3.0, max_timeline_rows=2)
    assert "細切れ" in md
    assert "08:00-08:59" in md


def test_a2_zero_total_no_coverage_line():
    day = date(2026, 8, 2)
    s = DailySummary(
        day=day,
        total_minutes=0.0,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
        by_site={},
    )
    md = render_markdown(s, TZ)
    assert "この表は合計" not in md
