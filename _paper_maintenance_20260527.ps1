# _paper_maintenance_20260527.ps1
# Reinicia papers (AR + EE), pausa na manutencao Polymarket 09:10-09:20 local,
# retoma as 09:25.

$repo    = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$logFile = "$repo\logs\_paper_maintenance_20260527.log"

function Log($msg) {
    $ts   = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
    $line = "$ts  $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

$stopAt    = [datetime]"2026-05-27 09:05:00"
$restartAt = [datetime]"2026-05-27 09:25:00"

Log "=== PAPER MAINTENANCE 2026-05-27 ==="
Log "Para    : $($stopAt.ToString('HH:mm:ss')) local"
Log "Retoma  : $($restartAt.ToString('HH:mm:ss')) local"

# --- Fase 1: Iniciar runners paper ---
$arArgs = "-ExecutionPolicy Bypass -NoProfile -File `"$repo\scripts\watch_current_almost_resolved_paper.ps1`""
$eeArgs = "-ExecutionPolicy Bypass -NoProfile -File `"$repo\scripts\watch_early_entry_paper.ps1`" -Continuous -RunSeconds 3600 -PollSeconds 0.5 -Qty 6"

Log "Iniciando AR paper watchdog..."
$arProc = Start-Process powershell.exe -ArgumentList $arArgs -WorkingDirectory $repo -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 4
Log "AR paper watchdog PID=$($arProc.Id)"

Log "Iniciando EE paper watchdog..."
$eeProc = Start-Process powershell.exe -ArgumentList $eeArgs -WorkingDirectory $repo -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 4
Log "EE paper watchdog PID=$($eeProc.Id)"

# --- Fase 2: Aguardar hora de parar ---
$now = Get-Date
if ($now -lt $stopAt) {
    $waitSecs = [int]($stopAt - $now).TotalSeconds
    Log "Aguardando $waitSecs s ate parada ($($stopAt.ToString('HH:mm:ss')))..."
    Start-Sleep -Seconds $waitSecs
} else {
    Log "Ja passou da hora de parada - encerrando imediatamente."
}

# --- Fase 3: Encerrar papers ---
Log "Encerrando papers antes da manutencao..."

function Stop-PaperPattern($pattern, $label) {
    $found = @()
    Get-Process python* -ErrorAction SilentlyContinue | ForEach-Object {
        $wmi = Get-WmiObject Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue
        if ($wmi -and $wmi.CommandLine -like "*$pattern*") {
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
            $found += $_.Id
            Log "Encerrado $label python PID=$($_.Id)"
        }
    }
    if ($found.Count -eq 0) { Log "Nenhum processo $label encontrado." }
}

Stop-PaperPattern "almost_resolved_paper" "AR-paper"
Stop-PaperPattern "early_entry_paper"     "EE-paper"
Stop-Process -Id $arProc.Id -Force -ErrorAction SilentlyContinue
Stop-Process -Id $eeProc.Id -Force -ErrorAction SilentlyContinue
Log "Watchdogs encerrados."

# --- Fase 4: Aguardar fim da manutencao ---
$now = Get-Date
if ($now -lt $restartAt) {
    $waitSecs = [int]($restartAt - $now).TotalSeconds
    Log "Manutencao em andamento. Aguardando $waitSecs s ate $($restartAt.ToString('HH:mm:ss'))..."
    Start-Sleep -Seconds $waitSecs
}

# --- Fase 5: Reiniciar papers ---
Log "Reiniciando AR paper..."
Start-Process powershell.exe -ArgumentList $arArgs -WorkingDirectory $repo -WindowStyle Hidden
Start-Sleep -Seconds 4

Log "Reiniciando EE paper..."
Start-Process powershell.exe -ArgumentList $eeArgs -WorkingDirectory $repo -WindowStyle Hidden
Start-Sleep -Seconds 4

$arUp = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    (Get-WmiObject Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine -like "*almost_resolved_paper*"
}
$eeUp = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    (Get-WmiObject Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine -like "*early_entry_paper*"
}

if ($arUp) { Log "AR paper OK PID=$($arUp.Id)" } else { Log "ERRO: AR paper nao subiu." }
if ($eeUp) { Log "EE paper OK PID=$($eeUp.Id)" } else { Log "ERRO: EE paper nao subiu." }
Log "=== FIM DO SCRIPT DE MANUTENCAO ==="
