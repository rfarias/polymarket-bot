import httpx, duckdb, pathlib, pandas as pd

KEY = "tlx_f9d340fc080239d2be49a84ebe878ea8"
OUT = pathlib.Path(__file__).parent / "telonex_data"
mkt_fwd = str(OUT / "polymarket_markets.parquet").replace("\\", "/")

# Pegar asset_id do mercado btc-updown-5m-1780563600
# Resolve em 2026-06-04 09:00 UTC | quotes_from=2026-06-03, quotes_to=2026-06-05
asset = str(duckdb.query(f"""
    SELECT asset_id_0 FROM read_parquet('{mkt_fwd}')
    WHERE slug = 'btc-updown-5m-1780563600' LIMIT 1
""").df().iloc[0, 0])
print("asset_id:", asset)

# Tentar download do dia de resolucao (2026-06-04)
url = "https://api.telonex.io/v1/downloads/polymarket/quotes/2026-06-04"
with httpx.Client(follow_redirects=True, timeout=60) as c:
    r = c.get(url, headers={"Authorization": f"Bearer {KEY}"}, params={"asset_id": asset})
    print(f"Status: {r.status_code}  Size: {len(r.content)/1024:.1f} KB")
    if r.status_code == 200:
        dest = OUT / "polymarket_quotes_2026-06-04_btc-1780563600.parquet"
        dest.write_bytes(r.content)
        fwd = str(dest).replace("\\", "/")
        df = duckdb.query(f"SELECT * FROM read_parquet('{fwd}')").df()
        df["ts_sec"] = df["timestamp_us"] / 1e6
        df["secs_to_end"] = 1780563600 - df["ts_sec"]
        df["bid_price"] = pd.to_numeric(df["bid_price"], errors="coerce")
        df["ask_price"] = pd.to_numeric(df["ask_price"], errors="coerce")
        window = df[(df["secs_to_end"] >= 0) & (df["secs_to_end"] <= 300)].copy()
        print(f"Total quotes: {len(df)}, nos ultimos 300s: {len(window)}")
        print("\nTodos os quotes (ordenados por secs_to_end):")
        print(df.sort_values("secs_to_end")[["secs_to_end","bid_price","ask_price","bid_size"]].to_string())
        if len(window) > 0:
            print("\n=== JANELA EE (25-155s) ===")
            ee = window[(window["secs_to_end"] >= 25) & (window["secs_to_end"] <= 155)]
            print(ee[["secs_to_end","bid_price","ask_price"]].to_string())
    elif r.status_code in (402, 429):
        print(f"CREDITOS ESGOTADOS ou rate limit: {r.text[:300]}")
    else:
        print(f"Erro: {r.text[:300]}")
