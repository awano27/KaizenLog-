from pathlib import Path

import pytest

from kaizenlog import skill_manager as manager
from kaizenlog.vault import atomic_write_bytes


@pytest.mark.parametrize("force", [False, True])
def test_partial_bundle_write_restores_originals_and_can_be_retried(monkeypatch, tmp_path, force):
    root = tmp_path / ".claude/skills/weekly-kaizen"
    skill = root / "SKILL.md"
    reference = root / "references/ai_work_deep_review.md"
    original = {}
    if force:
        manager.install_skill(tmp_path, "weekly-kaizen")
        skill.write_bytes(b"old skill\r\n  ")
        reference.write_bytes(b"old reference\r\n  ")
        original = {p: p.read_bytes() for p in (skill, reference)}
    root.mkdir(parents=True, exist_ok=True)
    user_file = root / "user-notes.md"
    user_file.write_bytes(b"preserve user data")
    failed = False
    old_text_write = Path.write_text

    def fault(path, payload):
        nonlocal failed
        if Path(path) == reference and not failed:
            failed = True
            reference.write_bytes(payload[:23])
            raise OSError("injected partial resource write")

    def text_write(path, text, *args, **kwargs):
        fault(path, text.encode("utf-8"))
        return old_text_write(path, text, *args, **kwargs)

    def bytes_write(path, payload):
        fault(path, payload)
        return atomic_write_bytes(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", text_write)
        patch.setattr(manager, "atomic_write_bytes", bytes_write, raising=False)
        with pytest.raises(OSError, match="injected partial resource write"):
            manager.install_skill(tmp_path, "weekly-kaizen", force=force)
    for target in (skill, reference):
        if force:
            assert target.read_bytes() == original[target]
            assert target.with_name(target.name + ".bak").read_bytes() == original[target]
        else:
            assert not target.exists()
    assert user_file.read_bytes() == b"preserve user data"
    manager.install_skill(tmp_path, "weekly-kaizen", force=force)
    assert manager.check_skill(tmp_path, "weekly-kaizen").state == "up-to-date"


def test_restore_failure_names_path_and_still_restores_other_files(monkeypatch, tmp_path):
    manager.install_skill(tmp_path, "weekly-kaizen")
    root = tmp_path / ".claude/skills/weekly-kaizen"
    skill = root / "SKILL.md"
    reference = root / "references/ai_work_deep_review.md"
    skill.write_bytes(b"old skill")
    reference.write_bytes(b"old reference")

    def write(path, payload):
        if path == reference:
            if payload != b"old reference":
                path.write_bytes(payload[:23])
            raise OSError("reference storage unavailable")
        return atomic_write_bytes(path, payload)

    monkeypatch.setattr(manager, "atomic_write_bytes", write)
    with pytest.raises(OSError) as caught:
        manager.install_skill(tmp_path, "weekly-kaizen", force=True)
    assert str(reference) in str(caught.value)
    assert skill.read_bytes() == b"old skill"
    assert reference.with_name(reference.name + ".bak").read_bytes() == b"old reference"
