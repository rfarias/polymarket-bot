"""
run_multi_coin_observer.py
Observa livros de múltiplas moedas/timeframes em paralelo.

Mercados monitorados (por padrão):
  - ETH 5m, SOL 5m, XRP 5m   (mesmos slots que BTC 5m)
  - BTC 15m

Para cada slot ativo, loga um snap a cada poll com:
  up_bid, down_bid, secs_to_end, coin, timeframe,
  leader (qual lado é maior), bid_vel_30s (velocidade do líder nos últimos 30s),
  spot_ref (preço Binance REST, atualizado a cada 30s)

Uso:
  python run_multi_coin_observer.py [--seconds 21600] [--poll 1.5]

Log: logs/multi_coin_observer_{ts}/observer.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

from market.book_5m import fetch_books_for_tokens, fetch_market_metadata_from_slug
from market.queue_multi_v1 import build_coin_queue

# Cache de token_ids por slug — evita metadata call a cada poll
_TOKEN_CACHE: Dict[str, list] = {}

# ---------------------------------------------------------------------------
# Configuração dos mercados a observar
# ---------------------------------------------------------------------------

MARKETS_TO_OBSERVE: list[Tuple[str, str]] = [
    ("eth", "5m"),
    ("sol", "5m"),
    ("xrp", "5m"),
    ("btc", "15m"),
]

BINANCE_SYMBOLS: Dict[str, str] = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}

SPOT_REFRESH_SECS = 30.0
QUEUE_REFRESH_SECS = 45.0   # re-descobre mercados a cada 45s
POLL_DEFAULT = 1.5
MAX_BID_HISTORY = 60         # snaps para velocidade (60 * 1.5s ≈ 90s de janela)


# ---------------------------------------------------------------------------
# Spot price via Binance REST (leve, sem WebSocket)
# ---------------------------------------------------------------------------

def _fetch_spot(symbol: str) -> Optional[float]:
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Histórico de bids para velocidade rolling
# ---------------------------------------------------------------------------

class _BidHistory:
    def __init__(self, maxlen: int = MAX_BID_HISTORY):
        self._up: deque = deque(maxlen=maxlen)
        self._down: deque = deque(maxlen=maxlen)

    def push(self, ts: float, up_bid: float, down_bid: float) -> None:
        self._up.append((ts, up_bid))
        self._down.append((ts, down_bid))

    def velocity(self, side: str, window_secs: float = 30.0) -> float:
        hist = self._up if side == "UP" else self._down
        if len(hist) < 2:
            return 0.0
        now_ts, now_bid = hist[-1]
        cutoff = now_ts - window_secs
        ref_ts, ref_bid = hist[0]
        for ts, bid in hist:
            if ts >= cutoff:
                ref_ts, ref_bid = ts, bid
                break
        elapsed = now_ts - ref_ts
        if elapsed < 0.5:
            return 0.0
        return round((now_bid - ref_bid) / elapsed, 6)


# ---------------------------------------------------------------------------
# Fetch livro para um slot ativo
# ---------------------------------------------------------------------------

def _fetch_token_mapping(slug: str) -> Optional[list]:
    """Retorna [(token_id, outcome), ...] para o slug. Cacheado em _TOKEN_CACHE."""
    if slug in _TOKEN_CACHE:
        return _TOKEN_CACHE[slug]
    meta = fetch_market_metadata_from_slug(slug)
    if not meta:
        return None
    mapping = [(x["token_id"], str(x.get("outcome") or "").upper())
               for x in meta["token_mapping"] if x.get("token_id")]
    if mapping:
        _TOKEN_CACHE[slug] = mapping
    return mapping or None


def _fetch_slot_snap(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Busca up_bid/down_bid para um item de queue (slug ativo)."""
    try:
        mapping = _fetch_token_mapping(item["slug"])
        if not mapping:
            return None
        token_ids = [tid for tid, _ in mapping]
        raw_books = fetch_books_for_tokens(token_ids)

        prices: Dict[str, float] = {}
        for tid, outcome in mapping:
            book = next((b for b in raw_books if str(b.get("asset_id")) == str(tid)), None)
            if book:
                bids = book.get("bids") or []
                prices[outcome] = float(bids[0]["price"]) if bids else 0.0

        up_bid = prices.get("UP", 0.0)
        down_bid = prices.get("DOWN", 0.0)
        return {"up_bid": up_bid, "down_bid": down_bid}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Estado por (coin, timeframe)
# ---------------------------------------------------------------------------

class _CoinState:
    def __init__(self, coin: str, timeframe: str):
        self.coin = coin
        self.timeframe = timeframe
        self.queue: Dict[str, Any] = {}
        self.last_queue_refresh: float = 0.0
        self.bid_hist: Dict[str, _BidHistory] = {}   # slug → history
        self.spot_ref: Optional[float] = None
        self.last_spot_refresh: float = 0.0

    def needs_queue_refresh(self, now: float) -> bool:
        return (now - self.last_queue_refresh) > QUEUE_REFRESH_SECS

    def needs_spot_refresh(self, now: float) -> bool:
        return (now - self.last_spot_refresh) > SPOT_REFRESH_SECS

    def refresh_queue(self) -> None:
        self.queue = build_coin_queue(self.coin, self.timeframe, verbose=False)
        self.last_queue_refresh = time.time()

    def refresh_spot(self) -> None:
        sym = BINANCE_SYMBOLS.get(self.coin)
        if sym:
            price = _fetch_spot(sym)
            if price:
                self.spot_ref = price
        self.last_spot_refresh = time.time()

    def active_slots(self) -> list[Tuple[str, Dict[str, Any]]]:
        return [(k, v) for k, v in self.queue.items() if v is not None]


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def _log_snap(log_fp, snap: Dict[str, Any]) -> None:
    log_fp.write(json.dumps(snap, ensure_ascii=False) + "\n")
    log_fp.flush()


def run(run_seconds: int = 21600, poll_secs: float = POLL_DEFAULT) -> None:
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path("logs") / f"multi_coin_observer_{ts_str}"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "observer.jsonl"

    print(f"[OBSERVER] Log: {log_path}")
    print(f"[OBSERVER] Mercados: {[f'{c}-{t}' for c,t in MARKETS_TO_OBSERVE]}")
    print(f"[OBSERVER] Run: {run_seconds}s | poll: {poll_secs}s")

    states = {(c, t): _CoinState(c, t) for c, t in MARKETS_TO_OBSERVE}

    deadline = time.time() + run_seconds
    with open(log_path, "w", encoding="utf-8") as log_fp:
        while time.time() < deadline:
            now = time.time()

            # Refresh queues e spot quando necessário
            for state in states.values():
                if state.needs_queue_refresh(now):
                    state.refresh_queue()
                if state.needs_spot_refresh(now):
                    state.refresh_spot()

            # Coleta livros em paralelo para todos os slots ativos
            tasks: list[Tuple[_CoinState, str, Dict]] = []
            for state in states.values():
                for slot_name, item in state.active_slots():
                    tasks.append((state, slot_name, item))

            if not tasks:
                time.sleep(poll_secs)
                continue

            def _fetch(args: Tuple[_CoinState, str, Dict]):
                state, slot_name, item = args
                snap = _fetch_slot_snap(item)
                return state, slot_name, item, snap

            results = []
            with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
                futs = {ex.submit(_fetch, t): t for t in tasks}
                for fut in as_completed(futs):
                    try:
                        results.append(fut.result())
                    except Exception:
                        pass

            snap_ts = time.time()
            for state, slot_name, item, snap in results:
                if snap is None:
                    continue

                up_bid = snap["up_bid"]
                down_bid = snap["down_bid"]
                slug = item["slug"]
                secs = item.get("seconds_to_end")

                # Actualiza bid history
                bh = state.bid_hist.setdefault(slug, _BidHistory())
                bh.push(snap_ts, up_bid, down_bid)

                # Leader e velocidade
                if up_bid > down_bid:
                    leader = "UP"
                    leader_bid = up_bid
                elif down_bid > up_bid:
                    leader = "DOWN"
                    leader_bid = down_bid
                else:
                    leader = None
                    leader_bid = max(up_bid, down_bid)

                bid_vel = bh.velocity(leader or "UP", window_secs=30.0) if leader else 0.0

                record = {
                    "type": "multi_snap",
                    "ts": round(snap_ts, 3),
                    "coin": state.coin,
                    "tf": state.timeframe,
                    "slot": slot_name,
                    "slug": slug,
                    "secs": secs,
                    "up_bid": round(up_bid, 4),
                    "down_bid": round(down_bid, 4),
                    "leader": leader,
                    "leader_bid": round(leader_bid, 4) if leader_bid else 0.0,
                    "bid_vel_30s": round(bid_vel, 5),
                    "spot_ref": state.spot_ref,
                }
                _log_snap(log_fp, record)

                # Display conciso
                vel_s = f"vel={bid_vel:+.4f}" if abs(bid_vel) > 0.0001 else ""
                spot_s = f"  ${state.spot_ref:.2f}" if state.spot_ref else ""
                side_s = f"{leader or '???':4s}@{leader_bid:.3f}" if leader_bid else "---"
                print(
                    f"  {state.coin.upper():3s}-{state.timeframe:3s} "
                    f"{slot_name:7s}  secs={str(secs):>5}  "
                    f"{side_s}  up={up_bid:.3f} dn={down_bid:.3f}  "
                    f"{vel_s}{spot_s}"
                )

            time.sleep(poll_secs)

    print(f"[OBSERVER] Concluído. Log: {log_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-coin Polymarket observer")
    parser.add_argument("--seconds", type=int, default=21600, help="Duração (s)")
    parser.add_argument("--poll", type=float, default=POLL_DEFAULT, help="Intervalo de poll (s)")
    args = parser.parse_args()
    run(run_seconds=args.seconds, poll_secs=args.poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())
