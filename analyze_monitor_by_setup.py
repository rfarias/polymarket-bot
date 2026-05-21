"""
analyze_monitor_by_setup.py

Simula trades por setup nos logs do run_market_monitor.py.

Setups:
  AR  — almost_resolved: winner>=0.88, secs<=120, entry=winner_bid, hold to $1
  SC  — spot_scalp: spot_delta_15s direcional, entry=lado do movimento, target/stop 2T
  MM  — market_maker: mid 0.38-0.62, compra lado barato, target 1T, stop 3T
  RS  — reversal_sniper: winner>=0.88 e caindo rápido (sem sinais nesta sessão)
"""
from __future__ import annotations
import json, sys, io
from pathlib import Path
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

QTY  = 6.0
TICK = 0.01


# ─── loaders ────────────────────────────────────────────────────────────────

def load(log_path: Path):
    rows = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
                if d.get("type") == "monitor_snap":
                    rows.append(d)
            except Exception:
                pass
    return rows


# ─── AR simulation ──────────────────────────────────────────────────────────

def sim_ar_slug(slist, stop_ticks=2, entry_min=0.88, max_entry_secs=120, max_end_secs=35):
    current = [s for s in slist if s["slot"] == "current"]
    if not current:
        return None

    entry = None
    for s in current:
        ws = s.get("winner_side")
        if not ws or not s.get("sig_ar"):
            continue
        wb = s["up_bid"] if ws == "UP" else s["down_bid"]
        secs = s.get("secs") or 999
        if wb >= entry_min and secs <= max_entry_secs:
            entry = s
            break
    if not entry:
        return None

    side = entry["winner_side"]
    ep   = entry["up_bid"] if side == "UP" else entry["down_bid"]
    sp   = round(ep - stop_ticks * TICK, 4)
    post = [s for s in current if s["ts"] > entry["ts"]]
    if not post:
        return None

    gap = 0
    worst = ep
    for s in post:
        wb = s["up_bid"] if side == "UP" else s["down_bid"]
        worst = min(worst, wb)
        if wb <= 0.0:
            gap += 1
            if gap >= 2:
                return dict(setup="AR", outcome="BOOK_GAP", pnl=round(-ep * QTY, 4),
                            ep=ep, exit=0.0, entry_secs=entry.get("secs"), worst=worst)
        else:
            gap = 0
        if wb <= sp:
            return dict(setup="AR", outcome="STOP", pnl=round((wb - ep) * QTY, 4),
                        ep=ep, exit=wb, entry_secs=entry.get("secs"), worst=worst)

    last = post[-1]
    lw   = last["up_bid"] if side == "UP" else last["down_bid"]
    if last.get("secs") and last["secs"] <= max_end_secs:
        if lw >= 0.85:
            return dict(setup="AR", outcome="WIN", pnl=round((1.0 - ep) * QTY, 4),
                        ep=ep, exit=1.0, entry_secs=entry.get("secs"), worst=worst)
        return dict(setup="AR", outcome="REVERSAL", pnl=round((lw - ep) * QTY, 4),
                    ep=ep, exit=lw, entry_secs=entry.get("secs"), worst=worst)
    return None


# ─── SC simulation ──────────────────────────────────────────────────────────

def sim_sc_slug(slist, stop_ticks=2, target_ticks=2, max_hold=30, max_entry_secs=200):
    sc_cur = [s for s in slist if s.get("sig_sc") and s["slot"] == "current"]
    if not sc_cur:
        return []

    # agrupa em clusters (gap > 8s = novo sinal)
    clusters = [[sc_cur[0]]]
    for s in sc_cur[1:]:
        if s["ts"] - clusters[-1][-1]["ts"] > 8.0:
            clusters.append([s])
        else:
            clusters[-1].append(s)

    results = []
    all_cur = [s for s in slist if s["slot"] == "current"]

    for cl in clusters:
        entry = cl[0]
        secs  = entry.get("secs") or 999
        if secs > max_entry_secs:
            continue
        d15 = entry.get("sc_d15") or 0.0
        if d15 == 0:
            continue

        side = "UP" if d15 > 0 else "DOWN"
        ep   = entry["up_bid"] if side == "UP" else entry["down_bid"]

        # SC só vale quando mercado ainda não está muito direcional
        if ep > 0.70 or ep < 0.30 or ep <= 0:
            continue

        sp = round(ep - stop_ticks  * TICK, 4)
        tp = round(ep + target_ticks * TICK, 4)

        post = [s for s in all_cur
                if entry["ts"] < s["ts"] <= entry["ts"] + max_hold]
        if not post:
            results.append(dict(setup="SC", outcome="UNRESOLVED", pnl=0,
                                ep=ep, d15=d15, entry_secs=secs))
            continue

        hit = False
        for s in post:
            wb = s["up_bid"] if side == "UP" else s["down_bid"]
            if wb >= tp:
                results.append(dict(setup="SC", outcome="TARGET",
                                    pnl=round((tp - ep) * QTY, 4),
                                    ep=ep, d15=d15, entry_secs=secs, exit=wb))
                hit = True
                break
            if wb <= sp:
                results.append(dict(setup="SC", outcome="STOP",
                                    pnl=round((wb - ep) * QTY, 4),
                                    ep=ep, d15=d15, entry_secs=secs, exit=wb))
                hit = True
                break
        if not hit:
            last = post[-1]
            lw = last["up_bid"] if side == "UP" else last["down_bid"]
            results.append(dict(setup="SC", outcome="TIMEOUT",
                                pnl=round((lw - ep) * QTY, 4),
                                ep=ep, d15=d15, entry_secs=secs, exit=lw))
    return results


# ─── MM simulation ──────────────────────────────────────────────────────────

def sim_mm_slug(slist, stop_ticks=3, target_ticks=1, min_secs=60, max_secs=240, max_end_secs=35):
    current = [s for s in slist if s["slot"] == "current"]
    if not current:
        return None

    entry = None
    for s in current:
        if not s.get("sig_mm"):
            continue
        secs = s.get("secs") or 0
        if not (min_secs <= secs <= max_secs):
            continue
        up_bid = s.get("up_bid") or 0
        up_ask = s.get("up_ask") or 0
        dn_bid = s.get("down_bid") or 0
        dn_ask = s.get("down_ask") or 0
        if up_bid <= 0 or dn_bid <= 0 or up_ask <= 0 or dn_ask <= 0:
            continue
        mid_up = (up_bid + up_ask) / 2
        mid_dn = (dn_bid + dn_ask) / 2
        if not (0.38 <= mid_up <= 0.62 and 0.38 <= mid_dn <= 0.62):
            continue
        sp_up = round(up_ask - up_bid, 3)
        sp_dn = round(dn_ask - dn_bid, 3)
        if sp_up <= 0 and sp_dn <= 0:
            continue

        # escolher lado com mid mais distante do centro (mais valor de reversion)
        if abs(0.50 - mid_up) >= abs(0.50 - mid_dn):
            side = "UP" if mid_up < 0.50 else "DOWN"
        else:
            side = "DOWN" if mid_dn < 0.50 else "UP"

        ep     = up_bid if side == "UP" else dn_bid
        spread = sp_up  if side == "UP" else sp_dn
        if spread <= 0:
            continue

        entry       = s
        entry_side  = side
        entry_ep    = ep
        entry_spread= spread
        break

    if not entry:
        return None

    sp = round(entry_ep - stop_ticks  * TICK, 4)
    tp = round(entry_ep + target_ticks * TICK, 4)
    post = [s for s in current if s["ts"] > entry["ts"]]
    if not post:
        return None

    gap = 0
    for s in post:
        wb = s["up_bid"] if entry_side == "UP" else s["down_bid"]
        if wb <= 0:
            gap += 1
            if gap >= 2:
                return dict(setup="MM", outcome="BOOK_GAP",
                            pnl=round(-entry_ep * QTY, 4),
                            ep=entry_ep, spread=entry_spread,
                            entry_secs=entry.get("secs"))
        else:
            gap = 0
        if wb <= sp:
            return dict(setup="MM", outcome="STOP",
                        pnl=round((wb - entry_ep) * QTY, 4),
                        ep=entry_ep, spread=entry_spread,
                        entry_secs=entry.get("secs"), exit=wb)
        if wb >= tp:
            return dict(setup="MM", outcome="TARGET",
                        pnl=round((tp - entry_ep) * QTY, 4),
                        ep=entry_ep, spread=entry_spread,
                        entry_secs=entry.get("secs"), exit=wb)

    last = post[-1]
    lw   = last["up_bid"] if entry_side == "UP" else last["down_bid"]
    secs_l = last.get("secs") or 999
    if secs_l <= max_end_secs:
        out = "WIN" if lw >= 0.85 else "LOSS"
        pnl = round(((1.0 if out == "WIN" else lw) - entry_ep) * QTY, 4)
        return dict(setup="MM", outcome=out, pnl=pnl,
                    ep=entry_ep, spread=entry_spread,
                    entry_secs=entry.get("secs"), exit=lw)
    return None


# ─── report ─────────────────────────────────────────────────────────────────

SEP = "─" * 60

def _report(trades):
    if not trades:
        print("    sem trades")
        return
    resolved = [t for t in trades if t["outcome"] != "UNRESOLVED"]
    if not resolved:
        print(f"    0 resolvidos ({len(trades)} unresolved)")
        return
    wins     = [t for t in resolved if t["outcome"] in ("WIN", "TARGET")]
    stops    = [t for t in resolved if t["outcome"] == "STOP"]
    gaps     = [t for t in resolved if t["outcome"] == "BOOK_GAP"]
    losses   = [t for t in resolved if t["outcome"] in ("LOSS", "REVERSAL")]
    timeouts = [t for t in resolved if t["outcome"] == "TIMEOUT"]

    total = sum(t["pnl"] for t in resolved)
    wr    = 100 * len(wins) / len(resolved) if resolved else 0

    def pnl_s(ts): return f"${sum(t['pnl'] for t in ts):+7.2f}"

    print(f"    Trades: {len(resolved)}  (+{len(trades)-len(resolved)} unresolved)")
    print(f"    WIN/TARGET  {len(wins):3d}   {pnl_s(wins)}")
    print(f"    STOP        {len(stops):3d}   {pnl_s(stops)}", end="")
    if stops:
        exits = [t.get("exit", 0) for t in stops]
        print(f"   exit_bid avg={sum(exits)/len(exits):.3f} min={min(exits):.3f}", end="")
    print()
    if gaps:
        print(f"    BOOK_GAP    {len(gaps):3d}   {pnl_s(gaps)}")
    if losses:
        print(f"    LOSS/REV    {len(losses):3d}   {pnl_s(losses)}")
    if timeouts:
        print(f"    TIMEOUT     {len(timeouts):3d}   {pnl_s(timeouts)}")
    print(f"    Win rate: {wr:.1f}%   PnL: ${total:+.2f}   avg/trade: ${total/len(resolved):+.4f}")


def main():
    logs = sorted(Path("logs").glob("market_monitor_*.jsonl"), reverse=True)
    if not logs:
        print("Nenhum log encontrado em logs/")
        sys.exit(1)
    log_path = logs[0]
    print(f"Log: {log_path.name}  ({log_path.stat().st_size/1024:.0f} KB)")

    rows = load(log_path)
    by_slug = defaultdict(list)
    for s in rows:
        by_slug[s["slug"]].append(s)

    ar_trades, sc_trades, mm_trades = [], [], []
    for slug, slist in sorted(by_slug.items(), key=lambda x: x[1][0]["ts"]):
        r = sim_ar_slug(slist)
        if r:
            ar_trades.append(r)
        sc_trades.extend(sim_sc_slug(slist))
        r2 = sim_mm_slug(slist)
        if r2:
            mm_trades.append(r2)

    print()
    print("╔════════════════════════════════════════════════════════════╗")
    print("║  RESULTADOS POR SETUP — 20/05 17:11 → 21/05 07:00  6 sh  ║")
    print("╚════════════════════════════════════════════════════════════╝")

    # ── AR ──
    print(f"\n{SEP}")
    print("  [AR] ALMOST_RESOLVED")
    print("       winner >= 0.88  |  secs <= 120  |  stop 2T  |  hold to $1")
    print(SEP)
    _report(ar_trades)

    # AR sensibilidade
    resolved_ar = [t for t in ar_trades if t["outcome"] != "UNRESOLVED"]
    print()
    print("    Sensibilidade secs de entrada:")
    for ms in [30, 60, 90, 120]:
        ts = [t for t in resolved_ar if (t.get("entry_secs") or 999) <= ms]
        tw = [t for t in ts if t["outcome"] == "WIN"]
        tp = sum(t["pnl"] for t in ts)
        wr2 = 100 * len(tw) / len(ts) if ts else 0
        print(f"    secs<={ms:3d}: {len(ts):3d} trades  WR={wr2:.0f}%  PnL=${tp:+.2f}")

    print()
    print("    Sensibilidade entry_price:")
    for th in [0.88, 0.90, 0.92, 0.94, 0.96]:
        ts = [t for t in resolved_ar if t["ep"] >= th]
        tw = [t for t in ts if t["outcome"] == "WIN"]
        tp = sum(t["pnl"] for t in ts)
        wr2 = 100 * len(tw) / len(ts) if ts else 0
        print(f"    ep>={th:.2f}: {len(ts):3d} trades  WR={wr2:.0f}%  PnL=${tp:+.2f}")

    # ── SC ──
    print(f"\n{SEP}")
    print("  [SC] SPOT_SCALP")
    print("       spot_delta_15s >= 2bps  |  mid 0.30-0.70  |  target +2T  |  stop -2T  |  max 30s")
    print(SEP)
    _report(sc_trades)

    sc_res = [t for t in sc_trades if t["outcome"] != "UNRESOLVED"]
    if sc_res:
        up_trades = [t for t in sc_res if t.get("d15", 0) > 0]
        dn_trades = [t for t in sc_res if t.get("d15", 0) < 0]
        print()
        for dir_, ts in [("UP (spot +)", up_trades), ("DOWN (spot -)", dn_trades)]:
            if not ts:
                continue
            tw = [t for t in ts if t["outcome"] in ("TARGET", "WIN")]
            tp = sum(t["pnl"] for t in ts)
            wr2 = 100 * len(tw) / len(ts)
            print(f"    {dir_:15}: {len(ts):3d} trades  WR={wr2:.0f}%  PnL=${tp:+.2f}")

    # ── MM ──
    print(f"\n{SEP}")
    print("  [MM] MARKET_MAKER")
    print("       mid 0.38-0.62  |  secs 60-240  |  target +1T  |  stop -3T")
    print(SEP)
    _report(mm_trades)

    mm_res = [t for t in mm_trades if t["outcome"] != "UNRESOLVED"]
    if mm_res:
        spreads = [t.get("spread", 0) for t in mm_res]
        print(f"\n    Spread médio na entrada: {sum(spreads)/len(spreads):.3f}  "
              f"(min {min(spreads):.3f}  max {max(spreads):.3f})")
        ep_vals = [t["ep"] for t in mm_res]
        print(f"    Entry_price avg: {sum(ep_vals)/len(ep_vals):.3f}")

    # ── RS ──
    print(f"\n{SEP}")
    print("  [RS] REVERSAL_SNIPER")
    print("       winner>=0.88  |  loser<=0.15  |  vel<=-0.005 abs/s")
    print(SEP)
    print("    0 sinais nesta sessão. Threshold corrigido — monitorar próxima.")

    # ── RESUMO ──
    print(f"\n{SEP}")
    print("  RESUMO COMPARATIVO")
    print(SEP)
    print(f"  {'Setup':12} {'Trades':>7} {'WR%':>6} {'PnL':>9} {'avg/trade':>10}")
    for name, trades in [("AR", ar_trades), ("SC", sc_trades), ("MM", mm_trades)]:
        res = [t for t in trades if t["outcome"] != "UNRESOLVED"]
        if not res:
            print(f"  {name:12} {'0':>7} {'—':>6} {'—':>9}")
            continue
        wins_r = [t for t in res if t["outcome"] in ("WIN", "TARGET")]
        tp     = sum(t["pnl"] for t in res)
        wr3    = 100 * len(wins_r) / len(res)
        print(f"  {name:12} {len(res):>7} {wr3:>5.1f}% ${tp:>+8.2f} ${tp/len(res):>+9.4f}")
    all_res = ([t for t in ar_trades if t["outcome"] != "UNRESOLVED"] +
               [t for t in sc_trades if t["outcome"] != "UNRESOLVED"] +
               [t for t in mm_trades if t["outcome"] != "UNRESOLVED"])
    all_pnl = sum(t["pnl"] for t in all_res)
    print(f"  {'TOTAL':12} {len(all_res):>7}        ${all_pnl:>+8.2f}")
    print()


if __name__ == "__main__":
    main()
