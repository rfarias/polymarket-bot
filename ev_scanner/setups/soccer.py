"""
ev_scanner/setups/soccer.py

Fonte de dados: SofaScore API (unofficial, sem chave) + ESPN como fallback.
Suporta: jogos de seleções (Copa do Mundo, amistosos, Nations League, etc.)
         e competições de clube (EPL, La Liga, UCL, Libertadores, etc.)

Resolução Polymarket: fifa.com para internacionais; fontes oficiais de cada liga.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from ev_scanner.utils.ev_calculator import calculate_edge, ev_per_dollar, should_enter, shares_for_bet
from ev_scanner.utils.logger import log_event
from ev_scanner.utils.polymarket_api import get_active_markets, parse_list_field

# ---------------------------------------------------------------------------
# Competições mapeadas — SofaScore tournament IDs + nomes
# ---------------------------------------------------------------------------
# Formato: "slug_keyword": (sofascore_tournament_id, nome_legivel)
# IDs confirmados da SofaScore API pública

COMPETITIONS: dict[str, tuple[int, str]] = {
    # Internacionais (seleções)
    "fifa.world":           (16,    "FIFA World Cup"),
    "worldq.conmebol":      (28,    "WC Qualifiers CONMEBOL"),
    "worldq.uefa":          (27,    "WC Qualifiers UEFA"),
    "worldq.caf":           (29,    "WC Qualifiers CAF"),
    "worldq.afc":           (30,    "WC Qualifiers AFC"),
    "worldq.concacaf":      (31,    "WC Qualifiers CONCACAF"),
    "friendly":             (11,    "FIFA Friendlies"),
    "uefa.nations":         (1091,  "UEFA Nations League"),
    "uefa.euro":            (1,     "UEFA Euro"),
    "copa.america":         (133,   "Copa America"),
    "concacaf.gold":        (140,   "CONCACAF Gold Cup"),
    "afcon":                (42,    "Africa Cup of Nations"),
    "afc.asian":            (72,    "AFC Asian Cup"),
    # Europa — clubes
    "eng.1":                (17,    "Premier League"),
    "esp.1":                (8,     "La Liga"),
    "ger.1":                (35,    "Bundesliga"),
    "ita.1":                (23,    "Serie A"),
    "fra.1":                (34,    "Ligue 1"),
    "por.1":                (238,   "Primeira Liga"),
    "ned.1":                (37,    "Eredivisie"),
    "tur.1":                (52,    "Süper Lig"),
    "sco.1":                (36,    "Scottish Premiership"),
    "gre.1":                (238,   "Super League Greece"),  # aproximado
    "rus.1":                (203,   "Russian Premier League"),
    "uefa.champions":       (7,     "UEFA Champions League"),
    "uefa.europa":          (679,   "UEFA Europa League"),
    "uefa.conf":            (17015, "UEFA Conference League"),
    "fifa.cwc":             (4,     "FIFA Club World Cup"),
    # Américas — clubes
    "usa.1":                (242,   "MLS"),
    "bra.1":                (325,   "Brasileirão Série A"),
    "bra.2":                (390,   "Brasileirão Série B"),
    "arg.1":                (155,   "Liga Profesional Argentina"),
    "col.1":                (11536, "Liga BetPlay"),
    "chi.1":                (422,   "Primera División Chile"),
    "mex.1":                (352,   "Liga MX"),
    "uru.1":                (278,   "Primera División Uruguay"),
    "ecu.1":                (240,   "Serie A Ecuador"),
    "per.1":                (406,   "Liga 1 Peru"),
    "ven.1":                (231,   "Primera División Venezuela"),
    "libertadores":         (384,   "Copa Libertadores"),
    "sudamericana":         (480,   "Copa Sudamericana"),
    "concacaf.champions":   (1320,  "CONCACAF Champions Cup"),
    # Ásia / Oriente Médio
    "afc.champions":        (695,   "AFC Champions League"),
    "jpn.1":                (196,   "J1 League"),
    "chn.1":                (339,   "Chinese Super League"),
    "sau.1":                (955,   "Saudi Pro League"),
    # África
    "caf.champions":        (205,   "CAF Champions League"),
    "rsa.1":                (1185,  "PSL South Africa"),
}

SOFA_BASE   = "https://api.sofascore.com/api/v1"
_SOFA_SESS  = requests.Session()
_SOFA_SESS.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.sofascore.com/",
})


def _sofa_get(path: str, params: dict | None = None) -> dict:
    resp = _SOFA_SESS.get(f"{SOFA_BASE}/{path}", params=params or {}, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# SofaScore: busca jogos futuros de uma competição
# ---------------------------------------------------------------------------
def _sofa_upcoming(tournament_id: int, days_ahead: int = 14) -> list[dict]:
    """Retorna jogos futuros de um torneio nos próximos N dias."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        data = _sofa_get(f"unique-tournament/{tournament_id}/events/next/0")
        return data.get("events", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# SofaScore: form recente de um time
# ---------------------------------------------------------------------------
def _sofa_team_form(team_id: int, n: int = 10) -> dict:
    """Busca últimos N jogos do time e calcula form."""
    try:
        data = _sofa_get(f"team/{team_id}/events/last/0")
        events = data.get("events", [])
    except Exception:
        return {}

    wins = draws = losses = gf = ga = 0
    count = 0
    for ev in reversed(events):
        home = ev.get("homeTeam", {})
        away = ev.get("awayTeam", {})
        score = ev.get("homeScore") or {}
        h_goals = score.get("current") if isinstance(score, dict) else None
        score_a = ev.get("awayScore") or {}
        a_goals = score_a.get("current") if isinstance(score_a, dict) else None
        if h_goals is None or a_goals is None:
            continue
        is_home = home.get("id") == team_id
        my_g  = h_goals if is_home else a_goals
        opp_g = a_goals if is_home else h_goals
        gf += my_g; ga += opp_g
        if my_g > opp_g:    wins += 1
        elif my_g == opp_g: draws += 1
        else:               losses += 1
        count += 1
        if count >= n: break

    if count == 0:
        return {}
    return {
        "win_rate":  wins  / count,
        "draw_rate": draws / count,
        "loss_rate": losses / count,
        "gf_avg":    gf / count,
        "ga_avg":    ga / count,
        "n":         count,
    }


def _sofa_find_team(tournament_id: int, name: str) -> Optional[int]:
    """Busca ID do time no torneio pelo nome."""
    try:
        data = _sofa_get(f"unique-tournament/{tournament_id}/season/latest/top-teams/overall")
        teams = data.get("topTeams", {})
        all_teams = []
        for bucket in teams.values():
            if isinstance(bucket, list):
                all_teams += [item.get("team", {}) for item in bucket]
        name_lower = name.lower()
        for t in all_teams:
            if (name_lower in t.get("name", "").lower() or
                    name_lower in t.get("shortName", "").lower()):
                return t.get("id")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Modelo Poisson
# ---------------------------------------------------------------------------
def _poisson_prob(lam: float, k: int) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _match_probs(lh: float, la: float, max_g: int = 8) -> tuple[float, float, float]:
    ph = pd = pa = 0.0
    for i in range(max_g + 1):
        for j in range(max_g + 1):
            p = _poisson_prob(max(0.01, lh), i) * _poisson_prob(max(0.01, la), j)
            if i > j: ph += p
            elif i == j: pd += p
            else: pa += p
    t = ph + pd + pa
    return ph / t, pd / t, pa / t


def _probs_from_form(home: dict, away: dict, league_avg: float = 1.45) -> tuple[float, float, float]:
    if not home or not away:
        return 0.45, 0.28, 0.27
    lh = home.get("gf_avg", 1.4) * (away.get("ga_avg", 1.4) / league_avg)
    la = away.get("gf_avg", 1.2) * (home.get("ga_avg", 1.4) / league_avg)
    ph_p, pd_p, pa_p = _match_probs(lh, la)
    # Combinar Poisson com win rate direta
    ph_f = min(0.80, home.get("win_rate", 0.45) + 0.05)  # leve vantagem casa
    pa_f = away.get("win_rate", 0.35)
    pd_f = max(0.05, 1.0 - ph_f - pa_f)
    ph = 0.5 * ph_p + 0.5 * ph_f
    pd = 0.5 * pd_p + 0.5 * pd_f
    pa = max(0.02, 1.0 - ph - pd)
    return round(ph, 4), round(pd, 4), round(pa, 4)


# ---------------------------------------------------------------------------
# Extração de times do título do mercado
# ---------------------------------------------------------------------------
def _extract_teams(title: str) -> tuple[Optional[str], Optional[str]]:
    m = re.search(r"^(.+?)\s+(?:vs\.?|v\.?|-)\s+(.+?)(?:\s*[:\(].*)?$",
                  title.strip(), re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def _infer_league(slug: str, title: str) -> Optional[tuple[str, int]]:
    """Tenta identificar a competição pelo slug/título e retorna (key, tournament_id)."""
    text = (slug + " " + title).lower()
    # Matches mais específicos primeiro
    mapping = [
        ("libertadores",      "libertadores"),
        ("sudamericana",      "sudamericana"),
        ("champions",         "uefa.champions"),
        ("europa-league",     "uefa.europa"),
        ("conference",        "uefa.conf"),
        ("premier-league",    "eng.1"),
        ("la-liga",           "esp.1"),
        ("bundesliga",        "ger.1"),
        ("serie-a",           "ita.1"),
        ("ligue-1",           "fra.1"),
        ("brasileirao",       "bra.1"),
        ("mls",               "usa.1"),
        ("liga-mx",           "mex.1"),
        ("copa-america",      "copa.america"),
        ("euro",              "uefa.euro"),
        ("nations-league",    "uefa.nations"),
        ("world-cup",         "fifa.world"),
        ("friendly",          "friendly"),
        ("fif-",              "friendly"),   # slug prefixo FIFA
    ]
    for pattern, key in mapping:
        if pattern in text:
            return key, COMPETITIONS[key][0]
    return None, None


# ---------------------------------------------------------------------------
# Scan principal
# ---------------------------------------------------------------------------
def run_scan(config: dict, min_volume: float = 500.0) -> list[dict]:
    edge_min = float(config.get("edge_minimum", 0.08))
    bet_size  = float(config.get("paper_bet_size", 20.0))
    results: list[dict] = []

    log_event("soccer", {"type": "scan_start",
                          "competitions": list(COMPETITIONS.keys())})

    markets = get_active_markets(tag_slug="soccer", limit=300)
    markets += [e for e in get_active_markets(tag_slug="football", limit=100)
                if e["slug"] not in {m["slug"] for m in markets}]

    now_utc = datetime.now(timezone.utc)
    markets_checked = 0

    for event in markets:
        slug  = event.get("slug", "")
        title = event.get("title", "")
        vol   = float(event.get("volume") or 0)
        if vol < min_volume:
            continue

        # Filtrar expirados
        end_str = event.get("endDate", "")
        if end_str:
            try:
                end_utc = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if (end_utc - now_utc).total_seconds() < 7200:
                    continue
            except Exception:
                pass

        # Precisa ter 3 sub-mercados (Home/Draw/Away)
        subs = event.get("markets", [])
        if len(subs) < 3:
            continue
        draw_sub = next((s for s in subs if isinstance(s, dict) and
                         "draw" in str(s.get("groupItemTitle", "")).lower()), None)
        if not draw_sub:
            continue

        home_name, away_name = _extract_teams(title)
        if not home_name or not away_name:
            continue

        comp_key, tournament_id = _infer_league(slug, title)
        p_home = p_draw = p_away = None
        form_source = "unavailable"

        # Tentar SofaScore
        if tournament_id:
            try:
                home_id = _sofa_find_team(tournament_id, home_name)
                away_id = _sofa_find_team(tournament_id, away_name)
                if home_id and away_id:
                    home_form = _sofa_team_form(home_id)
                    away_form = _sofa_team_form(away_id)
                    if home_form and away_form:
                        p_home, p_draw, p_away = _probs_from_form(home_form, away_form)
                        form_source = f"sofascore:{comp_key}"
                time.sleep(0.5)
            except Exception:
                pass

        # Fallback: preços do mercado como referência (NÃO gera EV+ — seria circular)
        if p_home is None:
            try:
                def _find_sub(subs, name, exclude=None):
                    parts = name.lower().split()
                    for s in subs:
                        if not isinstance(s, dict): continue
                        if exclude and s is exclude: continue
                        t = str(s.get("groupItemTitle","")).lower()
                        if "draw" in t: continue
                        if any(p in t for p in parts):
                            return s
                    return None

                home_sub = _find_sub(subs, home_name)
                away_sub = _find_sub(subs, away_name, exclude=home_sub)
                # Fallback posicional se nao encontrar por nome
                if not home_sub:
                    home_sub = next((s for s in subs if isinstance(s,dict) and s is not draw_sub), None)
                if not away_sub:
                    away_sub = next((s for s in reversed(subs) if isinstance(s,dict) and s is not draw_sub and s is not home_sub), None)
                if not home_sub or not away_sub:
                    continue
                p_home = float(parse_list_field(home_sub.get("outcomePrices", []))[0])
                p_draw = float(parse_list_field(draw_sub.get("outcomePrices", []))[0])
                p_away = float(parse_list_field(away_sub.get("outcomePrices", []))[0])
                form_source = "market_prior"
            except Exception:
                continue

        # Avaliar cada sub-mercado
        for pm in subs:
            if not isinstance(pm, dict):
                continue
            pm_title = str(pm.get("groupItemTitle") or pm.get("title") or "")
            prices   = parse_list_field(pm.get("outcomePrices", []))
            outcomes = parse_list_field(pm.get("outcomes", []))
            if not prices or not pm_title:
                continue
            try:
                yes_idx    = next((i for i, o in enumerate(outcomes) if str(o).upper() == "YES"), 0)
                price_poly = float(prices[yes_idx])
            except Exception:
                continue
            if price_poly <= 0 or price_poly >= 1:
                continue

            pm_lower = pm_title.lower()
            if "draw" in pm_lower:
                prob_real = p_draw
            elif any(p in pm_lower for p in home_name.lower().split()):
                prob_real = p_home
            elif any(p in pm_lower for p in away_name.lower().split()):
                prob_real = p_away
            else:
                idx = next((i for i, s in enumerate(subs) if s is pm), -1)
                prob_real = [p_home, p_draw, p_away][idx] if 0 <= idx <= 2 else None
            if prob_real is None:
                continue

            edge  = calculate_edge(prob_real, price_poly)
            ev    = ev_per_dollar(prob_real, price_poly)
            # market_prior é circular — nunca gera EV+ real
            enter = should_enter(edge, prob_real, price_poly, min_edge=edge_min) and form_source != "market_prior"

            row: dict = {
                "type":             "ev_found" if enter else "scan",
                "market_slug":      slug,
                "title":            title,
                "home_team":        home_name,
                "away_team":        away_name,
                "outcome":          pm_title,
                "competition":      comp_key or "unknown",
                "form_source":      form_source,
                "prob_home":        round(p_home, 4),
                "prob_draw":        round(p_draw, 4),
                "prob_away":        round(p_away, 4),
                "prob_real_target": round(prob_real, 4),
                "price_polymarket": round(price_poly, 4),
                "edge":             round(edge, 4),
                "ev_per_dollar":    round(ev, 4),
                "would_enter":      enter,
                "entry_side":       "YES",
            }
            log_event("soccer", row)
            markets_checked += 1

            if enter:
                entry_row = {
                    **row,
                    "type":             "simulated_entry",
                    "entry_price":      round(price_poly, 4),
                    "shares_simulated": shares_for_bet(bet_size, price_poly),
                    "bet_size":         bet_size,
                }
                log_event("soccer", entry_row)
                results.append(entry_row)
                print(f"[SOCCER] EV+  {title[:38]:<38}  '{pm_title}': "
                      f"edge={edge:.1%}  poly={price_poly:.3f}  real={prob_real:.3f}  [{form_source}]")

    log_event("soccer", {"type": "scan_end",
                          "markets_checked": markets_checked,
                          "ev_found": len(results)})
    return results
