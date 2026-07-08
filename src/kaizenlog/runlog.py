"""実行ログ: 毎晩の無人実行が成功したか・失敗したかを記録し、後から確認できるようにする。

「静かな故障」（夜間タスクが失敗し続けているのに気づかない）を防ぐための仕組み。
ログは `.kaizenlog/logs/runs.jsonl` に1行1実行で追記し、保持期間を過ぎた行は
書き込み時に自動で間引く。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

RUNS_FILE = "runs.jsonl"


def _runs_path(logs_dir: Path) -> Path:
    return Path(logs_dir) / RUNS_FILE


def load_runs(logs_dir: Path) -> list[dict]:
    path = _runs_path(logs_dir)
    if not path.is_file():
        return []
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and "ts" in entry:
            runs.append(entry)
    return runs


def log_run(
    logs_dir: Path,
    command: str,
    ok: bool,
    duration_seconds: float,
    error: str | None = None,
    retention_days: int = 90,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    entry = {
        "ts": now.isoformat(),
        "command": command,
        "ok": ok,
        "duration_seconds": round(duration_seconds, 1),
    }
    if error:
        entry["error"] = str(error)[:500]

    cutoff = now - timedelta(days=retention_days)
    kept = []
    for run in load_runs(logs_dir):
        try:
            ts = datetime.fromisoformat(run["ts"])
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            kept.append(run)
    kept.append(entry)

    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    _runs_path(logs_dir).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n",
        encoding="utf-8",
    )


def _fmt_ts(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def render_status(runs: list[dict]) -> str:
    """コマンド別の最終成功・直近の失敗を人間向けにまとめる。"""
    if not runs:
        return "実行履歴はまだありません。`kaizenlog run` を実行すると記録が始まります。"

    lines = ["# KaizenLog 実行状況", ""]
    commands = sorted({r.get("command", "?") for r in runs})
    for cmd in commands:
        cmd_runs = [r for r in runs if r.get("command") == cmd]
        last = cmd_runs[-1]
        last_ok = next((r for r in reversed(cmd_runs) if r.get("ok")), None)
        mark = "✅" if last.get("ok") else "❌"
        lines.append(f"## {cmd}")
        lines.append(f"- 直近の実行: {mark} {_fmt_ts(last['ts'])}"
                     f"（{last.get('duration_seconds', '?')}秒）")
        if last_ok:
            lines.append(f"- 最後に成功: {_fmt_ts(last_ok['ts'])}")
        else:
            lines.append("- 最後に成功: なし（一度も成功していません）")
        if not last.get("ok") and last.get("error"):
            lines.append(f"- 直近のエラー: {last['error']}")
        lines.append("")

    failures = [r for r in runs if not r.get("ok")][-5:]
    if failures:
        lines.append("## 直近の失敗（最大5件）")
        for r in failures:
            lines.append(f"- {_fmt_ts(r['ts'])} {r.get('command')}: {r.get('error', '')}")
    return "\n".join(lines)
