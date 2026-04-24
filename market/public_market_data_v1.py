from __future__ import annotations

from typing import Dict, List, Optional

import requests

CLOB_API = "https://clob.polymarket.com"
TIMEOUT = 3.0


def fetch_midpoints(token_ids: List[str]) -> Dict[str, Optional[float]]:
    token_ids = [str(t) for t in token_ids if t]
    if not token_ids:
        return {}
    url = f"{CLOB_API}/midpoints"
    payload = [{"token_id": t} for t in token_ids]
    try:
        res = requests.post(url, json=payload, timeout=TIMEOUT)
        res.raise_for_status()
        data = res.json() or {}
        out: Dict[str, Optional[float]] = {}
        for token_id in token_ids:
            value = data.get(token_id)
            try:
                out[token_id] = float(value) if value is not None else None
            except Exception:
                out[token_id] = None
        return out
    except Exception as e:
        print(f"[ERROR] Failed to fetch midpoints: {e}")
        return {t: None for t in token_ids}


def fetch_spread(token_id: str) -> Optional[float]:
    if not token_id:
        return None
    url = f"{CLOB_API}/spread"
    try:
        res = requests.get(url, params={"token_id": str(token_id)}, timeout=TIMEOUT)
        res.raise_for_status()
        data = res.json() or {}
        value = data.get("spread")
        return float(value) if value is not None else None
    except Exception as e:
        print(f"[ERROR] Failed to fetch spread for {token_id}: {e}")
        return None


def fetch_prices(token_ids: List[str], sides: List[str]) -> Dict[str, Dict[str, Optional[float]]]:
    token_ids = [str(t) for t in token_ids if t]
    sides = [str(s).upper() for s in sides if s]
    if not token_ids or not sides or len(token_ids) != len(sides):
        return {}
    url = f"{CLOB_API}/prices"
    try:
        res = requests.get(
            url,
            params={
                "token_ids": ",".join(token_ids),
                "sides": ",".join(sides),
            },
            timeout=TIMEOUT,
        )
        res.raise_for_status()
        data = res.json() or {}
        out: Dict[str, Dict[str, Optional[float]]] = {}
        for token_id, side in zip(token_ids, sides):
            value = ((data.get(token_id) or {}).get(side))
            try:
                num = float(value) if value is not None else None
            except Exception:
                num = None
            out.setdefault(token_id, {})[side] = num
        return out
    except Exception as e:
        print(f"[ERROR] Failed to fetch prices: {e}")
        out: Dict[str, Dict[str, Optional[float]]] = {}
        for token_id, side in zip(token_ids, sides):
            out.setdefault(token_id, {})[side] = None
        return out
