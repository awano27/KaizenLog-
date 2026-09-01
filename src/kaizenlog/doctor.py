"""kaizenlog doctor: セットアップと環境の健全性を一発診断する。

導入時のつまずきと障害時の切り分けを1コマンドで行う。
出力は ✅（正常）/ ⚠️（動くが注意）/ ❌（要修正）の3段階。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import Config, default_config_path, existing_config_candidates, find_config_file
from .collector import classify_input_bucket_health
from .memory import MEMORY_FILE
from .reliability import QualityState
from .runlog import consecutive_bad_advise_outcomes, load_runs
from .setup_detect import query_task_registered


class Check:
    def __init__(self):
        self.lines: list[str] = []
        self.has_error = False

    def ok(self, msg: str) -> None:
        self.lines.append(f"✅ {msg}")

    def warn(self, msg: str) -> None:
        self.lines.append(f"⚠️  {msg}")

    def error(self, msg: str) -> None:
        self.lines.append(f"❌ {msg}")
        self.has_error = True


def _check_config(c: Check, config_path: str | None) -> None:
    try:
        found = find_config_file(config_path)
    except FileNotFoundError as e:
        c.error(str(e))
        return
    if found:
        c.ok(f"設定ファイル: {found}")
        # 複数の設定ファイルが存在すると、実行時のカレントディレクトリ次第で
        # 別の設定が選ばれる（例: 手動実行はリポジトリの kaizenlog.toml、
        # 夜間タスクは %APPDATA% の config.toml）。内容がズレていると
        # 「手動では正常なのに夜間だけ別ボールトに書く」事故になるため警告する。
        if not config_path:
            app = default_config_path()
            # CWD の設定だけ使っていて AppData/XDG が無い → 夜間タスクとズレやすい
            if not app.is_file() and found.name in ("kaizenlog.toml", "config.toml"):
                try:
                    if found.resolve() != app.resolve():
                        c.warn(
                            f"作業ディレクトリの設定を使用中（推奨の AppData/XDG には未配置）: {found}。"
                            f"`kaizenlog setup` で {app} へ移行してください"
                        )
                except OSError:
                    pass
            others = [p for p in existing_config_candidates() if p.resolve() != found.resolve()]
            if others:
                c.warn("他の場所にも設定ファイルがあります（現在は無視）: "
                       + ", ".join(str(p) for p in others)
                       + " — 作業ディレクトリが異なる実行（夜間タスク等）では"
                         "そちらが使われる可能性があります。内容を同期するか片方を削除してください。"
                         " 修復: kaizenlog setup")
    else:
        c.error(
            "設定ファイルがありません。まず `kaizenlog setup` を実行してください"
            "（診断のみはこの doctor で続行します）"
        )


def _check_vault(c: Check, cfg: Config) -> None:
    vault = Path(cfg.vault_dir).expanduser()
    if not vault.is_dir():
        c.error(f"ボールトが存在しません: {vault}（config.tomlのvault_dirを確認）")
        return
    if not os.access(vault, os.W_OK):
        c.error(f"ボールトに書き込めません: {vault}")
        return
    c.ok(f"ボールト書き込み可: {vault}")
    if cfg.daily_notes_path.is_dir():
        c.ok(f"デイリーノート: {cfg.daily_notes_path}")
    else:
        c.warn(f"デイリーノートのフォルダがありません（初回実行時に作成されます）: {cfg.daily_notes_path}")


def _check_activitywatch(c: Check, cfg: Config) -> None:
    try:
        r = requests.get(f"{cfg.aw_base_url}/api/0/buckets/", timeout=10)
        r.raise_for_status()
        buckets = r.json()
    except requests.RequestException as e:
        c.error(f"ActivityWatch ({cfg.aw_base_url}) に接続できません: {e.__class__.__name__}。"
                "起動しているか確認してください。"
                " 修復: kaizenlog setup （または ActivityWatch を起動）")
        return
    types = {info.get("type") for info in buckets.values()}
    c.ok(f"ActivityWatch 応答あり（バケット{len(buckets)}個）")
    if "currentwindow" in types:
        c.ok("ウィンドウwatcher検出（aw-watcher-window）")
    else:
        c.error("ウィンドウwatcherが見つかりません。aw-watcher-windowの動作を確認してください")
    if "afkstatus" in types:
        c.ok("AFK watcher検出（離席時間を除外できます）")
    else:
        c.warn("AFK watcherが見つかりません（離席中の時間も計上されます）")
    if "web.tab.current" in types:
        c.ok("ブラウザwatcher検出（aw-watcher-web、サイト単位の分類が有効）")
    else:
        c.warn("ブラウザwatcher未導入（サイト別集計・site_minutes指標は無効）。"
               "ブラウザに aw-watcher-web 拡張を入れると有効になります")
    input_state, input_reason, _ = classify_input_bucket_health(
        buckets, now=datetime.now(timezone.utc)
    )
    if input_state is QualityState.OBSERVED:
        c.ok(
            "入力watcher検出（aw-watcher-input、集中ブロック指標が有効）"
            f"（{input_reason.value}）"
        )
    elif input_state is QualityState.STALE:
        c.warn(
            "入力watcherの更新が古いため集中ブロック・focus_blocks指標は無効です"
            f"（{input_reason.value}）"
        )
    elif input_state is QualityState.UNKNOWN:
        c.warn(
            "入力watcherの更新時刻を確認できないため集中ブロック・focus_blocks指標は不明です"
            f"（{input_reason.value}）"
        )
    else:
        c.warn(
            "入力watcher未導入（集中ブロック・focus_blocks指標は無効）。"
            "`pipx install aw-watcher-input` → `aw-watcher-input` 起動で有効になります"
            f"（{input_reason.value}）"
        )


def _list_api_models(base_url: str, headers: dict) -> list[str] | None:
    """OpenAI互換APIのモデル一覧を取得する。取得・解釈できなければ None。"""
    try:
        r = requests.get(f"{base_url}/models", headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
        return [i for i in ids if i] or None
    except (requests.RequestException, ValueError):
        return None


def _check_openai_compatible(c: Check, llm, *, as_fallback: bool, essential: bool = True) -> None:
    """openai-compatible（Ollama / GitHub Models等）の接続とモデル存在を確認する。

    essential=False は「主バックエンドが健在で、これは予備経路」の場合。
    そのときだけ問題を警告に留める（主が欠けた状態で予備も死んでいたらエラー）。
    """
    label = "フォールバック先ローカルLLM" if as_fallback else "LLM API"
    report_problem = c.error if essential else c.warn
    headers = {}
    api_key = os.environ.get(llm.api_key_env, "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        if not as_fallback:
            c.ok(f"APIキー検出（環境変数 {llm.api_key_env}）")
    elif not as_fallback:
        c.warn(f"環境変数 {llm.api_key_env} が未設定（Ollamaなら不要、クラウドAPIなら必須）")

    try:
        r = requests.get(f"{llm.base_url}/models", headers=headers, timeout=15)
        # 4xxも「不健康」。特に401/403（APIキー切れ）を✅と誤診すると、
        # 夜間のadviseが毎晩失敗するのにdoctorは正常と言い張ることになる
        if r.status_code in (401, 403):
            report_problem(f"{label} が認証エラー (HTTP {r.status_code})。"
                           f"環境変数 {llm.api_key_env} のAPIキーを確認してください")
            return
        if r.status_code >= 400:
            report_problem(f"{label} がエラー応答: HTTP {r.status_code}"
                           "（base_url の設定を確認してください）")
            return
    except requests.RequestException as e:
        report_problem(f"{label} ({llm.base_url}) に接続できません: {e.__class__.__name__}。"
                       "Ollamaの場合は起動を確認してください")
        return

    # 設定されたモデルが実際に利用可能かまで確認する（未pullのモデル指定を実行前に検出）
    models = _list_api_models(llm.base_url, headers)
    if models is not None and llm.model not in models:
        candidates = ", ".join(sorted(models)[:5])
        report_problem(f"{label}: モデル '{llm.model}' がありません（利用可能: {candidates} 等）。"
                       f"`ollama pull {llm.model}` するか config の model を変更してください")
        return
    c.ok(f"{label} 応答あり: {llm.base_url}（model: {llm.model}）")


def _check_llm(c: Check, cfg: Config) -> None:
    llm = cfg.llm
    if llm.backend == "none":
        c.warn("LLMバックエンド: none（改善提案・日報のLLMモードは無効）")
        return

    cli_checks = {
        "copilot-cli": ("Copilot CLI", llm.copilot_command,
                        "`npm install -g @github/copilot` 後、新しいシェルで再確認してください"),
        "claude-code-cli": ("Claude Code CLI", llm.claude_command,
                            "https://claude.com/claude-code からインストールしてください"),
    }
    if llm.backend in cli_checks:
        name, command, hint = cli_checks[llm.backend]
        path = shutil.which(command)
        if path:
            c.ok(f"{name} 検出: {path}")
            if llm.backend == "claude-code-cli":
                _check_claude_auth(c, path)
        else:
            # フォールバックが効く場合は起動不能ではないため警告に留める
            report = c.warn if llm.fallback_to_local else c.error
            report(f"{name} ('{command}') が見つかりません。{hint}")
    elif llm.backend == "openai-compatible":
        _check_openai_compatible(c, llm, as_fallback=False)
        return
    else:
        c.error(f"不明なLLMバックエンド: {llm.backend}")
        return

    if llm.fallback_to_local:
        # 主CLIが欠けているなら予備経路が生命線 → その障害はエラー扱い
        _check_openai_compatible(c, llm, as_fallback=True, essential=(path is None))


def _check_claude_auth(c: Check, path: str) -> None:
    """Read Claude auth state without exposing its CLI response."""
    try:
        result = subprocess.run(
            [path, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        c.warn("Claude Code 認証状態: provider_probe_timeout")
        return
    except OSError:
        c.warn("Claude Code 認証状態: provider_probe_unknown")
        return
    if result.returncode == 1:
        c.error("Claude Code 認証状態: provider_auth_required")
        return
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        c.warn("Claude Code 認証状態: provider_probe_unknown")
        return
    if not bool(payload.get("loggedIn")):
        c.error("Claude Code 認証状態: provider_auth_required")
        return
    c.ok("Claude Code 認証状態: ok")


def _check_screenpipe(c: Check, cfg: Config) -> None:
    """screenpipe: disabled / OK / unreachable（終了コードには影響しない）。"""
    sp = getattr(cfg, "screenpipe", None)
    if sp is None or not bool(getattr(sp, "enabled", False)):
        c.ok("screenpipe: disabled")
        return
    from .screenpipe_source import (
        ScreenpipeClient,
        is_localhost_url,
        resolve_api_key,
    )

    base = str(getattr(sp, "base_url", "") or "")
    if not is_localhost_url(base):
        c.warn("screenpipe: disabled（base_url が localhost 以外）")
        return
    key = resolve_api_key(str(getattr(sp, "api_key_env", "") or ""))
    client = ScreenpipeClient(
        base,
        api_key=key,
        timeout_seconds=float(getattr(sp, "timeout_seconds", 3.0) or 3.0),
    )
    health = client.health()
    if health is None:
        c.warn("screenpipe: unreachable（enabled だが応答なし）")
        return
    if not key:
        c.warn(
            "screenpipe: 認証未設定"
            f"（{getattr(sp, 'api_key_env', 'SCREENPIPE_API_KEY')} を確認）"
            " — /health のみ成功"
        )
        return
    # 軽い search で 403 を検出（空クエリ相当・短い窓）
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(getattr(cfg, "timezone", "Asia/Tokyo") or "Asia/Tokyo")
    end = datetime.now(tz)
    start = end - timedelta(minutes=5)
    client.search_text(None, start, end, limit=1)
    if client.last_warning and "認証" in client.last_warning:
        c.warn("screenpipe: 認証エラー（SCREENPIPE_API_KEY を確認）")
        return
    version = health.get("version") or "?"
    last = health.get("last_frame_timestamp")
    ago = ""
    if isinstance(last, str) and last:
        try:
            from datetime import datetime as dt

            ts = dt.fromisoformat(last.replace("Z", "+00:00"))
            mins = max(0, int((datetime.now(tz) - ts.astimezone(tz)).total_seconds() // 60))
            ago = f"・最終フレーム: {mins}分前"
        except Exception:
            ago = ""
    c.ok(f"screenpipe: OK（version {version}{ago}）")


def _check_aiwork(c: Check, cfg: Config) -> None:
    if not cfg.aiwork.enabled:
        c.warn("AI Work Telemetry: 無効（[aiwork] enabled = false）")
        return
    # アダプタごとの検出状況（未使用ソースは ➖ で案内）
    claude = Path(cfg.aiwork.claude_projects_dir).expanduser()
    if claude.is_dir():
        # プロジェクト直下のディレクトリ数を概算（ファイル全走査は重い）
        try:
            proj_count = sum(1 for p in claude.iterdir() if p.is_dir())
        except OSError:
            proj_count = 0
        c.ok(f"claude-code: {claude}（プロジェクト約{proj_count}件）")
    else:
        c.warn(f"➖ claude-code: {claude} が見つかりません（未使用なら問題なし）")

    codex = Path(cfg.aiwork.codex_sessions_dir).expanduser()
    if codex.is_dir():
        try:
            # 日付ディレクトリの有無だけ軽く確認
            day_dirs = sum(1 for _ in codex.rglob("rollout-*.jsonl"))
        except OSError:
            day_dirs = 0
        c.ok(f"codex: {codex}（ロールアウト約{day_dirs}件）")
    else:
        c.warn(f"➖ codex: {codex} が見つかりません（未使用なら問題なし）")


def _check_history(c: Check, cfg: Config) -> None:
    stats_files = sorted(cfg.stats_path.glob("*.json")) if cfg.stats_path.is_dir() else []
    if stats_files:
        c.ok(f"日次統計: {len(stats_files)}日分（最新: {stats_files[-1].stem}）")
    else:
        c.warn("日次統計がまだありません（`kaizenlog generate` で蓄積が始まります）")

    runs = load_runs(cfg.logs_path)
    if not runs:
        c.warn("実行履歴がまだありません")
        return
    last = runs[-1]
    if last.get("ok") and last.get("partial"):
        note = last.get("note") or ""
        extra = f" — {note}" if note else ""
        c.warn(
            f"直近の実行は部分成功: {last.get('command')} @ {last.get('ts', '')[:16]}"
            f"{extra}（詳細は `kaizenlog status`）"
        )
    elif last.get("ok"):
        c.ok(f"直近の実行: 成功（{last.get('command')} @ {last.get('ts', '')[:16]}）")
    else:
        c.error(f"直近の実行が失敗しています: {last.get('command')} — "
                f"{last.get('error', '')[:120]}（詳細は `kaizenlog status`）")


def _check_advise_health(c: Check, cfg: Config) -> None:
    """縮退/失敗の連続を検知（ヘルスレジャー）。"""
    runs = load_runs(cfg.logs_path)
    n = consecutive_bad_advise_outcomes(runs)
    if n >= 2:
        c.error(
            f"提案が連続して縮退しています（{n}回連続）。"
            "LLM出力が契約に合っていません（詳細は `kaizenlog status`）"
        )
    elif n == 1:
        c.warn(
            "直近の提案が縮退または失敗しました。"
            "連続する場合は LLM / プロンプトを確認してください"
        )
    else:
        c.ok("提案ヘルス: 縮退の連続なし")


# タスクスケジューラ名（scripts/register-task.ps1 / setup と一致）
TASK_NIGHTLY = "KaizenLog Daily"
TASK_MORNING = "KaizenLog Morning"


def _check_schedule(c: Check) -> None:
    """夜間/朝タスクの登録。消失=データ欠測につながるため doctor で検出する。"""
    nightly = query_task_registered(TASK_NIGHTLY)
    morning = query_task_registered(TASK_MORNING)
    if nightly is None and morning is None:
        c.ok("スケジュールタスク: 検出をスキップ（非 Windows または schtasks 利用不可）")
        return
    if nightly is True:
        c.ok(f"夜間タスク登録済み: {TASK_NIGHTLY}")
    elif nightly is False:
        c.error(
            "夜間タスクが未登録です。毎晩の記録が自動で走りません: "
            "`kaizenlog setup --register-task`"
        )
    else:
        # ✅表示だと「登録済み」と誤読されるため warn で手動確認を促す
        c.warn(f'夜間タスク: 検出できませんでした — 手動確認: schtasks /Query /TN "{TASK_NIGHTLY}"')

    if morning is True:
        c.ok(f"朝タスク登録済み: {TASK_MORNING}")
    elif morning is False:
        c.warn(
            f"朝タスク（{TASK_MORNING}）が未登録です。"
            "朝の追いつき・通知が自動で走りません: `kaizenlog setup --register-task`"
        )
    else:
        c.warn(f'朝タスク: 検出できませんでした — 手動確認: schtasks /Query /TN "{TASK_MORNING}"')


def _count_jsonl_entries(path: Path) -> tuple[int, int]:
    """(valid_or_any lines with content, unparseable lines)."""
    ok = 0
    bad = 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            json.loads(s)
            ok += 1
        except json.JSONDecodeError:
            bad += 1
    return ok, bad


def _check_artifacts(c: Check, cfg: Config) -> None:
    """第10弾以降の成果物パスの存在・概況（無い=即異常ではない）。"""
    # Memory
    mem_file = cfg.memory_path / MEMORY_FILE
    if mem_file.is_file():
        n_ok, n_bad = _count_jsonl_entries(mem_file)
        c.ok(f"Kaizen Memory: {mem_file}（{n_ok}行）")
        if n_bad:
            c.warn(f"Kaizen Memory にパース不能行が {n_bad} 件あります: {mem_file}")
    elif cfg.memory_path.is_dir():
        c.warn(
            f"Kaizen Memory ディレクトリはあるが {MEMORY_FILE} がありません"
            f"（初回 advise 後に作成）: {cfg.memory_path}"
        )
    else:
        c.warn(
            f"Kaizen Memory がまだありません（初回 advise で作成）: {cfg.memory_path}"
        )

    # Experiments
    exp = cfg.experiments_path
    if exp.is_dir():
        try:
            n_notes = sum(
                1
                for p in exp.rglob("*.md")
                if p.is_file() and not p.name.startswith(".")
            )
        except OSError:
            n_notes = 0
        c.ok(f"実験ノート: {exp}（{n_notes}件）")
    else:
        c.warn(f"実験ディレクトリがありません（任意）: {exp}")

    # Weekly Reviews（任意機能 → 無くても info 相当の warn ではなく軽い案内）
    weekly = cfg.daily_notes_path / "Weekly Reviews"
    if weekly.is_dir():
        try:
            notes = sorted(
                (p for p in weekly.glob("*.md") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            notes = []
        if notes:
            c.ok(f"Weekly Reviews: 直近 {notes[0].name}（計{len(notes)}件）")
        else:
            c.ok(f"Weekly Reviews フォルダあり（ノートはまだ無し）: {weekly}")
    else:
        c.ok(f"Weekly Reviews: 未作成（任意機能）: {weekly}")

    # browser_export_dir: 拡張未導入は正常 → error にしない
    if cfg.aiwork.enabled:
        raw = str(cfg.aiwork.browser_export_dir).strip()
        # 既定値との比較で「明示設定なのに無い」だけを warn にする
        # （既定のまま未導入のユーザーに恒常⚠️を出さない）
        default_dir = type(cfg.aiwork)().browser_export_dir
        if not raw:
            # 空文字は Path("") == Path(".") に化けて偽✅になるため先に弾く
            c.ok("ブラウザ AI エクスポート: 未設定（任意機能）")
        else:
            bdir = Path(raw).expanduser()
            if bdir.is_dir():
                c.ok(f"ブラウザ AI エクスポート: {bdir}")
            elif raw == default_dir:
                c.ok(f"ブラウザ AI エクスポート: 未導入（任意機能）: {bdir}")
            else:
                c.warn(f"browser_export_dir が明示設定されていますが存在しません: {bdir}")


def _check_guard(c: Check, cfg: Config) -> None:
    """空転ブレーカー: 状態 dir 書き込み可 / settings フック登録の有無。

    mkdir しない（読み取り専用診断）。未作成は情報表示のみ。
    """
    try:
        from .guard import state_dir, _is_kaizenlog_guard_command
    except Exception as e:
        c.warn(f"guard モジュールを読み込めません: {e}")
        return
    # 状態ディレクトリ（存在時のみ書き込み可否。mkdir しない）
    d = state_dir()
    if not d.exists():
        c.ok(
            f"guard 状態ディレクトリ: 未作成（guard 初回実行時に作成）: {d}"
        )
    elif not d.is_dir():
        c.warn(f"guard 状態パスがディレクトリではありません: {d}")
    else:
        try:
            if os.access(d, os.W_OK):
                c.ok(f"guard 状態ディレクトリ: 書き込み可 ({d})")
            else:
                c.warn(f"guard 状態ディレクトリに書けません: {d}")
        except OSError as e:
            c.warn(f"guard 状態ディレクトリを確認できません: {e}")
    # settings.json フック
    candidates = [
        Path.cwd() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.json",
    ]
    found = False
    for sp in candidates:
        if not sp.is_file():
            continue
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(hooks, dict):
            continue
        for _ev, blocks in hooks.items():
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                for h in block.get("hooks") or []:
                    if isinstance(h, dict) and _is_kaizenlog_guard_command(
                        str(h.get("command") or "")
                    ):
                        found = True
                        break
        if found:
            c.ok(f"guard フック登録: あり ({sp})")
            break
    if not found:
        c.warn(
            "guard フック未登録（任意）。"
            "`kaizenlog guard install --write --project` で UserPromptSubmit/Stop に登録可"
            "（PostToolUse は登録しない）"
        )


def run_doctor(
    cfg: Config,
    config_path: str | None = None,
    *,
    config_absent: bool = False,
    missing_config_message: str | None = None,
) -> tuple[str, bool]:
    """全チェックを実行し、(レポート文字列, エラー有無) を返す。

    config_absent / missing_config_message 指定時は設定エラーを必須とし、
    CWD をボールトとして正常判定しない。設定依存チェックはスキップする。
    """
    c = Check()
    if missing_config_message:
        c.error(missing_config_message)
    elif config_absent:
        c.error(
            "設定ファイルがありません。まず `kaizenlog setup` を実行してください"
        )
    else:
        _check_config(c, config_path)

    if config_absent or missing_config_message:
        # 設定なし: 書き込み可能ボールト判定はしない（CWD 誤認防止）
        c.warn(
            "ボールト / LLM / AIテレメトリ / 履歴 / 提案ヘルス / 成果物: "
            "設定作成後に確認"
        )
        # 環境だけは既定 URL で確認（設定不要の意味ある診断）
        _check_activitywatch(c, cfg)
        # タスク登録は設定に依存しない
        _check_schedule(c)
    else:
        _check_vault(c, cfg)
        _check_activitywatch(c, cfg)
        _check_schedule(c)
        _check_llm(c, cfg)
        _check_aiwork(c, cfg)
        _check_screenpipe(c, cfg)
        _check_history(c, cfg)
        _check_advise_health(c, cfg)
        _check_artifacts(c, cfg)
        _check_guard(c, cfg)
    verdict = "\n❌ 修正が必要な項目があります。" if c.has_error else "\n✅ すべて正常です。"
    return "\n".join(c.lines) + verdict, c.has_error
