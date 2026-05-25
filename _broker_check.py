"""Verifica ordens abertas e saldo do token em awaiting_redeem."""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from market.polymarket_broker_v3 import PolymarketBrokerV3
import json

broker = PolymarketBrokerV3.from_env()

print("=== ORDENS ABERTAS NO CLOB ===")
orders = broker.get_open_orders()[:50]
print(f"Total: {len(orders)}")
for o in orders:
    print(f"  id={o.order_id[:20]}  side={getattr(o,'side','')}  "
          f"token={str(getattr(o,'token_id',''))[:24]}  "
          f"size={getattr(o,'size','')}  matched={getattr(o,'size_matched','')}  "
          f"status={getattr(o,'status','')}")

# Token do trade em awaiting_redeem
token_id = "10116774110966694468023543883656643905523959827494217844716935582328135102545"
print(f"\n=== SALDO TOKEN DOWN awaiting_redeem ===")
try:
    p = broker.get_balance_allowance(asset_type="CONDITIONAL", token_id=token_id)
    qty = round(float(p.get("balance", 0)) / 1_000_000.0, 6)
    print(f"  token_id={token_id[:30]}...")
    print(f"  qty={qty}")
except Exception as e:
    print(f"  Erro: {e}")

print("\n=== COLLATERAL ===")
try:
    p2 = broker.get_balance_allowance(asset_type="COLLATERAL")
    usdc = round(float(p2.get("balance", 0)) / 1_000_000.0, 4)
    print(f"  USDC: ${usdc}")
except Exception as e:
    print(f"  Erro: {e}")
