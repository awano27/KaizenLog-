"""第13弾: AIテレメトリ stats v2 と weekly-context --write。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.aiwork import AISession
from kaizenlog.classifier import Classifier
from kaizenlog.config import Config, DEFAULT_RULES
from kaizenlog.experiments import (
    baseline_median_from_stats,
    compute_metric,
    create_experiment,
    load_experiments,
    metric_from_stats,
    record_measurement,
)
from kaizenlog.report import summarize
from kaizenlog.runlog import load_runs
from kaizenlog.stats import build_stats, write_stats
from kaizenlog.vault import WEEKLY_CONTEXT_MARKER, extract_section, upsert_section
from kaizenlog.weekly_context import (
    expired_recommendation,
    format_ai_tokens_week_line,
    monday_of,
    render_weekly_context,
    write_weekly_context,
)


TZ = ZoneInfo("Asia/Tokyo")


def _summary(day: date | None = None):
    classified = Classifier(DEFAULT_RULES).classify_all([])
    return summarize(day or date(2026, 7, 21), classified)


def _session(**kw) -> AISession:
    start = datetime(2026, 7, 21, 9, tzinfo=TZ)
    defaults = dict(
        session_id="s1",
        project="demo-app",
        start=start,
        end=start + timedelta(hours=1),
        user_turns=4,
        output_tokens=1000,
        api_calls=2,
    )
    defaults.update(kw)
    s = AISession(**{k: v for k, v in defaults.items() if k not in ("tool_counts", "models")})
    if "tool_counts" in kw:
        s.tool_counts.update(kw["tool_counts"])
    if "models" in kw:
        s.models.update(kw["models"])
    return s


# ---- W1 --------------------------------------------------------------------


def test_w1_build_stats_v2_keys():
    sessions = [
        _session(
            session_id="a",
            user_turns=2,
            output_tokens=500,
            api_calls=1,
            tool_counts={"Read": 3, "Edit": 1},
            models={"model-a"},
        ),
        _session(
            session_id="b",
            user_turns=6,
            output_tokens=1500,
            api_calls=3,
            tool_counts={"Read": 2, "Bash": 5},
            models={"model-b"},
        ),
    ]
    s = build_stats(date(2026, 7, 21), _summary(), sessions)
    assert s["version"] == 2
    ai = s["ai"]
    assert ai["turns_total"] == 8
    assert ai["avg_turns"] == 4.0
    assert ai["output_tokens"] == 2000
    assert ai["api_calls"] == 4
    assert ai["tool_counts"]["Bash"] == 5
    assert ai["tool_counts"]["Read"] == 5
    assert set(ai["models"]) == {"model-a", "model-b"}
    # 既存キー維持
    assert ai["sessions"] == 2
    assert "projects" in ai


def test_w1_v1_stats_still_readable():
    v1 = {
        "version": 1,
        "day": "2026-07-20",
        "total_minutes": 100.0,
        "context_switches": 10,
        "ai_activity_blocks": 2,
        "by_category": {"開発": 50.0},
        "ai": {
            "sessions": 2,
            "fragmented": 0,
            "tool_errors": 0,
            "interruptions": 0,
            "projects": {
                "p": {"sessions": 2, "turns": 10, "errors": 0, "fragmented": 0}
            },
        },
    }
    assert metric_from_stats("context_switches", v1) == 10.0
    assert metric_from_stats("ai_cc_sessions", v1) == 2.0
    # v1 近似: turns/sessions
    assert metric_from_stats("ai_avg_turns", v1) == 5.0
    assert metric_from_stats("ai_output_tokens", v1) is None


def test_w1_ai_avg_turns_three_paths():
    # path1: avg_turns
    assert metric_from_stats(
        "ai_avg_turns", {"ai": {"avg_turns": 3.5, "sessions": 2}}
    ) == 3.5
    # path2: turns_total / sessions
    assert metric_from_stats(
        "ai_avg_turns",
        {"ai": {"turns_total": 9, "sessions": 3}},
    ) == 3.0
    # path3: v1 projects
    assert metric_from_stats(
        "ai_avg_turns",
        {
            "ai": {
                "sessions": 2,
                "projects": {
                    "a": {"turns": 4},
                    "b": {"turns": 6},
                },
            }
        },
    ) == 5.0
    # empty
    assert metric_from_stats("ai_avg_turns", {"ai": {"sessions": 0}}) == 0.0
    assert metric_from_stats("ai_avg_turns", {"ai": {}}) is None


def test_w1_baseline_median_ai_avg_turns():
    stats_list = [
        {"ai": {"avg_turns": 4.0, "sessions": 2}},
        {"ai": {"avg_turns": 6.0, "sessions": 2}},
        {"ai": {"avg_turns": 5.0, "sessions": 2}},
    ]
    assert baseline_median_from_stats(stats_list, "ai_avg_turns", min_days=3) == 5.0


def test_w1_ai_output_tokens_compute_and_from_stats():
    sessions = [_session(output_tokens=100), _session(session_id="x", output_tokens=50)]
    assert compute_metric("ai_output_tokens", _summary(), sessions) == 150.0
    s = build_stats(date(2026, 7, 21), _summary(), sessions)
    assert metric_from_stats("ai_output_tokens", s) == 150.0


# ---- W2 --------------------------------------------------------------------


def test_w2_write_upsert_idempotent_and_preserves_outside(tmp_path):
    daily = tmp_path / "daily"
    week_start = date(2026, 7, 20)  # Monday
    body1 = "# 週次コンテキスト\n\n- v1\n"
    p = write_weekly_context(daily, body1, week_start)
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert "kaizenlog:weekly-context:start" in text
    # マーカー外に考察を追加
    text2 = text + "\n## 考察\n手書きの解釈\n"
    p.write_text(text2, encoding="utf-8")
    body2 = "# 週次コンテキスト\n\n- v2\n"
    write_weekly_context(daily, body2, week_start)
    final = p.read_text(encoding="utf-8")
    assert "手書きの解釈" in final
    sec = extract_section(final, WEEKLY_CONTEXT_MARKER)
    assert sec is not None
    assert "- v2" in sec
    assert "- v1" not in sec
    # 冪等
    write_weekly_context(daily, body2, week_start)
    final2 = p.read_text(encoding="utf-8")
    assert final2.count("kaizenlog:weekly-context:start") == 1
    assert "手書きの解釈" in final2


def test_w2_write_creates_new_note(tmp_path):
    daily = tmp_path / "notes"
    week_start = monday_of(date(2026, 7, 22))
    p = write_weekly_context(daily, "# ctx\n", week_start)
    assert p.exists()
    assert "Weekly Reviews" in p.parts
    assert "type/weekly-review" in p.read_text(encoding="utf-8")


def test_w2_expired_recommendation_labels():
    assert expired_recommendation(0, 0) == "実測不足"
    assert "採用" in expired_recommendation(3, 4)
    assert "棄却" in expired_recommendation(1, 4)


def test_w2_expired_in_render_with_recommendation(tmp_path):
    stats = tmp_path / "stats"
    mem = tmp_path / "mem"
    exp = tmp_path / "exp"
    stats.mkdir()
    mem.mkdir()
    path = create_experiment(
        exp,
        "E1",
        "context_switches",
        "<= 40",
        today=date(2026, 7, 1),
        deadline=date(2026, 7, 10),
        baseline=50.0,
    )
    e = load_experiments(exp)[0]
    # force expired + measurements
    from kaizenlog.experiments import _set_frontmatter_field
    from kaizenlog.vault import atomic_write_text

    content = path.read_text(encoding="utf-8")
    content = _set_frontmatter_field(content, "status", "expired")
    atomic_write_text(path, content)
    e = load_experiments(exp)[0]
    record_measurement(e, date(2026, 7, 5), 30.0)  # met
    e = load_experiments(exp)[0]
    record_measurement(e, date(2026, 7, 6), 20.0)  # met
    e = load_experiments(exp)[0]
    record_measurement(e, date(2026, 7, 7), 50.0)  # miss
    md = render_weekly_context(stats, mem, exp, date(2026, 7, 20))
    assert "expired" in md
    assert "採用推奨" in md or "棄却推奨" in md


def test_w2_token_week_line_v1_mix_and_all_missing():
    assert format_ai_tokens_week_line([]) == "AIトークン: -"
    assert format_ai_tokens_week_line([{"ai": {}}]) == "AIトークン: -"
    mixed = [
        {"ai": {"output_tokens": 1000}},
        {"ai": {}},  # v1 — 除外
        {"ai": {"output_tokens": 3000}},
    ]
    line = format_ai_tokens_week_line(mixed)
    assert "週計" in line
    assert "2日分" in line
    assert "4k" in line or "4000" in line or "2.0k" in line or "日平均" in line


def test_w2_cli_write_logs(tmp_path, monkeypatch, capsys):
    from kaizenlog import cli as cli_mod

    vault = tmp_path / "v"
    daily = vault / "01 Daily Notes"
    daily.mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / ".kaizenlog" / "logs").mkdir(parents=True)
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        stats_dir=".kaizenlog/stats",
        logs_dir=".kaizenlog/logs",
        memory_dir="Kaizen/Memory",
    )
    # write config for main path optional — call write path via command handler pieces
    week = date(2026, 7, 20)
    body = render_weekly_context(
        cfg.stats_path, cfg.memory_path, cfg.experiments_path, week
    )
    p = write_weekly_context(cfg.daily_notes_path, body, week)
    assert p.is_file()

    # simulate log_run as cmd would
    from kaizenlog.runlog import log_run

    log_run(cfg.logs_path, "weekly-context", ok=True, duration_seconds=0.1, note="write")
    runs = load_runs(cfg.logs_path)
    assert any(r.get("command") == "weekly-context" and r.get("ok") for r in runs)


def _isolate_cfg(monkeypatch, tmp_path: Path, vault: Path) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAIZENLOG_CONFIG", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr("kaizenlog.config.sys.platform", "win32")
    cfg_path = tmp_path / "AppData" / "kaizenlog" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        f'[general]\nvault_dir = "{vault.as_posix()}"\ntimezone = "Asia/Tokyo"\n'
        f'daily_notes_dir = "01 Daily Notes"\n'
        f'stats_dir = ".kaizenlog/stats"\n'
        f'logs_dir = ".kaizenlog/logs"\n'
        f'memory_dir = "Kaizen/Memory"\n',
        encoding="utf-8",
    )
    return cfg_path


def test_w2_main_weekly_context_write_success(tmp_path, monkeypatch, capsys):
    """CLI main 経路: --write 成功でノート upsert + runlog ok。"""
    from kaizenlog import cli as cli_mod
    from kaizenlog.vault import WEEKLY_CONTEXT_MARKER, extract_section

    vault = tmp_path / "vault"
    (vault / "01 Daily Notes").mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / ".kaizenlog" / "logs").mkdir(parents=True)
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    _isolate_cfg(monkeypatch, tmp_path, vault)

    code = cli_mod.main(
        ["weekly-context", "--write", "--week", "2026-W30"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "週次コンテキスト" in out or "書き込み" in out
    # 2026-W30 = week of 2026-07-20
    note = vault / "01 Daily Notes" / "Weekly Reviews" / "2026-W30.md"
    assert note.is_file()
    text = note.read_text(encoding="utf-8")
    sec = extract_section(text, WEEKLY_CONTEXT_MARKER)
    assert sec is not None
    runs = load_runs(vault / ".kaizenlog" / "logs")
    assert any(
        r.get("command") == "weekly-context" and r.get("ok") is True for r in runs
    )


def test_w2_main_weekly_context_write_failure_continues(
    tmp_path, monkeypatch, capsys
):
    """CLI main 経路: 書込失敗でも exit 0 + stderr 警告 + runlog 失敗。"""
    from kaizenlog import cli as cli_mod

    vault = tmp_path / "vault"
    (vault / "01 Daily Notes").mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / ".kaizenlog" / "logs").mkdir(parents=True)
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    _isolate_cfg(monkeypatch, tmp_path, vault)

    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(
        "kaizenlog.weekly_context.write_weekly_context", boom
    )
    code = cli_mod.main(
        ["weekly-context", "--write", "--week", "2026-W30"]
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "失敗" in err or "permission" in err.lower() or "書き込み" in err
    runs = load_runs(vault / ".kaizenlog" / "logs")
    assert any(
        r.get("command") == "weekly-context" and r.get("ok") is False for r in runs
    )


def test_s1_ai_output_tokens_gate():
    """構造化AI無しでは ai_output_tokens PASS は契約違反。有りでは通過。"""
    from kaizenlog.advice_evidence import build_advice_evidence
    from kaizenlog.advice_format import validate_advice
    from kaizenlog.classifier import known_category_names
    from kaizenlog.config import DEFAULT_RULES
    from tests.test_advice_evidence import CURRENT, HISTORY
    from tests.test_advice_format import _valid_data

    cats = known_category_names(DEFAULT_RULES)
    # without ai section
    stats_no = dict(CURRENT)
    stats_no.pop("ai", None)
    ev_no = build_advice_evidence(stats_no, HISTORY, known_categories=cats)
    assert ev_no.structured_ai_metrics_available is False
    data = _valid_data()
    data["actions"] = [data["actions"][0]]
    data["proposals"] = [data["proposals"][0]]
    data["actions"][0]["pass"] = "ai_output_tokens <= 5000"
    data["actions"][0]["fail"] = "5001以上"
    data["actions"][0]["fact_ids"] = data["proposals"][0]["fact_ids"]
    errs = validate_advice(data, ev_no)
    assert any("ai_output_tokens" in e and "計測不能" in e for e in errs)

    # with ai section (CURRENT has ai)
    ev_yes = build_advice_evidence(CURRENT, HISTORY, known_categories=cats)
    assert ev_yes.structured_ai_metrics_available is True
    assert validate_advice(data, ev_yes) == []
