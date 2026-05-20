"""
Extrai todos os trades reais do almost_resolved lendo apenas linhas relevantes.
Estratégia: parse somente linhas com "enter", "flat", "awaiting_redeem"
"""
import json
import glob
import os
from pathlib import Path

LOGS_DIR = "logs"

def iter_relevant_lines(path):
    keywords = ['"type": "enter"', '"type": "flat"', '"type": "awaiting_redeem"',
                '"type": "trade_summary"', '"type": "fill_on_invalid_signal"']
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if any(k in line for k in keywords):
                    try:
                        yield json.loads(line.strip())
                    except:
                        pass
    except:
        pass

def main():
    pattern = os.path.join(LOGS_DIR, "current_almost_resolved_real_*", "current_almost_resolved_real.jsonl")
    files = sorted(glob.glob(pattern))
    print(f"Sessões: {len(files)}")

    # coleta por slug: {slug: {"entries": [], "flats": [], "redeems": []}}
    from collections import defaultdict
    slug_data = defaultdict(lambda: {"enters": [], "flats": [], "redeems": [], "invalids": []})

    for fpath in files:
        for ev in iter_relevant_lines(fpath):
            t = ev.get("type", "")
            trade = ev.get("trade", {}) or {}
            slug = trade.get("event_slug") or ev.get("event_slug")
            if not slug:
                continue

            if t == "enter":
                slug_data[slug]["enters"].append({
                    "entry_price": ev.get("signal", {}).get("entry_price") or trade.get("entry_price"),
                    "qty": trade.get("entry_qty_requested"),
                    "side": trade.get("side"),
                    "stop": trade.get("stop_price"),
                    "target": trade.get("target_price"),
                    "variant": trade.get("setup_variant"),
                    "ts": ev.get("ts"),
                })
            elif t == "flat":
                exit_ord = ev.get("exit_order") or {}
                slug_data[slug]["flats"].append({
                    "exit_price": float(exit_ord.get("price", 0)),
                    "qty_matched": float(exit_ord.get("size_matched", 0)),
                    "status": exit_ord.get("status"),
                    "order_type": exit_ord.get("order_type"),
                    "entry_price": trade.get("entry_price"),
                    "entry_qty": trade.get("entry_qty_filled"),
                    "ts": ev.get("ts"),
                })
            elif t == "awaiting_redeem":
                slug_data[slug]["redeems"].append({
                    "side_won": ev.get("side_won"),
                    "platform_redeem_path": ev.get("platform_redeem_path"),
                    "entry_price": trade.get("entry_price"),
                    "entry_qty": trade.get("entry_qty_filled"),
                    "exit_qty": trade.get("exit_qty_filled"),
                    "last_reason": trade.get("last_reason"),
                    "ts": ev.get("ts"),
                })
            elif t == "fill_on_invalid_signal":
                slug_data[slug]["invalids"].append({
                    "fill_bid": ev.get("fill_bid"),
                    "fill_at_stop": ev.get("fill_at_stop"),
                    "entry_price": trade.get("entry_price"),
                    "qty": trade.get("entry_qty_filled"),
                    "ts": ev.get("ts"),
                })

    # reconstrói trades
    trades = []
    processed = set()

    for slug, data in slug_data.items():
        enters = data["enters"]
        flats = data["flats"]
        redeems = data["redeems"]
        invalids = data["invalids"]

        if not enters:
            continue

        # pode ter múltiplas entradas no mesmo slug (re-entry após stop parcial)
        for enter in enters:
            entry_price = enter["entry_price"]
            qty = enter["qty"] or 10.0
            side = enter["side"]
            stop = enter["stop"]
            target = enter["target"]
            variant = enter["variant"]
            entry_ts = enter["ts"]

            if entry_price is None:
                continue

            key = f"{slug}:{entry_price}:{entry_ts}"
            if key in processed:
                continue
            processed.add(key)

            # procura flat correspondente (após entry_ts)
            flat = next((f for f in flats if f["ts"] >= entry_ts and f["entry_price"] == entry_price), None)

            if flat and flat["qty_matched"] > 0:
                ep = flat["exit_price"]
                eq = flat["qty_matched"]
                pnl = (ep - entry_price) * eq
                trades.append({
                    "slug": slug[-25:],
                    "side": side,
                    "entry": entry_price,
                    "exit": ep,
                    "qty": eq,
                    "pnl": round(pnl, 4),
                    "outcome": "WIN" if pnl > 0 else "LOSS",
                    "type": "exit_flat",
                    "variant": variant,
                })
                continue

            # procura redeem correspondente
            redeem = next((r for r in redeems if r["ts"] >= entry_ts and r["entry_price"] == entry_price), None)
            if redeem:
                if redeem["side_won"] is True:
                    pnl = (1.0 - entry_price) * (redeem["entry_qty"] or qty)
                    trades.append({
                        "slug": slug[-25:],
                        "side": side,
                        "entry": entry_price,
                        "exit": 1.0,
                        "qty": redeem["entry_qty"] or qty,
                        "pnl": round(pnl, 4),
                        "outcome": "WIN",
                        "type": "redeem_win",
                        "variant": variant,
                    })
                elif redeem["side_won"] is False or redeem.get("platform_redeem_path"):
                    pnl = -entry_price * (redeem["entry_qty"] or qty)
                    trades.append({
                        "slug": slug[-25:],
                        "side": side,
                        "entry": entry_price,
                        "exit": 0.0,
                        "qty": redeem["entry_qty"] or qty,
                        "pnl": round(pnl, 4),
                        "outcome": "TOTAL_LOSS",
                        "type": "redeem_loss",
                        "variant": variant,
                    })
                elif redeem["last_reason"] and "pending_exit_platform_redeem_path" in (redeem["last_reason"] or ""):
                    pnl = -entry_price * (redeem["entry_qty"] or qty)
                    trades.append({
                        "slug": slug[-25:],
                        "side": side,
                        "entry": entry_price,
                        "exit": 0.0,
                        "qty": redeem["entry_qty"] or qty,
                        "pnl": round(pnl, 4),
                        "outcome": "TOTAL_LOSS",
                        "type": "platform_redeem_loss",
                        "variant": variant,
                    })
                continue

            # sem flat nem redeem - pendente ou perdida sem registro
            # tenta inferir pela invalids
            if invalids:
                inv = next((i for i in invalids if i["ts"] >= entry_ts and i["entry_price"] == entry_price), None)
                if inv and inv["fill_at_stop"]:
                    pnl = (stop - entry_price) * (inv["qty"] or qty) if stop else 0
                    trades.append({
                        "slug": slug[-25:],
                        "side": side,
                        "entry": entry_price,
                        "exit": stop,
                        "qty": inv["qty"] or qty,
                        "pnl": round(pnl, 4),
                        "outcome": "LOSS",
                        "type": "inferred_stop",
                        "variant": variant,
                    })

    trades.sort(key=lambda t: t.get("slug", ""))
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)

    print(f"\n{'='*80}")
    print(f"TODOS OS TRADES REAIS ({len(trades)} total)")
    print(f"{'='*80}")
    for i, t in enumerate(trades, 1):
        sign = "+" if t["pnl"] >= 0 else ""
        print(f"  #{i:3d} {t['outcome']:10s} | {t['slug']:25s} | {t['side']:4s} "
              f"| entry={t['entry']:.3f} exit={t['exit']:.3f} qty={t['qty']:.0f} "
              f"| pnl={sign}{t['pnl']:.4f} | {t['type']}")

    print(f"\n{'='*80}")
    print(f"RESUMO GERAL")
    print(f"{'='*80}")
    print(f"  Total trades:      {len(trades)}")
    if wins:
        print(f"  Wins ({len(wins)}):         soma=+{sum(t['pnl'] for t in wins):.4f}  "
              f"médio=+{sum(t['pnl'] for t in wins)/len(wins):.4f}")
    if losses:
        print(f"  Losses ({len(losses)}):       soma={sum(t['pnl'] for t in losses):.4f}  "
              f"médio={sum(t['pnl'] for t in losses)/len(losses):.4f}")
    print(f"  PnL TOTAL:         {'+' if total_pnl >= 0 else ''}{total_pnl:.4f}")
    if trades:
        print(f"  Win rate:          {len(wins)/len(trades)*100:.1f}%")
    if losses:
        worst = sorted(losses, key=lambda t: t["pnl"])[:5]
        print(f"\n  TOP 5 MAIORES PERDAS:")
        for t in worst:
            print(f"    {t['pnl']:.4f}  |  {t['slug']}  |  {t['type']}")

if __name__ == "__main__":
    main()
