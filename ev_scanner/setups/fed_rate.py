from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from ev_scanner.utils.ev_calculator import calculate_edge, ev_per_dollar, should_enter, shares_for_bet
from ev_scanner.utils.logger import log_event, read_open_position_keys
from ev_scanner.utils.polymarket_api import search_markets, parse_list_field

# CME FedWatch JSON endpoint alternatives (tentamos em ordem)
_CME_URLS = [
    "https://www.cmegroup.com/CmeWS/mvc/Meeting/FedWatch/V2/",
    "https://www.cmegroup.com/CmeWS/mvc/Meeting/FedWatch/V1/",
]

# Mapeamento bps → action: aceita "50+", "50", "+50" bps
_BPS_PATTERN = re.compile(r"(\d+)\+?\s*bps?", re.I)


def _parse_bps(title: str) -> Optional[tuple[str, int]]:
    """
    Extrai (action, bps) de títulos como '25 bps decrease', '50+ bps increase', 'No change'.
    Retorna None se não reconhecer ou se contiver múltiplas ações (sequência).
    """
    t = title.lower().strip()

    # Rejeita sequências multi-ação antes de qualquer parse
    action_words = sum(1 for w in ["cut", "decrease", "hike", "increase", "raise",
                                    "pause", "hold", "unchanged", "no change"]
                       if w in t)
    if action_words > 1:
        return None

    if any(w in t for w in ["no change", "hold", "pause", "unchanged"]):
        return ("hold", 0)

    m = _BPS_PATTERN.search(t)
    if not m:
        return None
    bps = int(m.group(1))

    if any(w in t for w in ["decrease", "cut", "lower"]):
        return ("cut", bps)
    if any(w in t for w in ["increase", "hike", "raise"]):
        return ("hike", bps)
    return None


def _is_single_meeting_market(slug: str, title: str) -> bool:
    """True apenas para mercados de reunião única (não sequência de múltiplas reuniões)."""
    s = slug.lower()
    # Plural "decisions" → sequência; singular "decision" → reunião única
    if "decisions" in s:
        return False
    # Segurança extra: rejeita se slug tem dois segmentos de mês (ex: apr-jul, jun-sep)
    if re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)-"
                 r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", s):
        return False
    return True


def _fetch_cme_fedwatch() -> Optional[dict[str, float]]:
    """
    Busca probabilidades implícitas do CME FedWatch para a próxima reunião.
    Retorna dict {'cut': p, 'hold': p, 'hike': p} ou None se indisponível.
    """
    for url in _CME_URLS:
        try:
            resp = requests.get(url, timeout=10,
                                headers={"User-Agent": "ev-scanner/1.0",
                                         "Referer": "https://www.cmegroup.com/"})
            if resp.status_code != 200:
                continue
            data = resp.json()
            meetings = data if isinstance(data, list) else data.get("meetings", data.get("data", []))
            if not meetings:
                continue
            next_mtg = meetings[0]
            probs_raw = next_mtg.get("probabilities", next_mtg.get("data", {}))
            if not isinstance(probs_raw, dict):
                continue

            result: dict[str, float] = {}
            for k, v in probs_raw.items():
                k_lower = k.lower()
                prob = float(v) / 100.0 if float(v) > 1 else float(v)
                if any(w in k_lower for w in ["cut", "decrease", "lower"]):
                    result["cut"] = result.get("cut", 0.0) + prob
                elif any(w in k_lower for w in ["hike", "increase", "raise"]):
                    result["hike"] = result.get("hike", 0.0) + prob
                elif any(w in k_lower for w in ["hold", "pause", "unchanged", "no change"]):
                    result["hold"] = result.get("hold", 0.0) + prob
            if result:
                return result
        except Exception:
            continue
    return None


def run_scan(config: dict, min_volume: float = 500.0) -> list[dict]:
    edge_min        = float(config.get("edge_minimum", 0.06))
    bet_size        = float(config.get("paper_bet_size", 20.0))
    min_divergence  = float(config.get("min_cme_divergence", 0.06))
    results: list[dict] = []

    open_keys        = read_open_position_keys("fed_rate")
    entered_this_run: set[str] = set()

    log_event("fed_rate", {"type": "scan_start"})

    # Buscar probabilidades do CME FedWatch — única fonte de prob_real válida
    cme_probs = _fetch_cme_fedwatch()

    if cme_probs is None:
        log_event("fed_rate", {
            "type": "data_unavailable",
            "reason": "CME FedWatch inacessivel — sem prob_real valida, abortando scan",
        })
        print("[FED_RATE] CME FedWatch indisponivel — scan abortado (sem edge model)")
        log_event("fed_rate", {"type": "scan_end", "ev_found": 0, "aborted": True})
        return []

    log_event("fed_rate", {"type": "cme_probs", "probs": cme_probs})
    print(f"[FED_RATE] CME FedWatch: cut={cme_probs.get('cut',0):.1%} "
          f"hold={cme_probs.get('hold',0):.1%} hike={cme_probs.get('hike',0):.1%}")

    markets = search_markets(
        keywords=["fed", "federal reserve", "rate cut", "rate hike", "fomc", "interest rate"],
        tag_slug="fed-rates",
        limit=100,
    )

    for event in markets:
        slug  = event.get("slug", "")
        title = event.get("title", "")
        vol   = float(event.get("volume") or 0)
        if vol < min_volume:
            continue

        # Só mercados de reunião única — exclui sequências multi-reunião
        if not _is_single_meeting_market(slug, title):
            log_event("fed_rate", {"type": "skip_sequence", "market_slug": slug})
            continue

        # Verificar endDate — não entrar se reunião já passou
        end_date_str = event.get("endDate") or ""
        try:
            end_utc = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            secs_remaining = (end_utc - datetime.now(timezone.utc)).total_seconds()
            if secs_remaining < 0:
                continue
        except Exception:
            pass

        poly_markets = event.get("markets", [])
        for pm in poly_markets:
            pm_title  = str(pm.get("groupItemTitle") or pm.get("title") or "")
            prices    = parse_list_field(pm.get("outcomePrices", []))
            outcomes  = parse_list_field(pm.get("outcomes", []))
            if not pm_title or not prices:
                continue
            try:
                yes_idx    = next((i for i, o in enumerate(outcomes) if str(o).upper() == "YES"), 0)
                price_poly = float(prices[yes_idx])
            except Exception:
                continue
            if price_poly <= 0 or price_poly >= 1:
                continue

            parsed = _parse_bps(pm_title)
            if parsed is None:
                continue
            action, bps = parsed

            prob_real = cme_probs.get(action)
            if prob_real is None:
                continue

            # Só entra se divergência CME vs Polymarket é suficiente
            divergence = abs(prob_real - price_poly)
            if divergence < min_divergence:
                continue

            edge  = calculate_edge(prob_real, price_poly)
            ev    = ev_per_dollar(prob_real, price_poly)
            enter = should_enter(edge, prob_real, price_poly, min_edge=edge_min)

            row: dict = {
                "type":             "ev_found" if enter else "scan",
                "market_slug":      slug,
                "outcome":          pm_title,
                "action":           action,
                "bps":              bps,
                "prob_real":        round(prob_real, 4),
                "cme_fedwatch_prob": round(prob_real, 4),
                "price_polymarket": round(price_poly, 4),
                "divergence":       round(divergence, 4),
                "edge":             round(edge, 4),
                "ev_per_dollar":    round(ev, 4),
                "would_enter":      enter,
                "entry_side":       "YES",
                "end_utc":          end_date_str,
            }
            log_event("fed_rate", row)

            if enter:
                pos_key = slug + "|" + pm_title
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
                log_event("fed_rate", entry_row)
                results.append(entry_row)
                print(f"[FED_RATE] EV+ {action} {slug} '{pm_title}': "
                      f"cme={prob_real:.1%} poly={price_poly:.3f} edge={edge:.1%}")

    log_event("fed_rate", {"type": "scan_end", "ev_found": len(results)})
    return results
