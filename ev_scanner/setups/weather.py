from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from ev_scanner.utils.ev_calculator import calculate_edge, ev_per_dollar, should_enter, shares_for_bet
from ev_scanner.utils.logger import log_event
from ev_scanner.utils.polymarket_api import search_markets, get_market_volume

CITIES: dict[str, dict] = {
    "São Paulo":      {"lat": -23.5505, "lon": -46.6333, "tz": "America/Sao_Paulo"},
    "Rio de Janeiro": {"lat": -22.9068, "lon": -43.1729, "tz": "America/Sao_Paulo"},
    "London":         {"lat": 51.5074,  "lon": -0.1278,  "tz": "Europe/London"},
    "New York":       {"lat": 40.7128,  "lon": -74.0060, "tz": "America/New_York"},
    "Hong Kong":      {"lat": 22.3193,  "lon": 114.1694, "tz": "Asia/Hong_Kong"},
    "Tokyo":          {"lat": 35.6762,  "lon": 139.6503, "tz": "Asia/Tokyo"},
    "Sydney":         {"lat": -33.8688, "lon": 151.2093, "tz": "Australia/Sydney"},
    "Paris":          {"lat": 48.8566,  "lon": 2.3522,   "tz": "Europe/Paris"},
}

FORECAST_URL  = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL   = "https://archive-api.open-meteo.com/v1/archive"
ENSEMBLE_URL  = "https://ensemble-api.open-meteo.com/v1/ensemble"


def _fetch_forecast_max_temp(lat: float, lon: float, date_str: str, tz: str) -> Optional[float]:
    """Fetch daily maximum temperature forecast for a specific date."""
    try:
        resp = requests.get(FORECAST_URL, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "timezone": tz,
            "start_date": date_str, "end_date": date_str,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        return float(temps[0]) if temps else None
    except Exception:
        return None


def _fetch_ensemble_prob(lat: float, lon: float, date_str: str, tz: str, bucket_lo: float, bucket_hi: float) -> Optional[float]:
    """Estimate probability that max temp falls in [bucket_lo, bucket_hi) using ensemble members."""
    try:
        resp = requests.get(ENSEMBLE_URL, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "models": "icon_seamless",
            "timezone": tz,
            "start_date": date_str, "end_date": date_str,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # ensemble returns multiple member columns
        daily = data.get("daily", {})
        member_vals = [v for k, v in daily.items() if k.startswith("temperature_2m_max") and v is not None]
        # flatten list of lists
        flat: list[float] = []
        for v in member_vals:
            if isinstance(v, list):
                flat.extend([x for x in v if x is not None])
            elif isinstance(v, (int, float)):
                flat.append(float(v))
        if not flat:
            return None
        in_bucket = sum(1 for x in flat if bucket_lo <= x < bucket_hi)
        return in_bucket / len(flat)
    except Exception:
        return None


def _fetch_historical_prob(lat: float, lon: float, month: int, day: int, tz: str,
                           bucket_lo: float, bucket_hi: float) -> Optional[float]:
    """Estimate probability from 30-year historical distribution for same calendar date."""
    try:
        end_year = datetime.now(timezone.utc).year - 1
        start_year = max(1993, end_year - 30)
        results = []
        # Batch: fetch all years in one request using date ranges
        for year in range(start_year, end_year + 1):
            try:
                date_str = f"{year}-{month:02d}-{day:02d}"
                resp = requests.get(ARCHIVE_URL, params={
                    "latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max",
                    "timezone": tz,
                    "start_date": date_str, "end_date": date_str,
                }, timeout=8)
                resp.raise_for_status()
                data = resp.json()
                temps = data.get("daily", {}).get("temperature_2m_max", [])
                if temps and temps[0] is not None:
                    results.append(float(temps[0]))
            except Exception:
                continue
            time.sleep(0.05)  # respect rate limit
        if len(results) < 5:
            return None
        in_bucket = sum(1 for t in results if bucket_lo <= t < bucket_hi)
        return in_bucket / len(results)
    except Exception:
        return None


def _parse_bucket(bucket_str: str) -> tuple[float, float]:
    """Parse bucket string like '>22°C', '<20°C', '20-22°C' → (lo, hi)."""
    s = bucket_str.replace("°C", "").replace("°F", "").strip()
    if s.startswith(">"):
        return float(s[1:]), 999.0
    if s.startswith("<"):
        return -999.0, float(s[1:])
    if "-" in s:
        parts = s.split("-")
        return float(parts[0]), float(parts[1])
    return -999.0, 999.0


def _city_from_slug(slug: str) -> Optional[str]:
    slug_lower = slug.lower()
    for city in CITIES:
        if city.lower().replace(" ", "-") in slug_lower or city.lower().replace(" ", "_") in slug_lower:
            return city
        # partial match
        first_word = city.lower().split()[0]
        if first_word in slug_lower:
            return city
    return None


def _extract_resolution_date(event: dict) -> Optional[str]:
    end_date = event.get("endDate") or event.get("end_date_iso") or ""
    if end_date:
        return end_date[:10]
    return None


def _days_until(date_str: str) -> int:
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (target - datetime.now(timezone.utc).date()).days
    except Exception:
        return 999


def run_scan(config: dict, min_volume: float = 500.0) -> list[dict]:
    """Scan Polymarket for weather markets, compute edge, log EV+ opportunities."""
    edge_min = float(config.get("edge_minimum", 0.08))
    bet_size  = float(config.get("paper_bet_size", 20.0))
    results: list[dict] = []

    log_event("weather", {"type": "scan_start", "cities": list(CITIES.keys())})

    markets = search_markets(
        keywords=["temperature", "temp", "highest", "weather"],
        tag="weather",
        limit=200,
    )

    for event in markets:
        slug  = event.get("slug", "")
        title = event.get("title", "")

        city = _city_from_slug(slug) or _city_from_slug(title.lower())
        if not city:
            continue

        vol = float(event.get("volume") or 0)
        if vol < min_volume:
            continue

        res_date = _extract_resolution_date(event)
        if not res_date:
            continue

        days_ahead = _days_until(res_date)
        if days_ahead < 0 or days_ahead > 14:
            continue

        coords = CITIES[city]
        lat, lon, tz = coords["lat"], coords["lon"], coords["tz"]
        target_date = res_date

        # Parse market buckets from outcomes
        poly_markets = event.get("markets", [])
        for pm in poly_markets:
            outcomes     = pm.get("outcomes", [])
            outcome_prices = pm.get("outcomePrices", [])
            if len(outcomes) != len(outcome_prices):
                continue

            for outcome, price_str in zip(outcomes, outcome_prices):
                try:
                    price_poly = float(price_str)
                except Exception:
                    continue
                if price_poly <= 0 or price_poly >= 1:
                    continue

                # Try to parse bucket from outcome label
                bucket_lo, bucket_hi = _parse_bucket(str(outcome))

                # Forecast probability
                forecast_temp = _fetch_forecast_max_temp(lat, lon, target_date, tz)
                if forecast_temp is None:
                    continue

                prob_forecast = _fetch_ensemble_prob(lat, lon, target_date, tz, bucket_lo, bucket_hi)
                if prob_forecast is None:
                    # Fallback: point forecast ±2°C gives rough probability
                    in_bucket = bucket_lo <= forecast_temp < bucket_hi
                    prob_forecast = 0.70 if in_bucket else 0.15

                # Historical probability
                try:
                    month = int(target_date[5:7])
                    day   = int(target_date[8:10])
                except Exception:
                    continue
                prob_historical = _fetch_historical_prob(lat, lon, month, day, tz, bucket_lo, bucket_hi)
                if prob_historical is None:
                    prob_historical = prob_forecast  # fallback

                # Combine: weight by days ahead
                if days_ahead <= 7:
                    w_forecast, w_hist = 0.70, 0.30
                else:
                    w_forecast, w_hist = 0.30, 0.70
                prob_real = w_forecast * prob_forecast + w_hist * prob_historical

                edge = calculate_edge(prob_real, price_poly)
                ev   = ev_per_dollar(prob_real, price_poly)
                enter = should_enter(edge, prob_real, price_poly, min_edge=edge_min)

                row: dict = {
                    "type": "ev_found" if enter else "scan",
                    "market_slug": slug,
                    "city": city,
                    "resolution_date": res_date,
                    "days_ahead": days_ahead,
                    "bucket": str(outcome),
                    "prob_real": round(prob_real, 4),
                    "prob_forecast": round(prob_forecast, 4),
                    "prob_historical": round(prob_historical, 4),
                    "price_polymarket": round(price_poly, 4),
                    "edge": round(edge, 4),
                    "ev_per_dollar": round(ev, 4),
                    "would_enter": enter,
                    "entry_side": "YES",
                    "forecast_temp": round(forecast_temp, 1),
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
                    print(f"[WEATHER] EV+ {city} {outcome} {res_date}: edge={edge:.1%} poly={price_poly:.2f} real={prob_real:.2f}")

    log_event("weather", {"type": "scan_end", "ev_found": len(results), "markets_checked": len(markets)})
    return results
