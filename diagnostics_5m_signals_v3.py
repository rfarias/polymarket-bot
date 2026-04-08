import asyncio

from market.ws_5m_signals_v3 import monitor_5m_signals_v3

print("[TEST] Starting 5m signal diagnostics v3...")
asyncio.run(monitor_5m_signals_v3(duration_seconds=20))
print("\n[TEST] 5m signal diagnostics v3 finished 🚀")
