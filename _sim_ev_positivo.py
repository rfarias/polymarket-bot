"""
_sim_ev_positivo.py

Simula o ganho total em ticks e USD considerando entradas sempre que
um dos filtros EV+ passar, com qty=6 cotas.

Filtros EV+ aplicados (baseados em RELATORIO_VALOR_EV.md v2):

  AR Standard:           entry_price in [0.91, 0.96]
  AR Dual Rich Late:     entry_price == 0.98
  EE vel>=0.17:          ep in [0.83, 0.86]  (0.82 excluído, EV≈0)

Fonte dos dados:
  AR paper: logs/current_almost_resolved_paper_202605*.jsonl
            logs/current_almost_resolved_paper_202606*.jsonl
  EE sim:   _sim_ee_gates.py (snapshots paper, sem gate spread, sem n5/hora)

Ticks: 1 tick = $0.01/share. PnL_USD = ticks * 0.01 * qty.

Uso:
  python _sim_ev_positivo.py
  python _sim_ev_positivo.py --qty 6      # default
  python _sim_ev_positivo.py --qty 50     # para comparar com o paper real
  python _sim_ev_positivo.py --detail     # mostra cada trade
"""
from __future__ import annotations
import argparse
import glob
import json
import sys
import io
from collections import defaultdict
from datetime import datetime, timezone, timedelta

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

TZ_LOCAL = timezone(timedelta(hours=-3))
QTY_PAPER = 50.0  # qty usada nos logs AR paper

# ---------------------------------------------------------------------------
# Filtros EV+
# ---------------------------------------------------------------------------

# AR: faixas de preco EV+
AR_EV_STANDARD_LO  = 0.91
AR_EV_STANDARD_HI  = 0.96
AR_EV_DUALRICH     = 0.98

# EE: faixa de ep EV+ (excluindo 0.82 que é EV≈0)
EE_EV_EP_LO = 0.83
EE_EV_EP_HI = 0.86
EE_VEL_MIN  = 0.17

# ---------------------------------------------------------------------------
# Carregamento AR paper
# ---------------------------------------------------------------------------

def load_ar_exits(patterns: list[str]) -> list[dict]:
    files = []
    for p in patterns:
        files.extend(sorted(glob.glob(p)))
    files = sorted(set(files))

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

                if ticks is None or quote is None:
                    continue

                # Aplica filtro EV+
                is_standard   = variant == "standard"
                is_dualrich   = variant == "dual_rich_late_limit"

                ev_pos = (
                    (is_standard  and AR_EV_STANDARD_LO <= ep <= AR_EV_STANDARD_HI) or
                    (is_dualrich  and abs(ep - AR_EV_DUALRICH) < 0.005)
                )
                if not ev_pos:
                    continue

                trades.append({
                    "source":  "AR",
                    "variant": variant,
                    "ep":      ep,
                    "xp":      xp,
                    "ticks":   float(ticks),
                    "quote_paper": float(quote),  # qty=50
                    "reason":  reason,
                    "ts":      ts,
                })

    return trades


# ---------------------------------------------------------------------------
# Carregamento EE (via simulação sobre snapshots)
# ---------------------------------------------------------------------------

def load_ee_sim(qty: float) -> list[dict]:
    """Roda _sim_ee_gates sobre snapshots e retorna trades EV+."""
    try:
        from _sim_ee_gates import load_snaps, simulate_slug
    except ImportError:
        print("[AVISO] _sim_ee_gates.py não encontrado — EE ignorado")
        return []

    by_slug = load_snaps("logs/ee_paper_*/ee_paper.jsonl")
    trades = []
    for slug, snaps in sorted(by_slug.items()):
        r = simulate_slug(
            snaps,
            vel_min   = EE_VEL_MIN,
            spread_thr= None,   # sem gate spread
            no_n5     = True,   # sem gate n180==5
            no_hora   = True,   # sem gate hora==6
            qty       = qty,
        )
        if r is None:
            continue
        ep = r["ep"]
        if not (EE_EV_EP_LO <= ep <= EE_EV_EP_HI):
            continue

        # Calcula ticks: (exit_price - entry_price) / 0.01
        ticks = round((r["exit_price"] - ep) / 0.01, 2)

        trades.append({
            "source":       "EE",
            "variant":      f"ee_vel{r['el_vel']:.2f}",
            "ep":           ep,
            "xp":           r["exit_price"],
            "ticks":        ticks,
            "quote_paper":  r["pnl"],  # já em qty especificada
            "pnl_usd":      r["pnl"],
            "reason":       r["outcome"],
            "ts":           r["ts"],
        })

    return trades


# ---------------------------------------------------------------------------
# Estatísticas por grupo
# ---------------------------------------------------------------------------

def group_stats(trades: list[dict], qty: float, label: str) -> dict:
    n      = len(trades)
    wins   = sum(1 for t in trades if t["ticks"] > 0)
    losses = n - wins
    total_ticks = sum(t["ticks"] for t in trades)

    # USD: para AR, escala de qty=50 para qty desejada
    usd_list = []
    for t in trades:
        if t["source"] == "AR":
            usd_list.append(t["quote_paper"] * (qty / QTY_PAPER))
        else:
            usd_list.append(t["pnl_usd"])
    total_usd = round(sum(usd_list), 4)

    wr       = round(wins / n * 100, 1) if n else 0.0
    avg_tick = round(total_ticks / n, 3) if n else 0.0
    avg_usd  = round(total_usd / n, 4)  if n else 0.0

    return {
        "label":       label,
        "n":           n,
        "wins":        wins,
        "losses":      losses,
        "wr":          wr,
        "total_ticks": round(total_ticks, 2),
        "total_usd":   total_usd,
        "avg_tick":    avg_tick,
        "avg_usd":     avg_usd,
        "usd_list":    usd_list,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qty",    type=float, default=6.0)
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--from-date", default="20260526",
                        help="Filtrar AR paper a partir desta data (YYYYMMDD)")
    args = parser.parse_args()

    qty       = args.qty
    from_date = args.from_date

    # Padroes de log AR
    ar_patterns = [
        f"logs/current_almost_resolved_paper_{from_date[:6]}*.jsonl",
        f"logs/current_almost_resolved_paper_{from_date[:4]}06*.jsonl",
    ]
    # Filtrar por data minima no nome do arquivo
    ar_patterns_exact = []
    for p in ar_patterns:
        for f in sorted(glob.glob(p)):
            # Extrai data do nome
            import re
            m = re.search(r'_(\d{8})_', f)
            if m and m.group(1) >= from_date:
                ar_patterns_exact.append(f)

    print(f"Parametros: QTY={qty} | AR a partir de {from_date}")
    print(f"Filtros EV+: AR std [0.91,0.96] | AR dual_rich 0.98 | EE vel>={EE_VEL_MIN} ep [{EE_EV_EP_LO},{EE_EV_EP_HI}]")
    print()

    # Carrega dados
    print("Carregando AR paper...")
    ar_trades = load_ar_exits(ar_patterns_exact)
    print(f"  {len(ar_trades)} exits filtrados")

    print("Carregando EE simulacao...")
    ee_trades = load_ee_sim(qty)
    print(f"  {len(ee_trades)} trades simulados")
    print()

    all_trades = ar_trades + ee_trades

    # ---------- Grupos ----------

    ar_std   = [t for t in ar_trades if t["variant"] == "standard"]
    ar_dual  = [t for t in ar_trades if "dual_rich" in t["variant"]]
    ee_all   = ee_trades

    groups = [
        group_stats(ar_std,  qty, "AR Standard (0.91-0.96)"),
        group_stats(ar_dual, qty, "AR Dual Rich (0.98)    "),
        group_stats(ee_all,  qty, "EE vel>=0.17 (0.83-0.86)"),
        group_stats(all_trades, qty, "TOTAL COMBINADO        "),
    ]

    # ---------- Sub-grupos AR por preço ----------
    ar_by_ep: dict[str, list] = defaultdict(list)
    for t in ar_trades:
        band = f"{t['ep']:.2f}"
        ar_by_ep[band].append(t)

    # ---------- Sub-grupos EE por ep ----------
    ee_by_ep: dict[str, list] = defaultdict(list)
    for t in ee_trades:
        band = f"{t['ep']:.2f}"
        ee_by_ep[band].append(t)

    # ---------- Relatório ----------
    LINE = "=" * 78

    print(LINE)
    print(f"  SIMULACAO EV+  —  QTY={qty} cotas  —  1 tick = ${qty*0.01:.2f}")
    print(LINE)
    print(f"  {'Setup':<28} {'N':>5} {'W':>5} {'L':>5} {'WR%':>6} {'Ticks':>8} {'USD':>10} {'Avg/trade':>10}")
    print(f"  {'-'*28} {'-'*5} {'-'*5} {'-'*5} {'-'*6} {'-'*8} {'-'*10} {'-'*10}")
    for g in groups:
        sep = "  " + ("─"*76) if g["label"].startswith("TOTAL") else ""
        if sep:
            print(sep)
        print(
            f"  {g['label']:<28} {g['n']:>5} {g['wins']:>5} {g['losses']:>5} "
            f"{g['wr']:>5.1f}% {g['total_ticks']:>+8.1f} {g['total_usd']:>+10.2f} {g['avg_usd']:>+10.4f}"
        )

    # ---------- AR por faixa de preço ----------
    print(f"\n{'─'*78}")
    print("  AR — DETALHE POR FAIXA DE PRECO")
    print(f"{'─'*78}")
    print(f"  {'ep':>5} {'variant':<26} {'N':>5} {'W':>4} {'L':>4} {'WR%':>6} {'Ticks':>7} {'USD':>9} {'Avg':>8}")
    print(f"  {'─'*5} {'─'*26} {'─'*5} {'─'*4} {'─'*4} {'─'*6} {'─'*7} {'─'*9} {'─'*8}")

    # Por variant e ep
    ar_by_variant_ep: dict[tuple, list] = defaultdict(list)
    for t in ar_trades:
        ar_by_variant_ep[(t["variant"], f"{t['ep']:.2f}")].append(t)

    for (variant, ep_str), grp in sorted(ar_by_variant_ep.items()):
        g = group_stats(grp, qty, "")
        short_v = variant.replace("_late_limit", "").replace("_", " ")
        print(
            f"  {ep_str:>5}  {short_v:<26} {g['n']:>5} {g['wins']:>4} {g['losses']:>4} "
            f"{g['wr']:>5.1f}% {g['total_ticks']:>+7.1f} {g['total_usd']:>+9.2f} {g['avg_usd']:>+8.4f}"
        )

    # ---------- EE por ep ----------
    print(f"\n{'─'*78}")
    print("  EE — DETALHE POR PRECO DE ENTRADA")
    print(f"{'─'*78}")
    print(f"  {'ep':>5} {'N':>5} {'W':>4} {'L':>4} {'WR%':>6} {'Ticks':>7} {'USD':>9} {'Avg':>8} {'EV':>7}")
    print(f"  {'─'*5} {'─'*5} {'─'*4} {'─'*4} {'─'*6} {'─'*7} {'─'*9} {'─'*8} {'─'*7}")
    for ep_str, grp in sorted(ee_by_ep.items()):
        g = group_stats(grp, qty, "")
        ep_f = float(ep_str)
        ev = round(g['wr']/100 - ep_f, 4)
        print(
            f"  {ep_str:>5} {g['n']:>5} {g['wins']:>4} {g['losses']:>4} "
            f"{g['wr']:>5.1f}% {g['total_ticks']:>+7.1f} {g['total_usd']:>+9.2f} {g['avg_usd']:>+8.4f} {ev:>+7.4f}"
        )

    # ---------- Detalhe por exit reason ----------
    print(f"\n{'─'*78}")
    print("  POR MOTIVO DE SAIDA (todos setups)")
    print(f"{'─'*78}")
    by_reason: dict[str, list] = defaultdict(list)
    for t in all_trades:
        by_reason[t["reason"]].append(t)
    for reason, grp in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        g = group_stats(grp, qty, "")
        print(f"  {reason:<22} n={g['n']:>4} W={g['wins']:>3} L={g['losses']:>2} WR={g['wr']:>5.1f}% "
              f"ticks={g['total_ticks']:>+8.1f} usd={g['total_usd']:>+9.2f}")

    # ---------- Projeção ----------
    total_g = groups[-1]
    if total_g["n"] > 0:
        # Periodo coberto
        all_ts = [t["ts"] for t in all_trades if t["ts"] > 0]
        if all_ts:
            ts_min = min(all_ts)
            ts_max = max(all_ts)
            days = (ts_max - ts_min) / 86400
            trades_per_day = total_g["n"] / days if days > 0 else 0
            usd_per_day    = total_g["total_usd"] / days if days > 0 else 0
        else:
            days = trades_per_day = usd_per_day = 0

        print(f"\n{'═'*78}")
        print(f"  RESUMO FINAL  —  QTY={qty} cotas  —  1 tick = ${qty*0.01:.2f}")
        print(f"{'═'*78}")
        print(f"  Periodo coberto:     {days:.1f} dias")
        print(f"  Total de trades:     {total_g['n']} ({total_g['wins']}W / {total_g['losses']}L)")
        print(f"  Win rate:            {total_g['wr']}%")
        print(f"  Ticks totais:        {total_g['total_ticks']:>+.1f} ticks")
        print(f"  USD total:           ${total_g['total_usd']:>+.2f}")
        print(f"  Avg por trade:       ${total_g['avg_usd']:>+.4f}  /  {total_g['avg_tick']:>+.3f} ticks")
        if trades_per_day > 0:
            print(f"  Ritmo:               {trades_per_day:.1f} trades/dia  |  ${usd_per_day:>+.2f}/dia")
            print(f"  Projecao mensal:     ${usd_per_day*21:>+.2f}  (21 dias uteis)")
        print(f"{'═'*78}")

    # ---------- Detalhe opcional ----------
    if args.detail:
        print(f"\n\n{'─'*90}")
        print("  TODOS OS TRADES (cronologico)")
        print(f"{'─'*90}")
        print(f"  {'Data':>11} {'Setup':<10} {'ep':>5} {'xp':>5} {'ticks':>6} {'usd':>8} {'reason'}")
        print(f"  {'─'*11} {'─'*10} {'─'*5} {'─'*5} {'─'*6} {'─'*8} {'─'*15}")
        for t in sorted(all_trades, key=lambda x: x["ts"]):
            dt = datetime.fromtimestamp(t["ts"], tz=TZ_LOCAL).strftime("%m-%d %H:%M") if t["ts"] else "?"
            usd = (t["quote_paper"] * (qty / QTY_PAPER)) if t["source"] == "AR" else t.get("pnl_usd", 0)
            src = t["source"] + ("_std" if t.get("variant") == "standard" else "_dr" if "dual" in t.get("variant","") else "_ee")
            print(f"  {dt:>11} {src:<10} {t['ep']:>5.3f} {t['xp']:>5.3f} {t['ticks']:>+6.2f} {usd:>+8.4f}  {t['reason']}")


if __name__ == "__main__":
    main()
