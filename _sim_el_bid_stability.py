"""
_sim_el_bid_stability.py
Analisa estabilidade do bid EL nos snapshots ANTES da entrada.

Para cada candidato de entrada (primeiro snap com el_bid em [0.82,0.86] e signal_ok),
olha os snaps dos 60s anteriores e calcula:

  el_bid_range_30s  -- amplitude max-min do EL bid nos ultimos 30s
  el_bid_delta_30s  -- bid_entry - bid_30s_ago  (>0 = bid ainda subindo = "chasing")
  el_bid_delta_60s  -- bid_entry - bid_60s_ago
  mono_ratio_30s    -- fracao de movimentos monotonicos no ultimo 30s (0..1)
  snaps_in_range    -- snaps consecutivos com el_bid em [0.82,0.86] ANTES de entrar
  time_in_range_s   -- segundos consecutivos em [0.82,0.86] antes de entrar
  opp_delta_30s     -- variacao do opp_bid nos ultimos 30s (>0 = pressao crescendo)

Hipoteses a testar:
  - el_bid ainda subindo (delta_30s > 0) = "chasing" = mais risco
  - el_bid esfriado/estavel (delta_30s <= 0) = consolidacao = menos risco
  - mono_ratio alto = movimento unidirecional = reversao mais provavel
  - mais tempo/snaps no range = sinal maduro = mais seguro
  - opp_delta > 0 = counter crescendo = mais risco

Uso:
    python _sim_el_bid_stability.py
    python _sim_el_bid_stability.py --logs "logs/ee_real_*/ee_real.jsonl"
    python _sim_el_bid_stability.py --logs "test_data/ee_paper/**/*.jsonl"
"""
import json, argparse, statistics
from pathlib import Path
from collections import defaultdict

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
_ap.add_argument('--logs', default='logs/ee_paper_*/ee_paper.jsonl',
                 help='Glob pattern para logs EE (paper ou real)')
_ap.add_argument('--cat', default='A',
                 help='Categorias a incluir: A=signal_ok, AB=signal_ok+cont_only, ALL')
_args = _ap.parse_args()


# ---------------------------------------------------------------------------
# Simulacao de saida (identica ao _sim_full_universe.py)
# ---------------------------------------------------------------------------
def _simulate_exit(snaps_after: list, ep: float, side: str):
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


# ---------------------------------------------------------------------------
# Metricas de estabilidade dos snaps anteriores a entrada
# ---------------------------------------------------------------------------
def _stability(snaps_before: list, entry_ts: float, entry_el_bid: float,
               side: str) -> dict:
    """snaps_before: lista ordenada por ts, todos ANTES do snap de entrada."""

    def el(s):  return s["up_bid"] if side == "UP" else s["dn_bid"]
    def opp(s): return s["dn_bid"] if side == "UP" else s["up_bid"]

    # Janelas de 30s e 60s antes da entrada
    w60 = [s for s in snaps_before if entry_ts - s["ts"] <= 60 and el(s) > 0]
    w30 = [s for s in snaps_before if entry_ts - s["ts"] <= 30 and el(s) > 0]

    r: dict = {"snap_count_30s": len(w30), "snap_count_60s": len(w60)}

    # -- el_bid_range_30s e el_bid_delta_30s --
    if len(w30) >= 2:
        bids = [el(s) for s in w30]
        r["el_bid_range_30s"]  = round(max(bids) - min(bids), 4)
        r["el_bid_delta_30s"]  = round(entry_el_bid - bids[0], 4)  # pos=subindo
    else:
        r["el_bid_range_30s"] = r["el_bid_delta_30s"] = None

    # -- el_bid_delta_60s --
    if len(w60) >= 2:
        r["el_bid_delta_60s"] = round(entry_el_bid - el(w60[0]), 4)
    else:
        r["el_bid_delta_60s"] = None

    # -- mono_ratio_30s: fracao de movimentos na direcao dominante --
    if len(w30) >= 3:
        bids = [el(s) for s in w30]
        deltas = [bids[i+1] - bids[i] for i in range(len(bids)-1)]
        pos = sum(1 for d in deltas if d > 0.001)
        neg = sum(1 for d in deltas if d < -0.001)
        total = len(deltas)
        r["mono_ratio_30s"] = round(max(pos, neg) / total, 3) if total else None
        r["mono_dir"] = "up" if pos >= neg else "dn"
    else:
        r["mono_ratio_30s"] = r["mono_dir"] = None

    # -- snaps_in_range / time_in_range: quantos snaps consecutivos em [0.82,0.86] --
    cnt = 0
    for s in reversed(snaps_before):
        b = el(s)
        if EE_ENTRY_LO <= b <= EE_ENTRY_HI:
            cnt += 1
        else:
            break
    r["snaps_in_range"] = cnt
    if cnt > 0 and len(snaps_before) >= cnt:
        first_in_range = snaps_before[-cnt]
        r["time_in_range_s"] = round(entry_ts - first_in_range["ts"], 1)
    else:
        r["time_in_range_s"] = 0.0

    # -- opp_delta_30s: counter bid crescendo antes da entrada? --
    w30_opp = [s for s in snaps_before if entry_ts - s["ts"] <= 30 and opp(s) > 0]
    if len(w30_opp) >= 2:
        opps = [opp(s) for s in w30_opp]
        r["opp_delta_30s"] = round(opps[-1] - opps[0], 4)
    else:
        r["opp_delta_30s"] = None

    return r


# ---------------------------------------------------------------------------
# 1. Carregar snapshots e construir candidatos
# ---------------------------------------------------------------------------
print("Carregando snapshots...")
sessions = sorted(Path(".").glob(_args.logs))
print(f"  Arquivos encontrados: {len(sessions)}")

slug_data: dict = {}

for lp in sessions:
    rows = []
    for line in lp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass

    snap_by_slug: dict = defaultdict(list)
    for r in rows:
        if r.get("type") != "snapshot":
            continue
        sl = r.get("current_slug", "")
        if sl:
            snap_by_slug[sl].append(r)

    for sl, snaps in snap_by_slug.items():
        if sl in slug_data:
            continue
        snaps_s = sorted(snaps, key=lambda s: s.get("ts", 0))

        entry_snap = None
        entry_idx  = 0
        for i, s in enumerate(snaps_s):
            secs = s.get("current_secs")
            ex   = s.get("current_exec", {})
            el   = s.get("early_leader", {})
            side = el.get("early_side")
            if not side or secs is None:
                continue
            el_bid = ex.get("up_bid", 0) if side == "UP" else ex.get("down_bid", 0)
            if 30 <= secs <= 180 and EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI:
                entry_snap = s
                entry_idx  = i
                break

        if not entry_snap:
            continue

        el_d  = entry_snap.get("early_leader", {})
        ex_d  = entry_snap.get("current_exec", {})
        side  = el_d.get("early_side", "")
        el_bid = ex_d.get("up_bid", 0) if side == "UP" else ex_d.get("down_bid", 0)

        sig_ok  = el_d.get("signal_ok", False)
        cont_ok = el_d.get("f3_ok", False)
        el_vel  = el_d.get("el_vel", 0)

        if _args.cat == "A" and not sig_ok:
            continue
        if _args.cat == "AB" and not (sig_ok or cont_ok):
            continue
        # ALL: aceita qualquer um com early_side

        if _args.cat == "A":
            sig_cat = "A_signal_ok"
        elif cont_ok:
            sig_cat = "A_signal_ok" if sig_ok else "B_cont_only"
        else:
            sig_cat = "C_el_only"

        # Snaps ANTES da entrada (para metricas de estabilidade)
        snaps_before = []
        for s in snaps_s[:entry_idx]:
            secs2 = s.get("current_secs")
            ex2   = s.get("current_exec", {})
            if s.get("current_slug") != sl or secs2 is None:
                continue
            snaps_before.append({
                "ts":     s.get("ts", 0),
                "secs":   secs2,
                "up_bid": ex2.get("up_bid", 0),
                "dn_bid": ex2.get("down_bid", 0),
            })

        # Snaps DEPOIS da entrada (para simular saida)
        snaps_after = []
        for s in snaps_s[entry_idx + 1:]:
            secs2 = s.get("current_secs")
            ex2   = s.get("current_exec", {})
            if s.get("current_slug") != sl or secs2 is None:
                continue
            snaps_after.append({
                "secs":   secs2,
                "up_bid": ex2.get("up_bid", 0),
                "dn_bid": ex2.get("down_bid", 0),
            })

        entry_ts = entry_snap.get("ts", 0)
        stab = _stability(snaps_before, entry_ts, round(el_bid, 4), side)

        slug_data[sl] = {
            "slug":     sl,
            "ts":       entry_ts,
            "ep":       round(el_bid, 4),
            "side":     side,
            "secs":     entry_snap.get("current_secs"),
            "el_vel":   el_vel,
            "sig_ok":   sig_ok,
            "cont_ok":  cont_ok,
            "sig_cat":  sig_cat,
            "snaps_after":  snaps_after,
            **stab,
        }

print(f"  Candidatos: {len(slug_data)}")

# ---------------------------------------------------------------------------
# 2. Simular saidas
# ---------------------------------------------------------------------------
for r in slug_data.values():
    outcome, pnl = _simulate_exit(r["snaps_after"], r["ep"], r["side"])
    r["outcome"] = outcome
    r["pnl"]     = pnl

valid = [r for r in slug_data.values() if r["outcome"] not in ("MISSED", None)]
print(f"  Simulados com outcome: {len(valid)}\n")

# ---------------------------------------------------------------------------
# helper stats
# ---------------------------------------------------------------------------
def _row(label, grp, width=38):
    if not grp:
        return
    n     = len(grp)
    wins  = sum(1 for r in grp if r["pnl"] > 0)
    stops = sum(1 for r in grp if r["outcome"] == "STOP_LOSS")
    pnl   = round(sum(r["pnl"] for r in grp), 2)
    print(f"  {label:<{width}}  {n:>4}  {wins/n:>7.1%}  {stops/n:>6.1%}  {pnl:>+9.2f}  {pnl/n:>+8.3f}")

HDR = f"  {'Cenario':<38}  {'n':>4}  {'WR':>7}  {'STOP%':>6}  {'PnL':>9}  {'avg':>8}"
SEP = "  " + "-" * 76

# ---------------------------------------------------------------------------
# 3. el_bid_delta_30s — ainda subindo vs esfriando na hora de entrar
# ---------------------------------------------------------------------------
has_d30 = [r for r in valid if r.get("el_bid_delta_30s") is not None]
print(f"{'='*80}")
print(f"  EL BID DELTA 30s — bid ainda subindo (pos) vs esfriando/caidno (neg/zero)")
print(f"  Universo com dados: {len(has_d30)}/{len(valid)}")
print(f"{'='*80}")
print(HDR); print(SEP)

bins_d30 = [
    ("delta_30s < -0.04  (bid caiu)",     lambda r: r["el_bid_delta_30s"] < -0.04),
    ("delta_30s -0.04..0 (estavel/cedo)", lambda r: -0.04 <= r["el_bid_delta_30s"] < 0),
    ("delta_30s == 0     (sem snap ant)", lambda r: r["el_bid_delta_30s"] == 0),
    ("delta_30s 0..0.02  (subiu pouco)",  lambda r: 0 < r["el_bid_delta_30s"] <= 0.02),
    ("delta_30s 0.02..0.05 (subindo)",    lambda r: 0.02 < r["el_bid_delta_30s"] <= 0.05),
    ("delta_30s > 0.05   (subindo rapido)", lambda r: r["el_bid_delta_30s"] > 0.05),
]
for label, fn in bins_d30:
    _row(label, [r for r in has_d30 if fn(r)])

print()
print(f"  --- por sinal binario (entry bid subindo vs nao-subindo) ---")
print(HDR); print(SEP)
rising = [r for r in has_d30 if r["el_bid_delta_30s"] > 0]
cool   = [r for r in has_d30 if r["el_bid_delta_30s"] <= 0]
_row("bid_subindo_30s (delta > 0)",   rising)
_row("bid_esfriado_30s (delta <= 0)", cool)
_row("base (todos com dados)",        has_d30)

# delta 60s
has_d60 = [r for r in valid if r.get("el_bid_delta_60s") is not None]
print(f"\n  --- el_bid_delta_60s (n={len(has_d60)}) ---")
print(HDR); print(SEP)
_row("bid_subindo_60s (delta > 0)",   [r for r in has_d60 if r["el_bid_delta_60s"] > 0])
_row("bid_esfriado_60s (delta <= 0)", [r for r in has_d60 if r["el_bid_delta_60s"] <= 0])

# ---------------------------------------------------------------------------
# 4. el_bid_range_30s — amplitude do bid antes de entrar
# ---------------------------------------------------------------------------
has_rng = [r for r in valid if r.get("el_bid_range_30s") is not None]
print(f"\n{'='*80}")
print(f"  EL BID RANGE 30s — amplitude do bid Polymarket antes de entrar")
print(f"  Universo com dados: {len(has_rng)}/{len(valid)}")
print(f"{'='*80}")
print(HDR); print(SEP)

bins_rng = [
    ("range_30s == 0     (bid fixo)",      lambda r: r["el_bid_range_30s"] == 0),
    ("range_30s 0..0.02  (muito estavel)", lambda r: 0 < r["el_bid_range_30s"] <= 0.02),
    ("range_30s 0.02..0.04 (leve oscil)", lambda r: 0.02 < r["el_bid_range_30s"] <= 0.04),
    ("range_30s 0.04..0.06 (oscilando)",  lambda r: 0.04 < r["el_bid_range_30s"] <= 0.06),
    ("range_30s > 0.06   (muito oscil)",  lambda r: r["el_bid_range_30s"] > 0.06),
]
for label, fn in bins_rng:
    _row(label, [r for r in has_rng if fn(r)])

# gate candidato
print()
print(HDR); print(SEP)
for gate in [0.02, 0.04, 0.06, 0.08]:
    _row(f"range_30s < {gate}", [r for r in has_rng if r["el_bid_range_30s"] < gate])
_row("base (todos com dados)", has_rng)

# ---------------------------------------------------------------------------
# 5. mono_ratio_30s — movimento monotônico = "chasing"?
# ---------------------------------------------------------------------------
has_mono = [r for r in valid if r.get("mono_ratio_30s") is not None]
print(f"\n{'='*80}")
print(f"  MONO_RATIO 30s — fracao de movimentos na mesma direcao")
print(f"  Universo com dados: {len(has_mono)}/{len(valid)}")
print(f"{'='*80}")
print(HDR); print(SEP)

bins_mono = [
    ("mono <= 0.50 (alternando)",   lambda r: r["mono_ratio_30s"] <= 0.50),
    ("mono 0.50..0.67 (leve mono)", lambda r: 0.50 < r["mono_ratio_30s"] <= 0.67),
    ("mono 0.67..0.80 (mono med)",  lambda r: 0.67 < r["mono_ratio_30s"] <= 0.80),
    ("mono > 0.80   (chasing?)",    lambda r: r["mono_ratio_30s"] > 0.80),
]
for label, fn in bins_mono:
    _row(label, [r for r in has_mono if fn(r)])

print()
print(HDR); print(SEP)
_row("mono_up <= 0.67",  [r for r in has_mono if r["mono_ratio_30s"] <= 0.67])
_row("mono_up > 0.67",   [r for r in has_mono if r["mono_ratio_30s"] > 0.67])
_row("base",             has_mono)

# ---------------------------------------------------------------------------
# 6. snaps_in_range / time_in_range — sinal maduro
# ---------------------------------------------------------------------------
print(f"\n{'='*80}")
print(f"  SNAPS EM [0.82,0.86] ANTES DE ENTRAR — sinal maduro vs recente")
print(f"{'='*80}")
print(HDR); print(SEP)

bins_sir = [
    ("0 snaps antes (1o snap do range)", lambda r: r["snaps_in_range"] == 0),
    ("1..2 snaps antes",                 lambda r: 1 <= r["snaps_in_range"] <= 2),
    ("3..5 snaps antes",                 lambda r: 3 <= r["snaps_in_range"] <= 5),
    ("6..10 snaps antes",                lambda r: 6 <= r["snaps_in_range"] <= 10),
    (">10 snaps antes (bid consolidado)",lambda r: r["snaps_in_range"] > 10),
]
for label, fn in bins_sir:
    _row(label, [r for r in valid if fn(r)])

print()
print(f"  --- por tempo em range (seconds) ---")
print(HDR); print(SEP)
bins_tir = [
    ("0s (sem snap no range antes)",  lambda r: r["time_in_range_s"] == 0),
    ("1..10s em range",               lambda r: 0 < r["time_in_range_s"] <= 10),
    ("10..30s em range",              lambda r: 10 < r["time_in_range_s"] <= 30),
    ("30..60s em range",              lambda r: 30 < r["time_in_range_s"] <= 60),
    (">60s em range (consolidado)",   lambda r: r["time_in_range_s"] > 60),
]
for label, fn in bins_tir:
    _row(label, [r for r in valid if fn(r)])

# ---------------------------------------------------------------------------
# 7. opp_delta_30s — counter crescendo?
# ---------------------------------------------------------------------------
has_opp = [r for r in valid if r.get("opp_delta_30s") is not None]
print(f"\n{'='*80}")
print(f"  OPP_DELTA 30s — counter bid subindo vs caindo antes de entrar")
print(f"  Universo com dados: {len(has_opp)}/{len(valid)}")
print(f"{'='*80}")
print(HDR); print(SEP)

_row("opp caindo (delta < -0.01)",   [r for r in has_opp if r["opp_delta_30s"] < -0.01])
_row("opp estavel (-0.01..0.01)",    [r for r in has_opp if abs(r["opp_delta_30s"]) <= 0.01])
_row("opp subindo (delta > 0.01)",   [r for r in has_opp if r["opp_delta_30s"] > 0.01])
_row("base",                         has_opp)

# ---------------------------------------------------------------------------
# 8. Combinacoes — melhores filtros juntos
# ---------------------------------------------------------------------------
print(f"\n{'='*80}")
print(f"  COMBINACOES — melhores filtros identificados")
print(f"{'='*80}")
print(HDR); print(SEP)

combos = [
    ("base",
     lambda r: True),
    ("bid_esfriado (delta_30s<=0)",
     lambda r: r.get("el_bid_delta_30s") is not None and r["el_bid_delta_30s"] <= 0),
    ("bid_esfriado + el_vel>=0.08",
     lambda r: r.get("el_bid_delta_30s") is not None and r["el_bid_delta_30s"] <= 0 and r["el_vel"] >= 0.08),
    ("bid_esfriado + el_vel>=0.13",
     lambda r: r.get("el_bid_delta_30s") is not None and r["el_bid_delta_30s"] <= 0 and r["el_vel"] >= 0.13),
    ("range_30s<=0.04 (bid estavel)",
     lambda r: r.get("el_bid_range_30s") is not None and r["el_bid_range_30s"] <= 0.04),
    ("range_30s<=0.04 + el_vel>=0.08",
     lambda r: r.get("el_bid_range_30s") is not None and r["el_bid_range_30s"] <= 0.04 and r["el_vel"] >= 0.08),
    (">=3 snaps no range antes",
     lambda r: r["snaps_in_range"] >= 3),
    (">=3 snaps + el_vel>=0.08",
     lambda r: r["snaps_in_range"] >= 3 and r["el_vel"] >= 0.08),
    ("bid_sub + range<=0.04 + el_vel>=0.08",
     lambda r: r.get("el_bid_delta_30s") is not None and r["el_bid_delta_30s"] <= 0
               and r.get("el_bid_range_30s") is not None and r["el_bid_range_30s"] <= 0.04
               and r["el_vel"] >= 0.08),
    ("bid_sub + el_vel>=0.13",
     lambda r: r.get("el_bid_delta_30s") is not None and r["el_bid_delta_30s"] <= 0
               and r["el_vel"] >= 0.13),
]
for label, fn in combos:
    _row(label, [r for r in valid if fn(r)])

# ---------------------------------------------------------------------------
# 9. Detalhe dos STOP_LOSS — o que as metricas mostram
# ---------------------------------------------------------------------------
stops = [r for r in valid if r["outcome"] == "STOP_LOSS"]
wins  = [r for r in valid if r["outcome"] != "STOP_LOSS"]
print(f"\n{'='*80}")
print(f"  MEDIAS DAS METRICAS: STOP_LOSS vs NAO-STOP  (n_stop={len(stops)}, n_win={len(wins)})")
print(f"{'='*80}")

def _avg(lst, key):
    vals = [x[key] for x in lst if x.get(key) is not None]
    return f"{sum(vals)/len(vals):>+7.3f} (n={len(vals)})" if vals else "     n/a"

metrics = [
    ("el_bid_delta_30s", "bid subindo 30s (pos=subindo)"),
    ("el_bid_delta_60s", "bid subindo 60s"),
    ("el_bid_range_30s", "range bid 30s  (vol Polymarket)"),
    ("mono_ratio_30s",   "mono_ratio 30s (1=puro mono)"),
    ("snaps_in_range",   "snaps em range antes"),
    ("time_in_range_s",  "segundos em range antes"),
    ("opp_delta_30s",    "opp_delta 30s  (pos=counter sobe)"),
    ("el_vel",           "el_vel"),
]
print(f"  {'Metrica':<28}  {'STOP_LOSS':>22}  {'nao-STOP':>22}  diff")
print("  " + "-"*80)
for key, label in metrics:
    sv = [x[key] for x in stops if x.get(key) is not None]
    wv = [x[key] for x in wins  if x.get(key) is not None]
    if not sv or not wv:
        continue
    sa = sum(sv)/len(sv)
    wa = sum(wv)/len(wv)
    print(f"  {label:<28}  {sa:>+8.4f} (n={len(sv):>3})  "
          f"{wa:>+8.4f} (n={len(wv):>3})  {sa-wa:>+7.4f}")
