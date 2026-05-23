"""Lê eventos brutos do EE paper log mais recente."""
import json, sys, io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logs = Path("logs")
ee_dirs = sorted(
    [d for d in logs.iterdir() if d.is_dir() and d.name.startswith("ee_paper_")],
    key=lambda d: d.stat().st_mtime, reverse=True,
)
if not ee_dirs:
    print("Nenhum log EE paper encontrado.")
    sys.exit(1)

ee = ee_dirs[0]
f = ee / "ee_paper.jsonl"
print(f"Log: {f}\n")

entries, closed, snaps = [], [], []
with f.open(encoding="utf-8", errors="replace") as fh:
    for line in fh:
        try:
            e = json.loads(line)
            t = e.get("type", "")
            if t == "ee_paper_entry":
                entries.append(e)
            elif t == "ee_paper_closed":
                closed.append(e)
            elif t == "snapshot":
                snaps.append(e)
        except Exception:
            pass

print("=== ee_paper_entry ===")
for e in entries:
    print(json.dumps(e, ensure_ascii=False, indent=2))

print("\n=== ee_paper_closed ===")
for e in closed:
    print(json.dumps(e, ensure_ascii=False, indent=2))

print(f"\nTotal snapshots: {len(snaps)}")
if snaps:
    last = snaps[-1]
    slug = str(last.get("slug") or "")
    secs = last.get("secs_to_end")
    el = last.get("early_leader") or {}
    print(f"Ultimo slug: ...{slug[-25:]}  secs={secs}")
    print(f"EL: side={el.get('early_side')} f3={el.get('f3_ok')} vel={el.get('el_vel')} signal_ok={el.get('signal_ok')}")
