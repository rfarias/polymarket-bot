param(
    [int]$RunSeconds = 3600,
    [int]$PollSeconds = 60
)

$ErrorActionPreference = "Continue"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$watchdogLog = Join-Path $repoRoot ("logs/portfolio_settlement_watchdog_{0}.log" -f $timestamp)

while ($true) {
    $env:POLY_PORTFOLIO_SETTLEMENT_ENABLED = "true"
    $env:POLY_PORTFOLIO_SETTLEMENT_POLL_SECS = [string]$PollSeconds
    $env:POLY_PORTFOLIO_SETTLEMENT_RUN_SECONDS = [string]$RunSeconds

    "[{0}] starting portfolio settlement cycle" -f (Get-Date -Format s) | Tee-Object -FilePath $watchdogLog -Append
    python run_portfolio_settlement_v1.py --seconds $RunSeconds
    "[{0}] cycle exited code=$LASTEXITCODE" -f (Get-Date -Format s) | Tee-Object -FilePath $watchdogLog -Append
    Start-Sleep -Seconds 5
}
