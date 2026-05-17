"""
Verifica trades recentes da conta rfarias (maker_address funder).
Filtra pelo dia atual e mostra saldo + trades com outcome.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import dotenv_values
from datetime import datetime, timezone

creds = dotenv_values(".env.manual")
from market.polymarket_broker_v3 import PolymarketBrokerV3

b = PolymarketBrokerV3(
    host=creds.get("POLY_HOST", "https://clob.polymarket.com"),
    chain_id=int(creds.get("POLY_CHAIN_ID", 137)),
    private_key=creds.get("POLY_PRIVATE_KEY"),
    funder=creds.get("POLY_FUNDER"),
    signature_type=int(creds.get("POLY_SIGNATURE_TYPE", 1)),
    api_key=creds.get("POLY_API_KEY"),
    api_secret=creds.get("POLY_API_SECRET"),
    api_passphrase=creds.get("POLY_PASSPHRASE"),
)

MY_OWNER = "6d1c4072-762e-1650-5b41-7d55b22655c3"
MY_ADDRESS = "0x531bfECd0f4C62784B9c2FDc01fEA239182DE19f".lower()

# Saldo colateral
bal = b.get_balance_allowance(asset_type="COLLATERAL")
usd = round(float(bal.get("balance", 0)) / 1e6, 4)
print(f"Saldo USDC colateral: ${usd}")
print()

tr = b.client.get_trades()
print(f"Total trades na conta: {len(tr) if tr else 0}")

# Filtrar pelo dia 17/05/2026 00:00 UTC
today_ts_start = int(datetime(2026, 5, 17, 0, 0, 0, tzinfo=timezone.utc).timestamp())

my_trades = []
for t in (tr or []):
    mt = int(t.get("match_time", 0))
    if mt < today_ts_start:
        continue
    our_order = None
    for mo in t.get("maker_orders", []):
        if mo.get("owner") == MY_OWNER or (mo.get("maker_address") or "").lower() == MY_ADDRESS:
            our_order = mo
            break
    if our_order:
        dt = datetime.fromtimestamp(mt, tz=timezone.utc).astimezone()
        my_trades.append((dt, t, our_order))

my_trades.sort(key=lambda x: x[0])
print(f"Trades do dia 17/05 (nossa conta como maker): {len(my_trades)}")
print()

for dt, t, mo in my_trades:
    side = mo.get("side", "?")
    outcome = mo.get("outcome", "?")
    price = mo.get("price", "?")
    qty = mo.get("matched_amount", "?")
    status = t.get("status", "?")
    market = t.get("market", "?")[-12:]
    print(f"  {dt.strftime('%H:%M:%S')}  {side:4} {outcome:4}  price={price}  qty={qty}  status={status}  market=...{market}")
