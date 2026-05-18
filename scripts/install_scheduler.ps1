<#
.SYNOPSIS
    Installs a Windows Task Scheduler watchdog that auto-starts the bot on logon.

.DESCRIPTION
    Creates a scheduled task that launches the Polymarket watchdog automatically
    when you log into Windows. This ensures the bot resumes after power loss,
    reboot, or internet recovery without any manual intervention.

    The task runs the watchdog in monitor-only mode by default (no real orders).
    Use -ArmReal to register the task in real-order mode.

    Run once to install. After that the task starts automatically on logon.
    To remove: .\scripts\install_scheduler.ps1 -Uninstall

.EXAMPLE
    # Install in monitor mode (safe default):
    .\scripts\install_scheduler.ps1

    # Install in real-order mode:
    .\scripts\install_scheduler.ps1 -ArmReal -Qty 6 -RunSeconds 1800

    # Remove the task:
    .\scripts\install_scheduler.ps1 -Uninstall
#>

param(
    [switch]$ArmReal,
    [int]$Qty = 6,
    [int]$RunSeconds = 1800,
    [double]$PollSeconds = 0.5,
    [switch]$Uninstall,
    [string]$TaskName = "PolymarketBotWatchdog"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

if ($Uninstall) {
    Write-Host ""
    Write-Host "Removing scheduled task: $TaskName"
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[OK] Task '$TaskName' removed."
    } catch {
        Write-Host "[WARN] Task not found or could not be removed: $_"
    }
    exit 0
}

# Build the watchdog command
$watchdogScript = Join-Path $repo "scripts\watch_current_almost_resolved_real.ps1"
if (-not (Test-Path $watchdogScript)) {
    Write-Error "Watchdog script not found: $watchdogScript"
    exit 1
}

$psArgs = "-NonInteractive -WindowStyle Normal -File `"$watchdogScript`" -Continuous -RunSeconds $RunSeconds -PollSeconds $PollSeconds -Qty $Qty"
if ($ArmReal) {
    $psArgs += " -ArmReal"
}

Write-Host ""
Write-Host "========================================"
Write-Host " TASK SCHEDULER INSTALL"
Write-Host "========================================"
Write-Host " Task Name   : $TaskName"
Write-Host " Trigger     : On logon (current user)"
Write-Host " Script      : $watchdogScript"
Write-Host " Args        : $psArgs"
Write-Host " Mode        : $(if ($ArmReal) { 'REAL ORDERS' } else { 'MONITOR ONLY' })"
Write-Host "========================================"

if ($ArmReal) {
    Write-Host ""
    Write-Host "[WARNING] This will post REAL ORDERS after every reboot/logon." -ForegroundColor Red
    Write-Host "          Press Ctrl+C to abort, or wait 5 seconds to continue..." -ForegroundColor Red
    Start-Sleep -Seconds 5
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $psArgs `
    -WorkingDirectory $repo

# Trigger: on logon for current user
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

# Settings: restart on failure, run missed task if PC was off
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

# Principal: run as current user (interactive, so the console window is visible)
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

try {
    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Host ""
        Write-Host "[UPDATE] Task '$TaskName' already exists — updating..."
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Polymarket bot watchdog — auto-restarts runner on Windows logon." | Out-Null

    Write-Host ""
    Write-Host "[OK] Task '$TaskName' registered successfully." -ForegroundColor Green
    Write-Host ""
    Write-Host "The watchdog will start automatically on next logon."
    Write-Host "To run it now without rebooting:"
    Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host ""
    Write-Host "To check status:"
    Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName'"
    Write-Host ""
    Write-Host "To remove:"
    Write-Host "  .\scripts\install_scheduler.ps1 -Uninstall"
} catch {
    Write-Host ""
    Write-Host "[ERROR] Failed to register task: $_" -ForegroundColor Red
    Write-Host "Try running this script as Administrator if permission errors occur."
    exit 1
}
