param(
    [int]$RunSeconds = 300,
    [double]$PollSeconds = 0.5,
    [int]$Qty = 5,
    [int]$RestartDelaySeconds = 5
)

$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$python = "python"
$watchdogTs = Get-Date -Format "yyyyMMdd_HHmmss"
$watchdogLog = Join-Path $repo ("logs\current_almost_resolved_real_watchdog_" + $watchdogTs + ".log")

Set-Location $repo

while ($true) {
    $env:POLY_CURRENT_ALMOST_RESOLVED_REAL_ENABLED = "true"
    $env:POLY_CURRENT_ALMOST_RESOLVED_QTY = [string]$Qty
    $env:POLY_CURRENT_ALMOST_RESOLVED_POLL_SECS = [string]$PollSeconds
    $env:POLY_CURRENT_ALMOST_RESOLVED_RUN_SECONDS = [string]$RunSeconds

    Add-Content -Path $watchdogLog -Value ("[START] " + (Get-Date -Format s) + " run_seconds=" + $RunSeconds + " poll_seconds=" + $PollSeconds + " qty=" + $Qty)
    & $python "run_live_current_almost_resolved_real_v1.py" "--seconds" ([string]$RunSeconds)
    $exitCode = $LASTEXITCODE
    Add-Content -Path $watchdogLog -Value ("[EXIT] " + (Get-Date -Format s) + " code=" + $exitCode)
    Start-Sleep -Seconds $RestartDelaySeconds
}
