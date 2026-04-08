import asyncio

from market.ws_5m_strategy_sim import simulate_5m_strategy

print("[TEST] Starting 5m strategy simulation...")
asyncio.run(simulate_5m_strategy(duration_seconds=20))
print("\n[TEST] 5m strategy simulation finished 🚀")
