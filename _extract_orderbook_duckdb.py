#!/usr/bin/env python3
"""
Extrai orderbook BTC 5min via DuckDB (out-of-core, sem OOM).
- Filter pushdown: só mercados com resolucao direta (616)
- Resample para 1 segundo: last(best_bid) por (market_id, outcome, segundo)
- Saída: ~185k linhas gerenciáveis
"""
import sys, warnings, time
warnings.filterwarnings('ignore')
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

import pandas as pd
import duckdb

# Carrega mercados com resolucao direta
mkt = pd.read_parquet('_btc5_markets.parquet')
resolved = mkt[mkt['resolution'].isin([0, 1])].copy()
resolved['market_id'] = resolved['market_id'].astype(str)
print(f'Mercados com resolucao direta: {len(resolved)}')

res_map = dict(zip(resolved['market_id'], resolved['resolution'].astype(int)))
ids_list = "','".join(sorted(res_map.keys()))

# Salva res_map
pd.DataFrame(list(res_map.items()), columns=['market_id','resolution'])\
  .to_parquet('_btc5_res_map.parquet', index=False)

print('Extraindo e reamostando via DuckDB (1s/tick)...')
t0 = time.time()

con = duckdb.connect()
# Configura memoria limitada para nao estourar RAM
con.execute("SET memory_limit='1.5GB'")
con.execute("SET threads=2")

# Resample para 1 segundo: last(best_bid) por (market_id, outcome, segundo)
# epoch_ms(ts_ms) converte ms -> datetime; floor divide para obter segundos
query = f"""
    SELECT
        market_id,
        outcome,
        (ts_ms // 1000) AS ts_sec,
        LAST(best_bid ORDER BY ts_ms) AS best_bid
    FROM read_parquet('_btc5_orderbook.parquet')
    WHERE market_id IN ('{ids_list}')
    GROUP BY market_id, outcome, ts_sec
    ORDER BY market_id, ts_sec
"""

ob = con.execute(query).df()
elapsed = time.time() - t0
print(f'Concluido em {elapsed:.0f}s')
print(f'Linhas apos resample 1s: {len(ob):,}  |  mercados: {ob["market_id"].nunique():,}')
print(f'outcome values: {ob["outcome"].unique()}')
print(f'best_bid range: {ob["best_bid"].min():.3f} a {ob["best_bid"].max():.3f}')
print(f'ts_sec sample: {ob["ts_sec"].head(3).tolist()}')

snaps_per_mkt = ob.groupby('market_id').size() / 2  # /2: UP+DOWN
print(f'Snaps/mercado: mean={snaps_per_mkt.mean():.0f}  median={snaps_per_mkt.median():.0f}')

ob.to_parquet('_btc5_ob_filtered.parquet', index=False)
print(f'Salvo: _btc5_ob_filtered.parquet')
