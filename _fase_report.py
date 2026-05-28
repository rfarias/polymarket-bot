import json, pathlib
from datetime import datetime, timezone, timedelta

rows = []
for f in sorted(pathlib.Path(".").glob("logs/current_almost_resolved_real_*/*.jsonl")):
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if r.get("type") == "trade_closed":
                rows.append(r)
        except:
            pass

seen = {}
for r in rows:
    slug = r.get("entry_slug") or r.get("event_slug") or ""
    k = slug if slug else id(r)
    if k not in seen:
        seen[k] = r

trades = sorted(seen.values(), key=lambda r: r.get("ts", 0))

def ts_to_dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)

# Determinar limites corretos a partir dos dados reais
if trades:
    first_ts = trades[0]["ts"]
    first_dt = ts_to_dt(first_ts)
    print(f"Primeiro trade: {first_dt.strftime('%Y-%m-%d %H:%M UTC')}")

# Calcular limites UTC por data
def day_utc(y, m, d, h=0):
    return datetime(y, m, d, h, 0, 0, tzinfo=timezone.utc).timestamp()

phases = [
    ("F1  AR puro        24/05",         day_utc(2026,5,24), day_utc(2026,5,25)),
    ("F2  EE estreia     25/05",         day_utc(2026,5,25), day_utc(2026,5,26)),
    ("F3  FAK catastrofe 26/05",         day_utc(2026,5,26), day_utc(2026,5,27)),
    ("F4a pre-fix        27/05 <12h UTC",day_utc(2026,5,27), day_utc(2026,5,27,12)),
    ("F4b pos-fix        27/05 >=12h UTC",day_utc(2026,5,27,12), day_utc(2026,5,28)),
    ("F5  hoje           28/05",         day_utc(2026,5,28), 9999999999.0),
]

grand_ee = 0.0
grand_ar = 0.0

for fname, ts0, ts1 in phases:
    ft = [t for t in trades if ts0 <= t.get("ts", 0) < ts1]
    ee = [t for t in ft if 0.80 <= float(t.get("entry_price") or 0) <= 0.88]
    ar = [t for t in ft if float(t.get("entry_price") or 0) > 0.88]

    ee_pnl = [float(t["pnl_usd"]) for t in ee if t.get("pnl_usd") is not None]
    ar_pnl = [float(t["pnl_usd"]) for t in ar if t.get("pnl_usd") is not None]
    ee_wins = [p for p in ee_pnl if p > 0]
    ee_stops_list = [t for t in ee if "stop" in str(t.get("last_reason", "")).lower()]
    ee_stops_pnl  = [float(t["pnl_usd"]) for t in ee_stops_list if t.get("pnl_usd") is not None]

    grand_ee += sum(ee_pnl)
    grand_ar += sum(ar_pnl)

    total = sum(ee_pnl + ar_pnl)
    sign = "+" if total >= 0 else ""
    print(f"\n{'='*55}")
    print(f"  {fname}")
    print(f"  AR : n={len(ar_pnl):2d}  pnl={sum(ar_pnl):+.2f}")
    print(f"  EE : n={len(ee_pnl):2d}  pnl={sum(ee_pnl):+.2f}  "
          f"wins={len(ee_wins)}  stops={len(ee_stops_list)}  "
          f"(stops_pnl={sum(ee_stops_pnl):+.2f})")
    print(f"  TOT: n={len(ee_pnl)+len(ar_pnl):2d}  pnl={total:+.2f}")
    for s in ee_stops_list:
        reason = str(s.get("last_reason", ""))[:40]
        print(f"    stop: ep={s.get('entry_price')}  pnl={s.get('pnl_usd'):>6}  {reason}")

print(f"\n{'='*55}")
print(f"  TOTAL AR: {grand_ar:+.2f}")
print(f"  TOTAL EE: {grand_ee:+.2f}")
print(f"  TOTAL   : {grand_ar+grand_ee:+.2f}")

# AR sem o outlier de -5.94
ar_all = [float(t["pnl_usd"]) for t in trades
          if float(t.get("entry_price") or 0) > 0.88 and t.get("pnl_usd") is not None]
ar_outlier = min(ar_all) if ar_all else 0
print(f"\n  AR sem outlier ({ar_outlier:+.2f}): {grand_ar - ar_outlier:+.2f}")
print(f"  EE sem FAK stops: {grand_ee - sum(float(t['pnl_usd']) for t in trades if 'fak' in str(t.get('last_reason','')).lower() and t.get('pnl_usd') is not None and 0.80 <= float(t.get('entry_price') or 0) <= 0.88):+.2f}")
