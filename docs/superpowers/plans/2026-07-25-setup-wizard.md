# Setup Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `kaizenlog setup` — a detect-first interactive wizard that writes trusted AppData config, configures vault/LLM, optionally installs ActivityWatch via winget, installs skills, registers the daily task, and ends with `doctor`.

**Architecture:** Pure detection in `setup_detect.py` (no side effects). Orchestration + `SetupUI` in `setup.py`. Config path trust and atomic template/merge writes live in `config.py`. CLI wires `setup` / updated `init-config`. Reuse `doctor`, `skill_manager`, and `register-task.ps1`.

**Tech stack:** Python 3.11+, pytest, requests (existing), subprocess for winget/task, PowerShell task script, tomllib (read only).

**Spec:** `docs/superpowers/specs/2026-07-25-setup-wizard-design.md`

---

## File map

| File | Responsibility |
| --- | --- |
| Create: `src/kaizenlog/setup_detect.py` | Detect vault candidates, LLM tools/models, AW reachability/exe, scheduled task presence, winget availability |
| Create: `src/kaizenlog/setup.py` | `SetupOptions`, `SetupUI`, phase runners, `run_setup()` → exit code |
| Modify: `src/kaizenlog/config.py` | `default_config_path()`, find order, `render_config_template()`, `write_config_file()`, merge helpers |
| Modify: `src/kaizenlog/cli.py` | Move template to config module usage; `cmd_init_config`; `setup` subcommand; import `run_setup` |
| Modify: `src/kaizenlog/doctor.py` | Point AW/LLM failures at `kaizenlog setup`; warn on CWD config shadow |
| Modify: `scripts/register-task.ps1` | Accept optional `-KaizenlogExe` so setup can register the exact binary |
| Modify: `README.md`, `docs/USAGE.md`, `config.example.toml` | Short path = setup |
| Create: `tests/test_setup_detect.py` | Detection unit tests |
| Create: `tests/test_setup_config.py` | Path trust + template/write |
| Create: `tests/test_setup_wizard.py` | Orchestration with FakeUI + mocks |
| Modify: `tests/test_ops.py` | Config candidate order expectations |

Do **not** touch `grok-desktop-experiment/`.

---

### Task 1: Config path trust + template writer

**Files:**
- Modify: `src/kaizenlog/config.py`
- Modify: `tests/test_ops.py` (candidate order)
- Create: `tests/test_setup_config.py`
- Modify: `src/kaizenlog/cli.py` (`CONFIG_TEMPLATE` / `cmd_init_config` only after GREEN on config tests — or keep thin wrappers)

- [ ] **Step 1: Write failing tests for default path and find order**

Create `tests/test_setup_config.py`:

```python
"""Config path trust and template writes for setup / init-config."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from kaizenlog.config import (
    default_config_path,
    find_config_file,
    render_config_template,
    write_config_file,
    load_config,
)


def test_default_config_path_windows(monkeypatch, tmp_path):
    monkeypatch.setattr("kaizenlog.config.sys.platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    p = default_config_path()
    assert p == tmp_path / "AppData" / "kaizenlog" / "config.toml"


def test_default_config_path_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr("kaizenlog.config.sys.platform", "linux")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # implementation may use Path.home()/.config if no XDG — either is fine if documented
    p = default_config_path()
    assert p.name == "config.toml"
    assert "kaizenlog" in p.parts


def test_find_prefers_appdata_over_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    app = tmp_path / "AppData" / "kaizenlog"
    app.mkdir(parents=True)
    app_cfg = app / "config.toml"
    app_cfg.write_text('[general]\nvault_dir = "from-app"\n', encoding="utf-8")
    cwd_cfg = tmp_path / "kaizenlog.toml"
    cwd_cfg.write_text('[general]\nvault_dir = "from-cwd"\n', encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr("kaizenlog.config.sys.platform", "win32")
    found = find_config_file()
    assert found == app_cfg


def test_find_falls_back_to_cwd_with_file_present(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty-appdata"))
    monkeypatch.setattr("kaizenlog.config.sys.platform", "win32")
    cwd_cfg = tmp_path / "kaizenlog.toml"
    cwd_cfg.write_text("[general]\n", encoding="utf-8")
    assert find_config_file() == cwd_cfg


def test_missing_env_config_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("KAIZENLOG_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(FileNotFoundError):
        find_config_file()


def test_render_template_embeds_vault_backend_model():
    text = render_config_template(
        vault_dir=Path("C:/vault"),
        backend="openai-compatible",
        model="gemma4:latest",
    )
    assert "C:/vault" in text or "C:\\\\vault" in text or "C:/vault" in text.replace("\\", "/")
    assert 'backend = "openai-compatible"' in text
    assert 'model = "gemma4:latest"' in text


def test_write_config_creates_parents_atomically(tmp_path):
    dest = tmp_path / "a" / "b" / "config.toml"
    write_config_file(
        dest,
        vault_dir=tmp_path / "vault",
        backend="none",
        model="qwen3:8b",
        merge=False,
    )
    assert dest.is_file()
    cfg = load_config(str(dest))
    assert cfg.vault_dir == tmp_path / "vault"
    assert cfg.llm.backend == "none"


def test_write_config_merge_preserves_extra_text(tmp_path):
    dest = tmp_path / "config.toml"
    dest.write_text(
        '[general]\nvault_dir = "old"\n\n# keep-me\n[[categories.rules]]\n'
        'name = "Custom"\npatterns = ["foo"]\n',
        encoding="utf-8",
    )
    write_config_file(
        dest,
        vault_dir=tmp_path / "newvault",
        backend="copilot-cli",
        model="gemma4:latest",
        merge=True,
    )
    text = dest.read_text(encoding="utf-8")
    assert "keep-me" in text
    assert "Custom" in text
    assert "newvault" in text.replace("\\", "/")
```

Update `tests/test_ops.py::test_existing_config_candidates_orders_cwd_first` expectations to match new priority (env → AppData → CWD). Rename to `test_existing_config_candidates_lists_all` and assert AppData appears before CWD when both exist.

- [ ] **Step 2: Run tests — expect FAIL**

```powershell
cd C:\develop\KaizenLog\KaizenLog-
.\.venv\Scripts\python.exe -m pytest tests/test_setup_config.py -q
```

Expected: import/attribute errors (`default_config_path` missing).

- [ ] **Step 3: Implement config helpers**

In `src/kaizenlog/config.py` add (adapt imports: `import sys`):

```python
import sys

def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "kaizenlog" / "config.toml"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "kaizenlog" / "config.toml"
    return Path.home() / ".config" / "kaizenlog" / "config.toml"


def find_config_file(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {p}")
        return p
    env = os.environ.get("KAIZENLOG_CONFIG")
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"KAIZENLOG_CONFIG の設定ファイルが見つかりません: {p}")
        return p
    app = default_config_path()
    if app.is_file():
        return app
    for cand in (Path("kaizenlog.toml"), Path("config.toml")):
        if cand.is_file():
            return cand
    return None


def existing_config_candidates() -> list[Path]:
    found: list[Path] = []
    env = os.environ.get("KAIZENLOG_CONFIG")
    if env and Path(env).expanduser().is_file():
        found.append(Path(env).expanduser())
    app = default_config_path()
    if app.is_file():
        found.append(app)
    for cand in (Path("kaizenlog.toml"), Path("config.toml")):
        if cand.is_file() and cand.resolve() not in {p.resolve() for p in found}:
            found.append(cand)
    return found
```

Move `CONFIG_TEMPLATE` body into `render_config_template(vault_dir, backend, model, **kwargs) -> str` (f-string or `.format`). Default backend for brand-new template when called from init-config without detection: `"none"` per trust design; setup will pass detected backend.

`write_config_file(path, *, vault_dir, backend, model, merge=False)`:
- `merge=False` or missing file → `atomic_write_text` full template (import from `vault`).
- `merge=True` → read text; replace/insert keys with helpers:

```python
def _upsert_toml_assignment(text: str, key: str, value: str) -> str:
    """Replace first `key = ...` line or append under a reasonable section."""
    import re
    pattern = re.compile(rf"(?m)^({re.escape(key)}\s*=\s*).*$")
    if pattern.search(text):
        return pattern.sub(rf'\1"{value}"', text, count=1)
    # append at end of file as last resort
    return text.rstrip() + f'\n{key} = "{value}"\n'
```

For paths use POSIX-ish forward slashes in TOML strings: `str(vault_dir).replace("\\", "/")`.

- [ ] **Step 4: GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_setup_config.py tests/test_ops.py -q
```

Expected: all pass.

- [ ] **Step 5: Wire `init-config`**

Update `cli.py`:

```python
def cmd_init_config(output: str | None = None) -> int:
    from .config import default_config_path, write_config_file
    out = Path(output).expanduser() if output else default_config_path()
    if out.exists():
        print(f"{out} は既に存在します。再構成は `kaizenlog setup` を使ってください。")
        return 1
    write_config_file(out, vault_dir=Path("."), backend="none", model="qwen3:8b", merge=False)
    print(f"✅ 設定ファイルの雛形を作成しました: {out.resolve()}")
    print("次: kaizenlog setup")
    return 0
```

Parser: `init = sub.add_parser("init-config"); init.add_argument("--output")`  
Main: `return cmd_init_config(getattr(args, "output", None))`.

Remove unused module-level `CONFIG_TEMPLATE` from cli if fully moved.

- [ ] **Step 6: Commit**

```powershell
rtk git add src/kaizenlog/config.py src/kaizenlog/cli.py tests/test_setup_config.py tests/test_ops.py
rtk git commit -m "feat(config): AppData default path, template writer, fail-closed env"
```

---

### Task 2: Detection module (pure)

**Files:**
- Create: `src/kaizenlog/setup_detect.py`
- Create: `tests/test_setup_detect.py`

- [ ] **Step 1: Failing tests**

```python
"""Pure detection helpers for kaizenlog setup."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kaizenlog.setup_detect import (
    detect_llm,
    recommend_ollama_model,
    detect_activitywatch,
    detect_vault_candidates,
    propose_backend,
)


def test_propose_backend_prefers_claude():
    assert propose_backend(claude=True, copilot=True, ollama_models=["gemma4:latest"]) == "claude-code-cli"


def test_propose_backend_copilot_then_ollama_then_none():
    assert propose_backend(claude=False, copilot=True, ollama_models=[]) == "copilot-cli"
    assert propose_backend(claude=False, copilot=False, ollama_models=["gemma4:latest"]) == "openai-compatible"
    assert propose_backend(claude=False, copilot=False, ollama_models=None) == "none"


def test_recommend_ollama_model_skips_embed_and_prefers_qwen_gemma():
    models = ["nomic-embed-text:latest", "gemma4:latest", "llama3:8b"]
    assert recommend_ollama_model(models, preferred=None) == "gemma4:latest"
    assert recommend_ollama_model(models, preferred="llama3:8b") == "llama3:8b"
    assert recommend_ollama_model(["nomic-embed-text:latest"], preferred=None) is None


def test_detect_llm_uses_which_and_models(monkeypatch):
    monkeypatch.setattr("kaizenlog.setup_detect.shutil.which", lambda c: f"C:/{c}.exe" if c in ("claude",) else None)

    def fake_models(base_url, timeout=15):
        assert "11434" in base_url
        return ["gemma4:latest"]

    monkeypatch.setattr("kaizenlog.setup_detect.list_openai_models", fake_models)
    info = detect_llm(base_url="http://localhost:11434/v1")
    assert info.claude_path is not None
    assert info.copilot_path is None
    assert info.ollama_models == ["gemma4:latest"]
    assert info.proposed_backend == "claude-code-cli"


def test_detect_activitywatch_ok(monkeypatch):
    monkeypatch.setattr(
        "kaizenlog.setup_detect.probe_aw_api",
        lambda url, timeout=5: True,
    )
    st = detect_activitywatch("http://localhost:5600")
    assert st.reachable is True


def test_detect_vault_candidates_includes_existing(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    (v / "note.md").write_text("x", encoding="utf-8")
    cands = detect_vault_candidates(existing=v, extra_roots=[tmp_path])
    assert v.resolve() in [p.resolve() for p in cands]
```

- [ ] **Step 2: Run — FAIL**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_setup_detect.py -q
```

- [ ] **Step 3: Implement `setup_detect.py`**

```python
"""Side-effect-free environment detection for setup wizard."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import requests

@dataclass
class LlmDetection:
    claude_path: str | None
    copilot_path: str | None
    ollama_models: list[str] | None  # None = unreachable
    proposed_backend: str
    proposed_model: str | None


@dataclass
class AwDetection:
    reachable: bool
    exe_path: Path | None = None


def propose_backend(*, claude: bool, copilot: bool, ollama_models: list[str] | None) -> str:
    if claude:
        return "claude-code-cli"
    if copilot:
        return "copilot-cli"
    if ollama_models:
        return "openai-compatible"
    return "none"


def recommend_ollama_model(models: list[str], preferred: str | None) -> str | None:
    if preferred and preferred in models:
        return preferred
    non_embed = [m for m in models if "embed" not in m.lower()]
    pool = non_embed or []
    if not pool:
        return None
    for token in ("qwen3", "qwen", "gemma", "llama", "mistral", "phi"):
        for m in pool:
            if token in m.lower():
                return m
    return pool[0]


def list_openai_models(base_url: str, timeout: float = 15) -> list[str] | None:
    try:
        r = requests.get(f"{base_url.rstrip('/')}/models", timeout=timeout)
        if r.status_code >= 400:
            return None
        ids = [m.get("id") for m in r.json().get("data", []) if isinstance(m, dict)]
        return [i for i in ids if i] or None
    except (requests.RequestException, ValueError):
        return None


def detect_llm(base_url: str = "http://localhost:11434/v1", preferred_model: str | None = None) -> LlmDetection:
    claude = shutil.which("claude")
    copilot = shutil.which("copilot")
    models = list_openai_models(base_url)
    backend = propose_backend(claude=bool(claude), copilot=bool(copilot), ollama_models=models)
    model = recommend_ollama_model(models or [], preferred_model) if models else None
    return LlmDetection(claude, copilot, models, backend, model)


def probe_aw_api(base_url: str, timeout: float = 5) -> bool:
    try:
        r = requests.get(f"{base_url.rstrip('/')}/api/0/buckets/", timeout=timeout)
        return r.status_code < 400
    except requests.RequestException:
        return False


def find_aw_exe() -> Path | None:
    which = shutil.which("aw-qt")
    if which:
        return Path(which)
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "activitywatch" / "aw-qt.exe",
        Path(os.environ.get("ProgramFiles", "")) / "ActivityWatch" / "aw-qt.exe",
    ]
    import os
    for c in candidates:
        if c.is_file():
            return c
    return None


def detect_activitywatch(base_url: str) -> AwDetection:
    if probe_aw_api(base_url):
        return AwDetection(True, find_aw_exe())
    return AwDetection(False, find_aw_exe())


def detect_vault_candidates(existing: Path | None = None, extra_roots: list[Path] | None = None) -> list[Path]:
    out: list[Path] = []
    if existing and Path(existing).expanduser().is_dir():
        out.append(Path(existing).expanduser().resolve())
    for root in extra_roots or []:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                out.append(child.resolve())
    # dedupe preserve order
    seen = set()
    uniq = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def is_task_registered(task_name: str = "KaizenLog Daily") -> bool:
    """Best-effort Windows check; returns False on non-Windows or errors."""
    import sys, subprocess
    if sys.platform != "win32":
        return False
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
```

Fix `import os` placement at top in real code.

- [ ] **Step 4: GREEN + commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_setup_detect.py -q
rtk git add src/kaizenlog/setup_detect.py tests/test_setup_detect.py
rtk git commit -m "feat(setup): pure environment detection helpers"
```

---

### Task 3: Setup orchestration + FakeUI (no AW install yet)

**Files:**
- Create: `src/kaizenlog/setup.py`
- Create: `tests/test_setup_wizard.py`
- Modify: `src/kaizenlog/cli.py`

- [ ] **Step 1: Failing orchestration tests**

```python
"""Setup wizard orchestration with FakeUI."""
from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, field

import pytest

from kaizenlog.setup import SetupOptions, SetupUI, run_setup


@dataclass
class FakeUI:
    answers: list  # bool | str | int in call order
    logs: list[str] = field(default_factory=list)

    def print(self, msg: str = "") -> None:
        self.logs.append(msg)

    def confirm(self, msg: str, default: bool = True) -> bool:
        if not self.answers:
            return default
        v = self.answers.pop(0)
        return bool(v)

    def choose(self, msg: str, options: list[str], default: int = 0) -> int:
        if not self.answers:
            return default
        v = self.answers.pop(0)
        return int(v)

    def ask_path(self, msg: str) -> Path:
        v = self.answers.pop(0)
        return Path(v)


def test_setup_yes_writes_appdata_config(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg_path = tmp_path / "cfg" / "config.toml"
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_llm",
        lambda **k: __import__("kaizenlog.setup_detect", fromlist=["LlmDetection"]).LlmDetection(
            None, None, ["gemma4:latest"], "openai-compatible", "gemma4:latest"
        ),
    )
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_activitywatch",
        lambda url: __import__("kaizenlog.setup_detect", fromlist=["AwDetection"]).AwDetection(False, None),
    )
    monkeypatch.setattr("kaizenlog.setup_detect.is_task_registered", lambda name="KaizenLog Daily": False)
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda cfg, p=None: ("✅ mock doctor", False))

    opts = SetupOptions(
        config_path=cfg_path,
        vault=vault,
        yes=True,
        skip_aw=True,
        skip_task=True,
        skip_skills=True,
    )
    code = run_setup(opts, ui=FakeUI([]))
    assert code == 0
    assert cfg_path.is_file()
    text = cfg_path.read_text(encoding="utf-8")
    assert "gemma4:latest" in text
    assert "openai-compatible" in text


def test_setup_yes_does_not_winget_without_flag(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg_path = tmp_path / "config.toml"
    called = []

    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_llm",
        lambda **k: __import__("kaizenlog.setup_detect", fromlist=["LlmDetection"]).LlmDetection(
            None, "copilot", None, "copilot-cli", None
        ),
    )
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_activitywatch",
        lambda url: __import__("kaizenlog.setup_detect", fromlist=["AwDetection"]).AwDetection(False, None),
    )
    monkeypatch.setattr("kaizenlog.setup.try_winget_install_aw", lambda: called.append("winget") or False)
    monkeypatch.setattr("kaizenlog.setup_detect.is_task_registered", lambda name="KaizenLog Daily": True)
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda cfg, p=None: ("❌ AW", True))

    code = run_setup(
        SetupOptions(config_path=cfg_path, vault=vault, yes=True, skip_aw=False, skip_task=True, skip_skills=True),
        ui=FakeUI([]),
    )
    assert called == []  # no --install-aw
    assert code == 1  # partial: doctor has error
    assert cfg_path.is_file()


def test_setup_missing_vault_noninteractive_fails(tmp_path):
    cfg_path = tmp_path / "config.toml"
    code = run_setup(
        SetupOptions(config_path=cfg_path, vault=None, yes=True, skip_aw=True, skip_task=True, skip_skills=True),
        ui=FakeUI([]),  # no path answers
    )
    assert code == 2
```

- [ ] **Step 2: Implement `setup.py` skeleton**

Key types:

```python
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
    ...  # input()-based; if not sys.stdin.isatty() and need input → raise SystemExit guidance


def run_setup(opts: SetupOptions, ui: SetupUI | None = None) -> int:
    ui = ui or ConsoleUI(yes=opts.yes)
    # Phase 1: resolve config path (default_config_path)
    # Phase 2: vault
    # Phase 3: detect_llm + write_config_file
    # Phase 4: AW (Task 4 fills install)
    # Phase 5: skills
    # Phase 6: task
    # Phase 7: run_doctor → exit code
```

Exit code rules from spec:
- vault/config write failure → 2
- success + doctor clean → 0
- success + doctor errors or AW failed → 1

For Task 3, stub:
- `try_winget_install_aw()` returns False without calling winget unless `opts.install_aw` or interactive confirm (implement confirm path in Task 4; here ensure `--yes` alone never calls it).
- skills/task: if skip, no-op; else stub message.

- [ ] **Step 3: CLI wiring**

```python
su = sub.add_parser("setup", help="対話式セットアップウィザード")
su.add_argument("--config")
su.add_argument("--vault")
su.add_argument("--yes", action="store_true")
su.add_argument("--force", action="store_true")
su.add_argument("--skip-aw", action="store_true")
su.add_argument("--skip-task", action="store_true")
su.add_argument("--skip-skills", action="store_true")
su.add_argument("--install-aw", action="store_true")
su.add_argument("--register-task", action="store_true")
su.add_argument("--time", default="21:30")
```

In main, handle `setup` **before** `load_config` required path — setup bootstraps config:

```python
if args.command == "setup":
    from .setup import SetupOptions, run_setup
    return run_setup(SetupOptions(
        config_path=Path(args.config).expanduser() if args.config else None,
        vault=Path(args.vault).expanduser() if args.vault else None,
        yes=args.yes,
        force=args.force,
        skip_aw=args.skip_aw,
        skip_task=args.skip_task,
        skip_skills=args.skip_skills,
        install_aw=args.install_aw,
        register_task=args.register_task,
        time=args.time,
    ))
```

Also allow `doctor` without config still (existing). Commands that need config keep `load_config`.

- [ ] **Step 4: GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_setup_wizard.py tests/test_setup_config.py tests/test_setup_detect.py -q
```

- [ ] **Step 5: Commit**

```powershell
rtk git add src/kaizenlog/setup.py src/kaizenlog/cli.py tests/test_setup_wizard.py
rtk git commit -m "feat(setup): wizard orchestration and CLI entrypoint"
```

---

### Task 4: ActivityWatch install / start

**Files:**
- Modify: `src/kaizenlog/setup.py`
- Modify: `tests/test_setup_wizard.py`

- [ ] **Step 1: Tests**

```python
def test_setup_install_aw_flag_calls_winget(tmp_path, monkeypatch):
    vault = tmp_path / "v"
    vault.mkdir()
    cfg = tmp_path / "c.toml"
    called = []
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_llm",
        lambda **k: __import__("kaizenlog.setup_detect", fromlist=["LlmDetection"]).LlmDetection(
            None, None, None, "none", None
        ),
    )
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_activitywatch",
        lambda url: __import__("kaizenlog.setup_detect", fromlist=["AwDetection"]).AwDetection(False, None),
    )
    monkeypatch.setattr("kaizenlog.setup.try_winget_install_aw", lambda: called.append(1) or True)
    monkeypatch.setattr("kaizenlog.setup.start_aw_and_wait", lambda url, exe=None, timeout=60: True)
    monkeypatch.setattr("kaizenlog.setup_detect.is_task_registered", lambda name="KaizenLog Daily": True)
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda cfg, p=None: ("ok", False))

    code = run_setup(
        SetupOptions(
            config_path=cfg, vault=vault, yes=True,
            skip_aw=False, install_aw=True, skip_task=True, skip_skills=True,
        ),
        ui=FakeUI([]),
    )
    assert called == [1]
    assert code == 0
```

- [ ] **Step 2: Implement**

```python
WINGET_AW_IDS = ("ActivityWatch.ActivityWatch",)  # adjust if winget search differs

def try_winget_install_aw() -> bool:
    if sys.platform != "win32" or not shutil.which("winget"):
        return False
    for pkg in WINGET_AW_IDS:
        r = subprocess.run(
            ["winget", "install", "-e", "--id", pkg, "--accept-package-agreements", "--accept-source-agreements"],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode == 0:
            return True
    return False


def start_aw_and_wait(base_url: str, exe: Path | None = None, timeout: float = 60) -> bool:
    exe = exe or find_aw_exe()
    if exe and exe.is_file():
        subprocess.Popen([str(exe)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe_aw_api(base_url):
            return True
        time.sleep(2)
    return False
```

Phase 4 logic:
1. If reachable → ok  
2. If exe → confirm start (or yes) → wait  
3. Else if interactive confirm OR `install_aw` → winget → start → wait  
4. Else fail soft  

- [ ] **Step 3: GREEN + commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_setup_wizard.py -q
rtk git add src/kaizenlog/setup.py tests/test_setup_wizard.py
rtk git commit -m "feat(setup): ActivityWatch winget install and startup wait"
```

---

### Task 5: Skills + scheduled task registration

**Files:**
- Modify: `src/kaizenlog/setup.py`
- Modify: `scripts/register-task.ps1`
- Modify: `tests/test_setup_wizard.py`

- [ ] **Step 1: Tests**

```python
def test_setup_installs_skills_when_not_skipped(tmp_path, monkeypatch):
    vault = tmp_path / "v"
    vault.mkdir()
    cfg = tmp_path / "c.toml"
    installed = []

    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_llm",
        lambda **k: __import__("kaizenlog.setup_detect", fromlist=["LlmDetection"]).LlmDetection(
            None, None, None, "none", None
        ),
    )
    monkeypatch.setattr(
        "kaizenlog.setup_detect.detect_activitywatch",
        lambda url: __import__("kaizenlog.setup_detect", fromlist=["AwDetection"]).AwDetection(True, None),
    )
    monkeypatch.setattr("kaizenlog.setup_detect.is_task_registered", lambda name="KaizenLog Daily": True)
    monkeypatch.setattr(
        "kaizenlog.setup.install_all_skills",
        lambda v, force=False: installed.append(str(v)) or [("daily-kaizen", "installed")],
    )
    monkeypatch.setattr("kaizenlog.setup.run_doctor", lambda cfg, p=None: ("ok", False))

    run_setup(
        SetupOptions(config_path=cfg, vault=vault, yes=True, skip_aw=True, skip_task=True, skip_skills=False),
        ui=FakeUI([]),
    )
    assert installed == [str(vault.resolve())]
```

- [ ] **Step 2: Implement skills helper**

```python
def install_all_skills(vault: Path, force: bool = False) -> list[tuple[str, str]]:
    from .skill_manager import bundled_skill_names, install_skill
    results = []
    for name in bundled_skill_names():
        status, path = install_skill(vault, name, force=force)
        results.append((name, status))
    return results
```

- [ ] **Step 3: Task registration**

Extend `scripts/register-task.ps1`:

```powershell
param(
    ...
    [string]$KaizenlogExe = ""
)
...
if ($KaizenlogExe) {
    $kaizenlog = $KaizenlogExe
} else {
    $kaizenlog = (Get-Command kaizenlog -ErrorAction SilentlyContinue).Source
}
```

In setup:

```python
def register_daily_task(time: str, kaizenlog_exe: str | None = None) -> bool:
    script = Path(__file__).resolve().parents[2] / "scripts" / "register-task.ps1"
    # also try importlib.resources if packaged later; for now repo script + optional resources path
    if not script.is_file():
        return False
    args = [
        "powershell", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-Time", time,
    ]
    if kaizenlog_exe:
        args += ["-KaizenlogExe", kaizenlog_exe]
    r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return r.returncode == 0
```

Gates: only if `register_task` flag or interactive confirm; never on bare `--yes`.

Resolve exe: `shutil.which("kaizenlog")` or `sys.argv[0]`.

- [ ] **Step 4: GREEN + commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_setup_wizard.py -q
rtk git add src/kaizenlog/setup.py scripts/register-task.ps1 tests/test_setup_wizard.py
rtk git commit -m "feat(setup): skill install and daily task registration hooks"
```

---

### Task 6: Doctor messages + docs + full regression

**Files:**
- Modify: `src/kaizenlog/doctor.py`
- Modify: `README.md`
- Modify: `docs/USAGE.md`
- Modify: `config.example.toml` (if present)
- Optionally: `cli.py` module docstring

- [ ] **Step 1: Doctor UX**

In `_check_activitywatch` error string append:  
`修復: kaizenlog setup （または ActivityWatch を起動）`

In `_check_config` when CWD file is used while AppData missing: warn to migrate via setup.

- [ ] **Step 2: README shortest path**

Replace multi-step setup intro with:

```markdown
### 最短セットアップ

```powershell
pipx install kaizenlog
kaizenlog setup
kaizenlog doctor
kaizenlog run          # ActivityWatch 起動後
```
```

Keep detailed manual path under collapse or USAGE.

- [ ] **Step 3: USAGE** — document `setup` flags table; mark `init-config --output` ; note AppData default.

- [ ] **Step 4: Full test suite**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all pass (232 + new tests). Fix any regressions from find_config order in other tests.

- [ ] **Step 5: Manual smoke (local)**

```powershell
.\.venv\Scripts\kaizenlog.exe setup --vault "C:\develop\obsidian\2026" --yes --skip-aw --skip-task --skip-skills --config "$env:TEMP\kaizenlog-smoke.toml"
.\.venv\Scripts\kaizenlog.exe --config "$env:TEMP\kaizenlog-smoke.toml" doctor
```

Expected: config written with detected LLM; doctor reports vault OK; AW may still ❌.

- [ ] **Step 6: Commit**

```powershell
rtk git add src/kaizenlog/doctor.py README.md docs/USAGE.md config.example.toml
rtk git commit -m "docs: make kaizenlog setup the primary onboarding path"
```

- [ ] **Step 7: Final commit if version note needed**

No version bump required unless releasing; leave `1.5.0rc1` or note in CHANGELOG under Unreleased:

```markdown
### Added
- `kaizenlog setup` interactive onboarding wizard
```

---

## Spec coverage checklist

| Spec requirement | Task |
| --- | --- |
| `kaizenlog setup` CLI + flags | 3 |
| AppData default + find order + env fail-closed | 1 |
| init-config `--output` + AppData | 1 |
| Detect-first vault/LLM | 2–3 |
| Ollama real model in config | 2–3 |
| `--yes` no winget/task without flags | 3–5 |
| winget AW + start wait | 4 |
| skill install | 5 |
| daily task only | 5 |
| doctor end + next steps | 3, 6 |
| Partial success exit 1/2 | 3 |
| README/USAGE | 6 |
| No grok-desktop / no JSONL-first | — out of scope |

## Placeholder / consistency notes

- Function names locked: `run_setup`, `SetupOptions`, `try_winget_install_aw`, `install_all_skills`, `render_config_template`, `write_config_file`, `default_config_path`.
- Winget package id may need one local `winget search activitywatch` adjustment during Task 4 — keep constant at top of `setup.py`.
- `register-task.ps1` path from installed wheel may be missing until assets packaging exists; setup must print manual command if script absent (graceful skip, not crash).

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-25-setup-wizard.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks  
2. **Inline Execution** — this session with executing-plans and checkpoints  

Which approach?
