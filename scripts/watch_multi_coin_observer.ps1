param(
    [int]$RunSeconds = 21600,
    [double]$PollSeconds = 1.5,
    [int]$RestartDelaySeconds = 5,
    [switch]$Continuous,
    [int]$MaxCycles = 1
)

$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$python = "python"
$watchdogTs = Get-Date -Format "yyyyMMdd_HHmmss"
$watchdogLog = Join-Path $repo ("logs\multi_coin_observer_watchdog_" + $watchdogTs + ".log")

Set-Location $repo

$cycle = 0
while ($true) {
    $cycle += 1
    $msg = "[START] " + (Get-Date -Format s) + " cycle=$cycle run=${RunSeconds}s poll=${PollSeconds}s"
    Add-Content -Path $watchdogLog -Value $msg
    Write-Host $msg

    & $python "run_multi_coin_observer.py" "--seconds" ([string]$RunSeconds) "--poll" ([string]$PollSeconds)
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
