"""AIセッションが触れたリポジトリの当日コミット統計（読み取り専用 git log）。"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RepoCommitStat:
    repo_label: str  # basename のみ。絶対パスは載せない
    commits: int
    insertions: int
    deletions: int
    subjects: list[str] = field(default_factory=list)  # 新しい順・最大3・各80字
    dirty: bool | None = None  # git status --porcelain（失敗時 None・旧 stats 互換）


def _git_toplevel(path: Path, *, timeout: float, env: dict) -> Path | None:
    """repo root へ正規化。失敗時 None（fail-closed）。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    root = (proc.stdout or "").strip()
    if not root:
        return None
    try:
        return Path(root).resolve()
    except (OSError, RuntimeError):
        return None


def _git_dirty(path: Path, *, timeout: float, env: dict) -> bool | None:
    """`git --no-optional-locks status --porcelain` で未コミット有無。失敗時 None。"""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "--no-optional-locks",
                "status",
                "--porcelain",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return bool((proc.stdout or "").strip())


def collect_commit_stats(
    repo_paths: Sequence[str | Path],
    day: date,
    *,
    tz: tzinfo | None = None,
    timeout: float = 5.0,
    max_repos: int = 5,
) -> tuple[list[RepoCommitStat], int]:
    """各 repo の当日コミット件数と ±行を返す。第2要素は上限超過で省いた数。

    各候補は `rev-parse --show-toplevel` で root へ畳んでから dedupe。
    per-repo の git 呼び出しは rev-parse + log の最大2回。
    --since=day開始, --until=当日23:59:59（閉区間・翌日0時は含まない）。
    """
    zone = tz or ZoneInfo("UTC")
    day_start = datetime.combine(day, time.min, tzinfo=zone)
    day_until = day_start + timedelta(days=1) - timedelta(seconds=1)
    since = day_start.isoformat()
    until = day_until.isoformat()

    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

    # path → root 正規化 + dedupe
    roots: list[Path] = []
    seen_roots: set[Path] = set()
    for raw in repo_paths:
        try:
            p = Path(raw).resolve()
        except (OSError, RuntimeError):
            continue
        if not p.is_dir():
            continue
        root = _git_toplevel(p, timeout=timeout, env=env)
        if root is None:
            continue
        if root in seen_roots:
            continue
        seen_roots.add(root)
        roots.append(root)

    omitted = max(0, len(roots) - max_repos)
    roots = roots[:max_repos]
    out: list[RepoCommitStat] = []
    for repo in roots:
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "log",
                    f"--since={since}",
                    f"--until={until}",
                    "--numstat",
                    "--format=%x01%s",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                env=env,
            )
        except FileNotFoundError:
            return [], 0
        except (subprocess.TimeoutExpired, OSError):
            continue
        if proc.returncode != 0:
            continue
        parsed = _parse_numstat(proc.stdout or "")
        if parsed is None:
            continue
        commits, ins, dels, subjects = parsed
        dirty = _git_dirty(repo, timeout=timeout, env=env)
        out.append(
            RepoCommitStat(
                repo_label=repo.name or "repo",
                commits=commits,
                insertions=ins,
                deletions=dels,
                subjects=subjects,
                dirty=dirty,
            )
        )
    return out, omitted


def _parse_numstat(text: str) -> tuple[int, int, int, list[str]] | None:
    """%x01%s 区切り + numstat。binary `- -` は行数に加算しない。異常なら None。

    subjects は新しい順（git log 既定）で最大3件・各80字切詰。
    """
    commits = 0
    insertions = 0
    deletions = 0
    subjects: list[str] = []
    in_commit = False
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line == "\x01" or line.startswith("\x01"):
            commits += 1
            in_commit = True
            # subject は \x01 直後（空 subject もあり得る）
            subj = line[1:] if line.startswith("\x01") else ""
            if len(subjects) < 3:
                # 80字超は省略記号付き（意味の途中切断を明示）
                s = subj if len(subj) <= 80 else subj[:79] + "…"
                subjects.append(s)
            continue
        if not in_commit and not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            if not line.strip():
                continue
            return None
        a, b = parts[0], parts[1]
        if a == "-" and b == "-":
            continue
        try:
            insertions += int(a)
            deletions += int(b)
        except ValueError:
            return None
    return commits, insertions, deletions, subjects
