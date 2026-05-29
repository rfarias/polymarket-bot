# _pause_maintenance_20260529.ps1
#
# Pausa automatica do runner durante manutencao Polymarket
# Data: 2026-05-29  Janela: 12:10-12:20 UTC (09:10-09:20 local UTC-3)
#
# Uso:
#   Start-Process powershell -ArgumentList "-File _pause_maintenance_20260529.ps1" -WorkingDirectory $PWD
#
# O script:
#   1. Aguarda ate 09:05 local (5 min antes da manutencao)
#   2. Encerra o runner e o watchdog
#   3. Aguarda ate 09:25 local (5 min apos fim previsto)
#   4. Reinicia o watchdog em modo real com os mesmos parametros

$repo    = Split-Path -Parent $MyInvocation.MyCommand.Path
$logFile = "$repo\logs\_maintenance_20260529.log"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
    $line = "$ts  $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

# Horarios locais (UTC-3)
$stopAt    = [datetime]"2026-05-29 09:05:00"   # para 5 min antes
$restartAt = [datetime]"2026-05-29 09:25:00"   # reinicia 5 min apos fim previsto

Log "=== MANUTENCAO POLYMARKET 2026-05-29 ==="
Log "Stop    : $($stopAt.ToString('HH:mm:ss')) local  (12:05 UTC)"
Log "Restart : $($restartAt.ToString('HH:mm:ss')) local  (12:25 UTC)"
Log "Script iniciado"

# --- Fase 1: Aguardar ate hora de parar ---
$now = Get-Date
if ($now -lt $stopAt) {
    $waitSecs = [int]($stopAt - $now).TotalSeconds
    Log "Aguardando $waitSecs s ate parada (ate $($stopAt.ToString('HH:mm:ss')))..."
    Start-Sleep -Seconds $waitSecs
} else {
    Log "Ja passou da hora de parada — encerrando imediatamente."
}

# --- Fase 2: Encerrar runners e watchdogs ---
Log "Encerrando runners antes da manutencao..."
$killed = @()
Get-Process python* -ErrorAction SilentlyContinue | ForEach-Object {
    $p = $_
    $wmi = Get-WmiObject Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue
    if ($wmi -and $wmi.CommandLine -like "*almost_resolved*") {
        $parentPid = $wmi.ParentProcessId
        Stop-Process -Id $p.Id       -Force -ErrorAction SilentlyContinue
        Stop-Process -Id $parentPid  -Force -ErrorAction SilentlyContinue
        $killed += "runner=$($p.Id) watchdog=$parentPid"
        Log "Encerrado: runner=$($p.Id)  watchdog=$parentPid"
    }
}
if ($killed.Count -eq 0) { Log "Nenhum runner ativo encontrado." }

# Verificar se ainda ha runners ativos
Start-Sleep -Seconds 2
$still = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    (Get-WmiObject Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine -like "*almost_resolved*"
}
if ($still) {
    Log "AVISO: ainda ha runners. Forcando encerramento..."
    $still | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
}
Log "Runners encerrados. Manutencao pode prosseguir."

# --- Fase 3: Aguardar fim da manutencao ---
$now = Get-Date
if ($now -lt $restartAt) {
    $waitSecs = [int]($restartAt - $now).TotalSeconds
    Log "Manutencao em andamento. Aguardando $waitSecs s ate $($restartAt.ToString('HH:mm:ss'))..."
    Start-Sleep -Seconds $waitSecs
}

# --- Fase 4: Reiniciar watchdog ---
Log "Reiniciando watchdog apos manutencao..."
$watchArgs = "-ExecutionPolicy Bypass -NoProfile -File `"$repo\scripts\watch_current_almost_resolved_real.ps1`" -ArmReal -ArmEE -Qty 6 -RunSeconds 1800 -PollSeconds 0.5 -Continuous"
Start-Process powershell.exe -ArgumentList $watchArgs -WorkingDirectory $repo -WindowStyle Normal
Start-Sleep -Seconds 8

# Confirmar que subiu
$newRunner = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    (Get-WmiObject Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine -like "*almost_resolved*"
}
if ($newRunner) {
    Log "Watchdog reiniciado OK. Runner PID=$($newRunner.Id)"
} else {
    Log "ERRO: runner nao subiu apos restart. Verificar manualmente."
}
Log "=== FIM DO SCRIPT DE MANUTENCAO ==="
