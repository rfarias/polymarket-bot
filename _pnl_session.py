import json, datetime, sys

LOG = r"C:\Users\Letícia\Documents\polymarket-bot\logs\current_almost_resolved_real_20260521_125445\current_almost_resolved_real.jsonl"

events = []
with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            pass

enters = []
exits_map = {}

for e in events:
    t = e.get("type", "")
    trade = e.get("trade") or {}
    ts = e.get("ts", 0)
    slug = trade.get("event_slug") or (e.get("signal") or {}).get("event_slug") or ""
    ts_str = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")

    if t == "enter":
        enters.append({
            "ts": ts_str, "side": trade.get("side"), "ep": trade.get("entry_price"),
            "stop": trade.get("stop_price"),
            "qty": trade.get("entry_qty_requested", 6), "qty_f": trade.get("entry_qty_filled", 0),
            "_ts": ts, "_slug": slug, "_slug_s": slug[-22:],
        })
    elif t == "fill":
        for en in reversed(enters):
            if en["_slug"] == slug:
                en["qty_f"] = max(en["qty_f"], trade.get("entry_qty_filled", 0))
                break
    elif t == "exit_posted":
        exits_map.setdefault(slug, []).append({
            "ts": ts_str, "reason": e.get("reason", ""),
            "xp": trade.get("exit_price_posted"), "_ts": ts,
            "qty": trade.get("entry_qty_filled", 0),
        })
    elif t == "awaiting_redeem":
        exits_map.setdefault(slug, []).append({
            "ts": ts_str, "reason": "redeem:" + (trade.get("last_reason") or ""),
            "xp": None, "ab": e.get("active_bid"), "_ts": ts,
            "qty": trade.get("entry_qty_filled", 0),
        })

print(f"{'#':>3} | {'Entrada':>8} | {'S':>4} | {'EP':>5} | {'Saida':>8} | {'Xp':>5} | Q  | {'PnL':>9} | Razao")
print("-" * 95)

total, nw, nl, nunk = 0.0, 0, 0, 0

for i, en in enumerate(enters, 1):
    sl = en["_slug"]
    ep = en["ep"] or 0
    qty = en["qty_f"] or en["qty"] or 6
    exs = sorted(
        [x for x in exits_map.get(sl, []) if x["_ts"] > en["_ts"]],
        key=lambda x: x["_ts"],
    )
    if not exs:
        print(f"{i:>3} | {en['ts']} | {en['side']:>4} | {ep:.3f} |    ---   |  --- | {qty:>2.0f} |       -- | OPEN {en['_slug_s']}")
        nunk += 1
        continue
    ex = exs[0]
    reason = ex["reason"][:40]
    xp = ex.get("xp") or ex.get("ab") or 0
    if ep > 0 and xp > 0:
        pnl = round((xp - ep) * qty, 4)
        total += pnl
        nw += int(pnl > 0)
        nl += int(pnl < 0)
        print(f"{i:>3} | {en['ts']} | {en['side']:>4} | {ep:.3f} | {ex['ts']} | {xp:.3f} | {qty:>2.0f} | {pnl:>+9.4f} | {reason}")
    else:
        print(f"{i:>3} | {en['ts']} | {en['side']:>4} | {ep:.3f} | {ex['ts']} |  ??? | {qty:>2.0f} |       ?? | {reason}")
        nunk += 1

print("-" * 95)
nc = nw + nl
wr = f"{nw/nc*100:.0f}%" if nc else "n/a"
print(f"Trades fechados: {nc}  ({nw}W / {nl}L  WR={wr})   Sem exit_price: {nunk}")
print(f"PnL total sessao: {total:>+.4f} USD")
