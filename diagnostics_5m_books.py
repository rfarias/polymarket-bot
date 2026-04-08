from market.book_5m import fetch_5m_queue_with_books

print("[TEST] Starting 5m orderbook diagnostics...")

data = fetch_5m_queue_with_books()
queue = data["queue"]
slots = data["slots"]

print("\n[RESULT] 5m queue summary:")
for name in ("current", "next_1", "next_2"):
    item = queue.get(name)
    if item:
        print(f"- {name}: {item['seconds_to_end']}s | {item['title']} | slug={item['slug']}")
    else:
        print(f"- {name}: none")

for name in ("current", "next_1", "next_2"):
    slot = slots.get(name)
    print(f"\n[{name.upper()}]")
    if not slot:
        print("none")
        continue

    meta = slot.get("meta")
    if meta:
        print(f"market_question={meta['market_question']}")
        print(f"acceptingOrders={meta['acceptingOrders']} | enableOrderBook={meta['enableOrderBook']}")
        print(f"liquidityClob={meta['liquidityClob']} | volumeClob={meta['volumeClob']}")

    books = slot.get("books") or []
    yes_ask = None
    no_ask = None

    for entry in books:
        outcome = str(entry.get("outcome"))
        book = entry.get("book") or {}
        print(
            f"- outcome={outcome} | token_id={entry.get('token_id')} | best_bid={book.get('best_bid')} | best_ask={book.get('best_ask')} | tick_size={book.get('tick_size')} | min_order_size={book.get('min_order_size')} | last_trade_price={book.get('last_trade_price')}"
        )
        if outcome.lower() == "yes":
            yes_ask = book.get("best_ask")
        elif outcome.lower() == "no":
            no_ask = book.get("best_ask")

    if yes_ask is not None and no_ask is not None:
        total = round(float(yes_ask) + float(no_ask), 4)
        print(f"[ARB_CHECK] yes_best_ask + no_best_ask = {total}")

print("\n[TEST] 5m orderbook diagnostics finished 🚀")
