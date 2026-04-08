import asyncio

from market.ws_5m_strategy_sim_v2 import simulate_5m_strategy_v2

print("[TEST] Starting 5m strategy simulation v2...")
asyncio.run(simulate_5m_strategy_v2(duration_seconds=20))
print("\n[TEST] 5m strategy simulation v2 finished 🚀")
