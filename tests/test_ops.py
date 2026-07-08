from datetime import date, datetime, timedelta, timezone

import pytest

from kaizenlog.advisor import AdvisorError, generate_text
from kaizenlog.config import LLMConfig
from kaizenlog.runlog import load_runs, log_run, render_status
from kaizenlog.stats import missing_days

NOW = datetime(2026, 7, 6, 21, 30, tzinfo=timezone.utc)


# ---- 実行ログ ----

def test_log_run_and_status(tmp_path):
    log_run(tmp_path, "run", ok=True, duration_seconds=12.3, now=NOW)
    log_run(tmp_path, "run", ok=False, duration_seconds=1.0,
            error="ActivityWatchに接続できません", now=NOW + timedelta(days=1))

    runs = load_runs(tmp_path)
    assert len(runs) == 2
    status = render_status(runs)
    assert "## run" in status
    assert "❌" in status
    assert "最後に成功: 2026-07-0" in status
    assert "ActivityWatchに接続できません" in status


def test_log_run_prunes_old_entries(tmp_path):
    log_run(tmp_path, "run", ok=True, duration_seconds=1.0,
            now=NOW - timedelta(days=200))
    log_run(tmp_path, "run", ok=True, duration_seconds=1.0,
            now=NOW, retention_days=90)
    runs = load_runs(tmp_path)
    assert len(runs) == 1  # 200日前の記録は間引かれる
    assert runs[0]["ts"] == NOW.isoformat()


def test_render_status_empty():
    assert "実行履歴はまだありません" in render_status([])


def test_load_runs_skips_broken_lines(tmp_path):
    (tmp_path / "runs.jsonl").write_text('{"ts": "2026-07-06T00:00:00+00:00", "command": "run", "ok": true}\n{broken\n', encoding="utf-8")
    assert len(load_runs(tmp_path)) == 1


# ---- LLMリトライ ----

def _cfg(backend="copilot-cli", retries=2):
    return LLMConfig(backend=backend, retries=retries, retry_wait_seconds=1)


def test_generate_text_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky(cfg, system, user):
        calls["n"] += 1
        if calls["n"] < 3:
            raise AdvisorError("一時エラー")
        return "成功しました"

    monkeypatch.setattr("kaizenlog.advisor._call_copilot_cli", flaky)
    sleeps = []
    result = generate_text(_cfg(), "sys", "user", sleep=sleeps.append)
    assert result == "成功しました"
    assert calls["n"] == 3
    assert sleeps == [1, 1]  # 2回リトライ待ちした


def test_generate_text_raises_after_all_retries(monkeypatch):
    def always_fail(cfg, system, user):
        raise AdvisorError("恒久エラー")

    monkeypatch.setattr("kaizenlog.advisor._call_copilot_cli", always_fail)
    with pytest.raises(AdvisorError, match="恒久エラー"):
        generate_text(_cfg(retries=1), "sys", "user", sleep=lambda s: None)


def test_generate_text_backend_none_does_not_retry():
    sleeps = []
    with pytest.raises(AdvisorError, match="none"):
        generate_text(_cfg(backend="none"), "sys", "user", sleep=sleeps.append)
    assert sleeps == []  # 設定エラーはリトライしない


# ---- 欠損日検出 ----

def test_missing_days(tmp_path):
    end = date(2026, 7, 6)
    (tmp_path / "2026-07-04.json").write_text("{}", encoding="utf-8")
    result = missing_days(tmp_path, end, lookback=3)
    assert result == [date(2026, 7, 3), date(2026, 7, 5)]  # 当日(7/6)は含まない


def test_missing_days_empty_dir(tmp_path):
    end = date(2026, 7, 6)
    result = missing_days(tmp_path / "nonexistent", end, lookback=2)
    assert result == [date(2026, 7, 4), date(2026, 7, 5)]
