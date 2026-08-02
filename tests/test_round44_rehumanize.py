"""第44弾: 過去ノート遡及平文化 (rehumanize) + digest 切詰め統一。"""
from __future__ import annotations

import shutil
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pytest

from kaizenlog.cli import cmd_rehumanize
from kaizenlog.config import Config
from kaizenlog.digest import build_digest
from kaizenlog.memory import (
    MemoryEntry,
    humanize_actions_section_markdown,
    humanize_advice_markdown_actions,
)
from kaizenlog.vault import (
    ACTIONS_MARKER,
    ACTIVITY_MARKER,
    ADVICE_MARKER,
    DailyNoteStore,
    extract_section,
    upsert_section,
)

TZ = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 7, 28)


def _cfg(vault: Path) -> Config:
    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        experiments_dir="03 Areas/Kaizen Experiments",
        stats_dir=".kaizenlog/stats",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
    )
    return cfg


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "01 Daily Notes").mkdir(parents=True)
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    return vault


def _note_with_sections(
    *,
    advice: str | None,
    actions: str | None = None,
    handwritten: str = "KEEP_HANDWRITTEN_BYTE_SAFE",
    activity: str = "### 📊 Activity Log\n\n合計 1h\n",
) -> str:
    body = (
        "---\n"
        f"date: {DAY.isoformat()}\n"
        "tags: [type/daily]\n"
        "---\n\n"
        f"{handwritten}\n\n"
    )
    if activity is not None:
        body = upsert_section(body, ACTIVITY_MARKER, activity)
    if advice is not None:
        body = upsert_section(body, ADVICE_MARKER, advice)
    if actions is not None:
        body = upsert_section(body, ACTIONS_MARKER, actions)
    return body


MACHINE_ADVICE = """\
## 🚀 Kaizen（AIからの改善提案）

### 今日の結論

作業がありました。

### 明日試すこと

- [ ] KZN-20260727-001: 終了するとき→git diff を実行する｜PASS: ai_retry_chains <= 0（リトライ連鎖数）｜FAIL: ai_retry_chains >= 1
    - なぜ効くと考えるか: 確認できる
    - 効かなかったと分かる条件: 連鎖が続く
"""

MACHINE_ACTIONS = """\
## 📌 今日のアクション
前日までの改善提案の未完了アクション。完了したらチェック
今週の消化 0件（提案 9件） / 実行済みPASS 0件（未実行のままPASS到達 4件：チェックなしで指標が目標値に達した提案）
- [ ] KZN-20260801-001: codexのセッションを終了するとき→git diff --stat を実行する｜PASS: ai_retry_chains <= 0（リトライ連鎖数）｜FAIL: ai_retry_chains >= 1（8/1提案・判定 ⏳ 集計中・途中値0・8/2の日締め後に確定）
  └ 判定後の実測: 8/2 2 ❌
全件表示: `kaizenlog today --all`
"""


def _write_day(vault: Path, day: date, content: str) -> Path:
    p = vault / "01 Daily Notes" / f"{day.isoformat()}.md"
    p.write_text(content, encoding="utf-8", newline="\n")
    return p


# ---------- helpers unit ----------


def test_g2_actions_checkbox_keeps_verdict_tag():
    out = humanize_actions_section_markdown(MACHINE_ACTIONS)
    assert "｜PASS:" not in out
    assert "｜FAIL:" not in out
    assert "効果指標: リトライ連鎖数 を 0 以下 に（8/1提案・判定 ⏳ 集計中・途中値0・8/2の日締め後に確定）" in out
    assert "└ 判定後の実測:" in out
    assert "全件表示:" in out


def test_g2_actions_summary_plain():
    out = humanize_actions_section_markdown(MACHINE_ACTIONS)
    assert "今週は9件提案し、チェック完了は0件。" in out
    assert "うち4件はチェックなしで指標が目標に達しています" in out
    assert "消化" not in out
    assert "実行済みPASS" not in out


def test_g2_unconvertible_line_kept():
    weird = (
        "## 📌\n"
        "- [ ] KZN-20260701-001: 壊れた行｜PASS: not_a_real_metric_xyz <= 1｜FAIL: 2\n"
        "- 想定外の行です\n"
    )
    out = humanize_actions_section_markdown(weird)
    # 未知メトリクスでも annotation 無しなら format_effect が生名フォールバックで変換し得る
    # 完全に壊れた行（PASS なし）は残る
    assert "想定外の行です" in out
    # PASS の中身が空＝解析不能なチェックボックス行は 2 行化せず原文のまま残す
    # （末尾改行は区間書き込み時に upsert_section が正規化するため比較対象外）
    broken = "## 📌\n- [ ] 壊れた行｜PASS:\nほか 1件"
    assert humanize_actions_section_markdown(broken) == broken


# ---------- cmd_rehumanize ----------


def test_g1_dry_run_no_write_mtime(tmp_path, capsys):
    vault = _vault(tmp_path)
    content = _note_with_sections(advice=MACHINE_ADVICE, actions=MACHINE_ACTIONS)
    path = _write_day(vault, DAY, content)
    mtime_before = path.stat().st_mtime_ns
    raw_before = path.read_bytes()

    rc = cmd_rehumanize(_cfg(vault), days=1, only_date=DAY, write=False, as_of=DAY)
    assert rc == 0
    assert path.read_bytes() == raw_before
    assert path.stat().st_mtime_ns == mtime_before
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "変更あり" in out
    assert "before:" in out or "代表例" in out


def test_g1_write_humanizes_advice(tmp_path):
    vault = _vault(tmp_path)
    content = _note_with_sections(advice=MACHINE_ADVICE)
    _write_day(vault, DAY, content)
    rc = cmd_rehumanize(_cfg(vault), only_date=DAY, write=True, as_of=DAY)
    assert rc == 0
    after = (_vault_day := vault / "01 Daily Notes" / f"{DAY.isoformat()}.md").read_text(
        encoding="utf-8"
    )
    sec = extract_section(after, ADVICE_MARKER)
    assert sec is not None
    assert "｜PASS:" not in sec
    assert "｜FAIL:" not in sec
    assert "効果指標:" in sec
    assert "なぜ効くと考えるか:" in sec


def test_g1_outside_section_byte_invariant(tmp_path):
    vault = _vault(tmp_path)
    handwritten = "KEEP_HANDWRITTEN_UNIQUE_xyz"
    content = _note_with_sections(
        advice=MACHINE_ADVICE,
        actions=MACHINE_ACTIONS,
        handwritten=handwritten,
        activity="### 📊 Activity Log\n\nACTIVITY_MARKER_BODY_99\n",
    )
    path = _write_day(vault, DAY, content)
    before = path.read_text(encoding="utf-8")

    def _outside(text: str) -> str:
        # strip advice and actions sections for comparison
        for marker in (ADVICE_MARKER, ACTIONS_MARKER):
            start = f"<!-- {marker}:start -->"
            end = f"<!-- {marker}:end -->"
            si, ei = text.find(start), text.find(end)
            if si >= 0 and ei > si:
                text = text[:si] + text[ei + len(end) :]
        return text

    cmd_rehumanize(_cfg(vault), only_date=DAY, write=True, as_of=DAY)
    after = path.read_text(encoding="utf-8")
    assert handwritten in after
    assert "ACTIVITY_MARKER_BODY_99" in after
    assert _outside(before) == _outside(after)


def test_g1_idempotent_second_write(tmp_path, capsys):
    vault = _vault(tmp_path)
    _write_day(vault, DAY, _note_with_sections(advice=MACHINE_ADVICE, actions=MACHINE_ACTIONS))
    cmd_rehumanize(_cfg(vault), only_date=DAY, write=True, as_of=DAY)
    capsys.readouterr()
    rc = cmd_rehumanize(_cfg(vault), only_date=DAY, write=True, as_of=DAY)
    assert rc == 0
    out = capsys.readouterr().out
    assert "変更あり 0件" in out


def test_g1_no_advice_section_unchanged(tmp_path, capsys):
    vault = _vault(tmp_path)
    body = "---\ndate: 2026-07-28\n---\n\nonly hand\n"
    _write_day(vault, DAY, body)
    before = (vault / "01 Daily Notes" / f"{DAY.isoformat()}.md").read_bytes()
    cmd_rehumanize(_cfg(vault), only_date=DAY, write=True, as_of=DAY)
    out = capsys.readouterr().out
    assert "変更あり 0件" in out
    assert "変更なし 1件" in out
    # マーカーが無いノートは 1 バイトも触らない
    assert (vault / "01 Daily Notes" / f"{DAY.isoformat()}.md").read_bytes() == before


def test_g1_already_plain_unchanged(tmp_path, capsys):
    vault = _vault(tmp_path)
    plain = humanize_advice_markdown_actions(MACHINE_ADVICE)
    _write_day(vault, DAY, _note_with_sections(advice=plain))
    cmd_rehumanize(_cfg(vault), only_date=DAY, write=False, as_of=DAY)
    out = capsys.readouterr().out
    assert "変更あり 0件" in out


def test_g2_actions_write_keeps_tag(tmp_path):
    vault = _vault(tmp_path)
    _write_day(vault, DAY, _note_with_sections(advice=None, actions=MACHINE_ACTIONS))
    # advice=None still has activity; write actions only
    content = (
        "---\ndate: 2026-07-28\n---\n\n"
        + f"<!-- {ACTIONS_MARKER}:start -->\n{MACHINE_ACTIONS}\n<!-- {ACTIONS_MARKER}:end -->\n"
    )
    path = _write_day(vault, DAY, content)
    cmd_rehumanize(_cfg(vault), only_date=DAY, write=True, as_of=DAY)
    sec = extract_section(path.read_text(encoding="utf-8"), ACTIONS_MARKER)
    assert sec is not None
    assert "8/1提案・判定" in sec
    assert "効果指標:" in sec
    assert "｜PASS:" not in sec


def test_g4_date_and_days_selection(tmp_path, capsys):
    vault = _vault(tmp_path)
    d1 = date(2026, 7, 28)
    d2 = date(2026, 7, 27)
    _write_day(vault, d1, _note_with_sections(advice=MACHINE_ADVICE))
    # d2 note without machine syntax
    _write_day(
        vault,
        d2,
        _note_with_sections(
            advice="### 明日試すこと\n\n- [ ] 自由文のみ\n",
        ),
    )
    # only_date
    capsys.readouterr()
    cmd_rehumanize(_cfg(vault), only_date=d1, write=False, as_of=d1)
    out = capsys.readouterr().out
    assert "対象 1件" in out
    assert "変更あり 1件" in out

    # days=2 from d1
    cmd_rehumanize(_cfg(vault), days=2, write=False, as_of=d1)
    out = capsys.readouterr().out
    assert "対象 2件" in out


def test_g4_backup_created_and_fail_skips_write(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    path = _write_day(
        vault, DAY, _note_with_sections(advice=MACHINE_ADVICE, actions=MACHINE_ACTIONS)
    )
    before = path.read_text(encoding="utf-8")
    cmd_rehumanize(_cfg(vault), only_date=DAY, write=True, as_of=DAY)
    # backup exists under .kaizenlog/backup/rehumanize/
    bak_root = vault / ".kaizenlog" / "backup" / "rehumanize"
    assert bak_root.is_dir()
    backups = list(bak_root.rglob(f"{DAY.isoformat()}.md"))
    assert backups, "backup file missing"
    assert "｜PASS:" in backups[0].read_text(encoding="utf-8")

    # restore machine content and force backup failure
    path.write_text(before, encoding="utf-8")

    def boom(*a, **k):
        raise OSError("no backup")

    monkeypatch.setattr(shutil, "copy2", boom)
    capsys.readouterr()
    cmd_rehumanize(_cfg(vault), only_date=DAY, write=True, as_of=DAY)
    out = capsys.readouterr().out
    assert "バックアップ失敗" in out
    # still machine syntax
    assert "｜PASS:" in path.read_text(encoding="utf-8")


# ---------- §G3 digest ----------


def test_g3_digest_snippet_boundary():
    body39 = "あ" * 39
    body40 = "あ" * 40
    body41 = "あ" * 41

    def _digest(action: str) -> str:
        entries = [
            MemoryEntry(
                id="KZN-20260801-001",
                date="2026-08-01",
                action=action,
                status="proposed",
            )
        ]
        stats = {
            "source_status": "verified",
            "activity_sha256": "x",
            "total_minutes": 100.0,
            "by_category": {"開発": 100.0},
        }
        body = build_digest(
            stats,
            entries,
            today=date(2026, 8, 1),
            redactor=lambda s: s,
        )
        assert body is not None
        return body

    d39 = _digest(body39)
    d40 = _digest(body40)
    d41 = _digest(body41)
    line39 = next(ln for ln in d39.splitlines() if "今日の1手" in ln)
    line40 = next(ln for ln in d40.splitlines() if "今日の1手" in ln)
    line41 = next(ln for ln in d41.splitlines() if "今日の1手" in ln)
    # 本文部分（ID の後）
    sn39 = line39.split("KZN-20260801-001 ", 1)[1]
    sn40 = line40.split("KZN-20260801-001 ", 1)[1]
    sn41 = line41.split("KZN-20260801-001 ", 1)[1]
    assert sn39 == body39
    assert sn40 == body40
    assert sn41.endswith("…")
    assert len(sn41) == 40
    assert sn41[:39] == body41[:39]
