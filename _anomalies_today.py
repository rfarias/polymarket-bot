"""Encontra writeoffs e exits suspeitos nas sessoes de hoje."""
import sys, io, json
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

for s in sorted(Path('logs').glob('current_almost_resolved_real_20260522_*')):
    jl = s / 'current_almost_resolved_real.jsonl'
    if not jl.exists():
        continue
    for line in jl.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get('type', '')
        td = e.get('trade', {})

        if t == 'resolution_loss_writeoff':
            slug = str(td.get('event_slug', ''))
            print(f"WRITEOFF  sess={s.name}  pnl={e.get('pnl_usd')}  slug=...{slug[-20:]}")

        if t in ('exit', 'flat'):
            ep = float(td.get('exit_price_posted') or 0)
            eq = float(td.get('exit_qty_filled') or 0)
            entry_p = float(td.get('entry_price') or 0)
            slug = str(td.get('event_slug', ''))[-20:]
            if ep < 0.5 and eq > 0:
                reason = e.get('reason', '')
                print(f"BAD_EXIT  sess={s.name}  exit={ep}  entry={entry_p}  qty={eq}  reason={reason}  slug=...{slug}")
