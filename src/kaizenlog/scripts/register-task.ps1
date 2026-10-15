# Packaged Windows Task Scheduler registration script.
# This file is the authoritative scheduler implementation.  The repository
# script delegates to it so source and installed distributions behave alike.

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

$ErrorActionPreference = "Stop"

function Test-TaskExists([string]$Name) {
    return $null -ne (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue)
}

function Assert-SafePathValue([string]$Value, [string]$Name) {
    if ($null -eq $Value) { return }
    if ($Value.IndexOf('"') -ge 0 -or $Value.IndexOf("'") -ge 0 -or $Value.IndexOf('`') -ge 0) {
        throw "$Name contains quoting characters"
    }
    foreach ($character in $Value.ToCharArray()) {
        if ([char]::IsControl($character)) {
            throw "$Name contains a control character"
        }
    }
}

function Resolve-SafeAbsolutePath([string]$Value, [string]$Name) {
    Assert-SafePathValue $Value $Name
    if ([string]::IsNullOrWhiteSpace($Value)) { return "" }
    try {
        return [System.IO.Path]::GetFullPath($Value)
    } catch {
        throw "$Name is not a valid path: $($_.Exception.Message)"
    }
}

function Get-ExistingActions($Task) {
    $actions = @($Task.Actions)
    if ($actions.Count -eq 0 -or [string]::IsNullOrWhiteSpace([string]$actions[0].Execute)) {
        throw "Existing task has no executable action"
    }
    return $actions
}

function Get-ExistingTime($Task, [string]$Fallback) {
    $trigger = @($Task.Triggers) | Select-Object -First 1
    if ($null -ne $trigger -and -not [string]::IsNullOrWhiteSpace([string]$trigger.At)) {
        return [string]$trigger.At
    }
    if ($null -ne $trigger -and -not [string]::IsNullOrWhiteSpace([string]$trigger.StartBoundary)) {
        try {
            return ([datetime]$trigger.StartBoundary).ToString("HH:mm")
        } catch {
            return $Fallback
        }
    }
    return $Fallback
}

function Get-DailyArguments([string]$SelectedMode, [string]$AbsoluteConfigPath) {
    $command = if ($SelectedMode -eq "journal") { "generate --skip-catch-up" } else { "run" }
    if ([string]::IsNullOrWhiteSpace($AbsoluteConfigPath)) {
        return $command
    }
    return '--config "' + $AbsoluteConfigPath + '" ' + $command
}

try {
    if ($Unregister) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $MorningTaskName -Confirm:$false -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $WeeklyTaskName -Confirm:$false -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $AutopilotTaskName -Confirm:$false -ErrorAction SilentlyContinue
        exit 0
    }

    $absoluteConfigPath = Resolve-SafeAbsolutePath $ConfigPath "ConfigPath"
    $workDir = (Get-Location).Path
    $timeExplicit = $PSBoundParameters.ContainsKey("Time")
    $modeExplicit = $PSBoundParameters.ContainsKey("Mode")
    $dailyExists = Test-TaskExists $TaskName
    $needsExecutable = (-not $dailyExists) -or $modeExplicit -or $Weekly -or $GuardWatch -or (-not [string]::IsNullOrWhiteSpace($MorningTime))
    $resolvedExe = ""
    if ($needsExecutable) {
        if (-not [string]::IsNullOrWhiteSpace($KaizenlogExe)) {
            $resolvedExe = Resolve-SafeAbsolutePath $KaizenlogExe "KaizenlogExe"
        } else {
            $resolvedExe = (Get-Command kaizenlog -ErrorAction SilentlyContinue).Source
            if ([string]::IsNullOrWhiteSpace($resolvedExe)) {
                throw "kaizenlog command was not found"
            }
            $resolvedExe = Resolve-SafeAbsolutePath $resolvedExe "KaizenlogExe"
        }
    }
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3)

    if ($dailyExists -and -not $timeExplicit -and -not $modeExplicit) {
        Write-Host "Daily task already exists; keeping its action and schedule."
    } else {
        $existingTask = $null
        if ($dailyExists) {
            $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        }

        $selectedMode = if ($modeExplicit) { $Mode } else { "journal" }
        if ($dailyExists -and -not $modeExplicit) {
            $action = Get-ExistingActions $existingTask
        } else {
            $actionExe = $resolvedExe
            $actionArguments = Get-DailyArguments $selectedMode $absoluteConfigPath
            $action = New-ScheduledTaskAction -Execute $actionExe -Argument $actionArguments -WorkingDirectory $workDir
        }
        $effectiveTime = if ($dailyExists -and -not $timeExplicit) {
            Get-ExistingTime $existingTask $Time
        } else {
            $Time
        }
        $trigger = New-ScheduledTaskTrigger -Daily -At $effectiveTime
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Write-Host "Daily task registered: $TaskName ($effectiveTime)."
    }

    if ($PSBoundParameters.ContainsKey("MorningTime") -and $MorningTime) {
        $morningArguments = if ($absoluteConfigPath) { '--config "' + $absoluteConfigPath + '" morning' } else { "morning" }
        $morningAction = New-ScheduledTaskAction -Execute $resolvedExe -Argument $morningArguments -WorkingDirectory $workDir
        $morningTrigger = New-ScheduledTaskTrigger -Daily -At $MorningTime
        Register-ScheduledTask -TaskName $MorningTaskName -Action $morningAction -Trigger $morningTrigger -Settings $settings -Force | Out-Null
    }

    if ($Weekly) {
        if (-not $VaultDir) { throw "-Weekly requires -VaultDir" }
        $weeklyArguments = if ($absoluteConfigPath) { '--config "' + $absoluteConfigPath + '" weekly-context --write --strict-write' } else { "weekly-context --write --strict-write" }
        $weeklyAction = New-ScheduledTaskAction -Execute $resolvedExe -Argument $weeklyArguments -WorkingDirectory $VaultDir
        $weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyDay -At $WeeklyTime
        Register-ScheduledTask -TaskName $WeeklyTaskName -Action $weeklyAction -Trigger $weeklyTrigger -Settings $settings -Force | Out-Null
    }

    if ($Autopilot) {
        if (-not $VaultDir) { throw "-Autopilot requires -VaultDir" }
        $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
        if (-not $claude) { throw "claude command was not found" }
        $autoAction = New-ScheduledTaskAction -Execute $claude -Argument '-p "/kaizen-autopilot" --allowedTools "Read Glob Grep Edit Write Bash"' -WorkingDirectory $VaultDir
        $autoTrigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 4 -DaysOfWeek $WeeklyDay -At $WeeklyTime
        Register-ScheduledTask -TaskName $AutopilotTaskName -Action $autoAction -Trigger $autoTrigger -Settings $settings -Force | Out-Null
    }

    if ($GuardWatch) {
        $watchArguments = if ($absoluteConfigPath) { '--config "' + $absoluteConfigPath + '" guard watch --once' } else { "guard watch --once" }
        $watchAction = New-ScheduledTaskAction -Execute $resolvedExe -Argument $watchArguments -WorkingDirectory $workDir
        $watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
        $watchSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName "KaizenLog Guard Watch" -Action $watchAction -Trigger $watchTrigger -Settings $watchSettings -Force | Out-Null
    }
    exit 0
} catch {
    Write-Error $_
    exit 1
}
