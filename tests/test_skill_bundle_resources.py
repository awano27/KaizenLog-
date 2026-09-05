from pathlib import Path
import subprocess
import sys

import pytest

from kaizenlog.advisor import load_bundled_prompt
from kaizenlog.skill_manager import check_skill, diff_skill, install_skill


WEEKLY_NAME = "weekly-kaizen"
REFERENCE_RELATIVE = Path("references") / "ai_work_deep_review.md"


def _skill_root(vault: Path) -> Path:
    return vault / ".claude" / "skills" / WEEKLY_NAME


def test_weekly_bundle_installs_canonical_review_reference_and_doctor_detects_loss(tmp_path):
    result, skill = install_skill(tmp_path, WEEKLY_NAME)
    reference = _skill_root(tmp_path) / REFERENCE_RELATIVE

    assert result == "installed"
    assert skill == _skill_root(tmp_path) / "SKILL.md"
    assert reference.read_text(encoding="utf-8") == load_bundled_prompt("ai_work_deep_review")
    assert check_skill(tmp_path, WEEKLY_NAME).state == "up-to-date"

    reference.unlink()

    assert check_skill(tmp_path, WEEKLY_NAME).state == "outdated-or-modified"
    diff = diff_skill(tmp_path, WEEKLY_NAME)
    assert "installed/weekly-kaizen/references/ai_work_deep_review.md" in diff
    assert "bundled/weekly-kaizen/references/ai_work_deep_review.md" in diff


def test_install_repairs_a_missing_owned_reference_when_other_owned_files_are_current(tmp_path):
    install_skill(tmp_path, WEEKLY_NAME)
    reference = _skill_root(tmp_path) / REFERENCE_RELATIVE
    reference.unlink()

    result, skill = install_skill(tmp_path, WEEKLY_NAME)

    assert result == "installed"
    assert skill == _skill_root(tmp_path) / "SKILL.md"
    assert reference.read_text(encoding="utf-8") == load_bundled_prompt("ai_work_deep_review")
    assert check_skill(tmp_path, WEEKLY_NAME).state == "up-to-date"


def test_modified_owned_file_skips_entire_bundle_without_creating_missing_reference(tmp_path):
    install_skill(tmp_path, WEEKLY_NAME)
    root = _skill_root(tmp_path)
    skill = root / "SKILL.md"
    reference = root / REFERENCE_RELATIVE
    skill.write_text(skill.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")
    reference.unlink()

    result, returned = install_skill(tmp_path, WEEKLY_NAME)

    assert result == "skipped"
    assert returned == skill
    assert "local edit" in skill.read_text(encoding="utf-8")
    assert not reference.exists()


def test_missing_skill_file_with_existing_modified_reference_is_diagnosed_and_diffed(tmp_path):
    root = _skill_root(tmp_path)
    reference = root / REFERENCE_RELATIVE
    reference.parent.mkdir(parents=True)
    reference.write_text("locally retained reference\n", encoding="utf-8")

    status = check_skill(tmp_path, WEEKLY_NAME)
    diff = diff_skill(tmp_path, WEEKLY_NAME)
    result, returned = install_skill(tmp_path, WEEKLY_NAME)

    assert status.state == "outdated-or-modified"
    assert status.installed_path == root / "SKILL.md"
    assert "installed/weekly-kaizen/SKILL.md" in diff
    assert "installed/weekly-kaizen/references/ai_work_deep_review.md" in diff
    assert "-locally retained reference" in diff
    assert result == "skipped"
    assert returned == root / "SKILL.md"
    assert not (root / "SKILL.md").exists()
    assert reference.read_text(encoding="utf-8") == "locally retained reference\n"


def test_force_preserves_existing_backups_and_backs_up_each_replaced_owned_file(tmp_path):
    install_skill(tmp_path, WEEKLY_NAME)
    root = _skill_root(tmp_path)
    skill = root / "SKILL.md"
    reference = root / REFERENCE_RELATIVE
    skill.write_text("locally modified skill\n", encoding="utf-8")
    reference.write_text("locally modified reference\n", encoding="utf-8")
    unrelated = root / "user-notes.md"
    unrelated.write_text("do not manage this file\n", encoding="utf-8")
    original_backup = skill.with_suffix(".md.bak")
    original_backup.write_text("older backup must survive\n", encoding="utf-8")

    result, returned = install_skill(tmp_path, WEEKLY_NAME, force=True)

    assert result == "overwritten"
    assert returned == skill
    assert original_backup.read_text(encoding="utf-8") == "older backup must survive\n"
    assert skill.with_suffix(".md.bak.1").read_text(encoding="utf-8") == "locally modified skill\n"
    assert reference.with_suffix(".md.bak").read_text(encoding="utf-8") == "locally modified reference\n"
    assert unrelated.read_text(encoding="utf-8") == "do not manage this file\n"
    assert check_skill(tmp_path, WEEKLY_NAME).state == "up-to-date"


def test_diff_shows_both_modified_owned_files_within_shared_line_limit(tmp_path):
    install_skill(tmp_path, WEEKLY_NAME)
    root = _skill_root(tmp_path)
    (root / "SKILL.md").write_text("local skill change\n" * 50, encoding="utf-8")
    (root / REFERENCE_RELATIVE).write_text("local reference change\n" * 50, encoding="utf-8")

    diff = diff_skill(tmp_path, WEEKLY_NAME)

    assert "installed/weekly-kaizen/SKILL.md" in diff
    assert "installed/weekly-kaizen/references/ai_work_deep_review.md" in diff
    assert "-local skill change" in diff
    assert "-local reference change" in diff
    assert len(diff.splitlines()) <= 40


def test_invalid_name_and_owned_directory_collision_fail_before_bundle_is_modified(tmp_path):
    with pytest.raises(ValueError, match="同梱されていない"):
        check_skill(tmp_path, "../weekly-kaizen")

    collision = _skill_root(tmp_path) / REFERENCE_RELATIVE
    collision.mkdir(parents=True)

    with pytest.raises(ValueError, match="ディレクトリ"):
        install_skill(tmp_path, WEEKLY_NAME)

    assert not (_skill_root(tmp_path) / "SKILL.md").exists()

    parent_collision_vault = tmp_path / "parent-collision"
    parent_collision = _skill_root(parent_collision_vault) / "references"
    parent_collision.parent.mkdir(parents=True)
    parent_collision.write_text("user file\n", encoding="utf-8")

    with pytest.raises(ValueError, match="ディレクトリ"):
        install_skill(parent_collision_vault, WEEKLY_NAME)

    assert not (_skill_root(parent_collision_vault) / "SKILL.md").exists()


def test_nested_owned_symlink_collision_is_rejected_before_install_when_supported(tmp_path):
    root = _skill_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    nested_link = root / "references"
    nested_link.parent.mkdir(parents=True)
    try:
        nested_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this Windows environment cannot create a directory symlink")

    with pytest.raises(ValueError, match="シンボリックリンク"):
        install_skill(tmp_path, WEEKLY_NAME)

    assert not (root / "SKILL.md").exists()
    assert not (outside / "ai_work_deep_review.md").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions are platform-specific")
def test_nested_owned_junction_collision_is_rejected_before_install_when_supported(tmp_path):
    root = _skill_root(tmp_path)
    outside = tmp_path / "junction-outside"
    outside.mkdir()
    nested_junction = root / "references"
    nested_junction.parent.mkdir(parents=True)
    result = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(nested_junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("this Windows environment cannot create a directory junction")

    with pytest.raises(ValueError, match="シンボリックリンク"):
        install_skill(tmp_path, WEEKLY_NAME)

    assert not (root / "SKILL.md").exists()
    assert not (outside / "ai_work_deep_review.md").exists()
