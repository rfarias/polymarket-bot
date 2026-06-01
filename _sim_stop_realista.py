"""
_sim_stop_realista.py

Análise precisa do trade-off de stop para EE.

O problema do script anterior: usava fill=0.47 para TODOS os stops,
independente de onde o bid estava quando o stop disparou.

Correção: para cada WIN com min_bid < stop_level, o fill real é
aproximadamente o min_bid (mercado já caiu até lá e ainda está caindo).

Para cada LOSS, o fill é: min(stop_level, primeiro_bid_abaixo_do_stop)
e não piora mais porque os losses caem rápido e fundo (0.0).

Resultado: calcula o PnL real de cada trade com e sem stop para cada nível.
"""
import glob, json, sys
from collections import defaultdict
from _sim_ee_gates import load_snaps, simulate_slug

QTY   = 6.0
STOPS = [0.80, 0.75, 0.70, 0.67, 0.65, 0.60, 0.55, 0.50, 0.47, 0.45, 0.40]
STOP_FIRES_RATE = 0.85


def get_trajectory(snaps, entry_secs, el_side):
    """Retorna lista de (secs, el_bid) após a entrada, em ordem cronológica."""
    post = [(int(s.get("current_secs", 0)),
             s.get("current_exec", {}).get("up_bid" if el_side == "UP" else "down_bid", 0.0))
            for s in snaps
            if int(s.get("current_secs", 999)) < entry_secs
            and s.get("current_exec")]
    return sorted(post, reverse=True)  # secs desc = cronológico


def simulate_with_stop(traj, ep, stop_level) -> tuple[float, bool]:
    """
    Simula o trade com um stop a stop_level dado a trajetória já computada.
    Retorna (pnl, foi_false_stop).

    Fill do stop: bid real no momento do disparo × 0.90 (spread/slippage).
    """
    if not traj:
        return round((1.0 - ep) * QTY, 4), False

    for secs, bid in traj:
        if bid <= 0:
            continue

        # Stop disparou
        if bid < stop_level:
            # Fill real ≈ bid atual (já deslizou, spread grande)
            # Mas adiciona 10% de spread: fill ligeiramente pior que o bid visto
            fill_price = max(bid * 0.90, 0.01)
            pnl = (fill_price - ep) * QTY * STOP_FIRES_RATE + \
                  (0.0 - ep) * QTY * (1 - STOP_FIRES_RATE)
            return round(pnl, 4), True  # True = stop disparou

        # Resolução normal (secs <= 5 ou bid >= 0.95)
        if secs <= 5 or bid >= 0.95:
            return round((1.0 - ep) * QTY, 4), False

    # Chegou ao fim sem resolver: assume WIN (hold binário)
    return round((1.0 - ep) * QTY, 4), False


def main():
    print("Carregando snapshots EE paper...")
    by_slug = load_snaps("logs/ee_paper_*/ee_paper.jsonl")
    print(f"  {len(by_slug)} slugs\n")

    # Coleta todos os trades com trajetória
    trades = []
    for slug, snaps in sorted(by_slug.items()):
        r = simulate_slug(snaps, 0.17, None, True, True, QTY)
        if r is None:
            continue
        ep = r["ep"]
        if not (0.83 <= ep <= 0.86):
            continue

        traj = get_trajectory(snaps, r["entry_secs"], r["el_side"])
        if not traj:
            continue

        trades.append({
            "slug":        slug,
            "ep":          ep,
            "el_vel":      r["el_vel"],
            "entry_secs":  r["entry_secs"],
            "el_side":     r["el_side"],
            "outcome_sim": r["outcome"],    # WIN ou STOP
            "pnl_hold":    r["pnl"],        # PnL sem stop (hold to resolution)
            "traj":        traj,
            "min_bid":     min(b for _, b in traj if b > 0) if traj else 1.0,
        })

    wins  = [t for t in trades if t["outcome_sim"] == "WIN"]
    losses = [t for t in trades if t["outcome_sim"] == "STOP"]
    print(f"Trades totais: {len(trades)} | WIN: {len(wins)} | LOSS: {len(losses)}\n")

    # ── Para cada nível de stop ───────────────────────────────────────────────
    print("=" * 80)
    print("  TRADE-OFF STOP — fill real baseado no bid no momento do disparo")
    print(f"  (STOP_FIRES_RATE={STOP_FIRES_RATE*100:.0f}%, fill = bid_atual × 0.90)")
    print("=" * 80)
    print(f"  {'Stop':>5}  {'F+/91':>7}  {'Custo F+':>10}  {'Econ Losses':>12}  {'Liquido':>9}  {'Avg/stop':>9}")
    print(f"  {'─'*5}  {'─'*7}  {'─'*10}  {'─'*12}  {'─'*9}  {'─'*9}")

    # PnL baseline (hold, sem stop) para cada grupo
    hold_wins_pnl  = sum(t["pnl_hold"] for t in wins)
    hold_loss_pnl  = sum(t["pnl_hold"] for t in losses)

    best_net = float("-inf")
    best_stop = None

    for stop in STOPS:
        # Wins: aplica stop, calcula PnL real
        false_stop_pnls = []
        stable_win_pnls = []
        for t in wins:
            pnl, fired = simulate_with_stop(t["traj"], t["ep"], stop)
            if fired:
                false_stop_pnls.append(pnl if pnl is not None else t["pnl_hold"])
            else:
                stable_win_pnls.append(t["pnl_hold"])  # hold normal

        # Losses: aplica stop
        loss_stop_pnls = []
        for t in losses:
            pnl, fired = simulate_with_stop(t["traj"], t["ep"], stop)
            loss_stop_pnls.append(pnl if pnl is not None else t["pnl_hold"])

        n_false = len(false_stop_pnls)
        custo   = round(sum(false_stop_pnls) - sum(t["pnl_hold"] for t in wins
                            if simulate_with_stop(t["traj"], t["ep"], stop)[1]), 2)
        # Recalcular corretamente o custo
        pnl_wins_with_stop = sum(false_stop_pnls) + sum(stable_win_pnls)
        cost_vs_hold = round(pnl_wins_with_stop - hold_wins_pnl, 2)

        econ_losses  = round(sum(loss_stop_pnls) - hold_loss_pnl, 2)
        net          = round(cost_vs_hold + econ_losses, 2)
        avg_per_stop = round(net / max(n_false + len(losses), 1), 3)

        flag = " ← MELHOR" if net > best_net else ""
        if net > best_net:
            best_net  = net
            best_stop = stop

        pct = n_false / len(wins) * 100 if wins else 0
        print(f"  {stop:.2f}   {n_false:>3} ({pct:>4.1f}%)  {cost_vs_hold:>+10.2f}  "
              f"{econ_losses:>+12.2f}  {net:>+9.2f}  {avg_per_stop:>+9.3f}{flag}")

    print(f"\n  Melhor stop: {best_stop} (net = {best_net:+.2f} USD / 10 dias)")
    print(f"  Hold total (sem stop): {round(hold_wins_pnl + hold_loss_pnl, 2):+.2f} USD")
    print(f"  Com melhor stop:       {round(hold_wins_pnl + hold_loss_pnl + best_net, 2):+.2f} USD")

    # ── Detalhe dos wins que disparariam o melhor stop ────────────────────────
    if best_stop:
        print(f"\n{'─'*80}")
        print(f"  WINS CORTADOS PELO STOP {best_stop} — trajetória + fill real")
        print(f"{'─'*80}")
        print(f"  {'slug[-18:]':18} {'ep':>5} {'min_bid':>8} {'fill':>6} "
              f"{'pnl_hold':>9} {'pnl_stop':>9} {'delta':>8}")
        for t in sorted(wins, key=lambda x: x["min_bid"]):
            if t["min_bid"] >= best_stop:
                break
            pnl_stop, _ = simulate_with_stop(t["traj"], t["ep"], best_stop)
            fill_est = round(t["min_bid"] * 0.90, 3)
            delta    = round((pnl_stop or 0) - t["pnl_hold"], 2)
            print(f"  {t['slug'][-18:]:18} {t['ep']:>5.2f} {t['min_bid']:>8.3f} "
                  f"{fill_est:>6.3f} {t['pnl_hold']:>+9.3f} "
                  f"{(pnl_stop or 0):>+9.3f} {delta:>+8.2f}")

    # ── Losses: o que o stop realmente salva ─────────────────────────────────
    print(f"\n{'─'*80}")
    print(f"  LOSSES — PnL hold vs com stop a {best_stop}")
    print(f"{'─'*80}")
    print(f"  {'slug[-18:]':18} {'ep':>5} {'min_bid':>8} {'pnl_hold':>9} "
          f"{'pnl_stop':>9} {'delta':>8}")
    for t in sorted(losses, key=lambda x: x["min_bid"]):
        pnl_s, _ = simulate_with_stop(t["traj"], t["ep"], best_stop or 0.50)
        delta = round((pnl_s or 0) - t["pnl_hold"], 2)
        print(f"  {t['slug'][-18:]:18} {t['ep']:>5.2f} {t['min_bid']:>8.3f} "
              f"{t['pnl_hold']:>+9.3f} {(pnl_s or 0):>+9.3f} {delta:>+8.2f}")


if __name__ == "__main__":
    main()
