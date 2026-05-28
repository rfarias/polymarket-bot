"""
Analisa posições EE que entraram na zona morta (el_bid em [0.50, 0.84] com secs > 35).
Busca nos snapshots o comportamento de el_bid e opp_bid durante a vida do trade.
"""
import json, pathlib, sys
from collections import defaultdict

LOG_GLOB = "logs/current_almost_resolved_real_*/*.jsonl"

# Carregar todos os eventos relevantes
entries = {}      # slug -> ee_real_entry
closeds = {}      # slug -> trade_closed
snapshots = defaultdict(list)  # slug -> [snap, ...]

for f in sorted(pathlib.Path(".").glob(LOG_GLOB)):
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip(): continue
        try: r = json.loads(line)
        except: continue
        t = r.get("type", "")
        if t == "ee_real_entry" and "slug" in r:
            if r["slug"] not in entries:
                entries[r["slug"]] = r
        elif t == "trade_closed" and "entry_slug" in r:
            s = r["entry_slug"]
            if s not in closeds:
                closeds[s] = r
        elif t == "snapshot":
            # guardar snapshots onde há um EE trade ativo (modo awaiting ou open)
            tr = r.get("trade") or {}
            slug = tr.get("event_slug") or r.get("current_slug", "")
            ee_tracker = r.get("ee_tracker") or {}
            if ee_tracker.get("signal_ok") or (tr.get("setup_variant") == "early_entry" and tr.get("mode") not in ("idle", None)):
                snapshots[slug].append({
                    "ts":       r.get("ts", 0),
                    "secs":     r.get("current_secs"),
                    "el_bid":   ee_tracker.get("el_vel", 0),  # aproximação
                    "el_bid_180": ee_tracker.get("el_bid_180", 0),
                    "el_bid_240": ee_tracker.get("el_bid_240", 0),
                    "el_vel":   ee_tracker.get("el_vel", 0),
                    "opp_bid":  r.get("current_scalp_context", {}).get("up_bid" if tr.get("side") == "DOWN" else "down_bid", 0),
                    "trade_mode": tr.get("mode",""),
                })

# Coletar EE trades com dados de snapshot
print(f"\nEE entries: {len(entries)}  closeds: {len(closeds)}")
print(f"Slugs com snapshots EE: {len(snapshots)}\n")

# Analisar zona morta por outcome
# Zona morta: trade aberto (early_entry), secs > 35, el_bid entre 0.50 e 0.84
zm_trades = []
for slug, entry in entries.items():
    cl = closeds.get(slug)
    if not cl: continue
    snaps = snapshots.get(slug, [])
    pnl = cl.get("pnl_usd")
    lr = str(cl.get("last_reason", ""))
    is_stop = "stop" in lr.lower()

    # Verificar se houve snapshot em zona morta
    zm_snaps = [s for s in snaps if s.get("secs", 0) and s["secs"] > 35
                and 0.50 <= s.get("el_bid_180", 0) <= 0.84]

    if zm_snaps or True:  # reportar todos para contexto
        zm_trades.append({
            "slug": slug,
            "ep": entry.get("ep", 0),
            "el_vel": entry.get("el", {}).get("el_vel", 0),
            "pnl": pnl,
            "lr": lr[:40],
            "is_stop": is_stop,
            "n_snaps": len(snaps),
            "n_zm": len(zm_snaps),
            "zm_min_el": min((s.get("el_bid_180", 1) for s in zm_snaps), default=None),
            "zm_max_opp": max((s.get("opp_bid", 0) for s in zm_snaps), default=None),
        })

if not zm_trades:
    print("Nenhum trade com snapshots encontrado.")
    print("(Snapshots do AR runner não incluem contexto EE detalhado em secs>35)")
else:
    stops = [t for t in zm_trades if t["is_stop"]]
    wins  = [t for t in zm_trades if not t["is_stop"] and (t["pnl"] or 0) > 0]
    print(f"Trades com snapshots: {len(zm_trades)} (stops={len(stops)}, wins={len(wins)})")
    for t in zm_trades[:20]:
        print(f"  {t['slug'][-10:]} ep={t['ep']} vel={t['el_vel']:.3f} "
              f"pnl={str(t['pnl']):>7} n_zm={t['n_zm']} lr={t['lr']}")
