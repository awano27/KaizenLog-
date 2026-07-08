"""Claude Codeスキルの同梱・安全なインストール・診断。

パッケージに同梱されたスキル（daily-kaizen / weekly-kaizen / kaizen-autopilot）を
ボールトの .claude/skills/ に配置する。既存ファイルは黙って上書きせず、
--force のときだけバックアップ（.bak）を取ってから上書きする。
"""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


def bundled_skill_names() -> list[str]:
    root = resources.files("kaizenlog") / "skills"
    return sorted(
        entry.name for entry in root.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    )


def bundled_skill_content(name: str) -> str:
    return (resources.files("kaizenlog") / "skills" / name / "SKILL.md").read_text(
        encoding="utf-8"
    )


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


def check_skill(vault_dir: Path, name: str) -> SkillStatus:
    dest = Path(vault_dir) / ".claude" / "skills" / name / "SKILL.md"
    if not dest.is_file():
        return SkillStatus(name=name, state="not-installed")
    if dest.read_text(encoding="utf-8") == bundled_skill_content(name):
        return SkillStatus(name=name, state="up-to-date", installed_path=dest)
    return SkillStatus(name=name, state="outdated-or-modified", installed_path=dest)


def install_skill(vault_dir: Path, name: str, force: bool = False) -> tuple[str, Path]:
    """スキルを1つインストールする。結果は ("installed"|"unchanged"|"overwritten"|"skipped", path)。"""
    if name not in bundled_skill_names():
        raise ValueError(f"同梱されていないスキルです: {name}")
    content = bundled_skill_content(name)
    dest = Path(vault_dir) / ".claude" / "skills" / name / "SKILL.md"

    if dest.is_file():
        existing = dest.read_text(encoding="utf-8")
        if existing == content:
            return "unchanged", dest
        if not force:
            return "skipped", dest
        shutil.copy2(dest, dest.with_suffix(".md.bak"))
        dest.write_text(content, encoding="utf-8")
        return "overwritten", dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return "installed", dest


def diff_skill(vault_dir: Path, name: str, context_lines: int = 2) -> str:
    """インストール済みと同梱版の差分（unified diff、先頭40行まで）。"""
    dest = Path(vault_dir) / ".claude" / "skills" / name / "SKILL.md"
    if not dest.is_file():
        return ""
    diff = difflib.unified_diff(
        dest.read_text(encoding="utf-8").splitlines(),
        bundled_skill_content(name).splitlines(),
        fromfile=f"installed/{name}/SKILL.md",
        tofile=f"bundled/{name}/SKILL.md",
        lineterm="",
        n=context_lines,
    )
    lines = list(diff)[:40]
    return "\n".join(lines)
