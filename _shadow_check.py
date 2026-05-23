"""Analisa logs das sessoes AR (EE shadow) e EE paper em paralelo."""
import json, sys, io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logs = Path("logs")

def latest_dir(prefix):
    dirs = sorted(
        [d for d in logs.iterdir() if d.is_dir() and d.name.startswith(prefix)],
        key=lambda d: d.stat().st_mtime, reverse=True,
    )
    return dirs[0] if dirs else None

ar_dir = latest_dir("current_almost_resolved_real_")
ee_dir = latest_dir("ee_paper_")

def read_jsonl(path):
    events = []
    if not path or not path.exists():
        return events
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except Exception:
                pass
    return events

ar_file = ar_dir / "current_almost_resolved_real.jsonl" if ar_dir else None
ee_file = ee_dir / "ee_paper.jsonl" if ee_dir else None

ar_events = read_jsonl(ar_file)
ee_events = read_jsonl(ee_file)

ar_snaps   = [e for e in ar_events if e.get("type") == "snapshot"]
ar_shadows = [e for e in ar_events if e.get("type") == "ee_shadow_entry"]
ar_startup = next((e for e in ar_events if e.get("type") == "startup"), {})
ee_entries = [e for e in ee_events if e.get("type") == "ee_paper_entry"]
ee_closed  = [e for e in ee_events if e.get("type") == "ee_paper_closed"]
ee_snaps   = [e for e in ee_events if e.get("type") == "snapshot"]

print(f"\n{'='*55}")
print(f"  SHADOW CHECK")
print(f"  AR:  {ar_dir.name if ar_dir else 'nenhum'}")
print(f"  EE:  {ee_dir.name if ee_dir else 'nenhum'}")
print(f"{'='*55}")

print(f"\n--- AR runner ---")
print(f"  EE enabled   : {ar_startup.get('ee_enabled')}  posts={ar_startup.get('ee_posts_enabled')}")
print(f"  Snapshots    : {len(ar_snaps)}")
print(f"  EE shadows   : {len(ar_shadows)}")
if ar_snaps:
    last = ar_snaps[-1]
    slug = str(last.get("current_slug") or "")
    secs = last.get("current_secs")
    sig  = last.get("signal") or {}
    el   = last.get("early_leader") or {}
    ee_t = last.get("ee_tracker") or {}
    print(f"  Ultimo slug  : ...{slug[-25:]}")
    print(f"  Ultimo secs  : {secs}")
    print(f"  AR allow     : {sig.get('allow')}  side={sig.get('side')}")
    print(f"  EL side      : {el.get('early_side')}  f3={el.get('f3_ok')}  inv={el.get('inverted')}")
    if ee_t:
        print(f"  EE tracker   : side={ee_t.get('early_side')}  vel={ee_t.get('el_vel',0):+.3f}  sig={ee_t.get('signal_ok')}  n240={ee_t.get('n_s240')}  n180={ee_t.get('n_s180')}")

if ar_shadows:
    print(f"\n  Ultimas ee_shadow_entry:")
    for s in ar_shadows[-5:]:
        el = s.get("el") or {}
        print(f"    {s.get('side')}  ep={s.get('el_bid', 0):.3f}  vel={el.get('el_vel', 0):+.3f}  secs={s.get('secs')}")

print(f"\n--- EE paper runner ---")
print(f"  Snapshots    : {len(ee_snaps)}")
print(f"  EE entries   : {len(ee_entries)}")
print(f"  EE closed    : {len(ee_closed)}")

if ee_entries:
    print(f"\n  Ultimas ee_paper_entry:")
    for e in ee_entries[-5:]:
        el = e.get("el") or {}
        print(f"    {e.get('side')}  ep={e.get('ep', 0):.3f}  secs={e.get('secs')}  vel={el.get('el_vel', 0):+.3f}")

if ee_closed:
    total = sum((e.get("ee") or {}).get("pnl") or 0 for e in ee_closed)
    print(f"\n  Trades fechados:")
    for e in ee_closed[-5:]:
        ee_d = e.get("ee") or {}
        print(f"    {ee_d.get('outcome','?')}  pnl={ee_d.get('pnl', 0):+.4f}  side={ee_d.get('entry_side','?')}  ep={ee_d.get('entry_ep',0):.3f}")
    print(f"  PnL total: {total:+.4f}")

# Comparar slugs em comum
ar_slug_set = set(s.get("current_slug") for s in ar_snaps if s.get("current_slug"))
ee_slug_set = set(s.get("current_slug") or s.get("slug") for s in ee_snaps if s.get("current_slug") or s.get("slug"))
common = ar_slug_set & ee_slug_set
print(f"\n  Slugs em comum (ambos viram): {len(common)}")

print()
