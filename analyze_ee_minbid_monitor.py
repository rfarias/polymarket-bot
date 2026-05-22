"""
analyze_ee_minbid_monitor.py

Analisa os logs do market_monitor (que já têm EE simulado) para responder:
  "Para todos os trades EE que teriam ocorrido ANTES do standalone runner,
   qual foi o min_bid do lado EL durante o hold?
   Algum WIN tocou abaixo de 0.67 ou 0.65?"

Usa monitor_snap events com campos:
  ee_state, ee_side, ee_entry_ep, ee_outcome, ee_pnl, up_bid, down_bid, secs, slug

Uso:
    python analyze_ee_minbid_monitor.py [--log-dir logs/]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from collections import defaultdict

STOP_LEVELS = [0.80, 0.75, 0.72, 0.70, 0.67, 0.65, 0.62, 0.60]


def _el_bid(snap: dict) -> float:
    side = snap.get("ee_side") or snap.get("el_leader")
    if side == "UP":
        return snap.get("up_bid", 0.0)
    elif side == "DOWN":
        return snap.get("down_bid", 0.0)
    return 0.0


def load_monitor_trades(log_paths: list[Path]) -> list[dict]:
    """
    Extrai trades EE completos dos logs de market_monitor.
    Cada trade: slug, side, ep, outcome, pnl, snaps_entry (lista de bids EL durante entry).
    Deduplicação por slug (pega o trade mais recente com outcome não-vazio).
    """
    # Agrupa snaps por slug
    slug_snaps: dict[str, list[dict]] = defaultdict(list)
    slug_outcome: dict[str, dict] = {}

    for lp in log_paths:
        rows = [json.loads(l) for l in lp.read_text(encoding="utf-8").splitlines() if l.strip()]
        for r in rows:
            if r.get("type") != "monitor_snap":
                continue
            slug = r.get("slug", "")
            if not slug:
                continue

            ee_state = r.get("ee_state", "none")
            if ee_state in ("entry", "hedged"):
                slug_snaps[slug].append({
                    "secs":    r.get("secs"),
                    "el_bid":  _el_bid(r),
                    "up_bid":  r.get("up_bid", 0),
                    "dn_bid":  r.get("down_bid", 0),
                    "state":   ee_state,
                    "ee_side": r.get("ee_side"),
                })

            # Captura o fechamento (snap com outcome não-vazio)
            outcome = r.get("ee_outcome", "")
            if outcome and outcome not in ("", "MISSED"):
                slug_outcome[slug] = {
                    "slug":    slug,
                    "side":    r.get("ee_side"),
                    "ep":      r.get("ee_entry_ep", 0),
                    "outcome": outcome,
                    "pnl":     r.get("ee_pnl", 0),
                    "secs":    r.get("secs"),
                }

    # Monta lista de trades com snaps de entrada
    trades = []
    for slug, meta in slug_outcome.items():
        snaps = slug_snaps.get(slug, [])
        # Apenas snaps em estado "entry" para min_bid da posição pura
        entry_bids = [s["el_bid"] for s in snaps if s["state"] == "entry" and s["el_bid"] > 0]
        trades.append({
            **meta,
            "slug_short": slug[-6:],
            "entry_bids": entry_bids,
            "min_bid":    round(min(entry_bids), 3) if entry_bids else None,
            "max_bid":    round(max(entry_bids), 3) if entry_bids else None,
            "n_snaps":    len(entry_bids),
        })

    trades.sort(key=lambda t: t["slug"])
    return trades


def run(log_dir: str = "logs/") -> None:
    base = Path(log_dir)
    monitor_logs = sorted(base.glob("market_monitor_*.jsonl"))

    if not monitor_logs:
        print("Nenhum log market_monitor encontrado.")
        return

    print(f"\n{'='*76}")
    print(f"  ANÁLISE EE — min_bid nos logs do market_monitor")
    print(f"  Logs: {[p.name for p in monitor_logs]}")
    print(f"{'='*76}\n")

    trades = load_monitor_trades(monitor_logs)

    if not trades:
        print("Nenhum trade EE encontrado nos logs.")
        return

    # Separa por outcome
    wins    = [t for t in trades if t["outcome"] in ("WIN", "WIN_BOUNCE")]
    losses  = [t for t in trades if t["outcome"] in ("WIN_HEDGE", "REVERSAL")]
    all_cat = wins + losses

    n_total = len(all_cat)
    print(f"Total de trades EE encontrados: {n_total}")
    print(f"  WIN / WIN_BOUNCE : {len(wins)}")
    print(f"  WIN_HEDGE / REV  : {len(losses)}\n")

    # ---- Tabela completa ----
    print(f"{'#':>3}  {'Slug':>8}  {'Side':>4}  {'EP':>5}  {'Outcome':>12}  "
          f"{'min_bid':>8}  {'max_bid':>8}  {'<0.67':>7}  {'<0.65':>7}  {'PnL':>8}")
    print("-" * 78)

    for i, t in enumerate(all_cat, 1):
        mb  = t["min_bid"]
        mxb = t["max_bid"]
        h67 = "SIM !" if mb is not None and mb < 0.67 else "-"
        h65 = "SIM !" if mb is not None and mb < 0.65 else "-"
        print(f"{i:>3}  {t['slug_short']:>8}  {t['side']:>4}  {t['ep']:>5.2f}  "
              f"{t['outcome']:>12}  {str(mb):>8}  {str(mxb):>8}  "
              f"{h67:>7}  {h65:>7}  {t['pnl']:>+8.2f}")

    print("-" * 78)

    # ---- Resumo por categoria ----
    def _summary(label: str, group: list[dict]) -> None:
        if not group:
            return
        bids = [t["min_bid"] for t in group if t["min_bid"] is not None]
        hit67 = sum(1 for b in bids if b < 0.67)
        hit65 = sum(1 for b in bids if b < 0.65)
        pnl   = round(sum(t["pnl"] for t in group), 2)
        print(f"\n  {label} ({len(group)} trades):")
        print(f"    min_bid range : {min(bids):.3f} – {max(bids):.3f}")
        print(f"    tocaram < 0.67: {hit67}  ({hit67/len(group):.1%})")
        print(f"    tocaram < 0.65: {hit65}  ({hit65/len(group):.1%})")
        print(f"    PnL total     : {pnl:+.2f}  avg: {pnl/len(group):+.3f}")

    print("\nRESUMO POR CATEGORIA:")
    _summary("WIN / WIN_BOUNCE", wins)
    _summary("WIN_HEDGE / REVERSAL", losses)

    # ---- Separação por nível de stop ----
    print(f"\n{'='*76}")
    print("  SIMULAÇÃO STOP — impacto em portfólio completo (qty=3)")
    print("  (Saída ao stop price — ideal/otimista)")
    print(f"{'='*76}\n")

    base_pnl = round(sum(t["pnl"] for t in all_cat), 2)
    base_wr  = len(wins) / n_total

    print(f"  {'Stop':>8}  {'Ativa total':>12}  {'Ativa WIN':>10}  {'Ativa LOSS':>11}  "
          f"{'WR':>6}  {'PnL':>9}  {'vs base':>8}")
    print("  " + "-" * 68)

    for sl in STOP_LEVELS:
        # Para cada trade: se min_bid < stop → saída ao stop price
        pnl_total = 0.0
        n_wins_after = 0
        ativa_win = ativa_loss = ativa_total = 0

        for t in all_cat:
            mb = t["min_bid"]
            if mb is not None and mb < sl:
                # Stop disparou: saída ao preço do stop (ideal)
                pnl_stop = (sl - t["ep"]) * 3.0
                pnl_total += pnl_stop
                ativa_total += 1
                is_win_outcome = t["outcome"] in ("WIN", "WIN_BOUNCE")
                if is_win_outcome:
                    ativa_win += 1
                    # Stop cortou um trade que teria ganho → conta como perda
                    if pnl_stop > 0:
                        n_wins_after += 1
                else:
                    ativa_loss += 1
                    if pnl_stop > 0:
                        n_wins_after += 1
            else:
                pnl_total += t["pnl"]
                if t["pnl"] > 0:
                    n_wins_after += 1

        pnl_total = round(pnl_total, 2)
        wr_after  = n_wins_after / n_total
        delta     = pnl_total - base_pnl
        print(f"  {sl:>8.2f}  {ativa_total:>12}  {ativa_win:>10}  {ativa_loss:>11}  "
              f"{wr_after:>6.1%}  {pnl_total:>+9.2f}  {delta:>+8.2f}")

    print(f"\n  BASE (sem stop): WR={base_wr:.1%}  PnL={base_pnl:+.2f}\n")

    # ---- Detalhe: WIN trades que tocaram < threshold ----
    threshold = 0.70
    risky_wins = [t for t in wins if t["min_bid"] is not None and t["min_bid"] < threshold]
    if risky_wins:
        print(f"\n{'='*76}")
        print(f"  WIN/WIN_BOUNCE com min_bid < {threshold} — trades que estão no limite")
        print(f"{'='*76}\n")
        print(f"  {'Slug':>8}  {'Side':>4}  {'EP':>5}  {'Outcome':>12}  "
              f"{'min_bid':>8}  {'<0.67':>7}  {'<0.65':>7}  {'PnL':>8}")
        print("  " + "-" * 65)
        for t in sorted(risky_wins, key=lambda x: x["min_bid"]):
            mb  = t["min_bid"]
            h67 = "SIM !" if mb < 0.67 else "-"
            h65 = "SIM !" if mb < 0.65 else "-"
            print(f"  {t['slug_short']:>8}  {t['side']:>4}  {t['ep']:>5.2f}  "
                  f"{t['outcome']:>12}  {mb:>8.3f}  {h67:>7}  {h65:>7}  {t['pnl']:>+8.2f}")
    else:
        print(f"\n  Nenhum WIN/WIN_BOUNCE teve min_bid < {threshold}. "
              f"Stop {threshold} seria 100% seguro nessa amostra.")

    # ---- Distribuição do min_bid por faixas ----
    print(f"\n{'='*76}")
    print("  DISTRIBUIÇÃO min_bid durante hold (estado entry)")
    print(f"{'='*76}\n")
    buckets = [(0.50, 0.60), (0.60, 0.65), (0.65, 0.67), (0.67, 0.70),
               (0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 1.01)]
    print(f"  {'Faixa':>14}  {'WIN':>6}  {'LOSS':>6}  {'Total':>7}")
    print("  " + "-" * 38)
    for lo, hi in buckets:
        nw = sum(1 for t in wins   if t["min_bid"] is not None and lo <= t["min_bid"] < hi)
        nl = sum(1 for t in losses if t["min_bid"] is not None and lo <= t["min_bid"] < hi)
        mark = " <-- zona perigosa" if lo < 0.67 and hi <= 0.67 else ""
        mark = " <-- stop 0.65 zona" if lo < 0.65 and hi <= 0.65 else mark
        print(f"  [{lo:.2f} – {hi:.2f})  {nw:>6}  {nl:>6}  {nw+nl:>7}{mark}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", default="logs/")
    args = parser.parse_args()
    run(log_dir=args.log_dir)
