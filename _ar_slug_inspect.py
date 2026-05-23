"""Inspeciona snapshots do AR runner antigo para o slug do trade EE."""
import json, sys, io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ar_old = Path("logs/current_almost_resolved_real_20260522_213652/current_almost_resolved_real.jsonl")
target_slug = "btc-updown-5m-1779497700"

startup = None
slug_snaps = []
with ar_old.open(encoding="utf-8", errors="replace") as f:
    for line in f:
        try:
            e = json.loads(line)
            t = e.get("type", "")
            if t == "startup":
                startup = e
            elif t == "snapshot" and e.get("current_slug") == target_slug:
                slug_snaps.append(e)
        except Exception:
            pass

if startup:
    cfg = startup.get("config") or startup
    print("=== Startup config ===")
    for k in ("ee_enabled", "ee_posts_enabled", "run_seconds", "qty"):
        print(f"  {k}: {cfg.get(k)}")

print(f"\nSnaps em {target_slug}: {len(slug_snaps)}")
for s in slug_snaps:
    secs = s.get("current_secs")
    mode = s.get("trade_mode", "?")
    el = s.get("early_leader") or {}
    ee_d = s.get("ee_tracker") or {}
    el_side = el.get("early_side")
    n240 = ee_d.get("n_s240", "?")
    n180 = ee_d.get("n_s180", "?")
    sig = ee_d.get("signal_ok", "?")
    print(f"  secs={secs}  mode={mode}  EL={el_side}  n240={n240}  n180={n180}  sig={sig}")
