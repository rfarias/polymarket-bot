"""
_sim_sniper_precursor.py

Simula o sniper de reversão usando o gate de precursor
(el_vel < 0.05 AND F3 falhou) nos logs do monitor.

Para cada slug que entra na janela do sniper (winner >= 0.88, loser <= 0.15,
secs 15-120), simula uma compra do lado perdedor e verifica se o mercado
reverteu. Compara: baseline vs com filtro de precursor ativo.

Uso:
    python _sim_sniper_precursor.py
    python _sim_sniper_precursor.py --qty 6
    python _sim_sniper_precursor.py --vel-max 0.05 --min-winner 0.88 --max-loser 0.12
"""
from __future__ import annotations

import argparse, json, sys, io
from pathlib import Path
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Parâmetros do sniper
MIN_WINNER   = 0.88
MAX_LOSER    = 0.15
MIN_SECS     = 15
MAX_SECS     = 120

# Parâmetros do precursor
VEL_MAX      = 0.05   # el_vel abaixo disso = EL fraco
F3_FAIL      = True   # F3 falhou = bid dip < 0.70 na janela 181-121

QTY_DEFAULT  = 6.0


def _sf(v, d=0.0):
    try: return float(v)
    except: return d


# ---------------------------------------------------------------------------
# Carrega slugs dos logs do monitor (campos EL pré-calculados)
# ---------------------------------------------------------------------------

def load_slugs(log_dir="logs"):
    by_slug = defaultdict(list)
    for path in sorted(Path(log_dir).glob("market_monitor_*.jsonl")):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try: d = json.loads(line)
                except: continue
                if d.get("type") != "monitor_snap" or d.get("slot") != "current":
                    continue
                slug = d.get("slug")
                secs = d.get("secs")
                if not slug or secs is None:
                    continue
                by_slug[slug].append({
                    "secs":       int(secs),
                    "up_bid":     _sf(d.get("up_bid")),
                    "down_bid":   _sf(d.get("down_bid")),
                    "el_leader":  d.get("el_leader"),
                    "el_vel":     _sf(d.get("el_vel")),
                    "el_cont_ok": bool(d.get("el_cont_ok")),
                    "ts":         _sf(d.get("ts")),
                })
    result = {}
    for slug, snaps in by_slug.items():
        seen = set()
        unique = []
        for s in sorted(snaps, key=lambda x: x["ts"]):
            if s["secs"] not in seen:
                seen.add(s["secs"])
                unique.append(s)
        result[slug] = unique
    return result


# ---------------------------------------------------------------------------
# Extrai estado do EL de um slug (usando campos do monitor)
# ---------------------------------------------------------------------------

def get_el_state(snaps):
    """Retorna dict com el_vel, had_f3_dip, early_leader para o slug."""
    s240 = [s for s in snaps if 181 <= s["secs"] <= 240 and s.get("el_leader")]
    s180 = [s for s in snaps if 121 <= s["secs"] <= 180 and s.get("el_leader")]

    if not s240 and not s180:
        return None

    # Pega último snapshot com campos EL preenchidos
    candidates = sorted([s for s in snaps if s.get("el_leader")], key=lambda x: x["secs"])
    if not candidates:
        return None

    # el_vel: pega do snapshot da janela 180 (calculado pelo tracker)
    el_vel_vals = [s["el_vel"] for s in s180 if s.get("el_vel") is not None]
    el_vel = el_vel_vals[-1] if el_vel_vals else (s240[-1]["el_vel"] if s240 else None)

    # el_cont_ok: False se algum snap na janela 180 marcou False
    cont_vals = [s["el_cont_ok"] for s in s180]
    had_f3_dip = (False in cont_vals) if cont_vals else None

    el_leader = candidates[-1]["el_leader"]

    return {
        "el_leader":  el_leader,
        "el_vel":     el_vel,
        "had_f3_dip": had_f3_dip,
    }


# ---------------------------------------------------------------------------
# Simulação principal
# ---------------------------------------------------------------------------

def simulate(snaps, min_winner, max_loser, qty, el_state):
    """
    Tenta entrar como sniper: compra o lado perdedor na primeira oportunidade
    dentro da janela (winner >= min_winner, loser <= max_loser, secs 15-120).

    Retorna dict com resultado ou None se não houve oportunidade.
    """
    # Ordenar por secs decrescente (maior secs = mais cedo no tempo real)
    snaps_sorted = sorted(snaps, key=lambda x: x["secs"], reverse=True)

    entry = None
    entry_side = None
    entry_price = None

    for s in snaps_sorted:
        if not (MIN_SECS <= s["secs"] <= MAX_SECS):
            continue
        winner_side = "UP" if s["up_bid"] >= s["down_bid"] else "DOWN"
        winner_bid  = max(s["up_bid"], s["down_bid"])
        loser_side  = "DOWN" if winner_side == "UP" else "UP"
        loser_price = round(1.0 - winner_bid, 4)

        if winner_bid >= min_winner and 0 < loser_price <= max_loser:
            entry       = s
            entry_side  = loser_side   # sniper compra o perdedor
            entry_price = loser_price
            break

    if entry is None:
        return None

    # Determinar outcome: qual lado venceu?
    final_snaps = [s for s in snaps if s["secs"] <= 10]
    if not final_snaps:
        return None
    last = max(final_snaps, key=lambda x: x["secs"])
    winner_at_end = "UP" if last["up_bid"] >= last["down_bid"] else "DOWN"

    reversal = (entry_side == winner_at_end)
    pnl_per_share = (1.0 - entry_price) if reversal else -entry_price
    pnl = round(pnl_per_share * qty, 4)

    return {
        "entry_secs":   entry["secs"],
        "entry_side":   entry_side,
        "entry_price":  entry_price,
        "winner_at_end": winner_at_end,
        "reversal":     reversal,
        "pnl":          pnl,
        "el_vel":       el_state.get("el_vel"),
        "had_f3_dip":   el_state.get("had_f3_dip"),
    }


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

def report(trades, label, qty):
    if not trades:
        print(f"  {label}: sem trades")
        return
    wins   = [t for t in trades if t["reversal"]]
    losses = [t for t in trades if not t["reversal"]]
    pnl    = sum(t["pnl"] for t in trades)
    wr     = 100 * len(wins) / len(trades)
    avg    = pnl / len(trades)

    prices = [t["entry_price"] for t in trades]
    avg_price = sum(prices) / len(prices)
    # PnL por reversão (quando ganha)
    win_pnls = [t["pnl"] for t in wins]
    loss_pnls = [t["pnl"] for t in losses]
    avg_win_pnl  = sum(win_pnls) / len(win_pnls) if win_pnls else 0
    avg_loss_pnl = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0

    print(f"  {label}")
    print(f"    n={len(trades)}  WR={wr:.1f}%  PnL=${pnl:+.2f}  avg=${avg:+.4f}")
    print(f"    entry_price: avg={avg_price:.3f}  min={min(prices):.3f}  max={max(prices):.3f}")
    print(f"    WIN: {len(wins)} (avg ${avg_win_pnl:+.2f}/trade)")
    print(f"    LOSS: {len(losses)} (avg ${avg_loss_pnl:+.2f}/trade)")

    # Distribuição por faixa de loser_price
    buckets = [(0.01, 0.05), (0.05, 0.08), (0.08, 0.12), (0.12, 0.16)]
    bucket_lines = []
    for lo, hi in buckets:
        bt = [t for t in trades if lo <= t["entry_price"] < hi]
        if not bt: continue
        bw = sum(1 for t in bt if t["reversal"])
        bp = sum(t["pnl"] for t in bt)
        # retorno se ganhar: (1 - entry_price) * qty
        avg_ep = sum(t["entry_price"] for t in bt) / len(bt)
        ret_if_win = round((1 - avg_ep) * qty, 2)
        bucket_lines.append(
            f"      loser {lo:.2f}-{hi:.2f}: n={len(bt)} WR={100*bw/len(bt):.0f}%"
            f"  PnL=${bp:+.2f}  ret_if_win≈${ret_if_win:.2f}"
        )
    if bucket_lines:
        print("    por faixa de loser_price:")
        for bl in bucket_lines:
            print(bl)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qty",        type=float, default=QTY_DEFAULT)
    ap.add_argument("--vel-max",    type=float, default=VEL_MAX,
                    help="el_vel máximo para precursor (padrão 0.05)")
    ap.add_argument("--min-winner", type=float, default=MIN_WINNER)
    ap.add_argument("--max-loser",  type=float, default=MAX_LOSER)
    ap.add_argument("--log-dir",    default="logs")
    args = ap.parse_args()

    print(f"Carregando logs de '{args.log_dir}'...")
    all_slugs = load_slugs(args.log_dir)
    print(f"Slugs carregados: {len(all_slugs)}")
    print()

    baseline_trades  = []
    precursor_trades = []
    no_precur_trades = []

    for slug, snaps in all_slugs.items():
        el_state = get_el_state(snaps)
        if el_state is None:
            el_state = {"el_leader": None, "el_vel": None, "had_f3_dip": None}

        t = simulate(snaps, args.min_winner, args.max_loser, args.qty, el_state)
        if t is None:
            continue

        baseline_trades.append(t)

        # Precursor: el_vel abaixo do threshold E F3 falhou
        vel = t.get("el_vel")
        dip = t.get("had_f3_dip")
        has_precursor = (
            vel is not None and vel < args.vel_max
            and dip is True
        )
        if has_precursor:
            precursor_trades.append(t)
        else:
            no_precur_trades.append(t)

    print("=" * 66)
    print(f"  SIMULAÇÃO SNIPER — loser_price entry | qty={args.qty}")
    print(f"  Precursor: el_vel < {args.vel_max} AND F3 falhou")
    print(f"  Janela: winner >= {args.min_winner} | loser <= {args.max_loser} | secs {MIN_SECS}-{MAX_SECS}")
    print("=" * 66)
    print()

    report(baseline_trades,  "BASELINE (todos os slugs na janela)", args.qty)
    report(precursor_trades, f"COM PRECURSOR (el_vel<{args.vel_max} AND F3 falhou)", args.qty)
    report(no_precur_trades, "SEM precursor", args.qty)

    # Resumo comparativo
    if precursor_trades and no_precur_trades:
        wr_p = 100 * sum(1 for t in precursor_trades if t["reversal"]) / len(precursor_trades)
        wr_n = 100 * sum(1 for t in no_precur_trades if t["reversal"]) / len(no_precur_trades)
        avg_p = sum(t["pnl"] for t in precursor_trades) / len(precursor_trades)
        avg_n = sum(t["pnl"] for t in no_precur_trades) / len(no_precur_trades)
        print("=" * 66)
        print("  COMPARATIVO")
        print("=" * 66)
        print(f"  {'':30} {'WR%':>7} {'avg PnL':>10}")
        print(f"  {'Com precursor':30} {wr_p:>6.1f}% ${avg_p:>+9.4f}")
        print(f"  {'Sem precursor':30} {wr_n:>6.1f}% ${avg_n:>+9.4f}")
        print(f"  {'Diferença WR':30} {wr_p - wr_n:>+6.1f}%")
        print()
        print(f"  Interpretação: entrar como sniper quando precursor ativo")
        print(f"  é {wr_p/wr_n:.1f}x mais provável de reverter do que sem o filtro.")
        print()

    # Gate: break-even mínimo necessário
    print("=" * 66)
    print("  BREAK-EVEN POR FAIXA DE LOSER_PRICE (qty={:.0f})".format(args.qty))
    print("=" * 66)
    print(f"  {'Loser price':15} {'Retorno se WIN':17} {'WR mín p/ +EV':15}")
    for lp in [0.03, 0.05, 0.08, 0.10, 0.12, 0.15]:
        ret_win  = round((1.0 - lp) * args.qty, 2)
        loss_val = round(lp * args.qty, 2)
        # break-even: wr * ret_win = (1-wr) * loss_val → wr = loss_val / (ret_win + loss_val)
        be_wr    = round(100 * loss_val / (ret_win + loss_val), 1)
        print(f"  {lp:.2f}           ${ret_win:>7.2f}/trade       {be_wr:>5.1f}%")
    print()


if __name__ == "__main__":
    main()
