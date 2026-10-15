"""Setup wizard orchestration (detect → config → optional side effects → doctor)."""
from __future__ import annotations

import re
import inspect
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as day_time
from importlib import resources
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import setup_detect
from .collector import ActivityWatchClient, ActivityWatchError
from .config import Config, ConfigError, default_config_path, load_config, write_config_file
from .doctor import run_doctor
from .skill_manager import bundled_skill_names, install_skill
from .setup_detect import find_aw_exe, probe_aw_api

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def validate_hhmm(value: str, field: str = "時刻") -> str:
    """HH:MM (0-23:00-59) を検証して正規化する。"""
    s = (value or "").strip()
    if not _HHMM_RE.match(s):
        raise ConfigError(f"{field} は HH:MM 形式で指定してください（例 08:30）: {value!r}")
    h, m = s.split(":")
    return f"{int(h):02d}:{int(m):02d}"

# Re-export for monkeypatch paths used by tests (kaizenlog.setup.run_doctor).
__all__ = [
    "SetupOptions",
    "SetupUI",
    "ConsoleUI",
    "run_setup",
    "try_winget_install_aw",
    "start_aw_and_wait",
    "install_all_skills",
    "register_daily_task",
    "register_weekly_task",
    "bounded_backfill_days",
    "generate_initial_journal",
    "backfill_after_confirmation",
    "oldest_window_event_day",
    "preview_initial_setup",
    "save_initial_setup",
    "run_doctor",
]


@dataclass
class SetupOptions:
    config_path: Path | None = None
    vault: Path | None = None
    yes: bool = False
    force: bool = False
    skip_aw: bool = False
    skip_task: bool = False
    skip_skills: bool = False
    install_aw: bool = False
    register_task: bool = False
    time: str = "21:30"
    morning_time: str = "08:30"  # 空なら朝タスク未登録
    aw_base_url: str = "http://localhost:5600"
    ollama_base_url: str = "http://localhost:11434/v1"
    daily_notes_dir: str | None = None
    daily_note_format: Literal["YYYY-MM-DD", "YYYYMMDD", "YYYY/MM/DD"] = "YYYY-MM-DD"
    backend: str = "none"
    model: str = ""
    # 初回日誌生成は明示指定時だけ行う。旧呼び出し元との互換性のため
    # 既定値は False に固定する。
    generate_today: bool = False


class SetupUI(Protocol):
    def print(self, msg: str = "") -> None: ...

    def confirm(self, msg: str, default: bool = True) -> bool: ...

    def choose(self, msg: str, options: list[str], default: int = 0) -> int: ...

    def ask_path(self, msg: str) -> Path: ...


class ConsoleUI:
    """stdin-based UI. Non-TTY + needed input → SystemExit with guidance."""

    def __init__(self, yes: bool = False) -> None:
        self.yes = yes

    def print(self, msg: str = "") -> None:
        print(msg)

    def _require_tty(self) -> None:
        if not sys.stdin.isatty():
            raise SystemExit(
                "対話入力が必要です。--yes / --vault 等のフラグを指定するか、"
                "ターミナルから実行してください。"
            )

    def confirm(self, msg: str, default: bool = True) -> bool:
        if self.yes:
            return default
        self._require_tty()
        hint = "Y/n" if default else "y/N"
        raw = input(f"{msg} [{hint}]: ").strip().lower()
        if not raw:
            return default
        return raw in ("y", "yes")

    def choose(self, msg: str, options: list[str], default: int = 0) -> int:
        if self.yes:
            return default
        self._require_tty()
        self.print(msg)
        for i, opt in enumerate(options):
            mark = " (default)" if i == default else ""
            self.print(f"  [{i}] {opt}{mark}")
        raw = input(f"番号を選択 [{default}]: ").strip()
        if not raw:
            return default
        try:
            idx = int(raw)
        except ValueError:
            return default
        if 0 <= idx < len(options):
            return idx
        return default

    def ask_path(self, msg: str) -> Path:
        self._require_tty()
        raw = input(f"{msg}: ").strip().strip('"')
        return Path(raw).expanduser()


# --- Side-effect helpers (monkeypatch targets for tests) ---

WINGET_AW_IDS = ("ActivityWatch.ActivityWatch",)


def try_winget_install_aw() -> bool:
    """Install ActivityWatch via winget (Windows only).

    Must only be called when opts.install_aw or interactive confirm —
    never on bare --yes alone.
    """
    if sys.platform != "win32" or not shutil.which("winget"):
        return False
    for pkg in WINGET_AW_IDS:
        try:
            r = subprocess.run(
                [
                    "winget",
                    "install",
                    "-e",
                    "--id",
                    pkg,
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if r.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    return False


def start_aw_and_wait(
    base_url: str,
    exe: Path | str | None = None,
    timeout: float = 60,
) -> bool:
    """Start aw-qt if found and poll probe_aw_api until ready or timeout."""
    path: Path | None
    if exe is not None:
        path = Path(exe)
    else:
        path = find_aw_exe()
    if path is not None and path.is_file():
        try:
            subprocess.Popen(
                [str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe_aw_api(base_url):
            return True
        time.sleep(2)
    return False


def install_all_skills(vault: Path, force: bool = False) -> list[tuple[str, str]]:
    """Install bundled Claude skills into vault. Thin wrap around skill_manager."""
    results: list[tuple[str, str]] = []
    for name in bundled_skill_names():
        status, _path = install_skill(vault, name, force=force)
        results.append((name, status))
    return results


def _find_register_task_script() -> Path | None:
    """Locate the wheel-bundled scheduler script, then a source-tree fallback."""
    try:
        bundled = resources.files("kaizenlog").joinpath("scripts", "register-task.ps1")
        bundled_path = Path(str(bundled))
        if bundled_path.is_file():
            return bundled_path
    except (FileNotFoundError, ModuleNotFoundError, TypeError):
        pass
    here = Path(__file__).resolve()
    # src/kaizenlog/setup.py → parents[2] = repo root
    candidates = [
        here.parents[i] / "scripts" / "register-task.ps1"
        for i in range(1, min(5, len(here.parents)))
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def register_daily_task(
    time: str | None = "21:30",
    kaizenlog_exe: str | None = None,
    morning_time: str = "",
    config_path: Path | str | None = None,
) -> bool:
    """Register KaizenLog tasks via scripts/register-task.ps1.

    time が None のときは日次タスクを触らない（朝だけ追加する経路）。
    morning_time が非空なら朝タスク（kaizenlog morning）も登録する。
    Returns False (graceful) if the script is missing or registration fails.
    """
    script = _find_register_task_script()
    if script is None:
        print(
            "⚠️  scripts/register-task.ps1 が見つかりません。"
            "リポジトリの scripts/register-task.ps1 を手動実行してください。"
        )
        return False

    exe = kaizenlog_exe or shutil.which("kaizenlog") or (sys.argv[0] if sys.argv else None)
    args = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]
    # -Time を渡すとスクリプトが日次を（再）登録。朝のみ追加時は省略して既存日次を守る
    if time is not None:
        args += ["-Time", validate_hhmm(time, "日次タスク時刻")]
    if morning_time:
        args += ["-MorningTime", validate_hhmm(morning_time, "朝タスク時刻")]
    if exe:
        args += ["-KaizenlogExe", str(exe)]
    if config_path is not None:
        args += ["-ConfigPath", str(Path(config_path).expanduser().resolve())]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def register_weekly_task(
    vault_dir: Path | str,
    weekly_time: str = "18:00",
    weekly_day: str = "Sunday",
    kaizenlog_exe: str | None = None,
    config_path: Path | str | None = None,
) -> bool:
    """Explicitly register the optional weekly companion task.

    This is intentionally separate from setup's non-interactive path.  The
    packaged PowerShell script still performs its own executable checks.
    """
    script = _find_register_task_script()
    if script is None:
        return False
    vault = Path(vault_dir).expanduser().resolve()
    if not vault.is_dir():
        return False
    args = [
        "powershell", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-Weekly", "-VaultDir", str(vault), "-WeeklyDay", weekly_day,
        "-WeeklyTime", validate_hhmm(weekly_time, "週次タスク時刻"),
    ]
    exe = kaizenlog_exe or shutil.which("kaizenlog")
    if exe:
        args += ["-KaizenlogExe", str(exe)]
    if config_path is not None:
        args += ["-ConfigPath", str(Path(config_path).expanduser().resolve())]
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _register_daily_with_config(
    time: str | None, morning_time: str, config_path: Path
) -> bool:
    """Pass explicit config through while retaining old lightweight test fakes."""
    parameters = inspect.signature(register_daily_task).parameters
    if "config_path" in parameters:
        return register_daily_task(time, morning_time=morning_time, config_path=config_path)
    return register_daily_task(time, morning_time=morning_time)


def bounded_backfill_days(oldest_observed_day: date, today: date, cap: int = 30) -> int:
    """How many completed prior days setup may offer to backfill (never > cap)."""
    if cap < 0:
        raise ValueError("cap must not be negative")
    return min(cap, max(0, (today - oldest_observed_day).days))


def generate_initial_journal(cfg: Config, day: date) -> Path:
    """Generate one initial journal only after the caller received confirmation."""
    # Import locally: setup's normal configuration path remains side-effect free.
    from .cli import cmd_generate

    cmd_generate(cfg, day)
    from .vault import DailyNoteStore

    return DailyNoteStore(cfg).path_for(day)


def backfill_after_confirmation(
    cfg: Config, oldest_observed_day: date, today: date, cap: int = 30
) -> int:
    """Run the existing oldest-first backfill only after an explicit confirmation."""
    days = bounded_backfill_days(oldest_observed_day, today, cap=cap)
    if not days:
        return 0
    # cmd_backfill delegates to missing_days(), so an already generated today
    # is not overwritten and source data remains the authority.
    from .cli import cmd_backfill

    completed = cmd_backfill(cfg, days, today)
    from .stats import missing_days
    remaining = missing_days(cfg.stats_path, today, days)
    if remaining:
        raise RuntimeError(f'補完は {completed}日まで進みましたが、{len(remaining)}日が未完了です。')
    return completed


def oldest_window_event_day(base_url: str, timezone_name: str, *, as_of: date | None = None) -> date | None:
    """Find the first available day, capped to the 30-day setup horizon.

    AW's events API supports start/end/limit, not an ascending-order flag.
    Prefix existence queries locate the day in at most six bounded requests.
    Each request returns at most one event, never the full private history.
    """
    try:
        client = ActivityWatchClient(base_url)
        buckets = client.buckets()
        if not isinstance(buckets, dict):
            return None
        bucket_id = next(
            (
                candidate
                for candidate, metadata in buckets.items()
                if isinstance(metadata, dict) and metadata.get("type") == "currentwindow"
            ),
            None,
        )
        if bucket_id is None:
            return None
        tz = ZoneInfo(timezone_name)
        today = as_of or datetime.now(tz).date()
        def exists_before_day_end(day):
            end = datetime.combine(day + timedelta(days=1), day_time.min, tzinfo=tz)
            events = client._get(f'/api/0/buckets/{bucket_id}/events', limit=1, end=end.isoformat())
            if not isinstance(events, list):
                raise ValueError('malformed events')
            if not events:
                return False
            timestamp = events[0].get('timestamp') if isinstance(events[0], dict) else None
            if not isinstance(timestamp, str):
                raise ValueError('missing timestamp')
            observed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            if observed.tzinfo is None or observed >= end:
                raise ValueError('out-of-window timestamp')
            return True
        if not exists_before_day_end(today):
            return None
        low, high = today - timedelta(days=30), today
        while low < high:
            mid = low + timedelta(days=(high-low).days // 2)
            if exists_before_day_end(mid):
                high = mid
            else:
                low = mid + timedelta(days=1)
        return low
    except (ActivityWatchError, AttributeError, TypeError, ValueError, ZoneInfoNotFoundError):
        return None


def _explicit_daily_notes_dir(config_path: Path) -> str | None:
    """Return the explicit setting only; defaults must not block discovery."""
    if not config_path.is_file():
        return None
    try:
        with config_path.open("rb") as f:
            general = tomllib.load(f).get("general", {})
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = general.get("daily_notes_dir") if isinstance(general, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _existing_llm_settings(config_path: Path) -> tuple[str, str] | None:
    """Return explicit prior LLM settings so a rerun keeps the user's choice."""
    if not config_path.is_file():
        return None
    try:
        with config_path.open("rb") as f:
            llm = tomllib.load(f).get("llm", {})
        backend = llm.get("backend") if isinstance(llm, dict) else None
        if backend not in {"none", "claude-code-cli", "copilot-cli", "openai-compatible"}:
            return None
        return backend, str(load_config(str(config_path)).llm.model)
    except (OSError, tomllib.TOMLDecodeError, ConfigError):
        return None


def _choose_llm_settings(
    opts: SetupOptions,
    ui: SetupUI,
    detected: setup_detect.LlmDetection,
    existing: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """Keep the interactive choice explicit and never print a guessed model id."""
    backends = ["none", "claude-code-cli", "copilot-cli", "openai-compatible"]
    labels = [
        "none — LLMを使わない（改善提案なし）",
        f"claude-code-cli — {'検出済み' if detected.claude_path else '未検出'}",
        f"copilot-cli — {'検出済み' if detected.copilot_path else '未検出'}",
        "openai-compatible — " + (detected.proposed_model or "ローカルモデル未検出"),
    ]
    if opts.yes:
        # Detection is diagnostic only for a new non-interactive setup.  A
        # detected provider/model is never implicit authority to enable it.
        backend, model = existing or ("none", "")
    else:
        default = backends.index(existing[0]) if existing is not None else 0
        backend = backends[ui.choose("LLM バックエンドを選択してください", labels, default=default)]
        if existing is not None and backend == existing[0]:
            model = existing[1]
        else:
            model = detected.proposed_model if backend == "openai-compatible" and detected.proposed_model else ""
    display_model = model if backend == "openai-compatible" else "なし"
    ui.print(f"LLM: backend={backend}, model={display_model}")
    return backend, model


def _resolve_vault(opts: SetupOptions, ui: SetupUI, config_path: Path) -> Path | None:
    """Resolve vault path. Returns None if required vault cannot be determined."""
    if opts.vault is not None:
        vault = Path(opts.vault).expanduser()
        if vault.is_dir():
            return vault.resolve()
        if opts.yes:
            ui.print(f"❌ ボールトが存在しません: {vault}")
            return None
        ui.print(f"⚠️  ボールトが存在しません: {vault}")
        # fall through to interactive

    existing: Path | None = None
    if config_path.is_file():
        try:
            cfg = load_config(str(config_path))
            if cfg.vault_dir and Path(cfg.vault_dir).expanduser().is_dir():
                existing = Path(cfg.vault_dir).expanduser()
        except Exception:
            pass

    candidates = setup_detect.detect_vault_candidates(existing=existing)
    if not candidates:
        if opts.yes:
            ui.print("❌ ボールトを指定してください（--vault PATH）")
            return None
        while True:
            path = ui.ask_path("Obsidian ボールトのパス")
            if path.is_dir():
                return path.resolve()
            ui.print(f"ディレクトリがありません: {path}")

    # --yes cannot safely make a choice among machine-discovered vaults.  It
    # may reuse an explicit existing configuration, otherwise requires --vault.
    if opts.yes and existing is None:
        ui.print("❌ ボールトを指定してください（--vault PATH）")
        return None

    if opts.yes or len(candidates) == 1:
        chosen = candidates[0]
        ui.print(f"ボールト: {chosen}")
        return chosen

    labels = [str(p) for p in candidates]
    idx = ui.choose("ボールトを選択してください", labels, default=0)
    return candidates[idx]


def _normalize_daily_notes_dir(value: str | None) -> str | None:
    """Normalize Obsidian's root-folder spelling at the setup boundary."""
    if value is None:
        return None
    normalized = value.strip().replace("\\", "/")
    return "" if normalized in {"", "."} else normalized


def _bare_yes_is_diagnosis_only(opts: SetupOptions) -> bool:
    """Whether setup was invoked without authority for any persistent action."""
    return bool(
        opts.yes
        and opts.vault is None
        and not opts.force
        and not opts.generate_today
        and not opts.install_aw
        and not opts.register_task
    )


def _run_setup_diagnosis(config_path: Path, ui: SetupUI) -> int:
    """Diagnose setup state without selecting a vault or writing anything."""
    if config_path.is_file():
        try:
            cfg = load_config(str(config_path))
            report, has_error = run_doctor(cfg, str(config_path))
            ui.print(report)
            return 1 if has_error else 0
        except Exception as exc:
            ui.print(f"❌ 設定の診断に失敗しました: {type(exc).__name__}")
            return 1
    report, _has_error = run_doctor(
        Config(), str(config_path), config_absent=True
    )
    ui.print(report)
    # Preserve the legacy non-interactive missing-vault exit code while the
    # diagnosis remains entirely read-only.
    return 2


def _run_explicit_journal_generation(cfg: Config, ui: SetupUI) -> bool:
    """Generate today's journal through journal_service only."""
    from .journal_service import execute_journal, preview_journal

    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    preview = preview_journal(cfg, today)
    result = execute_journal(cfg, preview)
    status = getattr(result, "status", "failed")
    if status in {"generated", "already_generated"}:
        ui.print(f"✅ 今日の日誌を生成しました: {today}")
        return True
    reason = getattr(result, "error", None) or getattr(preview, "reason", None) or status
    ui.print(f"⚠️ 今日の日誌の生成に失敗しました: {reason}")
    return False


def preview_initial_setup(*args, **kwargs):
    """Compatibility facade for the typed setup preview service."""
    from .journal_service import preview_initial_setup as _preview

    if kwargs.get("daily_notes_dir") is None:
        opts = args[0] if args else kwargs.get("opts")
        vault = getattr(opts, "vault", None)
        if vault is not None and Path(vault).expanduser().is_dir():
            kwargs["daily_notes_dir"] = _normalize_daily_notes_dir(
                setup_detect.detect_daily_notes_dir(Path(vault).expanduser())
            )
    elif "daily_notes_dir" in kwargs:
        kwargs["daily_notes_dir"] = _normalize_daily_notes_dir(kwargs["daily_notes_dir"])
    return _preview(*args, **kwargs)


def save_initial_setup(*args, **kwargs):
    """Compatibility facade for the typed setup save service."""
    from .journal_service import save_initial_setup as _save

    if "daily_notes_dir" in kwargs:
        kwargs["daily_notes_dir"] = _normalize_daily_notes_dir(kwargs["daily_notes_dir"])
    return _save(*args, **kwargs)


def run_setup(opts: SetupOptions, ui: SetupUI | None = None) -> int:
    """Run setup wizard phases. Exit: 0 ok, 1 partial/doctor error, 2 hard fail."""
    ui = ui or ConsoleUI(yes=opts.yes)
    soft_fail = False

    ui.print("フェーズ 1/7: 設定ファイルとボールトを確認します")
    config_path = (
        Path(opts.config_path).expanduser()
        if opts.config_path is not None
        else default_config_path()
    )
    ui.print(f"設定ファイル: {config_path}")

    if _bare_yes_is_diagnosis_only(opts):
        return _run_setup_diagnosis(config_path, ui)

    vault = _resolve_vault(opts, ui, config_path)
    if vault is None:
        return 2

    ui.print("フェーズ 2/7: 日誌フォルダと LLM を設定します")
    daily_notes_dir: str | None = None
    if _explicit_daily_notes_dir(config_path) is not None:
        ui.print("日誌フォルダ: 既存設定を維持します")
    else:
        detected_daily_dir = setup_detect.detect_daily_notes_dir(vault)
        if detected_daily_dir is not None:
            ui.print(f"Obsidian 日誌フォルダ候補: {detected_daily_dir}")
            if opts.yes or ui.confirm(f"この日誌フォルダを使いますか？: {detected_daily_dir}", default=True):
                daily_notes_dir = _normalize_daily_notes_dir(detected_daily_dir)

    existing_llm = _existing_llm_settings(config_path)
    llm = setup_detect.detect_llm(base_url=opts.ollama_base_url)
    backend, model = _choose_llm_settings(opts, ui, llm, existing=existing_llm)
    try:
        write_config_file(
            config_path,
            vault_dir=vault,
            backend=backend,
            model=model,
            merge=config_path.is_file(),
            daily_notes_dir=daily_notes_dir,
        )
    except OSError as e:
        ui.print(f"❌ 設定ファイルを書き込めません: {e}")
        return 2
    ui.print(f"✅ 設定を書き込みました: {config_path}")

    ui.print("フェーズ 3/7: ActivityWatch を確認します")
    if not opts.skip_aw:
        aw = setup_detect.detect_activitywatch(opts.aw_base_url)
        if aw.reachable:
            ui.print(f"✅ ActivityWatch 応答あり: {opts.aw_base_url}")
        elif aw.exe_path is not None:
            # Installed but not running — offer start (yes: start without winget)
            ui.print(f"⚠️  ActivityWatch に接続できません: {opts.aw_base_url}")
            # --yes confirms the config write only.  Starting an already
            # installed app remains an explicit interactive choice.
            do_start = (not opts.yes) and ui.confirm(
                "ActivityWatch を起動しますか？",
                default=False,
            )
            if do_start:
                ui.print("ActivityWatch を起動しています…")
                if start_aw_and_wait(opts.aw_base_url, exe=aw.exe_path):
                    ui.print("✅ ActivityWatch を起動しました")
                else:
                    ui.print("⚠️  ActivityWatch の起動確認に失敗しました")
                    soft_fail = True
            else:
                ui.print("ActivityWatch 起動をスキップしました")
                soft_fail = True
        else:
            ui.print(f"⚠️  ActivityWatch に接続できません: {opts.aw_base_url}")
            do_install = False
            if opts.install_aw:
                do_install = True
            elif not opts.yes:
                do_install = ui.confirm(
                    "winget で ActivityWatch をインストールしますか？",
                    default=False,
                )
            # bare --yes without --install-aw: never call winget
            if do_install:
                ui.print("ActivityWatch のインストールを試行します…")
                if try_winget_install_aw():
                    if start_aw_and_wait(opts.aw_base_url, exe=None):
                        ui.print("✅ ActivityWatch を起動しました")
                    else:
                        ui.print("⚠️  ActivityWatch の起動確認に失敗しました")
                        soft_fail = True
                else:
                    ui.print("⚠️  ActivityWatch のインストールに失敗しました（手動導入可）")
                    soft_fail = True
            else:
                ui.print(
                    "ActivityWatch は後から導入できます。"
                    " 公式: https://activitywatch.net/"
                )
                soft_fail = True
    else:
        ui.print("ActivityWatch フェーズをスキップしました")

    ui.print("フェーズ 4/7: 同梱スキルを確認します")
    if opts.skip_skills:
        ui.print("スキル導入をスキップしました")
    elif opts.yes:
        ui.print("スキル導入をスキップしました（--yes は設定保存のみ確認します）")
    elif ui.confirm("同梱スキルをボールトへ導入しますか？", default=False):
        try:
            results = install_all_skills(vault, force=opts.force)
            for name, status in results:
                ui.print(f"skill {name}: {status}")
        except Exception as e:
            ui.print(f"⚠️  スキル導入に失敗: {e}")
            soft_fail = True
    else:
        ui.print("スキル導入をスキップしました")

    ui.print("フェーズ 5/7: スケジュールを確認します")
    # Daily / Morning を個別判定（片方が欠けていれば提案）
    # never register on bare --yes without --register-task
    if not opts.skip_task:
        daily_ok = setup_detect.is_task_registered("KaizenLog Daily")
        morning_ok = setup_detect.is_task_registered("KaizenLog Morning")
        need_daily = not daily_ok
        need_morning = bool(opts.morning_time) and not morning_ok
        if daily_ok:
            ui.print("✅ 日次タスクは既に登録済みです")
        if morning_ok and opts.morning_time:
            ui.print("✅ 朝タスクは既に登録済みです")
        if not need_daily and not need_morning:
            pass  # both present (or morning not requested)
        else:
            do_reg = False
            if opts.register_task:
                do_reg = True
            elif not opts.yes:
                parts = []
                if need_daily:
                    parts.append(f"日次 {opts.time}")
                if need_morning:
                    parts.append(f"朝 {opts.morning_time}")
                do_reg = ui.confirm(
                    f"欠けているタスクを登録しますか？（{' / '.join(parts)}）",
                    default=True,
                )
            if do_reg:
                morning = (opts.morning_time or "") if need_morning else ""
                if morning and not opts.yes and need_morning:
                    if not ui.confirm(
                        f"朝タスク（kaizenlog morning）を {morning} に登録しますか？",
                        default=True,
                    ):
                        morning = ""
                # 日次が既にあれば -Time を渡さず既存時刻・作業フォルダを維持
                time_arg: str | None = opts.time if need_daily else None
                if _register_daily_with_config(time_arg, morning, config_path):
                    bits = []
                    if need_daily:
                        bits.append(f"日次 {opts.time}")
                    if morning:
                        bits.append(f"朝 {morning}")
                    ui.print("✅ タスクを登録しました（" + " / ".join(bits) + "）")
                else:
                    ui.print("⚠️  タスクの登録に失敗しました（後で手動可）")
                    soft_fail = True
            else:
                ui.print(
                    "タスク登録をスキップしました"
                    "（後で --register-task または scripts/register-task.ps1）"
                )
        if not opts.yes and ui.confirm("週次タスクも登録しますか？", default=False):
            if register_weekly_task(vault, config_path=config_path):
                ui.print("✅ 週次タスクを登録しました")
            else:
                ui.print("⚠️ 週次タスクの登録に失敗しました（後で手動可）")
                soft_fail = True
    else:
        ui.print("タスク登録フェーズをスキップしました")

    ui.print("フェーズ 6/7: 設定を診断します")
    try:
        cfg = load_config(str(config_path))
    except Exception as e:
        ui.print(f"❌ 設定の再読込に失敗: {e}")
        return 2
    report, has_error = run_doctor(cfg, str(config_path))
    ui.print(report)

    setup_status = 1 if has_error or soft_fail else 0
    if opts.generate_today:
        return setup_status if _run_explicit_journal_generation(cfg, ui) else 1
    if opts.yes:
        return setup_status
    if setup_status:
        ui.print('診断の未解決項目は残っています。初回の日誌生成は個別に試せます。')

    ui.print("フェーズ 7/7: 初回の日誌を作成します")
    if not ui.confirm("今日の日誌を生成しますか？", default=False):
        ui.print("初回の日誌生成をスキップしました")
        return setup_status
    try:
        today = datetime.now(ZoneInfo(cfg.timezone)).date()
        note_path = generate_initial_journal(cfg, today)
        ui.print(f"✅ 今日の日誌を生成しました: {note_path}")
    except Exception as exc:
        ui.print(f"⚠️ 初回の日誌生成に失敗しました: {exc}")
        return 1

    oldest = oldest_window_event_day(cfg.aw_base_url, cfg.timezone)
    days = bounded_backfill_days(oldest, today) if oldest is not None else 0
    if days and ui.confirm(f"ActivityWatch の過去 {days} 日を補完しますか？", default=False):
        try:
            done = backfill_after_confirmation(cfg, oldest, today, cap=30)
            ui.print(f"✅ 過去 {done} 日を補完しました")
            ui.print('月の記録を読み返すには `kaizenlog monthly` を実行してください。')
        except Exception as exc:
            ui.print(f"⚠️ 過去日誌の補完に失敗しました: {exc}")
            return 1
    return setup_status
