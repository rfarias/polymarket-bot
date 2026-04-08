import asyncio

from market.ws_5m_v2 import monitor_5m_queue_ws_v2

print("[TEST] Starting 5m websocket diagnostics v2...")
asyncio.run(monitor_5m_queue_ws_v2(duration_seconds=20))
print("\n[TEST] 5m websocket diagnostics v2 finished 🚀")
