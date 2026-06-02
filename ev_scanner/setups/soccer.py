from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from ev_scanner.utils.ev_calculator import calculate_edge, ev_per_dollar, should_enter, shares_for_bet
from ev_scanner.utils.logger import log_event
from ev_scanner.utils.polymarket_api import search_markets, parse_list_field

FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"

LEAGUE_CODES = {
    "EPL": "PL", "La Liga": "PD", "Serie A": "SA",
    "Bundesliga": "BL1", "Ligue 1": "FL1",
    "Champions League": "CL", "Brasileirao": "BSA",
}

# Approximate league average xG (goals per game per team)
LEAGUE_AVG_GOALS = {
    "PL": 1.55, "PD": 1.45, "SA": 1.40, "BL1": 1.60,
    "FL1": 1.35, "CL": 1.50, "BSA": 1.40,
}


def _fd_get(path: str, api_key: str, params: dict | None = None) -> dict:
    resp = requests.get(
        f"{FOOTBALL_DATA_BASE}/{path}",
        headers={"X-Auth-Token": api_key},
        params=params or {},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _team_form(team_id: int, api_key: str, n: int = 38) -> dict:
    """Returns win/draw/loss rates, xG estimates for home and away."""
    try:
        data = _fd_get(f"teams/{team_id}/matches", api_key,
                       params={"status": "FINISHED", "limit": n})
        matches = data.get("matches", [])
    except Exception:
        return {}

    home_wins = away_wins = home_draws = away_draws = 0
    home_total = away_total = 0
    goals_for_home = goals_for_away = 0.0
    goals_ag_home  = goals_ag_away  = 0.0

    for m in matches:
        home_id = m.get("homeTeam", {}).get("id")
        score   = m.get("score", {}).get("fullTime", {})
        home_g  = score.get("home") or 0
        away_g  = score.get("away") or 0
        winner  = m.get("score", {}).get("winner")

        if home_id == team_id:
            home_total += 1
            goals_for_home += home_g
            goals_ag_home  += away_g
            if winner == "HOME_TEAM":   home_wins  += 1
            elif winner == "DRAW":      home_draws += 1
        else:
            away_total += 1
            goals_for_away += away_g
            goals_ag_away  += home_g
            if winner == "AWAY_TEAM":   away_wins  += 1
            elif winner == "DRAW":      away_draws += 1

    return {
        "home_win_rate":  home_wins  / home_total  if home_total  else 0.5,
        "away_win_rate":  away_wins  / away_total  if away_total  else 0.35,
        "home_draw_rate": home_draws / home_total  if home_total  else 0.25,
        "away_draw_rate": away_draws / away_total  if away_total  else 0.25,
        "xg_for_home":   goals_for_home / home_total if home_total else 1.4,
        "xg_ag_home":    goals_ag_home  / home_total if home_total else 1.4,
        "xg_for_away":   goals_for_away / away_total if away_total else 1.2,
        "xg_ag_away":    goals_ag_away  / away_total if away_total else 1.4,
    }


def _h2h_win_rate(home_id: int, away_id: int, api_key: str) -> Optional[float]:
    try:
        data = _fd_get("matches", api_key, params={
            "homeTeam": home_id, "awayTeam": away_id,
            "status": "FINISHED", "limit": 10,
        })
        matches = data.get("matches", [])
        if len(matches) < 3:
            return None
        home_wins = sum(1 for m in matches if m.get("score", {}).get("winner") == "HOME_TEAM")
        return home_wins / len(matches)
    except Exception:
        return None


def _poisson_prob(lambda_: float, k: int) -> float:
    return math.exp(-lambda_) * (lambda_ ** k) / math.factorial(k)


def _poisson_match_probs(lambda_home: float, lambda_away: float, max_goals: int = 7) -> tuple[float, float, float]:
    p_home = p_draw = p_away = 0.0
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = _poisson_prob(lambda_home, i) * _poisson_prob(lambda_away, j)
            if i > j:   p_home += p
            elif i == j: p_draw += p
            else:        p_away += p
    total = p_home + p_draw + p_away
    return p_home / total, p_draw / total, p_away / total


def _combined_prob(home_form: dict, away_form: dict, h2h: Optional[float],
                   league_avg: float) -> tuple[float, float, float]:
    # Poisson model
    lambda_home = home_form.get("xg_for_home", 1.4) * (away_form.get("xg_ag_away", 1.4) / league_avg)
    lambda_away = away_form.get("xg_for_away", 1.2) * (home_form.get("xg_ag_home", 1.4) / league_avg)
    p_home_p, p_draw_p, p_away_p = _poisson_match_probs(max(0.1, lambda_home), max(0.1, lambda_away))

    # Form-based
    p_home_f = home_form.get("home_win_rate", 0.5)
    p_away_f = away_form.get("away_win_rate", 0.35)
    p_draw_f = 1.0 - p_home_f - p_away_f

    # H2H
    if h2h is not None:
        w_poisson, w_form, w_h2h = 0.30, 0.50, 0.20
        p_home = w_poisson * p_home_p + w_form * p_home_f + w_h2h * h2h
    else:
        w_poisson, w_form = 0.30, 0.70
        p_home = w_poisson * p_home_p + w_form * p_home_f

    p_draw  = 0.30 * p_draw_p + 0.70 * p_draw_f
    p_away  = max(0.01, 1.0 - p_home - p_draw)
    return round(p_home, 4), round(p_draw, 4), round(p_away, 4)


def _find_teams_in_market(title: str, api_key: str, league_code: str) -> tuple[Optional[int], Optional[int]]:
    """Try to find team IDs by searching the competition standings."""
    try:
        data = _fd_get(f"competitions/{league_code}/teams", api_key)
        teams = data.get("teams", [])
        title_lower = title.lower()
        matched = []
        for t in teams:
            name = t.get("name", "").lower()
            short = t.get("shortName", "").lower()
            tla   = t.get("tla", "").lower()
            if name in title_lower or short in title_lower or tla in title_lower:
                matched.append(t["id"])
        if len(matched) >= 2:
            return matched[0], matched[1]
    except Exception:
        pass
    return None, None


def run_scan(config: dict, min_volume: float = 500.0) -> list[dict]:
    edge_min = float(config.get("edge_minimum", 0.08))
    bet_size  = float(config.get("paper_bet_size", 20.0))
    api_key   = os.getenv("FOOTBALL_DATA_API_KEY", "")
    results: list[dict] = []

    if not api_key:
        log_event("soccer", {"type": "data_unavailable", "reason": "FOOTBALL_DATA_API_KEY not set"})
        print("[SOCCER] data_unavailable — set FOOTBALL_DATA_API_KEY in .env")
        return []

    log_event("soccer", {"type": "scan_start"})

    leagues = config.get("leagues", list(LEAGUE_CODES.keys()))
    markets = search_markets(keywords=["soccer", "football", "epl", "laliga", "serie", "bundesliga",
                                        "brasileirao", "champions", "premier league", "match"],
                              limit=200)

    for event in markets:
        slug  = event.get("slug", "")
        title = event.get("title", "")
        vol   = float(event.get("volume") or 0)
        if vol < min_volume:
            continue

        # Try to identify league
        league_code = None
        for league_name, code in LEAGUE_CODES.items():
            if league_name.lower() in title.lower() or league_name.lower() in slug.lower():
                league_code = code
                break
        if not league_code:
            continue

        league_avg = LEAGUE_AVG_GOALS.get(league_code, 1.45)
        home_id, away_id = _find_teams_in_market(title, api_key, league_code)
        if not home_id or not away_id:
            continue

        home_form = _team_form(home_id, api_key)
        away_form = _team_form(away_id, api_key)
        if not home_form or not away_form:
            continue

        h2h = _h2h_win_rate(home_id, away_id, api_key)
        p_home, p_draw, p_away = _combined_prob(home_form, away_form, h2h, league_avg)

        time.sleep(0.7)  # football-data.org: 10 calls/min free tier

        poly_markets = event.get("markets", [])
        for pm in poly_markets:
            outcomes       = parse_list_field(pm.get("outcomes", []))
            outcome_prices = parse_list_field(pm.get("outcomePrices", []))
            for outcome, price_str in zip(outcomes, outcome_prices):
                try:
                    price_poly = float(price_str)
                except Exception:
                    continue
                if price_poly <= 0 or price_poly >= 1:
                    continue

                out_lower = str(outcome).lower()
                if any(w in out_lower for w in ["home", "win", "yes"]):
                    prob_real = p_home
                elif "draw" in out_lower:
                    prob_real = p_draw
                elif any(w in out_lower for w in ["away", "loss"]):
                    prob_real = p_away
                else:
                    continue

                edge  = calculate_edge(prob_real, price_poly)
                ev    = ev_per_dollar(prob_real, price_poly)
                enter = should_enter(edge, prob_real, price_poly, min_edge=edge_min)

                row: dict = {
                    "type": "ev_found" if enter else "scan",
                    "market_slug": slug,
                    "league": league_code,
                    "title": title,
                    "outcome": str(outcome),
                    "prob_home": p_home, "prob_draw": p_draw, "prob_away": p_away,
                    "prob_real_target": round(prob_real, 4),
                    "price_polymarket": round(price_poly, 4),
                    "edge": round(edge, 4),
                    "ev_per_dollar": round(ev, 4),
                    "would_enter": enter,
                    "entry_side": "YES",
                    "model_components": {
                        "home_form_weight": 0.40,
                        "h2h_weight": 0.20 if h2h is not None else 0.0,
                        "poisson_weight": 0.30,
                    },
                }
                log_event("soccer", row)

                if enter:
                    entry_row = {**row, "type": "simulated_entry",
                                 "entry_price": round(price_poly, 4),
                                 "shares_simulated": shares_for_bet(bet_size, price_poly),
                                 "bet_size": bet_size}
                    log_event("soccer", entry_row)
                    results.append(entry_row)
                    print(f"[SOCCER] EV+ {title} {outcome}: edge={edge:.1%} poly={price_poly:.2f} real={prob_real:.2f}")

    log_event("soccer", {"type": "scan_end", "ev_found": len(results)})
    return results
