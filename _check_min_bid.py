import json
from pathlib import Path

sessions = sorted(Path("logs").glob("ee_paper_*/ee_paper.jsonl"))
entries = {}
snaps = {}
trades = []

for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding="utf-8").splitlines() if l.strip()]
    for r in rows:
        t = r.get("type")
        if t == "ee_paper_entry":
            entries[r["slug"]] = r
            snaps[r["slug"]] = []
        elif t == "snapshot" and r.get("current_slug") in entries:
            ep = r.get("ee_position")
            if ep and ep.get("state") in ("entry", "hedged"):
                slug = r["current_slug"]
                side = entries[slug]["side"]
                bid = r.get("current_exec", {}).get("up_bid" if side == "UP" else "down_bid", 0)
                snaps[slug].append(bid)
        elif t == "ee_paper_closed":
            slug = r["slug"]
            if slug not in entries:
                continue
            ee = r.get("ee", {})
            if ee.get("outcome") in (None, "MISSED"):
                entries.pop(slug, None)
                snaps.pop(slug, None)
                continue
            s = snaps.pop(slug, [])
            e = entries.pop(slug)
            min_b = round(min(s), 3) if s else None
            trades.append({
                "slug": slug[-6:],
                "side": e["side"],
                "ep": e["ep"],
                "outcome": ee["outcome"],
                "pnl": ee["pnl"],
                "min_bid": min_b,
                "n_snaps": len(s),
            })

print(f"{'#':>3}  {'Slug':>8}  {'Side':>4}  {'EP':>5}  {'Outcome':>12}  {'min_bid':>8}  {'<0.67':>7}  {'<0.65':>7}  {'PnL':>8}")
print("-" * 72)
for i, t in enumerate(trades, 1):
    mb = t["min_bid"]
    hit67 = "SIM !" if mb is not None and mb < 0.67 else "-"
    hit65 = "SIM !" if mb is not None and mb < 0.65 else "-"
    print(f"{i:>3}  {t['slug']:>8}  {t['side']:>4}  {t['ep']:>5.2f}  "
          f"{t['outcome']:>12}  {str(mb):>8}  {hit67:>7}  {hit65:>7}  {t['pnl']:>+8.2f}")
print("-" * 72)
wins   = [t for t in trades if t["outcome"] == "WIN"]
hedges = [t for t in trades if t["outcome"] == "WIN_HEDGE"]
if wins:
    min_win = min(t["min_bid"] for t in wins if t["min_bid"] is not None)
    print(f"WIN trades  : {len(wins):>3}  |  min_bid mais baixo entre WINs: {min_win:.3f}")
if hedges:
    max_hedge = max(t["min_bid"] for t in hedges if t["min_bid"] is not None)
    print(f"WIN_HEDGE   : {len(hedges):>3}  |  min_bid mais alto entre HEDGEs: {max_hedge:.3f}")
