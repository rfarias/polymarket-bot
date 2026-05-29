param(
    [double]$PollSeconds = 1.0,
    [int]$RestartDelaySeconds = 5,
    [switch]$Continuous,
    [int]$MaxCycles = 1
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$python = "python"
$watchdogTs = Get-Date -Format "yyyyMMdd_HHmmss"
$watchdogLog = Join-Path $repo ("logs\market_monitor_watchdog_" + $watchdogTs + ".log")

Set-Location $repo

$cycle = 0
while ($true) {
    $cycle += 1
    $msg = "[START] " + (Get-Date -Format s) + " cycle=$cycle poll=${PollSeconds}s"
    Add-Content -Path $watchdogLog -Value $msg
    Write-Host $msg

    & $python "run_market_monitor.py" "--poll" ([string]$PollSeconds)
    $exitCode = $LASTEXITCODE

    $msg = "[EXIT] " + (Get-Date -Format s) + " cycle=$cycle code=$exitCode"
    Add-Content -Path $watchdogLog -Value $msg
    Write-Host $msg

    if (-not $Continuous -and $cycle -ge $MaxCycles) {
        Write-Host "[STOP] completed $cycle cycle(s)"
        break
    }
    Write-Host "[RESTART] aguardando $RestartDelaySeconds s..."
    Start-Sleep -Seconds $RestartDelaySeconds
}
