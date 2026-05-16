<#
.SYNOPSIS
    Guardian de stop automático para posições manuais no almost-resolved.

.DESCRIPTION
    Monitora uma posição manual aberta e executa o stop automaticamente
    quando os critérios forem atingidos. Usa as credenciais de .env.manual.

.EXAMPLE
    # Monitorar apenas (sem executar — para testar):
    .\scripts\guardian_manual.ps1 -Side UP -EntryPrice 0.97 -Qty 50

    # Monitorar e executar stop real:
    .\scripts\guardian_manual.ps1 -Side UP -EntryPrice 0.97 -Qty 50 -ExecuteStop

    # Com slug específico (se não for o slot current):
    .\scripts\guardian_manual.ps1 -Side DOWN -EntryPrice 0.98 -Qty 200 -ExecuteStop -Slug btc-updown-5m-1778856900

    # Parâmetros customizados:
    .\scripts\guardian_manual.ps1 -Side UP -EntryPrice 0.96 -Qty 100 -ExecuteStop -MaxLossTicks 4 -PollSecs 0.5
#>

param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("UP","DOWN")]
    [string]$Side,

    [Parameter(Mandatory=$true)]
    [double]$EntryPrice,

    [Parameter(Mandatory=$true)]
    [double]$Qty,

    [switch]$ExecuteStop,

    [string]$Slug = "",

    [int]$MaxLossTicks = 3,

    [double]$PollSecs = 1.0,

    [int]$Seconds = 0,

    [double]$StopPrice = 0.0,

    [switch]$NoBeep
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
    "--side", $Side,
    "--entry-price", [string]$EntryPrice,
    "--qty", [string]$Qty,
    "--poll-secs", [string]$PollSecs,
    "--max-loss-ticks", [string]$MaxLossTicks,
    "--env-file", $envFile
)

if ($ExecuteStop) { $args_list += "--execute-stop" }
if ($Slug)        { $args_list += @("--slug", $Slug) }
if ($Seconds -gt 0) { $args_list += @("--seconds", [string]$Seconds) }
if ($StopPrice -gt 0) { $args_list += @("--price-stop", [string]$StopPrice) }
if ($NoBeep)      { $args_list += "--no-beep" }

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = Join-Path $repo "logs\guardian_manual_${Side}_${ts}.jsonl"
$args_list += @("--log-file", $logFile)

Write-Host ""
Write-Host "========================================"
Write-Host " GUARDIAN MANUAL — CONTA MANUAL"
Write-Host "========================================"
Write-Host " Side        : $Side"
Write-Host " Entry Price : $EntryPrice"
Write-Host " Qty         : $Qty"
Write-Host " Max Loss    : $MaxLossTicks ticks"
Write-Host " Execute Stop: $ExecuteStop"
Write-Host " Log         : $logFile"
Write-Host " Env File    : $envFile"
if ($Slug) { Write-Host " Slug        : $Slug" }
Write-Host "========================================"
Write-Host ""

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
