import json, glob
files = sorted(glob.glob('logs/current_almost_resolved_paper_202605*.jsonl'))
count = 0
for f in files[-3:]:
    with open(f, encoding='utf-8') as fp:
        for line in fp:
            line = line.strip()
            if not line: continue
            ev = json.loads(line)
            if ev.get('type') == 'exit':
                t = ev['trade']
                print(f"variant={t.get('setup_variant')} ep={t.get('entry_price')} xp={t.get('exit_price')} ticks={t.get('pnl_ticks')} quote={t.get('pnl_quote')} qty={t.get('qty')} reason={t.get('exit_reason')}")
                count += 1
                if count >= 5:
                    break
    if count >= 5:
        break
