"""Verifica resultado do mercado btc-updown-5m-1779412500."""
import sys, io, json, requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from dotenv import load_dotenv
load_dotenv()

slug = 'btc-updown-5m-1779412500'
GAMMA_API = 'https://gamma-api.polymarket.com'

res = requests.get(f"{GAMMA_API}/events/slug/{slug}", timeout=20)
event = res.json()

print(f"Titulo: {event.get('title', '')}")
print(f"Active: {event.get('active')}  Closed: {event.get('closed')}")
print(f"Resolved: {event.get('resolved')}  Resolution: {event.get('resolution')}")

markets = event.get('markets') or []
for i, m in enumerate(markets):
    print(f"\n  Market[{i}]:")
    print(f"    question: {m.get('question','')}")

    # outcomePrices e outcomes podem ser strings JSON
    op_raw = m.get('outcomePrices', '[]')
    outcomes_raw = m.get('outcomes', '[]')
    try:
        op = json.loads(op_raw) if isinstance(op_raw, str) else op_raw
    except:
        op = op_raw
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    except:
        outcomes = outcomes_raw

    print(f"    outcomes: {outcomes}")
    print(f"    outcomePrices: {op}")
    print(f"    winner: {m.get('winner')}")
    print(f"    resolved: {m.get('resolved')}")
    print(f"    lastTradePrice: {m.get('lastTradePrice')}")

    # Token IDs
    tids_raw = m.get('clob_token_ids') or m.get('clobTokenIds') or '[]'
    try:
        tids = json.loads(tids_raw) if isinstance(tids_raw, str) else tids_raw
    except:
        tids = []
    for j, tid in enumerate(tids):
        label = outcomes[j] if isinstance(outcomes, list) and j < len(outcomes) else f'token{j}'
        price = op[j] if isinstance(op, list) and j < len(op) else '?'
        print(f"    [{label}] price={price}  token={str(tid)[:50]}")
