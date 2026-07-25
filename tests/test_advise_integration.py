from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from kaizenlog.advisor import AdvisorError
from kaizenlog.cli import cmd_advise
from kaizenlog.config import Config
from kaizenlog.memory import MemoryEntry, append_entries, load_entries
from kaizenlog.stats import activity_fingerprint
from kaizenlog.vault import ACTIVITY_MARKER, ADVICE_MARKER, DailyNoteStore


DAY = date(2026, 7, 21)
VALID_GENERATED = """## 🚀 Kaizen（AIからの改善提案）

### 今日の改善提案
1. [F3] 集中枠を固定し、翌日の回数で効果を確認する。
2. [F9] 調査リンクをまとめ、カテゴリ遷移回数で効果を確認する。

### 明日の最小アクション
- [ ] [F3] 始業時に集中枠を1件入れる｜PASS: 2回以上｜FAIL: 1回以下
- [ ] [F9] 調査リンクを3件まとめる｜PASS: 遷移5回以下｜FAIL: 6回以上

### AI作業の改善
- [F5] 会話テレメトリが無ければ品質を断定しない。
"""


def _config(tmp_path) -> Config:
    cfg = Config(vault_dir=tmp_path)
    cfg.llm.lookback_days = 0
    return cfg


def _note_with_checked_action(cfg: Config) -> DailyNoteStore:
    store = DailyNoteStore(cfg.daily_notes_path)
    store.write_section(DAY, ACTIVITY_MARKER, "## 📊 Activity Log\n\n**合計**: 10m")
    store.write_section(
        DAY,
        ADVICE_MARKER,
        "## 🚀 Kaizen\n\n### 明日の最小アクション\n"
        "- [x] KZN-20260721-001: 既存アクション",
    )
    append_entries(
        cfg.memory_path,
        [MemoryEntry(id="KZN-20260721-001", date=DAY.isoformat(), action="既存アクション")],
    )
    return store


def test_failed_generation_still_persists_user_completed_status(monkeypatch, tmp_path):
    cfg = _config(tmp_path)
    _note_with_checked_action(cfg)
    memory_file = cfg.memory_path / "suggestions.jsonl"

    def fail(*args, **kwargs):
        raise AdvisorError("generation failed")

    monkeypatch.setattr("kaizenlog.cli.generate_advice", fail)
    with pytest.raises(AdvisorError, match="generation failed"):
        cmd_advise(cfg, DAY)

    assert load_entries(cfg.memory_path)[0].status == "done"


def test_success_commits_status_and_new_actions_together(monkeypatch, tmp_path):
    cfg = _config(tmp_path)
    store = _note_with_checked_action(cfg)
    monkeypatch.setattr("kaizenlog.cli.generate_advice", lambda *args, **kwargs: VALID_GENERATED)

    cmd_advise(cfg, DAY)

    entries = {entry.id: entry for entry in load_entries(cfg.memory_path)}
    assert entries["KZN-20260721-001"].status == "done"
    assert entries["KZN-20260721-002"].status == "proposed"
    assert entries["KZN-20260721-003"].status == "proposed"
    note = store.read(DAY) or ""
    assert "KZN-20260721-002: [F3]" in note
    assert "KZN-20260721-003: [F9]" in note


def test_note_write_failure_persists_completion_but_not_new_actions(monkeypatch, tmp_path):
    cfg = _config(tmp_path)
    _note_with_checked_action(cfg)
    monkeypatch.setattr("kaizenlog.cli.generate_advice", lambda *args, **kwargs: VALID_GENERATED)

    def fail_write(*args, **kwargs):
        raise OSError("note write failed")

    monkeypatch.setattr("kaizenlog.cli.DailyNoteStore.write_section", fail_write)
    with pytest.raises(OSError, match="note write failed"):
        cmd_advise(cfg, DAY)

    entries = load_entries(cfg.memory_path)
    assert len(entries) == 1
    assert entries[0].id == "KZN-20260721-001"
    assert entries[0].status == "done"


def test_cmd_advise_passes_current_and_baseline_evidence(monkeypatch, tmp_path):
    cfg = _config(tmp_path)
    cfg.llm.lookback_days = 3
    store = DailyNoteStore(cfg.daily_notes_path)
    store.write_section(DAY, ACTIVITY_MARKER, "## 📊 Activity Log\n\n**合計**: 2h")
    cfg.stats_path.mkdir(parents=True)

    def write_stats(day, switches, ai_blocks):
        payload = {
            "version": 1,
            "day": day.isoformat(),
            "total_minutes": 120.0,
            "context_switches": switches,
            "ai_activity_blocks": ai_blocks,
            "by_category": {"開発": 90.0, "AI作業": 30.0},
            "by_app": {"Code.exe": 90.0, "chrome.exe": 30.0},
            "by_site": {},
            "blocks": [{"start": f"{day.isoformat()}T09:00:00+09:00", "category": "開発"}],
            "ai": {"sessions": 0},
        }
        if day == DAY:
            payload["activity_sha256"] = activity_fingerprint(
                "## 📊 Activity Log\n\n**合計**: 2h"
            )
        (cfg.stats_path / f"{day.isoformat()}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    for offset in (1, 2, 3):
        write_stats(DAY - timedelta(days=offset), switches=10, ai_blocks=1)
    write_stats(DAY, switches=20, ai_blocks=5)

    captured = {}

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return VALID_GENERATED

    monkeypatch.setattr("kaizenlog.cli.generate_advice", spy)
    cmd_advise(cfg, DAY)

    evidence = captured["evidence"].markdown
    assert "合計アクティブ時間 120分" in evidence
    assert "5ブロック" in evidence
    assert "比較可能な過去3日中央値" in evidence
    assert "指紋は一致済み" in evidence


def test_cmd_advise_downgrades_mismatched_stats(monkeypatch, tmp_path):
    cfg = _config(tmp_path)
    store = DailyNoteStore(cfg.daily_notes_path)
    store.write_section(DAY, ACTIVITY_MARKER, "## 📊 Activity Log\n\nnew activity")
    cfg.stats_path.mkdir(parents=True)
    payload = {
        "version": 1,
        "day": DAY.isoformat(),
        "total_minutes": 999.0,
        "context_switches": 999,
        "activity_sha256": activity_fingerprint("old activity"),
    }
    (cfg.stats_path / f"{DAY.isoformat()}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    captured = {}

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return VALID_GENERATED

    monkeypatch.setattr("kaizenlog.cli.generate_advice", spy)
    cmd_advise(cfg, DAY)

    evidence = captured["evidence"].markdown
    assert "指紋が不一致" in evidence
    assert "[F0]" in evidence
    assert "合計アクティブ時間 999分" not in evidence


def test_dry_run_redacts_system_and_user_and_writes_nothing(capsys, tmp_path):
    cfg = _config(tmp_path)
    store = DailyNoteStore(cfg.daily_notes_path)
    store.write_section(DAY, ACTIVITY_MARKER, "## 📊 Activity Log\n\nSECRET user")
    custom_prompt = tmp_path / "system-prompt.md"
    custom_prompt.write_text("SECRET system", encoding="utf-8")
    cfg.llm.system_prompt = str(custom_prompt)
    cfg.privacy.redact_patterns = ["SECRET"]
    cfg.privacy.replacement = "[MASKED]"
    before = store.read(DAY)

    assert cmd_advise(cfg, DAY, dry_run=True) is None

    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert output.count("[MASKED]") == 2
    assert store.read(DAY) == before
    assert not (cfg.memory_path / "suggestions.jsonl").exists()
