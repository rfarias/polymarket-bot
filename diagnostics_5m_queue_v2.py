from market.queue_5m_v2 import build_5m_queue_v2, UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1

print("[TEST] Starting 5m queue diagnostics v2...")

queue = build_5m_queue_v2()

current = queue.get("current")
next_1 = queue.get("next_1")
next_2 = queue.get("next_2")

print("\n[RESULT] 5m queue v2:")
print(
    f"current -> {current['seconds_to_end']}s | {current['title']} | slug={current['slug']}" if current else "current -> none"
)
print(
    f"next_1  -> {next_1['seconds_to_end']}s | {next_1['title']} | slug={next_1['slug']}" if next_1 else "next_1  -> none"
)
print(
    f"next_2  -> {next_2['seconds_to_end']}s | {next_2['title']} | slug={next_2['slug']}" if next_2 else "next_2  -> none"
)

print(
    f"\n[RULE] cancel unfinished 2-leg orders on next_1 when next_1 secs_to_end <= {UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1}"
)
print("[RULE] focus entry logic on next_1 and keep next_2 prepared for roll-forward")

print("\n[TEST] 5m queue diagnostics v2 finished 🚀")
