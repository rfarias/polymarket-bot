param(
    [int]$Port = 8766,
    [double]$PollSeconds = 0.5,
    [int]$RestartDelaySeconds = 5,
    [switch]$Continuous,
    [int]$MaxCycles = 999
)

$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$python = "python"
$watchdogTs = Get-Date -Format "yyyyMMdd_HHmmss"
$watchdogLog = Join-Path $repo ("logs\el_monitor_watchdog_" + $watchdogTs + ".log")

Set-Location $repo

$cycle = 0
while ($true) {
    $cycle += 1

    $msg = "[START] " + (Get-Date -Format s) + " cycle=" + $cycle + " port=" + $Port + " poll=" + $PollSeconds
    Add-Content -Path $watchdogLog -Value $msg
    Write-Host $msg

    & $python "run_el_monitor_server.py" "--port" ([string]$Port) "--poll" ([string]$PollSeconds)
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
