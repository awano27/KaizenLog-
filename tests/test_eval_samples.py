"""第11弾補完: eval/samples 同梱とフォールバック。"""
from __future__ import annotations

from pathlib import Path

from kaizenlog.config import Config
from kaizenlog.evalharness import (
    load_case,
    load_cases_dir,
    package_samples_dir,
    repo_eval_samples_dir,
    resolve_eval_cases_dir,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_bundled_samples_three_cases_loadable():
    samples = _repo_root() / "eval" / "samples"
    assert samples.is_dir(), "eval/samples/ must exist in the repository"
    paths = sorted(samples.glob("*.json"))
    assert len(paths) >= 3
    cases = load_cases_dir(samples)
    assert len(cases) >= 3
    by_id = {c.id: c for c in cases}
    # case1: 標準日 — 日本語カテゴリと AI テレメトリ
    c1 = next(c for c in cases if "standard" in c.id or c.id.startswith("case1"))
    assert c1.current_stats is not None
    cats = c1.current_stats.get("by_category") or {}
    assert "AI作業" in cats
    assert "ブラウジング" in cats
    assert isinstance((c1.current_stats.get("ai") or {}).get("sessions"), (int, float))
    # case2: 薄い日 — 統計欠落
    c2 = next(c for c in cases if "thin" in c.id or c.id.startswith("case2"))
    assert c2.current_stats is None or c2.source_status == "missing"
    # case3: 文脈あり
    c3 = next(c for c in cases if "context" in c.id or c.id.startswith("case3"))
    assert c3.intent
    assert c3.experiments_ctx
    assert c3.memory_ctx
    # record 形式互換: ラウンドトリップ
    for p in paths:
        again = load_case(p)
        assert again.id
        assert again.day
        assert again.schema_version >= 1


def test_resolve_falls_back_to_samples_when_no_user_cases(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = Config(vault_dir=vault, timezone="Asia/Tokyo")
    # CWD に eval/cases を置かない
    monkeypatch.chdir(tmp_path)
    path, used = resolve_eval_cases_dir(cfg, explicit=None)
    assert used is True
    assert load_cases_dir(path), f"fallback dir empty: {path}"
    # repo samples か package のどちらか
    assert path == package_samples_dir() or "samples" in path.as_posix()


def test_resolve_prefers_user_cases(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    cases = vault / ".kaizenlog" / "eval" / "cases"
    cases.mkdir(parents=True)
    # 最小ケース
    (cases / "mine.json").write_text(
        '{"id":"mine","day":"2026-01-01","current_stats":null,'
        '"prior_stats":[],"today_md":"x","schema_version":1}',
        encoding="utf-8",
    )
    cfg = Config(vault_dir=vault, timezone="Asia/Tokyo")
    monkeypatch.chdir(tmp_path)
    path, used = resolve_eval_cases_dir(cfg, explicit=None)
    assert used is False
    assert path == cases
    loaded = load_cases_dir(path)
    assert len(loaded) == 1
    assert loaded[0].id == "mine"


def test_cmd_eval_run_prints_fallback_message(tmp_path, monkeypatch, capsys):
    import kaizenlog.cli as cli_mod
    from kaizenlog.advisor import PipelineReport

    vault = tmp_path / "v"
    vault.mkdir()
    cfg = Config(vault_dir=vault, timezone="Asia/Tokyo")
    monkeypatch.chdir(tmp_path)

    def fake_run_eval(cases, llm, **kw):
        from kaizenlog.evalharness import EvalAggregate

        agg = EvalAggregate()
        agg.total_runs = len(cases)
        agg.final_ok = len(cases)
        agg.first_pass = len(cases)
        return agg

    monkeypatch.setattr("kaizenlog.evalharness.run_eval", fake_run_eval)
    # generate path not used
    code = cli_mod.cmd_eval_run(cfg, cases_dir=None, repeat=1, min_pass_rate=None)
    assert code == 0
    out = capsys.readouterr().out
    assert "同梱サンプル" in out


def test_repo_eval_samples_dir_points_to_committed_tree():
    root = _repo_root()
    found = repo_eval_samples_dir()
    # 開発ツリーではリポジトリの eval/samples が取れる
    if (root / "eval" / "samples").is_dir():
        assert found is not None
        assert found.resolve() == (root / "eval" / "samples").resolve() or found.name == "samples"
