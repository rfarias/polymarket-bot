"""
Analise EE final: arquivo 2026-06-04 (resolucao) de btc-1780563600
"""
import duckdb, pathlib, pandas as pd

OUT = pathlib.Path(__file__).parent / "telonex_data"
fpath = str(OUT / "polymarket_quotes_2026-06-04_btc-1780563600.parquet").replace("\\", "/")
resolve_ts = 1780563600

df = duckdb.query(f"SELECT * FROM read_parquet('{fpath}')").df()
df["secs_to_end"] = resolve_ts - df["timestamp_us"] / 1e6
df["bid_price"] = pd.to_numeric(df["bid_price"], errors="coerce")
df["ask_price"] = pd.to_numeric(df["ask_price"], errors="coerce")
df["bid_size"] = pd.to_numeric(df["bid_size"], errors="coerce")

print(f"Total quotes no dia: {len(df)}")

# Janela EE: 25-155s antes da resolucao
ee = df[(df["secs_to_end"] >= 25) & (df["secs_to_end"] <= 155)].copy()
print(f"EE window (25-155s): {len(ee)} quotes")

if len(ee) > 0:
    print(ee[["secs_to_end", "bid_price", "ask_price", "bid_size"]].to_string())
    b_entry = ee["bid_price"].iloc[-1]   # bid mais longe da resolucao (~155s)
    b_exit  = ee["bid_price"].iloc[0]    # bid mais proximo (~25s)
    delta = b_exit - b_entry
    print(f"\nbid@155s = {b_entry:.3f}  bid@25s = {b_exit:.3f}  delta = {delta:.3f}")
    print(f"EE trigger (|delta| >= 0.17): {abs(delta) >= 0.17}")

# Toda a janela final 0-300s
window = df[(df["secs_to_end"] >= 0) & (df["secs_to_end"] <= 300)].copy()
print(f"\n=== RESUMO JANELA 0-300s ({len(window)} quotes) ===")
print(f"bid min: {window['bid_price'].min():.3f}  max: {window['bid_price'].max():.3f}")
print(f"ask min: {window['ask_price'].min():.3f}  max: {window['ask_price'].max():.3f}")

# Verificar se houve sinal EE em qualquer ponto
bid_at_155 = df[df["secs_to_end"] <= 155]["bid_price"].dropna()
bid_at_25  = df[df["secs_to_end"] <= 25]["bid_price"].dropna()
if len(bid_at_155) > 0 and len(bid_at_25) > 0:
    print(f"\nbid ultimo valor @>=155s: {bid_at_155.iloc[0]:.3f}")
    print(f"bid ultimo valor @<=25s: {bid_at_25.iloc[0]:.3f}")
