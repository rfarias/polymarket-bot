from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from ev_scanner.utils.ev_calculator import calculate_edge, ev_per_dollar, should_enter, shares_for_bet
from ev_scanner.utils.logger import log_event, read_open_position_keys
from ev_scanner.utils.polymarket_api import get_active_markets

# Coordenadas das estações de resolução confirmadas (não cidade centro).
# Fonte: descrição/resolutionSource de cada mercado na Polymarket (2026-06-02).
# Formato: city_slug → (lat, lon, tz, station_code)
# Fontes: Wunderground (aeroporto), Hong Kong Observatory, NOAA (Moscow/Istanbul)
STATION_COORDS: dict[str, tuple[float, float, str, str]] = {
    "amsterdam":    (52.3086,   4.7639,   "Europe/Amsterdam",               "EHAM"),
    "ankara":       (40.1282,  32.9951,   "Europe/Istanbul",                "LTAC"),
    "atlanta":      (33.6367, -84.4281,   "America/New_York",               "KATL"),
    "austin":       (30.1975, -97.6664,   "America/Chicago",                "KAUS"),
    "beijing":      (40.0799, 116.6031,   "Asia/Shanghai",                  "ZBAA"),
    "buenos-aires": (-34.8222,-58.5358,   "America/Argentina/Buenos_Aires", "SAEZ"),
    "busan":        (35.1795, 128.9380,   "Asia/Seoul",                     "RKPK"),
    "cape-town":    (-33.9715, 18.6021,   "Africa/Johannesburg",            "FACT"),
    "chengdu":      (30.5785, 103.9474,   "Asia/Shanghai",                  "ZUUU"),
    "chicago":      (41.9742, -87.9073,   "America/Chicago",                "KORD"),
    "chongqing":    (29.7192, 106.6417,   "Asia/Shanghai",                  "ZUCK"),
    "dallas":       (32.8471, -96.8518,   "America/Chicago",                "KDAL"),
    "denver":       (39.7017,-104.7515,   "America/Denver",                 "KBKF"),
    "guangzhou":    (23.3924, 113.2988,   "Asia/Shanghai",                  "ZGGG"),
    "helsinki":     (60.3172,  24.9633,   "Europe/Helsinki",                "EFHK"),
    "hong-kong":    (22.3017, 114.1722,   "Asia/Hong_Kong",                 "HKO"),   # Hong Kong Observatory
    "houston":      (29.6454, -95.2789,   "America/Chicago",                "KHOU"),
    "istanbul":     (41.2753,  28.7519,   "Europe/Istanbul",                "LTFM"),  # NOAA Istanbul Airport
    "jeddah":       (21.6796,  39.1565,   "Asia/Riyadh",                    "OEJN"),
    "jinan":        (36.8570, 117.0160,   "Asia/Shanghai",                  "ZSJN"),
    "karachi":      (24.9065,  67.1608,   "Asia/Karachi",                   "OPKC"),
    "kuala-lumpur": (2.7456,  101.7072,   "Asia/Kuala_Lumpur",              "WMKK"),
    "london":       (51.5053,   0.0553,   "Europe/London",                  "EGLC"),
    "los-angeles":  (33.9425,-118.4081,   "America/Los_Angeles",            "KLAX"),
    "lucknow":      (26.7606,  80.8893,   "Asia/Kolkata",                   "VILK"),
    "madrid":       (40.4936,  -3.5668,   "Europe/Madrid",                  "LEMD"),
    "manila":       (14.5086, 121.0197,   "Asia/Manila",                    "RPLL"),
    "mexico-city":  (19.4363, -99.0721,   "America/Mexico_City",            "MMMX"),
    "miami":        (25.7959, -80.2870,   "America/New_York",               "KMIA"),
    "milan":        (45.6306,   8.7281,   "Europe/Rome",                    "LIMC"),
    "moscow":       (55.5915,  37.2615,   "Europe/Moscow",                  "UUWW"),  # NOAA Vnukovo
    "munich":       (48.3538,  11.7861,   "Europe/Berlin",                  "EDDM"),
    "nyc":          (40.7773, -73.8726,   "America/New_York",               "KLGA"),
    "panama-city":  (8.9836,  -79.5536,   "America/Panama",                 "MPMG"),
    "paris":        (48.9694,   2.4414,   "Europe/Paris",                   "LFPB"),  # Le Bourget
    "qingdao":      (36.2661, 120.3747,   "Asia/Shanghai",                  "ZSQD"),
    "san-francisco":(37.6189,-122.3750,   "America/Los_Angeles",            "KSFO"),
    "sao-paulo":    (-23.4356,-46.4731,   "America/Sao_Paulo",              "SBGR"),
    "seattle":      (47.4502,-122.3088,   "America/Los_Angeles",            "KSEA"),
    "seoul":        (37.4691, 126.4505,   "Asia/Seoul",                     "RKSI"),
    "shanghai":     (31.1434, 121.8052,   "Asia/Shanghai",                  "ZSPD"),
    "shenzhen":     (22.6395, 113.8144,   "Asia/Shanghai",                  "ZGSZ"),
    "singapore":    (1.3502,  103.9943,   "Asia/Singapore",                 "WSSS"),
    "taipei":       (25.0694, 121.5521,   "Asia/Taipei",                    "RCSS"),
    "tel-aviv":     (32.0114,  34.8867,   "Asia/Jerusalem",                 "LLBG"),
    "tokyo":        (35.5494, 139.7798,   "Asia/Tokyo",                     "RJTT"),
    "toronto":      (43.6772, -79.6306,   "America/Toronto",                "CYYZ"),
    "warsaw":       (52.1657,  20.9671,   "Europe/Warsaw",                  "EPWA"),
    "wellington":   (-41.3272, 174.8049,  "Pacific/Auckland",               "NZWN"),
    "wuhan":        (30.7836, 114.2081,   "Asia/Shanghai",                  "ZHHH"),
    "zhengzhou":    (34.5197, 113.8408,   "Asia/Shanghai",                  "ZHCC"),
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
    edge_min      = float(config.get("edge_minimum", 0.08))
    min_price     = float(config.get("min_bucket_price", 0.04))
    bet_size      = float(config.get("paper_bet_size", 20.0))
    results: list[dict] = []

    # Build set of city slugs to scan (from config)
    configured_cities: list[str] = config.get("cities", list(STATION_COORDS.keys()))
    city_slugs_allowed = {
        c.lower().replace(" ", "-").replace("ã", "a").replace("ó", "o")
        for c in configured_cities
    }
    # Also accept exact keys from STATION_COORDS that partially match config city names
    for cfg_city in configured_cities:
        cfg_lower = cfg_city.lower()
        for key in STATION_COORDS:
            if key in cfg_lower or cfg_lower in key or cfg_lower.replace(" ", "-") == key:
                city_slugs_allowed.add(key)

    log_event("weather", {"type": "scan_start", "cities": list(city_slugs_allowed)})

    open_keys = read_open_position_keys("weather")

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
        if city_slug not in STATION_COORDS:
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

        lat, lon, tz, station_code = STATION_COORDS[city_slug]
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
        best_candidate: Optional[dict] = None  # melhor bucket EV+ deste evento

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
            if price_poly < min_price:
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
                "station_code": station_code,
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

            # Acumula melhor candidato por evento (máximo prob_real)
            if enter:
                entry_row = {
                    **row,
                    "type": "simulated_entry",
                    "entry_price": round(price_poly, 4),
                    "shares_simulated": shares_for_bet(bet_size, price_poly),
                    "bet_size": bet_size,
                }
                if best_candidate is None or prob_real > best_candidate["prob_real"]:
                    best_candidate = entry_row

        # Entra apenas no bucket de maior prob_real para este evento
        if best_candidate is not None:
            pos_key = best_candidate["market_slug"] + "|" + best_candidate["bucket"]
            if pos_key in open_keys:
                # Posição já aberta — registrar atualização de preço para trajetória
                log_event("weather", {
                    **{k: best_candidate[k] for k in (
                        "market_slug", "city", "resolution_date", "days_ahead",
                        "bucket", "bucket_temp", "station_code", "end_utc",
                        "secs_remaining", "prob_real", "prob_forecast",
                        "forecast_temp", "forecast_sigma", "edge", "ev_per_dollar",
                    )},
                    "type":          "price_update",
                    "price_current": best_candidate["entry_price"],
                })
            else:
                # Nova posição
                log_event("weather", best_candidate)
                results.append(best_candidate)
                print(f"[WEATHER] EV+  {city_slug} {date_str} '{best_candidate['bucket']}': "
                      f"edge={best_candidate['edge']:.1%}  poly={best_candidate['entry_price']:.3f}  "
                      f"real={best_candidate['prob_real']:.3f}")

    log_event("weather", {"type": "scan_end", "markets_checked": markets_checked, "ev_found": len(results)})
    return results
