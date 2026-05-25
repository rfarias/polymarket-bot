# _run_real_analysis.ps1
# Roda todas as simulacoes de analise EE nos logs do runner real.
# Executar no outro PC (casa) onde ficam os logs reais.
#
# Uso:
#   .\scripts\run_real_analysis.ps1           # roda e salva resultados
#   .\scripts\run_real_analysis.ps1 -Push     # roda, salva e faz git push
#
# Pre-requisito: runner real ja rodou e gerou logs/ee_real_*/ee_real.jsonl

param(
    [switch]$Push
)

Set-Location $PSScriptRoot

# ---------------------------------------------------------------------------
# 1. Verificar logs reais
# ---------------------------------------------------------------------------
$realLogs = Get-ChildItem "logs\ee_real_*\ee_real.jsonl" -ErrorAction SilentlyContinue
if ($realLogs.Count -eq 0) {
    Write-Host "ERRO: Nenhum log real encontrado em logs\ee_real_*\ee_real.jsonl" -ForegroundColor Red
    Write-Host "Verifique se o runner real ja rodou e gerou logs." -ForegroundColor Yellow
    exit 1
}
Write-Host "Logs reais encontrados: $($realLogs.Count) arquivo(s)" -ForegroundColor Green
$realLogs | ForEach-Object { Write-Host "  $_" }

# Contar trades reais
$tradeCount = 0
foreach ($lp in $realLogs) {
    $tradeCount += (Get-Content $lp | Where-Object { $_ -match '"ee_real_closed"' }).Count
}
Write-Host "Trades fechados estimados: $tradeCount" -ForegroundColor Cyan

if ($tradeCount -lt 5) {
    Write-Host "AVISO: Menos de 5 trades reais. Resultados podem ser instáveis." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 2. git pull antes de rodar
# ---------------------------------------------------------------------------
Write-Host "`nAtualizando repositorio..." -ForegroundColor Cyan
git pull
if ($LASTEXITCODE -ne 0) {
    Write-Host "AVISO: git pull falhou. Continuando com versao local." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 3. Configurar output
# ---------------------------------------------------------------------------
$ts        = Get-Date -Format "yyyyMMdd_HHmmss"
$outDir    = "analysis_results"
$outFile   = "$outDir\real_analysis_$ts.txt"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$LOGS = "logs/ee_real_*/ee_real.jsonl"

Write-Host "`nSalvando resultados em: $outFile" -ForegroundColor Cyan

# Cabecalho
@"
================================================================================
  ANALISE EE RUNNER REAL — $ts
  Logs: $LOGS
  Trades fechados: $tradeCount
================================================================================

"@ | Out-File $outFile -Encoding utf8

# ---------------------------------------------------------------------------
# 4. Rodar cada script e salvar output
# ---------------------------------------------------------------------------
$scripts = @(
    @{ name = "EL Flip (com stop=0.45)";   cmd = "python _sim_el_flip.py --logs `"$LOGS`"" },
    @{ name = "EL Flip (sem stop)";         cmd = "python _sim_el_flip.py --logs `"$LOGS`" --no-stop" },
    @{ name = "EL Flip (gap>=0.20 no-stop)";cmd = "python _sim_el_flip.py --logs `"$LOGS`" --no-stop --flip-gap 0.20" },
    @{ name = "Full Universe";              cmd = "python _sim_full_universe.py --logs `"$LOGS`"" },
    @{ name = "EL Bid Stability (cat A)";   cmd = "python _sim_el_bid_stability.py --logs `"$LOGS`" --cat A" },
    @{ name = "3 Gates";                    cmd = "python _sim_3gates.py --logs `"$LOGS`"" },
    @{ name = "Dip Strategies";             cmd = "python _sim_dip_strategies.py --logs `"$LOGS`"" },
    @{ name = "BTC Momentum";               cmd = "python _sim_btc_momentum.py --logs `"$LOGS`"" }
)

foreach ($s in $scripts) {
    Write-Host "`n>>> $($s.name)..." -ForegroundColor Yellow
    "`n$('='*80)`n  $($s.name.ToUpper())`n$('='*80)`n" | Out-File $outFile -Encoding utf8 -Append

    $result = Invoke-Expression $s.cmd 2>&1
    $result | Out-File $outFile -Encoding utf8 -Append
    $result | Write-Host
}

Write-Host "`n[OK] Analise concluida. Output em: $outFile" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 5. Copiar logs reais para test_data (para commitar e enviar ao PC de dev)
# ---------------------------------------------------------------------------
Write-Host "`nCopiando logs reais para test_data/ee_real/..." -ForegroundColor Cyan
$destBase = "test_data\ee_real"
New-Item -ItemType Directory -Force -Path $destBase | Out-Null

foreach ($lp in $realLogs) {
    $sessionDir = $lp.Directory.Name
    $dest       = "$destBase\$sessionDir"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item $lp.FullName -Destination "$dest\ee_real.jsonl" -Force
    Write-Host "  Copiado: $sessionDir"
}

# ---------------------------------------------------------------------------
# 6. Commit e push (opcional)
# ---------------------------------------------------------------------------
if ($Push) {
    Write-Host "`nCommitando resultados..." -ForegroundColor Cyan
    git add $outFile test_data\ee_real\
    git commit -m "Add real runner EE logs and analysis results ($ts)"
    git push
    Write-Host "[OK] Push concluido." -ForegroundColor Green
} else {
    Write-Host "`nPara commitar e enviar ao PC de dev, rode com -Push:" -ForegroundColor Yellow
    Write-Host "  .\scripts\run_real_analysis.ps1 -Push" -ForegroundColor White
    Write-Host "Ou manualmente:"
    Write-Host "  git add analysis_results\ test_data\ee_real\"
    Write-Host "  git commit -m 'Add real runner logs + analysis'"
    Write-Host "  git push"
}
