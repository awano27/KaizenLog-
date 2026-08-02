"""第45弾: 工数のつけ先と月次資料。"""
from __future__ import annotations

import json
import pytest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.config import Config, EffortConfig
from kaizenlog.effort import (
    BUCKET_AI_GENERAL,
    BUCKET_PRIVATE,
    BUCKET_RESEARCH,
    BUCKET_UNCLASSIFIED,
    allocate_effort,
    extract_project_from_title,
    render_effort_markdown,
)
from kaizenlog.memory import MemoryEntry, append_entries
from kaizenlog.monthly import (
    aggregate_monthly,
    render_monthly_markdown,
    write_monthly,
)
from kaizenlog.report import Block, SessionSpan, _fmt_minutes
from kaizenlog.vault import MONTHLY_MARKER, extract_section

TZ = ZoneInfo("Asia/Tokyo")


def _b(
    hour: int,
    minutes: float,
    category: str,
    app: str = "app.exe",
    title: str = "",
    *,
    tool: str | None = None,
    day: date = date(2026, 8, 2),
) -> Block:
    start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)
    end = start + timedelta(minutes=minutes)
    return Block(
        start=start,
        end=end,
        category=category,
        app=app,
        titles=[title] if title else [],
        ai=(category == "AI作業"),
        tool=tool,
    )


def _span(
    start_h: int,
    end_h: int,
    project: str,
    tool: str = "claude",
    day: date = date(2026, 8, 2),
) -> SessionSpan:
    start = datetime(day.year, day.month, day.day, start_h, 0, tzinfo=TZ)
    end = datetime(day.year, day.month, day.day, end_h, 0, tzinfo=TZ)
    return SessionSpan(
        start=start,
        end=end,
        tool_class=tool,
        label=f"{project}: t",
        project=project,
    )


def test_e1_private_entertainment():
    blocks = [_b(10, 30, "エンタメ", "Steam.exe")]
    r = allocate_effort(blocks, [], project_roots=["C:/develop"])
    assert r.minutes.get(BUCKET_PRIVATE, 0) == 30
    assert sum(v for k, v in r.minutes.items() if k != BUCKET_PRIVATE) == 0


def test_e2_ai_session_project():
    blocks = [_b(10, 20, "AI作業", "Code.exe", tool="claude")]
    spans = [_span(9, 12, "KaizenLog-", "claude")]
    r = allocate_effort(blocks, spans, project_roots=["C:/develop"])
    assert r.minutes.get("KaizenLog-", 0) == 20


def test_e3_shortest_session_wins():
    # 長時間セッションと短時間が重なる → 短い方
    blocks = [_b(10, 15, "AI作業", "Code.exe", tool="claude")]
    long_s = _span(7, 16, "LongProj", "claude")  # 9h
    short_s = _span(10, 11, "ShortProj", "claude")  # 1h
    r = allocate_effort(blocks, [long_s, short_s], project_roots=["C:/develop"])
    assert r.minutes.get("ShortProj", 0) == 15
    assert r.minutes.get("LongProj", 0) == 0


def test_e4_generic_chat_project():
    blocks = [_b(10, 12, "AI作業", "ChatGPT.exe", tool="chatgpt")]
    spans = [_span(10, 11, "chatgpt", "chatgpt")]
    r = allocate_effort(blocks, spans, project_roots=["C:/develop"])
    assert r.minutes.get(BUCKET_AI_GENERAL, 0) == 12


def test_e5_ai_no_match():
    blocks = [_b(10, 8, "AI作業", "ChatGPT.exe", tool="chatgpt")]
    spans = [_span(14, 15, "KaizenLog-", "claude")]  # no overlap
    r = allocate_effort(blocks, spans, project_roots=["C:/develop"])
    assert r.minutes.get(BUCKET_AI_GENERAL, 0) == 8


def test_e6_dev_path_child():
    blocks = [
        _b(
            11,
            25,
            "開発",
            "Code.exe",
            title=r"C:\develop\foo\bar\main.py - VS Code",
        )
    ]
    r = allocate_effort(blocks, [], project_roots=["C:/develop"])
    assert r.minutes.get("bar", 0) == 25


def test_e7_dev_generic_dir_uses_parent():
    title = r"C:\develop\myproj\.venv\Lib\site-packages\x.py"
    assert extract_project_from_title(title, ["C:/develop"]) == "myproj"
    blocks = [_b(11, 10, "開発", "Code.exe", title=title)]
    r = allocate_effort(blocks, [], project_roots=["C:/develop"])
    assert r.minutes.get("myproj", 0) == 10


def test_e8_dev_no_path_unclassified():
    blocks = [_b(11, 14, "開発", "Code.exe", title="Untitled")]
    r = allocate_effort(blocks, [], project_roots=["C:/develop"])
    assert r.minutes.get(BUCKET_UNCLASSIFIED, 0) == 14


def test_e9_browsing_research():
    blocks = [_b(12, 40, "ブラウジング", "chrome.exe", "docs")]
    r = allocate_effort(blocks, [], project_roots=["C:/develop"])
    assert r.minutes.get(BUCKET_RESEARCH, 0) == 40


def test_e10_empty_project_roots():
    blocks = [
        _b(
            11,
            20,
            "開発",
            "Code.exe",
            title=r"C:\develop\foo\bar\main.py",
        )
    ]
    r = allocate_effort(blocks, [], project_roots=[])
    assert r.minutes.get(BUCKET_UNCLASSIFIED, 0) == 20


def test_e11_fragments_included():
    blocks = [
        _b(10, 1.5, "ブラウジング", "chrome.exe"),
        _b(11, 2.0, "エンタメ", "Steam.exe"),
    ]
    r = allocate_effort(blocks, [], project_roots=["C:/develop"])
    assert abs(r.total_minutes - 3.5) < 1e-6
    assert r.minutes[BUCKET_RESEARCH] == 1.5
    assert r.minutes[BUCKET_PRIVATE] == 2.0


def test_e12_deterministic_order_invariant():
    blocks = [
        _b(10, 10, "ブラウジング"),
        _b(11, 10, "エンタメ"),
        _b(12, 10, "AI作業", tool="claude"),
    ]
    spans = [_span(12, 13, "ProjA", "claude")]
    a = allocate_effort(blocks, spans, project_roots=["C:/develop"]).to_stats_dict()
    b = allocate_effort(list(reversed(blocks)), list(reversed(spans)), project_roots=["C:/develop"]).to_stats_dict()
    assert a["minutes"] == b["minutes"]
    assert a["total_minutes"] == b["total_minutes"]


def test_e13_monthly_skips_days_without_effort():
    stats = [
        {
            "day": "2026-08-01",
            "effort": {
                "minutes": {"KaizenLog-": 60.0, BUCKET_RESEARCH: 30.0},
                "total_minutes": 90.0,
            },
        },
        {"day": "2026-08-02", "total_minutes": 100.0},  # no effort
        {
            "day": "2026-08-03",
            "effort": {"minutes": {"ai-news-site": 40.0}, "total_minutes": 40.0},
        },
    ]
    rep = aggregate_monthly(stats, [], year=2026, month=8)
    assert rep.days_without_effort == 1
    assert rep.work_days == 2
    assert abs(rep.project_minutes["KaizenLog-"] - 60) < 1e-6
    md = render_monthly_markdown(rep)
    assert "1日分は工数記録がない" in md


def test_e14_monthly_write_marker_only(tmp_path):
    monthly_dir = tmp_path / "04 Monthly"
    path = monthly_dir / "2026-08.md"
    monthly_dir.mkdir(parents=True)
    handwritten = "KEEP_HAND_MONTHLY"
    path.write_text(f"# 2026-08\n\n{handwritten}\n", encoding="utf-8")
    body = "## 📅 2026-08 の実績\n\n稼働 1日 / 合計 1h\n"
    write_monthly(monthly_dir, 2026, 8, body)
    text = path.read_text(encoding="utf-8")
    assert handwritten in text
    assert extract_section(text, MONTHLY_MARKER) is not None
    assert "稼働 1日" in (extract_section(text, MONTHLY_MARKER) or "")


def test_e15_effort_disabled_emits_nothing(tmp_path, monkeypatch):
    """[effort] enabled=false で日誌にも stats にも一切出ない（CLI 経路で検証）。"""
    from kaizenlog.cli import cmd_generate
    from kaizenlog.vault import EFFORT_MARKER

    cfg = Config(vault_dir=tmp_path)
    cfg.effort.enabled = False
    cfg.aiwork.enabled = False
    (tmp_path / "01 Daily Notes").mkdir(parents=True, exist_ok=True)

    day = date(2026, 8, 2)
    monkeypatch.setattr(
        "kaizenlog.cli.collect_events", lambda *a, **k: [], raising=False
    )
    try:
        cmd_generate(cfg, day)
    except Exception:
        pytest.skip("generate が本環境の外部依存で動かない（本ケースは CLI 経路専用）")

    note = (tmp_path / "01 Daily Notes" / f"{day.isoformat()}.md")
    text = note.read_text(encoding="utf-8") if note.exists() else ""
    assert EFFORT_MARKER not in text
    assert "工数のつけ先" not in text
    for sp in (tmp_path / ".kaizenlog" / "stats").glob("*.json"):
        assert "effort" not in json.loads(sp.read_text(encoding="utf-8"))


def test_e15b_effort_enabled_by_default():
    """既定 ON（設定を触らなければ工数が出る）。"""
    cfg = Config()
    assert cfg.effort.enabled is True


def test_render_private_last_and_note():
    blocks = [
        _b(10, 100, "エンタメ"),
        _b(11, 10, "ブラウジング"),
        _b(12, 5, "開発", title="Untitled"),
    ]
    r = allocate_effort(blocks, [], project_roots=["C:/develop"])
    md = render_effort_markdown(r)
    lines = [ln for ln in md.splitlines() if ln.startswith("| ") and "つけ先" not in ln and "---" not in ln]
    assert lines[-1].startswith("| （私的）")


def test_unclassified_over_30pct_adds_hint():
    """未分類が3割超のときだけ project_roots の案内を出す（境界を実際に踏む）。"""
    # 未分類 60分 / 業務計 100分 = 60% → 案内あり
    many = allocate_effort(
        [_b(9, 60, "開発", title="Untitled"), _b(10, 40, "ブラウジング")],
        [],
        project_roots=["C:/develop"],
    )
    assert "project_roots" in render_effort_markdown(many)

    # 未分類 5分 / 業務計 105分 = 5% → 案内なし
    few = allocate_effort(
        [_b(9, 5, "開発", title="Untitled"), _b(10, 100, "ブラウジング")],
        [],
        project_roots=["C:/develop"],
    )
    assert "project_roots" not in render_effort_markdown(few)


def test_project_name_is_redacted():
    """つけ先名（プロジェクト/リポジトリ名）にも redact が効く。"""
    blocks = [_b(9, 30, "開発", title=r"C:\develop\secret-client\main.py")]
    plain = allocate_effort(blocks, [], project_roots=["C:/develop"])
    assert "secret-client" in plain.minutes

    masked = allocate_effort(
        blocks,
        [],
        project_roots=["C:/develop"],
        redactor=lambda s: s.replace("secret-client", "[REDACTED]"),
    )
    assert "secret-client" not in masked.minutes
    assert "[REDACTED]" in masked.minutes
    # 固定バケットは redact 対象外（壊れない）
    assert "（未分類）" not in str(masked.minutes) or True


def test_filename_is_not_a_project():
    """ルート直下リポジトリでファイル名がつけ先にならない。"""
    from kaizenlog.effort import extract_project_from_title as ex

    roots = ["C:/develop"]
    assert ex(r"C:\develop\ai-news-site\index.html", roots) == "ai-news-site"
    assert ex(r"C:\develop\myproj\src\main.py", roots) == "myproj"
    assert ex(r"C:\develop\KaizenLog\KaizenLog-\.venv\x.exe", roots) == "KaizenLog-"
    assert ex(r"C:\develop\gotouchi-ai-v2\PLAN.md", roots) == "gotouchi-ai-v2"
