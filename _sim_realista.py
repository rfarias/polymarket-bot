"""
_sim_realista.py

Simulação realista com os problemas de execução identificados no runner real.

Problemas modelados (documentados em TESTES_ANALISE_EL.md):

  EE:
  1. Seleção adversa na entrada:
       - Trades vencedores: EL se movia rápido → livro fino → fill parcial (~75%)
       - Trades perdedores: mercado lento / reversão → fill completo (100%)
  2. Stop fill degradado:
       - Stop FAK removido (14/14 fails). Se usado novamente, fill em ~0.20 (user obs.)
       - Sem stop: hold até resolução → perda total (0.0)
  3. PP (Profit Protect @ 0.88):
       - Livro fino a 0.88 → fill parcial (~65%)
       - 35% restante: hold até resolução (recebe 1.0 se ganhar, 0 se reverter)

  AR:
  4. Entradas: mercado líquido, fill ~95% sempre
  5. Wins: hold to resolution → 100% fill a 1.0 (binário)
  6. Stops estruturais/timeout: gap pequeno, fill ~85% do preço

Cenários:
  P  — Paper idealizado (100% fill, stop a 0.65, PP a 0.88 @ 100%)
  H  — Hold to resolution (atual runner: sem stop, sem PP, com seleção adversa)
  PP — Com PP a 0.88 (fill parcial 65%, seleção adversa)
  ST — Com stop a 0.20 (fill completo, seleção adversa)
  CO — Combinado: PP + stop a 0.20

Uso:
  python _sim_realista.py            # mostra todos os cenários
  python _sim_realista.py --qty 6    # default
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import NamedTuple

TZ_LOCAL = timezone(timedelta(hours=-3))
QTY_PAPER_AR = 50.0

# ─────────────────────────────────────────────────────────────────────────────
# Parâmetros de execução realistas
# ─────────────────────────────────────────────────────────────────────────────

# Seleção adversa na ENTRADA (EE):
#   Se mercado vai virar a favor do EL, o livro de vendas a 0.83–0.86 some rápido.
#   Baseado na observação: wins preenchem parcial, losses preenchem completo.
EE_WIN_ENTRY_FILL  = 0.75  # 75% de qty: trading em mercado que já move
EE_LOSS_ENTRY_FILL = 1.00  # 100%: reversão — vendedores querem sair

# Stop real (fill degradado):
#   Baseado em: gap médio = 0.10 (seção 21.4), user obs "próximo de 0.20"
#   STOP FAK foi 0/14 → removido. Modela novo stop com GTC.
STOP_FILL_PRICE    = 0.20  # fill real quando stop ativa
STOP_FIRES_RATE    = 0.80  # 80% dos stops disparam antes de resolve completo

# PP (Profit Protect @ 0.88):
#   Livro de compra a 0.88 é fino quando EL está subindo.
#   Baseado em: WINs → PP perdem $0.17/trade por fill parcial (seção 21).
PP_BID         = 0.88
PP_FILL_RATE   = 0.65      # apenas 65% de qty sai a 0.88
PP_REM_WIN_P   = 0.90      # prob de os 35% restantes resolverem WIN (EL continua)

# AR Standard: mercado mais líquido, execution melhor
AR_ENTRY_FILL      = 0.95  # 95% fill nas entradas
AR_WIN_RES_FILL    = 1.00  # hold to resolution = 100% a 1.0
AR_STOP_FILL       = 0.90  # 90% fill nos stops (gap menor, mercado mais líquido)
AR_STOP_GAP        = 0.02  # gap médio nos stops AR (2 ticks)


# ─────────────────────────────────────────────────────────────────────────────
# Carregamento dos dados
# ─────────────────────────────────────────────────────────────────────────────

def load_ee_sim(qty: float) -> list[dict]:
    try:
        from _sim_ee_gates import load_snaps, simulate_slug
    except ImportError:
        return []

    by_slug = load_snaps("logs/ee_paper_*/ee_paper.jsonl")
    trades = []
    for slug, snaps in sorted(by_slug.items()):
        r = simulate_slug(snaps, 0.17, None, True, True, qty)
        if r is None:
            continue
        ep = r["ep"]
        if not (0.83 <= ep <= 0.86):
            continue
        trades.append({
            "source":   "EE",
            "ep":       ep,
            "xp":       r["exit_price"],     # exit price idealizado
            "outcome":  r["outcome"],         # WIN / STOP / PP
            "ticks_ideal": round((r["exit_price"] - ep) / 0.01, 2),
            "pnl_ideal":   r["pnl"],          # pnl com qty desejada
            "ts":       r["ts"],
            "el_vel":   r["el_vel"],
        })
    return trades


def load_ar_exits(qty: float, from_date: str = "20260526") -> list[dict]:
    files = []
    import re
    for pattern in [
        "logs/current_almost_resolved_paper_202605*.jsonl",
        "logs/current_almost_resolved_paper_202606*.jsonl",
    ]:
        for f in sorted(glob.glob(pattern)):
            m = re.search(r'_(\d{8})_', f)
            if m and m.group(1) >= from_date:
                files.append(f)

    trades = []
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("type") != "exit":
                    continue
                t = ev["trade"]
                variant = t.get("setup_variant", "")
                ep      = t.get("entry_price", 0.0)
                ticks   = t.get("pnl_ticks")
                quote   = t.get("pnl_quote")
                reason  = t.get("exit_reason", "")
                xp      = t.get("exit_price", 0.0)
                ts      = ev.get("ts", 0)
                stop_p  = t.get("stop_price", ep - 0.03)

                if ticks is None or quote is None:
                    continue

                is_std  = (variant == "standard"            and 0.91 <= ep <= 0.96)
                is_dual = ("dual_rich" in variant           and abs(ep - 0.98) < 0.005)
                if not (is_std or is_dual):
                    continue

                pnl_paper = float(quote) * (qty / QTY_PAPER_AR)
                trades.append({
                    "source":   "AR",
                    "variant":  variant,
                    "ep":       ep,
                    "xp":       xp,
                    "stop_p":   float(stop_p),
                    "reason":   reason,
                    "ticks_ideal": float(ticks),
                    "pnl_ideal":   round(pnl_paper, 4),
                    "ts":       ts,
                })
    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Aplicação dos cenários de execução
# ─────────────────────────────────────────────────────────────────────────────

def apply_scenario_ee(trade: dict, scenario: str, qty: float) -> float:
    """Retorna PnL realista para um trade EE dado o cenário de execução."""
    ep  = trade["ep"]
    out = trade["outcome"]   # WIN, STOP, PP (da simulação idealizada)

    # Seleção adversa na entrada: qty real pode ser menor
    if scenario == "P":
        filled_qty = qty
    else:
        filled_qty = qty * (EE_WIN_ENTRY_FILL if out == "WIN" else EE_LOSS_ENTRY_FILL)

    if scenario == "P":
        # ── Paper: PP a 0.88 @ 100% fill, stop a 0.65 @ 100%
        if out == "WIN":
            return round((0.97 - ep) * filled_qty, 4)  # avg exit WIN
        elif out == "STOP":
            return round((0.65 - ep) * filled_qty, 4)  # stop a 0.65
        else:
            return round((PP_BID - ep) * filled_qty, 4)

    elif scenario == "H":
        # ── Hold to Resolution: sem stop, sem PP, hold binário
        if out == "WIN":
            return round((1.0 - ep) * filled_qty, 4)
        else:
            # loss: full reversal (STOP ou PP abortado → resolve a 0)
            return round((0.0 - ep) * filled_qty, 4)

    elif scenario == "PP":
        # ── PP a 0.88 com fill parcial; seleção adversa na entrada
        if out == "WIN":
            pp_filled   = filled_qty * PP_FILL_RATE
            pp_rem      = filled_qty * (1 - PP_FILL_RATE)
            pnl_pp      = (PP_BID - ep) * pp_filled
            pnl_rem     = (1.0 - ep) * pp_rem  # resto resolve via binário
            return round(pnl_pp + pnl_rem, 4)
        else:
            return round((0.0 - ep) * filled_qty, 4)

    elif scenario == "ST":
        # ── Stop a 0.20 (com seleção adversa na entrada)
        if out == "WIN":
            return round((1.0 - ep) * filled_qty, 4)
        else:
            fired_pnl    = (STOP_FILL_PRICE - ep) * filled_qty
            nofired_pnl  = (0.0 - ep) * filled_qty
            return round(STOP_FIRES_RATE * fired_pnl + (1 - STOP_FIRES_RATE) * nofired_pnl, 4)

    elif scenario == "CO":
        # ── Combinado: PP a 0.88 (parcial) + stop a 0.20 nos losses
        if out == "WIN":
            pp_filled = filled_qty * PP_FILL_RATE
            pp_rem    = filled_qty * (1 - PP_FILL_RATE)
            return round((PP_BID - ep) * pp_filled + (1.0 - ep) * pp_rem, 4)
        else:
            fired_pnl   = (STOP_FILL_PRICE - ep) * filled_qty
            nofired_pnl = (0.0 - ep) * filled_qty
            return round(STOP_FIRES_RATE * fired_pnl + (1 - STOP_FIRES_RATE) * nofired_pnl, 4)

    return 0.0


def apply_scenario_ar(trade: dict, scenario: str, qty: float) -> float:
    """PnL realista para um trade AR dado o cenário.

    A paper exit (xp) já reflete o resultado aproximado: PP a ~0.92-0.97,
    target a 1.0, stop a ~0.88.

    'Hold to resolution' (H) elimina a saída antecipada por PP:
      - Wins: binário → 1.0 por share (melhor que sair a 0.93)
      - Losses: sem stop → 0.0 por share (pior que sair a 0.88)

    Como a WR do AR é muito alta (97%+), hold domina o PP.
    """
    ep     = trade["ep"]
    pnl_p  = trade["pnl_ideal"]   # PnL já calculado do paper (qty=6)
    reason = trade["reason"]
    xp     = trade["xp"]

    is_win = pnl_p >= 0  # classificação pelo PnL do paper

    if scenario == "P":
        # Paper: usa valores reais dos logs (já corretos, qty=6)
        return pnl_p

    # Cenários realistas — fill de entrada: 95% para AR
    filled = qty * AR_ENTRY_FILL

    if scenario == "H":
        # Hold to resolution: win→1.0, loss→0.0 (sem stop, sem PP)
        if is_win:
            return round((1.0 - ep) * filled, 4)
        else:
            return round((0.0 - ep) * filled, 4)

    elif scenario in ("PP", "CO"):
        # PP a preço do paper mas com fill parcial 80% (AR mais líquido que EE)
        ar_pp_fill = 0.80
        if is_win:
            pp_filled = filled * ar_pp_fill
            pp_rem    = filled * (1 - ar_pp_fill)
            exit_pp   = min(xp, 1.0)           # xp já é o preço de PP do paper
            return round((exit_pp - ep) * pp_filled + (1.0 - ep) * pp_rem, 4)
        else:
            return round((0.0 - ep) * filled, 4)

    elif scenario == "ST":
        # Stop a preço levemente degradado (AR mais líquido, gap ~2 ticks)
        if is_win:
            return round((1.0 - ep) * filled, 4)
        else:
            stop_p_real = max(xp - AR_STOP_GAP, ep - 0.05)
            return round((stop_p_real - ep) * filled * AR_STOP_FILL, 4)

    return pnl_p


# ─────────────────────────────────────────────────────────────────────────────
# Estatísticas por cenário
# ─────────────────────────────────────────────────────────────────────────────

class ScenResult(NamedTuple):
    label: str
    n: int
    wins: int
    losses: int
    total_pnl: float
    avg_pnl: float
    total_ticks: float
    avg_ticks: float


def run_scenarios(ee_trades: list[dict], ar_trades: list[dict],
                  qty: float, scenarios: list[str]) -> dict[str, dict]:
    results = {}
    for sc in scenarios:
        ee_pnls = [apply_scenario_ee(t, sc, qty) for t in ee_trades]
        ar_pnls = [apply_scenario_ar(t, sc, qty) for t in ar_trades]
        all_pnls = ee_pnls + ar_pnls

        n      = len(all_pnls)
        wins   = sum(1 for p in all_pnls if p > 0)
        total  = round(sum(all_pnls), 2)
        avg    = round(total / n, 4) if n else 0.0

        # ticks = pnl / (qty * 0.01)
        tick_val = qty * 0.01
        total_ticks = round(total / tick_val, 1) if tick_val else 0.0
        avg_ticks   = round(avg / tick_val, 3)   if tick_val else 0.0

        results[sc] = {
            "n": n, "wins": wins, "losses": n - wins,
            "wr": round(wins / n * 100, 1) if n else 0.0,
            "total_pnl": total, "avg_pnl": avg,
            "total_ticks": total_ticks, "avg_ticks": avg_ticks,
            "ee_pnl": round(sum(ee_pnls), 2),
            "ar_pnl": round(sum(ar_pnls), 2),
            "ee_n": len(ee_pnls), "ar_n": len(ar_pnls),
        }
    return results


def run_scenarios_only_ee(ee_trades: list[dict], qty: float,
                           scenarios: list[str]) -> dict[str, dict]:
    results = {}
    for sc in scenarios:
        pnls = [apply_scenario_ee(t, sc, qty) for t in ee_trades]
        n    = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        total = round(sum(pnls), 2)
        avg   = round(total / n, 4) if n else 0.0
        tick_val = qty * 0.01
        results[sc] = {
            "n": n, "wins": wins, "losses": n - wins,
            "wr": round(wins / n * 100, 1) if n else 0.0,
            "total_pnl": total, "avg_pnl": avg,
            "total_ticks": round(total / tick_val, 1),
            "avg_ticks": round(avg / tick_val, 3),
        }
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

SCENARIO_LABELS = {
    "P":  "P  — Paper idealizado          ",
    "H":  "H  — Hold resol. (atual runner) ",
    "PP": "PP — Profit Protect 0.88 parcial",
    "ST": "ST — Stop a 0.20               ",
    "CO": "CO — Combinado PP + Stop 0.20  ",
}

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qty",       type=float, default=6.0)
    parser.add_argument("--from-date", default="20260526")
    args = parser.parse_args()
    qty = args.qty

    print(f"Carregando dados... (QTY={qty})")
    ee = load_ee_sim(qty)
    ar = load_ar_exits(qty, args.from_date)
    print(f"  EE: {len(ee)} trades | AR: {len(ar)} trades")

    # Split EE por outcome
    ee_wins  = [t for t in ee if t["outcome"] == "WIN"]
    ee_stops = [t for t in ee if t["outcome"] == "STOP"]
    print(f"  EE WIN: {len(ee_wins)} | EE STOP/LOSS: {len(ee_stops)}")
    print()

    scenarios = ["P", "H", "PP", "ST", "CO"]
    res_total = run_scenarios(ee, ar, qty, scenarios)
    res_ee    = run_scenarios_only_ee(ee, qty, scenarios)

    LINE = "=" * 80

    # ── Tabela principal ──────────────────────────────────────────────────────
    print(LINE)
    print(f"  SIMULAÇÃO REALISTA — QTY={qty} cotas — 1 tick = ${qty*0.01:.2f}")
    print(f"  Período: {len(ar)} trades AR + {len(ee)} trades EE = {len(ar)+len(ee)} total")
    print(LINE)
    print(f"  {'Cenário':<35} {'N':>5} {'W':>5} {'L':>4} {'WR%':>5} "
          f"{'Ticks':>7} {'USD':>9} {'Avg/tr':>8} {'EE$':>7} {'AR$':>7}")
    print(f"  {'─'*35} {'─'*5} {'─'*5} {'─'*4} {'─'*5} {'─'*7} {'─'*9} {'─'*8} {'─'*7} {'─'*7}")
    for sc in scenarios:
        r  = res_total[sc]
        lbl = SCENARIO_LABELS[sc]
        print(f"  {lbl} {r['n']:>5} {r['wins']:>5} {r['losses']:>4} {r['wr']:>4.1f}% "
              f"{r['total_ticks']:>+7.0f} {r['total_pnl']:>+9.2f} {r['avg_pnl']:>+8.4f} "
              f"{r['ee_pnl']:>+7.2f} {r['ar_pnl']:>+7.2f}")

    # ── Detalhe EE ────────────────────────────────────────────────────────────
    print(f"\n{'─'*80}")
    print("  EE — ISOLADO (ver impacto da seleção adversa + stop/PP)")
    print(f"{'─'*80}")
    print(f"  {'Cenário':<35} {'N':>4} {'W':>4} {'L':>3} {'WR%':>5} {'Ticks':>7} {'USD':>8} {'Avg/tr':>8}")
    print(f"  {'─'*35} {'─'*4} {'─'*4} {'─'*3} {'─'*5} {'─'*7} {'─'*8} {'─'*8}")
    for sc in scenarios:
        r   = res_ee[sc]
        lbl = SCENARIO_LABELS[sc]
        print(f"  {lbl} {r['n']:>4} {r['wins']:>4} {r['losses']:>3} {r['wr']:>4.1f}% "
              f"{r['total_ticks']:>+7.0f} {r['total_pnl']:>+8.2f} {r['avg_pnl']:>+8.4f}")

    # ── Análise PP vs Hold ────────────────────────────────────────────────────
    print(f"\n{'─'*80}")
    print("  PP vs HOLD-TO-RESOLUTION — Análise por tipo de trade (EE only)")
    print(f"{'─'*80}")

    ee_win_data  = [apply_scenario_ee(t, "H",  qty) for t in ee_wins]
    ee_win_pp    = [apply_scenario_ee(t, "PP", qty) for t in ee_wins]
    ee_loss_h    = [apply_scenario_ee(t, "H",  qty) for t in ee_stops]
    ee_loss_st   = [apply_scenario_ee(t, "ST", qty) for t in ee_stops]

    def _s(vals):
        n = len(vals)
        if n == 0: return "n=0"
        return f"n={n} total={sum(vals):>+.2f} avg={sum(vals)/n:>+.4f}"

    print(f"  Wins — Hold:           {_s(ee_win_data)}")
    print(f"  Wins — PP 0.88 (65%):  {_s(ee_win_pp)}")
    delta_win = round(sum(ee_win_pp) - sum(ee_win_data), 2)
    print(f"  ΔPnL (PP - Hold) wins: {delta_win:>+.2f} USD", end="")
    if delta_win < 0:
        print(f"  ← PP PIORA wins (sai antes de 1.0)")
    else:
        print(f"  ← PP MELHORA wins")

    print()
    print(f"  Stops — Hold (0.0):    {_s(ee_loss_h)}")
    print(f"  Stops — ST (0.20):     {_s(ee_loss_st)}")
    delta_loss = round(sum(ee_loss_st) - sum(ee_loss_h), 2)
    print(f"  ΔPnL (Stop - Hold) losses: {delta_loss:>+.2f} USD", end="")
    if delta_loss > 0:
        print(f"  ← Stop SALVA capital")
    else:
        print(f"  ← Stop não ajuda")

    # ── Impacto da seleção adversa ────────────────────────────────────────────
    print(f"\n{'─'*80}")
    print("  IMPACTO DA SELEÇÃO ADVERSA NA ENTRADA (EE only)")
    print(f"{'─'*80}")
    ideal_win_pnl = sum(t["pnl_ideal"] for t in ee_wins)
    real_win_pnl  = sum(apply_scenario_ee(t, "H", qty) for t in ee_wins)
    ideal_loss_pnl = sum(t["pnl_ideal"] for t in ee_stops)
    real_loss_pnl  = sum(apply_scenario_ee(t, "H", qty) for t in ee_stops)

    sa_impact = round((real_win_pnl + real_loss_pnl) - (ideal_win_pnl + ideal_loss_pnl), 2)
    print(f"  Wins idealizados:   {sum(t['pnl_ideal'] for t in ee_wins):>+.2f} USD")
    print(f"  Wins realistas:     {real_win_pnl:>+.2f} USD  (fill {EE_WIN_ENTRY_FILL*100:.0f}%)")
    print(f"  Losses idealizados: {ideal_loss_pnl:>+.2f} USD")
    print(f"  Losses realistas:   {real_loss_pnl:>+.2f} USD  (fill {EE_LOSS_ENTRY_FILL*100:.0f}%)")
    print(f"  Custo seleção adversa: {sa_impact:>+.2f} USD  ({sa_impact/(real_win_pnl+real_loss_pnl)*100:.1f}% do PnL)")

    # ── Resumo ────────────────────────────────────────────────────────────────
    all_ts = [t["ts"] for t in ee + ar if t.get("ts", 0) > 0]
    days   = (max(all_ts) - min(all_ts)) / 86400 if all_ts else 10.0

    print(f"\n{'═'*80}")
    print(f"  RESUMO FINAL — QTY={qty} cotas — 10 dias úteis")
    print(f"{'═'*80}")
    print(f"  {'Cenário':<35} {'USD/10d':>8} {'USD/dia':>8} {'USD/mês':>9} {'EV%':>7}")
    print(f"  {'─'*35} {'─'*8} {'─'*8} {'─'*9} {'─'*7}")
    for sc in scenarios:
        r   = res_total[sc]
        d   = round(r["total_pnl"] / days, 2) if days > 0 else 0.0
        m   = round(d * 21, 2)
        ev  = round(r["total_pnl"] / (len(ee) * qty * 0.85 + len(ar) * qty * 0.95) * 100, 2)
        lbl = SCENARIO_LABELS[sc]
        print(f"  {lbl} {r['total_pnl']:>+8.2f} {d:>+8.2f} {m:>+9.2f} {ev:>+6.2f}%")

    print()
    print(f"  Parâmetros usados:")
    print(f"    EE seleção adversa entrada: WIN={EE_WIN_ENTRY_FILL*100:.0f}% | LOSS={EE_LOSS_ENTRY_FILL*100:.0f}%")
    print(f"    Stop fill real:             {STOP_FILL_PRICE:.2f} (vs 0.65 ideal) | fire rate {STOP_FIRES_RATE*100:.0f}%")
    print(f"    PP fill rate:               {PP_FILL_RATE*100:.0f}% a {PP_BID} (35% restante → resolução)")
    print(f"    AR entry fill:              {AR_ENTRY_FILL*100:.0f}% | stop gap: {AR_STOP_GAP:.2f}")
    print(f"{'═'*80}")


if __name__ == "__main__":
    main()
