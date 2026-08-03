"""きのうの続きから（kaizenlog:resume）。決定論のみ・LLM 不関与。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import tzinfo
from typing import Any

from .report import _fmt_minutes, hhmm_from_iso

_KZN_ID_RE = re.compile(r"\b(KZN-\d{8}-\d+)\b")
_RESUME_ONE_RE = re.compile(
    r"^(\s*-\s*\[)([ xX])(\]\s*再開1手:.*)$", re.MULTILINE
)


def _parse_end_key(value: object) -> str:
    """end 比較用キー（欠落は末尾扱いしない）。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _session_digests(stats: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    ai = stats.get("ai")
    if not isinstance(ai, Mapping):
        return []
    digests = ai.get("session_digests")
    if not isinstance(digests, list):
        return []
    return [d for d in digests if isinstance(d, Mapping)]


def _last_block_line(
    stats: Mapping[str, Any], *, tz: tzinfo | None = None
) -> str | None:
    blocks = stats.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return None
    best: Mapping[str, Any] | None = None
    best_end = ""
    for b in blocks:
        if not isinstance(b, Mapping):
            continue
        end_key = _parse_end_key(b.get("end"))
        if not end_key:
            continue
        if best is None or end_key > best_end:
            best = b
            best_end = end_key
    if best is None:
        return None
    start_hh = hhmm_from_iso(best.get("start"), tz) or "??:??"
    end_hh = hhmm_from_iso(best.get("end"), tz) or "??:??"
    app = str(best.get("app") or "—").strip() or "—"
    try:
        mins = float(best.get("minutes") or 0)
    except (TypeError, ValueError):
        mins = 0.0
    return f"- 最終作業ブロック: {start_hh}–{end_hh} / {app} {_fmt_minutes(mins)}"


def _top_projects(digests: Sequence[Mapping[str, Any]], *, limit: int = 2) -> list[str]:
    ordered = sorted(
        digests,
        key=lambda d: _parse_end_key(d.get("end")),
        reverse=True,
    )
    projects: list[str] = []
    seen: set[str] = set()
    for d in ordered:
        proj = str(d.get("project") or "").strip() or "—"
        if proj in seen:
            continue
        seen.add(proj)
        projects.append(proj)
        if len(projects) >= limit:
            break
    return projects


def _digests_for_project(
    digests: Sequence[Mapping[str, Any]], project: str
) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for d in digests:
        proj = str(d.get("project") or "").strip() or "—"
        if proj == project:
            out.append(d)
    out.sort(key=lambda d: _parse_end_key(d.get("end")), reverse=True)
    return out


def _commands_line(digests: Sequence[Mapping[str, Any]]) -> str | None:
    counts: dict[str, int] = {}
    order: list[str] = []
    for d in digests:
        cmds = d.get("commands_run")
        if not isinstance(cmds, list):
            continue
        for c in cmds:
            name = str(c or "").strip()
            if not name:
                continue
            head = name.split()[0]
            head = head.replace("\\", "/").rsplit("/", 1)[-1]
            if not head:
                continue
            if head not in counts:
                order.append(head)
                counts[head] = 0
            counts[head] += 1
    if not counts:
        return None
    ranked = sorted(counts.keys(), key=lambda k: (-counts[k], order.index(k)))
    return "よく使ったコマンド: " + ", ".join(ranked[:5])


def _files_line(digests: Sequence[Mapping[str, Any]]) -> str | None:
    seen: list[str] = []
    for d in digests:
        files = d.get("files_touched")
        if not isinstance(files, list):
            continue
        for f in files:
            name = str(f or "").strip()
            if name and name not in seen:
                seen.append(name)
            if len(seen) >= 5:
                break
        if len(seen) >= 5:
            break
    if not seen:
        return None
    return "触っていたファイル: " + ", ".join(seen)


def _last_reply_line(digests: Sequence[Mapping[str, Any]]) -> str | None:
    for d in digests:
        reply = d.get("last_reply_digest")
        if isinstance(reply, str) and reply.strip():
            flat = " ".join(reply.split())
            if not flat:
                continue
            return f"AI最後の返答（要旨）: {flat}"
    return None


def _unresolved_line(
    digests: Sequence[Mapping[str, Any]], *, tz: tzinfo | None = None
) -> str | None:
    for d in digests:
        if not d.get("ended_in_error"):
            continue
        end_hh = hhmm_from_iso(d.get("end"), tz)
        if end_hh:
            return f"未決着: {end_hh} 終了のセッションが末尾エラーのまま"
        return "未決着: セッションが末尾エラーのまま"
    return None


def _project_detail_lines(
    pd: Sequence[Mapping[str, Any]], *, tz: tzinfo | None = None
) -> list[str]:
    """project 配下に出せる行のみ。0行なら空。"""
    details: list[str] = []
    files = _files_line(pd)
    if files:
        details.append(f"  - {files}")
    cmds = _commands_line(pd)
    if cmds:
        details.append(f"  - {cmds}")
    reply = _last_reply_line(pd)
    if reply:
        details.append(f"  - {reply}")
    unresolved = _unresolved_line(pd, tz=tz)
    if unresolved:
        details.append(f"  - {unresolved}")
    return details


def _ai_project_lines(
    digests: Sequence[Mapping[str, Any]], *, tz: tzinfo | None = None
) -> list[str]:
    projects = _top_projects(digests, limit=2)
    if not projects:
        return []
    blocks: list[list[str]] = []
    for proj in projects:
        pd = _digests_for_project(digests, proj)
        details = _project_detail_lines(pd, tz=tz)
        if not details:
            # 配下に出せる行が無い project 見出しは出さない（設計原則8）
            continue
        blocks.append([f"- **{proj}**", *details])
    if not blocks:
        return []
    lines: list[str] = ["", "### AI セッションの続き"]
    for b in blocks:
        lines.extend(b)
    return lines


def _git_lines(stats: Mapping[str, Any]) -> list[str]:
    og = stats.get("outcome_git")
    if not isinstance(og, list) or not og:
        return []
    lines: list[str] = ["", "### git の状態"]
    any_line = False
    for item in og:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("repo_label") or "").strip()
        if not label:
            continue
        subjects = item.get("subjects")
        subj = ""
        if isinstance(subjects, list) and subjects:
            subj = str(subjects[0] or "").strip()
        parts: list[str] = []
        if subj:
            parts.append(subj)
        if "dirty" in item:
            dirty = item.get("dirty")
            if dirty is True:
                parts.append("未コミット変更あり")
            elif dirty is False:
                parts.append("未コミット変更なし")
        if not parts:
            lines.append(f"- {label}: （subject 欠測）")
        else:
            lines.append(f"- {label}: " + " / ".join(parts))
        any_line = True
    if not any_line:
        return []
    return lines


def _unchecked_kzn_line(actions_content: str | None) -> str | None:
    if not actions_content:
        return None
    ids: list[str] = []
    seen: set[str] = set()
    for line in actions_content.splitlines():
        stripped = line.strip()
        if not re.match(r"^-\s*\[\s*\]\s*", stripped):
            continue
        m = _KZN_ID_RE.search(stripped)
        if not m:
            continue
        kid = m.group(1)
        if kid not in seen:
            seen.add(kid)
            ids.append(kid)
    if not ids:
        return None
    return f"- 未チェック📌: {len(ids)}件（{', '.join(ids)}）"


def _resume_one_checked(existing_resume: str | None) -> bool:
    if not existing_resume:
        return False
    m = _RESUME_ONE_RE.search(existing_resume)
    if not m:
        return False
    return m.group(2).lower() == "x"


def _resume_one_line(
    digests: Sequence[Mapping[str, Any]], *, checked: bool
) -> str:
    mark = "x" if checked else " "
    # 再開手がかりが出せる project を優先（空見出し除外と同じ材料）
    clue_proj: str | None = None
    clue_detail: str | None = None
    for proj in _top_projects(digests, limit=5):
        pd = _digests_for_project(digests, proj)
        cmds = _commands_line(pd)
        files = _files_line(pd)
        if cmds:
            clue_proj = proj
            clue_detail = f"上記コマンド（{cmds.split(': ', 1)[-1]}）の再実行から"
            break
        if files:
            clue_proj = proj
            clue_detail = "触っていたファイルから状態を復元"
            break
        if _last_reply_line(pd) or _unresolved_line(pd):
            clue_proj = proj
            clue_detail = "セッション続きから状態を復元"
            break
    if clue_proj and clue_detail:
        clue = f"{clue_proj}: {clue_detail}"
    else:
        clue = "最終作業ブロックから状態を復元"
    return f"- [{mark}] 再開1手: {clue}（チェックは保持されます）"


def build_resume_section(
    stats: Mapping[str, Any] | None,
    *,
    prev_actions_content: str | None = None,
    existing_resume: str | None = None,
    tz: tzinfo | None = None,
) -> str | None:
    """前日 stats から resume 区間本文を組み立てる。データ無しなら None。"""
    if not isinstance(stats, Mapping):
        return None

    lines: list[str] = ["## ↩️ きのうの続きから", ""]
    body_count = 0

    last_block = _last_block_line(stats, tz=tz)
    if last_block:
        lines.append(last_block)
        body_count += 1

    digests = _session_digests(stats)
    ai_lines = _ai_project_lines(digests, tz=tz)
    if ai_lines:
        lines.extend(ai_lines)
        body_count += 1

    git_lines = _git_lines(stats)
    if git_lines:
        lines.extend(git_lines)
        body_count += 1

    unchecked = _unchecked_kzn_line(prev_actions_content)
    if unchecked:
        if body_count:
            lines.append("")
        lines.append(unchecked)
        body_count += 1

    if body_count == 0 and not digests:
        return None

    checked = _resume_one_checked(existing_resume)
    if body_count:
        lines.append("")
    lines.append(_resume_one_line(digests, checked=checked))

    return "\n".join(lines) + "\n"
