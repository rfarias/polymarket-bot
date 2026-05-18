$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$host.UI.RawUI.WindowTitle = "GUARDIAN - monitor"
while ($true) {
    Write-Host ("[WATCHDOG] Iniciando guardian monitor " + (Get-Date -Format "s")) -ForegroundColor Cyan
    python diagnostics_current_almost_resolved_guardian_v1.py --side UP --entry-price 0.97 --qty 0 --poll-secs 1.0
    Write-Host ("[WATCHDOG] Reiniciando em 5s... exit=" + $LASTEXITCODE) -ForegroundColor Yellow
    Start-Sleep 5
}
