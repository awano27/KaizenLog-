from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

import pytest

from kaizenlog.config import Config, PrivacyConfig
from kaizenlog.prompt_draft import draft_prompt


def _entry(prm_id="PRM-20261015-001", *, status="new", representative="顧客 SECRET の依頼を整理"):
    return {
        "id": prm_id,
        "representative": representative,
        "count_total": 2,
        "days_seen": 1,
        "first_seen": "2026-10-15",
        "last_seen": "2026-10-15",
        "status": status,
        "skill_name": "整理" if status == "skilled" else None,
        "marked_on": "2026-10-15" if status == "skilled" else None,
    }


def _cfg(tmp_path, *entries):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "prompt_clusters.jsonl").write_bytes(
        b"".join((json.dumps(e, ensure_ascii=False) + "\n").encode("utf-8") for e in entries)
    )
    return Config(
        vault_dir=tmp_path,
        memory_dir="memory",
        privacy=PrivacyConfig(redact_patterns=["SECRET"]),
    )


def test_preview_new_and_skilled_is_unverified_and_does_not_write(tmp_path):
    cfg = _cfg(tmp_path, _entry(), _entry("PRM-20261015-002", status="skilled"))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    result = draft_prompt(cfg, "PRM-20261015-001", project=None)

    assert result.status == "preview"
    assert result.path is None
    assert "UNVERIFIED_DRAFT / 効果未検証・編集用" in result.content
    assert "PRM-20261015-001" in result.content
    assert result.fingerprint == hashlib.sha256(
        (json.dumps(_entry(), ensure_ascii=False) + "\n").encode("utf-8")
    ).hexdigest()
    assert "[REDACTED]" in result.content
    assert "SECRET" not in result.content
    assert "未記入" in result.content
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    skilled = draft_prompt(cfg, "PRM-20261015-002")
    assert skilled.status == "preview"


def test_write_requires_explicit_project(tmp_path):
    cfg = _cfg(tmp_path, _entry())

    result = draft_prompt(cfg, "PRM-20261015-001", write=True)

    assert result.status == "invalid"
    assert result.path is None
    assert not (tmp_path / ".kaizenlog").exists()


def test_draft_does_not_require_export_30_day_observation(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, _entry())
    import kaizenlog.prompt_draft as module
    monkeypatch.setattr(module, "collect_ai_telemetry", lambda *a, **k: pytest.fail("telemetry called"), raising=False)

    result = draft_prompt(cfg, "PRM-20261015-001")

    assert result.status == "preview"
    assert "30日" not in result.content


def test_invalid_target_tail_never_falls_back(tmp_path):
    cfg = _cfg(tmp_path, _entry())
    ledger = cfg.memory_path / "prompt_clusters.jsonl"
    bad = dict(_entry(), representative="", status="future")
    with ledger.open("ab") as stream:
        stream.write((json.dumps(bad, ensure_ascii=False) + "\n").encode("utf-8"))

    result = draft_prompt(cfg, "PRM-20261015-001")

    assert result.status == "invalid"
    assert result.content == ""


def test_old_malformed_target_then_valid_repair_uses_last_valid_target(tmp_path):
    cfg = _cfg(tmp_path, _entry())
    ledger = cfg.memory_path / "prompt_clusters.jsonl"
    ledger.write_bytes(b'{"id":"PRM-20261015-001", "representative":\n')
    repaired = dict(_entry(), representative="repaired")
    with ledger.open("ab") as stream:
        stream.write((json.dumps(repaired, ensure_ascii=False) + "\n").encode("utf-8"))

    result = draft_prompt(cfg, "PRM-20261015-001")

    assert result.status == "preview"
    assert "repaired" in result.content


def test_final_malformed_target_stays_invalid_after_valid_repair(tmp_path):
    cfg = _cfg(tmp_path, _entry())
    ledger = cfg.memory_path / "prompt_clusters.jsonl"
    repaired = dict(_entry(), representative="repaired")
    ledger.write_bytes((json.dumps(repaired, ensure_ascii=False) + "\n").encode("utf-8"))
    with ledger.open("ab") as stream:
        stream.write(b'{"id":"PRM-20261015-001", "representative":\n')

    result = draft_prompt(cfg, "PRM-20261015-001")

    assert result.status == "invalid"


def test_unrelated_append_keeps_target_fingerprint(tmp_path):
    cfg = _cfg(tmp_path, _entry())
    first = draft_prompt(cfg, "PRM-20261015-001")
    with (cfg.memory_path / "prompt_clusters.jsonl").open("ab") as stream:
        stream.write((json.dumps(_entry("PRM-20261015-002"), ensure_ascii=False) + "\n").encode("utf-8"))

    second = draft_prompt(cfg, "PRM-20261015-001")

    assert second.fingerprint == first.fingerprint


def test_write_requires_force_for_changed_existing_draft(tmp_path):
    cfg = _cfg(tmp_path, _entry())
    project = tmp_path / "project"
    project.mkdir()
    preview = draft_prompt(cfg, "PRM-20261015-001")
    written = draft_prompt(cfg, "PRM-20261015-001", project=project, write=True, expected_fingerprint=preview.fingerprint)
    assert written.status == "written"
    assert draft_prompt(cfg, "PRM-20261015-001", project=project, write=True).status == "unchanged"
    target = project / ".kaizenlog" / "drafts" / "PRM-20261015-001.md"
    target.write_text("USER CONTENT", encoding="utf-8")
    conflict = draft_prompt(cfg, "PRM-20261015-001", project=project, write=True)
    assert conflict.status == "conflict"
    assert not target.with_name(target.name + ".bak").exists()
    forced = draft_prompt(cfg, "PRM-20261015-001", project=project, write=True, force=True)
    assert forced.status == "written"
    assert forced.backup and forced.backup.name == "PRM-20261015-001.md.bak"
    assert forced.backup.read_text(encoding="utf-8") == "USER CONTENT"


def test_force_write_rechecks_target_before_backup_and_replace(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, _entry())
    project = tmp_path / "project"
    project.mkdir()
    preview = draft_prompt(cfg, "PRM-20261015-001")
    target = project / ".kaizenlog" / "drafts" / "PRM-20261015-001.md"
    draft_prompt(cfg, "PRM-20261015-001", project=project, write=True, expected_fingerprint=preview.fingerprint)
    target.write_text("A", encoding="utf-8")
    import kaizenlog.prompt_draft as module
    real_read = module._read_existing_target
    reads = {"count": 0}

    def read_then_external_write(path):
        value = real_read(path)
        reads["count"] += 1
        if reads["count"] == 1:
            target.write_text("B", encoding="utf-8")
        return value

    monkeypatch.setattr(module, "_read_existing_target", read_then_external_write)
    first = draft_prompt(cfg, "PRM-20261015-001", project=project, write=True, force=True)
    assert first.status == "conflict"
    assert target.read_text(encoding="utf-8") == "B"
    assert not target.with_name(target.name + ".bak").exists()

    target.write_text("A", encoding="utf-8")
    monkeypatch.setattr(module, "_read_existing_target", real_read)
    real_backup = module._ensure_exclusive_backup

    def backup_then_external_write(path, old):
        result = real_backup(path, old)
        target.write_text("B", encoding="utf-8")
        return result

    monkeypatch.setattr(module, "_ensure_exclusive_backup", backup_then_external_write)
    second = draft_prompt(cfg, "PRM-20261015-001", project=project, write=True, force=True)
    assert second.status == "conflict"
    assert target.read_text(encoding="utf-8") == "B"
    assert target.with_name(target.name + ".bak").read_text(encoding="utf-8") == "A"


def test_force_write_rechecks_expected_fingerprint_after_backup(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, _entry())
    project = tmp_path / "project"
    project.mkdir()
    preview = draft_prompt(cfg, "PRM-20261015-001")
    target = project / ".kaizenlog" / "drafts" / "PRM-20261015-001.md"
    draft_prompt(cfg, "PRM-20261015-001", project=project, write=True, expected_fingerprint=preview.fingerprint)
    target.write_text("A", encoding="utf-8")

    import kaizenlog.prompt_draft as module
    real_backup = module._ensure_exclusive_backup

    def backup_then_append_target_update(path, old):
        result = real_backup(path, old)
        with (cfg.memory_path / "prompt_clusters.jsonl").open("ab") as stream:
            stream.write((json.dumps(dict(_entry(), representative="new source"), ensure_ascii=False) + "\n").encode("utf-8"))
        return result

    monkeypatch.setattr(module, "_ensure_exclusive_backup", backup_then_append_target_update)
    result = draft_prompt(cfg, "PRM-20261015-001", project=project, write=True, force=True, expected_fingerprint=preview.fingerprint)

    assert result.status == "stale"
    assert target.read_text(encoding="utf-8") == "A"


def test_path_is_revalidated_before_target_lock(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, _entry())
    project = tmp_path / "project"
    project.mkdir()
    state = {"armed": False}
    original = __import__("kaizenlog.prompt_draft", fromlist=["_is_reparse_point"])
    real_detector = original._is_reparse_point

    def arm_detector(path):
        if state["armed"] and path.name == "drafts":
            return True
        return real_detector(path)

    real_lock = original.locked_read_modify_write

    @contextmanager
    def arm_after_ledger_lock(path, **kwargs):
        with real_lock(path, **kwargs):
            state["armed"] = True
            yield path

    monkeypatch.setattr(original, "locked_read_modify_write", arm_after_ledger_lock)
    monkeypatch.setattr(original, "_is_reparse_point", arm_detector)
    result = draft_prompt(cfg, "PRM-20261015-001", project=project, write=True)
    assert result.status == "invalid"
    assert not (tmp_path / ".kaizenlog").exists()
    assert list(project.rglob("*.lock")) == []
    assert list(project.rglob("PRM-20261015-001.md*")) == []


def test_write_rechecks_fingerprint_before_replace(tmp_path):
    cfg = _cfg(tmp_path, _entry())
    project = tmp_path / "project"
    project.mkdir()
    preview = draft_prompt(cfg, "PRM-20261015-001")
    ledger = cfg.memory_path / "prompt_clusters.jsonl"
    with ledger.open("ab") as stream:
        stream.write((json.dumps(dict(_entry(), representative="changed"), ensure_ascii=False) + "\n").encode("utf-8"))

    result = draft_prompt(cfg, "PRM-20261015-001", project=project, write=True, force=True, expected_fingerprint=preview.fingerprint)

    assert result.status == "stale"
    assert result.path is None
    assert not (project / ".kaizenlog").exists()


def test_project_drafts_reparse_point_and_backup_stay_inside(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, _entry())
    project = tmp_path / "project"
    project.mkdir()
    import kaizenlog.prompt_draft as module
    monkeypatch.setattr(module, "_is_reparse_point", lambda path: path.name == "drafts", raising=True)

    result = draft_prompt(cfg, "PRM-20261015-001", project=project, write=True)

    assert result.status == "invalid"
    assert not (tmp_path / ".kaizenlog").exists()


@pytest.mark.parametrize("prm_id", ["PRM-20261015-01", "../escape", "a/b", "CON", "PRM-20261015-001.md"])
def test_rejects_dismissed_missing_and_path_like_ids(tmp_path, prm_id):
    cfg = _cfg(tmp_path, _entry("PRM-20261015-001", status="dismissed"))
    project = tmp_path / "project"
    project.mkdir()

    result = draft_prompt(cfg, prm_id, project=project, write=True)

    assert result.status in {"invalid", "missing"}
    assert result.path is None
    assert not (project / ".kaizenlog").exists()
