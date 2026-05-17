"""
Extrai todos os trades reais (fills + resultado) de todos os logs.
Calcula PnL em ticks (1 tick = $0.01 por share) e por sessao.

Uso:
    python scripts/analyse_all_trades.py
    python scripts/analyse_all_trades.py --since 2026-05-01
"""
import json
import sys
import os
import argparse
from pathlib import Path
from datetime import datetime, timezone, date

ROOT = Path(__file__).resolve().parent.parent
TICK = 0.01


def ts_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()


def load(path):
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    return events


def analyse_session(log_path: str) -> list[dict]:
    events = load(log_path)
    sess = Path(log_path).parent.name

    # Indexar por event_slug: fill -> outcome
    fills: dict[str, dict] = {}
    stops: dict[str, dict] = {}
    redeems: dict[str, dict] = {}
    exits_flat: dict[str, dict] = {}
    # Lista de eventos awaiting_redeem_status por slug (para delta de colateral)
    redeem_status_by_slug: dict[str, list[dict]] = {}

    for e in events:
        t = e.get("type", "")
        slug = e.get("trade", {}).get("event_slug", "") or ""
        if not slug:
            slug = e.get("slug", "") or ""

        if t in ("fill", "fill_on_invalid_signal"):
            fills[slug] = e

        elif t == "redeem_flat":
            redeems[slug] = e

        elif t == "flat":
            exits_flat[slug] = e

        elif t == "exit_posted":
            stops[slug] = e

        elif t == "awaiting_redeem_status" and slug:
            redeem_status_by_slug.setdefault(slug, []).append(e)

    trades = []
    for slug, fill_ev in fills.items():
        trade_info = fill_ev.get("trade", {})
        side = trade_info.get("side", "?")
        entry = float(trade_info.get("entry_price") or 0)
        qty = float(trade_info.get("entry_qty_requested") or 6)
        fill_ts = fill_ev.get("ts", 0)
        fill_type = fill_ev.get("type", "fill")
        variant = trade_info.get("setup_variant", "")

        outcome = "?"
        exit_price = None
        pnl_ticks = None
        pnl_usd = None
        exit_reason = "?"
        resolved = False

        if slug in redeems:
            r = redeems[slug]
            last_reason = r.get("trade", {}).get("last_reason", "")
            resolved = True
            if "win" in last_reason:
                exit_price = 1.0
                outcome = "WIN"
            else:
                # Try to determine WIN/LOSS from collateral delta:
                # redeem_flat now includes collateral_balance_usd (post-redeem).
                # The last awaiting_redeem_status before the flat has the pre-redeem balance.
                redeem_ts = r.get("ts", 0)
                collateral_after = r.get("collateral_balance_usd")
                collateral_before = None
                prior_statuses = [s for s in redeem_status_by_slug.get(slug, []) if s.get("ts", 0) < redeem_ts]
                if prior_statuses:
                    collateral_before = prior_statuses[-1].get("collateral_balance_usd")

                if collateral_after is not None and collateral_before is not None:
                    delta = float(collateral_after) - float(collateral_before)
                    # WIN: received ~qty * $1.00 back (delta should be ≥ 0.5 * qty)
                    if delta >= qty * 0.5:
                        exit_price = 1.0
                        outcome = "WIN"
                    else:
                        exit_price = 0.0
                        outcome = "LOSS(redeem)"
                else:
                    # Fallback: best_bid of our token at redeem time.
                    # If >= 0.90, market values our side highly → almost certainly WIN.
                    # (losing tokens near resolution would be priced near 0.0)
                    best_bid = float(r.get("trade", {}).get("best_bid") or 0)
                    if best_bid >= 0.99:
                        exit_price = 1.0
                        outcome = "WIN"
                    elif best_bid >= 0.90:
                        exit_price = 1.0
                        outcome = "WIN~"   # heuristic: best_bid 0.90-0.98 at redeem
                    elif best_bid > 0.0:
                        exit_price = best_bid
                        outcome = "LOSS(redeem)"
                    else:
                        exit_price = 0.0
                        outcome = "LOSS(redeem)"
            exit_reason = last_reason

        elif slug in exits_flat:
            f = exits_flat[slug]
            eo = f.get("exit_order") or {}
            exit_price = float(eo.get("price") or 0)
            reason_src = stops.get(slug, {}).get("reason", "") or ""
            exit_reason = reason_src or "stop/exit"
            outcome = "STOP" if exit_price < entry else "PROFIT_EXIT"

        elif slug in stops and slug not in exits_flat:
            # stop posted but no flat yet — partial
            outcome = "STOP(pending)"
            exit_reason = stops[slug].get("reason", "")

        if exit_price is not None and entry > 0:
            pnl_ticks = round((exit_price - entry) / TICK, 1)
            pnl_usd = round(pnl_ticks * TICK * qty, 4)

        trades.append({
            "session": sess,
            "date": ts_dt(fill_ts).strftime("%d/%m %H:%M"),
            "side": side,
            "entry": entry,
            "exit": exit_price,
            "qty": qty,
            "outcome": outcome,
            "pnl_ticks": pnl_ticks,
            "pnl_usd": pnl_usd,
            "exit_reason": exit_reason,
            "fill_type": fill_type,
            "variant": variant,
            "resolved": resolved,
        })

    return trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=None, help="Data mínima YYYY-MM-DD")
    args = parser.parse_args()

    since = None
    if args.since:
        since = date.fromisoformat(args.since)

    logs_dir = ROOT / "logs"
    log_files = sorted(logs_dir.rglob("current_almost_resolved_real.jsonl"),
                       key=lambda p: p.stat().st_mtime)

    all_trades: list[dict] = []
    for log_path in log_files:
        sess_date_str = log_path.parent.name[len("current_almost_resolved_real_"):]
        try:
            sess_date = datetime.strptime(sess_date_str[:8], "%Y%m%d").date()
        except Exception:
            continue
        if since and sess_date < since:
            continue
        all_trades.extend(analyse_session(str(log_path)))

    if not all_trades:
        print("Nenhum trade encontrado.")
        return

    # --- Sumário por sessão ---
    print("=" * 90)
    print(f" {'Data':16} {'Side':5} {'Entry':6} {'Exit':6} {'Qty':4} {'Ticks':7} {'USD':7}  Resultado / Razão")
    print("=" * 90)
    last_sess = None
    for t in all_trades:
        if t["session"] != last_sess:
            print(f"\n[{t['session']}]")
            last_sess = t["session"]
        ticks_str = f"{t['pnl_ticks']:+.1f}" if t["pnl_ticks"] is not None else "  ?"
        usd_str   = f"{t['pnl_usd']:+.2f}"   if t["pnl_usd"]   is not None else "  ?"
        flag = "(*)" if t["fill_type"] == "fill_on_invalid_signal" else "   "
        print(f"  {flag} {t['date']:16} {t['side']:5} {t['entry']:.2f}  {str(t['exit'] or '?'):6} "
              f"{t['qty']:4.0f}  {ticks_str:>7}  {usd_str:>7}  {t['outcome']:15} {t['exit_reason'][:40]}")

    # --- Totais ---
    known = [t for t in all_trades if t["pnl_ticks"] is not None]
    wins   = [t for t in known if t["outcome"] in ("WIN", "WIN~")]
    stops  = [t for t in known if t["outcome"] in ("STOP",)]
    prof   = [t for t in known if t["outcome"] in ("PROFIT_EXIT",)]
    unkn   = [t for t in all_trades if t["pnl_ticks"] is None]
    wins_heuristic = [t for t in known if t["outcome"] == "WIN~"]

    total_ticks = sum(t["pnl_ticks"] for t in known)
    total_usd   = sum(t["pnl_usd"]   for t in known)
    avg_win_ticks  = sum(t["pnl_ticks"] for t in wins+prof)  / max(len(wins+prof), 1)
    avg_loss_ticks = sum(t["pnl_ticks"] for t in stops)       / max(len(stops), 1)

    print("\n" + "=" * 90)
    print(f"TOTAIS ({len(known)} trades com resultado conhecido, {len(unkn)} sem dados)")
    heur_note = f"  (dos quais {len(wins_heuristic)} WIN~ por heuristica best_bid)" if wins_heuristic else ""
    print(f"  Wins (redeem/profit_exit): {len(wins)+len(prof):3d}{heur_note}   "
          f"Stops: {len(stops):2d}   Desconhecido: {len(unkn):2d}")
    print(f"  Win rate   : {100*(len(wins)+len(prof))/max(len(known),1):.1f}%")
    print(f"  Avg win    : {avg_win_ticks:+.1f} ticks  ({avg_win_ticks*TICK*6:+.3f} USD @ 6 shares)")
    print(f"  Avg loss   : {avg_loss_ticks:+.1f} ticks  ({avg_loss_ticks*TICK*6:+.3f} USD @ 6 shares)")
    if avg_loss_ticks < 0:
        print(f"  Risk/Reward: {abs(avg_win_ticks/avg_loss_ticks):.2f}x")
    print(f"  Total ticks: {total_ticks:+.1f}   Total USD (6 shares): {total_usd:+.4f}")
    print()

    # Por mês/semana
    print("  Por dia:")
    from collections import defaultdict
    by_day: dict[str, list] = defaultdict(list)
    for t in known:
        day = t["date"][:5]
        by_day[day].append(t)
    for day in sorted(by_day):
        ts_day = by_day[day]
        ticks = sum(t["pnl_ticks"] for t in ts_day)
        usd   = sum(t["pnl_usd"]   for t in ts_day)
        w = sum(1 for t in ts_day if t["outcome"] in ("WIN", "WIN~", "PROFIT_EXIT"))
        l = sum(1 for t in ts_day if t["outcome"] == "STOP")
        print(f"    {day}  trades={len(ts_day):2d}  wins={w}  stops={l}  ticks={ticks:+.1f}  usd={usd:+.4f}")

    # Distribuição de ticks
    print()
    print("  Distribuição de resultados por faixa de ticks (por trade):")
    buckets = [(-99,-10),(-9,-6),(-5,-3),(-2,-1),(1,2),(3,5),(6,9),(10,99)]
    for lo, hi in buckets:
        n = sum(1 for t in known if lo <= t["pnl_ticks"] <= hi)
        bar = "#" * n
        print(f"    [{lo:+3d} a {hi:+3d}]: {bar} ({n})")


if __name__ == "__main__":
    main()
