import sys, io, json
from pathlib import Path
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

log = sorted(Path("logs").glob("market_monitor_*.jsonl"), reverse=True)[0]
rows = []
with open(log, encoding="utf-8") as f:
    for line in f:
        try:
            d = json.loads(line)
            if d.get("type") == "monitor_snap":
                rows.append(d)
        except:
            pass

by_slug = defaultdict(list)
for s in rows:
    by_slug[s["slug"]].append(s)

# Reconstrói cada slug com resolução final inferida
slugs_info = []
for slug, slist in by_slug.items():
    cur = [s for s in slist if s["slot"] == "current"]
    if not cur:
        continue
    last = cur[-1]
    if (last.get("secs") or 999) > 40:
        continue
    up_f, dn_f = last["up_bid"], last["down_bid"]
    if up_f >= 0.85:
        resolution = "UP"
    elif dn_f >= 0.85:
        resolution = "DOWN"
    else:
        continue

    def snap_at(lo, hi, c=cur):
        return [s for s in c if s.get("secs") and lo <= s["secs"] <= hi]

    slugs_info.append({
        "slug": slug, "resolution": resolution,
        "s240": snap_at(181, 240),
        "s180": snap_at(121, 180),
        "s120": snap_at(61, 120),
        "s60":  snap_at(31, 60),
        "s30":  snap_at(1, 30),
        "all":  cur,
    })

QTY = 6.0; TICK = 0.01
SEP = "─" * 62

def avg(lst, key, default=0):
    vals = [x.get(key) or default for x in lst]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


# ═══════════════════════════════════════════════════════════════
# MM REDESENHADO: early momentum (secs 240-181)
# Premissa: o lado que lidera aos 4min tende a resolver (80%+)
# Comprar o líder, stop -3T, hold to resolution
# ═══════════════════════════════════════════════════════════════

print()
print("╔══════════════════════════════════════════════════════════════╗")
print("║  MM REDESENHADO — early momentum leader (secs 240-181s)     ║")
print("╚══════════════════════════════════════════════════════════════╝")
print()
print("  Premissa: quem lidera em 240-181s resolve 80%+ das vezes.")
print("  Setup: comprar o lado lider quando up_bid > threshold,")
print("         stop -3T, target $1.00 na resolucao.")
print()

def run_mm(slugs, min_bid, stop_t=3, secs_window="s240"):
    trades = []
    for si in slugs:
        snaps = si[secs_window]
        if not snaps:
            continue
        entry = None
        for s in snaps:
            if s["up_bid"] > min_bid:
                entry = s; side = "UP"; ep = s["up_bid"]; break
            if s["down_bid"] > min_bid:
                entry = s; side = "DOWN"; ep = s["down_bid"]; break
        if not entry:
            continue
        sp = round(ep - stop_t * TICK, 4)
        post = [s for s in si["all"] if s["ts"] > entry["ts"]]
        if not post:
            continue
        gap = 0; outcome = None; exit_b = None
        for s in post:
            wb = s["up_bid"] if side == "UP" else s["down_bid"]
            if wb <= 0:
                gap += 1
                if gap >= 2:
                    outcome = "BOOK_GAP"; exit_b = 0.0; break
            else:
                gap = 0
            if wb <= sp:
                outcome = "STOP"; exit_b = wb; break
        if outcome is None:
            lw = si["all"][-1]["up_bid"] if side == "UP" else si["all"][-1]["down_bid"]
            outcome = "WIN" if lw >= 0.85 else "REVERSAL"
            exit_b = 1.0 if outcome == "WIN" else lw
        dir_correct = (side == "UP") == (si["resolution"] == "UP")
        trades.append({
            "outcome": outcome,
            "pnl": round((exit_b - ep) * QTY, 4),
            "ep": ep, "side": side,
            "dir_correct": dir_correct,
            "res": si["resolution"],
        })
    return trades

print(f"  {'threshold':>10}  {'n':>5}  {'WR%':>5}  {'dir%':>5}  {'PnL':>8}  {'avg/trade':>10}")
for mb in [0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62]:
    t = run_mm(slugs_info, mb)
    if not t:
        continue
    wins = [x for x in t if x["outcome"] == "WIN"]
    wr   = 100 * len(wins) / len(t)
    dc   = 100 * sum(1 for x in t if x["dir_correct"]) / len(t)
    pnl  = sum(x["pnl"] for x in t)
    print(f"  up_bid>{mb:.2f}  {len(t):>5}  {wr:>4.0f}%  {dc:>4.0f}%  ${pnl:>+7.2f}  ${pnl/len(t):>+9.4f}")

# Detalhe do melhor threshold (0.52)
print()
print("  Detalhe com threshold=0.52, stop 3T:")
t52 = run_mm(slugs_info, 0.52)
for label, subset in [("Direcao correta", [x for x in t52 if x["dir_correct"]]),
                       ("Direcao errada ", [x for x in t52 if not x["dir_correct"]])]:
    if not subset: continue
    wins = [x for x in subset if x["outcome"] == "WIN"]
    stops = [x for x in subset if x["outcome"] == "STOP"]
    pnl = sum(x["pnl"] for x in subset)
    wr  = 100 * len(wins) / len(subset)
    print(f"    {label}: {len(subset):3d} trades  WR={wr:.0f}%  PnL=${pnl:+.2f}  stops={len(stops)}")

# Comparar com stop menor (2T) e maior (4T)
print()
print("  Sensibilidade do stop (threshold=0.52):")
for st in [2, 3, 4, 5]:
    t = run_mm(slugs_info, 0.52, stop_t=st)
    wins = [x for x in t if x["outcome"] == "WIN"]
    pnl  = sum(x["pnl"] for x in t)
    wr   = 100 * len(wins) / len(t) if t else 0
    stops_n = sum(1 for x in t if x["outcome"] == "STOP")
    print(f"    stop {st}T: WR={wr:.0f}%  stops={stops_n}/{len(t)}  PnL=${pnl:+.2f}")


# ═══════════════════════════════════════════════════════════════
# SC REDESENHADO: momentum confirma direção do líder
# Quando secs 120-61s, líder >= threshold E spot_d15 concorda
# → trade na mesma direção, target +3T, stop -2T, max 60s
# ═══════════════════════════════════════════════════════════════

print()
print(SEP)
print("  SC REDESENHADO — spot confirma lider (secs 120-61s)")
print(SEP)
print()
print("  Premissa: spot momentum em 120-61s confirma ou nega o lider.")
print("  Cenarios:")
print("  A) Lider >= 0.55 + spot_d15 > 0 concordando: comprar lider")
print("  B) Lider >= 0.55 + spot_d15 discordando: sinal de reversao")
print()

sc_results = []
for si in slugs_info:
    snaps = si["s120"]  # secs 61-120
    if not snaps:
        continue
    # identificar lider e spot
    for s in snaps:
        d15 = s.get("sc_d15") or 0.0
        if d15 == 0:
            continue
        ub = s["up_bid"]; db = s["down_bid"]
        if ub > db and ub >= 0.55:
            leader = "UP"; leader_bid = ub
        elif db > ub and db >= 0.55:
            leader = "DOWN"; leader_bid = db
        else:
            continue

        spot_agrees = (d15 > 0) == (leader == "UP")
        sc_results.append({
            "si": si, "entry": s, "leader": leader, "leader_bid": leader_bid,
            "d15": d15, "spot_agrees": spot_agrees,
        })
        break  # 1 por slug

print("  Cenario A (spot concorda com lider):")
agree = [r for r in sc_results if r["spot_agrees"]]
disagree = [r for r in sc_results if not r["spot_agrees"]]
for label, subset in [("Spot CONCORDA", agree), ("Spot DISCORDA", disagree)]:
    if not subset:
        continue
    correct = sum(1 for r in subset if r["leader"] == r["si"]["resolution"])
    pct     = 100 * correct / len(subset)
    lbids   = [r["leader_bid"] for r in subset]
    d15s    = [r["d15"] for r in subset]
    print(f"    {label}: {len(subset):3d} slugs  acerto_direcao={pct:.0f}%  "
          f"leader_bid_avg={sum(lbids)/len(lbids):.3f}  d15_avg={sum(d15s)/len(d15s):+.1f}bps")

# Simular trade no Cenario A: comprar lider quando spot concorda
print()
print("  Simulacao Cenario A (spot concorda, compra lider, stop 2T, target 3T, max 60s):")
sc_trades = []
for r in agree:
    si = r["si"]
    s_entry = r["entry"]
    side = r["leader"]
    ep   = r["leader_bid"]
    sp   = round(ep - 2 * TICK, 4)
    tp   = round(ep + 3 * TICK, 4)
    post = [s for s in si["all"]
            if s["ts"] > s_entry["ts"] and s["ts"] <= s_entry["ts"] + 60]
    if not post:
        sc_trades.append({"outcome": "UNRESOLVED", "pnl": 0, "ep": ep})
        continue
    gap = 0; outcome = None; exit_b = None
    for s in post:
        wb = s["up_bid"] if side == "UP" else s["down_bid"]
        if wb <= 0: gap += 1
        else: gap = 0
        if gap >= 2: outcome = "BOOK_GAP"; exit_b = 0.0; break
        if wb <= sp: outcome = "STOP"; exit_b = wb; break
        if wb >= tp: outcome = "TARGET"; exit_b = tp; break
    if outcome is None:
        lw = post[-1]["up_bid"] if side == "UP" else post[-1]["down_bid"]
        last_secs = post[-1].get("secs") or 999
        if last_secs <= 35:
            outcome = "WIN" if lw >= 0.85 else "LOSS"
            exit_b  = 1.0 if outcome == "WIN" else lw
        else:
            outcome = "TIMEOUT"; exit_b = lw
    sc_trades.append({"outcome": outcome, "pnl": round((exit_b - ep) * QTY, 4), "ep": ep})

res_sc = [t for t in sc_trades if t["outcome"] != "UNRESOLVED"]
if res_sc:
    wins_sc = [t for t in res_sc if t["outcome"] in ("TARGET", "WIN")]
    pnl_sc  = sum(t["pnl"] for t in res_sc)
    wr_sc   = 100 * len(wins_sc) / len(res_sc)
    print(f"    {len(res_sc)} trades  WR={wr_sc:.0f}%  PnL=${pnl_sc:+.2f}  avg=${pnl_sc/len(res_sc):+.4f}")
    for out in ["TARGET", "WIN", "TIMEOUT", "STOP", "BOOK_GAP", "LOSS"]:
        ts = [t for t in res_sc if t["outcome"] == out]
        if ts:
            print(f"    {out:10}: {len(ts):3d}  PnL=${sum(t['pnl'] for t in ts):+.2f}")

# Cenario B: spot discorda → reversao iminente → comprar lado CONTRARIO ao lider
print()
print("  Simulacao Cenario B (spot discorda do lider → reversao, compra contra-lider):")
sc_trades_b = []
for r in disagree:
    si = r["si"]
    s_entry = r["entry"]
    side = "DOWN" if r["leader"] == "UP" else "UP"  # contra-lider
    ep   = s_entry["down_bid"] if side == "DOWN" else s_entry["up_bid"]
    if ep <= 0.05 or ep > 0.50: continue  # só se loser ainda tiver valor
    sp   = round(ep - 2 * TICK, 4)
    tp   = round(ep + 3 * TICK, 4)
    post = [s for s in si["all"]
            if s["ts"] > s_entry["ts"] and s["ts"] <= s_entry["ts"] + 60]
    if not post:
        continue
    gap = 0; outcome = None; exit_b = None
    for s in post:
        wb = s["up_bid"] if side == "UP" else s["down_bid"]
        if wb <= 0: gap += 1
        else: gap = 0
        if gap >= 2: outcome = "BOOK_GAP"; exit_b = 0.0; break
        if wb <= sp: outcome = "STOP"; exit_b = wb; break
        if wb >= tp: outcome = "TARGET"; exit_b = tp; break
    if outcome is None:
        lw = post[-1]["up_bid"] if side == "UP" else post[-1]["down_bid"]
        last_secs = post[-1].get("secs") or 999
        if last_secs <= 35:
            outcome = "WIN" if lw >= 0.85 else "LOSS"
            exit_b  = 1.0 if outcome == "WIN" else lw
        else:
            outcome = "TIMEOUT"; exit_b = lw
    sc_trades_b.append({"outcome": outcome, "pnl": round((exit_b - ep) * QTY, 4), "ep": ep})

res_b = [t for t in sc_trades_b if t["outcome"] not in ("UNRESOLVED",)]
if res_b:
    wins_b = [t for t in res_b if t["outcome"] in ("TARGET", "WIN")]
    pnl_b  = sum(t["pnl"] for t in res_b)
    wr_b   = 100 * len(wins_b) / len(res_b)
    print(f"    {len(res_b)} trades  WR={wr_b:.0f}%  PnL=${pnl_b:+.2f}  avg=${pnl_b/len(res_b):+.4f}")
    for out in ["TARGET", "WIN", "TIMEOUT", "STOP", "BOOK_GAP", "LOSS"]:
        ts = [t for t in res_b if t["outcome"] == out]
        if ts:
            print(f"    {out:10}: {len(ts):3d}  PnL=${sum(t['pnl'] for t in ts):+.2f}")
else:
    print("    sem trades suficientes")


# ═══════════════════════════════════════════════════════════════
# RESUMO FINAL
# ═══════════════════════════════════════════════════════════════
print()
print(SEP)
print("  RESUMO — melhores cenarios identificados")
print(SEP)
print()
print("  [MM early] up_bid > 0.52 em secs 240-181s → compra lider:")
t52 = run_mm(slugs_info, 0.52, stop_t=3)
w52 = [x for x in t52 if x["outcome"] == "WIN"]
print(f"    {len(t52)} trades  WR={100*len(w52)/len(t52):.0f}%  PnL=${sum(x['pnl'] for x in t52):+.2f}")
print()
print("  [SC confirm] spot concorda com lider em secs 120-61s:")
if res_sc:
    print(f"    {len(res_sc)} trades  WR={wr_sc:.0f}%  PnL=${pnl_sc:+.2f}")
print()
print("  Observacoes:")
print("  - MM redesenhado e uma apostao direcional desde o inicio, nao market making")
print("  - SC confirm usa spot como filtro adicional sobre o lider ja identificado")
print("  - Ambos dependem de mercado com liquidez decente em secs 120-240s")
print("  - Amostra: 162 slugs resolvidos em 13.8h — validar com mais sessoes")
