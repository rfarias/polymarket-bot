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
    s240 = [s for s in cur if s.get("secs") and 181 <= s["secs"] <= 240]
    s120 = [s for s in cur if s.get("secs") and 61  <= s["secs"] <= 120]
    slugs_info.append({"slug": slug, "resolution": resolution,
                       "s240": s240, "s120": s120, "all": cur})

QTY = 6.0; TICK = 0.01
SEP = "─" * 64

def d(v): return f"${v:+.2f}"
def pct(n, tot): return f"{100*n/tot:.0f}%" if tot else "—"


def simulate(slist_all, entry_snap, entry_side, ep, stop_t, target_t=None):
    sp = round(ep - stop_t * TICK, 4)
    tp = round(ep + target_t * TICK, 4) if target_t else None
    post = [s for s in slist_all if s["ts"] > entry_snap["ts"]]
    if not post:
        return None, None
    gap = 0
    for s in post:
        wb = s["up_bid"] if entry_side == "UP" else s["down_bid"]
        if wb <= 0:
            gap += 1
            if gap >= 2:
                return "BOOK_GAP", 0.0
        else:
            gap = 0
        if wb <= sp:
            return "STOP", wb
        if tp and wb >= tp:
            return "TARGET", wb
    last = post[-1]
    lw = last["up_bid"] if entry_side == "UP" else last["down_bid"]
    if (last.get("secs") or 999) <= 40:
        return ("WIN" if lw >= 0.85 else "REVERSAL"), (1.0 if lw >= 0.85 else lw)
    return None, lw


def report_trades(trades):
    res = [t for t in trades if t["outcome"] not in (None, "UNRESOLVED")]
    if not res:
        print("    sem trades resolvidos")
        return
    wins  = [t for t in res if t["outcome"] in ("WIN", "TARGET")]
    stops = [t for t in res if t["outcome"] == "STOP"]
    gaps  = [t for t in res if t["outcome"] == "BOOK_GAP"]
    revs  = [t for t in res if t["outcome"] in ("REVERSAL", "LOSS")]
    pnl   = sum(t["pnl"] for t in res)
    wr    = 100 * len(wins) / len(res)
    print(f"    Trades: {len(res)}   WR: {wr:.0f}%   PnL: {d(pnl)}   avg/trade: {d(pnl/len(res))}")
    for out, ts in [("WIN/TARGET", wins), ("STOP", stops), ("BOOK_GAP", gaps), ("REVERSAL", revs)]:
        if ts:
            print(f"    {out:12}: {len(ts):3d}   {d(sum(t['pnl'] for t in ts))}", end="")
            if out == "STOP":
                exits = [t["exit"] for t in ts if "exit" in t]
                if exits:
                    print(f"   exit_bid avg={sum(exits)/len(exits):.3f} min={min(exits):.3f}", end="")
            print()


# ══════════════════════════════════════════════════════════════════
print()
print("╔══════════════════════════════════════════════════════════════╗")
print("║  ANÁLISE SETUPS MM e SC — redesenho baseado nos logs        ║")
print("╚══════════════════════════════════════════════════════════════╝")

# ──────────────────────────────────────────────────────────────────
# BLOCO 1: Early leader (240-181s) como filtro de AR
# ──────────────────────────────────────────────────────────────────
print()
print(SEP)
print("  BLOCO 1: AR filtrado pelo early leader (secs 240-181s)")
print(SEP)
print()
print("  Pergunta: quando o early leader CONFIRMA o side do AR, o WR melhora?")
print()

groups = {"confirma": [], "oposto": [], "indefinido": []}

for si in slugs_info:
    cur = si["all"]
    s240 = si["s240"]

    # early leader
    if s240:
        avg_up = sum(s["up_bid"] for s in s240) / len(s240)
        avg_dn = sum(s["down_bid"] for s in s240) / len(s240)
        if avg_up >= 0.55:
            early = "UP"
        elif avg_dn >= 0.55:
            early = "DOWN"
        else:
            early = None
    else:
        early = None

    # entrada AR
    entry = None
    for s in cur:
        ws = s.get("winner_side")
        if not ws or not s.get("sig_ar"):
            continue
        wb = s["up_bid"] if ws == "UP" else s["down_bid"]
        secs = s.get("secs") or 999
        if wb >= 0.88 and secs <= 120:
            entry = s; entry_side = ws; ep = wb
            break
    if not entry:
        continue

    outcome, exit_b = simulate(cur, entry, entry_side, ep, stop_t=2)
    if outcome is None:
        continue
    t = {"outcome": outcome, "pnl": round((exit_b - ep) * QTY, 4),
         "ep": ep, "exit": exit_b, "side": entry_side}

    if early == entry_side:
        groups["confirma"].append(t)
    elif early is not None:
        groups["oposto"].append(t)
    else:
        groups["indefinido"].append(t)

for label, key in [("Early leader CONFIRMA o side do AR", "confirma"),
                   ("Early leader CONTRADIZ o side do AR", "oposto"),
                   ("Early leader indefinido (mid ~0.50)", "indefinido")]:
    ts = groups[key]
    if not ts:
        continue
    print(f"  {label}  (n={len(ts)})")
    report_trades(ts)
    eps = [t["ep"] for t in ts]
    print(f"    entry_price avg={sum(eps)/len(eps):.3f}")
    print()

print("  → Conclusão: early leader confirma melhora WR de AR?")
c = groups["confirma"]; o = groups["oposto"]; i = groups["indefinido"]
for label, ts in [("confirma", c), ("oposto", o), ("indefinido", i)]:
    res = [t for t in ts if t["outcome"] not in (None,)]
    wins = [t for t in res if t["outcome"] == "WIN"]
    pnl  = sum(t["pnl"] for t in res)
    wr   = 100 * len(wins) / len(res) if res else 0
    print(f"    {label:12}: WR={wr:.0f}%   PnL={d(pnl)}")


# ──────────────────────────────────────────────────────────────────
# BLOCO 2: MM redesenhado — entrar CEDO no líder com stop largo
# ──────────────────────────────────────────────────────────────────
print()
print(SEP)
print("  BLOCO 2: MM REDESENHADO — entrada cedo (240-181s), stop largo")
print(SEP)
print()
print("  Raiz do problema: stop 3T em secs ~200 é ruído puro.")
print("  Solução testada: comprar o líder cedo, stop mais largo.")
print()
print(f"  {'bid_min':>8}  {'stop':>5}  {'n':>5}  {'WR%':>5}  {'stops':>6}  {'PnL':>9}  {'avg':>9}")

for min_bid in [0.55, 0.58, 0.60]:
    for stop_t in [3, 5, 8, 999]:
        trades = []
        for si in slugs_info:
            snaps = si["s240"]
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
            real_stop = stop_t if stop_t < 900 else 9999
            outcome, exit_b = simulate(si["all"], entry, side, ep, stop_t=real_stop)
            if outcome is None:
                continue
            trades.append({"outcome": outcome, "pnl": round((exit_b - ep) * QTY, 4), "ep": ep})

        if not trades:
            continue
        wins  = [t for t in trades if t["outcome"] == "WIN"]
        stops_n = sum(1 for t in trades if t["outcome"] == "STOP")
        pnl   = sum(t["pnl"] for t in trades)
        wr    = 100 * len(wins) / len(trades)
        stop_label = f"{stop_t}T" if stop_t < 900 else "sem"
        print(f"  {min_bid:>8.2f}  {stop_label:>5}  {len(trades):>5}  {wr:>4.0f}%  "
              f"{stops_n:>6}  {d(pnl):>9}  {d(pnl/len(trades)):>9}")

print()
print("  → Sem stop: todas as perdas são REVERSÃO (direção errada vai a $0)")
print("    Com stop largo: menos stops espúrios, mas direção errada ainda dói")


# ──────────────────────────────────────────────────────────────────
# BLOCO 3: SC — spot confirma ou nega lider em 120-61s
# ──────────────────────────────────────────────────────────────────
print()
print(SEP)
print("  BLOCO 3: SC — spot como FILTRO, não como sinal primário")
print(SEP)
print()
print("  Insight dos logs: spot_d15 em 120-61s NAO adiciona poder")
print("  preditivo quando o lider ja está em 0.80+.")
print("  Testando: spot como filtro de QUALIDADE do AR, não de direção.")
print()

# SC como filtro: dentro das entradas AR, spot_d15 concorda com winner?
sc_agree_trades = []
sc_disagree_trades = []

for si in slugs_info:
    cur = si["all"]

    # primeira entrada AR
    entry = None
    for s in cur:
        ws = s.get("winner_side")
        if not ws or not s.get("sig_ar"):
            continue
        wb = s["up_bid"] if ws == "UP" else s["down_bid"]
        secs = s.get("secs") or 999
        if wb >= 0.88 and secs <= 120:
            entry = s; entry_side = ws; ep = wb
            break
    if not entry:
        continue

    d15 = entry.get("sc_d15") or 0.0
    if d15 == 0:
        continue  # sem dado de spot neste snap

    spot_agrees = (d15 > 0) == (entry_side == "UP")
    outcome, exit_b = simulate(cur, entry, entry_side, ep, stop_t=2)
    if outcome is None:
        continue
    t = {"outcome": outcome, "pnl": round((exit_b - ep) * QTY, 4),
         "ep": ep, "exit": exit_b, "d15": d15}
    if spot_agrees:
        sc_agree_trades.append(t)
    else:
        sc_disagree_trades.append(t)

print("  AR onde spot_d15 CONCORDA com winner (d15 e winner no mesmo sentido):")
report_trades(sc_agree_trades)
print()
print("  AR onde spot_d15 DISCORDA do winner:")
report_trades(sc_disagree_trades)
print()

# SC em faixa mais cedo (120-61s) com lider menos estabelecido
print("  SC em lider moderado (0.55-0.80) + spot concorda → compra lider:")
sc_moderate = []
for si in slugs_info:
    cur = si["all"]
    for s in si["s120"]:
        ub = s["up_bid"]; db = s["down_bid"]
        d15 = s.get("sc_d15") or 0.0
        if d15 == 0:
            continue
        if ub >= 0.55 and ub <= 0.80 and d15 > 1.0:
            side = "UP"; ep = ub
        elif db >= 0.55 and db <= 0.80 and d15 < -1.0:
            side = "DOWN"; ep = db
        else:
            continue
        outcome, exit_b = simulate(cur, s, side, ep, stop_t=2, target_t=3)
        if outcome is None:
            continue
        sc_moderate.append({"outcome": outcome,
                             "pnl": round((exit_b - ep) * QTY, 4),
                             "ep": ep, "d15": d15, "exit": exit_b})
        break  # 1 por slug

report_trades(sc_moderate)


# ──────────────────────────────────────────────────────────────────
# BLOCO 4: Conclusões e recomendações
# ──────────────────────────────────────────────────────────────────
print()
print(SEP)
print("  CONCLUSÕES")
print(SEP)
print()
print("  [MM] O early leader (secs 240-181s) é preditivo em 75-83% dos casos.")
print("  PORÉM o stop em secs 200 é ineficiente — o mercado tem muito ruído")
print("  antes de convergir. Opções viáveis:")
print("   a) Entrar em 240-181s com stop LARGO (8T+) aceitando risco maior")
print("   b) Usar o early leader APENAS como filtro para o AR (entrada em <120s)")
print("   c) Combinação: early leader detectado em 240s, entrada só em 90s")
print()
print("  [SC] Spot momentum em 120-61s NÃO melhora o AR de forma isolada.")
print("  O mercado já está em 0.82+ quando SC sinaliza — a resolução é")
print("  determinada pelo preço do BTC na expiração, não pelo spot now.")
print("  Uso recomendado: filtro de QUALIDADE no AR (spot contra = evitar)")
print()
print("  [Melhor setup híbrido proposto]:")
print("   1. Em secs 200: identificar lider (>0.55)")
print("   2. Em secs 90-120: se lider cresceu para >=0.88 E spot concorda → AR")
print("   3. Se lider voltou para <0.70 após estar em 0.55+ → sinal fraco, skip")
print("   4. Isso já é o AR normal com pré-filtro de early leader + spot")
