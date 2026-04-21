$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$python = "python"
$watchdogTs = Get-Date -Format "yyyyMMdd_HHmmss"
$watchdogLog = Join-Path $repo ("logs\current_almost_resolved_watchdog_" + $watchdogTs + ".log")

Set-Location $repo

while ($true) {
    Add-Content -Path $watchdogLog -Value ("[START] " + (Get-Date -Format s))
    & $python "diagnostics_current_almost_resolved_paper_v1.py" "--seconds" "21600" "--poll-secs" "2.0"
    $exitCode = $LASTEXITCODE
    Add-Content -Path $watchdogLog -Value ("[EXIT] " + (Get-Date -Format s) + " code=" + $exitCode)
    Start-Sleep -Seconds 5
}
