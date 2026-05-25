"""PnL do dia atual (22/05/2026)."""
import sys, io, json
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sessions = sorted(Path('logs').glob('current_almost_resolved_real_20260522_*'))
fills = []
exits = []
writeoffs = []
doubles = []

for s in sessions:
    jl = s / 'current_almost_resolved_real.jsonl'
    if not jl.exists():
        continue
    cur_fill = None
    for line in jl.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get('type', '')
        td = e.get('trade', {})
        if t in ('fill', 'fill_on_invalid_signal'):
            cur_fill = {
                'slug': td.get('event_slug', ''),
                'entry': float(td.get('entry_price') or 0),
                'qty': float(td.get('entry_qty_filled') or 0),
                'side': td.get('side', ''),
                'fill_type': t,
            }
            fills.append(cur_fill)
        elif t in ('exit', 'flat') and cur_fill:
            eq = float(td.get('exit_qty_filled') or 0)
            ep = float(td.get('exit_price_posted') or 0)
            if eq > 0:
                pnl = round((ep - cur_fill['entry']) * cur_fill['qty'], 4)
                exits.append({
                    'slug': cur_fill['slug'],
                    'entry': cur_fill['entry'],
                    'exit': ep,
                    'qty': cur_fill['qty'],
                    'side': cur_fill['side'],
                    'pnl': pnl,
                })
                cur_fill = None
        elif t == 'resolution_win_awaiting_redeem' and cur_fill:
            pnl = round((1.0 - cur_fill['entry']) * cur_fill['qty'], 4)
            exits.append({
                'slug': cur_fill['slug'],
                'entry': cur_fill['entry'],
                'exit': 1.0,
                'qty': cur_fill['qty'],
                'side': cur_fill['side'],
                'pnl': pnl,
                'held_res': True,
            })
            cur_fill = None
        elif t == 'resolution_loss_writeoff':
            writeoffs.append({
                'pnl': e.get('pnl_usd', 0),
                'slug': td.get('event_slug', ''),
            })
        elif t == 'double_entry_detected':
            doubles.append(e)

print('=== HOJE 2026-05-22 ===')
print(f'Sessoes: {len(sessions)}')
print(f'Fills: {len(fills)}  Exits fechados: {len(exits)}  Writeoffs: {len(writeoffs)}  Doubles detectados: {len(doubles)}')
print()

total_pnl = 0.0
for ex in exits:
    double_m = ' [DOUBLE]' if ex.get('qty', 0) > 6 else ''
    held_m = ' [held_res]' if ex.get('held_res') else ''
    slug_s = str(ex['slug'])[-15:]
    print(f"  {ex['side']} @{ex['entry']:.3f}->{ex['exit']:.3f} x{ex['qty']} | PnL={ex['pnl']:+.4f} | ...{slug_s}{double_m}{held_m}")
    total_pnl += ex['pnl']

for wo in writeoffs:
    slug_s = str(wo['slug'])[-15:]
    pnl_v = float(wo['pnl'] or 0)
    print(f"  WRITEOFF | PnL={pnl_v:+.4f} | ...{slug_s}")
    total_pnl += pnl_v

print()
print(f'PnL hoje: {total_pnl:+.4f} USD')
if fills and len(exits) < len(fills):
    print(f'Posicoes em aberto (sem exit registrado): {len(fills) - len(exits)}')
