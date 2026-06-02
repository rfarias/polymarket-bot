from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from ev_scanner.utils.ev_calculator import calculate_edge, ev_per_dollar, should_enter, shares_for_bet
from ev_scanner.utils.logger import log_event
from ev_scanner.utils.polymarket_api import search_markets

FRED_BASE = "https://api.fred.stlouisfed.org/series/observations"

# CME FedWatch JSON endpoint (public, no key)
CME_FEDWATCH_URL = "https://www.cmegroup.com/CmeWS/mvc/Meeting/FedWatch/V2/"

# Historical base rates: P(cut | context) from 1990-2024 FOMC data
# Rows: (cpi_above_3, unemployment_above_4_5) → P(cut), P(hold), P(hike)
_BASE_RATES: dict[tuple[bool, bool], dict[str, float]] = {
    (True,  True):  {"cut": 0.10, "hold": 0.50, "hike": 0.40},
    (True,  False): {"cut": 0.05, "hold": 0.45, "hike": 0.50},
    (False, True):  {"cut": 0.40, "hold": 0.45, "hike": 0.15},
    (False, False): {"cut": 0.20, "hold": 0.55, "hike": 0.25},
}


def _fred_latest(series_id: str, api_key: str) -> Optional[float]:
    try:
        resp = requests.get(FRED_BASE, params={
            "series_id": series_id, "api_key": api_key,
            "file_type": "json", "sort_order": "desc", "limit": 1,
        }, timeout=10)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        if obs and obs[0].get("value") not in (".", None, ""):
            return float(obs[0]["value"])
    except Exception:
        pass
    return None


def _cme_fedwatch_prob(action: str) -> Optional[float]:
    """Fetch next-meeting probabilities from CME FedWatch public endpoint."""
    try:
        resp = requests.get(CME_FEDWATCH_URL, timeout=10,
                            headers={"User-Agent": "ev-scanner/1.0",
                                     "Referer": "https://www.cmegroup.com/"})
        resp.raise_for_status()
        data = resp.json()
        # Structure varies; try to find next meeting probabilities
        meetings = data if isinstance(data, list) else data.get("meetings", data.get("data", []))
        if not meetings:
            return None
        next_meeting = meetings[0]
        probs = next_meeting.get("probabilities", next_meeting.get("data", {}))
        action_lower = action.lower()
        for k, v in (probs.items() if isinstance(probs, dict) else []):
            if action_lower in k.lower():
                return float(v) / 100.0 if float(v) > 1 else float(v)
    except Exception:
        pass
    return None


def run_scan(config: dict, min_volume: float = 500.0) -> list[dict]:
    edge_min = float(config.get("edge_minimum", 0.06))
    bet_size  = float(config.get("paper_bet_size", 20.0))
    results: list[dict] = []

    fred_key = os.getenv("FRED_API_KEY", "")

    log_event("fed_rate", {"type": "scan_start"})

    # Fetch macro context
    cpi   = _fred_latest("CPIAUCSL", fred_key) if fred_key else None
    unemp = _fred_latest("UNRATE",   fred_key) if fred_key else None

    if cpi is None or unemp is None:
        log_event("fed_rate", {"type": "data_unavailable",
                                "reason": "FRED_API_KEY missing or API error",
                                "cpi": cpi, "unemployment": unemp})
        print("[FED_RATE] data_unavailable — set FRED_API_KEY in .env")
        return []

    cpi_above_3   = cpi > 3.0
    unemp_above_45 = unemp > 4.5
    base_rates = _BASE_RATES[(cpi_above_3, unemp_above_45)]

    # Search Polymarket for Fed-related markets
    markets = search_markets(
        keywords=["fed", "federal reserve", "rate cut", "rate hike", "fomc", "interest rate"],
        limit=100,
    )

    for event in markets:
        slug   = event.get("slug", "")
        title  = event.get("title", "")
        vol    = float(event.get("volume") or 0)
        if vol < min_volume:
            continue

        poly_markets = event.get("markets", [])
        for pm in poly_markets:
            outcomes       = pm.get("outcomes", [])
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

                outcome_lower = str(outcome).lower()

                # Determine action type from outcome label
                if any(w in outcome_lower for w in ["cut", "lower", "decrease"]):
                    action = "cut"
                elif any(w in outcome_lower for w in ["hike", "raise", "increase"]):
                    action = "hike"
                elif any(w in outcome_lower for w in ["hold", "pause", "unchanged", "no change"]):
                    action = "hold"
                else:
                    continue

                cme_prob = _cme_fedwatch_prob(action)
                base_prob = base_rates.get(action, 0.25)

                if cme_prob is not None:
                    prob_real = 0.70 * cme_prob + 0.30 * base_prob
                else:
                    prob_real = base_prob
                    log_event("fed_rate", {"type": "data_unavailable",
                                            "reason": "CME FedWatch unavailable, using base rate only",
                                            "action": action})

                edge  = calculate_edge(prob_real, price_poly)
                ev    = ev_per_dollar(prob_real, price_poly)
                enter = should_enter(edge, prob_real, price_poly, min_edge=edge_min)

                row: dict = {
                    "type": "ev_found" if enter else "scan",
                    "market_slug": slug,
                    "outcome": str(outcome),
                    "action": action,
                    "prob_real": round(prob_real, 4),
                    "cme_fedwatch_prob": round(cme_prob, 4) if cme_prob is not None else None,
                    "base_rate_contextual": round(base_prob, 4),
                    "current_cpi": round(cpi, 2),
                    "current_unemployment": round(unemp, 2),
                    "price_polymarket": round(price_poly, 4),
                    "edge": round(edge, 4),
                    "ev_per_dollar": round(ev, 4),
                    "would_enter": enter,
                    "entry_side": "YES",
                }
                log_event("fed_rate", row)

                if enter:
                    entry_row = {
                        **row,
                        "type": "simulated_entry",
                        "entry_price": round(price_poly, 4),
                        "shares_simulated": shares_for_bet(bet_size, price_poly),
                        "bet_size": bet_size,
                    }
                    log_event("fed_rate", entry_row)
                    results.append(entry_row)
                    print(f"[FED_RATE] EV+ {action} {slug}: edge={edge:.1%} poly={price_poly:.2f} real={prob_real:.2f}")

    log_event("fed_rate", {"type": "scan_end", "ev_found": len(results)})
    return results
