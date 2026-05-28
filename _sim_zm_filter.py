"""
Analisa se há filtros que distinguem wins de stops na zona morta EE
(el_bid_180 em [0.50, 0.84] com secs > 35) sem matar os wins.
"""
import json, pathlib
from collections import defaultdict

LOG_GLOB = "logs/current_almost_resolved_real_*/*.jsonl"

entries = {}
closeds = {}
snapshots = defaultdict(list)

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
            tr = r.get("trade") or {}
            slug = tr.get("event_slug") or r.get("current_slug", "")
            ee_tracker = r.get("ee_tracker") or {}
            if ee_tracker.get("signal_ok") or (tr.get("setup_variant") == "early_entry" and tr.get("mode") not in ("idle", None)):
                snapshots[slug].append({
                    "ts":         r.get("ts", 0),
                    "secs":       r.get("current_secs"),
                    "el_bid_180": ee_tracker.get("el_bid_180", 0),
                    "el_bid_240": ee_tracker.get("el_bid_240", 0),
                    "el_vel":     ee_tracker.get("el_vel", 0),
                    "opp_bid":    r.get("current_scalp_context", {}).get(
                                    "up_bid" if tr.get("side") == "DOWN" else "down_bid", 0) or 0,
                    "trade_mode": tr.get("mode", ""),
                })

zm_trades = []
for slug, entry in entries.items():
    cl = closeds.get(slug)
    if not cl: continue
    snaps = snapshots.get(slug, [])
    pnl = cl.get("pnl_usd")
    if pnl is None: continue
    pnl = float(pnl)
    lr = str(cl.get("last_reason", ""))
    is_stop = "stop" in lr.lower()
    is_win = pnl > 0

    zm_snaps = [s for s in snaps
                if s.get("secs") and s["secs"] > 35
                and 0.50 <= s.get("el_bid_180", 0) <= 0.84]

    zm_trades.append({
        "slug":       slug[-10:],
        "ep":         float(entry.get("ep", 0) or 0),
        "vel":        float((entry.get("el") or {}).get("el_vel", 0) or 0),
        "pnl":        pnl,
        "lr":         lr[:45],
        "is_stop":    is_stop,
        "is_win":     is_win,
        "n_zm":       len(zm_snaps),
        "zm_min_el":  min((s["el_bid_180"] for s in zm_snaps), default=None),
        "zm_max_el":  max((s["el_bid_180"] for s in zm_snaps), default=None),
        "zm_max_opp": max((s["opp_bid"] for s in zm_snaps), default=None),
        "zm_min_opp": min((s["opp_bid"] for s in zm_snaps), default=None),
    })

zm_only = [t for t in zm_trades if t["n_zm"] > 0]
wins_zm  = [t for t in zm_only if t["is_win"]]
stops_zm = [t for t in zm_only if t["is_stop"]]
other_zm = [t for t in zm_only if not t["is_win"] and not t["is_stop"]]

print(f"\nTotal com zona morta (n_zm>0): {len(zm_only)}")
print(f"  wins={len(wins_zm)}  stops={len(stops_zm)}  outros={len(other_zm)}")

print("\n--- WINS na zona morta ---")
print(f"  {'slug':>12}  ep   vel    pnl   n_zm  zm_min_el  zm_max_opp  lr")
for t in sorted(wins_zm, key=lambda x: x["pnl"], reverse=True):
    mel = f"{t['zm_min_el']:.2f}" if t["zm_min_el"] is not None else "  -"
    mopp = f"{t['zm_max_opp']:.2f}" if t["zm_max_opp"] is not None else "  -"
    print(f"  {t['slug']:>12}  {t['ep']:.2f} {t['vel']:.3f} {t['pnl']:+6.2f}  {t['n_zm']:3d}   {mel}       {mopp}   {t['lr'][:35]}")

print("\n--- STOPS na zona morta ---")
print(f"  {'slug':>12}  ep   vel    pnl   n_zm  zm_min_el  zm_max_opp  lr")
for t in sorted(stops_zm, key=lambda x: x["pnl"]):
    mel = f"{t['zm_min_el']:.2f}" if t["zm_min_el"] is not None else "  -"
    mopp = f"{t['zm_max_opp']:.2f}" if t["zm_max_opp"] is not None else "  -"
    print(f"  {t['slug']:>12}  {t['ep']:.2f} {t['vel']:.3f} {t['pnl']:+6.2f}  {t['n_zm']:3d}   {mel}       {mopp}   {t['lr'][:35]}")

# Análise de limiar
print("\n=== ANÁLISE DE LIMIARES (zona morta) ===")
thresholds = [0.55, 0.60, 0.65, 0.70]
for thr in thresholds:
    # filtro: sair se zm_min_el < thr (EL caiu muito baixo)
    filtered_out_wins  = [t for t in wins_zm  if t["zm_min_el"] is not None and t["zm_min_el"] < thr]
    filtered_out_stops = [t for t in stops_zm if t["zm_min_el"] is not None and t["zm_min_el"] < thr]
    surviving_wins     = [t for t in wins_zm  if t["zm_min_el"] is None or t["zm_min_el"] >= thr]
    surviving_stops    = [t for t in stops_zm if t["zm_min_el"] is None or t["zm_min_el"] >= thr]
    print(f"\n  Filtro zm_min_el < {thr:.2f} -> sai:")
    print(f"    Para stops: {len(filtered_out_stops)}/{len(stops_zm)}")
    print(f"    Para wins:  {len(filtered_out_wins)}/{len(wins_zm)}  (kills: {[round(t['pnl'],2) for t in filtered_out_wins]})")
    print(f"    WR restante: {len(surviving_wins)}/{len(surviving_wins)+len(surviving_stops)} = "
          f"{100*len(surviving_wins)/max(1,len(surviving_wins)+len(surviving_stops)):.0f}%")

print("\n--- opp_bid threshold ---")
opp_thrs = [0.55, 0.60, 0.65, 0.70]
for thr in opp_thrs:
    fo_wins  = [t for t in wins_zm  if t["zm_max_opp"] is not None and t["zm_max_opp"] >= thr]
    fo_stops = [t for t in stops_zm if t["zm_max_opp"] is not None and t["zm_max_opp"] >= thr]
    sur_wins = [t for t in wins_zm  if t["zm_max_opp"] is None or t["zm_max_opp"] < thr]
    sur_stops = [t for t in stops_zm if t["zm_max_opp"] is None or t["zm_max_opp"] < thr]
    print(f"\n  Filtro zm_max_opp >= {thr:.2f} -> sai:")
    print(f"    Para stops: {len(fo_stops)}/{len(stops_zm)}")
    print(f"    Para wins:  {len(fo_wins)}/{len(wins_zm)}")
    print(f"    WR restante: {len(sur_wins)}/{max(1,len(sur_wins)+len(sur_stops))} = "
          f"{100*len(sur_wins)/max(1,len(sur_wins)+len(sur_stops)):.0f}%")
