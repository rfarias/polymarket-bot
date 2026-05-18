<#
.SYNOPSIS
    Inicia paper trading + guardian monitor para rodar o fim de semana.
.EXAMPLE
    .\scripts\weekend_monitor.ps1
#>

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

Write-Host "[SETUP] Desativando sleep do Windows..."
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
Write-Host "[SETUP] Sleep desativado."
Write-Host ""

Write-Host "[LAUNCH] Abrindo janela de paper trading..."
Start-Process powershell -ArgumentList "-NoExit", "-File", "$repo\scripts\_paper_watchdog.ps1"

Start-Sleep -Seconds 3

Write-Host "[LAUNCH] Abrindo janela do guardian monitor..."
Start-Process powershell -ArgumentList "-NoExit", "-File", "$repo\scripts\_guardian_watchdog.ps1"

Write-Host ""
Write-Host "========================================"
Write-Host " Duas janelas abertas:"
Write-Host "   PAPER   - paper trading watchdog"
Write-Host "   GUARDIAN - monitor de sinais"
Write-Host ""
Write-Host " Para parar: feche as duas janelas."
Write-Host " Para reativar sleep ao terminar:"
Write-Host "   powercfg /change standby-timeout-ac 30"
Write-Host "========================================"
