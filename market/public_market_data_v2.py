from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

import requests

CLOB_API = "https://clob.polymarket.com"
TIMEOUT = 3.0


def fetch_price(token_id: str, side: str) -> Optional[float]:
    token_id = str(token_id) if token_id is not None else ""
    side = str(side).upper()
    if not token_id or side not in ("BUY", "SELL"):
        return None
    url = f"{CLOB_API}/price"
    try:
        res = requests.get(url, params={"token_id": token_id, "side": side}, timeout=TIMEOUT)
        res.raise_for_status()
        data = res.json() or {}
        value = data.get("price")
        return float(value) if value is not None else None
    except Exception as e:
        print(f"[ERROR] Failed to fetch price for token={token_id} side={side}: {e}")
        return None


def fetch_token_executable_prices(token_id: str) -> Dict[str, Optional[float]]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        buy_future = pool.submit(fetch_price, token_id, "BUY")
        sell_future = pool.submit(fetch_price, token_id, "SELL")
        return {
            "BUY": buy_future.result(),
            "SELL": sell_future.result(),
        }
