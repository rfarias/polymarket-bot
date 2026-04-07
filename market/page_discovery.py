import re
import requests

PAGE_URLS = [
    "https://polymarket.com/crypto/btc",
    "https://polymarket.com/crypto/5M",
    "https://polymarket.com/crypto/15M",
    "https://polymarket.com/crypto/hourly",
]

EVENT_HINTS = [
    "btc-updown-5m",
    "btc-updown-15m",
    "btc-updown-1h",
    "bitcoin-up-or-down",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
}


def fetch_page(url: str) -> str:
    print(f"[PAGE] Fetching {url}")
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
        print(f"[PAGE] OK {url} | size={len(res.text)}")
        return res.text
    except Exception as e:
        print(f"[ERROR] Failed to fetch {url}: {e}")
        return ""


def extract_btc_event_links(html: str):
    matches = re.findall(r'href=["\'](/event/[^"\']+)["\']', html, flags=re.IGNORECASE)
    urls = []
    seen = set()

    for path in matches:
        lower = path.lower()
        if not any(h in lower for h in EVENT_HINTS):
            continue
        full = f"https://polymarket.com{path}"
        if full in seen:
            continue
        seen.add(full)
        urls.append(full)

    return urls


def discover_btc_fast_market_links():
    found = []
    seen = set()

    for url in PAGE_URLS:
        html = fetch_page(url)
        if not html:
            continue

        links = extract_btc_event_links(html)
        print(f"[DISCOVERY] Links found in {url}: {len(links)}")

        for link in links:
            if link not in seen:
                seen.add(link)
                found.append(link)

    print(f"[DISCOVERY] Total BTC fast market links found: {len(found)}")
    return found
