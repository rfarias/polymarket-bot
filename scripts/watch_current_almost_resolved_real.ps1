param(
    [int]$RunSeconds = 300,
    [double]$PollSeconds = 0.5,
    [int]$Qty = 6,
    [int]$RestartDelaySeconds = 5,
    [switch]$ArmReal,
    [switch]$Continuous,
    [int]$MaxCycles = 1
)

$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$python = "python"
$watchdogTs = Get-Date -Format "yyyyMMdd_HHmmss"
$watchdogLog = Join-Path $repo ("logs\current_almost_resolved_real_watchdog_" + $watchdogTs + ".log")

Set-Location $repo

$cycle = 0
while ($true) {
    $cycle += 1
    if ($ArmReal) {
        $env:POLY_GUARDED_ENABLED = "true"
        $env:POLY_GUARDED_SHADOW_ONLY = "false"
        $env:POLY_GUARDED_REAL_POSTS_ENABLED = "true"
    }
    $env:POLY_CURRENT_ALMOST_RESOLVED_REAL_ENABLED = "true"
    $env:POLY_CURRENT_ALMOST_RESOLVED_QTY = [string]$Qty
    $env:POLY_CURRENT_ALMOST_RESOLVED_POLL_SECS = [string]$PollSeconds
    $env:POLY_CURRENT_ALMOST_RESOLVED_RUN_SECONDS = [string]$RunSeconds
    $env:POLY_CURRENT_ALMOST_RESOLVED_HYBRID_ENTRY = "true"
    $env:POLY_CURRENT_ALMOST_RESOLVED_HYBRID_AGGRESSIVE_AFTER_SECS = "1.5"
    $env:POLY_CURRENT_ALMOST_RESOLVED_HYBRID_AGGRESSIVE_MAX_PRICE = "0.99"
    $env:POLY_CURRENT_ALMOST_RESOLVED_AGGRESSIVE_ENTRY_FAK = "true"
    $env:POLY_CURRENT_ALMOST_RESOLVED_HOLD_WINNER_TO_RESOLUTION = "true"
    $env:POLY_CURRENT_ALMOST_RESOLVED_AUTO_REDEEM_ENABLED = "false"

    Add-Content -Path $watchdogLog -Value ("[START] " + (Get-Date -Format s) + " cycle=" + $cycle + " run_seconds=" + $RunSeconds + " poll_seconds=" + $PollSeconds + " qty=" + $Qty)
    & $python "run_live_current_almost_resolved_real_v1.py" "--seconds" ([string]$RunSeconds)
    $exitCode = $LASTEXITCODE
    Add-Content -Path $watchdogLog -Value ("[EXIT] " + (Get-Date -Format s) + " cycle=" + $cycle + " code=" + $exitCode)
    if (-not $Continuous -and $cycle -ge $MaxCycles) {
        Add-Content -Path $watchdogLog -Value ("[STOP] " + (Get-Date -Format s) + " completed requested cycles")
        break
    }
    Start-Sleep -Seconds $RestartDelaySeconds
}
