# KaizenLog を Windows タスクスケジューラに登録するスクリプト
# 毎晩指定時刻に「ログ収集 → LLM改善提案」を自動実行します。
# -Weekly を付けると、週次のClaude Codeエージェント深掘り分析
# （claude -p "/weekly-kaizen"）も登録します。
#
# 使い方（管理者権限は不要）:
#   powershell -ExecutionPolicy Bypass -File .\register-task.ps1
#   powershell -ExecutionPolicy Bypass -File .\register-task.ps1 -Time "22:30"
#   powershell -ExecutionPolicy Bypass -File .\register-task.ps1 -MorningTime "08:30"
#   powershell -ExecutionPolicy Bypass -File .\register-task.ps1 -Weekly -VaultDir "C:\develop\obsidian\2026"
#   powershell -ExecutionPolicy Bypass -File .\register-task.ps1 -Unregister
#
# 登録規則（既存タスクの黙った巻き戻し防止）:
#   - 日次タスク: -Time が明示されたとき、または未登録のときだけ Register。
#     既存があり -Time 未指定なら現状維持してその旨を表示。
#   - 朝タスク: -MorningTime が明示されたときだけ（再）登録。未指定なら触らない。

param(
    [string]$Time = "21:30",
    [string]$TaskName = "KaizenLog Daily",
    [string]$MorningTime = "",
    [string]$MorningTaskName = "KaizenLog Morning",
    [switch]$Weekly,
    [switch]$Autopilot,
    [string]$VaultDir = "",
    [string]$WeeklyDay = "Sunday",
    [string]$WeeklyTime = "18:00",
    [string]$WeeklyTaskName = "KaizenLog Weekly",
    [string]$AutopilotTaskName = "KaizenLog Autopilot",
    [string]$KaizenlogExe = "",
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

function Test-TaskExists([string]$Name) {
    return $null -ne (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue)
}

try {
    if ($Unregister) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $MorningTaskName -Confirm:$false -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $WeeklyTaskName -Confirm:$false -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $AutopilotTaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "タスク '$TaskName' / '$MorningTaskName' / '$WeeklyTaskName' / '$AutopilotTaskName' を削除しました。"
        exit 0
    }

    if ($KaizenlogExe) {
        $kaizenlog = $KaizenlogExe
    } else {
        $kaizenlog = (Get-Command kaizenlog -ErrorAction SilentlyContinue).Source
    }
    if (-not $kaizenlog) {
        Write-Error "kaizenlog コマンドが見つかりません。先に `pipx install kaizenlog` などでインストールしてください。"
        exit 1
    }

    # 作業フォルダを登録時のカレントに固定する。
    # これがないとタスクは C:\Windows\System32 で実行され、カレント優先の設定解決
    # （./kaizenlog.toml → %APPDATA%）により手動実行と別の設定を拾う事故が起きる。
    $workDir = (Get-Location).Path
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)

    $timeExplicit = $PSBoundParameters.ContainsKey('Time')
    $dailyExists = Test-TaskExists $TaskName
    $shouldRegisterDaily = $timeExplicit -or -not $dailyExists

    if ($shouldRegisterDaily) {
        $action = New-ScheduledTaskAction -Execute $kaizenlog -Argument "run" -WorkingDirectory $workDir
        $trigger = New-ScheduledTaskTrigger -Daily -At $Time
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Write-Host "タスク '$TaskName' を登録しました（毎日 $Time に実行 / 作業フォルダ: $workDir）。"
    } elseif ($dailyExists) {
        Write-Host "タスク '$TaskName' は既に登録済みのため現状を維持します（-Time 指定時のみ上書き）。"
    }

    # 朝タスク: -MorningTime 明示時のみ（再）登録。-Time だけの再実行で朝を巻き戻さない
    if ($PSBoundParameters.ContainsKey('MorningTime') -and $MorningTime) {
        $morningAction = New-ScheduledTaskAction -Execute $kaizenlog -Argument "morning" -WorkingDirectory $workDir
        $morningTrigger = New-ScheduledTaskTrigger -Daily -At $MorningTime
        Register-ScheduledTask -TaskName $MorningTaskName -Action $morningAction -Trigger $morningTrigger -Settings $settings -Force | Out-Null
        Write-Host "タスク '$MorningTaskName' を登録しました（毎日 $MorningTime に実行）。"
    }

    if ($Weekly) {
        if (-not $VaultDir) {
            Write-Error "-Weekly には -VaultDir （Obsidianボールトのパス）が必要です。"
            exit 1
        }
        $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
        if (-not $claude) {
            Write-Error "claude コマンドが見つかりません。Claude Code をインストールしてください（https://claude.com/claude-code）。"
            exit 1
        }
        # ボールト内で /weekly-kaizen スキルを実行（skills/weekly-kaizen/ を .claude/skills/ にコピーしておくこと）
        $weeklyAction = New-ScheduledTaskAction -Execute $claude `
            -Argument '-p "/weekly-kaizen" --allowedTools "Read Glob Grep Edit Write"' `
            -WorkingDirectory $VaultDir
        $weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyDay -At $WeeklyTime
        Register-ScheduledTask -TaskName $WeeklyTaskName -Action $weeklyAction -Trigger $weeklyTrigger -Settings $settings -Force | Out-Null
        Write-Host "タスク '$WeeklyTaskName' を登録しました（毎週 $WeeklyDay $WeeklyTime に実行）。"
    }

    if ($Autopilot) {
        if (-not $VaultDir) {
            Write-Error "-Autopilot には -VaultDir （Obsidianボールトのパス）が必要です。"
            exit 1
        }
        $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
        if (-not $claude) {
            Write-Error "claude コマンドが見つかりません。Claude Code をインストールしてください。"
            exit 1
        }
        # 4週ごとに自動化候補を実装してPR/提案ノートとして提出（有効化は常に人間の承認待ち）
        $autoAction = New-ScheduledTaskAction -Execute $claude `
            -Argument '-p "/kaizen-autopilot" --allowedTools "Read Glob Grep Edit Write Bash"' `
            -WorkingDirectory $VaultDir
        $autoTrigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 4 -DaysOfWeek $WeeklyDay -At $WeeklyTime
        Register-ScheduledTask -TaskName $AutopilotTaskName -Action $autoAction -Trigger $autoTrigger -Settings $settings -Force | Out-Null
        Write-Host "タスク '$AutopilotTaskName' を登録しました（4週ごと $WeeklyDay $WeeklyTime に実行）。"
    }

    Write-Host "確認: タスクスケジューラを開くか、次を実行 → Get-ScheduledTask -TaskName 'KaizenLog*'"
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
