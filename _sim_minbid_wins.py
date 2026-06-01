"""
_sim_minbid_wins.py

Analisa o min_bid do EL durante o hold period para cada WIN simulado.
Responde: qual % dos wins toca abaixo de cada nível de stop?
Com isso calculamos o trade-off real: stops salvam X nos losses mas matam Y dos wins.
"""
import glob, json, sys
from collections import defaultdict
from _sim_ee_gates import load_snaps, simulate_slug

QTY    = 6.0
STOPS  = [0.80, 0.75, 0.70, 0.67, 0.65, 0.60, 0.55, 0.50, 0.45]

def get_min_bid_after_entry(snaps, entry_secs, el_side):
    """Retorna o bid mínimo do EL após a entrada até a resolução."""
    post = [s for s in snaps if int(s.get("current_secs", 999)) < entry_secs]
    min_b = float("inf")
    for s in post:
        exc = s.get("current_exec") or {}
        b = exc.get("up_bid", 0.0) if el_side == "UP" else exc.get("down_bid", 0.0)
        if b > 0:
            min_b = min(min_b, b)
    return min_b if min_b < float("inf") else None

def main():
    print("Carregando snapshots EE paper...")
    by_slug = load_snaps("logs/ee_paper_*/ee_paper.jsonl")
    print(f"  {len(by_slug)} slugs\n")

    wins_data  = []   # lista de (ep, min_bid, slug)
    losses_data= []

    for slug, snaps in sorted(by_slug.items()):
        r = simulate_slug(snaps, 0.17, None, True, True, QTY)
        if r is None:
            continue
        ep = r["ep"]
        if not (0.83 <= ep <= 0.86):
            continue

        min_b = get_min_bid_after_entry(snaps, r["entry_secs"], r["el_side"])
        if min_b is None:
            continue

        row = {"slug": slug, "ep": ep, "el_vel": r["el_vel"],
               "entry_secs": r["entry_secs"], "outcome": r["outcome"],
               "min_bid": min_b, "pnl_ideal": r["pnl"]}

        if r["outcome"] == "WIN":
            wins_data.append(row)
        else:
            losses_data.append(row)

    print(f"WIN trades analisados:  {len(wins_data)}")
    print(f"LOSS trades analisados: {len(losses_data)}")

    # ── Distribuição min_bid em WINS ─────────────────────────────────────────
    print("\n=== MIN_BID durante hold — WINS ===")
    print(f"  (pior bid que o EL atingiu entre entrada e resolucao)")
    edges = [0.45, 0.50, 0.55, 0.60, 0.65, 0.67, 0.70, 0.75, 0.80, 0.85, 0.90]
    buckets = defaultdict(list)
    for w in wins_data:
        for e in edges:
            if w["min_bid"] < e:
                buckets[e].append(w)
                break
        else:
            buckets[">=0.90"].append(w)

    print(f"  {'min_bid faixa':<18} {'N wins':>7} {'%':>6}  (falso stop se stop >= limite)")
    prev = 0.0
    for e in edges:
        grp = buckets[e]
        pct = len(grp) / len(wins_data) * 100 if wins_data else 0
        flag = " ← FALSO STOP" if e > 0.65 else ""
        print(f"  [{prev:.2f}, {e:.2f})    {len(grp):>7}  {pct:>5.1f}%{flag}")
        prev = e
    grp = buckets[">=0.90"]
    pct = len(grp) / len(wins_data) * 100 if wins_data else 0
    print(f"  [0.90, 1.00]    {len(grp):>7}  {pct:>5.1f}%")

    # ── Trade-off por nível de stop ──────────────────────────────────────────
    print("\n=== TRADE-OFF: falso stop em wins vs economia nos losses ===")
    print(f"  (qty=6, stop fill=0.47, losses todos caem a ~0.0 sem stop)")
    print(f"  {'Stop':>5}  {'False+':>7}  {'Custo':>8}  {'Saves':>7}  {'Econ':>8}  {'Liquido':>9}")
    print(f"  {'─'*5}  {'─'*7}  {'─'*8}  {'─'*7}  {'─'*8}  {'─'*9}")

    loss_save_per_stop = sum(
        (0.47 - t["ep"]) * QTY - (0.0 - t["ep"]) * QTY
        for t in losses_data
    )  # total economizado nos losses reais

    for stop in STOPS:
        false_stops = [w for w in wins_data if w["min_bid"] < stop]
        n_false = len(false_stops)
        # Custo: trade WIN vira loss. Perda = (stop_fill - ep)*qty - (1.0 - ep)*qty
        cost = sum(
            (0.47 - w["ep"]) * QTY - (1.0 - w["ep"]) * QTY
            for w in false_stops
        )
        # Economia: stops salvam parte do loss
        n_saves = len(losses_data)
        # Assumes STOP_FIRES_RATE = 85%
        saves = loss_save_per_stop * 0.85

        liquido = round(saves + cost, 2)
        pct_false = n_false / len(wins_data) * 100 if wins_data else 0
        print(f"  {stop:.2f}   {n_false:>4} ({pct_false:>4.1f}%)  {cost:>+8.2f}  "
              f"{n_saves:>5}  {saves:>+8.2f}  {liquido:>+9.2f}")

    # ── Piores wins (os que mais caem) ───────────────────────────────────────
    print(f"\n=== WINS QUE MAIS CAEM (min_bid < 0.70) ===")
    dangerous = sorted([w for w in wins_data if w["min_bid"] < 0.70],
                       key=lambda x: x["min_bid"])
    if dangerous:
        print(f"  {'slug[-20:]':20} {'ep':>5} {'min_bid':>8} {'entry_secs':>11} {'el_vel':>7}")
        for w in dangerous:
            print(f"  {w['slug'][-20:]:20} {w['ep']:>5.2f} {w['min_bid']:>8.3f} "
                  f"{w['entry_secs']:>11} {w['el_vel']:>7.3f}")
    else:
        print("  Nenhum win caiu abaixo de 0.70 nesta amostra.")

    # ── Resumo final ─────────────────────────────────────────────────────────
    wins_floor = min(w["min_bid"] for w in wins_data) if wins_data else 0
    print(f"\n=== RESUMO ===")
    print(f"  Min_bid floor dos wins: {wins_floor:.3f}")
    print(f"  Wins com min_bid < 0.65: "
          f"{sum(1 for w in wins_data if w['min_bid'] < 0.65)} / {len(wins_data)}")
    print(f"  Wins com min_bid < 0.67: "
          f"{sum(1 for w in wins_data if w['min_bid'] < 0.67)} / {len(wins_data)}")
    print(f"  Wins com min_bid < 0.70: "
          f"{sum(1 for w in wins_data if w['min_bid'] < 0.70)} / {len(wins_data)}")

if __name__ == "__main__":
    main()
