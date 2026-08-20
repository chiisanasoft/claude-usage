# scripts/run-triage.ps1
#
# Invoked by the Windows Task Scheduler entry created by setup-weekly-triage.ps1.
# Runs `claude -p "/triage"` in the repo root, tees output to a timestamped log,
# rotates logs to 12, preserves claude's exit code so a failed run is visible
# in Task Scheduler's "Last Run Result" column.

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir   = Join-Path $RepoRoot 'logs'

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

$claudeCmd = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claudeCmd) {
    $err = Join-Path $LogDir ('triage-error-' + (Get-Date -Format 'yyyy-MM-dd-HHmmss') + '.log')
    "claude CLI not found on PATH at $(Get-Date -Format o)" | Set-Content -LiteralPath $err -Encoding utf8
    exit 127
}

Set-Location -LiteralPath $RepoRoot

$log = Join-Path $LogDir ('triage-' + (Get-Date -Format 'yyyy-MM-dd-HHmmss') + '.log')

& $claudeCmd.Source -p '/triage' 2>&1 | Tee-Object -FilePath $log
$code = $LASTEXITCODE

# Rotate logs — keep the 12 most recent.
Get-ChildItem (Join-Path $LogDir 'triage-*.log') |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 12 |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
