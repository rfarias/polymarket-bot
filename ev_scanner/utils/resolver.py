"""
ev_scanner/utils/resolver.py — tracking de resolução de posições simuladas

Para cada setup, varre as entradas sem saída correspondente, consulta
a Polymarket API e grava um evento simulated_exit quando o mercado resolve.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from ev_scanner.utils.logger import log_event, read_all_positions
from ev_scanner.utils.polymarket_api import fetch_event_by_slug, parse_list_field

# Preço mínimo/máximo para considerar mercado resolvido
_RESOLVED_YES_MIN = 0.99
_RESOLVED_NO_MAX  = 0.01


def _parse_temp(s: str) -> Optional[float]:
    """Extrai temperatura de strings como '23°C', '23Â°C', '-5°C or lower'."""
    s_clean = s.replace("Â°", "°").replace("Â°", "°")
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*°", s_clean)
    return float(m.group(1)) if m else None


def _is_lower(s: str) -> bool:
    return any(w in s.lower() for w in ["or below", "or lower", "and below"])


def _is_higher(s: str) -> bool:
    return any(w in s.lower() for w in ["or above", "or higher", "and above"])


def _entry_key(entry: dict) -> str:
    slug = entry.get("market_slug", "")
    setup = entry.get("setup", "")
    if setup == "weather":
        return f"{slug}|{entry.get('bucket', '')}"
    return f"{slug}|{entry.get('outcome', '')}"


def _check_weather(entry: dict, event: dict) -> Optional[dict]:
    """Retorna {won, exit_price} se o mercado de temperatura resolveu, senão None."""
    entry_temp  = _parse_temp(entry.get("bucket", ""))
    entry_lower = _is_lower(entry.get("bucket", ""))
    entry_higher = _is_higher(entry.get("bucket", ""))
    if entry_temp is None:
        return None

    for pm in event.get("markets", []):
        title = str(pm.get("groupItemTitle") or pm.get("title") or "")
        pm_temp = _parse_temp(title)
        if pm_temp is None or pm_temp != entry_temp:
            continue
        if _is_lower(title) != entry_lower or _is_higher(title) != entry_higher:
            continue

        prices   = parse_list_field(pm.get("outcomePrices", []))
        outcomes = parse_list_field(pm.get("outcomes", []))
        if not prices:
            continue
        try:
            yes_idx   = next((i for i, o in enumerate(outcomes) if str(o).upper() == "YES"), 0)
            yes_price = float(prices[yes_idx])
        except Exception:
            continue

        if yes_price >= _RESOLVED_YES_MIN:
            return {"won": True,  "exit_price": yes_price}
        if yes_price <= _RESOLVED_NO_MAX:
            return {"won": False, "exit_price": yes_price}

    return None


def _check_generic(entry: dict, event: dict) -> Optional[dict]:
    """Para NBA/NFL e Fed Rate: encontra o sub-mercado pelo outcome e verifica preço."""
    target = str(entry.get("outcome", "")).lower()
    if not target:
        return None

    for pm in event.get("markets", []):
        outcomes = parse_list_field(pm.get("outcomes", []))
        prices   = parse_list_field(pm.get("outcomePrices", []))
        title    = str(pm.get("groupItemTitle") or pm.get("title") or "").lower()
        if not prices or not outcomes:
            continue

        outcome_strs = [str(o).lower() for o in outcomes]

        if any(str(o).upper() == "YES" for o in outcomes):
            # Mercado binário YES/NO — verificar se o título corresponde ao nosso outcome
            title_match = target in title or title in target
            if not title_match:
                continue
            try:
                yes_idx   = next((i for i, o in enumerate(outcomes) if str(o).upper() == "YES"), 0)
                yes_price = float(prices[yes_idx])
            except Exception:
                continue
            if yes_price >= _RESOLVED_YES_MIN:
                return {"won": True,  "exit_price": yes_price}
            if yes_price <= _RESOLVED_NO_MAX:
                return {"won": False, "exit_price": yes_price}
        else:
            # Multi-outcome (ex: ["Knicks", "Spurs"]) — encontrar o outcome pelo nome
            for o, p in zip(outcome_strs, prices):
                o_match = o in target or target in o or any(
                    word in o for word in target.split() if len(word) > 3
                )
                if not o_match:
                    continue
                try:
                    price = float(p)
                except Exception:
                    continue
                if price >= _RESOLVED_YES_MIN:
                    return {"won": True,  "exit_price": price}
                if price <= _RESOLVED_NO_MAX:
                    return {"won": False, "exit_price": price}

    return None


def check_and_log_resolutions(setup_name: str) -> int:
    """
    Verifica posições abertas de um setup e grava simulated_exit para as resolvidas.
    Retorna quantas novas resoluções foram registradas.
    """
    all_rows = read_all_positions(setup_name)

    # Primeiro entry por chave; conjunto de chaves que já têm saída
    entries: dict[str, dict] = {}
    exit_keys: set[str] = set()
    for row in all_rows:
        key = _entry_key(row)
        if row.get("type") == "simulated_entry":
            if key not in entries:
                entries[key] = row
        elif row.get("type") == "simulated_exit":
            exit_keys.add(key)

    open_entries = {k: v for k, v in entries.items() if k not in exit_keys}
    if not open_entries:
        return 0

    resolved_count = 0
    event_cache: dict[str, dict] = {}   # slug → event data (uma chamada por slug)

    for key, entry in open_entries.items():
        slug = entry.get("market_slug", "")
        if not slug:
            continue

        if slug not in event_cache:
            time.sleep(0.3)
            event_data = fetch_event_by_slug(slug)
            event_cache[slug] = event_data or {}

        event = event_cache[slug]
        if not event:
            continue

        # Só tentar resolução se o mercado estiver fechado
        if not event.get("closed", False):
            continue

        result = None
        if setup_name == "weather":
            result = _check_weather(entry, event)
        else:
            result = _check_generic(entry, event)

        if result is None:
            continue

        bet_size    = float(entry.get("bet_size", 20.0))
        entry_price = float(entry.get("entry_price", 0.5))
        shares      = float(entry.get("shares_simulated", 0.0))
        pnl = shares * (1.0 - entry_price) if result["won"] else -bet_size

        exit_row: dict = {
            "type":            "simulated_exit",
            "market_slug":     slug,
            "entry_key":       key,
            "entry_ts":        entry.get("ts_human", ""),
            "entry_price":     entry_price,
            "exit_price":      result["exit_price"],
            "won":             result["won"],
            "pnl_simulated":   round(pnl, 4),
            "bet_size":        bet_size,
            "shares_simulated": shares,
        }
        for field in ("city", "bucket", "resolution_date", "outcome",
                      "league", "home_team", "away_team", "action"):
            if field in entry:
                exit_row[field] = entry[field]

        log_event(setup_name, exit_row)
        resolved_count += 1
        status = "WIN " if result["won"] else "LOSS"
        print(f"[{setup_name.upper()}] RESOLVED {status}  {slug}  pnl=${pnl:.2f}")

    return resolved_count
