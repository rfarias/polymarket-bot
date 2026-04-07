import requests
from config.settings import GAMMA_API


def fetch_markets():
    url = f"{GAMMA_API}/markets"
    print(f"[API] Fetching markets from {url}")

    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        data = res.json()

        print(f"[API] Total markets received: {len(data)}")
        return data

    except Exception as e:
        print(f"[ERROR] Failed to fetch markets: {e}")
        return []


def filter_btc_markets(markets):
    btc_markets = []

    for m in markets:
        title = str(m.get("question", "")).lower()

        if "btc" in title or "bitcoin" in title:
            btc_markets.append(m)

    print(f"[FILTER] BTC-related markets: {len(btc_markets)}")
    return btc_markets
