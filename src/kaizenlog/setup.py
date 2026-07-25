"""Setup wizard orchestration (detect → config → optional side effects → doctor)."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import setup_detect
from .config import default_config_path, load_config, write_config_file
from .doctor import run_doctor
from .skill_manager import bundled_skill_names, install_skill

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
    aw_base_url: str = "http://localhost:5600"
    ollama_base_url: str = "http://localhost:11434/v1"


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


# --- Stubs / thin wrappers (Task 4/5 fill in; keep monkeypatch targets) ---


def try_winget_install_aw() -> bool:
    """Install ActivityWatch via winget. Task 3 stub: always False.

    Must only be called when opts.install_aw or interactive confirm —
    never on bare --yes alone.
    """
    return False


def start_aw_and_wait(url: str, exe: Path | str | None = None, timeout: float = 60) -> bool:
    """Start AW and poll API until ready. Task 3 stub: always False."""
    return False


def install_all_skills(vault: Path, force: bool = False) -> list[tuple[str, str]]:
    """Install bundled Claude skills into vault. Thin wrap around skill_manager."""
    results: list[tuple[str, str]] = []
    for name in bundled_skill_names():
        status, _path = install_skill(vault, name, force=force)
        results.append((name, status))
    return results


def register_daily_task(time: str = "21:30") -> bool:
    """Register daily scheduled task. Task 3/6 stub: always False."""
    return False


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

    if opts.yes or len(candidates) == 1:
        chosen = candidates[0]
        ui.print(f"ボールト: {chosen}")
        return chosen

    labels = [str(p) for p in candidates]
    idx = ui.choose("ボールトを選択してください", labels, default=0)
    return candidates[idx]


def run_setup(opts: SetupOptions, ui: SetupUI | None = None) -> int:
    """Run setup wizard phases. Exit: 0 ok, 1 partial/doctor error, 2 hard fail."""
    ui = ui or ConsoleUI(yes=opts.yes)
    soft_fail = False

    # Phase 1: config path
    config_path = (
        Path(opts.config_path).expanduser()
        if opts.config_path is not None
        else default_config_path()
    )
    ui.print(f"設定ファイル: {config_path}")

    # Phase 2: vault
    vault = _resolve_vault(opts, ui, config_path)
    if vault is None:
        return 2

    # Phase 3: LLM + write config
    llm = setup_detect.detect_llm(base_url=opts.ollama_base_url)
    backend = llm.proposed_backend
    model = llm.proposed_model or "qwen3:8b"
    ui.print(f"LLM: backend={backend}, model={model}")
    try:
        write_config_file(
            config_path,
            vault_dir=vault,
            backend=backend,
            model=model,
            merge=config_path.is_file(),
        )
    except OSError as e:
        ui.print(f"❌ 設定ファイルを書き込めません: {e}")
        return 2
    ui.print(f"✅ 設定を書き込みました: {config_path}")

    # Phase 4: ActivityWatch
    if not opts.skip_aw:
        aw = setup_detect.detect_activitywatch(opts.aw_base_url)
        if aw.reachable:
            ui.print(f"✅ ActivityWatch 応答あり: {opts.aw_base_url}")
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
                    if start_aw_and_wait(opts.aw_base_url, exe=aw.exe_path):
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

    # Phase 5: skills
    if not opts.skip_skills:
        try:
            results = install_all_skills(vault, force=opts.force)
            for name, status in results:
                ui.print(f"skill {name}: {status}")
        except Exception as e:
            ui.print(f"⚠️  スキル導入に失敗: {e}")
            soft_fail = True
    else:
        ui.print("スキル導入をスキップしました")

    # Phase 6: task — never register on bare --yes without --register-task
    if not opts.skip_task:
        already = setup_detect.is_task_registered()
        if already:
            ui.print("✅ 日次タスクは既に登録済みです")
        else:
            do_reg = False
            if opts.register_task:
                do_reg = True
            elif not opts.yes:
                do_reg = ui.confirm(
                    f"日次タスクを {opts.time} に登録しますか？",
                    default=False,
                )
            if do_reg:
                if register_daily_task(opts.time):
                    ui.print(f"✅ 日次タスクを登録しました（{opts.time}）")
                else:
                    ui.print("⚠️  日次タスクの登録に失敗しました（後で手動可）")
                    soft_fail = True
            else:
                ui.print(
                    "タスク登録をスキップしました"
                    "（後で --register-task または scripts/register-task.ps1）"
                )
    else:
        ui.print("タスク登録フェーズをスキップしました")

    # Phase 7: doctor
    try:
        cfg = load_config(str(config_path))
    except Exception as e:
        ui.print(f"❌ 設定の再読込に失敗: {e}")
        return 2
    report, has_error = run_doctor(cfg, str(config_path))
    ui.print(report)

    if has_error or soft_fail:
        return 1
    return 0
