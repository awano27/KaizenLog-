"""Behavior checks for the source and portable distribution artifacts.

The tests use disposable trees and PowerShell subprocesses.  They never build
the product executable, contact package indexes, or inspect a user's profile.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import warnings
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_smoke_helper():
    spec = importlib.util.spec_from_file_location(
        "kaizenlog_verify_install", ROOT / "packaging" / "verify_install.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _powershell() -> str:
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if not executable:
        pytest.skip("PowerShell is required for distribution artifact checks")
    return executable


def _run_ps(script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1"},
    )


def _make_portable_fixture(tmp_path: Path, include_real_tk: bool = True) -> tuple[Path, str]:
    """Create a realistic synthetic one-folder bundle and all hash records."""

    portable = tmp_path / "portable"
    payloads = {
        "cli/kaizenlog.exe": b"cli fixture",
        "cli/_internal/python311.dll": b"python runtime",
        "gui/kaizenlog-home.exe": b"gui fixture",
        "gui/_internal/python311.dll": b"python runtime",
        "README.md": b"fixture\n",
        "LICENSE": b"MIT\n",
        "portable-resources.json": json.dumps(
            {
                "schema_version": 1,
                "embedded_module_inventory": {"required_modules": ["requests", "tzdata"]},
            }
        ).encode("utf-8"),
    }
    if include_real_tk:
        payloads.update(
            {
                "cli/_internal/_tcl_data/tcl8.6/init.tcl": b"tcl runtime",
                "cli/_internal/_tk_data/tk8.6/tk.tcl": b"tk runtime",
                "cli/_internal/tcl86t.dll": b"tcl dll",
                "cli/_internal/tk86t.dll": b"tk dll",
                "gui/_internal/_tcl_data/tcl8.6/init.tcl": b"tcl runtime",
                "gui/_internal/_tk_data/tk8.6/tk.tcl": b"tk runtime",
                "gui/_internal/tcl86t.dll": b"tcl dll",
                "gui/_internal/tk86t.dll": b"tk dll",
            }
        )
    else:
        payloads.update(
            {
                "gui/_internal/_tcl_data/unrelated.txt": b"not Tcl startup data",
                "gui/_internal/_tk_data/unrelated.txt": b"not Tk startup data",
            }
        )
    payloads["cli/embedded-modules.json"] = json.dumps(
        {
            "schema_version": 1,
            "analysis": "PyInstaller.Analysis.pure",
            "entrypoint": "cli/kaizenlog.exe",
            "executable_sha256": hashlib.sha256(payloads["cli/kaizenlog.exe"]).hexdigest(),
            "modules": ["requests", "tzdata"],
        }
    ).encode("utf-8")
    payloads["gui/embedded-modules.json"] = json.dumps(
        {
            "schema_version": 1,
            "analysis": "PyInstaller.Analysis.pure",
            "entrypoint": "gui/kaizenlog-home.exe",
            "executable_sha256": hashlib.sha256(payloads["gui/kaizenlog-home.exe"]).hexdigest(),
            "modules": ["requests", "tzdata"],
        }
    ).encode("utf-8")
    for relative, content in payloads.items():
        path = portable / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    files = [{"path": relative, "sha256": hashlib.sha256(content).hexdigest()} for relative, content in payloads.items()]
    archive = portable / "kaizenlog-portable-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for relative in sorted(payloads):
            handle.writestr(relative, payloads[relative])
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "zip": archive.name,
        "zip_sha256": digest,
        "entrypoints": {"cli": "cli/kaizenlog.exe", "gui": "gui/kaizenlog-home.exe"},
        "files": files,
    }
    (portable / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    sums = []
    for path in sorted(portable.rglob("*")):
        if path.is_file() and path.name not in {"SHA256SUMS.txt", "verification.json"}:
            sums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(portable).as_posix()}")
    (portable / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return portable, digest


def test_source_manifest_includes_uncommitted_files_and_provenance(tmp_path: Path) -> None:
    """A reviewed working-tree file is packaged and recorded beside HEAD."""

    fixture = tmp_path / "repo"
    (fixture / "packaging").mkdir(parents=True)
    (fixture / "src").mkdir()
    (fixture / "tests").mkdir()
    (fixture / "README.md").write_text("fixture\n", encoding="utf-8")
    (fixture / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (fixture / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n', encoding="utf-8")
    (fixture / "config.example.toml").write_text("[config]\n", encoding="utf-8")
    (fixture / "src" / "main.py").write_text("print('fixture')\n", encoding="utf-8")
    (fixture / "tests" / "test_fixture.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    (fixture / "packaging" / "source-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "include": [
                    "src/**",
                    "tests/**",
                    "assets/readme/**",
                    "README.md",
                    "LICENSE",
                    "pyproject.toml",
                    "config.example.toml",
                ],
                "exclude": [".git/**", ".venv/**", "__pycache__/**", "*.egg-info/**", "userlogs/**", "secrets/**", ".env", ".env.*"],
                "required": ["README.md", "LICENSE", "pyproject.toml"],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=fixture, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=fixture, check=True)
    subprocess.run(["git", "add", "."], cwd=fixture, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=fixture, check=True)
    (fixture / "src" / "working-tree.py").write_text("VALUE = 1\n", encoding="utf-8")
    (fixture / ".env").write_text("SECRET=must-not-ship\n", encoding="utf-8")
    (fixture / "src" / ".env.nested").write_text("SECRET=nested\n", encoding="utf-8")
    (fixture / "src" / "cache.egg-info" / "METADATA").parent.mkdir(parents=True)
    (fixture / "src" / "cache.egg-info" / "METADATA").write_text("private\n", encoding="utf-8")
    (fixture / "src" / "userlogs" / "private.log").parent.mkdir(parents=True)
    (fixture / "src" / "userlogs" / "private.log").write_text("private\n", encoding="utf-8")
    (fixture / "src" / "secrets" / "token").parent.mkdir(parents=True)
    (fixture / "src" / "secrets" / "token").write_text("private\n", encoding="utf-8")
    (fixture / "assets" / "readme" / "hero.svg").parent.mkdir(parents=True)
    (fixture / "assets" / "readme" / "hero.svg").write_text("root asset\n", encoding="utf-8")
    # These names share an allowlist basename/prefix but are outside the
    # repository-root include paths.  They must not enter provenance or ZIP.
    (fixture / ".pytest_cache" / "README.md").parent.mkdir(parents=True)
    (fixture / ".pytest_cache" / "README.md").write_text("cache\n", encoding="utf-8")
    (fixture / "artifacts" / "portable" / "README.md").parent.mkdir(parents=True)
    (fixture / "artifacts" / "portable" / "README.md").write_text("artifact\n", encoding="utf-8")
    (fixture / "private" / "src" / "should-not-ship.py").parent.mkdir(parents=True)
    (fixture / "private" / "src" / "should-not-ship.py").write_text("private\n", encoding="utf-8")
    (fixture / "nested" / "assets" / "readme" / "hero.svg").parent.mkdir(parents=True)
    (fixture / "nested" / "assets" / "readme" / "hero.svg").write_text("nested asset\n", encoding="utf-8")

    output = tmp_path / "artifacts" / "source"
    result = _run_ps(
        ROOT / "scripts" / "build-source-manifest.ps1",
        "-RepositoryRoot",
        str(fixture),
        "-Manifest",
        str(fixture / "packaging" / "source-manifest.json"),
        "-OutputDir",
        str(output),
        "-BaseRef",
        "HEAD",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    provenance = json.loads((output / "source-provenance.json").read_text(encoding="utf-8"))
    names = {entry["path"] for entry in provenance["files"]}
    assert "src/working-tree.py" in names
    assert ".env" not in names
    assert "src/.env.nested" not in names
    assert "src/cache.egg-info/METADATA" not in names
    assert "src/userlogs/private.log" not in names
    assert "src/secrets/token" not in names
    assert "README.md" in names
    assert "assets/readme/hero.svg" in names
    assert ".pytest_cache/README.md" not in names
    assert "artifacts/portable/README.md" not in names
    assert "private/src/should-not-ship.py" not in names
    assert "nested/assets/readme/hero.svg" not in names
    assert provenance["head"]
    assert provenance["head_manifest_sha256"]
    assert provenance["working_tree_manifest_sha256"]
    assert provenance["uncommitted_diff_sha256"]
    archives = list(output.glob("kaizenlog-source-*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        assert "src/working-tree.py" in archive.namelist()
        assert ".env" not in archive.namelist()
        assert "src/.env.nested" not in archive.namelist()
        assert "src/cache.egg-info/METADATA" not in archive.namelist()
        archive_names = set(archive.namelist())
        assert "README.md" in archive_names
        assert "assets/readme/hero.svg" in archive_names
        assert ".pytest_cache/README.md" not in archive_names
        assert "artifacts/portable/README.md" not in archive_names
        assert "private/src/should-not-ship.py" not in archive_names
        assert "nested/assets/readme/hero.svg" not in archive_names


def test_portable_wrappers_call_distinct_typed_entrypoints(tmp_path: Path) -> None:
    """The two launcher scripts execute their corresponding package entrypoint."""

    package = tmp_path / "kaizenlog"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "def main():\n    print('cli-entry')\n    return 17\n", encoding="utf-8"
    )
    (package / "home_gui.py").write_text(
        "def main():\n    print('gui-entry')\n    return 23\n", encoding="utf-8"
    )
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONNOUSERSITE": "1"}
    for wrapper, marker, code in (
        (ROOT / "packaging" / "portable_cli.py", "cli-entry", 17),
        (ROOT / "packaging" / "portable_gui.py", "gui-entry", 23),
    ):
        result = subprocess.run([sys.executable, str(wrapper)], text=True, capture_output=True, env=env)
        assert result.returncode == code, result.stdout + result.stderr
        assert result.stdout.strip() == marker


def test_verify_reports_pythonless_not_run_without_external_evidence(tmp_path: Path) -> None:
    """Artifact integrity can pass while missing Pythonless evidence stays NOT_RUN."""

    portable, _digest = _make_portable_fixture(tmp_path)

    result = _run_ps(ROOT / "scripts" / "verify-portable.ps1", "-InputDir", str(portable))
    assert result.returncode == 0, result.stdout + result.stderr
    verification = json.loads((portable / "verification.json").read_text(encoding="utf-8"))
    assert verification["artifact_pass"] == "PASS"
    assert verification["pythonless_status"] == "NOT_RUN"
    assert verification["gui_status"] == "NOT_RUN"
    assert verification["entrypoints"]["cli_parent"] != verification["entrypoints"]["gui_parent"]


def test_verify_rejects_source_tree_contamination(tmp_path: Path) -> None:
    """A portable archive containing repository metadata fails verification."""

    portable, _digest = _make_portable_fixture(tmp_path)
    archive = portable / "kaizenlog-portable-windows-x64.zip"
    with zipfile.ZipFile(archive, "a") as handle:
        handle.writestr(".git/config", b"should fail")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = json.loads((portable / "manifest.json").read_text(encoding="utf-8"))
    manifest["zip_sha256"] = digest
    (portable / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    result = _run_ps(ROOT / "scripts" / "verify-portable.ps1", "-InputDir", str(portable))
    assert result.returncode != 0
    verification = json.loads((portable / "verification.json").read_text(encoding="utf-8"))
    assert verification["artifact_pass"] == "FAIL"
    assert any("source" in reason.lower() or "repository" in reason.lower() for reason in verification["reasons"])


def test_verify_does_not_accept_summary_only_or_mismatched_evidence(tmp_path: Path) -> None:
    """PASS plus a host is insufficient when artifact-bound checks are absent."""

    portable, digest = _make_portable_fixture(tmp_path)
    evidence = tmp_path / "pythonless.json"
    evidence.write_text(
        json.dumps({"schema_version": 1, "status": "PASS", "host": "fixture", "artifact_sha256": "wrong"}),
        encoding="utf-8",
    )
    result = _run_ps(
        ROOT / "scripts" / "verify-portable.ps1",
        "-InputDir",
        str(portable),
        "-PythonlessEvidencePath",
        str(evidence),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    verification = json.loads((portable / "verification.json").read_text(encoding="utf-8"))
    assert verification["artifact_pass"] == "PASS"
    assert verification["pythonless_status"] == "NOT_RUN"


def test_verify_preserves_explicit_failure_evidence(tmp_path: Path) -> None:
    """A recorded execution failure remains FAIL instead of becoming NOT_RUN/PASS."""

    portable, digest = _make_portable_fixture(tmp_path)
    evidence = tmp_path / "pythonless-failure.json"
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "FAIL",
                "artifact_sha256": digest,
                "entrypoint": "cli/kaizenlog.exe",
                "host": "fixture",
                "reason": "CLI did not start",
            }
        ),
        encoding="utf-8",
    )
    result = _run_ps(
        ROOT / "scripts" / "verify-portable.ps1",
        "-InputDir",
        str(portable),
        "-PythonlessEvidencePath",
        str(evidence),
    )
    assert result.returncode != 0
    verification = json.loads((portable / "verification.json").read_text(encoding="utf-8"))
    assert verification["artifact_pass"] == "FAIL"
    assert verification["pythonless_status"] == "FAIL"


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        ("gui", "gui_execution.displayed", "false"),
        ("pythonless", "checks.python_present", None),
        ("pythonless", "cli_execution.exit_code", "0"),
    ],
)
def test_verify_rejects_wrong_typed_evidence_fields(
    tmp_path: Path, kind: str, field: str, value: object
) -> None:
    """JSON values with the wrong type cannot be coerced into execution proof."""

    portable, digest = _make_portable_fixture(tmp_path)
    evidence = {
        "schema_version": 1,
        "status": "PASS",
        "artifact_sha256": digest,
        "entrypoint": "gui/kaizenlog-home.exe" if kind == "gui" else "cli/kaizenlog.exe",
        "host": "fixture",
        "inventory": ["gui/kaizenlog-home.exe" if kind == "gui" else "cli/kaizenlog.exe"],
        "checks": {"python_present": False, "py_launcher_present": False},
        "cli_execution": {"launched": True, "exit_code": 0},
        "gui_execution": {"launched": True, "displayed": True, "actions": True, "closed": True},
    }
    target: dict[str, object] = evidence
    for component in field.split(".")[:-1]:
        target = target[component]  # type: ignore[assignment,index]
    target[field.split(".")[-1]] = value
    evidence_path = tmp_path / ("gui.json" if kind == "gui" else "pythonless.json")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    argument = "-GuiEvidencePath" if kind == "gui" else "-PythonlessEvidencePath"
    result = _run_ps(ROOT / "scripts" / "verify-portable.ps1", "-InputDir", str(portable), argument, str(evidence_path))
    assert result.returncode == 0, result.stdout + result.stderr
    verification = json.loads((portable / "verification.json").read_text(encoding="utf-8"))
    assert verification["artifact_pass"] == "PASS"
    assert verification["gui_status" if kind == "gui" else "pythonless_status"] == "NOT_RUN"


def test_verify_accepts_fully_typed_artifact_bound_pythonless_evidence(tmp_path: Path) -> None:
    """A complete typed record is accepted when it binds to the fixture artifact."""

    portable, digest = _make_portable_fixture(tmp_path)
    evidence = tmp_path / "pythonless-valid.json"
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "PASS",
                "artifact_sha256": digest,
                "entrypoint": "cli/kaizenlog.exe",
                "host": "fixture",
                "inventory": ["cli/kaizenlog.exe"],
                "checks": {"python_present": False, "py_launcher_present": False},
                "cli_execution": {"launched": True, "exit_code": 0},
            }
        ),
        encoding="utf-8",
    )
    result = _run_ps(
        ROOT / "scripts" / "verify-portable.ps1",
        "-InputDir",
        str(portable),
        "-PythonlessEvidencePath",
        str(evidence),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    verification = json.loads((portable / "verification.json").read_text(encoding="utf-8"))
    assert verification["artifact_pass"] == "PASS"
    assert verification["pythonless_status"] == "PASS"


def test_build_rejects_broad_output_directory_without_mutation(tmp_path: Path) -> None:
    """Portable cleanup refuses the repository root before touching any output."""

    result = _run_ps(
        ROOT / "scripts" / "build-portable.ps1",
        "-OutputDir",
        str(ROOT),
        "-SourceManifest",
        str(ROOT / "packaging" / "source-manifest.json"),
    )
    assert result.returncode != 0


def test_verify_rechecks_manifest_files_and_sha256sums(tmp_path: Path) -> None:
    """A changed or unlisted unpacked member invalidates the artifact."""

    portable, _digest = _make_portable_fixture(tmp_path)
    (portable / "cli" / "_internal" / "_tcl_data" / "tcl8.6" / "init.tcl").write_bytes(b"tampered")
    (portable / "rogue.bin").write_bytes(b"unlisted")
    result = _run_ps(ROOT / "scripts" / "verify-portable.ps1", "-InputDir", str(portable))
    assert result.returncode != 0
    verification = json.loads((portable / "verification.json").read_text(encoding="utf-8"))
    assert verification["artifact_pass"] == "FAIL"
    assert any("mismatch" in reason.lower() or "unlisted" in reason.lower() for reason in verification["reasons"])


def test_verify_rejects_missing_real_tcl_tk_runtime_files(tmp_path: Path) -> None:
    """Unrelated files under Tcl/Tk directories cannot satisfy runtime proof."""

    portable, _digest = _make_portable_fixture(tmp_path, include_real_tk=False)
    result = _run_ps(ROOT / "scripts" / "verify-portable.ps1", "-InputDir", str(portable))
    assert result.returncode != 0
    verification = json.loads((portable / "verification.json").read_text(encoding="utf-8"))
    assert verification["artifact_pass"] == "FAIL"
    assert any("tcl_data" in reason.lower() or "tk_data" in reason.lower() for reason in verification["reasons"])


def test_verify_rejects_duplicate_and_traversal_entries(tmp_path: Path) -> None:
    """Duplicate and traversal ZIP entries are rejected even with a valid hash."""

    portable, _digest = _make_portable_fixture(tmp_path)
    archive = portable / "kaizenlog-portable-windows-x64.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(archive, "a") as handle:
            handle.writestr("cli/kaizenlog.exe", b"duplicate")
            handle.writestr("../outside.txt", b"escape")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = json.loads((portable / "manifest.json").read_text(encoding="utf-8"))
    manifest["zip_sha256"] = digest
    (portable / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    result = _run_ps(ROOT / "scripts" / "verify-portable.ps1", "-InputDir", str(portable))
    assert result.returncode != 0
    verification = json.loads((portable / "verification.json").read_text(encoding="utf-8"))
    assert any("duplicate" in reason.lower() or "unsafe" in reason.lower() for reason in verification["reasons"])


@pytest.mark.parametrize("boundary", ["zip-unlisted", "sums-traversal", "manifest-ads", "entrypoint-path"])
def test_verify_rejects_unlisted_or_unsafe_distribution_boundaries(tmp_path: Path, boundary: str) -> None:
    """ZIP, manifest, sums, and entrypoint names share the same safe-path boundary."""

    portable, _digest = _make_portable_fixture(tmp_path)
    archive = portable / "kaizenlog-portable-windows-x64.zip"
    manifest = json.loads((portable / "manifest.json").read_text(encoding="utf-8"))
    if boundary == "zip-unlisted":
        with zipfile.ZipFile(archive, "a") as handle:
            handle.writestr("rogue.bin", b"not in manifest")
        manifest["zip_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    elif boundary == "sums-traversal":
        sums = (portable / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
        digest, _path = sums[0].split("  ", 1)
        sums[0] = f"{digest}  ../escape.txt"
        (portable / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    elif boundary == "manifest-ads":
        manifest["files"].append({"path": "C:relative.txt", "sha256": "0" * 64})
    elif boundary == "entrypoint-path":
        manifest["entrypoints"]["cli"] = "other/kaizenlog.exe"
    (portable / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    result = _run_ps(ROOT / "scripts" / "verify-portable.ps1", "-InputDir", str(portable))
    assert result.returncode != 0
    verification = json.loads((portable / "verification.json").read_text(encoding="utf-8"))
    assert verification["artifact_pass"] == "FAIL"
    assert verification["reasons"]


def test_verify_overwrites_stale_pass_with_latest_failure(tmp_path: Path) -> None:
    """A later invalid manifest cannot leave a previous PASS receipt behind."""

    portable, _digest = _make_portable_fixture(tmp_path)
    first = _run_ps(ROOT / "scripts" / "verify-portable.ps1", "-InputDir", str(portable))
    assert first.returncode == 0, first.stdout + first.stderr
    (portable / "manifest.json").write_text("{bad", encoding="utf-8")
    second = _run_ps(ROOT / "scripts" / "verify-portable.ps1", "-InputDir", str(portable))
    assert second.returncode != 0
    verification = json.loads((portable / "verification.json").read_text(encoding="utf-8"))
    assert verification["artifact_pass"] == "FAIL"
    assert any("invalid" in reason.lower() or "manifest" in reason.lower() for reason in verification["reasons"])


def _write_smoke_output_fixture(vault: Path, *, known_activity: bool) -> None:
    notes = vault / "01 Daily Notes"
    stats_dir = vault / ".kaizenlog" / "stats"
    notes.mkdir(parents=True)
    stats_dir.mkdir(parents=True)
    (notes / "2026-10-14.md").write_text(
        "# fixture\n<!-- kaizenlog:activity -->\n<!-- kaizenlog:nippou -->\n",
        encoding="utf-8",
    )
    (notes / "2026-10-15.md").write_text("<!-- kaizenlog:resume -->\n", encoding="utf-8")
    stats = {"day": "2026-10-14"}
    if known_activity:
        stats.update(
            {
                "total_minutes": 60.0,
                "by_app": {"Code.exe": 60.0},
                "blocks": [
                    {
                        "start": "2026-10-14T09:00:00+09:00",
                        "end": "2026-10-14T10:00:00+09:00",
                        "minutes": 60.0,
                        "app": "Code.exe",
                        "title": "KaizenLog fixture task",
                    }
                ],
                "input": {
                    "keypresses": 42,
                    "clicks": 5,
                    "active_input_minutes": 1.0,
                    "focus_blocks": 0,
                    "focus_minutes": 0.0,
                },
            }
        )
    (stats_dir / "2026-10-14.json").write_text(json.dumps(stats), encoding="utf-8")


def test_smoke_output_rejects_marker_only_artifacts(tmp_path: Path) -> None:
    """Markers alone cannot stand in for known ActivityWatch fixture evidence."""

    helper = _load_smoke_helper()
    vault = tmp_path / "vault"
    _write_smoke_output_fixture(vault, known_activity=False)
    result = helper._inspect_outputs(vault)
    assert result["status"] == "FAIL"
    assert not all(result["known_fixture_activity"].values())


def test_smoke_output_accepts_known_fixture_activity(tmp_path: Path) -> None:
    """The real fixture shape is accepted with finite, positive known values."""

    helper = _load_smoke_helper()
    vault = tmp_path / "vault"
    _write_smoke_output_fixture(vault, known_activity=True)
    result = helper._inspect_outputs(vault)
    assert result["status"] == "PASS"
    assert all(result["known_fixture_activity"].values())


def test_smoke_package_paths_reject_unrelated_source_tree(tmp_path: Path) -> None:
    """An imported source copy is not evidence of the supplied external venv."""

    helper = _load_smoke_helper()
    venv = tmp_path / "external-venv"
    purelib = venv / "Lib" / "site-packages"
    imported = tmp_path / "checkout" / "src" / "kaizenlog" / "__init__.py"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = external\n", encoding="utf-8")
    purelib.mkdir(parents=True)
    imported.parent.mkdir(parents=True)
    imported.write_text("", encoding="utf-8")
    checks = helper._package_python_path_checks(
        venv / "Scripts" / "python.exe",
        imported,
        venv,
        purelib,
        tmp_path / "checkout",
    )
    assert checks["pyvenv_cfg"]
    assert checks["prefix_matches_external_venv"]
    assert not checks["module_under_purelib"]
    assert not all(checks.values())


def test_smoke_resource_check_uses_full_output_and_bounded_tail(tmp_path: Path) -> None:
    """A required resource near stdout's prefix survives bounded evidence capture."""

    helper = _load_smoke_helper()
    code = "print('daily-kaizen'); print('x' * 10000)"
    result = helper._run_child(
        [sys.executable, "-c", code],
        tmp_path,
        {**os.environ, "PYTHONNOUSERSITE": "1"},
        required_stdout=("daily-kaizen",),
    )
    assert result["status"] == "PASS"
    assert result["stdout_required"] == {"daily-kaizen": True}
    assert result["stdout_required_all"] is True
    assert len(result["stdout_tail"]) <= helper.MAX_TAIL
    assert len(result["stdout_sha256"]) == 64
