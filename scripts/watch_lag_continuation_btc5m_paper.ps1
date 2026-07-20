param(
    [int]$RunSeconds = 3600,
    [double]$PollSeconds = 1,
    [double]$Stake = 6.0,
    [int]$RestartDelaySeconds = 5,
    [switch]$Continuous,
    [int]$MaxCycles = 1
)

$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$python = "python"
$watchdogTs = Get-Date -Format "yyyyMMdd_HHmmss"
$watchdogLog = Join-Path $repo ("logs\lag_continuation_btc5m_paper_watchdog_" + $watchdogTs + ".log")

Set-Location $repo

$cycle = 0
while ($true) {
    $cycle += 1
    $logFile = Join-Path $repo ("logs\lag_continuation_btc5m_paper_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".jsonl")

    $msg = "[START] " + (Get-Date -Format s) + " cycle=" + $cycle + " run_seconds=" + $RunSeconds + " poll=" + $PollSeconds + " stake=" + $Stake
    Add-Content -Path $watchdogLog -Value $msg
    Write-Host $msg

    & $python "diagnostics_lag_continuation_btc5m_paper_v1.py" `
        "--seconds" ([string]$RunSeconds) `
        "--poll-secs" ([string]$PollSeconds) `
        "--stake" ([string]$Stake) `
        "--log-file" $logFile

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
