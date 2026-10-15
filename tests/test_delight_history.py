"""Synthetic, no-write contracts for the history-recall view."""
from __future__ import annotations

import json
from datetime import date

from kaizenlog.history_recall import render_anniversaries, search_history


def _write_stat(stats_dir, day, **extra):
    data = {"day": day, "blocks": [], "ai": {}, "effort": {}}
    data.update(extra)
    (stats_dir / f"{day}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def test_searches_full_history_after_redaction_and_skips_private_and_malformed(tmp_path):
    stats = tmp_path / "stats"
    stats.mkdir()
    _write_stat(
        stats,
        "2026-01-15",
        blocks=[
            {"category": "開発", "title": "CustomerX release investigation", "app": "Code"},
            {"category": "エンタメ", "title": "needle must never display", "app": "browser"},
            "malformed",
        ],
        ai={"session_digests": [{"title": "Needle session CustomerX", "project": "app"}]},
        effort={"minutes": {"CustomerX repo": 30}},
    )
    (stats / "broken.json").write_text("{not json", encoding="utf-8")

    hits = search_history(
        stats,
        "customerx",
        redactor=lambda text: text.replace("CustomerX", "[REDACTED]"),
        private_title_patterns=("needle",),
    )

    shown = "\n".join(hit.text for hit in hits)
    assert len(hits) == 2
    assert "CustomerX" not in shown
    assert "[REDACTED]" in shown
    assert "needle" not in shown


def test_anniversaries_use_calendar_month_boundaries_and_only_confirmed_records(tmp_path):
    stats = tmp_path / "stats"
    memory = tmp_path / "memory"
    stats.mkdir()
    memory.mkdir()
    _write_stat(stats, "2026-02-28", blocks=[{"category": "開発", "title": "one-month"}])
    _write_stat(stats, "2025-12-31", blocks=[{"category": "開発", "title": "three-months"}])
    (memory / "suggestions.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"id": "KZN-1", "date": "2026-02-28", "action": "confirmed", "verdict": "pass", "verdict_stage": "confirmed", "decision": {"choice": "adopt"}}, ensure_ascii=False),
                json.dumps({"id": "KZN-2", "date": "2025-12-31", "action": "provisional", "verdict": "pass", "verdict_stage": "provisional"}, ensure_ascii=False),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    text = render_anniversaries(stats, memory, date(2026, 3, 31))

    assert "1か月前（2026-02-28）" in text
    assert "3か月前（2025-12-31）" in text
    assert "one-month" in text
    assert "three-months" in text
    assert "KZN-1" in text
    assert "KZN-2" not in text
    assert "現在の台帳状態" in text


def test_recall_rejects_mismatched_days_private_effort_and_markdown_injection(tmp_path):
    stats = tmp_path / "stats"
    stats.mkdir()
    _write_stat(
        stats, "2026-02-01",
        blocks=[{"category": "開発", "title": "![bad](x) <tag>", "app": "Code"}],
        effort={"minutes": {"（私的）": 99, "public": 1}},
    )
    # Filename and embedded day must both be canonical and equal.
    _write_stat(stats, "2026-02-02", blocks=[{"category": "開発", "title": "wrong day"}])
    (stats / "2026-02-03.json").write_text(
        json.dumps({"day": "2026-02-04", "blocks": [{"title": "forged"}]}),
        encoding="utf-8",
    )

    hits = search_history(stats, "bad")
    effort = search_history(stats, "public")
    forged = search_history(stats, "forged")

    assert len(hits) == 1
    assert "![bad]" not in hits[0].text
    assert "\\!\\[bad\\]" in hits[0].text
    assert len(effort) == 1
    assert not search_history(stats, "私的")
    assert forged == []
