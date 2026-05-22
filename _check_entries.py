import json
from pathlib import Path

sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))
print(f'Sessoes: {len(sessions)}')

all_entries = []
for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]
    entries = {r['slug']: r for r in rows if r.get('type') == 'ee_paper_entry'}
    closed  = {r['slug']: r for r in rows if r.get('type') == 'ee_paper_closed'}
    for slug, e in entries.items():
        cl = closed.get(slug)
        outcome = cl['ee']['outcome'] if cl else 'aberta'
        pnl = cl['ee']['pnl'] if cl else None
        all_entries.append({
            'slug': slug[-6:],
            'side': e.get('side', ''),
            'ep': e.get('ep', 0),
            'secs': e.get('secs', 0),
            'el_vel': e.get('el', {}).get('el_vel', 0),
            'outcome': outcome,
            'pnl': pnl,
        })

print(f'\n{"#":>3}  {"Slug":>8}  {"Side":>4}  {"EP":>6}  {"secs":>5}  {"el_vel":>7}  {"Outcome":>14}  {"PnL":>8}')
print('-' * 72)
for i, e in enumerate(all_entries, 1):
    pnl_str = f'{e["pnl"]:+.2f}' if e['pnl'] is not None else 'aberta'
    vel_str = f'{e["el_vel"]:.3f}' if isinstance(e['el_vel'], float) else str(e['el_vel'])
    print(f'{i:>3}  {e["slug"]:>8}  {e["side"]:>4}  {e["ep"]:>6.3f}  {e["secs"]:>5}  {vel_str:>7}  {e["outcome"]:>14}  {pnl_str:>8}')

eps = [e['ep'] for e in all_entries]
print(f'\nTotal: {len(all_entries)} entradas')
print(f'EP range: {min(eps):.3f} -- {max(eps):.3f}  |  media: {sum(eps)/len(eps):.3f}')
