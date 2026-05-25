"""
_sim_full_universe.py
Simula TODOS os candidatos de entrada EE nos logs (440 slugs com el_bid
em [0.82, 0.86] em secs [30, 180]), independente de signal_ok.

Para cada slug:
  - Detecta a primeira snapshot com el_bid no range de entrada
  - Simula saida com logica real: stop<0.65, PP>=0.88@secs36-70, WIN<=35
  - Busca BTC momentum 3min antes via Binance klines historico

Categorias de sinal:
  A) signal_ok   = el_vel>=0.08 + cont_ok  (o que o runner atual aceita)
  B) cont_only   = cont_ok mas el_vel<0.08  (bloqueado pelo el_vel gate)
  C) el_only     = el detectado mas sem cont_ok
  D) no_signal   = apenas el_bid em range, sem EL detectado
"""
import json, re, time, requests, statistics
from pathlib import Path
from collections import defaultdict

QTY = 6.0
EE_STOP   = 0.65
EE_PP_BID = 0.88
EE_PP_SECS_LO = 36
EE_PP_SECS_HI = 70
EE_WIN_SECS   = 35

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
TIMEOUT = 6
_cache: dict = {}

def _btc_pre_entry(ts_entry: float, side: str) -> dict:
    start_ms = (int(ts_entry // 60) - 3) * 60 * 1000
    key = f"mom_{start_ms}"
    if key in _cache:
        rows = _cache[key]
    else:
        try:
            r = requests.get(BINANCE_KLINES, params={
                "symbol": "BTCUSDT", "interval": "1m",
                "startTime": start_ms, "limit": 3,
            }, timeout=TIMEOUT)
            rows = r.json() or []
            _cache[key] = rows
            time.sleep(0.10)
        except Exception:
            _cache[key] = []
    if len(rows) < 3:
        return {}
    closes  = [float(rows[i][4]) for i in range(3)]
    highs   = [float(rows[i][2]) for i in range(3)]
    lows    = [float(rows[i][3]) for i in range(3)]
    ref     = closes[-1]
    d1m     = round(closes[2] - closes[1], 2)
    d3m     = round(closes[2] - closes[0], 2)
    rng3m   = round(max(highs) - min(lows), 2)
    return {
        "delta_1m_usd": d1m,
        "delta_3m_usd": d3m,
        "delta_1m_bps": round(d1m / ref * 10000, 2) if ref else 0,
        "range_3m_usd": rng3m,
        "adverse_1m":   (d1m < 0 if side == "UP" else d1m > 0),
        "adverse_3m":   (d3m < 0 if side == "UP" else d3m > 0),
    }

def _simulate_exit(snaps_chrono: list, ep: float, side: str) -> tuple:
    """Simula saida a partir de snapshots ordenados. Retorna (outcome, exit_bid, pnl, exit_secs)."""
    for s in snaps_chrono:
        secs   = s.get("secs", 999)
        up_bid = s.get("up_bid", 0)
        dn_bid = s.get("dn_bid", 0)
        el_bid  = up_bid if side == "UP" else dn_bid
        opp_bid = dn_bid if side == "UP" else up_bid

        if secs <= EE_WIN_SECS:
            if el_bid >= 0.85:
                pnl = round((1.0 - ep) * QTY, 4)
                return ("WIN", 1.0, pnl, secs)
            elif opp_bid >= 0.85:
                pnl = round((0.0 - ep) * QTY, 4)
                return ("REVERSAL", 0.0, pnl, secs)
        elif EE_PP_SECS_LO <= secs <= EE_PP_SECS_HI and el_bid >= EE_PP_BID:
            pnl = round((el_bid - ep) * QTY, 4)
            return ("PROFIT_PROTECT", el_bid, pnl, secs)
        elif 0 < el_bid < EE_STOP:
            pnl = round((el_bid - ep) * QTY, 4)
            return ("STOP_LOSS", el_bid, pnl, secs)

    return ("MISSED", 0.0, 0.0, 0)

# ---------------------------------------------------------------------------
# 1. Varrer snapshots — construir candidatos e trajetorias
# ---------------------------------------------------------------------------
sessions = sorted(Path("logs").glob("ee_paper_*/ee_paper.jsonl"))

# slug -> {"entry": snap, "snaps_after": [...], "ts_entry": float}
slug_data: dict = {}

for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding="utf-8").splitlines() if l.strip()]
    snap_by_slug: dict = defaultdict(list)
    for r in rows:
        if r.get("type") != "snapshot":
            continue
        sl = r.get("current_slug", "")
        if sl:
            snap_by_slug[sl].append(r)

    for sl, snaps in snap_by_slug.items():
        if sl in slug_data:
            continue   # ja processado em outra sessao
        snaps_sorted = sorted(snaps, key=lambda s: s.get("ts", 0))
        entry_snap = None
        entry_idx  = 0
        for i, s in enumerate(snaps_sorted):
            secs = s.get("current_secs")
            ex   = s.get("current_exec", {})
            el   = s.get("early_leader", {})
            side = el.get("early_side")
            if not side or secs is None:
                continue
            up_bid = ex.get("up_bid", 0)
            dn_bid = ex.get("down_bid", 0)
            el_bid  = up_bid if side == "UP" else dn_bid
            if 30 <= secs <= 180 and 0.82 <= el_bid <= 0.86:
                entry_snap = s
                entry_idx  = i
                break

        if not entry_snap:
            continue

        el  = entry_snap.get("early_leader", {})
        ex  = entry_snap.get("current_exec", {})
        side = el.get("early_side", "")
        up_bid = ex.get("up_bid", 0)
        dn_bid = ex.get("down_bid", 0)
        el_bid  = up_bid if side == "UP" else dn_bid
        opp_bid = dn_bid if side == "UP" else up_bid

        # Snapshots apos a entrada (para simular saida)
        snaps_after = []
        for s in snaps_sorted[entry_idx + 1:]:
            secs2 = s.get("current_secs")
            ex2   = s.get("current_exec", {})
            if s.get("current_slug") != sl or secs2 is None:
                continue
            snaps_after.append({
                "secs":   secs2,
                "up_bid": ex2.get("up_bid", 0),
                "dn_bid": ex2.get("down_bid", 0),
            })

        # Classificar tipo de sinal
        sig_ok   = el.get("signal_ok", False)
        cont_ok  = el.get("f3_ok", False)
        el_vel   = el.get("el_vel", 0)
        if sig_ok:
            sig_cat = "A_signal_ok"
        elif cont_ok:
            sig_cat = "B_cont_only"
        elif el.get("early_side"):
            sig_cat = "C_el_only"
        else:
            sig_cat = "D_no_signal"

        slug_data[sl] = {
            "slug":       sl,
            "ts":         entry_snap.get("ts", 0),
            "ep":         round(el_bid, 4),
            "side":       side,
            "secs":       entry_snap.get("current_secs"),
            "el_vel":     el_vel,
            "cont_ok":    cont_ok,
            "sig_ok":     sig_ok,
            "sig_cat":    sig_cat,
            "snaps_after": snaps_after,
        }

print(f"Candidatos de entrada encontrados: {len(slug_data)}")
by_cat = defaultdict(list)
for v in slug_data.values():
    by_cat[v["sig_cat"]].append(v)
for cat in sorted(by_cat):
    print(f"  {cat}: {len(by_cat[cat])}")

# ---------------------------------------------------------------------------
# 2. Simular saidas
# ---------------------------------------------------------------------------
print("\nSimulando saidas...")
for r in slug_data.values():
    outcome, exit_bid, pnl, exit_secs = _simulate_exit(r["snaps_after"], r["ep"], r["side"])
    r.update(outcome=outcome, exit_bid=exit_bid, pnl=pnl, exit_secs=exit_secs)

valid = [r for r in slug_data.values() if r.get("outcome") not in ("MISSED", None)]
print(f"Trades simulados com outcome: {len(valid)}")

# ---------------------------------------------------------------------------
# 3. Buscar BTC momentum
# ---------------------------------------------------------------------------
print("Buscando BTC momentum (pode levar ~60s)...")
n_ok = 0
for r in valid:
    mom = _btc_pre_entry(r["ts"], r["side"])
    r.update(mom)
    if mom:
        n_ok += 1
valid_m = [r for r in valid if r.get("delta_1m_usd") is not None]
print(f"  Momentum obtido: {n_ok}/{len(valid)}")

# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------
def _stats_row(grp, label):
    n = len(grp)
    if n == 0:
        return
    wins  = sum(1 for r in grp if r["pnl"] > 0)
    stops = sum(1 for r in grp if r.get("outcome") == "STOP_LOSS")
    pnl   = round(sum(r["pnl"] for r in grp), 2)
    adv   = sum(1 for r in grp if r.get("adverse_1m"))
    rng   = [r["range_3m_usd"] for r in grp if r.get("range_3m_usd")]
    rng_avg = sum(rng)/len(rng) if rng else 0
    print(f"  {label:<36}  {n:>4}  {wins/n:>7.1%}  {stops/n:>6.1%}  "
          f"{pnl:>+9.2f}  {pnl/n:>+8.3f}  {adv/n:>6.1%}  {rng_avg:>6.1f}")

# ---------------------------------------------------------------------------
# 4. Resultados por categoria de sinal
# ---------------------------------------------------------------------------
print(f"\n{'='*90}")
print(f"  RESULTADOS POR CATEGORIA DE SINAL  (simulado com logica real runner)")
print(f"{'='*90}")
print(f"  {'Categoria':<36}  {'n':>4}  {'WR':>7}  {'STOP%':>6}  {'PnL':>9}  {'avg':>8}  {'adv1m':>6}  {'rng3m':>6}")
print("  " + "-"*84)
for cat in sorted(by_cat):
    grp = [r for r in valid_m if r["sig_cat"] == cat]
    _stats_row(grp, cat)
_stats_row(valid_m, "TOTAL")

# ---------------------------------------------------------------------------
# 5. Por el_vel (bins)
# ---------------------------------------------------------------------------
print(f"\n{'='*90}")
print(f"  POR FAIXA el_vel — universo completo")
print(f"{'='*90}")
print(f"  {'el_vel':>22}  {'n':>4}  {'WR':>7}  {'STOP%':>6}  {'PnL':>9}  {'avg':>8}  {'adv1m':>6}  {'rng3m':>6}")
print("  " + "-"*84)
bins = [
    ("<0.0",   lambda r: r["el_vel"] < 0),
    ("[0.0,0.04)", lambda r: 0.0 <= r["el_vel"] < 0.04),
    ("[0.04,0.06)", lambda r: 0.04 <= r["el_vel"] < 0.06),
    ("[0.06,0.08)", lambda r: 0.06 <= r["el_vel"] < 0.08),
    ("[0.08,0.10)", lambda r: 0.08 <= r["el_vel"] < 0.10),
    ("[0.10,0.13)", lambda r: 0.10 <= r["el_vel"] < 0.13),
    (">=0.13",  lambda r: r["el_vel"] >= 0.13),
]
for label, fn in bins:
    grp = [r for r in valid_m if fn(r)]
    _stats_row(grp, label)

# ---------------------------------------------------------------------------
# 6. Adverso vs Favoravel — universo completo
# ---------------------------------------------------------------------------
print(f"\n{'='*90}")
print(f"  ADVERSO vs FAVORAVEL — universo completo (por categoria)")
print(f"{'='*90}")
print(f"  {'Grupo':<36}  {'n':>4}  {'WR':>7}  {'STOP%':>6}  {'PnL':>9}  {'avg':>8}")
print("  " + "-"*72)
for cat in sorted(by_cat):
    grp = [r for r in valid_m if r["sig_cat"] == cat]
    if not grp: continue
    adv = [r for r in grp if r.get("adverse_1m")]
    fav = [r for r in grp if not r.get("adverse_1m")]
    print(f"  [{cat}]")
    for lbl, sub in [("  adverso_1m", adv), ("  favoravel_1m", fav)]:
        if not sub: continue
        n = len(sub)
        wins  = sum(1 for r in sub if r["pnl"] > 0)
        stops = sum(1 for r in sub if r.get("outcome") == "STOP_LOSS")
        pnl   = round(sum(r["pnl"] for r in sub), 2)
        print(f"  {lbl:<36}  {n:>4}  {wins/n:>7.1%}  {stops/n:>6.1%}  {pnl:>+9.2f}  {pnl/n:>+8.3f}")

# ---------------------------------------------------------------------------
# 7. Gate volatilidade — universo completo
# ---------------------------------------------------------------------------
print(f"\n{'='*90}")
print(f"  GATE range_3m_usd < X — universo completo")
print(f"{'='*90}")
print(f"  {'Gate':>12}  {'n':>4}  {'WR':>7}  {'STOP%':>6}  {'PnL':>9}  {'avg':>8}  bloq")
print("  " + "-"*66)
base_n = len(valid_m)
base_pnl = round(sum(r["pnl"] for r in valid_m), 2)
base_wr  = sum(1 for r in valid_m if r["pnl"] > 0) / base_n
for gate in [50, 80, 100, 150, 200]:
    sub = [r for r in valid_m if r.get("range_3m_usd", 999) < gate]
    if not sub: continue
    n     = len(sub)
    wins  = sum(1 for r in sub if r["pnl"] > 0)
    stops = sum(1 for r in sub if r.get("outcome") == "STOP_LOSS")
    pnl   = round(sum(r["pnl"] for r in sub), 2)
    print(f"  {gate:>12}  {n:>4}  {wins/n:>7.1%}  {stops/n:>6.1%}  {pnl:>+9.2f}  {pnl/n:>+8.3f}  ({base_n-n})")
print(f"  {'base':>12}  {base_n:>4}  {base_wr:>7.1%}  "
      f"{sum(1 for r in valid_m if r.get('outcome')=='STOP_LOSS')/base_n:>6.1%}  "
      f"{base_pnl:>+9.2f}  {base_pnl/base_n:>+8.3f}")

# ---------------------------------------------------------------------------
# 8. Combinacoes no universo completo
# ---------------------------------------------------------------------------
print(f"\n{'='*90}")
print(f"  COMBINACOES — universo completo (440 candidatos)")
print(f"{'='*90}")
print(f"  {'Cenario':<40}  {'n':>4}  {'WR':>7}  {'STOP%':>6}  {'PnL':>9}  {'avg':>8}")
print("  " + "-"*78)
combos = [
    ("base (qualquer el_bid em range)",       lambda r: True),
    ("el_vel>=0.08 (signal_ok)",              lambda r: r["el_vel"] >= 0.08 and r["cont_ok"]),
    ("el_vel>=0.13",                          lambda r: r["el_vel"] >= 0.13),
    ("adverso_1m (universo completo)",        lambda r: r.get("adverse_1m")),
    ("favoravel_1m (universo completo)",      lambda r: not r.get("adverse_1m")),
    ("range_3m<80",                           lambda r: r.get("range_3m_usd", 999) < 80),
    ("el_vel>=0.08 + adverso_1m",             lambda r: r["el_vel"] >= 0.08 and r["cont_ok"] and r.get("adverse_1m")),
    ("el_vel>=0.08 + favoravel_1m",           lambda r: r["el_vel"] >= 0.08 and r["cont_ok"] and not r.get("adverse_1m")),
    ("el_vel>=0.08 + range_3m<80",            lambda r: r["el_vel"] >= 0.08 and r["cont_ok"] and r.get("range_3m_usd",999) < 80),
    ("el_vel>=0.13 + adverso_1m",             lambda r: r["el_vel"] >= 0.13 and r.get("adverse_1m")),
    ("el_vel>=0.13 + range_3m<80",            lambda r: r["el_vel"] >= 0.13 and r.get("range_3m_usd",999) < 80),
    ("el_vel>=0.13 + adv + range<80",         lambda r: r["el_vel"] >= 0.13 and r.get("adverse_1m") and r.get("range_3m_usd",999) < 80),
]
for label, fn in combos:
    sub = [r for r in valid_m if fn(r)]
    if not sub: continue
    n     = len(sub)
    wins  = sum(1 for r in sub if r["pnl"] > 0)
    stops = sum(1 for r in sub if r.get("outcome") == "STOP_LOSS")
    pnl   = round(sum(r["pnl"] for r in sub), 2)
    print(f"  {label:<40}  {n:>4}  {wins/n:>7.1%}  {stops/n:>6.1%}  {pnl:>+9.2f}  {pnl/n:>+8.3f}")
