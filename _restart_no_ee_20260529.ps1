# _restart_no_ee_20260529.ps1
# Reinicia o watchdog sem -ArmEE (EE em shadow mode — rastreia mas nao posta ordens reais).
# Usar apos o stop automatico de meia-noite ou manualmente.

$repo    = Split-Path -Parent $MyInvocation.MyCommand.Path
$logFile = "$repo\logs\_restart_no_ee_20260529.log"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
    $line = "$ts  $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

Log "=== RESTART SEM EE 2026-05-29 ==="

# Encerrar runners existentes
Log "Encerrando runners ativos..."
Get-Process python* -ErrorAction SilentlyContinue | ForEach-Object {
    $wmi = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue
    if ($wmi -and $wmi.CommandLine -like "*almost_resolved*") {
        $parentPid = $wmi.ParentProcessId
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        Stop-Process -Id $parentPid -Force -ErrorAction SilentlyContinue
        Log "Encerrado: runner=$($_.Id) watchdog=$parentPid"
    }
}

Start-Sleep -Seconds 3

# Reiniciar SEM -ArmEE  (EE fica em shadow mode)
Log "Reiniciando watchdog sem EE real..."
$watchArgs = "-ExecutionPolicy Bypass -NoProfile -File `"$repo\scripts\watch_current_almost_resolved_real.ps1`" -ArmReal -Qty 6 -RunSeconds 1800 -PollSeconds 0.5 -Continuous"
Start-Process powershell.exe -ArgumentList $watchArgs -WorkingDirectory $repo -WindowStyle Normal
Start-Sleep -Seconds 8

$runner = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine -like "*almost_resolved*"
}
if ($runner) {
    Log "Runner subiu OK. PID=$($runner.Id). EE em shadow mode (sem ordens reais)."
} else {
    Log "ERRO: runner nao subiu. Verificar manualmente."
}
Log "=== FIM ==="
