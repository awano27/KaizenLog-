"""実行ログ: 毎晩の無人実行が成功したか・失敗したかを記録し、後から確認できるようにする。

「静かな故障」（夜間タスクが失敗し続けているのに気づかない）を防ぐための仕組み。
ログは `.kaizenlog/logs/runs.jsonl` に1行1実行で追記し、保持期間を過ぎた行は
書き込み時に自動で間引く。
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .vault import atomic_write_text

RUNS_FILE = "runs.jsonl"

# advise 品質レジャー（ヘルス）の outcome
ADVISE_OUTCOMES = frozenset({"ok", "repaired", "degraded", "failed"})
ADVISE_HEALTH_COMMAND = "advise_health"
_BAD_OUTCOMES = frozenset({"degraded", "failed"})


def _runs_path(logs_dir: Path) -> Path:
    return Path(logs_dir) / RUNS_FILE


def load_runs(logs_dir: Path) -> list[dict]:
    path = _runs_path(logs_dir)
    if not path.is_file():
        return []
    runs = []
    # errors="replace": 途中で切れた書き込み等の不正バイトが1行あるだけで
    # 全実行ログ（=status/doctor/毎晩のlog_run）が壊れないようにする。
    # 化けた行は下のJSONパースで自然に間引かれる
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
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


def _append_run_entry(
    logs_dir: Path,
    entry: dict,
    retention_days: int = 90,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    if "ts" not in entry:
        entry = {**entry, "ts": now.isoformat()}
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
    atomic_write_text(
        _runs_path(logs_dir),
        "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n",
    )


def log_run(
    logs_dir: Path,
    command: str,
    ok: bool,
    duration_seconds: float,
    error: str | None = None,
    retention_days: int = 90,
    now: datetime | None = None,
    *,
    notify_failed: bool = False,
    note: str | None = None,
    partial: bool = False,
) -> None:
    now = now or datetime.now(timezone.utc)
    entry: dict = {
        "ts": now.isoformat(),
        "command": command,
        "ok": ok,
        "duration_seconds": round(duration_seconds, 1),
    }
    if error:
        entry["error"] = str(error)[:500]
    if notify_failed:
        entry["notify_failed"] = True
    if note:
        entry["note"] = str(note)[:500]
    if partial:
        entry["partial"] = True
    _append_run_entry(logs_dir, entry, retention_days=retention_days, now=now)


def classify_violation_kind(message: str) -> str:
    """契約違反メッセージを短い種別タグに落とす（本文・提案は残さない）。"""
    m = message or ""
    # 特異的な needle を先に評価する。
    # 「機械構文として解析できません」は json の "解析できません" より前に判定する。
    rules = (
        (
            "pass_not_machine_readable",
            (
                "機械構文",
                "自由文は自動判定",
                "自動判定できず",
                "機械構文として解析できません",
            ),
        ),
        # JSON 破損: 一般の「解析できません」は残すが、機械構文文面は上で捕捉済み
        ("json", ("JSON", "json", "解析できません", "閉じていません")),
        ("cardinality", ("1対1", "件数", "1〜3", "1〜2")),
        ("notification", ("通知",)),
        ("previous_day", ("前日",)),
        ("ai_metrics", ("AI会話", "依頼方法")),
        ("watcher", ("watcher", "ブラウザ実測")),
        ("category", ("カテゴリ", "存在しません")),
        ("site", ("サイト", "観測されていません")),
        ("pass_fail", ("PASS", "FAIL", "数値条件", "指標名")),
        ("heading", ("見出し",)),
        ("kzn", ("KZN",)),
        ("semantic", ("変換しています", "娯楽・私用", "測定不能")),
    )
    for kind, needles in rules:
        if any(n in m for n in needles):
            return kind
    return "contract"


def log_advise_health(
    logs_dir: Path,
    *,
    day: date | str,
    backend: str,
    outcome: str,
    duration_seconds: float,
    violations: list[str] | None = None,
    retention_days: int = 90,
    now: datetime | None = None,
) -> None:
    """advise 品質レジャーを追記する。

    violations は種別タグのみ（プロンプト・提案本文を残さない — プライバシーと
    ロック画面対策の既存原則。記録失敗は呼び出し側で握り潰すこと）。
    """
    if outcome not in ADVISE_OUTCOMES:
        outcome = "failed"
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    kinds = [classify_violation_kind(v) for v in (violations or [])]
    # 重複タグは順序保持で一意化
    seen: set[str] = set()
    uniq_kinds: list[str] = []
    for k in kinds:
        if k not in seen:
            seen.add(k)
            uniq_kinds.append(k)
    entry = {
        "ts": (now or datetime.now(timezone.utc)).isoformat(),
        "command": ADVISE_HEALTH_COMMAND,
        "ok": outcome in ("ok", "repaired"),
        "duration_seconds": round(float(duration_seconds), 1),
        "date": day_s,
        "backend": str(backend or ""),
        "outcome": outcome,
        # 本文は載せない（種別タグのみ）
        "violations": uniq_kinds,
    }
    _append_run_entry(logs_dir, entry, retention_days=retention_days, now=now)


def advise_health_records(runs: list[dict]) -> list[dict]:
    """advise_health 行だけを date/ts 昇順で返す。"""
    rows = [r for r in runs if r.get("command") == ADVISE_HEALTH_COMMAND]
    def sort_key(r: dict):
        return (str(r.get("date") or ""), str(r.get("ts") or ""))
    return sorted(rows, key=sort_key)


def consecutive_bad_advise_outcomes(runs: list[dict]) -> int:
    """直近の advise_health から degraded/failed の連続回数（最新から遡る）。

    途中に ok/repaired があればそこでリセット（連続が切れる）。
    """
    rows = advise_health_records(runs)
    if not rows:
        return 0
    n = 0
    for r in reversed(rows):
        outcome = r.get("outcome")
        if outcome in _BAD_OUTCOMES:
            n += 1
        else:
            break
    return n


def last_advise_health(runs: list[dict]) -> dict | None:
    rows = advise_health_records(runs)
    return rows[-1] if rows else None


def command_duration_stats(
    runs: list[dict], command: str
) -> tuple[float | None, float | None]:
    """コマンド別 duration_seconds の (中央値, 最大)。データ無ければ (None, None)。"""
    vals = [
        float(r["duration_seconds"])
        for r in runs
        if r.get("command") == command
        and isinstance(r.get("duration_seconds"), (int, float))
    ]
    if not vals:
        return None, None
    return float(statistics.median(vals)), float(max(vals))


def advise_health_warning_line(runs: list[dict]) -> str | None:
    """昨夜が縮退/失敗のときだけ出す1行（本文・違反詳細は載せない）。"""
    last = last_advise_health(runs)
    if last is None or last.get("outcome") not in _BAD_OUTCOMES:
        return None
    n = consecutive_bad_advise_outcomes(runs)
    if n <= 0:
        return None
    return f"⚠ 昨夜の提案は縮退しました（{n}日連続）"


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
        last_ok = next(
            (r for r in reversed(cmd_runs) if r.get("ok") and not r.get("partial")),
            None,
        )
        # partial は ok=True でも成功扱いしない（追いつき未完了など）
        if last.get("partial"):
            mark = "⚠"
        elif last.get("ok"):
            mark = "✅"
        else:
            mark = "❌"
        lines.append(f"## {cmd}")
        lines.append(f"- 直近の実行: {mark} {_fmt_ts(last['ts'])}"
                     f"（{last.get('duration_seconds', '?')}秒）")
        if last.get("partial") and last.get("note"):
            lines.append(f"- 部分成功: {last['note']}")
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

    if any(r.get("notify_failed") for r in runs):
        lines.append("")
        lines.append(
            "⚠ 失敗通知の送出に失敗した記録があります。"
            "通知経路を doctor で確認してください。"
        )

    # ヘルス: advise 品質レジャー
    last_h = last_advise_health(runs)
    if last_h is not None:
        lines.append("")
        lines.append("## 提案ヘルス")
        outcome = last_h.get("outcome", "?")
        bad_n = consecutive_bad_advise_outcomes(runs)
        lines.append(
            f"- 直近 advise: outcome={outcome}"
            f"（{last_h.get('date', '?')} / {last_h.get('backend', '?')}）"
        )
        if bad_n > 0:
            lines.append(f"- 縮退/失敗の連続: {bad_n}回")
        med, _mx = command_duration_stats(runs, ADVISE_HEALTH_COMMAND)
        last_dur = last_h.get("duration_seconds")
        if (
            isinstance(med, (int, float))
            and isinstance(last_dur, (int, float))
            and med > 0
            and float(last_dur) > float(med) * 2
        ):
            lines.append(
                f"- ⚠ 実行時間が悪化（直近 {last_dur}s / 中央値 {med:.1f}s。"
                "LLMタイムアウト接近の可能性）"
            )
    return "\n".join(lines)
