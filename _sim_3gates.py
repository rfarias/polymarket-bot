"""
_sim_3gates.py
Testa os 3 gates de estabilidade do EL bid identificados em _sim_el_bid_stability.py.

GATE 1 — Dip entry   : so entra quando el_bid_delta_30s < threshold
                        (bid deu pullback antes de entrar, nao entrada em pico)
GATE 2 — Mono block  : bloqueia quando mono_ratio_30s > threshold
                        (bid subindo mono-direcional = chasing = mais risco)
GATE 3 — Opp block   : bloqueia quando opp_delta_30s > threshold
                        (counter bid crescendo antes da entrada)

Para cada gate: mostra "passa", "bloqueado" e delta de qualidade.
Ao final: combinacoes e ranking.

Uso:
    python _sim_3gates.py
    python _sim_3gates.py --logs "test_data/ee_paper/**/*.jsonl"
    python _sim_3gates.py --logs "logs/ee_paper_*/ee_paper.jsonl" --cat ALL
"""
import json, argparse
from pathlib import Path
from collections import defaultdict

# ---------------------------------------------------------------------------
# Config / parametros do runner EE
# ---------------------------------------------------------------------------
QTY           = 6.0
EE_STOP       = 0.65
EE_PP_BID     = 0.88
EE_PP_SECS_LO = 36
EE_PP_SECS_HI = 70
EE_WIN_SECS   = 35
EE_ENTRY_LO   = 0.82
EE_ENTRY_HI   = 0.86

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
_ap = argparse.ArgumentParser()
_ap.add_argument('--logs', default='logs/ee_paper_*/ee_paper.jsonl')
_ap.add_argument('--cat', default='ALL',
                 help='A=signal_ok, AB=signal_ok+cont_only, ALL')
_args = _ap.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _simulate_exit(snaps_after, ep, side):
    for s in snaps_after:
        secs    = s["secs"]
        el_bid  = s["up_bid"] if side == "UP" else s["dn_bid"]
        opp_bid = s["dn_bid"] if side == "UP" else s["up_bid"]
        if secs <= EE_WIN_SECS:
            if el_bid >= 0.85:
                return "WIN", round((1.0 - ep) * QTY, 4)
            elif opp_bid >= 0.85:
                return "REVERSAL", round((0.0 - ep) * QTY, 4)
        elif EE_PP_SECS_LO <= secs <= EE_PP_SECS_HI and el_bid >= EE_PP_BID:
            return "PROFIT_PROTECT", round((el_bid - ep) * QTY, 4)
        elif 0 < el_bid < EE_STOP:
            return "STOP_LOSS", round((el_bid - ep) * QTY, 4)
    return "MISSED", 0.0


def _el(s, side):  return s["up_bid"] if side == "UP" else s["dn_bid"]
def _opp(s, side): return s["dn_bid"] if side == "UP" else s["up_bid"]


def _stability(snaps_before, entry_ts, entry_el_bid, side):
    w30 = [s for s in snaps_before if entry_ts - s["ts"] <= 30 and _el(s, side) > 0]
    w60 = [s for s in snaps_before if entry_ts - s["ts"] <= 60 and _el(s, side) > 0]
    r = {}

    # delta_30s
    if len(w30) >= 2:
        bids = [_el(s, side) for s in w30]
        r["delta_30s"]  = round(entry_el_bid - bids[0], 4)
        r["range_30s"]  = round(max(bids) - min(bids), 4)
    else:
        r["delta_30s"] = r["range_30s"] = None

    # delta_60s
    if len(w60) >= 2:
        r["delta_60s"] = round(entry_el_bid - _el(w60[0], side), 4)
    else:
        r["delta_60s"] = None

    # mono_ratio_30s
    if len(w30) >= 3:
        bids = [_el(s, side) for s in w30]
        deltas = [bids[i+1] - bids[i] for i in range(len(bids)-1)]
        pos = sum(1 for d in deltas if d > 0.001)
        neg = sum(1 for d in deltas if d < -0.001)
        total = len(deltas)
        r["mono"] = round(max(pos, neg) / total, 3) if total else None
    else:
        r["mono"] = None

    # snaps_in_range / time_in_range
    cnt = 0
    for s in reversed(snaps_before):
        b = _el(s, side)
        if EE_ENTRY_LO <= b <= EE_ENTRY_HI:
            cnt += 1
        else:
            break
    r["snaps_in_range"] = cnt
    if cnt > 0 and len(snaps_before) >= cnt:
        r["time_in_range_s"] = round(entry_ts - snaps_before[-cnt]["ts"], 1)
    else:
        r["time_in_range_s"] = 0.0

    # opp_delta_30s
    w30o = [s for s in snaps_before if entry_ts - s["ts"] <= 30 and _opp(s, side) > 0]
    if len(w30o) >= 2:
        opps = [_opp(s, side) for s in w30o]
        r["opp_delta"] = round(opps[-1] - opps[0], 4)
    else:
        r["opp_delta"] = None

    r["n30"] = len(w30)
    return r


# ---------------------------------------------------------------------------
# 1. Carregar snapshots e construir candidatos
# ---------------------------------------------------------------------------
print(f"\nCarregando snapshots de: {_args.logs}")
sessions = sorted(Path(".").glob(_args.logs))
print(f"  Arquivos: {len(sessions)}")

slug_data: dict = {}

for lp in sessions:
    rows = []
    for line in lp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line: continue
        try:    rows.append(json.loads(line))
        except: pass

    snap_by_slug: dict = defaultdict(list)
    for r in rows:
        if r.get("type") != "snapshot": continue
        sl = r.get("current_slug", "")
        if sl: snap_by_slug[sl].append(r)

    for sl, snaps in snap_by_slug.items():
        if sl in slug_data: continue
        snaps_s = sorted(snaps, key=lambda s: s.get("ts", 0))

        entry_snap = entry_idx = None
        for i, s in enumerate(snaps_s):
            secs = s.get("current_secs")
            ex   = s.get("current_exec", {})
            el   = s.get("early_leader", {})
            side = el.get("early_side")
            if not side or secs is None: continue
            el_bid = ex.get("up_bid", 0) if side == "UP" else ex.get("down_bid", 0)
            if 30 <= secs <= 180 and EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI:
                entry_snap, entry_idx = s, i
                break
        if entry_snap is None: continue

        el_d  = entry_snap.get("early_leader", {})
        ex_d  = entry_snap.get("current_exec", {})
        side  = el_d.get("early_side", "")
        ep    = ex_d.get("up_bid", 0) if side == "UP" else ex_d.get("down_bid", 0)

        sig_ok  = el_d.get("signal_ok", False)
        cont_ok = el_d.get("f3_ok", False)
        el_vel  = el_d.get("el_vel", 0)

        if _args.cat == "A"  and not sig_ok:            continue
        if _args.cat == "AB" and not (sig_ok or cont_ok): continue

        if   sig_ok:   sig_cat = "A_signal_ok"
        elif cont_ok:  sig_cat = "B_cont_only"
        elif side:     sig_cat = "C_el_only"
        else:          continue

        # snaps antes e depois
        snaps_before, snaps_after = [], []
        for s in snaps_s[:entry_idx]:
            sc = s.get("current_secs"); ex2 = s.get("current_exec", {})
            if s.get("current_slug") != sl or sc is None: continue
            snaps_before.append({"ts": s.get("ts",0), "secs": sc,
                                  "up_bid": ex2.get("up_bid",0),
                                  "dn_bid": ex2.get("down_bid",0)})
        for s in snaps_s[entry_idx+1:]:
            sc = s.get("current_secs"); ex2 = s.get("current_exec", {})
            if s.get("current_slug") != sl or sc is None: continue
            snaps_after.append({"secs": sc, "up_bid": ex2.get("up_bid",0),
                                 "dn_bid": ex2.get("down_bid",0)})

        stab = _stability(snaps_before, entry_snap.get("ts",0), round(ep,4), side)
        slug_data[sl] = {"slug": sl, "ep": round(ep,4), "side": side,
                         "secs": entry_snap.get("current_secs"),
                         "el_vel": el_vel, "sig_ok": sig_ok,
                         "cont_ok": cont_ok, "sig_cat": sig_cat,
                         "snaps_after": snaps_after, **stab}

# simular saidas
for r in slug_data.values():
    r["outcome"], r["pnl"] = _simulate_exit(r["snaps_after"], r["ep"], r["side"])

valid = [r for r in slug_data.values()
         if r.get("outcome") not in ("MISSED", None)]
print(f"  Candidatos: {len(slug_data)}  |  com outcome: {len(valid)}")
from collections import Counter
print(f"  Categorias: {dict(Counter(r['sig_cat'] for r in valid))}")
print(f"  Outcomes  : {dict(Counter(r['outcome'] for r in valid))}")


# ---------------------------------------------------------------------------
# helpers de exibicao
# ---------------------------------------------------------------------------
W = 36

def _stats(grp):
    n = len(grp)
    if n == 0: return n, 0, 0, 0, 0
    wins  = sum(1 for r in grp if r["pnl"] > 0)
    stops = sum(1 for r in grp if r["outcome"] == "STOP_LOSS")
    pnl   = round(sum(r["pnl"] for r in grp), 2)
    return n, wins/n, stops/n, pnl, pnl/n

def _row(label, grp, w=W):
    n, wr, sp, pnl, avg = _stats(grp)
    if n == 0: return
    print(f"  {label:<{w}}  {n:>4}  {wr:>7.1%}  {sp:>6.1%}  {pnl:>+9.2f}  {avg:>+8.3f}")

HDR = f"  {'Cenario':<{W}}  {'n':>4}  {'WR':>7}  {'STOP%':>6}  {'PnL':>9}  {'avg':>8}"
SEP = "  " + "-"*74


# ---------------------------------------------------------------------------
# 2. BASE
# ---------------------------------------------------------------------------
base_A   = [r for r in valid if r["sig_cat"] == "A_signal_ok"]
base_AB  = [r for r in valid if r["sig_cat"] in ("A_signal_ok", "B_cont_only")]
base_ALL = valid

print(f"\n{'='*78}")
print(f"  BASE — sem nenhum gate")
print(f"{'='*78}")
print(HDR); print(SEP)
_row("A  signal_ok",              base_A)
_row("AB signal_ok + cont_only",  base_AB)
_row("ALL (universo completo)",   base_ALL)


# ---------------------------------------------------------------------------
# helper: mostrar gate com thresholds
# ---------------------------------------------------------------------------
def _gate_block(valid_set, key, op, thresholds, label_pfx, label_blk="bloqueado"):
    """
    op = 'lt' -> passa quando r[key] < thr  (ex: delta_30s < -0.02)
    op = 'gt' -> passa quando r[key] > thr  (ex: mono <= 0.75 = bloq quando > 0.75)
    """
    has = [r for r in valid_set if r.get(key) is not None]
    no_data = [r for r in valid_set if r.get(key) is None]
    print(f"\n  Universo com dados: {len(has)}/{len(valid_set)}  "
          f"(sem dados suficientes: {len(no_data)})")
    print(HDR); print(SEP)
    for thr in thresholds:
        if op == "lt":
            passes  = [r for r in has if r[key] < thr]
            blocked = [r for r in has if r[key] >= thr]
            lp = f"{label_pfx} < {thr:+.2f}"
        else:  # gt block
            passes  = [r for r in has if r[key] <= thr]
            blocked = [r for r in has if r[key] > thr]
            lp = f"{label_pfx} <= {thr:.2f}"
        lb = f"  {label_blk}"
        _row(lp,       passes)
        _row(lb,       blocked)
        print(SEP)
    _row("base (todos com dados)", has)


# ---------------------------------------------------------------------------
# 3. GATE 1 — Dip entry: delta_30s < threshold
# ---------------------------------------------------------------------------
print(f"\n{'='*78}")
print(f"  GATE 1 — DIP ENTRY: so entrar quando el_bid_delta_30s < threshold")
print(f"  Hipotese: bid que deu pullback antes de entrar = entrada em dip = mais segura")
print(f"{'='*78}")

for label, grp in [("A signal_ok", base_A), ("ALL universo", base_ALL)]:
    print(f"\n  [{label}]")
    _gate_block(grp, "delta_30s", "lt",
                [-0.06, -0.04, -0.02, -0.01, 0.0, 0.02],
                "dip_entry delta_30s")

# Distribuicao do delta_30s nos stops vs wins (para calibrar threshold)
stops = [r for r in valid if r["outcome"] == "STOP_LOSS" and r.get("delta_30s") is not None]
wins  = [r for r in valid if r["outcome"] != "STOP_LOSS" and r.get("delta_30s") is not None]
if stops and wins:
    print(f"\n  Distribuicao delta_30s — STOP_LOSS vs nao-STOP:")
    stop_d = [r["delta_30s"] for r in stops]
    win_d  = [r["delta_30s"] for r in wins]
    print(f"    STOP_LOSS (n={len(stops)}): avg={sum(stop_d)/len(stop_d):+.4f}  "
          f"min={min(stop_d):+.4f}  max={max(stop_d):+.4f}")
    print(f"    nao-STOP  (n={len(wins)}): avg={sum(win_d)/len(win_d):+.4f}  "
          f"min={min(win_d):+.4f}  max={max(win_d):+.4f}")
    print(f"    diff: {sum(stop_d)/len(stop_d) - sum(win_d)/len(win_d):+.4f}")


# ---------------------------------------------------------------------------
# 4. GATE 2 — Mono block: bloquear quando mono_ratio > threshold
# ---------------------------------------------------------------------------
print(f"\n{'='*78}")
print(f"  GATE 2 — MONO BLOCK: bloquear quando mono_ratio_30s > threshold")
print(f"  Hipotese: bid subindo monotonicamente = chasing = mais risco de reversao")
print(f"{'='*78}")

for label, grp in [("A signal_ok", base_A), ("ALL universo", base_ALL)]:
    print(f"\n  [{label}]")
    _gate_block(grp, "mono", "gt",
                [0.50, 0.60, 0.67, 0.75, 0.80],
                "mono_ok mono_ratio", "bloqueado mono_ratio")

stops_m = [r for r in valid if r["outcome"] == "STOP_LOSS" and r.get("mono") is not None]
wins_m  = [r for r in valid if r["outcome"] != "STOP_LOSS" and r.get("mono") is not None]
if stops_m and wins_m:
    print(f"\n  Distribuicao mono_ratio — STOP_LOSS vs nao-STOP:")
    sm = [r["mono"] for r in stops_m]
    wm = [r["mono"] for r in wins_m]
    print(f"    STOP_LOSS (n={len(sm)}): avg={sum(sm)/len(sm):.4f}")
    print(f"    nao-STOP  (n={len(wm)}): avg={sum(wm)/len(wm):.4f}")
    print(f"    diff: {sum(sm)/len(sm) - sum(wm)/len(wm):+.4f}")


# ---------------------------------------------------------------------------
# 5. GATE 3 — Opp delta: bloquear quando opp_delta > threshold
# ---------------------------------------------------------------------------
print(f"\n{'='*78}")
print(f"  GATE 3 — OPP BLOCK: bloquear quando opp_delta_30s > threshold")
print(f"  Hipotese: counter bid crescendo antes da entrada = pressao contra o lado EL")
print(f"{'='*78}")

for label, grp in [("A signal_ok", base_A), ("ALL universo", base_ALL)]:
    print(f"\n  [{label}]")
    _gate_block(grp, "opp_delta", "gt",
                [-0.01, 0.0, 0.01, 0.02],
                "opp_ok opp_delta", "bloqueado opp_delta")

stops_o = [r for r in valid if r["outcome"] == "STOP_LOSS" and r.get("opp_delta") is not None]
wins_o  = [r for r in valid if r["outcome"] != "STOP_LOSS" and r.get("opp_delta") is not None]
if stops_o and wins_o:
    print(f"\n  Distribuicao opp_delta — STOP_LOSS vs nao-STOP:")
    so = [r["opp_delta"] for r in stops_o]
    wo = [r["opp_delta"] for r in wins_o]
    print(f"    STOP_LOSS (n={len(so)}): avg={sum(so)/len(so):+.4f}")
    print(f"    nao-STOP  (n={len(wo)}): avg={sum(wo)/len(wo):+.4f}")
    print(f"    diff: {sum(so)/len(so) - sum(wo)/len(wo):+.4f}")


# ---------------------------------------------------------------------------
# 6. COMBINACOES — melhores gates juntos vs el_vel>=0.13 (referencia)
# ---------------------------------------------------------------------------
print(f"\n{'='*78}")
print(f"  COMBINACOES — gates individuais e combinados vs referencia el_vel>=0.13")
print(f"{'='*78}")

def _comb(label, fn, grp=base_ALL, w=W):
    sub = [r for r in grp if fn(r)]
    _row(label, sub, w)

has_d   = lambda r: r.get("delta_30s") is not None
has_m   = lambda r: r.get("mono") is not None
has_o   = lambda r: r.get("opp_delta") is not None
has_all = lambda r: has_d(r) and has_m(r) and has_o(r)

combos_all = [
    ("base ALL",                    lambda r: True),
    ("base A (signal_ok)",          lambda r: r["sig_cat"] == "A_signal_ok"),
    ("ref: el_vel>=0.13",           lambda r: r["el_vel"] >= 0.13),
    # gate 1 sozinho
    ("G1: dip delta_30s<0",         lambda r: has_d(r) and r["delta_30s"] < 0),
    ("G1: dip delta_30s<-0.02",     lambda r: has_d(r) and r["delta_30s"] < -0.02),
    ("G1: dip delta_30s<-0.04",     lambda r: has_d(r) and r["delta_30s"] < -0.04),
    # gate 2 sozinho
    ("G2: mono<=0.67",              lambda r: has_m(r) and r["mono"] <= 0.67),
    ("G2: mono<=0.75",              lambda r: has_m(r) and r["mono"] <= 0.75),
    # gate 3 sozinho
    ("G3: opp_delta<=0",            lambda r: has_o(r) and r["opp_delta"] <= 0),
    ("G3: opp_delta<=0.01",         lambda r: has_o(r) and r["opp_delta"] <= 0.01),
    # combinacoes G1+G2
    ("G1+G2 dip<-0.02+mono<=0.75", lambda r: has_d(r) and has_m(r) and r["delta_30s"] < -0.02 and r["mono"] <= 0.75),
    # combinacoes com el_vel
    ("G1 dip<0 + el_vel>=0.08",     lambda r: has_d(r) and r["delta_30s"] < 0 and r["el_vel"] >= 0.08 and r["sig_cat"]=="A_signal_ok"),
    ("G1 dip<-0.04 + el_vel>=0.08", lambda r: has_d(r) and r["delta_30s"] < -0.04 and r["el_vel"] >= 0.08 and r["sig_cat"]=="A_signal_ok"),
    ("G3 opp<=0 + el_vel>=0.08",    lambda r: has_o(r) and r["opp_delta"] <= 0 and r["el_vel"] >= 0.08 and r["sig_cat"]=="A_signal_ok"),
    # G1+G3
    ("G1+G3 dip<0+opp<=0.01",       lambda r: has_d(r) and has_o(r) and r["delta_30s"] < 0 and r["opp_delta"] <= 0.01),
    # tripla
    ("G1+G2+G3 (threshold otimo)",  lambda r: has_all(r) and r["delta_30s"] < 0 and r["mono"] <= 0.75 and r["opp_delta"] <= 0.01),
]

print(HDR); print(SEP)
for label, fn in combos_all:
    _comb(label, fn)

# ---------------------------------------------------------------------------
# 7. RESUMO — qual gate melhora mais o avg sem destruir volume
# ---------------------------------------------------------------------------
print(f"\n{'='*78}")
print(f"  RESUMO — impacto de cada gate no avg/trade (universo ALL)")
print(f"{'='*78}")
print(f"  {'Gate':<36}  {'n_passa':>7}  {'n_blq':>6}  {'avg_passa':>9}  {'avg_blq':>9}  delta_avg")
print("  " + "-"*80)

gates_summary = [
    ("G1 dip delta_30s < 0",
     lambda r: has_d(r) and r["delta_30s"] < 0,
     lambda r: has_d(r) and r["delta_30s"] >= 0),
    ("G1 dip delta_30s < -0.02",
     lambda r: has_d(r) and r["delta_30s"] < -0.02,
     lambda r: has_d(r) and r["delta_30s"] >= -0.02),
    ("G1 dip delta_30s < -0.04",
     lambda r: has_d(r) and r["delta_30s"] < -0.04,
     lambda r: has_d(r) and r["delta_30s"] >= -0.04),
    ("G2 mono_ratio <= 0.67",
     lambda r: has_m(r) and r["mono"] <= 0.67,
     lambda r: has_m(r) and r["mono"] > 0.67),
    ("G2 mono_ratio <= 0.75",
     lambda r: has_m(r) and r["mono"] <= 0.75,
     lambda r: has_m(r) and r["mono"] > 0.75),
    ("G3 opp_delta <= 0",
     lambda r: has_o(r) and r["opp_delta"] <= 0,
     lambda r: has_o(r) and r["opp_delta"] > 0),
    ("G3 opp_delta <= 0.01",
     lambda r: has_o(r) and r["opp_delta"] <= 0.01,
     lambda r: has_o(r) and r["opp_delta"] > 0.01),
]
for label, fp, fb in gates_summary:
    gp = [r for r in valid if fp(r)]
    gb = [r for r in valid if fb(r)]
    if not gp: continue
    ap  = sum(r["pnl"] for r in gp) / len(gp) if gp else 0
    ab_ = sum(r["pnl"] for r in gb) / len(gb) if gb else 0
    print(f"  {label:<36}  {len(gp):>7}  {len(gb):>6}  {ap:>+9.3f}  {ab_:>+9.3f}  {ap-ab_:>+9.3f}")
