"""`kaizenlog prompts --roi` は表示専用で Kaizen/Memory を変えない。

第32弾 doctor と同じ「診断・表示コマンドは状態を変えない」方針の回帰テスト。
既定の `kaizenlog prompts`（発掘）は upsert を維持する（PRM 採番→ mark の前提）。
"""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.aiwork import UserPrompt
from kaizenlog.cli import cmd_prompts
from kaizenlog.config import AIWorkConfig, Config
from kaizenlog.promptledger import (
    PromptLedgerEntry,
    append_prompt_ledger,
    load_prompt_ledger,
)

AS_OF = date(2026, 10, 18)
TZ = ZoneInfo("Asia/Tokyo")


def _cfg(tmp_path: Path, *, aiwork_enabled: bool) -> Config:
    vault = tmp_path / "vault"
    vault.mkdir()
    return Config(vault_dir=vault, aiwork=AIWorkConfig(enabled=aiwork_enabled))


def _fake_prompts() -> list[UserPrompt]:
    return [
        UserPrompt(
            timestamp=datetime(2026, 10, 17, 9, 0, tzinfo=TZ),
            project="proj",
            text="pushしてください",
        ),
        UserPrompt(
            timestamp=datetime(2026, 10, 18, 9, 0, tzinfo=TZ),
            project="proj",
            text="pushしてください",
        ),
    ]


def _patch_telemetry(monkeypatch, prompts: list[UserPrompt]) -> None:
    monkeypatch.setattr("kaizenlog.cli.available_adapters", lambda cfg: [])
    monkeypatch.setattr(
        "kaizenlog.cli.collect_ai_telemetry",
        lambda adapters, start, end: ([], prompts, 0),
    )


def test_roi_display_creates_nothing_under_vault(tmp_path, capsys):
    cfg = _cfg(tmp_path, aiwork_enabled=False)
    vault = Path(cfg.vault_dir)
    assert not cfg.memory_path.exists()

    cmd_prompts(cfg, 14, AS_OF, 2, roi=True)

    out = capsys.readouterr().out
    assert "プロンプト資産ROI" in out
    assert list(vault.rglob("*")) == []


def test_roi_display_leaves_existing_ledger_byte_identical(
    tmp_path, capsys, monkeypatch
):
    cfg = _cfg(tmp_path, aiwork_enabled=True)
    _patch_telemetry(monkeypatch, _fake_prompts())
    entry = PromptLedgerEntry(
        id="PRM-20261001-001",
        representative="pushしてください",
        count_total=8,
        days_seen=4,
        first_seen="2026-10-01",
        last_seen="2026-10-04",
        status="new",
        skill_name=None,
        marked_on=None,
    )
    append_prompt_ledger(cfg.memory_path, [entry])
    vault = Path(cfg.vault_dir)
    ledger_path = cfg.memory_path / "prompt_clusters.jsonl"
    before_bytes = ledger_path.read_bytes()
    before_tree = sorted(str(p.relative_to(vault)) for p in vault.rglob("*"))

    cmd_prompts(cfg, 14, AS_OF, 2, roi=True)

    out = capsys.readouterr().out
    assert "PRM-20261001-001" in out
    assert ledger_path.read_bytes() == before_bytes
    after_tree = sorted(str(p.relative_to(vault)) for p in vault.rglob("*"))
    assert after_tree == before_tree


def test_default_prompts_view_still_upserts(tmp_path, capsys, monkeypatch):
    cfg = _cfg(tmp_path, aiwork_enabled=True)
    _patch_telemetry(monkeypatch, _fake_prompts())

    cmd_prompts(cfg, 14, AS_OF, 2, roi=False)

    capsys.readouterr()
    ledger = load_prompt_ledger(cfg.memory_path)
    assert len(ledger) == 1
    assert ledger[0].count_total == 2
    assert ledger[0].status == "new"
