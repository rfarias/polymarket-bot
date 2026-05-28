"""
Analisa trades AR em ep >= 0.95 para identificar filtros que evitam o -$5.94
sem matar os wins pequenos (0.06 a 0.24).
"""
import json, pathlib
from collections import defaultdict

LOG_GLOB = "logs/current_almost_resolved_real_*/*.jsonl"

rows = []
for f in sorted(pathlib.Path(".").glob(LOG_GLOB)):
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip(): continue
        try: r = json.loads(line)
        except: continue
        if r.get("type") == "trade_closed":
            rows.append(r)

seen = {}
for r in rows:
    k = r.get("entry_slug") or r.get("event_slug") or id(r)
    if k not in seen: seen[k] = r

trades = sorted(seen.values(), key=lambda r: r.get("ts", 0))
ar_trades = [t for t in trades if float(t.get("entry_price") or 0) >= 0.95]

print(f"\nAR trades (ep>=0.95): {len(ar_trades)}\n")

# Separar por faixa de ep
buckets = {"0.95-0.96": [], "0.97-0.98": [], "0.99+": []}
for t in ar_trades:
    ep = float(t.get("entry_price") or 0)
    pnl = t.get("pnl_usd")
    lr = str(t.get("last_reason", ""))
    src = str(t.get("source", ""))
    if ep >= 0.99: buckets["0.99+"].append(t)
    elif ep >= 0.97: buckets["0.97-0.98"].append(t)
    else: buckets["0.95-0.96"].append(t)

for name, ts in buckets.items():
    pnls = [float(t["pnl_usd"]) for t in ts if t.get("pnl_usd") is not None]
    losses = [p for p in pnls if p < -0.12]
    print(f"ep {name}: n={len(pnls)}  total={sum(pnls):+.2f}  avg={sum(pnls)/max(1,len(pnls)):+.3f}  losses={len(losses)}")
    for t in ts:
        pnl = t.get("pnl_usd")
        lr  = str(t.get("last_reason",""))[:45]
        src = str(t.get("source",""))[:20]
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(t.get("ts",0), tz=timezone.utc).strftime("%m-%d %H:%M")
        mark = " <<< LOSS" if pnl is not None and float(pnl) < -0.5 else ""
        print(f"  {dt} ep={t.get('entry_price')} pnl={str(pnl):>7} {src:<22} {lr}{mark}")
    print()

# Análise do evento catastrófico
print("="*60)
print("EVENTO CATASTROFICO (-5.94):")
for t in ar_trades:
    pnl = t.get("pnl_usd")
    if pnl is not None and float(pnl) < -2:
        print(json.dumps(t, indent=2, default=str))
