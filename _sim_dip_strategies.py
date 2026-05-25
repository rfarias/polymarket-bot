"""
_sim_dip_strategies.py
Compara dois modos de aplicar o gate de dip entry (el_bid_delta_30s < threshold):

  ATUAL   : entra no primeiro snap com signal_ok + el_bid em [0.82,0.86]
  BLOQUEAR: so entra se delta_30s < threshold NAQUELE snap; senao, nao entra
  ESPERAR : apos signal_ok, continua monitorando ate delta_30s < threshold
             ou o tempo esgotar (secs < 30)

A diferenca entre BLOQUEAR e ESPERAR:
  - BLOQUEAR perde todos os trades em que o dip nao ocorreu no primeiro snap
  - ESPERAR captura trades adicionais onde o dip ocorre depois (possivelmente
    com ep diferente e menos tempo restante)

Uso:
    python _sim_dip_strategies.py
    python _sim_dip_strategies.py --logs "test_data/ee_paper/**/*.jsonl"
    python _sim_dip_strategies.py --threshold -0.04
"""
import json, argparse
from pathlib import Path
from collections import defaultdict, Counter

QTY           = 6.0
EE_STOP       = 0.65
EE_PP_BID     = 0.88
EE_PP_SECS_LO = 36
EE_PP_SECS_HI = 70
EE_WIN_SECS   = 35
EE_ENTRY_LO   = 0.82
EE_ENTRY_HI   = 0.86

_ap = argparse.ArgumentParser()
_ap.add_argument('--logs', default='logs/ee_paper_*/ee_paper.jsonl')
_ap.add_argument('--threshold', type=float, default=-0.04,
                 help='Delta_30s minimo para o dip (default: -0.04)')
_ap.add_argument('--cat', default='A',
                 help='A=signal_ok, AB=signal_ok+cont_only, ALL')
_args = _ap.parse_args()
THR = _args.threshold

# ---------------------------------------------------------------------------
def _el(s, side):  return s["up_bid"] if side == "UP" else s["dn_bid"]
def _opp(s, side): return s["dn_bid"] if side == "UP" else s["up_bid"]

def _simulate_exit(snaps_after, ep, side):
    for s in snaps_after:
        secs    = s["secs"]
        el_bid  = _el(s, side)
        opp_bid = _opp(s, side)
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

def _delta30(snap_seq, i, side):
    """delta_30s no snap i: el_bid[i] - el_bid_oldest_dentro_30s."""
    ts_i    = snap_seq[i]["ts"]
    el_now  = _el(snap_seq[i], side)
    w30 = [s for s in snap_seq[:i]
           if ts_i - s["ts"] <= 30 and _el(s, side) > 0]
    if not w30:
        return None
    return round(el_now - _el(w30[0], side), 4)

# ---------------------------------------------------------------------------
# Carregar snapshots
# ---------------------------------------------------------------------------
print(f"\nCarregando: {_args.logs}")
sessions = sorted(Path(".").glob(_args.logs))
print(f"  Arquivos: {len(sessions)}")

results = {}   # slug -> dict com as 3 estrategias

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
        if sl in results: continue
        snaps_s = sorted(snaps, key=lambda s: s.get("ts", 0))

        # Construir seq normalizada com campos necessarios
        seq = []
        for s in snaps_s:
            sc  = s.get("current_secs")
            ex  = s.get("current_exec", {})
            el  = s.get("early_leader", {})
            if s.get("current_slug") != sl or sc is None: continue
            seq.append({
                "ts":     s.get("ts", 0),
                "secs":   sc,
                "up_bid": ex.get("up_bid", 0),
                "dn_bid": ex.get("down_bid", 0),
                "sig_ok": el.get("signal_ok", False),
                "cont_ok":el.get("f3_ok", False),
                "el_vel": el.get("el_vel", 0),
                "side":   el.get("early_side", ""),
            })

        # Encontrar primeiro snap elegivel (signal_ok + el_bid em range + secs ok)
        first_idx = None
        for i, s in enumerate(seq):
            side = s["side"]
            if not side: continue
            if _args.cat == "A"  and not s["sig_ok"]:             continue
            if _args.cat == "AB" and not (s["sig_ok"] or s["cont_ok"]): continue
            el_bid = _el(s, side)
            if 30 <= s["secs"] <= 180 and EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI:
                first_idx = i
                break

        if first_idx is None: continue

        side    = seq[first_idx]["side"]
        sig_cat = ("A_signal_ok" if seq[first_idx]["sig_ok"]
                   else "B_cont_only" if seq[first_idx]["cont_ok"]
                   else "C_el_only")
        el_vel  = seq[first_idx]["el_vel"]

        # ---------- ESTRATEGIA 1: ATUAL (entra no primeiro snap) ----------
        ep_atual = round(_el(seq[first_idx], side), 4)
        secs_atual = seq[first_idx]["secs"]
        after_atual = [{"secs": s["secs"], "up_bid": s["up_bid"],
                        "dn_bid": s["dn_bid"]} for s in seq[first_idx+1:]]
        out_atual, pnl_atual = _simulate_exit(after_atual, ep_atual, side)

        # delta_30s no primeiro snap
        d30_first = _delta30(seq, first_idx, side)

        # ---------- ESTRATEGIA 2: BLOQUEAR ----------
        if d30_first is not None and d30_first < THR:
            # dip ja presente no primeiro snap → entra igual ao atual
            out_blk, pnl_blk = out_atual, pnl_atual
            ep_blk   = ep_atual
            secs_blk = secs_atual
            blk_mode = "entra_imediato"
        else:
            out_blk, pnl_blk = "BLOQUEADO", 0.0
            ep_blk   = None
            secs_blk = None
            blk_mode = "bloqueado"

        # ---------- ESTRATEGIA 3: ESPERAR ----------
        # A partir do primeiro snap elegivel, continua ate dip ocorrer ou tempo acabar
        wait_idx  = None
        ep_wait   = None
        secs_wait = None
        for i in range(first_idx, len(seq)):
            s    = seq[i]
            side_i = s["side"]
            if not side_i: break

            # Verificar condicoes de entrada ainda validas
            if _args.cat == "A" and not s["sig_ok"]: break
            if s["secs"] < 30: break

            el_bid = _el(s, side)
            # bid saiu abaixo da faixa → para de esperar
            if el_bid < EE_ENTRY_LO: break
            # bid acima da faixa → continua monitorando (pode voltar)
            if el_bid > EE_ENTRY_HI: continue

            # bid em range: calcular delta_30s
            d30 = _delta30(seq, i, side)
            if d30 is None:
                # sem snaps anteriores suficientes: considera 0 (sem pullback)
                continue
            if d30 < THR:
                wait_idx  = i
                ep_wait   = round(el_bid, 4)
                secs_wait = s["secs"]
                break

        if wait_idx is not None:
            after_wait = [{"secs": s["secs"], "up_bid": s["up_bid"],
                           "dn_bid": s["dn_bid"]} for s in seq[wait_idx+1:]]
            out_wait, pnl_wait = _simulate_exit(after_wait, ep_wait, side)
            wait_mode = ("entra_imediato" if wait_idx == first_idx else
                         f"espera_{seq[wait_idx]['secs']}s")
        else:
            out_wait, pnl_wait = "PERDIDO_WAIT", 0.0
            wait_mode = "nunca_dip"

        results[sl] = {
            "slug":     sl, "side": side, "sig_cat": sig_cat, "el_vel": el_vel,
            "d30_first": d30_first,
            # atual
            "ep_atual":   ep_atual, "secs_atual":   secs_atual,
            "out_atual":  out_atual, "pnl_atual":   pnl_atual,
            # bloquear
            "ep_blk":     ep_blk,   "secs_blk":     secs_blk,
            "out_blk":    out_blk,  "pnl_blk":      pnl_blk, "blk_mode": blk_mode,
            # esperar
            "ep_wait":    ep_wait,  "secs_wait":    secs_wait,
            "out_wait":   out_wait, "pnl_wait":     pnl_wait, "wait_mode": wait_mode,
        }

print(f"  Slugs processados: {len(results)}")

# Filtrar MISSED da estrategia atual (sem resolucao nos snaps)
valid = [r for r in results.values() if r["out_atual"] not in ("MISSED", None)]
print(f"  Com outcome (atual): {len(valid)}\n")

# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------
def _show(label, grp, w=40):
    taken  = [r for r in grp if r not in ("BLOQUEADO", "PERDIDO_WAIT", "MISSED")]
    if not grp: return
    pnl_all = sum(r["pnl"] for r in grp)   # inclui 0 dos bloqueados/perdidos
    taken_pnl = [r["pnl"] for r in grp if r["pnl"] != 0 or r["outcome"] not in ("BLOQUEADO","PERDIDO_WAIT","MISSED")]
    n   = len(grp)
    nt  = sum(1 for r in grp if r["outcome"] not in ("BLOQUEADO","PERDIDO_WAIT","MISSED", None))
    ws  = sum(1 for r in grp if r["pnl"] > 0)
    stp = sum(1 for r in grp if r["outcome"] == "STOP_LOSS")
    avg_taken = pnl_all / nt if nt else 0
    avg_all   = pnl_all / n  if n else 0
    print(f"  {label:<{w}}  {n:>4}  {nt:>5}  {ws/nt:>7.1%}  {stp/nt:>6.1%}  "
          f"{pnl_all:>+9.2f}  {avg_taken:>+8.3f}  {avg_all:>+8.3f}"
          if nt else
          f"  {label:<{w}}  {n:>4}  {nt:>5}  {'n/a':>7}  {'n/a':>6}  "
          f"  {'n/a':>9}  {'n/a':>8}  {'n/a':>8}")

HDR = (f"  {'Estrategia':<40}  {'N':>4}  {'tomad':>5}  {'WR':>7}  "
       f"{'STOP%':>6}  {'PnL_all':>9}  {'avg_trad':>8}  {'avg_all':>8}")
SEP = "  " + "-" * 86

# ---------------------------------------------------------------------------
# FUNCAO AUXILIAR: montar grp com campos de outcome/pnl padronizados
# ---------------------------------------------------------------------------
def _grp(valid, out_key, pnl_key):
    return [{"outcome": r[out_key], "pnl": r[pnl_key], **r} for r in valid]

def _print_strat(label, grp):
    n    = len(grp)
    taken = [r for r in grp if r["outcome"] not in ("BLOQUEADO","PERDIDO_WAIT","MISSED")]
    nt   = len(taken)
    if nt == 0:
        print(f"  {label:<40}  {n:>4}  {nt:>5}  {'n/a':>7}  {'n/a':>6}  "
              f"  {'n/a':>9}  {'n/a':>8}  {'n/a':>8}")
        return
    ws   = sum(1 for r in taken if r["pnl"] > 0)
    stp  = sum(1 for r in taken if r["outcome"] == "STOP_LOSS")
    pnl_all  = round(sum(r["pnl"] for r in grp), 2)   # bloqueado/perdido = 0
    avg_trad = round(pnl_all / nt, 3)
    avg_all  = round(pnl_all / n, 3)
    print(f"  {label:<40}  {n:>4}  {nt:>5}  {ws/nt:>7.1%}  {stp/nt:>6.1%}  "
          f"{pnl_all:>+9.2f}  {avg_trad:>+8.3f}  {avg_all:>+8.3f}")

# ---------------------------------------------------------------------------
# 1. RESULTADOS GLOBAIS — 3 estrategias
# ---------------------------------------------------------------------------
print(f"{'='*88}")
print(f"  COMPARACAO DAS 3 ESTRATEGIAS  (threshold dip = {THR:+.2f})")
print(f"{'='*88}")
print(f"  {'Estrategia':<40}  {'N':>4}  {'tomad':>5}  {'WR':>7}  {'STOP%':>6}  "
      f"{'PnL_all':>9}  {'avg_tomad':>9}  {'avg_all':>8}")
print(f"  (avg_all considera bloqueados/perdidos como pnl=0)")
print(SEP)

atual_g = [{"outcome": r["out_atual"], "pnl": r["pnl_atual"]} for r in valid]
blk_g   = [{"outcome": r["out_blk"],   "pnl": r["pnl_blk"]}  for r in valid]
wait_g  = [{"outcome": r["out_wait"],  "pnl": r["pnl_wait"]} for r in valid]

_print_strat("ATUAL (1o snap signal_ok em range)",  [{"outcome":r["out_atual"],"pnl":r["pnl_atual"]} for r in valid])
_print_strat("BLOQUEAR (so entra em dip)",          [{"outcome":r["out_blk"], "pnl":r["pnl_blk"]}  for r in valid])
_print_strat("ESPERAR  (aguarda dip ate secs=30)",  [{"outcome":r["out_wait"],"pnl":r["pnl_wait"]} for r in valid])

# ---------------------------------------------------------------------------
# 2. O QUE ACONTECE COM OS BLOQUEADOS/PERDIDOS
# ---------------------------------------------------------------------------
bloqueados = [r for r in valid if r["out_blk"] == "BLOQUEADO"]
perdidos   = [r for r in valid if r["out_wait"] == "PERDIDO_WAIT"]
dip_found  = [r for r in valid if r["out_wait"] not in ("PERDIDO_WAIT", "MISSED")]
dip_imm    = [r for r in valid if r["wait_mode"] == "entra_imediato"]
dip_later  = [r for r in valid if r["wait_mode"] not in ("entra_imediato","nunca_dip")]

print(f"\n{'='*88}")
print(f"  DETALHE: O QUE ACONTECE COM CADA GRUPO")
print(f"{'='*88}")
print(f"\n  Distribuicao geral (universo = {len(valid)} trades atuais):")
print(f"    Dip ja presente no 1o snap (bloquear passaria): {len(valid)-len(bloqueados):>4}  ({(len(valid)-len(bloqueados))/len(valid):.1%})")
print(f"    Sem dip no 1o snap (bloquear bloquearia)      : {len(bloqueados):>4}  ({len(bloqueados)/len(valid):.1%})")
print(f"      -> Dip aparece depois (esperar captura)     : {len(dip_later):>4}  ({len(dip_later)/len(valid):.1%})")
print(f"      -> Dip nunca aparece (ambos perdem)         : {len(perdidos):>4}  ({len(perdidos)/len(valid):.1%})")

# Como iam os bloqueados se fossem tomados (resultado atual deles)
if bloqueados:
    b_wins  = sum(1 for r in bloqueados if r["pnl_atual"] > 0)
    b_stops = sum(1 for r in bloqueados if r["out_atual"] == "STOP_LOSS")
    b_pnl   = sum(r["pnl_atual"] for r in bloqueados)
    print(f"\n  Trades que BLOQUEAR perderia (resultado se fossem tomados):")
    print(f"    n={len(bloqueados)}  WR={b_wins/len(bloqueados):.1%}  "
          f"STOP={b_stops/len(bloqueados):.1%}  PnL={b_pnl:+.2f}  avg={b_pnl/len(bloqueados):+.3f}")

# Como vao os que ESPERAR captura depois do 1o snap
if dip_later:
    w_wins  = sum(1 for r in dip_later if r["pnl_wait"] > 0)
    w_stops = sum(1 for r in dip_later if r["out_wait"] == "STOP_LOSS")
    w_pnl   = sum(r["pnl_wait"] for r in dip_later)
    ep_delta = [(r["ep_wait"] - r["ep_atual"]) for r in dip_later]
    secs_delta = [(r["secs_wait"] - r["secs_atual"]) for r in dip_later if r["secs_wait"] and r["secs_atual"]]
    print(f"\n  Trades que ESPERAR captura apos o 1o snap:")
    print(f"    n={len(dip_later)}  WR={w_wins/len(dip_later):.1%}  "
          f"STOP={w_stops/len(dip_later):.1%}  PnL={w_pnl:+.2f}  avg={w_pnl/len(dip_later):+.3f}")
    if ep_delta:
        print(f"    ep vs 1o snap: avg={sum(ep_delta)/len(ep_delta):+.4f}  "
              f"min={min(ep_delta):+.4f}  max={max(ep_delta):+.4f}")
    if secs_delta:
        print(f"    secs vs 1o snap: avg={sum(secs_delta)/len(secs_delta):+.1f}  "
              f"min={min(secs_delta):+.0f}  max={max(secs_delta):+.0f}")

# Trades que nenhuma estrategia captura
if perdidos:
    p_wins  = sum(1 for r in perdidos if r["pnl_atual"] > 0)
    p_stops = sum(1 for r in perdidos if r["out_atual"] == "STOP_LOSS")
    p_pnl   = sum(r["pnl_atual"] for r in perdidos)
    print(f"\n  Trades que ambas as estrategias PERDEM (dip nao aparece):")
    print(f"    n={len(perdidos)}  WR={p_wins/len(perdidos):.1%}  "
          f"STOP={p_stops/len(perdidos):.1%}  PnL={p_pnl:+.2f}  avg={p_pnl/len(perdidos):+.3f}")

# ---------------------------------------------------------------------------
# 3. POR CATEGORIA DE SINAL
# ---------------------------------------------------------------------------
print(f"\n{'='*88}")
print(f"  POR CATEGORIA DE SINAL")
print(f"{'='*88}")
print(f"  {'Categoria/Estrategia':<40}  {'N':>4}  {'tomad':>5}  {'WR':>7}  "
      f"{'STOP%':>6}  {'PnL_all':>9}  {'avg_tomad':>9}  {'avg_all':>8}")
print(SEP)

for cat in ["A_signal_ok", "B_cont_only", "C_el_only"]:
    sub = [r for r in valid if r["sig_cat"] == cat]
    if not sub: continue
    print(f"\n  [{cat}]")
    _print_strat("  atual", [{"outcome":r["out_atual"],"pnl":r["pnl_atual"]} for r in sub])
    _print_strat("  bloquear", [{"outcome":r["out_blk"], "pnl":r["pnl_blk"]}  for r in sub])
    _print_strat("  esperar",  [{"outcome":r["out_wait"],"pnl":r["pnl_wait"]} for r in sub])

# ---------------------------------------------------------------------------
# 4. CURVA DE THRESHOLDS: impacto de diferentes valores de THR
# ---------------------------------------------------------------------------
print(f"\n{'='*88}")
print(f"  CURVA DE THRESHOLDS — BLOQUEAR com diferentes valores de dip")
print(f"{'='*88}")
print(f"  {'threshold':<12}  {'n_entra':>7}  {'n_blk':>6}  {'WR':>7}  "
      f"{'STOP%':>6}  {'PnL':>9}  {'avg_tomad':>9}  {'avg_all':>8}")
print("  " + "-"*80)

for thr in [-0.08, -0.06, -0.04, -0.03, -0.02, -0.01, 0.00, 0.02, 0.05]:
    sub_pass = []
    sub_blk  = []
    for r in valid:
        d = r["d30_first"]
        if d is not None and d < thr:
            sub_pass.append(r)
        else:
            sub_blk.append(r)
    n   = len(valid)
    nt  = len(sub_pass)
    if nt == 0:
        print(f"  {thr:>+12.2f}  {0:>7}  {n:>6}  {'n/a':>7}  {'n/a':>6}  {'n/a':>9}  {'n/a':>9}  {'n/a':>8}")
        continue
    ws  = sum(1 for r in sub_pass if r["pnl_atual"] > 0)
    stp = sum(1 for r in sub_pass if r["out_atual"] == "STOP_LOSS")
    pnl = round(sum(r["pnl_atual"] for r in sub_pass), 2)
    print(f"  {thr:>+12.2f}  {nt:>7}  {n-nt:>6}  {ws/nt:>7.1%}  "
          f"{stp/nt:>6.1%}  {pnl:>+9.2f}  {pnl/nt:>+9.3f}  {pnl/n:>+8.3f}")

# linha base
nt  = len(valid)
ws  = sum(1 for r in valid if r["pnl_atual"] > 0)
stp = sum(1 for r in valid if r["out_atual"] == "STOP_LOSS")
pnl = round(sum(r["pnl_atual"] for r in valid), 2)
print(f"  {'base':>12}  {nt:>7}  {0:>6}  {ws/nt:>7.1%}  "
      f"{stp/nt:>6.1%}  {pnl:>+9.2f}  {pnl/nt:>+9.3f}  {pnl/nt:>+8.3f}")

# ---------------------------------------------------------------------------
# 5. CURVA DE THRESHOLDS: ESPERAR
# ---------------------------------------------------------------------------
print(f"\n{'='*88}")
print(f"  CURVA DE THRESHOLDS — ESPERAR com diferentes valores de dip")
print(f"{'='*88}")
print(f"  (recalcula esperar para cada threshold — pode demorar alguns segundos)")

# Para a curva de esperar, preciso recalcular o wait para cada threshold
# Mas ja tenho o slug_data com seq completa — nao, ja descartei
# Vou re-usar a informacao que ja temos: d30_first e out/pnl atuais
# Para esperar, a recalculacao completa seria necessaria, mas posso aproximar:
# - Se d30_first < thr: entra imediatamente (mesma coisa que bloquear/atual)
# - Se d30_first >= thr: resultado do esperar eh out_wait/pnl_wait (calculado com THR original)
# Isso nao e' exato para thresholds diferentes, mas da uma ideia

print(f"  (NOTA: resultado esperar calculado com threshold original {THR:+.2f})")
print(f"  {'threshold':>12}  {'tomad':>7}  {'perdid':>7}  {'WR':>7}  "
      f"{'STOP%':>6}  {'PnL':>9}  {'avg_tomad':>9}  {'avg_all':>8}")
print("  " + "-"*80)

for thr in [-0.08, -0.06, -0.04, -0.03, -0.02, -0.01, 0.00, 0.02, 0.05]:
    taken = []
    for r in valid:
        d = r["d30_first"]
        if d is not None and d < thr:
            # dip ja no 1o snap: entra com resultado atual
            taken.append(r["pnl_atual"])
        elif r["out_wait"] not in ("PERDIDO_WAIT", "MISSED"):
            # esperar capturou depois: usa resultado do wait
            taken.append(r["pnl_wait"])
        # else: perdido = 0
    n   = len(valid)
    nt  = len(taken)
    pnl = round(sum(taken), 2) if taken else 0
    # WR e STOP aproximados — precisaria de mais info
    print(f"  {thr:>+12.2f}  {nt:>7}  {n-nt:>7}  {'---':>7}  "
          f"{'---':>6}  {pnl:>+9.2f}  {pnl/nt if nt else 0:>+9.3f}  {pnl/n:>+8.3f}")

# ---------------------------------------------------------------------------
# 6. RESUMO FINAL
# ---------------------------------------------------------------------------
print(f"\n{'='*88}")
print(f"  RESUMO — o que cada estrategia entrega vs base  (threshold={THR:+.2f})")
print(f"{'='*88}")

def _summarize(nome, out_key, pnl_key):
    taken  = [r for r in valid if r[out_key] not in ("BLOQUEADO","PERDIDO_WAIT","MISSED")]
    skipped = len(valid) - len(taken)
    if not taken:
        print(f"  {nome}: nenhum trade tomado"); return
    nt   = len(taken)
    ws   = sum(1 for r in taken if r[pnl_key] > 0)
    stp  = sum(1 for r in taken if r[out_key] == "STOP_LOSS")
    pnl  = round(sum(r[pnl_key] for r in valid), 2)
    print(f"  {nome}:")
    print(f"    Trades tomados: {nt}/{len(valid)} (perde {skipped})")
    print(f"    WR: {ws/nt:.1%}  STOP%: {stp/nt:.1%}")
    print(f"    PnL total: {pnl:+.2f}  avg/tomado: {pnl/nt:+.3f}  avg/oportunidade: {pnl/len(valid):+.3f}")

_summarize("ATUAL",    "out_atual", "pnl_atual")
_summarize("BLOQUEAR", "out_blk",   "pnl_blk")
_summarize("ESPERAR",  "out_wait",  "pnl_wait")
