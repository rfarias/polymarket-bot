<#
.SYNOPSIS
    Guardian de stop automático para posições manuais no almost-resolved.

.DESCRIPTION
    Monitora uma posição manual aberta e executa o stop automaticamente
    quando os critérios forem atingidos. Usa as credenciais de .env.manual.

.EXAMPLE
    # AUTO-DETECT (recomendado): inicie antes de entrar, o guardian detecta tudo sozinho
    .\scripts\guardian_manual.ps1 -ExecuteStop
    .\scripts\guardian_manual.ps1 -ExecuteStop -Timeframe 15m

    # Manual (quando já sabe exatamente o que entrou):
    .\scripts\guardian_manual.ps1 -Side UP -EntryPrice 0.97 -Qty 50 -ExecuteStop
    .\scripts\guardian_manual.ps1 -Side UP -EntryPrice 0.97 -Qty 50 -ExecuteStop -Timeframe 15m

    # Monitor sem executar (testes):
    .\scripts\guardian_manual.ps1

    # Com slug específico:
    .\scripts\guardian_manual.ps1 -ExecuteStop -Slug btc-updown-15m-1778856900

    # Parâmetros customizados:
    .\scripts\guardian_manual.ps1 -ExecuteStop -MaxLossTicks 4 -PollSecs 0.5
#>

param(
    [ValidateSet("UP","DOWN","")]
    [string]$Side = "",

    [double]$EntryPrice = 0.0,

    [double]$Qty = 0.0,

    [switch]$ExecuteStop,

    [string]$Slug = "",

    [int]$MaxLossTicks = 3,

    [double]$PollSecs = 1.0,

    [int]$Seconds = 0,

    [double]$StopPrice = 0.0,

    [switch]$NoBeep,

    [ValidateSet("5m","15m")]
    [string]$Timeframe = "5m",

    [ValidateSet("","controlled_late_entry","resolved_pullback_limit","extreme_99_limit")]
    [string]$SetupVariant = "",

    [switch]$NoOracle
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$envFile = Join-Path $repo ".env.manual"
if (-not (Test-Path $envFile)) {
    Write-Error "Arquivo .env.manual não encontrado em: $envFile"
    Write-Host "Crie o arquivo com suas credenciais (veja .env.manual como template)."
    exit 1
}

# Verificar se POLY_PRIVATE_KEY está preenchida
$manualContent = Get-Content $envFile
$hasPK = $manualContent | Where-Object { $_ -match "^POLY_PRIVATE_KEY=.+" }
if (-not $hasPK) {
    Write-Error "POLY_PRIVATE_KEY não está preenchida em .env.manual. Adicione sua chave privada antes de usar --ExecuteStop."
    if ($ExecuteStop) { exit 1 }
    Write-Host "[GUARDIAN] Continuando em modo monitor apenas (sem credenciais)."
}

# Construir argumentos
$args_list = @(
    "--poll-secs", [string]$PollSecs,
    "--max-loss-ticks", [string]$MaxLossTicks,
    "--env-file", $envFile
)

if ($Side)        { $args_list += @("--side", $Side) }
if ($EntryPrice -gt 0) { $args_list += @("--entry-price", [string]$EntryPrice) }
if ($Qty -gt 0)   { $args_list += @("--qty", [string]$Qty) }
if ($ExecuteStop) { $args_list += "--execute-stop" }
if ($Slug)        { $args_list += @("--slug", $Slug) }
if ($Seconds -gt 0) { $args_list += @("--seconds", [string]$Seconds) }
if ($StopPrice -gt 0) { $args_list += @("--price-stop", [string]$StopPrice) }
if ($NoBeep)      { $args_list += "--no-beep" }
if ($Timeframe -ne "5m") { $args_list += @("--timeframe", $Timeframe) }
if ($SetupVariant)        { $args_list += @("--setup-variant", $SetupVariant) }
if ($NoOracle)            { $args_list += "--no-oracle" }

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = Join-Path $repo "logs\guardian_manual_${Side}_${ts}.jsonl"
$args_list += @("--log-file", $logFile)

Write-Host ""
Write-Host "========================================"
Write-Host " GUARDIAN MANUAL — CONTA MANUAL"
Write-Host "========================================"
$sideDisplay = if ($Side) { $Side } else { "AUTO-DETECT" }
$entryDisplay = if ($EntryPrice -gt 0) { $EntryPrice } else { "AUTO-DETECT" }
$qtyDisplay   = if ($Qty -gt 0) { $Qty } else { "AUTO-DETECT" }
Write-Host " Side        : $sideDisplay"
Write-Host " Timeframe   : $Timeframe"
Write-Host " Entry Price : $entryDisplay"
Write-Host " Qty         : $qtyDisplay"
Write-Host " Max Loss    : $MaxLossTicks ticks"
Write-Host " Execute Stop: $ExecuteStop"
Write-Host " Log         : $logFile"
Write-Host " Env File    : $envFile"
if ($Slug) { Write-Host " Slug        : $Slug" }
Write-Host "========================================"
Write-Host ""

if (-not $Side) {
    Write-Host "[GUARDIAN] MODO AUTO-DETECT — detectará posição automaticamente ao entrar." -ForegroundColor Cyan
}
if ($ExecuteStop) {
    Write-Host "[GUARDIAN] MODO REAL — STOP AUTOMÁTICO ATIVADO" -ForegroundColor Red
    Write-Host "[GUARDIAN] Se o stop disparar, uma ordem SELL será enviada automaticamente." -ForegroundColor Red
} else {
    Write-Host "[GUARDIAN] MODO MONITOR — apenas alerta, sem ordens reais." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Iniciando... (Ctrl+C para parar)"
Write-Host ""

python "diagnostics_current_almost_resolved_guardian_v1.py" @args_list
