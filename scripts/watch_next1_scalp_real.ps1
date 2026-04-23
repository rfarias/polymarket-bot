param(
    [int]$RunSeconds = 21600,
    [double]$PollSeconds = 0.5,
    [int]$RestartDelaySeconds = 5
)

$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$python = "python"
$watchdogTs = Get-Date -Format "yyyyMMdd_HHmmss"
$watchdogLog = Join-Path $repo ("logs\next1_scalp_real_watchdog_" + $watchdogTs + ".log")

Set-Location $repo

while ($true) {
    $env:POLY_NEXT1_SCALP_RUN_SECONDS = [string]$RunSeconds
    $env:POLY_NEXT1_SCALP_POLL_SECS = [string]$PollSeconds

    Add-Content -Path $watchdogLog -Value ("[START] " + (Get-Date -Format s) + " run_seconds=" + $RunSeconds + " poll_seconds=" + $PollSeconds)
    & $python "run_live_next1_scalp_real_v1.py"
    $exitCode = $LASTEXITCODE
    Add-Content -Path $watchdogLog -Value ("[EXIT] " + (Get-Date -Format s) + " code=" + $exitCode)
    Start-Sleep -Seconds $RestartDelaySeconds
}
