from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from ev_scanner.utils.ev_calculator import calculate_edge, ev_per_dollar, should_enter, shares_for_bet
from ev_scanner.utils.logger import log_event
from ev_scanner.utils.polymarket_api import search_markets, parse_list_field

try:
    from nba_api.stats.endpoints import teamgamelog, leaguegamefinder
    from nba_api.stats.static import teams as nba_teams_static
    _NBA_AVAILABLE = True
except ImportError:
    _NBA_AVAILABLE = False

# NBA home advantage in points (historical constant)
NBA_HOME_ADVANTAGE = 3.0
# NFL home advantage in points
NFL_HOME_ADVANTAGE = 2.5

TEAM_MAP_NBA: dict[str, str] = {
    "lakers": "Los Angeles Lakers", "celtics": "Boston Celtics",
    "warriors": "Golden State Warriors", "bulls": "Chicago Bulls",
    "heat": "Miami Heat", "nets": "Brooklyn Nets",
    "knicks": "New York Knicks", "bucks": "Milwaukee Bucks",
    "suns": "Phoenix Suns", "nuggets": "Denver Nuggets",
    "clippers": "Los Angeles Clippers", "76ers": "Philadelphia 76ers",
    "raptors": "Toronto Raptors", "mavs": "Dallas Mavericks",
    "mavericks": "Dallas Mavericks", "spurs": "San Antonio Spurs",
    "thunder": "Oklahoma City Thunder", "jazz": "Utah Jazz",
    "blazers": "Portland Trail Blazers", "hawks": "Atlanta Hawks",
    "hornets": "Charlotte Hornets", "pacers": "Indiana Pacers",
    "pistons": "Detroit Pistons", "wizards": "Washington Wizards",
    "magic": "Orlando Magic", "cavaliers": "Cleveland Cavaliers",
    "cavs": "Cleveland Cavaliers", "timberwolves": "Minnesota Timberwolves",
    "wolves": "Minnesota Timberwolves", "grizzlies": "Memphis Grizzlies",
    "pelicans": "New Orleans Pelicans", "kings": "Sacramento Kings",
}

TEAM_MAP_NFL: dict[str, str] = {
    "patriots": "New England Patriots", "chiefs": "Kansas City Chiefs",
    "eagles": "Philadelphia Eagles", "49ers": "San Francisco 49ers",
    "cowboys": "Dallas Cowboys", "packers": "Green Bay Packers",
    "ravens": "Baltimore Ravens", "bills": "Buffalo Bills",
    "broncos": "Denver Broncos", "steelers": "Pittsburgh Steelers",
    "saints": "New Orleans Saints", "rams": "Los Angeles Rams",
    "seahawks": "Seattle Seahawks", "bears": "Chicago Bears",
    "giants": "New York Giants", "jets": "New York Jets",
    "dolphins": "Miami Dolphins", "bengals": "Cincinnati Bengals",
    "browns": "Cleveland Browns", "vikings": "Minnesota Vikings",
    "lions": "Detroit Lions", "buccaneers": "Tampa Bay Buccaneers",
    "bucs": "Tampa Bay Buccaneers", "falcons": "Atlanta Falcons",
    "panthers": "Carolina Panthers", "texans": "Houston Texans",
    "colts": "Indianapolis Colts", "titans": "Tennessee Titans",
    "jaguars": "Jacksonville Jaguars", "raiders": "Las Vegas Raiders",
    "chargers": "Los Angeles Chargers", "cardinals": "Arizona Cardinals",
    "commanders": "Washington Commanders",
}


def _find_team_in_text(text: str, team_map: dict[str, str]) -> Optional[str]:
    text_lower = text.lower()
    for key, full_name in team_map.items():
        if key in text_lower or full_name.lower() in text_lower:
            return full_name
    return None


def _nba_team_id(name: str) -> Optional[int]:
    if not _NBA_AVAILABLE:
        return None
    all_teams = nba_teams_static.get_teams()
    name_lower = name.lower()
    for t in all_teams:
        if name_lower in t["full_name"].lower() or name_lower in t["nickname"].lower():
            return t["id"]
    return None


def _nba_net_rating(team_id: int, n_games: int = 15) -> dict:
    """Fetch last N games for a team and return net rating + win rate."""
    if not _NBA_AVAILABLE:
        return {}
    try:
        log = teamgamelog.TeamGameLog(team_id=team_id, season="2024-25")
        df = log.get_data_frames()[0].head(n_games)
        if df.empty:
            return {}
        pts_for = float(df["PTS"].mean())
        # approximate opponent pts via +/-
        plus_minus = float(df["PLUS_MINUS"].mean()) if "PLUS_MINUS" in df.columns else 0.0
        pts_against = pts_for - plus_minus
        wins = int((df["WL"] == "W").sum())
        return {
            "net_rating": round(pts_for - pts_against, 2),
            "pts_for":    round(pts_for, 2),
            "pts_against": round(pts_against, 2),
            "win_rate":   round(wins / len(df), 3),
            "n_games":    len(df),
        }
    except Exception:
        return {}


def _logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _nba_win_prob(home_net: float, away_net: float) -> float:
    net_diff = home_net - away_net + NBA_HOME_ADVANTAGE
    return round(_logistic(net_diff / 10.0), 4)


def _nfl_win_prob(home_net: float, away_net: float) -> float:
    net_diff = home_net - away_net + NFL_HOME_ADVANTAGE
    return round(_logistic(net_diff / 7.0), 4)


def run_scan(config: dict, min_volume: float = 500.0) -> list[dict]:
    edge_min = float(config.get("edge_minimum", 0.08))
    bet_size  = float(config.get("paper_bet_size", 20.0))
    results: list[dict] = []

    if not _NBA_AVAILABLE:
        log_event("nba_nfl", {"type": "data_unavailable", "reason": "nba_api not installed"})
        print("[NBA_NFL] nba_api not available — pip install nba_api")
        return []

    log_event("nba_nfl", {"type": "scan_start"})

    markets = search_markets(
        keywords=["nba", "nfl", "basketball", "football", "playoffs",
                  "super bowl", "conference", "finals"],
        limit=200,
    )

    for event in markets:
        slug  = event.get("slug", "")
        title = event.get("title", "")
        vol   = float(event.get("volume") or 0)
        if vol < min_volume:
            continue

        title_lower = title.lower() + " " + slug.lower()
        is_nba = "nba" in title_lower or "basketball" in title_lower
        is_nfl = "nfl" in title_lower or "super bowl" in title_lower

        if not is_nba and not is_nfl:
            # try team name detection
            if _find_team_in_text(title_lower, TEAM_MAP_NBA):
                is_nba = True
            elif _find_team_in_text(title_lower, TEAM_MAP_NFL):
                is_nfl = True

        if not is_nba and not is_nfl:
            continue

        team_map = TEAM_MAP_NBA if is_nba else TEAM_MAP_NFL
        league   = "NBA" if is_nba else "NFL"

        # Extract two teams
        found = []
        for key, name in team_map.items():
            if key in title_lower or name.lower() in title_lower:
                if name not in found:
                    found.append(name)
        if len(found) < 2:
            continue

        home_name, away_name = found[0], found[1]

        if is_nba:
            home_id = _nba_team_id(home_name)
            away_id = _nba_team_id(away_name)
            if not home_id or not away_id:
                continue
            time.sleep(0.6)  # nba_api rate limit
            home_stats = _nba_net_rating(home_id)
            away_stats = _nba_net_rating(away_id)
            if not home_stats or not away_stats:
                continue
            p_home = _nba_win_prob(home_stats["net_rating"], away_stats["net_rating"])
        else:
            # NFL: no live API — use flat prior with home advantage only
            home_stats = {"net_rating": 0.0, "win_rate": 0.5}
            away_stats = {"net_rating": 0.0, "win_rate": 0.5}
            p_home = _nfl_win_prob(0.0, 0.0)

        p_away = round(1.0 - p_home, 4)

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
                if home_name.split()[-1].lower() in out_lower or "home" in out_lower:
                    prob_real = p_home
                elif away_name.split()[-1].lower() in out_lower or "away" in out_lower:
                    prob_real = p_away
                else:
                    continue

                edge  = calculate_edge(prob_real, price_poly)
                ev    = ev_per_dollar(prob_real, price_poly)
                enter = should_enter(edge, prob_real, price_poly, min_edge=edge_min)

                row: dict = {
                    "type": "ev_found" if enter else "scan",
                    "market_slug": slug,
                    "league": league,
                    "home_team": home_name, "away_team": away_name,
                    "outcome": str(outcome),
                    "prob_home_model": p_home, "prob_away_model": p_away,
                    "prob_real_target": round(prob_real, 4),
                    "model_inputs": {
                        "home_net_rating": home_stats.get("net_rating"),
                        "away_net_rating": away_stats.get("net_rating"),
                        "home_win_rate_last15": home_stats.get("win_rate"),
                        "away_win_rate_last15": away_stats.get("win_rate"),
                        "h2h_win_rate": None,
                    },
                    "price_polymarket": round(price_poly, 4),
                    "edge": round(edge, 4),
                    "ev_per_dollar": round(ev, 4),
                    "would_enter": enter,
                    "entry_side": "YES",
                }
                log_event("nba_nfl", row)

                if enter:
                    entry_row = {**row, "type": "simulated_entry",
                                 "entry_price": round(price_poly, 4),
                                 "shares_simulated": shares_for_bet(bet_size, price_poly),
                                 "bet_size": bet_size}
                    log_event("nba_nfl", entry_row)
                    results.append(entry_row)
                    print(f"[{league}] EV+ {home_name} vs {away_name} {outcome}: edge={edge:.1%}")

    log_event("nba_nfl", {"type": "scan_end", "ev_found": len(results)})
    return results
