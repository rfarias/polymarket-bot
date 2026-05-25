"""Trades recentes do AR runner em ordem cronológica."""
import sys, io, json
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

results = []
for s in sorted(Path('logs').glob('current_almost_resolved_real_20260522_*')):
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
                'ts': e.get('ts', 0),
                'sess': s.name[-6:],
                'slug': str(td.get('event_slug', ''))[-15:],
                'side': td.get('side', ''),
                'entry': float(td.get('entry_price') or 0),
                'qty': float(td.get('entry_qty_filled') or 0),
                'fill_type': t,
            }
        elif t in ('exit', 'flat') and cur_fill:
            eq = float(td.get('exit_qty_filled') or 0)
            ep = float(td.get('exit_price_posted') or 0)
            if eq > 0:
                pnl = round((ep - cur_fill['entry']) * cur_fill['qty'], 4)
                results.append({**cur_fill, 'exit': ep, 'pnl': pnl})
                cur_fill = None
        elif t == 'resolution_win_awaiting_redeem' and cur_fill:
            pnl = round((1.0 - cur_fill['entry']) * cur_fill['qty'], 4)
            results.append({**cur_fill, 'exit': 1.0, 'pnl': pnl, 'held': True})
            cur_fill = None

print(f'Total trades fechados: {len(results)}')
wins = [r for r in results if r['pnl'] > 0]
losses = [r for r in results if r['pnl'] < 0]
breaks = [r for r in results if r['pnl'] == 0]
print(f'Wins: {len(wins)}  Losses: {len(losses)}  Break-even: {len(breaks)}')
print(f'PnL total: {sum(r["pnl"] for r in results):+.4f}')
print()
print('--- Ultimos 16 trades ---')
for r in results[-16:]:
    held_m = ' [held]' if r.get('held') else ''
    double_m = ' [DOUBLE]' if r.get('qty', 0) > 6 else ''
    result_tag = 'WIN ' if r['pnl'] > 0 else ('LOSS' if r['pnl'] < 0 else 'EVEN')
    sess = r['sess']
    side = r['side']
    entry = r['entry']
    exit_p = r['exit']
    qty = r['qty']
    pnl = r['pnl']
    print(f"  [{result_tag}] {sess} {side} @{entry:.3f}->{exit_p:.3f} x{qty} | PnL={pnl:+.4f}{held_m}{double_m}")
