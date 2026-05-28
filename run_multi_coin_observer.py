"""
run_multi_coin_observer.py
Observa livros de múltiplas moedas/timeframes em paralelo.

Mercados monitorados:
  - ETH 5m, SOL 5m, XRP 5m   (paper EL Inversion)
  - BTC 15m                   (observe-only, benchmark)

Por slug, loga a cada poll:
  up_bid, down_bid, secs, coin, tf
  leader, leader_bid, bid_vel_30s        — estado do livro
  el_leader, el_bid_240                  — early leader (janela 181-240s)
  inv, inv_new_leader, inv_bid, flip_gap — inversão detectada (secs 15-60s)
  spot_ref, price_to_beat                — referência Binance
  dist_beat_bps, recent_vol_bps, beat_crosses_60s  — contexto spot

Uso:
  python run_multi_coin_observer.py [--seconds 21600] [--poll 1.5]

Log: logs/multi_coin_observer_{ts}/observer.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from market.book_5m import fetch_books_for_tokens, fetch_market_metadata_from_slug
from market.queue_multi_v1 import build_coin_queue

# Cache de token_ids por slug
_TOKEN_CACHE: Dict[str, list] = {}

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

MARKETS_TO_OBSERVE: List[Tuple[str, str]] = [
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

# EL Inversion params (do repositório de pesquisa)
EL_MIN_BID          = 0.55   # mínimo para ser early leader
INV_MIN_NEW_BID     = 0.60   # novo líder mínimo
INV_MAX_NEW_BID     = 0.72   # novo líder máximo
INV_MIN_FLIP_GAP    = 0.03   # novo líder deve superar antigo por ao menos 3 cents
INV_MAX_ENTRY_ASK   = 0.65   # conservador: só entra se ask <= 0.65
INV_MIN_SECS        = 15
INV_MAX_SECS        = 60

SPOT_REFRESH_SECS   = 5.0    # spot a cada 5s para historico adequado
QUEUE_REFRESH_SECS  = 45.0
POLL_DEFAULT        = 1.5
MAX_BID_HISTORY     = 120    # ~3min @ 1.5s
MAX_SPOT_HISTORY    = 120    # ~10min @ 5s


# ---------------------------------------------------------------------------
# Spot — batch Binance REST
# ---------------------------------------------------------------------------

def _fetch_one_spot(symbol: str) -> Tuple[str, Optional[float]]:
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        return symbol, float(r.json()["price"])
    except Exception:
        return symbol, None


def _fetch_all_spots(symbols: List[str]) -> Dict[str, float]:
    """Busca preços Binance em paralelo (uma chamada por símbolo)."""
    result: Dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=len(symbols)) as ex:
        for sym, price in ex.map(_fetch_one_spot, symbols):
            if price is not None:
                result[sym] = price
    return result


# ---------------------------------------------------------------------------
# EL Inversion Tracker — por slug
# ---------------------------------------------------------------------------

class _ELInversionTracker:
    """Detecta early leader e inversão para um slug."""

    def __init__(self):
        self._slug: Optional[str] = None
        self._s240: List[Dict] = []         # bids na janela 181-240s
        self.el_leader: Optional[str] = None
        self.el_bid_240: float = 0.0
        # estado de inversão
        self.inv: bool = False
        self.inv_new_leader: Optional[str] = None
        self.inv_bid: float = 0.0
        self.flip_gap: float = 0.0

    def update(self, slug: str, secs: Optional[int],
               up_bid: float, down_bid: float) -> None:
        if secs is None:
            return
        if self._slug != slug:
            self._reset(slug)

        # Janela de detecção do early leader
        if 181 <= secs <= 240:
            self._s240.append({"up_bid": up_bid, "down_bid": down_bid})
            self._compute_el()

        # Detecção de inversão: secs 15-60, lado oposto assume
        if self.el_leader and INV_MIN_SECS <= secs <= INV_MAX_SECS:
            opp = "DOWN" if self.el_leader == "UP" else "UP"
            opp_bid = down_bid if opp == "DOWN" else up_bid
            el_bid  = up_bid  if self.el_leader == "UP" else down_bid
            gap = round(opp_bid - el_bid, 4)
            if INV_MIN_NEW_BID <= opp_bid <= INV_MAX_NEW_BID and gap >= INV_MIN_FLIP_GAP:
                self.inv             = True
                self.inv_new_leader  = opp
                self.inv_bid         = round(opp_bid, 4)
                self.flip_gap        = gap

    def _compute_el(self) -> None:
        if not self._s240:
            return
        avg_up = sum(s["up_bid"]   for s in self._s240) / len(self._s240)
        avg_dn = sum(s["down_bid"] for s in self._s240) / len(self._s240)
        if avg_up >= EL_MIN_BID and avg_up >= avg_dn:
            self.el_leader  = "UP"
            self.el_bid_240 = round(avg_up, 4)
        elif avg_dn >= EL_MIN_BID and avg_dn > avg_up:
            self.el_leader  = "DOWN"
            self.el_bid_240 = round(avg_dn, 4)

    def _reset(self, slug: str) -> None:
        self._slug          = slug
        self._s240          = []
        self.el_leader      = None
        self.el_bid_240     = 0.0
        self.inv            = False
        self.inv_new_leader = None
        self.inv_bid        = 0.0
        self.flip_gap       = 0.0


# ---------------------------------------------------------------------------
# Bid history — velocidade rolling do livro
# ---------------------------------------------------------------------------

class _BidHistory:
    def __init__(self, maxlen: int = MAX_BID_HISTORY):
        self._up:   deque = deque(maxlen=maxlen)
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
# Spot history — contexto de regime
# ---------------------------------------------------------------------------

class _SpotHistory:
    def __init__(self, maxlen: int = MAX_SPOT_HISTORY):
        self._hist: deque = deque(maxlen=maxlen)  # (ts, price)

    def push(self, ts: float, price: float) -> None:
        self._hist.append((ts, price))

    def latest(self) -> Optional[float]:
        return self._hist[-1][1] if self._hist else None

    def distance_beat_bps(self, beat: Optional[float]) -> Optional[float]:
        if not beat or beat <= 0:
            return None
        spot = self.latest()
        if spot is None:
            return None
        return round((spot - beat) / beat * 10000, 2)

    def recent_vol_bps(self, window_secs: float = 60.0) -> Optional[float]:
        if not self._hist:
            return None
        now_ts = self._hist[-1][0]
        prices = [p for ts, p in self._hist if ts >= now_ts - window_secs]
        if len(prices) < 3:
            return None
        rets = [(prices[i] / prices[i - 1] - 1) * 10000 for i in range(1, len(prices))]
        mean = sum(rets) / len(rets)
        var  = sum((r - mean) ** 2 for r in rets) / len(rets)
        return round(math.sqrt(var), 3)

    def beat_crosses_60s(self, beat: Optional[float], window_secs: float = 60.0) -> Optional[int]:
        if not beat or beat <= 0 or not self._hist:
            return None
        now_ts = self._hist[-1][0]
        prices = [p for ts, p in self._hist if ts >= now_ts - window_secs]
        if len(prices) < 2:
            return 0
        signs = [1 if p >= beat else -1 for p in prices]
        return sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_epoch_from_slug(slug: str) -> Optional[int]:
    m = re.search(r"-(\d{9,10})$", slug or "")
    return int(m.group(1)) if m else None


def _fetch_token_mapping(slug: str) -> Optional[list]:
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

        return {"up_bid": prices.get("UP", 0.0), "down_bid": prices.get("DOWN", 0.0)}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Estado por (coin, timeframe)
# ---------------------------------------------------------------------------

class _CoinState:
    def __init__(self, coin: str, timeframe: str):
        self.coin      = coin
        self.timeframe = timeframe
        self.queue:    Dict[str, Any] = {}
        self.last_queue_refresh: float = 0.0
        self.last_spot_refresh:  float = 0.0
        # por slug
        self.bid_hist:   Dict[str, _BidHistory]        = {}
        self.el_tracker: Dict[str, _ELInversionTracker] = {}
        self.price_to_beat: Dict[str, Optional[float]] = {}   # slug → beat price
        # spot history partilhada por coin
        self.spot_hist: _SpotHistory = _SpotHistory()
        self.spot_ref:  Optional[float] = None

    def needs_queue_refresh(self, now: float) -> bool:
        return (now - self.last_queue_refresh) > QUEUE_REFRESH_SECS

    def needs_spot_refresh(self, now: float) -> bool:
        return (now - self.last_spot_refresh) > SPOT_REFRESH_SECS

    def update_spot(self, price: float) -> None:
        self.spot_ref = price
        self.spot_hist.push(time.time(), price)
        self.last_spot_refresh = time.time()

    def refresh_queue(self) -> None:
        self.queue = build_coin_queue(self.coin, self.timeframe, verbose=False)
        self.last_queue_refresh = time.time()

    def active_slots(self) -> List[Tuple[str, Dict[str, Any]]]:
        return [(k, v) for k, v in self.queue.items() if v is not None]

    def register_price_to_beat(self, slug: str, secs: Optional[int]) -> None:
        """Registra price_to_beat como o primeiro spot observado quando secs ≈ max."""
        if slug in self.price_to_beat:
            return
        step = 300 if self.timeframe == "5m" else 900
        if secs is not None and secs >= step - 20:   # primeiros 20s do candle
            self.price_to_beat[slug] = self.spot_ref


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log_snap(log_fp, snap: Dict[str, Any]) -> None:
    log_fp.write(json.dumps(snap, ensure_ascii=False) + "\n")
    log_fp.flush()


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def run(run_seconds: int = 21600, poll_secs: float = POLL_DEFAULT) -> None:
    ts_str   = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir  = Path("logs") / f"multi_coin_observer_{ts_str}"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "observer.jsonl"

    print(f"[OBSERVER] Log: {log_path}")
    print(f"[OBSERVER] Mercados: {[f'{c}-{t}' for c, t in MARKETS_TO_OBSERVE]}")
    print(f"[OBSERVER] Run: {run_seconds}s | poll: {poll_secs}s")

    states   = {(c, t): _CoinState(c, t) for c, t in MARKETS_TO_OBSERVE}
    all_syms = list({BINANCE_SYMBOLS[c] for c, _ in MARKETS_TO_OBSERVE if c in BINANCE_SYMBOLS})

    deadline = time.time() + run_seconds
    with open(log_path, "w", encoding="utf-8") as log_fp:
        while time.time() < deadline:
            now = time.time()

            # Spot batch refresh
            any_needs_spot = any(s.needs_spot_refresh(now) for s in states.values())
            if any_needs_spot:
                spot_map = _fetch_all_spots(all_syms)
                for state in states.values():
                    sym = BINANCE_SYMBOLS.get(state.coin)
                    if sym and sym in spot_map:
                        state.update_spot(spot_map[sym])

            # Queue refresh
            for state in states.values():
                if state.needs_queue_refresh(now):
                    state.refresh_queue()

            # Coleta livros em paralelo
            tasks: List[Tuple[_CoinState, str, Dict]] = [
                (state, slot_name, item)
                for state in states.values()
                for slot_name, item in state.active_slots()
            ]
            if not tasks:
                time.sleep(poll_secs)
                continue

            def _fetch(args: Tuple[_CoinState, str, Dict]):
                state, slot_name, item = args
                return state, slot_name, item, _fetch_slot_snap(item)

            results = []
            with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
                for fut in as_completed({ex.submit(_fetch, t): t for t in tasks}):
                    try:
                        results.append(fut.result())
                    except Exception:
                        pass

            snap_ts = time.time()
            for state, slot_name, item, snap in results:
                if snap is None:
                    continue

                up_bid   = snap["up_bid"]
                down_bid = snap["down_bid"]
                slug     = item["slug"]

                # secs dinâmico: calculado do epoch do slug (não do cache de 45s)
                epoch = _extract_epoch_from_slug(slug)
                step  = 900 if state.timeframe == "15m" else 300
                secs  = round(epoch + step - snap_ts) if epoch else item.get("seconds_to_end")

                # Regista price_to_beat no início do candle
                state.register_price_to_beat(slug, secs)

                # Bid history + velocidade
                bh = state.bid_hist.setdefault(slug, _BidHistory())
                bh.push(snap_ts, up_bid, down_bid)

                # Leader corrente
                if up_bid > down_bid:
                    leader, leader_bid = "UP",   up_bid
                elif down_bid > up_bid:
                    leader, leader_bid = "DOWN", down_bid
                else:
                    leader, leader_bid = None,   max(up_bid, down_bid)

                bid_vel = bh.velocity(leader or "UP", window_secs=30.0) if leader else 0.0

                # EL Inversion tracker
                elt = state.el_tracker.setdefault(slug, _ELInversionTracker())
                elt.update(slug, secs, up_bid, down_bid)

                # Contexto spot
                beat = state.price_to_beat.get(slug)
                sh   = state.spot_hist
                dist_beat   = sh.distance_beat_bps(beat)
                recent_vol  = sh.recent_vol_bps(window_secs=60.0)
                beat_crosses = sh.beat_crosses_60s(beat, window_secs=60.0)

                record: Dict[str, Any] = {
                    "type":    "multi_snap",
                    "ts":      round(snap_ts, 3),
                    "coin":    state.coin,
                    "tf":      state.timeframe,
                    "slot":    slot_name,
                    "slug":    slug,
                    "secs":    secs,
                    # livro
                    "up_bid":      round(up_bid, 4),
                    "down_bid":    round(down_bid, 4),
                    "leader":      leader,
                    "leader_bid":  round(leader_bid, 4) if leader_bid else 0.0,
                    "bid_vel_30s": round(bid_vel, 5),
                    # early leader
                    "el_leader":   elt.el_leader,
                    "el_bid_240":  elt.el_bid_240,
                    # inversão
                    "inv":         elt.inv,
                    "inv_new_leader": elt.inv_new_leader,
                    "inv_bid":     elt.inv_bid,
                    "flip_gap":    elt.flip_gap,
                    # spot context
                    "spot_ref":       state.spot_ref,
                    "price_to_beat":  beat,
                    "dist_beat_bps":  dist_beat,
                    "recent_vol_bps": recent_vol,
                    "beat_crosses_60s": beat_crosses,
                }
                _log_snap(log_fp, record)

                # Display
                inv_s = f"  *** INV {elt.inv_new_leader}@{elt.inv_bid:.3f} gap={elt.flip_gap:.3f}" if elt.inv else ""
                el_s  = f" EL={elt.el_leader}" if elt.el_leader else ""
                vel_s = f" vel={bid_vel:+.4f}" if abs(bid_vel) > 0.0001 else ""
                print(
                    f"  {state.coin.upper():3s}-{state.timeframe:3s}"
                    f" {slot_name:7s}  secs={str(secs or '?'):>5}"
                    f"  up={up_bid:.3f} dn={down_bid:.3f}"
                    f"{el_s}{vel_s}{inv_s}"
                )

            time.sleep(poll_secs)

    print(f"[OBSERVER] Concluído. Log: {log_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-coin observer + EL Inversion")
    parser.add_argument("--seconds", type=int,   default=21600,       help="Duração (s)")
    parser.add_argument("--poll",    type=float, default=POLL_DEFAULT, help="Poll interval (s)")
    args = parser.parse_args()
    run(run_seconds=args.seconds, poll_secs=args.poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())
