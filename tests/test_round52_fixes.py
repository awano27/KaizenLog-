"""第52弾: 第51弾レビュー残件（TZ・14日窓・空見出し・dirty・config）。"""

from __future__ import annotations

import os
import subprocess
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kaizenlog.config import Config, DelegationConfig, load_config
from kaizenlog.digest import build_delegation_subsection, build_digest
from kaizenlog.outcome_git import _git_dirty
from kaizenlog.reflect import build_reflect_section, collect_reflect_questions
from kaizenlog.report import hhmm_from_iso
from kaizenlog.resume import build_resume_section

TZ = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 8, 2)


# ---------------------------------------------------------------------------
# §R1 timezone
# ---------------------------------------------------------------------------


def test_r1_hhmm_utc_to_jst():
    assert hhmm_from_iso("2026-08-02T14:59:00+00:00", TZ) == "23:59"


def test_r1_hhmm_jst_midnight_clamp():
    assert hhmm_from_iso("2026-08-03T00:00:00+09:00", TZ) == "00:00"


def test_r1_resume_shows_jst():
    stats = {
        "day": DAY.isoformat(),
        "blocks": [
            {
                "start": "2026-08-02T14:58:00+00:00",
                "end": "2026-08-03T00:00:00+09:00",
                "app": "claude.exe",
                "minutes": 2.0,
            }
        ],
        "ai": {
            "session_digests": [
                {
                    "project": "KaizenLog",
                    "ended_in_error": True,
                    "files_touched": ["a.py"],
                    "commands_run": [],
                    "last_reply_digest": None,
                    "end": "2026-08-02T14:59:00+00:00",
                }
            ]
        },
    }
    body = build_resume_section(stats, tz=TZ)
    assert body is not None
    # UTC 14:58 → JST 23:58 / end clamp 00:00
    assert "23:58–00:00" in body
    assert "未決着: 23:59 終了" in body
    assert "14:58" not in body
    assert "14:59" not in body


def test_r1_reflect_shows_jst():
    stats = {
        "day": DAY.isoformat(),
        "by_site": {},
        "ai": {
            "session_digests": [
                {
                    "ended_in_error": True,
                    "tests_run": False,
                    "prompts_digest": ["pushしてください"],
                    "end": "2026-08-02T14:59:00+00:00",
                }
            ]
        },
    }
    qs = collect_reflect_questions(stats, [], today=DAY, tz=TZ)
    assert qs
    assert qs[0].startswith("23:59 終了")
    body = build_reflect_section(stats, [], today=DAY, tz=TZ)
    assert body is not None
    assert "23:59 終了" in body
    assert "14:59" not in body


def test_r1_naive_iso_no_shift():
    assert hhmm_from_iso("2026-08-02T14:59:00", TZ) == "14:59"


# ---------------------------------------------------------------------------
# §R2 window labels
# ---------------------------------------------------------------------------


def _hist_days(n: int, *, edits_per: int = 10, turns: int = 10) -> list[dict]:
    out = []
    for i in range(n, 0, -1):
        d = DAY - timedelta(days=i)
        out.append(
            {
                "day": d.isoformat(),
                "by_app": {"Code.exe": 100.0 + i},
                "ai": {
                    "session_digests": [
                        {
                            "edits": edits_per,
                            "user_turns": turns,
                            "tests_run": False,
                        }
                    ]
                },
                "outcome_git": [{"repo_label": "x", "commits": 1}],
            }
        )
    return out


def test_r2_label_14_days():
    stats = {
        "day": DAY.isoformat(),
        "source_status": "verified",
        "total_minutes": 120.0,
        "by_category": {"AI作業": 40.0},
        "by_app": {"Code.exe": 125.0},
        "ai": {
            "session_digests": [
                {"edits": 20, "user_turns": 10, "tests_run": True}
            ]
        },
        "outcome_git": [{"repo_label": "x", "commits": 2}],
    }
    hist = _hist_days(14)
    sub = build_delegation_subsection(stats, hist, today=DAY)
    assert sub is not None
    assert "14日中央値" in sub
    assert "5日中央値" not in sub
    assert "### 🤝 委譲の形（" not in sub
    assert "### 🤝 委譲の形" in sub
    # 30秒サマリは不変（委譲は末尾のみ）
    core = build_digest(stats, [], today=DAY, redactor=None, stats_history=hist)
    assert core is not None
    head = core.split("### 🤝")[0]
    assert "稼働" in head
    assert "14日中央値" not in head  # 先頭部は7日ラベルのまま
    assert "\n\n### 🤝 委譲の形" in core
    assert "### 🤝 委譲の形（" not in core


def test_r2_label_5_days():
    stats = {
        "day": DAY.isoformat(),
        "by_app": {"Code.exe": 125.0},
        "ai": {
            "session_digests": [
                {"edits": 20, "user_turns": 10, "tests_run": False}
            ]
        },
        "outcome_git": [{"repo_label": "x", "commits": 2}],
    }
    hist = _hist_days(5)
    sub = build_delegation_subsection(stats, hist, today=DAY)
    assert sub is not None
    assert "5日中央値" in sub
    assert "14日中央値" not in sub
    assert "### 🤝 委譲の形（" not in sub
    assert "### 🤝 委譲の形" in sub


# ---------------------------------------------------------------------------
# §R3 empty project omit
# ---------------------------------------------------------------------------


def test_r3_empty_project_heading_omitted():
    stats = {
        "day": DAY.isoformat(),
        "blocks": [
            {
                "start": "2026-08-02T10:00:00+09:00",
                "end": "2026-08-02T11:00:00+09:00",
                "app": "Code.exe",
                "minutes": 60.0,
            }
        ],
        "ai": {
            "session_digests": [
                {
                    "project": "empty-proj",
                    "ended_in_error": False,
                    "files_touched": [],
                    "commands_run": [],
                    "last_reply_digest": None,
                    "end": "2026-08-02T11:00:00+09:00",
                },
                {
                    "project": "rich-proj",
                    "ended_in_error": False,
                    "files_touched": ["x.py"],
                    "commands_run": ["pytest"],
                    "last_reply_digest": None,
                    "end": "2026-08-02T10:00:00+09:00",
                },
            ]
        },
    }
    body = build_resume_section(stats, tz=TZ)
    assert body is not None
    assert "rich-proj" in body
    assert "empty-proj" not in body
    assert "触っていたファイル: x.py" in body


# ---------------------------------------------------------------------------
# §R5 dirty
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )


def test_r5_dirty_clean_and_dirty(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    assert _git_dirty(repo, timeout=5.0, env=env) is False
    (repo / "b.txt").write_text("dirty\n", encoding="utf-8")
    assert _git_dirty(repo, timeout=5.0, env=env) is True


def test_r5_dirty_fail_closed(tmp_path: Path):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    # 非 git ディレクトリ
    assert _git_dirty(tmp_path, timeout=5.0, env=env) is None
    # timeout はモックで
    import kaizenlog.outcome_git as og

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=0.01)

    orig = og.subprocess.run
    og.subprocess.run = boom  # type: ignore[assignment]
    try:
        assert _git_dirty(tmp_path, timeout=0.01, env=env) is None
    finally:
        og.subprocess.run = orig  # type: ignore[assignment]


def test_r5_dirty_does_not_mutate_tree(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    f = repo / "a.txt"
    f.write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    before = {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in repo.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }
    # porcelain 一覧も固定
    st_before = _git(repo, "status", "--porcelain").stdout
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    _ = _git_dirty(repo, timeout=5.0, env=env)
    after = {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in repo.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }
    st_after = _git(repo, "status", "--porcelain").stdout
    assert before == after
    assert st_before == st_after


# ---------------------------------------------------------------------------
# §R6 config parse
# ---------------------------------------------------------------------------


def test_r6_delegation_editor_apps_parsed(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text(
        f'[general]\nvault_dir = "{tmp_path.as_posix()}"\n'
        '[delegation]\neditor_apps = ["foo.exe"]\n',
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.delegation.editor_apps == ["foo.exe"]


def test_r6_delegation_default_when_missing(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text(
        f'[general]\nvault_dir = "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert "Code.exe" in cfg.delegation.editor_apps
    assert cfg.delegation.editor_apps == DelegationConfig().editor_apps


def test_r6_delegation_non_list_keeps_default(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text(
        f'[general]\nvault_dir = "{tmp_path.as_posix()}"\n'
        '[delegation]\neditor_apps = "not-a-list"\n',
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.delegation.editor_apps == DelegationConfig().editor_apps
