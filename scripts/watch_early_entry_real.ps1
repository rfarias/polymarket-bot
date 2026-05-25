param(
    [int]$RunSeconds = 1800,
    [double]$PollSeconds = 0.5,
    [double]$Qty = 6.0,
    [int]$RestartDelaySeconds = 5,
    [switch]$ArmReal,
    [switch]$Continuous,
    [int]$MaxCycles = 1
)

$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$python = "python"
$watchdogTs = Get-Date -Format "yyyyMMdd_HHmmss"
$watchdogLog = Join-Path $repo ("logs\ee_real_watchdog_" + $watchdogTs + ".log")

Set-Location $repo

$cycle = 0
while ($true) {
    $cycle += 1

    $env:EE_REAL_ENABLED       = "true"
    $env:EE_REAL_QTY           = [string]$Qty
    $env:EE_REAL_POLL_SECS     = [string]$PollSeconds
    $env:EE_REAL_RUN_SECONDS   = [string]$RunSeconds

    if ($ArmReal) {
        $env:EE_REAL_POSTS_ENABLED = "true"
    } else {
        $env:EE_REAL_POSTS_ENABLED = "false"
    }

    $mode = if ($ArmReal) { "REAL" } else { "SHADOW" }
    $msg = "[START] " + (Get-Date -Format s) + " cycle=" + $cycle + " mode=" + $mode + " run_seconds=" + $RunSeconds + " poll=" + $PollSeconds + " qty=" + $Qty
    Add-Content -Path $watchdogLog -Value $msg
    Write-Host $msg

    & $python "run_live_early_entry_real_v1.py" "--seconds" ([string]$RunSeconds)
    $exitCode = $LASTEXITCODE

    $msg = "[EXIT] " + (Get-Date -Format s) + " cycle=" + $cycle + " code=" + $exitCode
    Add-Content -Path $watchdogLog -Value $msg
    Write-Host $msg

    if (-not $Continuous -and $cycle -ge $MaxCycles) {
        $msg = "[STOP] " + (Get-Date -Format s) + " completed requested cycles"
        Add-Content -Path $watchdogLog -Value $msg
        Write-Host $msg
        break
    }
    Write-Host "[RESTART] aguardando $RestartDelaySeconds s..."
    Start-Sleep -Seconds $RestartDelaySeconds
}
