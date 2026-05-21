"""
_sim_early_entry_v2.py

Análise da entrada antecipada (0.82-0.86) com EL estável, em duas dimensões:

BLOCO A — HEDGE / EXIT no cruzamento de 0.50
  Quando o winner bid cruza abaixo de 0.50, o mercado inverteu o consenso.
  Simular: sair no cruzamento de 0.50 vs hold to resolution.
  Também simular: comprar o lado oposto quando cruza 0.50 (hedge de posição).

BLOCO B — FILTROS ADICIONAIS DE CONTINUIDADE
  Quais variáveis no momento da entrada melhoram a probabilidade de WIN?
  Candidatos: market_range_15s, spot_delta_15s, velocidade do EL bid,
              spread, EL bid no momento da entrada, secs de entrada.
"""
import json, sys, io
from pathlib import Path
from collections import defaultdict
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

QTY = 6.0; TICK = 0.01
SEP = "─" * 70


# ─── Loader (reutilizado) ──────────────────────────────────────────────────────

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
                        sc = d; secs = d.get("secs")
                        ub = d.get("up_bid"); db = d.get("down_bid")
                        range15 = 0.0; d15 = d.get("sc_d15") or 0.0
                        spread = 0.0; slug = d.get("slug")
                    else:
                        if d.get("type") != "snapshot": continue
                        sc2 = d.get("scalp_context") or d.get("current_scalp_context") or {}
                        secs = sc2.get("secs_to_end") or d.get("current_secs")
                        ub = sc2.get("up_bid"); db = sc2.get("down_bid")
                        if ub is None:
                            lb = d.get("leader_buy"); cb = d.get("counter_buy")
                            side = (d.get("guardian") or {}).get("side") or \
                                   (d.get("almost_resolved_signal") or {}).get("side")
                            if lb is not None and cb is not None and side:
                                ub = lb if side == "UP" else cb
                                db = cb if side == "UP" else lb
                        range15 = sc2.get("market_range_15s") or 0.0
                        d15     = sc2.get("spot_delta_15s_bps") or 0.0
                        spread  = sc2.get("spread_up") or 0.0
                        slug    = d.get("slug") or d.get("current_slug")
                    if slug and secs is not None and ub is not None:
                        by_slug[slug].append({
                            "secs": int(secs), "up_bid": float(ub), "down_bid": float(db),
                            "ts": d["ts"], "range15": range15, "d15": d15, "spread": spread,
                        })
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
            el = None; cont_ok = False; el_bid_at_180 = 0.0; el_vel = 0.0
            if s240:
                avg_up = sum(s["up_bid"]   for s in s240) / len(s240)
                avg_dn = sum(s["down_bid"] for s in s240) / len(s240)
                if avg_up >= 0.55:   el = "UP";   el_bid_240 = avg_up
                elif avg_dn >= 0.55: el = "DOWN";  el_bid_240 = avg_dn
                else:                el_bid_240 = max(avg_up, avg_dn)
            else:
                el_bid_240 = 0.0
            if el and s180:
                bids180 = [s["up_bid"] if el == "UP" else s["down_bid"] for s in s180]
                min_bid = min(bids180); avg_bid = sum(bids180)/len(bids180)
                cont_ok = min_bid >= 0.70
                el_bid_at_180 = avg_bid
                # velocidade: variação do EL bid de 240 para 180
                el_vel = avg_bid - el_bid_240
            slugs.append({
                "snaps": snaps, "winner": winner, "el": el,
                "cont_ok": cont_ok, "el_bid_at_180": el_bid_at_180, "el_vel": el_vel,
            })
    return slugs


# ─── Simulação base ───────────────────────────────────────────────────────────

def simulate_base(slug_data, ep_lo=0.82, ep_hi=0.86, entry_secs_max=180):
    """Encontra a entrada e retorna o snap de entrada + snaps posteriores."""
    if not (slug_data["el"] and slug_data["cont_ok"]):
        return None
    snaps   = slug_data["snaps"]
    winner  = slug_data["winner"]
    el      = slug_data["el"]
    entry   = None
    for i, s in enumerate(snaps):
        if s["secs"] > entry_secs_max: continue
        wb = s["up_bid"] if el == "UP" else s["down_bid"]
        if ep_lo <= wb <= ep_hi:
            entry = s; ep = wb; entry_idx = i; break
    if not entry: return None
    post = snaps[entry_idx+1:]
    if not post: return None
    return {
        "entry": entry, "ep": ep, "el": el, "winner": winner,
        "post": post, "entry_idx": entry_idx, "snaps": snaps,
        "el_bid_at_180": slug_data["el_bid_at_180"],
        "el_vel": slug_data["el_vel"],
    }


# ─── BLOCO A: Hedge / exit no cruzamento de 0.50 ─────────────────────────────

def sim_with_cross50(ctx, hedge=False):
    """
    Simula hold to resolution com exit/hedge quando winner bid < 0.50.
    hedge=False: sai no cruzamento (stop no 0.50)
    hedge=True:  quando winner cruza 0.50, compra o lado oposto com qty idêntica
    """
    el = ctx["el"]; winner = ctx["winner"]; ep = ctx["ep"]
    post = ctx["post"]
    cross_secs = None; cross_bid = None
    outcome = None; exit_b = ep; pnl = 0.0

    for s in post:
        wb = s["up_bid"] if el == "UP" else s["down_bid"]
        # cruzamento do 0.50
        if wb < 0.50 and cross_secs is None:
            cross_secs = s["secs"]; cross_bid = wb
            if not hedge:
                # sair aqui
                outcome = "CROSS50_EXIT"
                exit_b = wb
                pnl = round((exit_b - ep) * QTY, 4)
                return {"outcome": outcome, "pnl": pnl, "ep": ep, "exit_b": exit_b,
                        "cross_secs": cross_secs, "cross_bid": cross_bid,
                        "dir_correct": el == winner}
            # hedge: continua monitorando mas registra o cruzamento

    if outcome is None:
        lw = post[-1]["up_bid"] if el == "UP" else post[-1]["down_bid"]
        if post[-1]["secs"] <= 35:
            if lw >= 0.85:
                outcome = "WIN"; exit_b = 1.0
            else:
                outcome = "REVERSAL"; exit_b = lw
        else:
            return None

    if not hedge or cross_secs is None:
        pnl = round((exit_b - ep) * QTY, 4)
        return {"outcome": outcome, "pnl": pnl, "ep": ep, "exit_b": exit_b,
                "cross_secs": cross_secs, "cross_bid": cross_bid,
                "dir_correct": el == winner}

    # hedge: posição original + compra oposta no cruzamento
    # pnl_original: (exit_b - ep) * QTY
    # pnl_hedge: (exit_b_oposto - cross_bid) * QTY
    # exit_b_oposto: se o winner continua caindo, o oposto sobe para 1.0
    opp_exit = 1.0 if outcome == "REVERSAL" else 0.0
    pnl_orig  = round((exit_b - ep) * QTY, 4)
    pnl_hedge = round((opp_exit - cross_bid) * QTY, 4)
    pnl = round(pnl_orig + pnl_hedge, 4)
    return {"outcome": f"HEDGE_{outcome}", "pnl": pnl, "ep": ep,
            "exit_b": exit_b, "cross_secs": cross_secs, "cross_bid": cross_bid,
            "pnl_orig": pnl_orig, "pnl_hedge": pnl_hedge,
            "dir_correct": el == winner}


def report_cross50(slugs):
    contexts = [c for s in slugs for c in [simulate_base(s)] if c]
    print(f"\n  Slugs com entrada EL estavel 0.82-0.86: {len(contexts)}")

    # quantos cruzam 0.50?
    cross_cases = []
    no_cross    = []
    for ctx in contexts:
        el = ctx["el"]
        crossed = any((s["up_bid"] if el=="UP" else s["down_bid"]) < 0.50
                      for s in ctx["post"])
        if crossed:
            cross_cases.append(ctx)
        else:
            no_cross.append(ctx)

    print(f"  Cruzam 0.50 (inversão total): {len(cross_cases)} "
          f"({100*len(cross_cases)/len(contexts):.1f}%)")
    print(f"  Nunca cruzam 0.50:            {len(no_cross)} "
          f"({100*len(no_cross)/len(contexts):.1f}%)")

    # dos que cruzam 0.50, qual era a resolução?
    cross_winner_el  = sum(1 for c in cross_cases if c["el"] == c["winner"])
    cross_winner_opp = sum(1 for c in cross_cases if c["el"] != c["winner"])
    print(f"\n  Dos que cruzam 0.50:")
    print(f"    EL original venceu (bounce): {cross_winner_el} "
          f"({100*cross_winner_el/len(cross_cases):.0f}%)")
    print(f"    Lado oposto venceu (reversão real): {cross_winner_opp} "
          f"({100*cross_winner_opp/len(cross_cases):.0f}%)")

    print()
    print(SEP)
    print("  Estratégia 1 — SEM STOP (hold to resolution):")
    trades_ns = []
    for ctx in contexts:
        t = sim_with_cross50(ctx, hedge=False)
        if t and "CROSS50" not in t["outcome"]:
            trades_ns.append(t)
        elif t:
            # tratar cross50_exit como trade resolvido mesmo no sem stop
            trades_ns.append(t)
    _report_trades([t for ctx in contexts
                    for t in [sim_with_cross50(ctx, hedge=False)] if t and "CROSS50" not in t.get("outcome","")],
                   "Hold to resolution (sem hedge, sem stop)")

    print()
    print(SEP)
    print("  Estratégia 2 — EXIT no cruzamento de 0.50 (stop dinâmico):")
    trades_cross = []
    for ctx in contexts:
        t = sim_with_cross50(ctx, hedge=False)
        if t: trades_cross.append(t)
    _report_trades(trades_cross, "Exit no cruzamento de 0.50")

    print()
    print(SEP)
    print("  Estratégia 3 — HEDGE no cruzamento de 0.50 (compra lado oposto):")
    trades_hedge = []
    for ctx in contexts:
        t = sim_with_cross50(ctx, hedge=True)
        if t: trades_hedge.append(t)
    _report_trades(trades_hedge, "Hedge no cruzamento de 0.50")

    # detalhar os cases que cruzaram 0.50
    print()
    print(SEP)
    print("  DETALHE — trades que cruzaram 0.50 (bounce vs reversão real):")
    for ctx in cross_cases[:0]:  # só para não printar muito
        pass
    bounce = []; real_rev = []
    for ctx in cross_cases:
        el = ctx["el"]
        # encontrar o bid mínimo e quando se recuperou
        min_wb = min(s["up_bid"] if el=="UP" else s["down_bid"] for s in ctx["post"])
        last_wb = (ctx["post"][-1]["up_bid"] if el=="UP"
                   else ctx["post"][-1]["down_bid"])
        if last_wb >= 0.85:
            bounce.append({"min_wb": min_wb, "ctx": ctx})
        else:
            real_rev.append({"min_wb": min_wb, "ctx": ctx})

    if bounce:
        min_wbs = [b["min_wb"] for b in bounce]
        print(f"\n  Bounces (EL recuperou, venceu mesmo): n={len(bounce)}")
        print(f"    Bid mínimo atingido: avg={sum(min_wbs)/len(min_wbs):.3f} "
              f"min={min(min_wbs):.3f} max={max(min_wbs):.3f}")
        print(f"    → Sair no 0.50 teria perdido esses casos!")

    if real_rev:
        min_wbs = [r["min_wb"] for r in real_rev]
        cross_bids = []
        for r in real_rev:
            el2 = r["ctx"]["el"]
            for s in r["ctx"]["post"]:
                wb = s["up_bid"] if el2=="UP" else s["down_bid"]
                if wb < 0.50:
                    cross_bids.append(wb)
                    break
        print(f"\n  Reversões reais (EL perdeu): n={len(real_rev)}")
        print(f"    Bid ao cruzar 0.50: avg={sum(cross_bids)/len(cross_bids):.3f} "
              f"min={min(cross_bids):.3f}")
        if cross_bids:
            # pnl de sair no cruzamento vs perda total
            ep_avg = sum(c["ctx"]["ep"] for c in real_rev) / len(real_rev)
            pnl_exit_cross = sum((cb - c["ctx"]["ep"]) * QTY
                                 for cb, c in zip(cross_bids, real_rev))
            pnl_full_loss  = sum(-c["ctx"]["ep"] * QTY for c in real_rev)
            print(f"    PnL saindo no cruzamento 0.50: ${pnl_exit_cross:+.2f} "
                  f"(avg ${pnl_exit_cross/len(real_rev):+.3f}/trade)")
            print(f"    PnL perdendo até 0 (hold):     ${pnl_full_loss:+.2f} "
                  f"(avg ${pnl_full_loss/len(real_rev):+.3f}/trade)")
            print(f"    Economia por sair no 0.50: ${pnl_exit_cross - pnl_full_loss:+.2f}")


def _report_trades(trades, label):
    if not trades:
        print(f"  {label}: 0 trades"); return
    wins   = [t for t in trades if t["outcome"] in ("WIN", "HEDGE_WIN")]
    revs   = [t for t in trades if t["outcome"] in ("REVERSAL", "HEDGE_REVERSAL")]
    cross  = [t for t in trades if "CROSS50" in t.get("outcome", "")]
    pnl    = sum(t["pnl"] for t in trades)
    wr     = 100 * len(wins) / len(trades)
    print(f"  {label}")
    print(f"    n={len(trades)}  WR={wr:.1f}%  PnL=${pnl:+.2f}  avg=${pnl/len(trades):+.4f}")
    for lbl, ts in [("WIN", wins), ("REVERSAL", revs), ("CROSS50_EXIT", cross)]:
        if ts:
            p = sum(t["pnl"] for t in ts)
            print(f"    {lbl:14}: {len(ts):3d}  ${p:+.2f}  avg=${p/len(ts):+.4f}")


# ─── BLOCO B: Filtros adicionais de continuidade ─────────────────────────────

def report_filters(slugs):
    contexts = [c for s in slugs for c in [simulate_base(s)] if c]

    # enriquecer com dados do snap de entrada
    enriched = []
    for ctx in contexts:
        entry = ctx["entry"]; el = ctx["el"]
        wb = ctx["ep"]
        range15  = entry.get("range15") or 0.0
        d15      = entry.get("d15") or 0.0
        spread   = entry.get("spread") or 0.0
        el_bid180 = ctx["el_bid_at_180"]
        el_vel    = ctx["el_vel"]

        # outcome final (hold to resolution, sem stop)
        post = ctx["post"]
        lw = post[-1]["up_bid"] if el=="UP" else post[-1]["down_bid"]
        if post[-1]["secs"] > 35:
            continue
        outcome = "WIN" if lw >= 0.85 else "REVERSAL"
        pnl = round(((1.0 if outcome=="WIN" else lw) - ctx["ep"]) * QTY, 4)

        enriched.append({
            "outcome": outcome, "pnl": pnl, "ep": wb,
            "range15": range15, "d15": d15, "spread": spread,
            "el_bid180": el_bid180, "el_vel": el_vel,
            "el": el, "winner": ctx["winner"], "entry_secs": entry["secs"],
        })

    if not enriched:
        print("  Sem dados suficientes."); return

    wins = [t for t in enriched if t["outcome"] == "WIN"]
    revs = [t for t in enriched if t["outcome"] == "REVERSAL"]
    total_pnl = sum(t["pnl"] for t in enriched)
    print(f"\n  Base (EL estavel, sem filtros adicionais):")
    print(f"  n={len(enriched)}  WR={100*len(wins)/len(enriched):.1f}%  "
          f"PnL=${total_pnl:+.2f}  avg=${total_pnl/len(enriched):+.4f}")
    print(f"  WIN={len(wins)}  REVERSAL={len(revs)}")

    print()
    print(SEP)
    print("  FILTRO 1 — EL bid em secs 180-121 (elev. do EL na janela de confirmação)")
    print(SEP)
    print(f"  {'el_bid180 >=':>14} {'n':>5} {'WR%':>6} {'PnL':>9} {'avg':>8} {'n_rev':>6}")
    for thr in [0.70, 0.72, 0.75, 0.78, 0.80, 0.83, 0.85]:
        sub = [t for t in enriched if t["el_bid180"] >= thr]
        if not sub: continue
        ws = [t for t in sub if t["outcome"] == "WIN"]
        p  = sum(t["pnl"] for t in sub)
        rs = [t for t in sub if t["outcome"] == "REVERSAL"]
        print(f"  {thr:>14.2f} {len(sub):>5} {100*len(ws)/len(sub):>5.1f}% "
              f"${p:>+8.2f} ${p/len(sub):>+7.4f} {len(rs):>6}")

    print()
    print(SEP)
    print("  FILTRO 2 — market_range_15s na entrada (< = mercado mais quieto)")
    print(SEP)
    has_range = [t for t in enriched if t["range15"] > 0]
    if has_range:
        print(f"  (n com range15 disponível: {len(has_range)}/{len(enriched)})")
        print(f"  {'range15 <':>12} {'n':>5} {'WR%':>6} {'PnL':>9} {'avg':>8}")
        for thr in [0.010, 0.015, 0.020, 0.025, 0.030, 0.040]:
            sub = [t for t in has_range if t["range15"] < thr]
            if not sub: continue
            ws  = [t for t in sub if t["outcome"] == "WIN"]
            p   = sum(t["pnl"] for t in sub)
            print(f"  {thr:>12.3f} {len(sub):>5} {100*len(ws)/len(sub):>5.1f}% "
                  f"${p:>+8.2f} ${p/len(sub):>+7.4f}")
    else:
        print("  range15 não disponível nesta amostra de logs.")

    print()
    print(SEP)
    print("  FILTRO 3 — velocidade do EL (bid 180s vs bid 240s): positiva = crescendo")
    print(SEP)
    print(f"  {'el_vel >=':>12} {'n':>5} {'WR%':>6} {'PnL':>9} {'avg':>8}")
    for thr in [-0.10, -0.05, 0.0, 0.02, 0.05, 0.08, 0.10]:
        sub = [t for t in enriched if t["el_vel"] >= thr]
        if not sub: continue
        ws = [t for t in sub if t["outcome"] == "WIN"]
        p  = sum(t["pnl"] for t in sub)
        print(f"  {thr:>12.2f} {len(sub):>5} {100*len(ws)/len(sub):>5.1f}% "
              f"${p:>+8.2f} ${p/len(sub):>+7.4f}")

    print()
    print(SEP)
    print("  FILTRO 4 — spot_delta_15s concorda com EL (d15 > 0 para EL=UP)")
    print(SEP)
    has_d15 = [t for t in enriched if abs(t["d15"]) > 0.5]
    if has_d15:
        agree    = [t for t in has_d15 if (t["d15"] > 0) == (t["el"] == "UP")]
        disagree = [t for t in has_d15 if (t["d15"] > 0) != (t["el"] == "UP")]
        for lbl2, sub in [("Spot CONCORDA", agree), ("Spot DISCORDA", disagree)]:
            if not sub: continue
            ws = [t for t in sub if t["outcome"] == "WIN"]
            p  = sum(t["pnl"] for t in sub)
            rs = [t for t in sub if t["outcome"] == "REVERSAL"]
            print(f"  {lbl2:16}: n={len(sub):3d}  WR={100*len(ws)/len(sub):4.1f}%  "
                  f"PnL=${p:+.2f}  avg=${p/len(sub):+.4f}  rev={len(rs)}")
    else:
        print("  spot_d15 não disponível nesta amostra.")

    print()
    print(SEP)
    print("  FILTRO 5 — secs de entrada (mais cedo = mais caro, mais barato = vice-versa)")
    print(SEP)
    print(f"  {'secs':>8} {'n':>5} {'WR%':>6} {'PnL':>9} {'avg':>8} {'ep_avg':>7}")
    for lo, hi in [(121,180),(91,120),(61,90),(31,60),(1,30)]:
        sub = [t for t in enriched if lo <= t["entry_secs"] <= hi]
        if not sub: continue
        ws  = [t for t in sub if t["outcome"] == "WIN"]
        p   = sum(t["pnl"] for t in sub)
        ep_ = sum(t["ep"] for t in sub)/len(sub)
        print(f"  {lo:>4}-{hi:<3} {len(sub):>5} {100*len(ws)/len(sub):>5.1f}% "
              f"${p:>+8.2f} ${p/len(sub):>+7.4f} {ep_:>7.3f}")

    # Melhor combinação de filtros
    print()
    print(SEP)
    print("  COMBINAÇÕES PROMISSORAS")
    print(SEP)
    combos = [
        ("EL estavel baseline",
         lambda t: True),
        ("+ el_bid180 >= 0.75",
         lambda t: t["el_bid180"] >= 0.75),
        ("+ el_bid180 >= 0.78",
         lambda t: t["el_bid180"] >= 0.78),
        ("+ el_bid180 >= 0.80",
         lambda t: t["el_bid180"] >= 0.80),
        ("+ el_vel >= 0.0 (crescendo)",
         lambda t: t["el_vel"] >= 0.0),
        ("+ el_bid180>=0.75 + el_vel>=0",
         lambda t: t["el_bid180"] >= 0.75 and t["el_vel"] >= 0.0),
        ("+ el_bid180>=0.78 + el_vel>=0",
         lambda t: t["el_bid180"] >= 0.78 and t["el_vel"] >= 0.0),
        ("+ el_bid180>=0.80 + el_vel>=0",
         lambda t: t["el_bid180"] >= 0.80 and t["el_vel"] >= 0.0),
    ]
    print(f"  {'Filtro':40} {'n':>5} {'WR%':>6} {'PnL':>9} {'avg':>8} {'rev':>4}")
    for lbl3, fn in combos:
        sub = [t for t in enriched if fn(t)]
        if not sub: continue
        ws  = [t for t in sub if t["outcome"] == "WIN"]
        rs  = [t for t in sub if t["outcome"] == "REVERSAL"]
        p   = sum(t["pnl"] for t in sub)
        print(f"  {lbl3:40} {len(sub):>5} {100*len(ws)/len(sub):>5.1f}% "
              f"${p:>+8.2f} ${p/len(sub):>+7.4f} {len(rs):>4}")


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    print("\nCarregando slugs...")
    slugs = load_slugs()
    el_n = sum(1 for s in slugs if s["el"] and s["cont_ok"])
    print(f"Total: {len(slugs)}  |  EL estavel (F3): {el_n}")

    print()
    print("=" * 70)
    print("  BLOCO A — HEDGE / EXIT NO CRUZAMENTO DE 0.50")
    print("=" * 70)
    report_cross50(slugs)

    print()
    print("=" * 70)
    print("  BLOCO B — FILTROS ADICIONAIS DE CONTINUIDADE")
    print("=" * 70)
    report_filters(slugs)


if __name__ == "__main__":
    main()
