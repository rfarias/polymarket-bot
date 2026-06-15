"""
Analise dos downloads BTC 5-min Telonex.
Verifica schema e comportamento do bid nos ultimos 300s antes da resolucao.
"""
import pathlib, duckdb
import pandas as pd

OUT = pathlib.Path(__file__).parent / "telonex_data"

# Todos os parquets de quotes BTC baixados
files = {
    "btc-updown-5m-1780650600": (OUT/"polymarket_quotes_2026-06-04_btc-updown-5m-1780650600_Up.parquet", 1780650600),
    "btc-updown-5m-1780563600": (OUT/"polymarket_quotes_2026-06-03_btc-updown-5m-1780563600.parquet",   1780563600),
    "btc-updown-5m-1780579200": (OUT/"polymarket_quotes_2026-06-03_btc-updown-5m-1780579200.parquet",   1780579200),
    "btc-updown-5m-1780594800": (OUT/"polymarket_quotes_2026-06-03_btc-updown-5m-1780594800.parquet",   1780594800),
    "btc-updown-5m-1780610400": (OUT/"polymarket_quotes_2026-06-03_btc-updown-5m-1780610400.parquet",   1780610400),
}

print("=== SCHEMA DO PRIMEIRO ARQUIVO ===")
first_path = str(list(files.values())[0][0]).replace("\\", "/")
schema = duckdb.query(f"DESCRIBE SELECT * FROM read_parquet('{first_path}')").df()
print(schema[["column_name","column_type"]].to_string())

print("\n=== ANALISE EE - TODOS OS MERCADOS ===")
for slug, (fpath, resolve_ts) in files.items():
    if not fpath.exists():
        print(f"\n  {slug}: arquivo nao encontrado")
        continue

    fwd = str(fpath).replace("\\", "/")
    try:
        # Ler todos os dados
        df = duckdb.query(f"SELECT * FROM read_parquet('{fwd}')").df()
        n_total = len(df)

        # Converter tipos
        df["ts_sec"] = df["timestamp_us"] / 1e6
        df["secs_to_end"] = resolve_ts - df["ts_sec"]
        df["bid_price"] = pd.to_numeric(df["bid_price"], errors="coerce")
        df["ask_price"] = pd.to_numeric(df["ask_price"], errors="coerce")
        df["spread"] = df["ask_price"] - df["bid_price"]

        # Filtrar janela EE (25-300s antes da resolucao)
        window = df[(df["secs_to_end"] >= 0) & (df["secs_to_end"] <= 300)].copy()
        ee_window = window[(window["secs_to_end"] >= 25) & (window["secs_to_end"] <= 155)]

        print(f"\n--- {slug} | resolve_ts={resolve_ts} | total quotes={n_total} ---")
        print(f"  Quotes em 0-300s antes: {len(window)}")
        if len(window) > 0:
            print(window[["secs_to_end","bid_price","ask_price","spread","bid_size"]].to_string())

        if len(ee_window) >= 2:
            bid_start = ee_window["bid_price"].iloc[0]
            bid_end   = ee_window["bid_price"].iloc[-1]
            vel = (bid_end - bid_start) / (ee_window["secs_to_end"].iloc[0] - ee_window["secs_to_end"].iloc[-1] + 1e-9)
            el = "Up" if bid_end >= 0.50 else "Down"
            ee_trigger = (bid_end >= 0.50) and (abs(vel) >= 0.17)
            print(f"\n  EE window (25-155s): {len(ee_window)} quotes")
            print(f"    bid_start={bid_start:.3f} bid_end={bid_end:.3f} vel={vel:.3f}")
            print(f"    Early Leader: {el} | EE trigger (vel>=0.17): {ee_trigger}")
        else:
            print(f"  EE window: insuficiente ({len(ee_window)} quotes)")
    except Exception as e:
        print(f"  ERRO {slug}: {e}")
