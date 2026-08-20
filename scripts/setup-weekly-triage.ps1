# scripts/setup-weekly-triage.ps1
#
# Register a weekly Windows Task Scheduler job that runs `/triage` via
# scripts/run-triage.ps1. Idempotent — re-running just updates the schedule.
#
# Usage (from this repo's root, normal PowerShell — elevation not required):
#   .\scripts\setup-weekly-triage.ps1
#   .\scripts\setup-weekly-triage.ps1 -DayOfWeek Monday -Time 09:00
#   .\scripts\setup-weekly-triage.ps1 -Remove
#
# What this does:
#   - Registers a task named "claude-usage weekly triage" that invokes
#     `powershell.exe -File scripts\run-triage.ps1` weekly at the given
#     day + time, running only when the user is logged in (Interactive
#     logon — no stored password, no service account).
#   - The runner script logs to logs/triage-<timestamp>.log and rotates.
#
# What this DOES NOT do:
#   - Does not grant any new permissions. Claude Code's settings govern
#     what the routine can do.
#   - Does not push to main. Per /triage workflow, only DEV is pushed.

[CmdletBinding()]
param(
    [ValidateSet('Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday')]
    [string]$DayOfWeek = 'Monday',

    [ValidatePattern('^\d{2}:\d{2}$')]
    [string]$Time = '09:00',

    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$TaskName  = 'claude-usage weekly triage'
$RepoRoot  = Split-Path -Parent $PSScriptRoot
$RunScript = Join-Path $PSScriptRoot 'run-triage.ps1'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled task '$TaskName'."
    } else {
        Write-Output "No scheduled task '$TaskName' found."
    }
    return
}

if (-not (Test-Path -LiteralPath $RunScript)) {
    throw "Runner script not found: $RunScript. Make sure scripts/run-triage.ps1 is committed."
}

# Use full DOMAIN\User form (works for both local and AD accounts).
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# -File is the right invocation for Task Scheduler — no quoting/newline games.
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`"" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek $DayOfWeek `
    -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

# Interactive logon: task fires only when the user is signed in. No password stored.
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Run /triage on $RepoRoot every $DayOfWeek at $Time. Pushes DEV, never main."

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

Write-Output "Registered scheduled task '$TaskName'."
Write-Output "  Repo:     $RepoRoot"
Write-Output "  Runner:   $RunScript"
Write-Output "  Cadence:  every $DayOfWeek at $Time"
Write-Output "  Runs as:  $userId (Interactive logon - only fires when you are signed in)"
Write-Output "  Logs:     $(Join-Path $RepoRoot 'logs')\triage-*.log (rotates after 12 runs)"
Write-Output ""
Write-Output "Smoke test (runs now):"
Write-Output "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Output ""
Write-Output "Remove:"
Write-Output "  .\scripts\setup-weekly-triage.ps1 -Remove"
