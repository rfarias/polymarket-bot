from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from ev_scanner.utils.ev_calculator import calculate_edge, ev_per_dollar, should_enter, shares_for_bet
from ev_scanner.utils.logger import log_event
from ev_scanner.utils.polymarket_api import get_active_markets

# City slug → (lat, lon, tz) — baseado nas cidades encontradas na Polymarket
CITY_COORDS: dict[str, tuple[float, float, str]] = {
    "amsterdam":    (52.3676,   4.9041,   "Europe/Amsterdam"),
    "ankara":       (39.9334,  32.8597,   "Europe/Istanbul"),
    "atlanta":      (33.7490,  -84.3880,  "America/New_York"),
    "austin":       (30.2672,  -97.7431,  "America/Chicago"),
    "beijing":      (39.9042,  116.4074,  "Asia/Shanghai"),
    "buenos-aires": (-34.6037, -58.3816,  "America/Argentina/Buenos_Aires"),
    "busan":        (35.1796,  129.0756,  "Asia/Seoul"),
    "cape-town":    (-33.9249,  18.4241,  "Africa/Johannesburg"),
    "chengdu":      (30.5728,  104.0668,  "Asia/Shanghai"),
    "chicago":      (41.8781,  -87.6298,  "America/Chicago"),
    "chongqing":    (29.5630,  106.5516,  "Asia/Shanghai"),
    "dallas":       (32.7767,  -96.7970,  "America/Chicago"),
    "denver":       (39.7392, -104.9903,  "America/Denver"),
    "guangzhou":    (23.1291,  113.2644,  "Asia/Shanghai"),
    "helsinki":     (60.1699,   24.9384,  "Europe/Helsinki"),
    "hong-kong":    (22.3193,  114.1694,  "Asia/Hong_Kong"),
    "houston":      (29.7604,  -95.3698,  "America/Chicago"),
    "istanbul":     (41.0082,   28.9784,  "Europe/Istanbul"),
    "jeddah":       (21.3891,   39.8579,  "Asia/Riyadh"),
    "jinan":        (36.6512,  117.1201,  "Asia/Shanghai"),
    "karachi":      (24.8607,   67.0011,  "Asia/Karachi"),
    "kuala-lumpur": (3.1390,   101.6869,  "Asia/Kuala_Lumpur"),
    "london":       (51.5074,   -0.1278,  "Europe/London"),
    "los-angeles":  (34.0522, -118.2437,  "America/Los_Angeles"),
    "lucknow":      (26.8467,   80.9462,  "Asia/Kolkata"),
    "madrid":       (40.4168,   -3.7038,  "Europe/Madrid"),
    "manila":       (14.5995,  120.9842,  "Asia/Manila"),
    "mexico-city":  (19.4326,  -99.1332,  "America/Mexico_City"),
    "miami":        (25.7617,  -80.1918,  "America/New_York"),
    "milan":        (45.4654,    9.1859,  "Europe/Rome"),
    "moscow":       (55.7558,   37.6173,  "Europe/Moscow"),
    "munich":       (48.1351,   11.5820,  "Europe/Berlin"),
    "nyc":          (40.7128,  -74.0060,  "America/New_York"),
    "panama-city":  (8.9936,   -79.5197,  "America/Panama"),
    "paris":        (48.8566,    2.3522,  "Europe/Paris"),
    "qingdao":      (36.0671,  120.3826,  "Asia/Shanghai"),
    "san-francisco":(37.7749, -122.4194,  "America/Los_Angeles"),
    "sao-paulo":    (-23.5505,  -46.6333, "America/Sao_Paulo"),
    "seattle":      (47.6062, -122.3321,  "America/Los_Angeles"),
    "seoul":        (37.5665,  126.9780,  "Asia/Seoul"),
    "shanghai":     (31.2304,  121.4737,  "Asia/Shanghai"),
    "shenzhen":     (22.5431,  114.0579,  "Asia/Shanghai"),
    "singapore":    (1.3521,   103.8198,  "Asia/Singapore"),
    "taipei":       (25.0330,  121.5654,  "Asia/Taipei"),
    "tel-aviv":     (32.0853,   34.7818,  "Asia/Jerusalem"),
    "tokyo":        (35.6762,  139.6503,  "Asia/Tokyo"),
    "toronto":      (43.6532,  -79.3832,  "America/Toronto"),
    "warsaw":       (52.2297,   21.0122,  "Europe/Warsaw"),
    "wellington":   (-41.2866,  174.7756, "Pacific/Auckland"),
    "wuhan":        (30.5928,  114.3055,  "Asia/Shanghai"),
    "zhengzhou":    (34.7466,  113.6253,  "Asia/Shanghai"),
}

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"

# Forecast uncertainty (std dev in °C) by days ahead — calibrated to NWP skill
_FORECAST_SIGMA = {0: 1.0, 1: 1.5, 2: 2.0, 3: 2.5, 4: 3.0, 5: 3.5, 6: 4.0, 7: 4.5}


def _slug_to_city(slug: str) -> Optional[str]:
    m = re.search(r"highest-temperature-in-([a-z\-]+)-on-", slug)
    return m.group(1) if m else None


def _slug_to_date(slug: str) -> Optional[str]:
    # e.g. highest-temperature-in-tokyo-on-june-4-2026
    m = re.search(r"-on-([a-z]+-\d+-\d+)$", slug)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%B-%d-%Y").strftime("%Y-%m-%d")
    except Exception:
        return None


def _parse_bucket_temp(group_item_title: str) -> Optional[float]:
    """Parse '23°C', '23°C or lower', '23°C or higher' → 23.0"""
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*[°℃]", group_item_title)
    return float(m.group(1)) if m else None


def _days_until(date_str: str) -> int:
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (target - datetime.now(timezone.utc).date()).days
    except Exception:
        return 999


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _fetch_point_forecast(lat: float, lon: float, tz: str, date_str: str) -> Optional[float]:
    """Fetch deterministic daily max temperature forecast (single fast call)."""
    try:
        resp = requests.get(FORECAST_URL, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "timezone": tz,
            "start_date": date_str, "end_date": date_str,
        }, timeout=10)
        resp.raise_for_status()
        temps = resp.json().get("daily", {}).get("temperature_2m_max", [])
        return float(temps[0]) if temps and temps[0] is not None else None
    except Exception:
        return None


def _forecast_bucket_prob(forecast_temp: float, sigma: float,
                          bucket_temp: float, is_lower: bool, is_higher: bool) -> float:
    """P(bucket) using normal distribution N(forecast_temp, sigma²).
    Each exact-degree bucket spans [bucket_temp-0.5, bucket_temp+0.5)."""
    if is_lower:
        return _normal_cdf((bucket_temp + 0.5 - forecast_temp) / sigma)
    if is_higher:
        return 1.0 - _normal_cdf((bucket_temp - 0.5 - forecast_temp) / sigma)
    lo = _normal_cdf((bucket_temp - 0.5 - forecast_temp) / sigma)
    hi = _normal_cdf((bucket_temp + 0.5 - forecast_temp) / sigma)
    return max(0.0, hi - lo)


def _fetch_historical_prob(lat: float, lon: float, tz: str,
                           month: int, day: int,
                           bucket_temp: float, is_lower: bool, is_higher: bool,
                           years: int = 20) -> Optional[float]:
    """Estimate probability using batch archive requests (one request per ~10yr window)."""
    end_year   = datetime.now(timezone.utc).year - 1
    start_year = max(1994, end_year - years + 1)
    results: list[float] = []

    # Batch: fetch full-year windows and filter matching month+day
    # Keeps API calls to 2-3 instead of 20-30
    batch_size = 10  # years per request
    for batch_start in range(start_year, end_year + 1, batch_size):
        batch_end = min(batch_start + batch_size - 1, end_year)
        try:
            resp = requests.get(ARCHIVE_URL, params={
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max",
                "timezone": tz,
                "start_date": f"{batch_start}-{month:02d}-01",
                "end_date":   f"{batch_end}-{month:02d}-{day:02d}",
            }, timeout=20)
            resp.raise_for_status()
            data = resp.json().get("daily", {})
            dates = data.get("time", [])
            temps = data.get("temperature_2m_max", [])
            target_suffix = f"-{month:02d}-{day:02d}"
            for d, t in zip(dates, temps):
                if d.endswith(target_suffix) and t is not None:
                    results.append(float(t))
        except Exception:
            pass

    if len(results) < 3:
        return None
    if is_lower:
        return sum(1 for t in results if t <= bucket_temp + 0.5) / len(results)
    if is_higher:
        return sum(1 for t in results if t >= bucket_temp - 0.5) / len(results)
    return sum(1 for t in results if abs(t - bucket_temp) < 0.5) / len(results)


def run_scan(config: dict, min_volume: float = 500.0) -> list[dict]:
    edge_min = float(config.get("edge_minimum", 0.08))
    bet_size  = float(config.get("paper_bet_size", 20.0))
    results: list[dict] = []

    # Build set of city slugs to scan (from config)
    configured_cities: list[str] = config.get("cities", list(CITY_COORDS.keys()))
    city_slugs_allowed = {
        c.lower().replace(" ", "-").replace("ã", "a").replace("ó", "o")
        for c in configured_cities
    }
    # Also accept exact keys from CITY_COORDS that partially match config city names
    for cfg_city in configured_cities:
        cfg_lower = cfg_city.lower()
        for key in CITY_COORDS:
            if key in cfg_lower or cfg_lower in key or cfg_lower.replace(" ", "-") == key:
                city_slugs_allowed.add(key)

    log_event("weather", {"type": "scan_start", "cities": list(city_slugs_allowed)})

    events = get_active_markets(tag_slug="daily-temperature", limit=500)
    events2 = get_active_markets(tag_slug="highest-temperature", limit=200)
    seen = {e["slug"] for e in events}
    events += [e for e in events2 if e["slug"] not in seen]

    markets_checked = 0

    now_utc      = datetime.now(timezone.utc)
    min_buffer_h = 2.0  # horas mínimas antes do endDate para entrar

    for event in events:
        slug   = event.get("slug", "")
        vol    = float(event.get("volume") or 0)
        if vol < min_volume:
            continue

        city_slug = _slug_to_city(slug)
        date_str  = _slug_to_date(slug)
        if not city_slug or not date_str:
            continue
        if city_slug not in CITY_COORDS:
            continue
        if city_slug not in city_slugs_allowed:
            continue

        # Verificar endDate — mercado deve ter pelo menos min_buffer_h restantes
        end_date_str = event.get("endDate") or ""
        try:
            end_utc = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            # Se não tem endDate, usar meia-noite UTC do dia seguinte como fallback
            try:
                end_utc = datetime.strptime(date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc) + timedelta(days=1)
            except Exception:
                continue
        secs_remaining = (end_utc - now_utc).total_seconds()
        if secs_remaining < min_buffer_h * 3600:
            # Mercado expirado ou prestes a fechar — não entrar
            log_event("weather", {
                "type": "skip_expired",
                "market_slug": slug,
                "end_utc": end_date_str,
                "secs_remaining": round(secs_remaining),
            })
            continue

        # days_ahead calculado a partir do endDate (mais preciso que slug date)
        days_ahead = max(0, int(secs_remaining / 86400))
        if days_ahead > 7:
            continue

        lat, lon, tz = CITY_COORDS[city_slug]
        try:
            month = int(date_str[5:7])
            day   = int(date_str[8:10])
        except Exception:
            continue

        markets_checked += 1

        # One forecast call per city+date (shared across all buckets)
        sigma         = _FORECAST_SIGMA.get(min(days_ahead, 7), 4.5)
        forecast_temp = _fetch_point_forecast(lat, lon, tz, date_str)
        if forecast_temp is None:
            continue

        sub_markets = event.get("markets", [])

        for pm in sub_markets:
            title    = str(pm.get("groupItemTitle") or pm.get("title") or "")
            raw_prices   = pm.get("outcomePrices") or []
            raw_outcomes = pm.get("outcomes") or []
            try:
                prices   = json.loads(raw_prices)   if isinstance(raw_prices, str)   else raw_prices
                outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
            except Exception:
                continue

            if not prices or not title:
                continue
            try:
                yes_idx = next((i for i, o in enumerate(outcomes) if str(o).upper() == "YES"), 0)
                price_poly = float(prices[yes_idx])
            except Exception:
                continue
            if price_poly <= 0 or price_poly >= 1:
                continue

            bucket_temp = _parse_bucket_temp(title)
            if bucket_temp is None:
                continue

            is_lower  = any(w in title.lower() for w in ["or below", "or lower", "and below"])
            is_higher = any(w in title.lower() for w in ["or above", "or higher", "and above"])

            # Analytic probability: N(forecast_temp, sigma) — no extra API call
            prob_forecast = _forecast_bucket_prob(forecast_temp, sigma, bucket_temp, is_lower, is_higher)

            # Use forecast only (fast — no extra API call).
            # Historical enrichment runs separately in background for the audit log.
            prob_real = prob_forecast

            edge  = calculate_edge(prob_real, price_poly)
            ev    = ev_per_dollar(prob_real, price_poly)
            enter = should_enter(edge, prob_real, price_poly, min_edge=edge_min)

            row: dict = {
                "type": "ev_found" if enter else "scan",
                "market_slug": slug,
                "city": city_slug,
                "resolution_date": date_str,
                "days_ahead": days_ahead,
                "bucket": title,
                "bucket_temp": bucket_temp,
                "end_utc": end_date_str,
                "secs_remaining": round(secs_remaining),
                "prob_real": round(prob_real, 4),
                "prob_forecast": round(prob_forecast, 4),
                "forecast_temp": round(forecast_temp, 1),
                "forecast_sigma": sigma,
                "price_polymarket": round(price_poly, 4),
                "edge": round(edge, 4),
                "ev_per_dollar": round(ev, 4),
                "would_enter": enter,
                "entry_side": "YES",
            }
            log_event("weather", row)

            if enter:
                entry_row = {
                    **row,
                    "type": "simulated_entry",
                    "entry_price": round(price_poly, 4),
                    "shares_simulated": shares_for_bet(bet_size, price_poly),
                    "bet_size": bet_size,
                }
                log_event("weather", entry_row)
                results.append(entry_row)
                print(f"[WEATHER] EV+  {city_slug} {date_str} '{title}': "
                      f"edge={edge:.1%}  poly={price_poly:.3f}  real={prob_real:.3f}")

    log_event("weather", {"type": "scan_end", "markets_checked": markets_checked, "ev_found": len(results)})
    return results
