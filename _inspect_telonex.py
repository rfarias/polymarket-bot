import duckdb, pathlib, os

base = pathlib.Path(__file__).parent
prefix = "100698025363657020790903234023020875322370654614111181942779685024172456779656_2026-01-20"

for suffix in ["trades", "quotes", "book_snapshot_full"]:
    path = str(base / f"{prefix}_{suffix}.parquet")
    print(f"=== {suffix.upper()} ===")
    try:
        escaped = path.replace("\\", "/")
        df = duckdb.query(f"SELECT * FROM read_parquet('{escaped}') LIMIT 5").df()
        n = duckdb.query(f"SELECT COUNT(*) FROM read_parquet('{escaped}')").df().iloc[0, 0]
        print("Colunas:", list(df.columns))
        print("Linhas:", n)
        print(df.to_string())
    except Exception as e:
        print("ERRO:", e)
    print()
