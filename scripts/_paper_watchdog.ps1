$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$host.UI.RawUI.WindowTitle = "PAPER - almost-resolved"
while ($true) {
    Write-Host ("[WATCHDOG] Iniciando paper trader " + (Get-Date -Format "s")) -ForegroundColor Cyan
    python diagnostics_current_almost_resolved_paper_v1.py --seconds 21600 --poll-secs 2.0
    Write-Host ("[WATCHDOG] Reiniciando em 5s... exit=" + $LASTEXITCODE) -ForegroundColor Yellow
    Start-Sleep 5
}
