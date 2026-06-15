"""
Download Telonex - versao corrigida.
Logica: slug btc-updown-5m-{ts} onde ts = Unix timestamp da resolucao.
Para EE janela, preciso do arquivo do DIA que contem o ts de resolucao.
Filtra mercados resolvidos com ts em horario "util" (09:00-22:00 UTC).
"""
import pathlib, duckdb, json, datetime
import httpx

KEY = "tlx_f9d340fc080239d2be49a84ebe878ea8"
BASE = "https://api.telonex.io/v1"
OUT = pathlib.Path(__file__).parent / "telonex_data"
OUT.mkdir(exist_ok=True)

mkt_fwd = str(OUT / "polymarket_markets.parquet").replace("\\", "/")

# ── Extrair timestamp do slug e filtrar por dia/horario ────────────────────────
# slug = btc-updown-5m-{resolve_ts_unix}
# resolve_ts_unix / 86400 = dia UTC da resolucao
# Queremos mercados que resolvem em 2026-06-04 entre 09:00 e 22:00 UTC

D = datetime.date(2026, 6, 4)
ts_day_start = int(datetime.datetime(2026, 6, 4, 9,  0, tzinfo=datetime.timezone.utc).timestamp())
ts_day_end   = int(datetime.datetime(2026, 6, 4, 22, 0, tzinfo=datetime.timezone.utc).timestamp())

print(f"Filtro: resolucao entre {D} 09:00 e {D} 22:00 UTC")
print(f"  ts range: {ts_day_start} - {ts_day_end}")

targets = duckdb.query(f"""
    WITH btc AS (
        SELECT slug, asset_id_0 as asset_id, outcome_0 as outcome,
               status,
               CAST(REPLACE(slug, 'btc-updown-5m-', '') AS BIGINT) as resolve_ts,
               quotes_from, quotes_to,
               trades_from, trades_to
        FROM read_parquet('{mkt_fwd}')
        WHERE slug LIKE 'btc-updown-5m-%'
          AND status = 'resolved'
    )
    SELECT *,
           to_timestamp(resolve_ts) as resolve_utc,
           CAST(to_timestamp(resolve_ts) AS DATE) as resolve_date
    FROM btc
    WHERE resolve_ts >= {ts_day_start}
      AND resolve_ts <= {ts_day_end}
      AND quotes_from IS NOT NULL
    ORDER BY resolve_ts ASC
""").df()

print(f"\n{len(targets)} mercados encontrados que resolvem em 2026-06-04 09-22h UTC")
print(targets[["slug","resolve_utc","quotes_from","quotes_to"]].to_string())

# Selecionar 4 espacados ao longo do dia
n = len(targets)
if n >= 4:
    idxs = [0, n//3, 2*n//3, n-1]
elif n > 0:
    idxs = list(range(n))
else:
    print("NENHUM mercado encontrado — verificar filtro")
    exit(1)

selected = targets.iloc[idxs].reset_index(drop=True)
print(f"\nSelecionados {len(selected)}:")
print(selected[["slug","resolve_utc","quotes_from"]].to_string())

# ── Downloads ───────────────────────────────────────────────────────────────────
# A data do arquivo que contem o dado da resolucao = quotes_from
downloaded = []
with httpx.Client(follow_redirects=True, timeout=120) as client:
    for _, row in selected.iterrows():
        slug = row["slug"]
        asset_id = str(row["asset_id"])
        # Usar o dia da resolucao como data do arquivo
        resolve_date = str(row["quotes_from"])  # dia onde ficam os quotes finais

        dest = OUT / f"polymarket_quotes_{resolve_date}_{slug}.parquet"
        if dest.exists():
            sz = dest.stat().st_size
            print(f"\n  Ja existe: {dest.name} ({sz/1024:.1f} KB)")
            downloaded.append((slug, float(row["resolve_ts"]), str(dest)))
            continue

        url = f"{BASE}/downloads/polymarket/quotes/{resolve_date}"
        params = {"asset_id": asset_id}
        headers = {"Authorization": f"Bearer {KEY}"}

        resolve_utc = row["resolve_utc"]
        print(f"\n  Baixando: {slug} (resolve {resolve_utc} UTC) | arquivo: {resolve_date}")
        try:
            resp = client.get(url, headers=headers, params=params)
            print(f"  Status: {resp.status_code}  Size: {len(resp.content)/1024:.1f} KB")
            if resp.status_code == 200:
                with open(dest, "wb") as f:
                    f.write(resp.content)
                downloaded.append((slug, float(row["resolve_ts"]), str(dest)))
                print(f"  OK -> {dest.name}")
            elif resp.status_code == 402:
                print(f"  CREDITOS ESGOTADOS: {resp.text[:200]}")
                break
            else:
                print(f"  Erro {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"  Excecao: {e}")

# ── Analise EE: ultimos 300s antes da resolucao ────────────────────────────────
print("\n\n=== ANALISE EE - ULTIMOS 300s ANTES DA RESOLUCAO ===")
for slug, resolve_ts, fpath in downloaded:
    fwd = fpath.replace("\\", "/")
    try:
        n_total = duckdb.query(f"SELECT COUNT(*) FROM read_parquet('{fwd}')").df().iloc[0,0]
        df = duckdb.query(f"""
            SELECT
                outcome,
                ROUND(({resolve_ts:.0f} - timestamp_us/1e6), 1) as secs_to_end,
                bid_price,
                ask_price,
                ROUND(ask_price - bid_price, 3) as spread,
                bid_size,
                ask_size
            FROM read_parquet('{fwd}')
            WHERE ({resolve_ts:.0f} - timestamp_us/1e6) BETWEEN -10 AND 300
            ORDER BY timestamp_us
        """).df()
        print(f"\n--- {slug} | resolve_ts={resolve_ts:.0f} | total quotes={n_total} ---")
        if len(df) > 0:
            print(f"  Quotes nos ultimos 300s: {len(df)}")
            print(df.to_string())
            # Verificar se EE seria acionado (bid > 0.50, vel calculavel)
            ee_window = df[(df["secs_to_end"] >= 25) & (df["secs_to_end"] <= 155)]
            if len(ee_window) >= 2:
                bid_max = ee_window["bid_price"].max()
                bid_min = ee_window["bid_price"].min()
                print(f"  Janela EE (25-155s): {len(ee_window)} quotes | bid {bid_min:.3f}-{bid_max:.3f}")
            else:
                print(f"  Janela EE: dados insuficientes ({len(ee_window)} quotes)")
        else:
            print(f"  Nenhum dado nos ultimos 300s (total: {n_total})")
    except Exception as e:
        print(f"  ERRO {slug}: {e}")
