"""Distribution and scheduler resource contracts.

The PowerShell tests use a disposable child process with scheduler cmdlets
replaced by recording functions.  No real Windows scheduled task is touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = ROOT / "src" / "kaizenlog" / "scripts" / "register-task.ps1"
ROOT_SCRIPT = ROOT / "scripts" / "register-task.ps1"


def _powershell() -> str:
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if not executable:
        pytest.skip("PowerShell is required for scheduler contract tests")
    return executable


def _run_script(
    script: Path,
    *arguments: str,
    existing: bool = False,
    existing_arguments: str = "legacy --run",
    existing_exe: str = r"C:\old\kaizenlog.exe",
    existing_working_directory: str = r"C:\old\work",
    existing_time: str = "21:30",
    command_available: bool = True,
) -> tuple[int, list[dict[str, str]], str]:
    """Run a task script with recording scheduler cmdlets in a child process."""

    with tempfile.TemporaryDirectory(prefix="kaizenlog-task-test-") as temporary:
        temporary_path = Path(temporary)
        capture = temporary_path / "calls.jsonl"
        driver = temporary_path / "driver.ps1"
        target = str(script).replace("'", "''")
        capture_literal = str(capture).replace("'", "''")
        existing_flag = "$true" if existing else "$false"
        existing_arg_literal = existing_arguments.replace("'", "''")
        existing_exe_literal = existing_exe.replace("'", "''")
        existing_working_directory_literal = existing_working_directory.replace("'", "''")
        existing_time_literal = existing_time.replace("'", "''")
        command_function = "" if command_available else "function Get-Command { param($Name, $ErrorAction); return $null }"
        rendered_arguments = " ".join(
            value
            if value.startswith("-") and value.replace("-", "").isalnum()
            else "'" + value.replace("'", "''") + "'"
            for value in arguments
        )
        driver.write_text(
            f"""
$ErrorActionPreference = 'Stop'
$capture = '{capture_literal}'
$existing = {existing_flag}
$existingArguments = '{existing_arg_literal}'
$existingExe = '{existing_exe_literal}'
$existingWorkingDirectory = '{existing_working_directory_literal}'
$existingTime = '{existing_time_literal}'
function Write-Call($value) {{
    $value | ConvertTo-Json -Compress -Depth 8 | Add-Content -LiteralPath $capture -Encoding UTF8
}}
function Get-ScheduledTask([string]$TaskName) {{
    if (-not $existing) {{ return $null }}
    return [pscustomobject]@{{
        TaskName = $TaskName
        Actions = @([pscustomobject]@{{ Execute = $existingExe; Arguments = $existingArguments; WorkingDirectory = $existingWorkingDirectory; Marker = 'preserved' }})
        Triggers = @([pscustomobject]@{{ StartBoundary = '20261015T213000'; At = $existingTime }})
    }}
}}
function New-ScheduledTaskSettingsSet {{ param([switch]$StartWhenAvailable, $ExecutionTimeLimit, $MultipleInstances) return [pscustomobject]@{{ kind = 'settings' }} }}
function New-ScheduledTaskAction {{ param([string]$Execute, [string]$Argument, [string]$WorkingDirectory) return [pscustomobject]@{{ Execute = $Execute; Arguments = $Argument; WorkingDirectory = $WorkingDirectory }} }}
function New-ScheduledTaskTrigger {{ param([switch]$Daily, [switch]$Weekly, [switch]$Once, $At, $DaysOfWeek, $WeeksInterval, $RepetitionInterval) return [pscustomobject]@{{ At = [string]$At; Daily = [bool]$Daily; Weekly = [bool]$Weekly }} }}
function Register-ScheduledTask {{ param([string]$TaskName, $Action, $Trigger, $Settings, [switch]$Force) Write-Call ([pscustomobject]@{{ kind = 'register'; task = $TaskName; execute = $Action.Execute; arguments = $Action.Arguments; working_directory = $Action.WorkingDirectory; trigger_time = $Trigger.At }}) }}
function Unregister-ScheduledTask {{ param([string]$TaskName, [switch]$Confirm) Write-Call ([pscustomobject]@{{ kind = 'unregister'; task = $TaskName }}) }}
{command_function}
& '{target}' {rendered_arguments}
""",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [_powershell(), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
        )
        calls = []
        if capture.exists():
            calls = [json.loads(line) for line in capture.read_text(encoding="utf-8-sig").splitlines()]
        return completed.returncode, calls, completed.stdout + completed.stderr


def _registered(calls: list[dict[str, str]]) -> dict[str, str]:
    registrations = [call for call in calls if call.get("kind") == "register"]
    assert len(registrations) == 1, calls
    return registrations[0]


def test_packaged_task_script_is_authoritative() -> None:
    """The packaged script selects journal/advice and preserves old actions."""

    config = Path("config") / "kaizenlog.toml"
    executable = Path("bin") / "kaizenlog.exe"

    status, package_calls, package_output = _run_script(
        PACKAGE_SCRIPT,
        "-Mode",
        "journal",
        "-ConfigPath",
        str(config),
        "-KaizenlogExe",
        str(executable),
        "-Time",
        "22:15",
    )
    assert status == 0, package_output
    journal = _registered(package_calls)
    expected_config = (ROOT / config).resolve()
    expected_exe = (ROOT / executable).resolve()
    assert journal["arguments"] == f'--config "{expected_config}" generate --skip-catch-up'
    assert Path(journal["execute"]).resolve() == expected_exe
    assert Path(journal["working_directory"]).resolve() == ROOT.resolve()

    status, advice_calls, advice_output = _run_script(
        PACKAGE_SCRIPT,
        "-Mode",
        "advice",
        "-ConfigPath",
        str(config),
        "-KaizenlogExe",
        str(executable),
        "-Time",
        "22:15",
    )
    assert status == 0, advice_output
    advice = _registered(advice_calls)
    assert advice["arguments"] == f'--config "{expected_config}" run'

    status, legacy_calls, legacy_output = _run_script(
        PACKAGE_SCRIPT,
        "-Time",
        "22:15",
        existing=True,
        existing_arguments="legacy --keep-this-action",
        existing_exe=r"C:\old\kaizenlog.exe",
    )
    assert status == 0, legacy_output
    legacy = _registered(legacy_calls)
    assert legacy["arguments"] == "legacy --keep-this-action"
    assert legacy["execute"] == r"C:\old\kaizenlog.exe"
    assert legacy["working_directory"] == r"C:\old\work"

    status, legacy_without_path_calls, legacy_without_path_output = _run_script(
        PACKAGE_SCRIPT,
        "-Time",
        "22:15",
        existing=True,
        existing_arguments="legacy --path-independent",
        existing_exe=r"C:\old\kaizenlog.exe",
        existing_working_directory=r"C:\old\stored-work",
        command_available=False,
    )
    assert status == 0, legacy_without_path_output
    legacy_without_path = _registered(legacy_without_path_calls)
    assert legacy_without_path["execute"] == r"C:\old\kaizenlog.exe"
    assert legacy_without_path["arguments"] == "legacy --path-independent"
    assert legacy_without_path["working_directory"] == r"C:\old\stored-work"

    status, mode_change_calls, mode_change_output = _run_script(
        PACKAGE_SCRIPT,
        "-Mode",
        "advice",
        "-KaizenlogExe",
        str(executable),
        existing=True,
        existing_time="06:45",
    )
    assert status == 0, mode_change_output
    mode_change = _registered(mode_change_calls)
    assert mode_change["arguments"] == "run"
    assert mode_change["trigger_time"] == "06:45"

    status, default_calls, default_output = _run_script(
        PACKAGE_SCRIPT,
        "-KaizenlogExe",
        str(executable),
    )
    assert status == 0, default_output
    default_journal = _registered(default_calls)
    assert default_journal["arguments"] == "generate --skip-catch-up"

    status, untouched_calls, untouched_output = _run_script(
        PACKAGE_SCRIPT,
        existing=True,
        existing_arguments="legacy --untouched",
        command_available=False,
    )
    assert status == 0, untouched_output
    assert not [call for call in untouched_calls if call.get("kind") == "register"]

    status, wrapper_calls, wrapper_output = _run_script(
        ROOT_SCRIPT,
        "-Mode",
        "journal",
        "-ConfigPath",
        str(config),
        "-KaizenlogExe",
        str(executable),
        "-Time",
        "22:15",
    )
    assert status == 0, wrapper_output
    wrapper = _registered(wrapper_calls)
    assert wrapper["arguments"] == journal["arguments"]
    assert wrapper["execute"] == journal["execute"]


def test_scheduler_paths_reject_injection() -> None:
    """Quoted/control-character paths fail before a scheduler side effect."""

    for invalid in ['C:\\safe\\bad".toml', "C:\\safe\\bad\n.toml"]:
        status, calls, output = _run_script(
            PACKAGE_SCRIPT,
            "-Mode",
            "journal",
            "-ConfigPath",
            invalid,
            "-KaizenlogExe",
            r"C:\safe\kaizenlog.exe",
            "-Time",
            "22:15",
        )
        assert status != 0, output
        assert not [call for call in calls if call.get("kind") == "register"]

    status, calls, output = _run_script(
        PACKAGE_SCRIPT,
        "-Mode",
        "journal",
        "-ConfigPath",
        r"C:\safe\kaizenlog.toml",
        "-KaizenlogExe",
        'C:\\safe\\bad".exe',
        "-Time",
        "22:15",
    )
    assert status != 0, output
    assert not [call for call in calls if call.get("kind") == "register"]



def test_gui_script_and_portable_name_match() -> None:
    import tomllib

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    gui_scripts = metadata["project"]["gui-scripts"]
    assert gui_scripts["kaizenlog-home"] == "kaizenlog.home_gui:main"
    portable_exe = Path("portable") / "gui" / "kaizenlog-home.exe"
    assert "kaizenlog-home" == portable_exe.stem
