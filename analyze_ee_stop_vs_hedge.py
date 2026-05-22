"""
analyze_ee_stop_vs_hedge.py

Análise de estratégias de saída para trades EE:
  1. Stop em vários níveis de bid (0.60 a 0.80)
  2. Hedge em 0.50 (comportamento atual)
  3. Profit protect: sair quando bid EL >= threshold em secs <= gate
  4. Combinação: stop + profit protect

Para cada trade lê os snapshots tick a tick e simula cada estratégia.

Uso:
    python analyze_ee_stop_vs_hedge.py [--log-dir logs/]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Parâmetros de simulação
# ---------------------------------------------------------------------------

STOP_LEVELS     = [0.60, 0.65, 0.67, 0.70, 0.72, 0.75, 0.78, 0.80]
PP_BID_LEVELS   = [0.85, 0.88, 0.90, 0.92]      # profit protect bid
PP_SECS_GATES   = [60, 70, 80]                   # profit protect secs threshold
QTY             = 6.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_session(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _collect_trades(rows: list[dict]) -> list[dict]:
    """Retorna lista de trades com metadata e snapshots durante o hold."""
    entries: dict[str, dict] = {}
    snaps_by_slug: dict[str, list[dict]] = {}

    trades = []

    for r in rows:
        t = r.get("type")

        if t == "ee_paper_entry":
            slug = r["slug"]
            entries[slug] = r
            snaps_by_slug[slug] = []

        elif t == "snapshot":
            slug = r.get("current_slug", "")
            if slug in entries:
                ep = r.get("ee_position")
                if ep and ep.get("state") in ("entry", "hedged"):
                    snaps_by_slug[slug].append({
                        "secs":   r.get("current_secs"),
                        "up_bid": (r.get("current_exec") or {}).get("up_bid", 0),
                        "dn_bid": (r.get("current_exec") or {}).get("down_bid", 0),
                        "state":  ep.get("state"),
                    })

        elif t == "ee_paper_closed":
            slug = r["slug"]
            if slug not in entries:
                continue
            ee = r.get("ee", {})
            if ee.get("outcome") in (None, "MISSED"):
                entries.pop(slug, None)
                snaps_by_slug.pop(slug, None)
                continue
            entry_r = entries.pop(slug)
            snaps   = snaps_by_slug.pop(slug, [])
            trades.append({
                "slug":       slug,
                "side":       ee["entry_side"],
                "ep":         ee["entry_ep"],
                "entry_secs": ee["entry_secs"],
                "hedge_ep":   ee.get("hedge_ep"),
                "hedge_secs": ee.get("hedge_secs"),
                "outcome":    ee["outcome"],
                "pnl_actual": ee["pnl"],
                "el_vel":     entry_r.get("el", {}).get("el_vel", 0),
                "snaps":      snaps,
            })

    return trades


def _el_bid(snap: dict, side: str) -> float:
    return snap["up_bid"] if side == "UP" else snap["dn_bid"]


def _opp_bid(snap: dict, side: str) -> float:
    return snap["dn_bid"] if side == "UP" else snap["up_bid"]


# ---------------------------------------------------------------------------
# Simulações por trade
# ---------------------------------------------------------------------------

def sim_stop(trade: dict, stop_level: float) -> dict:
    """Para quando bid EL cai abaixo de stop_level."""
    side = trade["side"]
    ep   = trade["ep"]
    for s in trade["snaps"]:
        if s["state"] != "entry":
            continue
        bid = _el_bid(s, side)
        if bid <= stop_level:
            exit_ep = stop_level  # assume sai no nível (stop limit)
            pnl = round((exit_ep - ep) * QTY, 4)
            return {"triggered": True, "exit_secs": s["secs"], "exit_ep": exit_ep, "pnl": pnl}
    # stop não ativou → resultado original
    return {"triggered": False, "pnl": trade["pnl_actual"]}


def sim_hedge(trade: dict, hedge_thr: float = 0.50) -> dict:
    """Comportamento atual: hedge quando bid EL < hedge_thr."""
    side = trade["side"]
    ep   = trade["ep"]
    for s in trade["snaps"]:
        if s["state"] != "entry":
            continue
        bid = _el_bid(s, side)
        opp = _opp_bid(s, side)
        if bid < hedge_thr and opp > 0:
            # simula hedge: entra no oposto ao preço opp, resolve a 1.0
            pnl_entry = (0.0 - ep) * QTY        # EL lado perdeu
            pnl_hedge = (1.0 - opp) * QTY       # lado oposto ganhou
            pnl = round(pnl_entry + pnl_hedge, 4)
            return {"triggered": True, "exit_secs": s["secs"], "hedge_ep": opp, "pnl": pnl}
    # hedge não ativou → resultado original
    return {"triggered": False, "pnl": trade["pnl_actual"]}


def sim_profit_protect(trade: dict, pp_bid: float, pp_secs: int) -> dict:
    """
    Profit protect: sai ao preço de mercado quando bid EL >= pp_bid em secs <= pp_secs.
    Usa o bid atual como exit price (não espera resolução).
    """
    side = trade["side"]
    ep   = trade["ep"]
    for s in trade["snaps"]:
        if s["state"] != "entry":
            continue
        secs = s["secs"] or 999
        bid  = _el_bid(s, side)
        if secs <= pp_secs and bid >= pp_bid:
            pnl = round((bid - ep) * QTY, 4)
            return {"triggered": True, "exit_secs": secs, "exit_ep": bid, "pnl": pnl}
    return {"triggered": False, "pnl": trade["pnl_actual"]}


def sim_stop_then_pp(trade: dict, stop_level: float, pp_bid: float, pp_secs: int) -> dict:
    """Combina stop + profit protect — o que ativar primeiro prevalece."""
    side = trade["side"]
    ep   = trade["ep"]
    for s in trade["snaps"]:
        if s["state"] != "entry":
            continue
        secs = s["secs"] or 999
        bid  = _el_bid(s, side)
        # profit protect tem prioridade se ativar antes do stop
        if secs <= pp_secs and bid >= pp_bid:
            pnl = round((bid - ep) * QTY, 4)
            return {"triggered": "pp", "exit_secs": secs, "exit_ep": bid, "pnl": pnl}
        if bid <= stop_level:
            pnl = round((stop_level - ep) * QTY, 4)
            return {"triggered": "stop", "exit_secs": secs, "exit_ep": stop_level, "pnl": pnl}
    return {"triggered": False, "pnl": trade["pnl_actual"]}


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

def _sep(n: int = 70) -> str:
    return "-" * n


def run(log_dir: str = "logs/") -> None:
    base = Path(log_dir)
    session_dirs = sorted(base.glob("ee_paper_*/"))
    all_trades: list[dict] = []

    for sd in session_dirs:
        lp = sd / "ee_paper.jsonl"
        if not lp.exists():
            continue
        rows   = _load_session(lp)
        trades = _collect_trades(rows)
        for t in trades:
            t["session"] = sd.name
        all_trades.extend(trades)

    if not all_trades:
        print("Nenhum trade encontrado.")
        return

    print(f"\n{'='*70}")
    print(f"  ANÁLISE EE — Stop vs Hedge vs Profit Protect")
    print(f"  {len(all_trades)} trades  |  QTY={QTY}")
    print(f"{'='*70}\n")

    # --- Tabela de trades (base) ---
    print("TRADES REAIS:")
    print(f"{'#':>3}  {'Slug':>12}  {'Side':>4}  {'EP':>5}  {'secs':>5}  {'vel':>6}  {'Outcome':>12}  {'PnL':>8}")
    print(_sep())
    for i, t in enumerate(all_trades, 1):
        slug = t["slug"].replace("btc-updown-5m-1779", "…")
        print(f"{i:>3}  {slug:>12}  {t['side']:>4}  {t['ep']:>5.2f}  "
              f"{t['entry_secs']:>5}  {t['el_vel']:>+6.3f}  "
              f"{t['outcome']:>12}  {t['pnl_actual']:>+8.2f}")

    pnl_base = sum(t["pnl_actual"] for t in all_trades)
    wr_base  = sum(1 for t in all_trades if t["pnl_actual"] > 0) / len(all_trades)
    print(_sep())
    print(f"  ATUAL  WR={wr_base:.1%}  PnL={pnl_base:>+.2f}\n")

    # --- Análise detalhada slug com WIN_HEDGE ---
    hedge_trades = [t for t in all_trades if t["outcome"] == "WIN_HEDGE"]
    if hedge_trades:
        print(f"\n{'='*70}")
        print("  DETALHE: trajectória do WIN_HEDGE tick a tick")
        print(f"{'='*70}")
        for t in hedge_trades:
            print(f"\n  Slug: {t['slug'].replace('btc-updown-5m-1779','…')}")
            print(f"  Side={t['side']}  EP={t['ep']}  entry_secs={t['entry_secs']}  el_vel={t['el_vel']:+.3f}")
            print(f"  Outcome={t['outcome']}  PnL_real={t['pnl_actual']:+.2f}")
            print(f"\n  {'secs':>5}  {'EL bid':>8}  {'OPP bid':>8}  {'estado':>8}  nota")
            prev_el = t["ep"]
            for s in t["snaps"]:
                el  = _el_bid(s, t["side"])
                opp = _opp_bid(s, t["side"])
                secs = s["secs"]
                nota = ""
                if abs(el - prev_el) >= 0.15:
                    nota = f"  <- GAP {el-prev_el:+.2f}"
                if el >= 0.88:
                    nota = "  <- PEAK"
                if el < 0.50:
                    nota = "  <- HEDGE zona"
                print(f"  {secs:>5}  {el:>8.3f}  {opp:>8.3f}  {s['state']:>8}{nota}")
                prev_el = el

            print(f"\n  Comparativo de saídas alternativas para este trade:")
            print(f"  {'Estratégia':30}  {'Exit':>6}  {'secs':>5}  {'PnL':>8}  vs hedge")
            print(f"  {'-'*60}")
            baseline = t["pnl_actual"]
            for sl in STOP_LEVELS:
                r = sim_stop(t, sl)
                delta = r["pnl"] - baseline
                trig  = f"secs={r.get('exit_secs','n/a')}" if r["triggered"] else "não ativou"
                print(f"  {'Stop @ ' + str(sl):30}  {trig:>6}  {'':>5}  {r['pnl']:>+8.2f}  {delta:>+8.2f}")
            for pp_b in PP_BID_LEVELS:
                for pp_s in PP_SECS_GATES:
                    r = sim_profit_protect(t, pp_b, pp_s)
                    delta = r["pnl"] - baseline
                    trig  = f"secs={r.get('exit_secs','n/a')}" if r["triggered"] else "não ativou"
                    print(f"  {'PP bid>=' + str(pp_b) + ' secs<=' + str(pp_s):30}  {trig:>6}  {'':>5}  {r['pnl']:>+8.2f}  {delta:>+8.2f}")
        print()

    # --- Simulação: stop em todos os trades ---
    print(f"\n{'='*70}")
    print("  SIMULAÇÃO STOP — impacto no portfólio completo")
    print(f"{'='*70}")
    print(f"\n  {'Stop nível':>12}  {'Ativações':>10}  {'WR':>6}  {'PnL total':>10}  vs base")
    print("  " + _sep(55))

    for sl in STOP_LEVELS:
        results = [sim_stop(t, sl) for t in all_trades]
        pnls    = [r["pnl"] for r in results]
        total   = round(sum(pnls), 4)
        wins    = sum(1 for p in pnls if p > 0)
        trigs   = sum(1 for r in results if r["triggered"])
        wr      = wins / len(all_trades)
        delta   = total - pnl_base
        print(f"  {sl:>12.2f}  {trigs:>10}  {wr:>6.1%}  {total:>+10.2f}  {delta:>+8.2f}")

    # --- Simulação: profit protect ---
    print(f"\n{'='*70}")
    print("  SIMULAÇÃO PROFIT PROTECT — impacto no portfólio completo")
    print(f"{'='*70}")
    print(f"\n  {'PP bid':>8}  {'PP secs':>8}  {'Ativações':>10}  {'WR':>6}  {'PnL total':>10}  vs base")
    print("  " + _sep(60))

    for pp_b in PP_BID_LEVELS:
        for pp_s in PP_SECS_GATES:
            results = [sim_profit_protect(t, pp_b, pp_s) for t in all_trades]
            pnls    = [r["pnl"] for r in results]
            total   = round(sum(pnls), 4)
            wins    = sum(1 for p in pnls if p > 0)
            trigs   = sum(1 for r in results if r["triggered"])
            wr      = wins / len(all_trades)
            delta   = total - pnl_base
            print(f"  {pp_b:>8}  {pp_s:>8}  {trigs:>10}  {wr:>6.1%}  {total:>+10.2f}  {delta:>+8.2f}")

    # --- Combinação stop + PP (melhor candidato) ---
    print(f"\n{'='*70}")
    print("  COMBINAÇÃO: Stop 0.65 + Profit Protect (melhores candidatos)")
    print(f"{'='*70}")
    print(f"\n  {'Stop':>6}  {'PP bid':>8}  {'PP secs':>8}  {'Ativações':>10}  {'WR':>6}  {'PnL total':>10}  vs base")
    print("  " + _sep(65))

    for sl in [0.65, 0.70]:
        for pp_b in PP_BID_LEVELS:
            for pp_s in PP_SECS_GATES:
                results = [sim_stop_then_pp(t, sl, pp_b, pp_s) for t in all_trades]
                pnls    = [r["pnl"] for r in results]
                total   = round(sum(pnls), 4)
                wins    = sum(1 for p in pnls if p > 0)
                trigs   = sum(1 for r in results if r["triggered"])
                wr      = wins / len(all_trades)
                delta   = total - pnl_base
                print(f"  {sl:>6}  {pp_b:>8}  {pp_s:>8}  {trigs:>10}  {wr:>6.1%}  {total:>+10.2f}  {delta:>+8.2f}")

    # --- Resumo da trajectória para todos os trades ---
    print(f"\n{'='*70}")
    print("  TRAJECTÓRIA: min/max bid EL durante o hold por trade")
    print(f"{'='*70}")
    print(f"\n  {'#':>3}  {'Slug':>12}  {'EP':>5}  {'min_bid':>8}  {'max_bid':>8}  {'max_secs':>9}  {'gap_max':>8}  {'Outcome':>12}")
    print("  " + _sep(80))

    for i, t in enumerate(all_trades, 1):
        snaps_entry = [s for s in t["snaps"] if s["state"] == "entry"]
        if not snaps_entry:
            slug = t["slug"].replace("btc-updown-5m-1779", "…")
            print(f"  {i:>3}  {slug:>12}  sem snaps de entry")
            continue

        bids = [_el_bid(s, t["side"]) for s in snaps_entry]
        secs_list = [s["secs"] or 0 for s in snaps_entry]
        min_b = min(bids)
        max_b = max(bids)
        max_s = secs_list[bids.index(max_b)]

        # maior queda entre polls consecutivos (gap)
        gaps = [abs(bids[i] - bids[i-1]) for i in range(1, len(bids))]
        max_gap = max(gaps) if gaps else 0

        slug = t["slug"].replace("btc-updown-5m-1779", "…")
        print(f"  {i:>3}  {slug:>12}  {t['ep']:>5.2f}  {min_b:>8.3f}  {max_b:>8.3f}  "
              f"{max_s:>9}  {max_gap:>8.3f}  {t['outcome']:>12}")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", default="logs/", help="Diretório base dos logs EE")
    args = parser.parse_args()
    run(log_dir=args.log_dir)
