import json, datetime

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

# --- coleta por slug ---
enters = []
exits_map = {}
redeems = {}
redeem_finals = {}

for e in events:
    t = e.get("type", "")
    trade = e.get("trade") or {}
    ts = e.get("ts", 0)
    slug = (
        trade.get("event_slug")
        or (e.get("signal") or {}).get("event_slug")
        or e.get("slug")
        or ""
    )
    ts_str = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")

    if t == "enter":
        enters.append({
            "ts": ts_str, "side": trade.get("side"), "ep": trade.get("entry_price"),
            "stop": trade.get("stop_price"),
            "qty": trade.get("entry_qty_requested", 6),
            "qty_f": trade.get("entry_qty_filled", 0),
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
            "xp": trade.get("exit_price_posted"),
            "_ts": ts, "qty": trade.get("entry_qty_filled", 0),
            "kind": "exit",
        })
    elif t == "awaiting_redeem":
        redeems[slug] = {
            "ts": ts_str,
            "side": trade.get("side"),
            "ep": trade.get("entry_price"),
            "qty": trade.get("entry_qty_filled", 0) or trade.get("entry_qty_requested", 6),
            "active_bid": e.get("active_bid"),
            "last_reason": trade.get("last_reason", ""),
        }
        exits_map.setdefault(slug, []).append({
            "ts": ts_str, "reason": "redeem:" + (trade.get("last_reason") or ""),
            "xp": None, "ab": e.get("active_bid"),
            "_ts": ts, "qty": trade.get("entry_qty_filled", 0),
            "kind": "redeem",
        })
    elif t in ("redeem_flat", "flat"):
        if slug in redeems:
            redeem_finals[slug] = {
                "ts": ts_str,
                "collat": e.get("collateral_balance_usd"),
                "token_bal": e.get("token_balance_qty"),
                "pnl_usd": e.get("pnl_usd"),       # novo campo (runner corrigido)
                "exit_price": e.get("exit_price"),  # novo campo
            }
    elif t == "external_close_detected":
        # external_close é uma saída — adiciona ao exits_map como "redeem" para ser tratado
        rd = redeems.setdefault(slug, {})
        rd["external_close"] = True
        rd["ext_pnl_usd"] = e.get("pnl_usd")
        rd["ext_exit_price"] = e.get("exit_price")
        exits_map.setdefault(slug, []).append({
            "ts": ts_str, "reason": "external_close",
            "xp": None, "ab": None,
            "_ts": ts, "qty": trade.get("entry_qty_filled", 0),
            "kind": "redeem",
        })

# --- calcula resultado de redeems ---
def redeem_pnl(slug, ep, qty):
    """Retorna (pnl, label) para trades que foram a redeem ou external_close."""
    final = redeem_finals.get(slug)
    rd = redeems.get(slug, {})
    ab = rd.get("active_bid") or 0

    # 1. Dado inline pelo runner (logs novos)
    if final and final.get("pnl_usd") is not None:
        pnl = final["pnl_usd"]
        xp = final.get("exit_price", 1.0 if pnl > 0 else 0.0)
        label = "WIN (redeem=$1)" if xp == 1.0 else "LOSS (redeem=$0)"
        return pnl, label

    if rd.get("ext_pnl_usd") is not None:
        pnl = rd["ext_pnl_usd"]
        return pnl, "LOSS (ext_close@0)"

    # 2. Inferência por collateral (logs antigos)
    if final:
        col = final.get("collat") or 0
        tb = final.get("token_bal") or 0
        if col > 0 and tb == 0:
            pnl = round((1.0 - ep) * qty, 4)
            return pnl, "WIN (redeem=$1)"
        elif tb == 0 and col == 0:
            pnl = round((0.0 - ep) * qty, 4)
            return pnl, "LOSS (redeem=$0)"

    if rd.get("external_close"):
        pnl = round((0.0 - ep) * qty, 4)
        return pnl, "LOSS (ext_close)"

    if ab >= 0.93:
        pnl = round((1.0 - ep) * qty, 4)
        return pnl, "WIN~(bid={:.2f})".format(ab)
    elif ab <= 0.10 and ab > 0:
        pnl = round((0.0 - ep) * qty, 4)
        return pnl, "LOSS~(bid={:.2f})".format(ab)
    return None, "UNKNOWN(bid={:.2f})".format(ab)


# --- tabela ---
print("{:>3} | {:>8} | {:>4} | {:>5} | {:>8} | {:>5} | {:>2} | {:>9} | {}".format(
    "#", "Entrada", "Side", "EP", "Saida", "Xp", "Q", "PnL", "Razao"))
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
        print("{:>3} | {} | {:>4} | {:.3f} |    ---   |  --- | {:>2.0f} | {:>9} | OPEN {}".format(
            i, en["ts"], en["side"] or "?", ep, qty, "--", en["_slug_s"]))
        nunk += 1
        continue

    ex = exs[0]
    reason = ex["reason"][:40]

    if ex["kind"] == "redeem":
        pnl, label = redeem_pnl(sl, ep, qty)
        if pnl is not None:
            total += pnl
            nw += int(pnl > 0)
            nl += int(pnl < 0)
            print("{:>3} | {} | {:>4} | {:.3f} | {:>8} | {:>5} | {:>2.0f} | {:>+9.4f} | {}".format(
                i, en["ts"], en["side"] or "?", ep, ex["ts"], "1.00" if pnl > 0 else "0.00", qty, pnl, label))
        else:
            print("{:>3} | {} | {:>4} | {:.3f} | {:>8} | {:>5} | {:>2.0f} | {:>9} | {} {}".format(
                i, en["ts"], en["side"] or "?", ep, ex["ts"], "???", qty, "???", label, reason))
            nunk += 1
    else:
        xp = ex.get("xp") or 0
        if ep > 0 and xp > 0:
            pnl = round((xp - ep) * qty, 4)
            total += pnl
            nw += int(pnl > 0)
            nl += int(pnl < 0)
            print("{:>3} | {} | {:>4} | {:.3f} | {:>8} | {:.3f} | {:>2.0f} | {:>+9.4f} | {}".format(
                i, en["ts"], en["side"] or "?", ep, ex["ts"], xp, qty, pnl, reason))
        else:
            print("{:>3} | {} | {:>4} | {:.3f} | {:>8} | {:>5} | {:>2.0f} | {:>9} | {}".format(
                i, en["ts"], en["side"] or "?", ep, ex["ts"], "???", qty, "???", reason))
            nunk += 1

print("-" * 95)
nc = nw + nl
wr = "{:.0f}%".format(nw / nc * 100) if nc else "n/a"
print("Trades: {}  ({} W / {} L  WR={})   Inconclusos: {}".format(nc, nw, nl, wr, nunk))
print("PnL total sessao: {:>+.4f} USD".format(total))
