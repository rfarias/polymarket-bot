"""
_sim_ee_gates.py

Simula diferentes configurações de gates EE sobre os snapshots do paper local.

Para cada config, determina:
  - quantos trades teriam entrado
  - qual seria o resultado (WIN / REVERSAL / STOP / PP)
  - WR e PnL total

Configs testadas:
  A. Atual     (spread<0.70 + n180<3 + n180==5 + hora==6)
  B. No-spread (remove gate spread)
  C. Spread<0.75
  D. Spread<0.80
  E. Só-vel    (apenas vel>=VEL_MIN, remove tudo)
  F. Sem_5     (remove apenas gate n180==5)
  G. Sem_hora  (remove apenas gate hora==6)

Uso:
  python _sim_ee_gates.py
  python _sim_ee_gates.py --vel 0.13   # testar com VEL_MIN diferente
  python _sim_ee_gates.py --qty 6
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import io
from collections import defaultdict
from datetime import datetime, timezone, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Parâmetros
# ---------------------------------------------------------------------------
EE_VEL_MIN    = 0.17
EE_ENTRY_LO   = 0.82
EE_ENTRY_HI   = 0.86
EE_STOP_LEVEL = 0.65
EE_PP_BID     = 0.88
EE_PP_SECS_HI = 70
QTY           = 6.0
LOG_PATTERN   = "logs/ee_paper_*/ee_paper.jsonl"

TZ_LOCAL = timezone(timedelta(hours=-3))

# ---------------------------------------------------------------------------
# Carregamento de snapshots
# ---------------------------------------------------------------------------

def load_snaps(pattern: str) -> dict[str, list[dict]]:
    """Retorna {slug: [snap, ...]} com snaps ordenados por secs desc."""
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"Nenhum log encontrado: {pattern}")

    by_slug: dict[str, list[dict]] = defaultdict(list)
    seen: set = set()

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
                if ev.get("type") != "snapshot":
                    continue
                slug = ev.get("current_slug", "")
                secs = ev.get("current_secs")
                if not slug or secs is None:
                    continue
                key = (slug, int(secs), round(ev.get("ts", 0)))
                if key in seen:
                    continue
                seen.add(key)
                by_slug[slug].append(ev)

    # Ordena cada slug por secs desc (180→0), simula passagem do tempo
    for slug in by_slug:
        by_slug[slug].sort(key=lambda x: x["current_secs"], reverse=True)

    return dict(by_slug)


# ---------------------------------------------------------------------------
# Gate config
# ---------------------------------------------------------------------------

def is_blocked(ev: dict, el_bid: float, opp_bid: float, secs: int,
               spread_thr: float | None, no_n5: bool, no_hora: bool) -> tuple[bool, str]:
    """Retorna (bloqueado, reason)."""
    el = ev.get("early_leader") or {}
    n_s180 = el.get("n_s180", 0)
    ts = ev.get("ts", 0)
    utc_hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour

    leader_spread = round(el_bid - opp_bid, 4)

    # gate n180<3 (sempre ativo — já está no runner real)
    if n_s180 < 3:
        return True, f"n180_lt3:{n_s180}<3"

    # gate n180==5 (candidato)
    if (not no_n5) and n_s180 == 5:
        return True, f"n180_5:{n_s180}==5"

    # gate hora UTC=6 (candidato)
    if (not no_hora) and utc_hour == 6:
        return True, f"hora_06utc:{utc_hour}"

    # gate spread (ajustável)
    if spread_thr is not None and leader_spread >= spread_thr:
        return True, f"spread:{leader_spread:.3f}>={spread_thr}"

    # gate ep_vel (sempre ativo)
    el_vel = el.get("el_vel", 0.0)
    if el_bid >= 0.86 and el_vel < 0.13:
        return True, f"ep_vel:ep={el_bid:.3f},vel={el_vel:.4f}"

    return False, ""


# ---------------------------------------------------------------------------
# Simulação para um slug
# ---------------------------------------------------------------------------

def simulate_slug(snaps: list[dict], vel_min: float,
                  spread_thr: float | None, no_n5: bool, no_hora: bool,
                  qty: float) -> dict | None:
    """
    Simula uma entrada EE com hold-to-resolution + stop + profit_protect.
    Retorna dict do trade ou None.
    """
    entry_snap = None
    entry_ep   = 0.0
    el_side    = None
    entry_secs = 0

    for snap in snaps:
        el   = snap.get("early_leader") or {}
        exc  = snap.get("current_exec") or {}
        secs = int(snap.get("current_secs", 0))

        if not el.get("signal_ok"):
            continue
        if secs < 30 or secs > 180:
            continue

        side    = el.get("early_side")
        el_bid  = exc.get("up_bid", 0.0) if side == "UP" else exc.get("down_bid", 0.0)
        opp_bid = exc.get("down_bid", 0.0) if side == "UP" else exc.get("up_bid", 0.0)

        if el.get("el_vel", 0.0) < vel_min:
            continue
        if not (EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI):
            continue

        blocked, _ = is_blocked(snap, el_bid, opp_bid, secs, spread_thr, no_n5, no_hora)
        if blocked:
            continue

        entry_snap = snap
        entry_ep   = el_bid
        el_side    = side
        entry_secs = secs
        break  # primeira entrada válida

    if entry_snap is None or el_side is None:
        return None

    # Agora simula o que acontece após a entrada
    # Snaps após a entrada (secs menores)
    post_snaps = [s for s in snaps if int(s.get("current_secs", 999)) < entry_secs]
    post_snaps.sort(key=lambda x: x["current_secs"])  # crescente = secs mais baixo primeiro

    outcome = None
    exit_price = 0.0

    for snap in post_snaps:
        exc  = snap.get("current_exec") or {}
        secs = int(snap.get("current_secs", 0))

        el_bid_now  = exc.get("up_bid", 0.0) if el_side == "UP" else exc.get("down_bid", 0.0)
        opp_bid_now = exc.get("down_bid", 0.0) if el_side == "UP" else exc.get("up_bid", 0.0)

        # Stop loss
        if el_bid_now < EE_STOP_LEVEL:
            outcome    = "STOP"
            exit_price = el_bid_now
            break

        # Profit protect
        if EE_PP_SECS_HI >= secs >= 36 and el_bid_now >= EE_PP_BID:
            outcome    = "PP"
            exit_price = el_bid_now
            break

        # Resolução
        if secs <= 35:
            if el_bid_now >= 0.85:
                outcome    = "WIN"
                exit_price = 1.0
            elif opp_bid_now >= 0.85:
                outcome    = "REVERSAL"
                exit_price = 0.0
            else:
                outcome    = "WIN"   # parcialmente resolvido, assume win se EL ainda lidera
                exit_price = el_bid_now
            break

    if outcome is None:
        return None  # não resolveu nos dados disponíveis

    pnl = round((exit_price - entry_ep) * qty, 4)

    return {
        "el_side":    el_side,
        "ep":         round(entry_ep, 4),
        "exit_price": round(exit_price, 4),
        "entry_secs": entry_secs,
        "outcome":    outcome,
        "pnl":        pnl,
        "ts":         entry_snap.get("ts", 0),
        "el_vel":     (entry_snap.get("early_leader") or {}).get("el_vel", 0),
        "n_s180":     (entry_snap.get("early_leader") or {}).get("n_s180", 0),
    }


# ---------------------------------------------------------------------------
# Runner de uma config
# ---------------------------------------------------------------------------

def run_config(label: str, by_slug: dict, vel_min: float,
               spread_thr: float | None, no_n5: bool, no_hora: bool,
               qty: float, verbose: bool = False) -> dict:
    trades = []
    for slug, snaps in sorted(by_slug.items()):
        res = simulate_slug(snaps, vel_min, spread_thr, no_n5, no_hora, qty)
        if res:
            res["slug"] = slug
            trades.append(res)

    n     = len(trades)
    wins  = sum(1 for t in trades if t["pnl"] > 0)
    losses = n - wins
    total_pnl = round(sum(t["pnl"] for t in trades), 2)
    avg   = round(total_pnl / n, 3) if n else 0.0
    wr    = round(wins / n * 100, 1) if n else 0.0

    return {
        "label":     label,
        "n":         n,
        "wins":      wins,
        "losses":    losses,
        "wr":        wr,
        "total_pnl": total_pnl,
        "avg":       avg,
        "trades":    trades,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", default=LOG_PATTERN)
    parser.add_argument("--vel",  type=float, default=EE_VEL_MIN)
    parser.add_argument("--qty",  type=float, default=QTY)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print(f"Carregando snapshots: {args.logs}")
    by_slug = load_snaps(args.logs)
    print(f"  {len(by_slug)} slugs únicos\n")

    qty     = args.qty
    vel_min = args.vel

    # Gates candidatos testados
    configs = [
        # label, spread_thr,  no_n5,  no_hora
        ("A. Atual (spread<0.70 + n5 + hora6)", 0.70,  False, False),
        ("B. Sem spread",                        None,  False, False),
        ("C. Spread<0.75",                       0.75,  False, False),
        ("D. Spread<0.80",                       0.80,  False, False),
        ("E. Só vel (remove tudo exceto n<3)",   None,  True,  True),
        ("F. Sem gate n180==5",                  0.70,  True,  False),
        ("G. Sem gate hora==6",                  0.70,  False, True),
    ]

    results = []
    for label, spread_thr, no_n5, no_hora in configs:
        r = run_config(label, by_slug, vel_min, spread_thr, no_n5, no_hora, qty)
        results.append(r)

    # Tabela resumo
    print("=" * 80)
    print(f"SIMULAÇÃO EE — VEL>={vel_min}  QTY={qty}  ({len(by_slug)} slugs)")
    print("=" * 80)
    print(f"  {'Config':<44} {'N':>4} {'W':>4} {'L':>4} {'WR%':>6} {'TotalPnL':>10} {'Avg':>7}")
    print(f"  {'-'*44} {'-'*4} {'-'*4} {'-'*4} {'-'*6} {'-'*10} {'-'*7}")
    for r in results:
        print(f"  {r['label']:<44} {r['n']:>4} {r['wins']:>4} {r['losses']:>4} "
              f"{r['wr']:>5.1f}% {r['total_pnl']:>+10.2f} {r['avg']:>+7.3f}")

    # Detalhe das configs com mais trades
    best = max(results, key=lambda x: x["n"])
    if best["n"] > 0 and args.verbose:
        print(f"\n\n{'═'*70}")
        print(f"DETALHE — {best['label']}")
        print(f"{'═'*70}")
        print(f"  {'Slug[-20:]':20} {'EL':4} {'ep':5} {'exit':5} {'scs':4} {'vel':6} {'n180':5} {'outcome':10} {'PnL':>8}")
        print(f"  {'-'*20} {'-'*4} {'-'*5} {'-'*5} {'-'*4} {'-'*6} {'-'*5} {'-'*10} {'-'*8}")
        for t in sorted(best["trades"], key=lambda x: x["ts"]):
            dt = datetime.fromtimestamp(t["ts"], tz=TZ_LOCAL).strftime("%m-%d %H:%M")
            print(f"  {t['slug'][-20:]:20} {t['el_side']:4} {t['ep']:.3f} "
                  f"{t['exit_price']:.3f} {t['entry_secs']:>4} {t['el_vel']:>6.3f} "
                  f"{t['n_s180']:>5} {t['outcome']:<10} {t['pnl']:>+8.4f}")

    # Outcomes breakdown da config sem spread
    no_spread = next(r for r in results if r["label"].startswith("B."))
    if no_spread["n"] > 0:
        outcomes: dict[str, list] = defaultdict(list)
        for t in no_spread["trades"]:
            outcomes[t["outcome"]].append(t["pnl"])

        print(f"\n\nOutcomes — Config B (Sem spread, {no_spread['n']} trades):")
        print(f"  {'Outcome':<12} {'N':>4} {'WR%':>6} {'TotalPnL':>10} {'Avg':>7}")
        for outcome, pnls in sorted(outcomes.items()):
            n = len(pnls)
            w = sum(1 for p in pnls if p > 0)
            wr_o = round(w / n * 100, 1)
            total = round(sum(pnls), 2)
            avg_o = round(total / n, 3)
            print(f"  {outcome:<12} {n:>4} {wr_o:>5.1f}% {total:>+10.2f} {avg_o:>+7.3f}")


if __name__ == "__main__":
    main()
