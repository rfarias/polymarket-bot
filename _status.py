import json, datetime, os, glob

# Pegar a sessao mais recente
dirs = sorted(glob.glob(r'logs\current_almost_resolved_real_2026*'))
log = os.path.join(dirs[-1], 'current_almost_resolved_real.jsonl') if dirs else None

if not log or not os.path.exists(log):
    print("Nenhum log encontrado")
    exit()

def fmt(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime('%H:%M:%S')
    except:
        return str(ts)

events = []
last_snap = None
with open(log) as f:
    for line in f:
        try:
            j = json.loads(line)
            ev = j.get('event') or j.get('type')
            if ev == 'snapshot':
                last_snap = j
            elif ev:
                events.append((j.get('ts', 0), ev, j))
        except:
            pass

session = dirs[-1].split('\\')[-1]
print(f"Sessao: {session}")
print(f"Eventos nao-snapshot: {len(events)}")
print()
print("Ultimos 15 eventos:")
for ts, ev, j in events[-15:]:
    tr = j.get('trade', {})
    extra = ''
    if ev in ('fill', 'trade_opened_from_fill'):
        extra = ' {} @ {} qty={}'.format(tr.get('side'), tr.get('entry_price'), tr.get('entry_qty_filled'))
    elif ev == 'startup':
        extra = ' hold_winner={} passive_only={}'.format(
            j.get('hold_winner_to_resolution'), j.get('passive_capture_only'))
    elif ev in ('flat', 'awaiting_redeem', 'redeem_flat', 'awaiting_redeem_balance_lag'):
        extra = ' {} reason={}'.format(tr.get('side'), str(tr.get('last_reason', ''))[:60])
    elif ev == 'exception':
        extra = ' ' + str(j.get('error', ''))[:80]
    print('  [{}] {}{}'.format(fmt(ts), ev, extra))

if last_snap:
    tr = last_snap.get('trade', {})
    sig = last_snap.get('signal', {})
    print()
    print("Estado atual (ultimo snapshot):")
    print('  mode={} secs_to_end={} allow={}'.format(
        tr.get('mode'), sig.get('secs_to_end'), sig.get('allow')))
    print('  reason={}'.format(sig.get('reason')))
