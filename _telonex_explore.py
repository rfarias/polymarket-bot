"""
Exploração Telonex - schema e mercados BTC 5-min
"""
import pathlib, duckdb, json
import urllib.request

KEY = "tlx_f9d340fc080239d2be49a84ebe878ea8"
BASE = "https://api.telonex.io/v1"
OUT = pathlib.Path(__file__).parent / "telonex_data"
mkt_fwd = str(OUT / "polymarket_markets.parquet").replace("\\", "/")

# Schema
schema = duckdb.query(f"DESCRIBE SELECT * FROM read_parquet('{mkt_fwd}')").df()
cols = list(schema["column_name"])
print("COLUNAS:", cols)

# Primeira linha
row = duckdb.query(f"SELECT * FROM read_parquet('{mkt_fwd}') LIMIT 1").df()
for c in cols:
    print(f"  {c}: {str(row[c].iloc[0])[:100]}")

# Buscar BTC 5-min pelo slug
print("\n=== MERCADOS btc-updown-5m ===")
btc = duckdb.query(f"""
    SELECT slug, market_id, outcome_0, outcome_1, asset_id_0, asset_id_1,
           status,
           epoch_ms(CAST(start_date_us/1000 AS BIGINT))::DATE as start_date,
           epoch_ms(CAST(end_date_us/1000 AS BIGINT))::DATE as end_date,
           trades_from, trades_to, quotes_from, quotes_to
    FROM read_parquet('{mkt_fwd}')
    WHERE slug LIKE 'btc-updown-5m-%'
    ORDER BY end_date_us DESC
    LIMIT 20
""").df()
print(btc.to_string())

print("\n=== TOTAIS BTC 5-min ===")
total = duckdb.query(f"""
    SELECT COUNT(*) as n_markets,
           epoch_ms(CAST(MIN(start_date_us)/1000 AS BIGINT))::DATE as primeiro,
           epoch_ms(CAST(MAX(end_date_us)/1000 AS BIGINT))::DATE as ultimo,
           COUNT(CASE WHEN quotes_from IS NOT NULL THEN 1 END) as com_quotes,
           COUNT(CASE WHEN trades_from IS NOT NULL THEN 1 END) as com_trades
    FROM read_parquet('{mkt_fwd}')
    WHERE slug LIKE 'btc-updown-5m-%'
""").df()
print(total.to_string())

print("\n=== POR DIA (ultimos 10) ===")
por_dia = duckdb.query(f"""
    SELECT epoch_ms(CAST(end_date_us/1000 AS BIGINT))::DATE as dia,
           COUNT(*) as n_mercados,
           COUNT(CASE WHEN quotes_from IS NOT NULL THEN 1 END) as com_quotes
    FROM read_parquet('{mkt_fwd}')
    WHERE slug LIKE 'btc-updown-5m-%'
    GROUP BY 1
    ORDER BY 1 DESC
    LIMIT 10
""").df()
print(por_dia.to_string())

# Pegar sample recente para availability
sample = duckdb.query(f"""
    SELECT slug, asset_id_0, outcome_0, asset_id_1, outcome_1,
           quotes_from, quotes_to
    FROM read_parquet('{mkt_fwd}')
    WHERE slug LIKE 'btc-updown-5m-%'
      AND quotes_from IS NOT NULL
    ORDER BY end_date_us DESC
    LIMIT 1
""").df()
print("\n=== AMOSTRA PARA AVAILABILITY ===")
print(sample.T.to_string())

if len(sample) > 0:
    s = sample.iloc[0]
    slug = s["slug"]
    print(f"\n=== AVAILABILITY: {slug} ===")
    try:
        url = f"{BASE}/availability/polymarket?slug={slug}&outcome={s['outcome_0']}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        print(json.dumps(data, indent=2, default=str))
    except Exception as e:
        print(f"ERRO: {e}")
