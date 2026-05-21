import json, sys, io
from pathlib import Path
from collections import defaultdict
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

QTY = 6.0; TICK = 0.01

def load_slugs():
    slugs = []
    for fname in sorted(Path("logs").glob("*.jsonl")):
        is_monitor = "market_monitor" in fname.name
        by_slug = defaultdict(list)
        with open(fname, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if is_monitor:
                        if d.get("type") != "monitor_snap" or d.get("slot") != "current":
                            continue
                        secs = d.get("secs"); ub = d.get("up_bid"); db = d.get("down_bid")
                        slug = d.get("slug")
                    else:
                        if d.get("type") != "snapshot": continue
                        sc = d.get("scalp_context") or d.get("current_scalp_context") or {}
                        secs = sc.get("secs_to_end") or d.get("current_secs")
                        ub = sc.get("up_bid"); db = sc.get("down_bid")
                        if ub is None:
                            lb = d.get("leader_buy"); cb = d.get("counter_buy")
                            side = (d.get("guardian") or {}).get("side") or \
                                   (d.get("almost_resolved_signal") or {}).get("side")
                            if lb is not None and cb is not None and side:
                                ub = lb if side == "UP" else cb
                                db = cb if side == "UP" else lb
                        slug = d.get("slug") or d.get("current_slug")
                    if slug and secs is not None and ub is not None:
                        by_slug[slug].append({"secs": int(secs), "up_bid": ub, "down_bid": db, "ts": d["ts"]})
                except Exception:
                    pass

        for slug, snaps in by_slug.items():
            snaps.sort(key=lambda x: x["ts"])
            last = snaps[-1]
            if (last.get("secs") or 999) > 40: continue
            winner = ("UP"   if last["up_bid"]   >= 0.85 else
                      "DOWN" if last["down_bid"] >= 0.85 else None)
            if not winner: continue

            s240 = [s for s in snaps if 181 <= s["secs"] <= 240]
            s180 = [s for s in snaps if 121 <= s["secs"] <= 180]
            el = None; cont_ok = False
            if s240:
                avg_up = sum(s["up_bid"]   for s in s240) / len(s240)
                avg_dn = sum(s["down_bid"] for s in s240) / len(s240)
                if avg_up >= 0.55:   el = "UP"
                elif avg_dn >= 0.55: el = "DOWN"
            if el and s180:
                bids180 = [s["up_bid"] if el == "UP" else s["down_bid"] for s in s180]
                cont_ok = min(bids180) >= 0.70
            slugs.append({"snaps": snaps, "winner": winner, "el": el, "cont_ok": cont_ok})
    return slugs


def simulate(slug_data, ep_lo, ep_hi, stop_ticks, entry_secs_max=180, use_el=False):
    """
    Usa a direção do EL para entrada (não o winner real) — sem lookahead bias.
    Quando use_el=True: exige EL estavel, entra na direção do EL.
    Quando use_el=False: entra no lado com maior bid no momento (proxy de mercado).
    O winner real só é usado para calcular o outcome final.
    """
    snaps = slug_data["snaps"]
    winner = slug_data["winner"]
    el = slug_data["el"]

    if use_el:
        if not (el and slug_data["cont_ok"]):
            return None
        entry_side = el  # usa direção do EL, não do winner real
    else:
        entry_side = None  # determinado no momento da entrada pelo maior bid

    entry = None
    for i, s in enumerate(snaps):
        if s["secs"] > entry_secs_max: continue
        if entry_side:
            wb = s["up_bid"] if entry_side == "UP" else s["down_bid"]
        else:
            # sem EL: entra no lado que lidera no momento
            wb = max(s["up_bid"], s["down_bid"])
            entry_side = "UP" if s["up_bid"] >= s["down_bid"] else "DOWN"
        if ep_lo <= wb <= ep_hi:
            entry = s; ep = wb; entry_idx = i; break
        entry_side = el if use_el else None  # reset para próximo snap se não entrou

    if not entry: return None
    sp = round(ep - stop_ticks * TICK, 4) if stop_ticks < 900 else -1.0
    post = snaps[entry_idx+1:]
    if not post: return None

    outcome = None; exit_b = ep; gap = 0
    for s in post:
        wb = s["up_bid"] if entry_side == "UP" else s["down_bid"]
        if wb <= 0.0:
            gap += 1
            if gap >= 2: outcome = "BOOK_GAP"; exit_b = 0.0; break
        else:
            gap = 0
        if sp > 0 and wb <= sp: outcome = "STOP"; exit_b = wb; break

    if outcome is None:
        lw = post[-1]["up_bid"] if entry_side == "UP" else post[-1]["down_bid"]
        if post[-1]["secs"] <= 35:
            # WIN = acertou a direção E preço chegou >= 0.85
            if lw >= 0.85:
                outcome = "WIN"; exit_b = 1.0
            else:
                # entrou mas mercado não chegou a 0.85: pode ser reversão parcial
                outcome = "REVERSAL"; exit_b = lw
        else:
            return None

    dir_correct = (entry_side == winner)
    pnl = round((exit_b - ep) * QTY, 4)
    return {"outcome": outcome, "pnl": pnl, "ep": ep, "exit_b": exit_b,
            "dir_correct": dir_correct, "entry_side": entry_side, "winner": winner}


slugs = load_slugs()
el_n = sum(1 for s in slugs if s["el"] and s["cont_ok"])
print(f"Slugs carregados: {len(slugs)}  |  EL estavel (F3): {el_n}")
print()

ep_lo, ep_hi = 0.82, 0.86

print("=" * 72)
print("  SIMULACAO: entrada 0.82-0.86, stops variados, com/sem EL estavel")
print("=" * 72)
print()
print(f"  {'Filtro':18} {'stop':>7} {'n':>5} {'WR%':>5} {'stop%':>6} {'REV%':>5} "
      f"{'PnL':>9} {'avg':>8} {'min_exit':>8}")
print(f"  {'':18} {'':>7} {'':>5} {'':>5} {'':>6} {'':>5} "
      f"{'':>9} {'':>8} {'':>8}")

for label, use_el in [("Baseline", False), ("EL estavel (F3)", True)]:
    print(f"  {label}")
    for stop_t in [2, 5, 8, 10, 12, 15, 999]:
        trades = []
        for sd in slugs:
            t = simulate(sd, ep_lo, ep_hi, stop_t, use_el=use_el)
            if t: trades.append(t)
        if not trades: continue
        wins  = [t for t in trades if t["outcome"] == "WIN"]
        stops = [t for t in trades if t["outcome"] == "STOP"]
        revs  = [t for t in trades if t["outcome"] == "REVERSAL"]
        pnl   = sum(t["pnl"] for t in trades)
        wr    = 100 * len(wins) / len(trades)
        sp_p  = 100 * len(stops) / len(trades)
        rv_p  = 100 * len(revs) / len(trades)
        exits = [t["exit_b"] for t in stops]
        min_e = min(exits) if exits else 0.0
        stop_label = f"{stop_t}T" if stop_t < 900 else "sem stop"
        print(f"  {'':18} {stop_label:>7} {len(trades):>5} {wr:>4.1f}% "
              f"{sp_p:>5.1f}% {rv_p:>4.1f}% "
              f"${pnl:>+8.2f} ${pnl/len(trades):>+7.4f} {min_e:>8.3f}")
    print()

print()
print("=" * 72)
print("  COMPARATIVO: AR normal (ep>=0.88) vs Early Entry (0.82-0.86)")
print("=" * 72)
print()
print("  AR normal baseline (ep>=0.88, stop 2T):")
ar_trades = []
for sd in slugs:
    t = simulate(sd, 0.88, 0.99, 2, entry_secs_max=120, use_el=False)
    if t: ar_trades.append(t)
ar_wins = [t for t in ar_trades if t["outcome"] == "WIN"]
ar_pnl  = sum(t["pnl"] for t in ar_trades)
print(f"    n={len(ar_trades)}  WR={100*len(ar_wins)/len(ar_trades):.1f}%  "
      f"PnL=${ar_pnl:+.2f}  avg=${ar_pnl/len(ar_trades):+.4f}")

print()
print("  Early Entry EL estavel (0.82-0.86, stop 10T):")
ee_trades = []
for sd in slugs:
    t = simulate(sd, 0.82, 0.86, 10, entry_secs_max=180, use_el=True)
    if t: ee_trades.append(t)
if ee_trades:
    ee_wins = [t for t in ee_trades if t["outcome"] == "WIN"]
    ee_pnl  = sum(t["pnl"] for t in ee_trades)
    ee_stops = [t for t in ee_trades if t["outcome"] == "STOP"]
    ee_revs  = [t for t in ee_trades if t["outcome"] == "REVERSAL"]
    print(f"    n={len(ee_trades)}  WR={100*len(ee_wins)/len(ee_trades):.1f}%  "
          f"PnL=${ee_pnl:+.2f}  avg=${ee_pnl/len(ee_trades):+.4f}")
    print(f"    WIN:{len(ee_wins)}  STOP:{len(ee_stops)}  REVERSAL:{len(ee_revs)}")
    if ee_stops:
        ex = [t["exit_b"] for t in ee_stops]
        print(f"    Stop exit: avg={sum(ex)/len(ex):.3f}  min={min(ex):.3f}")
    eps = [t["ep"] for t in ee_trades]
    print(f"    entry_price: avg={sum(eps)/len(eps):.3f}  "
          f"min={min(eps):.3f}  max={max(eps):.3f}")

print()
print("  Early Entry EL estavel (0.82-0.86, stop 12T):")
ee2_trades = []
for sd in slugs:
    t = simulate(sd, 0.82, 0.86, 12, entry_secs_max=180, use_el=True)
    if t: ee2_trades.append(t)
if ee2_trades:
    ee2_wins = [t for t in ee2_trades if t["outcome"] == "WIN"]
    ee2_pnl  = sum(t["pnl"] for t in ee2_trades)
    ee2_stops = [t for t in ee2_trades if t["outcome"] == "STOP"]
    ee2_revs  = [t for t in ee2_trades if t["outcome"] == "REVERSAL"]
    print(f"    n={len(ee2_trades)}  WR={100*len(ee2_wins)/len(ee2_trades):.1f}%  "
          f"PnL=${ee2_pnl:+.2f}  avg=${ee2_pnl/len(ee2_trades):+.4f}")
    print(f"    WIN:{len(ee2_wins)}  STOP:{len(ee2_stops)}  REVERSAL:{len(ee2_revs)}")

print()
print("  Early Entry EL estavel (0.82-0.86, SEM STOP):")
ee3_trades = []
for sd in slugs:
    t = simulate(sd, 0.82, 0.86, 999, entry_secs_max=180, use_el=True)
    if t: ee3_trades.append(t)
if ee3_trades:
    ee3_wins = [t for t in ee3_trades if t["outcome"] == "WIN"]
    ee3_pnl  = sum(t["pnl"] for t in ee3_trades)
    ee3_revs  = [t for t in ee3_trades if t["outcome"] == "REVERSAL"]
    print(f"    n={len(ee3_trades)}  WR={100*len(ee3_wins)/len(ee3_trades):.1f}%  "
          f"PnL=${ee3_pnl:+.2f}  avg=${ee3_pnl/len(ee3_trades):+.4f}")
    print(f"    WIN:{len(ee3_wins)}  REVERSAL:{len(ee3_revs)}")
    if ee3_revs:
        r_pnls = [t["pnl"] for t in ee3_revs]
        print(f"    Reversao: avg_pnl=${sum(r_pnls)/len(r_pnls):+.3f}  "
              f"max_loss=${min(r_pnls):+.3f}")
