"""Mostra estado EE paper ao vivo para o slug atual."""
import json, sys, io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ee_dir = sorted(
    [d for d in Path("logs").iterdir() if d.is_dir() and d.name.startswith("ee_paper_")],
    key=lambda d: d.stat().st_mtime, reverse=True,
)[0]

target = "1779501900"
snaps = []
entries = []
with (ee_dir / "ee_paper.jsonl").open(encoding="utf-8", errors="replace") as f:
    for line in f:
        try:
            e = json.loads(line)
            slug = str(e.get("current_slug") or e.get("slug") or "")
            if target in slug:
                if e.get("type") == "snapshot":
                    snaps.append(e)
                elif e.get("type") == "ee_paper_entry":
                    entries.append(e)
        except Exception:
            pass

print(f"Slug: ...{target}  |  snaps={len(snaps)}  entries={len(entries)}")
for s in snaps[-6:]:
    el = s.get("early_leader") or {}
    secs = s.get("current_secs") or s.get("secs_to_end")
    side = el.get("early_side")
    vel  = el.get("el_vel") or 0.0
    f3   = el.get("f3_ok")
    sig  = el.get("signal_ok")
    n240 = el.get("n_s240", 0)
    n180 = el.get("n_s180", 0)
    print(f"  secs={str(secs):>4}  EL={side}  vel={vel:+.3f}  f3={str(f3):<5}  sig={sig}  n240={n240} n180={n180}")

if entries:
    for e in entries:
        el = e.get("el") or {}
        print(f"\n  ENTRADA: {e.get('side')}  ep={e.get('ep', 0):.3f}  secs={e.get('secs')}  vel={el.get('el_vel', 0):+.3f}")
