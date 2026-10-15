"""Integration contracts for the marker-owned reader-facing views."""
from __future__ import annotations

import json
from datetime import date

from kaizenlog.config import Config, PrivacyConfig
from kaizenlog.delight_views import (
    DAILY_ANNIVERSARY_MARKER,
    MONTHLY_ALBUM_MARKER,
    WEEKLY_STORY_MARKER,
    preview_monthly_album,
    write_daily_anniversaries,
    write_monthly_album,
    write_weekly_story,
)
from kaizenlog.vault import extract_section


def _config(tmp_path, *, redact_patterns=(), private_title_patterns=()):
    return Config(
        vault_dir=tmp_path / "vault",
        privacy=PrivacyConfig(
            redact_patterns=list(redact_patterns),
            private_title_patterns=list(private_title_patterns),
        ),
    )


def _write_stat(cfg, day, **extra):
    cfg.stats_path.mkdir(parents=True, exist_ok=True)
    stat = {"day": day, "blocks": [], "ai": {}, "effort": {}}
    stat.update(extra)
    (cfg.stats_path / f"{day}.json").write_text(
        json.dumps(stat, ensure_ascii=False), encoding="utf-8"
    )


def test_daily_anniversaries_replace_only_their_marker_and_redact_output(tmp_path):
    cfg = _config(tmp_path, redact_patterns=("CustomerX",))
    _write_stat(
        cfg,
        "2026-02-28",
        blocks=[{"category": "開発", "title": "CustomerX release", "app": "Code"}],
    )
    note = cfg.daily_notes_path / "2026-03-31.md"
    note.parent.mkdir(parents=True)
    note.write_text("# 手書き\r\n\r\n本文は保持\r\n", encoding="utf-8", newline="")

    path = write_daily_anniversaries(cfg, date(2026, 3, 31))
    first = path.read_bytes().decode("utf-8")
    write_daily_anniversaries(cfg, date(2026, 3, 31))
    second = path.read_bytes().decode("utf-8")

    assert path == note
    assert first == second
    assert "# 手書き\r\n\r\n本文は保持\r\n" in second
    section = extract_section(second, DAILY_ANNIVERSARY_MARKER)
    assert section is not None
    assert "CustomerX" not in section
    assert "\\[REDACTED\\] release" in section


def test_weekly_story_coexists_with_context_marker_and_preserves_user_text(tmp_path):
    cfg = _config(tmp_path)
    for day, total in (("2026-08-03", 10), ("2026-08-04", 20), ("2026-08-05", 30)):
        _write_stat(cfg, day, total_minutes=total, ai={"loop_tax": {"total_wasted_tokens": 2}})
    note = cfg.daily_notes_path / "Weekly Reviews" / "2026-W32.md"
    note.parent.mkdir(parents=True)
    context = "<!-- kaizenlog:weekly-context:start -->\ncontext stays\n<!-- kaizenlog:weekly-context:end -->"
    note.write_text(f"# Weekly\n\n{context}\n\n自分の振り返り\n", encoding="utf-8")

    path = write_weekly_story(cfg, date(2026, 8, 3))
    content = path.read_text(encoding="utf-8")

    assert path == note
    assert "context stays" in content
    assert "自分の振り返り" in content
    section = extract_section(content, WEEKLY_STORY_MARKER)
    assert section is not None
    assert "今週の物語" in section
    assert "ループ税は6 tokens（3日分）" in section


def test_monthly_preview_writes_nothing_and_write_adds_album_and_sparklines(tmp_path):
    cfg = _config(tmp_path)
    _write_stat(
        cfg,
        "2026-08-02",
        ai={"loop_tax": {"total_wasted_tokens": 2}, "est_cost_usd": 0.1},
    )
    cfg.memory_path.mkdir(parents=True)
    (cfg.memory_path / "suggestions.jsonl").write_text(
        json.dumps(
            {
                "id": "KZN-1",
                "date": "2026-08-02",
                "action": "confirmed action",
                "verdict": "pass",
                "verdict_stage": "confirmed",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    note = cfg.monthly_path / "2026-08.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "# 2026-08\n\n手書きの月次メモ\n\n"
        "<!-- kaizenlog:monthly:start -->\nbase report\n<!-- kaizenlog:monthly:end -->\n",
        encoding="utf-8",
    )

    preview = preview_monthly_album(cfg, 2026, 8)

    assert "月刊アルバム" in preview
    assert not (cfg.monthly_path / "assets").exists()
    assert note.read_text(encoding="utf-8").endswith("<!-- kaizenlog:monthly:end -->\n")

    path = write_monthly_album(cfg, 2026, 8)
    content = path.read_text(encoding="utf-8")
    section = extract_section(content, MONTHLY_ALBUM_MARKER)

    assert path == note
    assert "手書きの月次メモ" in content
    assert "base report" in content
    assert section is not None
    assert "KZN-1" in section
    assert "![[assets/loop-tax-2026-08.svg]]" in section
    assert (cfg.monthly_path / "assets" / "loop-tax-2026-08.svg").is_file()
    assert (cfg.monthly_path / "assets" / "est-cost-usd-2026-08.svg").is_file()
