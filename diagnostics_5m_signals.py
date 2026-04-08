import asyncio

from market.ws_5m_signals import monitor_5m_signals

print("[TEST] Starting 5m signal diagnostics...")
asyncio.run(monitor_5m_signals(duration_seconds=20))
print("\n[TEST] 5m signal diagnostics finished 🚀")
