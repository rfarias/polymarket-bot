param(
    [int]$RunSeconds = 3600,
    [double]$PollSeconds = 0.5,
    [double]$Qty = 6.0,
    [int]$RestartDelaySeconds = 5,
    [switch]$Continuous,
    [int]$MaxCycles = 1
)

$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$python = "python"
$watchdogTs = Get-Date -Format "yyyyMMdd_HHmmss"
$watchdogLog = Join-Path $repo ("logs\ee_paper_watchdog_" + $watchdogTs + ".log")

Set-Location $repo

$cycle = 0
while ($true) {
    $cycle += 1
    $env:EE_PAPER_QTY         = [string]$Qty
    $env:EE_PAPER_POLL_SECS   = [string]$PollSeconds
    $env:EE_PAPER_RUN_SECONDS = [string]$RunSeconds

    $msg = "[START] " + (Get-Date -Format s) + " cycle=" + $cycle + " run_seconds=" + $RunSeconds + " poll=" + $PollSeconds + " qty=" + $Qty
    Add-Content -Path $watchdogLog -Value $msg
    Write-Host $msg

    & $python "run_live_early_entry_paper_v1.py" "--seconds" ([string]$RunSeconds)
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
