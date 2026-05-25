"""Analisa a evolução de preço de um trade catastrófico pelo timestamp de fill."""
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

log = Path("logs/current_almost_resolved_real_20260517_185650/current_almost_resolved_real.jsonl")
events = []
with open(log, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except Exception:
                pass

# Encontrar o fill das 19:03
fill_slug = None
for e in events:
    if e.get("type") == "fill":
        ts = datetime.fromtimestamp(e["ts"], tz=timezone.utc).astimezone()
        if ts.hour == 19 and ts.minute < 10:
            fill_slug = e.get("trade", {}).get("event_slug", "")
            print(f"Fill: {ts.strftime('%H:%M:%S')}  slug={fill_slug}")
            tr = e.get("trade", {})
            print(f"  side={tr.get('side')}  entry={tr.get('entry_price')}  qty={tr.get('entry_qty_requested')}")
            print(f"  stop_price={tr.get('stop_price')}  target_price={tr.get('target_price')}")
            break

if not fill_slug:
    print("Fill nao encontrado")
    sys.exit(1)

print()
print("Evolucao do mercado apos fill (snapshots do slug):")
print(f"  {'Hora':10}  {'secs':5}  {'down_buy':9}  {'up_buy':7}  {'mode':20}")
for e in events:
    if e.get("type") != "snapshot":
        continue
    slug = e.get("trade", {}).get("event_slug") or e.get("current_slug", "")
    if slug != fill_slug:
        continue
    tr = e.get("trade", {})
    mode = tr.get("mode", "")
    if mode == "idle":
        continue
    sig = e.get("signal", {})
    ts = datetime.fromtimestamp(e["ts"], tz=timezone.utc).astimezone()
    down_buy = sig.get("down_buy", "?")
    up_buy = sig.get("up_buy", "?")
    secs = e.get("current_secs", "?")
    print(f"  {ts.strftime('%H:%M:%S')}  {str(secs):5}  {str(down_buy):9}  {str(up_buy):7}  {mode}")

print()
print("Eventos de controle:")
for e in events:
    t = e.get("type", "")
    if t not in ("exit_posted", "flat", "entry_blocked", "fill"):
        continue
    slug = e.get("trade", {}).get("event_slug", "")
    if slug != fill_slug:
        continue
    ts = datetime.fromtimestamp(e["ts"], tz=timezone.utc).astimezone()
    tr = e.get("trade", {})
    eo = e.get("exit_order") or {}
    extra = ""
    if t == "exit_posted":
        extra = f"reason={e.get('reason')}  stop={tr.get('stop_price')}"
    elif t == "flat":
        extra = f"exit_price={eo.get('price')}  status={eo.get('status')}  order_type={eo.get('order_type')}"
    print(f"  [{t}] {ts.strftime('%H:%M:%S')}  {extra}")
