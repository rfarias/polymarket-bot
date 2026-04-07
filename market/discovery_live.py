import requests
from datetime import datetime, timezone
from config.settings import GAMMA_API


def fetch_markets(limit=500):
    url = f"{GAMMA_API}/markets"
    print(f"[API] Fetching markets from {url}")

    try:
        res = requests.get(url, params={"limit": limit}, timeout=15)
        res.raise_for_status()
        data = res.json()

        print(f"[API] Total markets received: {len(data)}")
        return data

    except Exception as e:
        print(f"[ERROR] Failed to fetch markets: {e}")
        return []


def _parse_end_date(raw):
    if not raw:
        return None

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def filter_relevant_btc_markets(markets, max_seconds_ahead=600):
    results = []
    now = datetime.now(timezone.utc)

    for m in markets:
        title = str(m.get("question", "")).lower()

        if not ("btc" in title or "bitcoin" in title):
            continue

        if m.get("active") is not True:
            continue

        if m.get("closed") is True:
            continue

        end_dt = _parse_end_date(m.get("endDate"))
        if not end_dt:
            continue

        time_diff = (end_dt - now).total_seconds()

        # Janela atual e próximas janelas próximas
        if 0 < time_diff <= max_seconds_ahead:
            results.append(m)

    results.sort(key=lambda x: x.get("endDate", ""))
    print(f"[FILTER] Relevant BTC markets (current + next windows): {len(results)}")
    return results
