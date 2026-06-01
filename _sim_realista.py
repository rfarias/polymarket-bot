"""
_sim_realista.py

Simulação realista com os problemas de execução identificados no runner real.

Problemas modelados (documentados em TESTES_ANALISE_EL.md + observações runner):

  EE — problema central: SELEÇÃO ADVERSA NA ENTRADA
    Wins: EL sobe rápido → livro de vendas some → fill parcial (~75%)
    Losses: EL prestes a reverter → vendedores abundantes → fill completo (100%)
    Custo: -68% do PnL potencial nos wins

  Stop fill degradado (corrigido):
    Spread grande + liquidez pequena → fill em ~0.45–0.50 (não 0.65)
    midpoint usado: 0.47

  PP removido corretamente:
    Com fill parcial 65%, PP piora wins em -$32 sem compensar nos stops.
    Hold to resolution é claramente superior.

Cenários EE:
  P   — Paper idealizado (100% fill, stop a 0.65)
  H   — Hold (atual runner): sem stop, sem PP, seleção adversa
  ST  — Stop a 0.47 + hold wins: ideal identificado (sem PP)
  SA1 — SA reduzida: entrada precoce secs>160 → fill 90% wins
  SA2 — SA reduzida: bid passivo ep-0.01 → fill 88% wins
  SA3 — SA reduzida: qty 4 (fill 95%), menos exposição nos losses

Uso:
  python _sim_realista.py            # todos os cenários EE + AR
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

# ── Seleção adversa na ENTRADA (EE) ─────────────────────────────────────────
# Wins: EL em aceleração → livro de vendas ralo → fill parcial
# Losses: reversão se aproxima → vendedores abundantes → fill completo
EE_WIN_ENTRY_FILL  = 0.75   # 75% de qty preenchida nos wins (baseline)
EE_LOSS_ENTRY_FILL = 1.00   # 100% fill nos losses (seleção adversa clássica)

# ── Stop real (corrigido 2026-06-01) ─────────────────────────────────────────
# Spread grande + liquidez pequena → fill em ~0.45–0.50, não 0.65
# midpoint 0.47 = ponto central da faixa observada
STOP_LEVEL         = 0.65   # preço que dispara o stop
STOP_FILL_PRICE    = 0.47   # fill real após slippage/spread (0.45–0.50)
STOP_FIRES_RATE    = 0.85   # 85% dos stops disparam (vs FAK que era 0%)
# Poupança vs hold: (0.47 - 0.0) por share → +$0.47×qty por stop ativado

# ── PP (removido do runner real, mantido apenas para comparação) ──────────────
PP_BID         = 0.88
PP_FILL_RATE   = 0.65       # fill parcial 65% a 0.88
PP_REM_WIN_P   = 0.90       # prob restante 35% resolver WIN

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

def _ee_pnl(ep: float, out: str, qty: float,
            win_fill: float, stop_fill: float, stop_fire: float,
            use_pp: bool = False) -> float:
    """Núcleo do cálculo de PnL EE com parâmetros de execução explícitos."""
    filled = qty * (win_fill if out == "WIN" else EE_LOSS_ENTRY_FILL)

    if out == "WIN":
        if use_pp:
            pf  = filled * PP_FILL_RATE
            rem = filled * (1 - PP_FILL_RATE)
            return round((PP_BID - ep) * pf + (1.0 - ep) * rem, 4)
        else:
            return round((1.0 - ep) * filled, 4)
    else:
        # Loss: stop tenta sair a STOP_LEVEL, fill real = stop_fill
        fired   = (stop_fill  - ep) * filled
        nofired = (0.0        - ep) * filled
        return round(stop_fire * fired + (1 - stop_fire) * nofired, 4)


def apply_scenario_ee(trade: dict, scenario: str, qty: float) -> float:
    """Retorna PnL realista para um trade EE dado o cenário de execução.

    Cenários:
      P    — Paper idealizado: 100% fill, stop a 0.65, sem seleção adversa
      H    — Hold (atual runner): sem stop, sem PP, seleção adversa 75%/100%
      ST   — Stop a 0.47 (sem PP): ideal identificado
      SA1  — SA reduzida via entrada precoce (secs>160): fill 90%
      SA2  — SA reduzida via bid passivo ep-0.01: fill 88%
      SA3  — SA reduzida via qty=4 (sempre ~95% fill): qty fixo 4
      PP   — PP 0.88 fill parcial (mantido só para comparação)
    """
    ep  = trade["ep"]
    out = trade["outcome"]

    if scenario == "P":
        # Paper: fill 100%, stop funciona a 0.65
        if out == "WIN":
            return round((1.0 - ep) * qty, 4)
        else:
            return round((0.65 - ep) * qty, 4)

    elif scenario == "H":
        # Hold to resolution: seleção adversa baseline, sem stop
        return _ee_pnl(ep, out, qty,
                       win_fill=EE_WIN_ENTRY_FILL,
                       stop_fill=0.0, stop_fire=0.0)

    elif scenario == "ST":
        # Ideal: sem PP, stop a 0.47 (fill real do spread largo)
        return _ee_pnl(ep, out, qty,
                       win_fill=EE_WIN_ENTRY_FILL,
                       stop_fill=STOP_FILL_PRICE,
                       stop_fire=STOP_FIRES_RATE)

    elif scenario == "SA1":
        # Entrada precoce (secs 160-180): mercado menos resolvido → mais vendedores
        # Melhora fill win de 75% → 90%
        return _ee_pnl(ep, out, qty,
                       win_fill=0.90,
                       stop_fill=STOP_FILL_PRICE,
                       stop_fire=STOP_FIRES_RATE)

    elif scenario == "SA2":
        # Bid passivo a ep-0.01: fica na fila de compra, preenche quando mercado recua 1 tick
        # Melhora fill win de 75% → 88%; custo: entrada a ep-0.01 (preço melhor!)
        ep_adj = ep - 0.01  # entra 1 tick abaixo → benefício adicional nos wins
        filled = qty * (0.88 if out == "WIN" else EE_LOSS_ENTRY_FILL)
        if out == "WIN":
            return round((1.0 - ep_adj) * filled, 4)
        else:
            fired   = (STOP_FILL_PRICE - ep_adj) * filled
            nofired = (0.0 - ep_adj)             * filled
            return round(STOP_FIRES_RATE * fired + (1 - STOP_FIRES_RATE) * nofired, 4)

    elif scenario == "SA3":
        # Qty=4 fixo: fill quase 100% mesmo nos wins; losses menores
        qty4  = 4.0
        fill4 = 0.95
        filled = qty4 * (fill4 if out == "WIN" else EE_LOSS_ENTRY_FILL)
        if out == "WIN":
            return round((1.0 - ep) * filled, 4)
        else:
            fired   = (STOP_FILL_PRICE - ep) * filled
            nofired = (0.0 - ep)             * filled
            return round(STOP_FIRES_RATE * fired + (1 - STOP_FIRES_RATE) * nofired, 4)

    elif scenario == "PP":
        # PP fill parcial — comparação apenas
        return _ee_pnl(ep, out, qty,
                       win_fill=EE_WIN_ENTRY_FILL,
                       stop_fill=0.0, stop_fire=0.0,
                       use_pp=True)

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
    "P":   "P   — Paper idealizado (100% fill)  ",
    "H":   "H   — Hold (atual runner, SA 75%)   ",
    "ST":  "ST  — Stop 0.47 sem PP [IDEAL]      ",
    "SA1": "SA1 — Entrada precoce secs>160       ",
    "SA2": "SA2 — Bid passivo ep-0.01            ",
    "SA3": "SA3 — Qty=4 fill 95%                ",
    "PP":  "PP  — PP 0.88 parcial (comparacao)  ",
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

    scenarios = ["P", "H", "ST", "SA1", "SA2", "SA3", "PP"]
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

    # ── PP vs Hold vs Stop correto ───────────────────────────────────────────
    print(f"\n{'─'*80}")
    print("  ANALISE PP vs HOLD vs STOP 0.47 — EE only")
    print(f"{'─'*80}")

    def _s(vals):
        n = len(vals)
        if n == 0: return "n=0"
        return f"n={n:>3}  total={sum(vals):>+7.2f}  avg={sum(vals)/n:>+.4f}"

    ee_h   = [apply_scenario_ee(t, "H",  qty) for t in ee_wins]
    ee_st  = [apply_scenario_ee(t, "ST", qty) for t in ee_wins]
    ee_pp  = [apply_scenario_ee(t, "PP", qty) for t in ee_wins]
    print(f"  Wins — Hold (1.0):         {_s(ee_h)}")
    print(f"  Wins — ST stop (hold win): {_s(ee_st)}")
    print(f"  Wins — PP 0.88 (65%fill):  {_s(ee_pp)}")
    dpp = round(sum(ee_pp) - sum(ee_h), 2)
    print(f"  Delta PP vs Hold:  {dpp:>+.2f} USD  ({'PP PIORA' if dpp < 0 else 'PP melhora'})")

    print()
    l_h  = [apply_scenario_ee(t, "H",  qty) for t in ee_stops]
    l_st = [apply_scenario_ee(t, "ST", qty) for t in ee_stops]
    print(f"  Losses — Hold (0.0):       {_s(l_h)}")
    print(f"  Losses — Stop a 0.47:      {_s(l_st)}")
    dstop = round(sum(l_st) - sum(l_h), 2)
    print(f"  Delta Stop vs Hold: {dstop:>+.2f} USD  ({'Stop SALVA capital' if dstop > 0 else 'Stop nao ajuda'})")

    total_st_vs_h = round((sum(ee_st) + sum(l_st)) - (sum(ee_h) + sum(l_h)), 2)
    print(f"\n  Ganho liquido ST vs H (todos trades): {total_st_vs_h:>+.2f} USD")

    # ── Seleção adversa: custo e estratégias de redução ─────────────────────
    print(f"\n{'─'*80}")
    print("  SELECAO ADVERSA — custo e estrategias de reducao (EE only)")
    print(f"{'─'*80}")

    def _ee_total(sc):
        return sum(apply_scenario_ee(t, sc, qty) for t in ee)

    base_h  = _ee_total("H")
    base_st = _ee_total("ST")

    print(f"  {'Cenario':<40} {'PnL/10d':>8}  {'vs Hold':>8}  {'vs ST':>8}")
    print(f"  {'─'*40} {'─'*8}  {'─'*8}  {'─'*8}")
    for sc, lbl in [
        ("P",   "Paper 100% fill, stop 0.65         "),
        ("H",   "Hold (atual, SA 75%)                "),
        ("ST",  "Stop 0.47 sem PP [IDEAL]            "),
        ("SA1", "SA1: entrada precoce secs>160 (90%) "),
        ("SA2", "SA2: bid passivo ep-0.01 (88%)      "),
        ("SA3", "SA3: qty=4 fill 95%                 "),
        ("PP",  "PP 0.88 parcial (removido, ref.)    "),
    ]:
        tot = _ee_total(sc)
        vh  = round(tot - base_h,  2)
        vst = round(tot - base_st, 2)
        marker = " <<< IDEAL" if sc == "ST" else (" ***" if sc in ("SA1","SA2") else "")
        print(f"  {lbl:<40} {tot:>+8.2f}  {vh:>+8.2f}  {vst:>+8.2f}{marker}")

    print()
    print(f"  Custo da SA (P vs H):    {round(base_h - _ee_total('P'), 2):>+.2f} USD / 10 dias")
    print(f"  Potencial de recuperacao:")
    print(f"    SA1 (entrada precoce): {round(_ee_total('SA1') - base_st, 2):>+.2f} vs ST ideal")
    print(f"    SA2 (bid passivo):     {round(_ee_total('SA2') - base_st, 2):>+.2f} vs ST ideal")

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
