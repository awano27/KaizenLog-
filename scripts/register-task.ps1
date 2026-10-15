# Source-tree compatibility wrapper.  Keep scheduler behavior in the package
# resource so source and installed distributions use the same implementation.

param(
    [string]$Time = "21:30",
    [string]$TaskName = "KaizenLog Daily",
    [string]$MorningTime = "",
    [string]$MorningTaskName = "KaizenLog Morning",
    [switch]$Weekly,
    [switch]$GuardWatch,
    [switch]$Autopilot,
    [string]$VaultDir = "",
    [string]$WeeklyDay = "Sunday",
    [string]$WeeklyTime = "18:00",
    [string]$WeeklyTaskName = "KaizenLog Weekly",
    [string]$AutopilotTaskName = "KaizenLog Autopilot",
    [string]$KaizenlogExe = "",
    [string]$ConfigPath = "",
    [ValidateSet("journal", "advice")]
    [string]$Mode = "",
    [switch]$Unregister
)

$packageScript = Join-Path $PSScriptRoot "..\src\kaizenlog\scripts\register-task.ps1"
if (-not (Test-Path -LiteralPath $packageScript -PathType Leaf)) {
    Write-Error "Packaged scheduler script was not found: $packageScript"
    exit 1
}

& $packageScript @PSBoundParameters
