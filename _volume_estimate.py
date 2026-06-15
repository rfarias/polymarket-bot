"""
Estima volume de dados Telonex necessario para backtest EE/AR/SA2.
"""
import duckdb, pathlib

OUT = pathlib.Path(__file__).parent / "telonex_data"
mkt_fwd = str(OUT / "polymarket_markets.parquet").replace("\\", "/")

# ── Estatisticas do periodo disponivel ────────────────────────────────────────
print("=== PERIODO DISPONIVEL (BTC 5-min) ===")
periodo = duckdb.query(f"""
    SELECT
        COUNT(DISTINCT market_id)/2 as n_mercados_unicos,
        COUNT(DISTINCT CAST(epoch_ms(CAST(end_date_us/1000 AS BIGINT))::DATE AS VARCHAR)) as n_dias,
        MIN(epoch_ms(CAST(end_date_us/1000 AS BIGINT))::DATE) as primeiro_dia,
        MAX(epoch_ms(CAST(end_date_us/1000 AS BIGINT))::DATE) as ultimo_dia,
        COUNT(CASE WHEN quotes_from IS NOT NULL THEN 1 END) / 2 as com_quotes
    FROM read_parquet('{mkt_fwd}')
    WHERE slug LIKE 'btc-updown-5m-%'
""").df()
print(periodo.to_string())

# ── Downloads necessarios por cenario ─────────────────────────────────────────
print("\n=== DOWNLOADS NECESSARIOS POR CENARIO ===")

# Dados empiricos do nosso teste
FILE_KB      = 706       # KB por arquivo de resolution-day (quotes)
SIGNAL_RATE  = 0.01      # ~1% dos mercados disparam EE (conservador, vel>=0.17)
MKTS_PER_DAY = 288       # mercados BTC 5-min por dia (somente outcome=Up)

# Quantidade de dias historicos disponíveis
n_dias_total = 171       # dez/2025 a jun/2026 (~6 meses)
n_mkts_total = n_dias_total * MKTS_PER_DAY

cenarios = [
    ("Minimo EE (50 sinais)",      50,  int(50 / SIGNAL_RATE)),
    ("Robusto EE (200 sinais)",   200,  int(200 / SIGNAL_RATE)),
    ("1 mes completo (Jun25-Jun26)", MKTS_PER_DAY * 30, MKTS_PER_DAY * 30),
    ("Historico completo (out25-jun26)", n_mkts_total, n_mkts_total),
]

print(f"\n{'Cenario':<45} {'Downloads':>10} {'GB':>8} {'Sinais EE (est)':>16}")
print("-" * 82)
for nome, sinais, n_downloads in cenarios:
    gb = n_downloads * FILE_KB / 1024 / 1024
    ee_sinais = int(n_downloads * SIGNAL_RATE)
    print(f"{nome:<45} {n_downloads:>10,} {gb:>8.1f} {ee_sinais:>16,}")

# ── Custo vs valor ────────────────────────────────────────────────────────────
print("\n=== CUSTO-BENEFICIO ===")
print(f"Plano Plus: $79/mes (downloads ilimitados)")
print(f"Plano Free: 5 downloads totais (esgotado)")
print()

# Arquivo de resolucao = unico necessario (criacao tem apenas ~17 rows)
# Na pratica, so precisamos baixar outcome_0 (Up) de cada mercado
# Down e simetrico, EL pode ser identificado pelo bid

print("=== ESTRATEGIA DE DOWNLOAD (otimizada) ===")
print(f"Arquivo resolution-day por mercado: ~706 KB")
print(f"So outcome='Up' necessario (Down e espelho do livro)")
print(f"Periodo recomendado para backtest out-of-sample: mai-jun/2026")
print(f"  (pos-calibracao das gates EE — dados nao vistos)")

mai_jun = duckdb.query(f"""
    SELECT
        COUNT(DISTINCT market_id)/2 as n_mercados,
        COUNT(DISTINCT CAST(epoch_ms(CAST(end_date_us/1000 AS BIGINT))::DATE AS VARCHAR)) as n_dias
    FROM read_parquet('{mkt_fwd}')
    WHERE slug LIKE 'btc-updown-5m-%'
      AND quotes_to >= '2026-05-01'
      AND quotes_to <= '2026-06-06'
      AND quotes_from IS NOT NULL
""").df()
n_mkts_mai_jun = int(mai_jun.iloc[0]["n_mercados"])
n_dias_mai_jun = int(mai_jun.iloc[0]["n_dias"])
print(f"  mai-jun/2026: {n_mkts_mai_jun:,} mercados, {n_dias_mai_jun} dias")
print(f"  Volume: {n_mkts_mai_jun * FILE_KB / 1024 / 1024:.1f} GB")
print(f"  Sinais EE esperados: ~{int(n_mkts_mai_jun * SIGNAL_RATE)}")

print()
print("=== ALTERNATIVAS ===")
print(f"A) Plano Plus 1 mes ($79):")
print(f"   - Historico completo out25-jun26: {n_mkts_total:,} downloads, {n_mkts_total*FILE_KB/1024/1024:.0f} GB")
print(f"   - ~{int(n_mkts_total*SIGNAL_RATE)} sinais EE com bid real")
print(f"   - Definitivo: valida vel>=0.17, SA2, n_s180>=6, EE_ENTRY_HI=0.85")
print()
print(f"B) Sem Telonex (gratis, ja temos):")
print(f"   - BrockMisner: 5.817 mercados, 37 sinais EE (mid-price, nao bid real)")
print(f"   - Paper logs: ~50-100 trades reais (cresce diariamente)")
print(f"   - Resultado: validacao parcial, sem bid real para SA2")
print()

# ROI estimado
print("=== ROI ESTIMADO ===")
print("Bot atual (6 shares, EE no paper):")
print("  avg_pnl EE paper = +$0.44/trade (CLAUDE.md, vel>=0.17)")
print("  ~2-3 sinais EE/dia → $0.88-1.32/dia estimado")
print("  Bot real (outro PC): validar com logs reais pos-pull")
print()
print("Com Telonex ($79/mes):")
print("  + Valida SA2 (fill 88% → +$33/10d estimado com qty=6)")
print("  + Valida n_s180>=6 gate (WR 65→95%)")
print("  + Define EE_ENTRY_HI=0.85 com confiança estatistica")
print("  + Out-of-sample mai-jun/2026 (evita look-ahead do BrockMisner)")
print()
print("Breakeven: SA2 validado + gate WR boost → $33/10d extra")
print("  com qty=6, 10 dias = $33 > $26 (1/3 do mes) → ROI positivo em ~1 mes")
