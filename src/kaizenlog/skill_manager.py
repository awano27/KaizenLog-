"""Claude Codeスキルの同梱・安全なインストール・診断。

パッケージに同梱されたスキルをボールトの .claude/skills/ に配置する。既存の
所有ファイルは黙って上書きせず、--force のときだけ置換する各ファイルを .bak に退避する。
"""

from __future__ import annotations

import difflib
import stat
import shutil
from dataclasses import dataclass
from importlib import resources
from importlib.abc import Traversable
from pathlib import Path, PurePosixPath


# スキル本文の外にあるが、配布時に weekly-kaizen が所有する参照ファイル。
# advisor.load_bundled_prompt() の既存利用先と同じ packaged prompt を正本に保つ。
_EXTRA_OWNED_RESOURCES: dict[str, dict[PurePosixPath, Traversable]] = {
    "weekly-kaizen": {
        PurePosixPath("references/ai_work_deep_review.md"): (
            resources.files("kaizenlog") / "prompts" / "ai_work_deep_review.md"
        ),
    },
}


def bundled_skill_names() -> list[str]:
    root = resources.files("kaizenlog") / "skills"
    return sorted(
        entry.name for entry in root.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    )


def _validated_skill_name(name: str) -> str:
    if name not in bundled_skill_names():
        raise ValueError(f"同梱されていないスキルです: {name}")
    return name


def _validate_relative_resource_path(relative: PurePosixPath) -> None:
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise ValueError(f"同梱リソースの相対パスが不正です: {relative}")


def _walk_resource_files(
    root: Traversable,
    relative: PurePosixPath = PurePosixPath("."),
) -> dict[PurePosixPath, Traversable]:
    """スキル配下の通常ファイルを再帰的に列挙する。"""
    files: dict[PurePosixPath, Traversable] = {}
    for entry in root.iterdir():
        child_relative = relative / entry.name
        _validate_relative_resource_path(child_relative)
        if entry.is_file():
            files[child_relative] = entry
        elif entry.is_dir():
            files.update(_walk_resource_files(entry, child_relative))
    return files


def _owned_resources(name: str) -> dict[PurePosixPath, Traversable]:
    """指定スキルが配布時に管理する全ファイルを返す。"""
    name = _validated_skill_name(name)
    skill_root = resources.files("kaizenlog") / "skills" / name
    owned = _walk_resource_files(skill_root)
    for relative, source in _EXTRA_OWNED_RESOURCES.get(name, {}).items():
        _validate_relative_resource_path(relative)
        if not source.is_file():
            raise ValueError(f"同梱リソースが見つかりません: {relative}")
        if relative in owned:
            raise ValueError(f"同梱リソースが重複しています: {relative}")
        owned[relative] = source
    return dict(sorted(owned.items(), key=lambda item: item[0].as_posix()))


def bundled_skill_content(name: str) -> str:
    return _owned_resources(name)[PurePosixPath("SKILL.md")].read_text(encoding="utf-8")


def skill_description(content: str) -> str:
    """SKILL.md frontmatterからdescriptionを取り出す（先頭120字）。"""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            return line.partition(":")[2].strip().strip('"')[:120]
    return ""


@dataclass
class SkillStatus:
    name: str
    state: str  # "not-installed" | "up-to-date" | "outdated-or-modified"
    installed_path: Path | None = None


def _skill_destination_root(vault_dir: Path, name: str) -> Path:
    return Path(vault_dir) / ".claude" / "skills" / name


def _destination_path(root: Path, relative: PurePosixPath) -> Path:
    _validate_relative_resource_path(relative)
    destination = root.joinpath(*relative.parts)
    try:
        destination.relative_to(root)
    except ValueError as e:
        raise ValueError(f"同梱リソースの保存先が不正です: {relative}") from e
    return destination


def _is_link_or_junction(path: Path) -> bool:
    """Windows junctions are reparse points but Path.is_symlink() returns False."""
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _raise_if_file_collision(path: Path) -> None:
    if _is_link_or_junction(path):
        raise ValueError(f"同梱スキルの保存先にシンボリックリンクは使えません: {path}")
    if path.exists() and not path.is_file():
        raise ValueError(f"同梱スキルのファイル保存先がディレクトリです: {path}")


def _raise_if_parent_collision(root: Path, destination: Path) -> None:
    """root 以下の既存親を検査し、リンク越し・ファイル越しの書き込みを防ぐ。"""
    parent = destination.parent
    while True:
        if _is_link_or_junction(parent):
            raise ValueError(f"同梱スキルの保存先にシンボリックリンクは使えません: {parent}")
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"同梱スキルの保存先がディレクトリではありません: {parent}")
        if parent == root:
            return
        parent = parent.parent


def _preflight_install_destinations(root: Path, owned: dict[PurePosixPath, Traversable]) -> None:
    """書き込み前に親ディレクトリと全所有ファイルの衝突を検査する。"""
    parents = (root.parents[2], root.parents[1], root.parent, root)
    for directory in parents:
        if _is_link_or_junction(directory):
            raise ValueError(f"同梱スキルの保存先にシンボリックリンクは使えません: {directory}")
        if directory.exists() and not directory.is_dir():
            raise ValueError(f"同梱スキルの保存先がディレクトリではありません: {directory}")
    for relative in owned:
        destination = _destination_path(root, relative)
        _raise_if_parent_collision(root, destination)
        _raise_if_file_collision(destination)


def _safe_backup_path(path: Path) -> Path:
    """既存バックアップを残す、次に利用可能なバックアップ名を返す。"""
    candidate = path.with_name(f"{path.name}.bak")
    suffix = 1
    while candidate.exists() or _is_link_or_junction(candidate):
        candidate = path.with_name(f"{path.name}.bak.{suffix}")
        suffix += 1
    return candidate


def _installed_file_state(
    root: Path,
    owned: dict[PurePosixPath, Traversable],
) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """(変更済み, 欠損) の全所有ファイルを、同梱内容と共に返す。"""
    modified: list[tuple[Path, str]] = []
    missing: list[tuple[Path, str]] = []
    for relative, source in owned.items():
        destination = _destination_path(root, relative)
        _raise_if_parent_collision(root, destination)
        _raise_if_file_collision(destination)
        bundled = source.read_text(encoding="utf-8")
        if not destination.is_file():
            missing.append((destination, bundled))
        elif destination.read_text(encoding="utf-8", errors="replace") != bundled:
            modified.append((destination, bundled))
    return modified, missing


def check_skill(vault_dir: Path, name: str) -> SkillStatus:
    name = _validated_skill_name(name)
    owned = _owned_resources(name)
    root = _skill_destination_root(vault_dir, name)
    primary = _destination_path(root, PurePosixPath("SKILL.md"))
    modified, missing = _installed_file_state(root, owned)
    # 完全に未配置のときだけ未インストール扱い。SKILL.md が欠けていても、
    # 参照ファイルが残るなら部分インストールなので修復・差分対象にする。
    if len(missing) == len(owned):
        return SkillStatus(name=name, state="not-installed")
    if not modified and not missing:
        return SkillStatus(name=name, state="up-to-date", installed_path=primary)
    return SkillStatus(name=name, state="outdated-or-modified", installed_path=primary)


def install_skill(vault_dir: Path, name: str, force: bool = False) -> tuple[str, Path]:
    """スキルを1つインストールする。

    結果は既存契約どおり
    ("installed"|"unchanged"|"overwritten"|"skipped", SKILL.md のパス)。
    """
    name = _validated_skill_name(name)
    owned = _owned_resources(name)
    root = _skill_destination_root(vault_dir, name)
    primary = _destination_path(root, PurePosixPath("SKILL.md"))
    _preflight_install_destinations(root, owned)
    modified, missing = _installed_file_state(root, owned)

    if not modified and not missing:
        return "unchanged", primary
    if modified and not force:
        return "skipped", primary

    # 衝突確認・変更判定を全て終えてから書く。ローカル改変がある bundle では
    # 参照だけの修復を行わず、部分更新を防ぐ。
    for destination, _bundled in (*modified, *missing):
        destination.parent.mkdir(parents=True, exist_ok=True)
    for destination, _bundled in modified:
        shutil.copy2(destination, _safe_backup_path(destination))
    for destination, bundled in (*modified, *missing):
        destination.write_text(bundled, encoding="utf-8")

    return ("overwritten" if modified else "installed"), primary


def diff_skill(vault_dir: Path, name: str, context_lines: int = 2) -> str:
    """全所有ファイル差分を、変更ファイルに表示枠を分けて最大40行で示す。"""
    name = _validated_skill_name(name)
    owned = _owned_resources(name)
    root = _skill_destination_root(vault_dir, name)
    _modified, missing = _installed_file_state(root, owned)
    # 既存の「完全に未インストールなら空文字」契約を維持する。SKILL.md
    # だけが欠けた部分インストールでは、残った所有ファイルも含めて差分を出す。
    if len(missing) == len(owned):
        return ""

    file_diffs: list[list[str]] = []
    for relative, source in owned.items():
        destination = _destination_path(root, relative)
        _raise_if_parent_collision(root, destination)
        _raise_if_file_collision(destination)
        installed = (
            destination.read_text(encoding="utf-8", errors="replace").splitlines()
            if destination.is_file()
            else []
        )
        bundled = source.read_text(encoding="utf-8").splitlines()
        diff = list(
            difflib.unified_diff(
                installed,
                bundled,
                fromfile=f"installed/{name}/{relative.as_posix()}",
                tofile=f"bundled/{name}/{relative.as_posix()}",
                lineterm="",
                n=context_lines,
            )
        )
        if diff:
            file_diffs.append(diff)
    lines: list[str] = []
    for index, diff in enumerate(file_diffs):
        # 大きな SKILL.md の差分が、上書きを見送った参照ファイルを隠さない。
        budget = (40 - len(lines)) // (len(file_diffs) - index)
        if budget:
            lines.extend(diff[:budget])
    return "\n".join(lines[:40])
