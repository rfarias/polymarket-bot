<#
.SYNOPSIS
    Inicia paper trading + guardian monitor para rodar o fim de semana.

.DESCRIPTION
    Abre duas janelas separadas:
      1. Paper trader (almost-resolved) — watchdog com reinício automático
      2. Guardian em modo monitor — loga decisões HOLD/WARN/STOP sem executar ordens

    Configura o Windows para não entrar em sleep enquanto roda.

.EXAMPLE
    .\scripts\weekend_monitor.ps1
#>

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# Impedir sleep enquanto o script principal estiver rodando
Write-Host "[SETUP] Desativando sleep do Windows..."
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
Write-Host "[SETUP] Sleep desativado. Reative manualmente depois com:"
Write-Host "        powercfg /change standby-timeout-ac 30"
Write-Host ""

# Janela 1: Paper trader com watchdog
Write-Host "[LAUNCH] Abrindo janela de paper trading..."
Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "Set-Location '$repo'; `$host.UI.RawUI.WindowTitle = '📄 PAPER — almost-resolved'; while (`$true) { Write-Host ('[WATCHDOG] Iniciando paper trader ' + (Get-Date -Format 's')) -ForegroundColor Cyan; python diagnostics_current_almost_resolved_paper_v1.py --seconds 21600 --poll-secs 2.0; Write-Host ('[WATCHDOG] Reiniciando em 5s... exit=' + `$LASTEXITCODE) -ForegroundColor Yellow; Start-Sleep 5 }"
)

Start-Sleep -Seconds 3

# Janela 2: Guardian monitor (sem --execute-stop, posição UP representativa)
Write-Host "[LAUNCH] Abrindo janela do guardian monitor..."
Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "Set-Location '$repo'; `$host.UI.RawUI.WindowTitle = '👁 GUARDIAN — monitor'; while (`$true) { Write-Host ('[WATCHDOG] Iniciando guardian monitor ' + (Get-Date -Format 's')) -ForegroundColor Cyan; python diagnostics_current_almost_resolved_guardian_v1.py --side UP --entry-price 0.97 --qty 0 --poll-secs 1.0; Write-Host ('[WATCHDOG] Reiniciando em 5s... exit=' + `$LASTEXITCODE) -ForegroundColor Yellow; Start-Sleep 5 }"
)

Write-Host ""
Write-Host "========================================"
Write-Host " Duas janelas abertas:"
Write-Host "   📄 PAPER   — paper trading watchdog"
Write-Host "   👁 GUARDIAN — monitor de sinais"
Write-Host ""
Write-Host " Para parar tudo: feche as duas janelas."
Write-Host " Para reativar sleep ao terminar:"
Write-Host "   powercfg /change standby-timeout-ac 30"
Write-Host "========================================"
