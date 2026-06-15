import duckdb, pathlib, datetime, os

base = pathlib.Path(__file__).parent
prefix = "100698025363657020790903234023020875322370654614111181942779685024172456779656_2026-01-20"

for suffix in ["quotes", "trades", "book_snapshot_full"]:
    path = str(base / f"{prefix}_{suffix}.parquet").replace("\\", "/")
    print(f"=== {suffix.upper()} ===")
    r = duckdb.query(f"""
        SELECT
            COUNT(*) as n,
            MIN(timestamp_us)/1e6 as ts_min,
            MAX(timestamp_us)/1e6 as ts_max,
            (MAX(timestamp_us)-MIN(timestamp_us))/1e6/3600 as span_horas
        FROM read_parquet('{path}')
    """).df()
    print(r.to_string(index=False))
    ts_min = r.iloc[0]["ts_min"]
    ts_max = r.iloc[0]["ts_max"]
    print(f"  De:  {datetime.datetime.utcfromtimestamp(ts_min)} UTC")
    print(f"  Ate: {datetime.datetime.utcfromtimestamp(ts_max)} UTC")
    # interval distribution for quotes
    if suffix == "quotes":
        intervals = duckdb.query(f"""
            WITH t AS (
                SELECT timestamp_us,
                       LAG(timestamp_us) OVER (ORDER BY timestamp_us) as prev_ts
                FROM read_parquet('{path}')
            )
            SELECT
                MIN((timestamp_us-prev_ts)/1e3) as min_ms,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (timestamp_us-prev_ts)/1e3) as median_ms,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY (timestamp_us-prev_ts)/1e3) as p95_ms,
                MAX((timestamp_us-prev_ts)/1e3) as max_ms
            FROM t WHERE prev_ts IS NOT NULL
        """).df()
        print("  Intervalo entre quotes (ms):", intervals.to_string(index=False))
    print()
