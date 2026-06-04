from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from ev_scanner.utils.ev_calculator import calculate_edge, ev_per_dollar, should_enter, shares_for_bet
from ev_scanner.utils.logger import log_event, read_open_position_keys
from ev_scanner.utils.polymarket_api import search_markets, parse_list_field

try:
    from nba_api.stats.endpoints import teamgamelog, leaguedashteamstats
    from nba_api.stats.static import teams as nba_teams_static
    _NBA_AVAILABLE = True
except ImportError:
    _NBA_AVAILABLE = False

# Cache de ratings por temporada: {season: {team_id: {"net_rating": float, "win_rate": float}}}
_ratings_cache: dict[str, dict] = {}


def _current_nba_season() -> str:
    """Retorna a temporada NBA atual no formato 'YYYY-YY'."""
    now = datetime.now(timezone.utc)
    # Temporada começa em outubro; antes de outubro é a temporada do ano anterior
    year = now.year if now.month >= 10 else now.year - 1
    return f"{year}-{str(year + 1)[-2:]}"


def _fetch_all_team_ratings(season: str) -> dict[int, dict]:
    """Busca NET_RATING, OFF_RATING, DEF_RATING para todos os times em uma chamada."""
    if season in _ratings_cache:
        return _ratings_cache[season]
    try:
        time.sleep(0.6)
        data = leaguedashteamstats.LeagueDashTeamStats(
            season=season,
            measure_type_detailed_defense="Advanced",
        )
        df = data.get_data_frames()[0]
        result: dict[int, dict] = {}
        for _, row in df.iterrows():
            result[int(row["TEAM_ID"])] = {
                "net_rating": float(row.get("NET_RATING", 0.0) or 0.0),
                "off_rating": float(row.get("OFF_RATING", 0.0) or 0.0),
                "def_rating": float(row.get("DEF_RATING", 0.0) or 0.0),
                "wins":       int(row.get("W", 0)),
                "losses":     int(row.get("L", 0)),
            }
        _ratings_cache[season] = result
        return result
    except Exception:
        return {}

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


def _nba_team_stats(team_id: int, all_ratings: dict) -> dict:
    """Retorna stats do time a partir do cache de ratings."""
    r = all_ratings.get(team_id, {})
    if not r:
        return {}
    total = r["wins"] + r["losses"]
    return {
        "net_rating": r["net_rating"],
        "off_rating": r["off_rating"],
        "def_rating": r["def_rating"],
        "win_rate":   round(r["wins"] / total, 3) if total > 0 else 0.5,
        "wins":       r["wins"],
        "losses":     r["losses"],
    }


def _logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _nba_win_prob(home_net: float, away_net: float) -> float:
    net_diff = home_net - away_net + NBA_HOME_ADVANTAGE
    return round(_logistic(net_diff / 10.0), 4)


def _nfl_win_prob(home_net: float, away_net: float) -> float:
    net_diff = home_net - away_net + NFL_HOME_ADVANTAGE
    return round(_logistic(net_diff / 7.0), 4)


_TITLE_NOISE = frozenset({
    "spread", "1h", "2h", "1q", "2q", "3q", "4q",
    "half", "quarter", "score", "first", "moneyline", "ml",
    "double", "handicap", "team",
})


def _is_win_market(outcomes: list[str], home_name: str, away_name: str, title: str) -> bool:
    """True se o sub-mercado é de vitória simples pré-jogo (não spread/half/quarter/props).

    Critérios: outcomes = exatamente os dois times E título vazio ou sem palavras de ruído.
    """
    if len(outcomes) != 2:
        return False
    names = {o.strip().lower() for o in outcomes}
    home_last = home_name.split()[-1].lower()
    away_last = away_name.split()[-1].lower()
    if names not in ({home_last, away_last}, {home_name.lower(), away_name.lower()}):
        return False
    t = title.lower().strip()
    if not t:
        return True  # título vazio = outright win market
    if re.search(r"\d", t):
        return False  # tem número → spread ou prop
    return not any(w in t for w in _TITLE_NOISE)


def run_scan(config: dict, min_volume: float = 500.0) -> list[dict]:
    edge_min      = float(config.get("edge_minimum", 0.08))
    bet_size      = float(config.get("paper_bet_size", 20.0))
    min_price_win = float(config.get("min_price_win", 0.25))   # ignora se mercado muito skewed
    pregame_hours = float(config.get("pregame_only_hours", 4.0))  # só entra até X h antes do endDate
    results: list[dict] = []

    if not _NBA_AVAILABLE:
        log_event("nba_nfl", {"type": "data_unavailable", "reason": "nba_api not installed"})
        print("[NBA_NFL] nba_api not available — pip install nba_api")
        return []

    season      = _current_nba_season()
    all_ratings = _fetch_all_team_ratings(season)
    open_keys        = read_open_position_keys("nba_nfl")
    entered_this_run: set[str] = set()   # dedup intra-ciclo
    now_utc          = datetime.now(timezone.utc)

    print(f"[NBA_NFL] ratings carregados: {len(all_ratings)} times (temporada {season})")
    log_event("nba_nfl", {"type": "scan_start", "season": season, "teams_rated": len(all_ratings)})

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

        # Verificar endDate — pular jogos em andamento ou encerrados
        end_date_str = event.get("endDate") or ""
        try:
            end_utc = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            end_utc = None

        if end_utc is not None:
            secs_remaining = (end_utc - now_utc).total_seconds()
            # endDate em eventos NBA/NFL é o tip-off; pular se dentro da janela do jogo
            if secs_remaining < pregame_hours * 3600:
                log_event("nba_nfl", {
                    "type": "skip_live_or_ended",
                    "market_slug": slug,
                    "end_utc": end_date_str,
                    "secs_remaining": round(secs_remaining),
                })
                continue

        title_lower = title.lower() + " " + slug.lower()
        is_nba = "nba" in title_lower or "basketball" in title_lower
        is_nfl = "nfl" in title_lower or "super bowl" in title_lower

        if not is_nba and not is_nfl:
            if _find_team_in_text(title_lower, TEAM_MAP_NBA):
                is_nba = True
            elif _find_team_in_text(title_lower, TEAM_MAP_NFL):
                is_nfl = True
        if not is_nba and not is_nfl:
            continue

        team_map = TEAM_MAP_NBA if is_nba else TEAM_MAP_NFL
        league   = "NBA" if is_nba else "NFL"

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
            home_stats = _nba_team_stats(home_id, all_ratings)
            away_stats = _nba_team_stats(away_id, all_ratings)
            if not home_stats or not away_stats:
                continue
            p_home = _nba_win_prob(home_stats["net_rating"], away_stats["net_rating"])
        else:
            home_stats = {"net_rating": 0.0, "win_rate": 0.5}
            away_stats = {"net_rating": 0.0, "win_rate": 0.5}
            p_home = _nfl_win_prob(0.0, 0.0)

        p_away = round(1.0 - p_home, 4)

        poly_markets = event.get("markets", [])
        for pm in poly_markets:
            outcomes       = parse_list_field(pm.get("outcomes", []))
            outcome_prices = parse_list_field(pm.get("outcomePrices", []))
            pm_title       = str(pm.get("groupItemTitle") or pm.get("title") or "")

            # Só mercados de vitória simples — outcomes=dois times E título sem ruído
            if not _is_win_market(outcomes, home_name, away_name, pm_title):
                continue
            for outcome, price_str in zip(outcomes, outcome_prices):
                try:
                    price_poly = float(price_str)
                except Exception:
                    continue
                if price_poly <= 0 or price_poly >= 1:
                    continue
                # Preço muito skewed → jogo provavelmente em andamento
                if price_poly < min_price_win:
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
                        "home_win_rate": home_stats.get("win_rate"),
                        "away_win_rate": away_stats.get("win_rate"),
                        "h2h_win_rate": None,
                    },
                    "price_polymarket": round(price_poly, 4),
                    "edge": round(edge, 4),
                    "ev_per_dollar": round(ev, 4),
                    "would_enter": enter,
                    "entry_side": "YES",
                    "end_utc": end_date_str,
                }
                log_event("nba_nfl", row)

                if enter:
                    pos_key = slug + "|" + str(outcome)
                    if pos_key in open_keys or pos_key in entered_this_run:
                        continue
                    entered_this_run.add(pos_key)
                    entry_row = {
                        **row,
                        "type":             "simulated_entry",
                        "entry_price":      round(price_poly, 4),
                        "shares_simulated": shares_for_bet(bet_size, price_poly),
                        "bet_size":         bet_size,
                    }
                    log_event("nba_nfl", entry_row)
                    results.append(entry_row)
                    print(f"[{league}] EV+ {home_name} vs {away_name} — {outcome}: "
                          f"edge={edge:.1%} poly={price_poly:.3f} model={prob_real:.3f}")

    log_event("nba_nfl", {"type": "scan_end", "ev_found": len(results)})
    return results
