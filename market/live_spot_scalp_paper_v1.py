"""
market/live_spot_scalp_paper_v1.py

Spot Scalp Paper — Fase 1: zero ordens reais.

Estratégia: explorar o lag entre o preço spot do BTC (via Binance) e o preço
do token na Polymarket. A Polymarket usa Chainlink como oráculo, que atualiza
com latência. Quando o spot se move mas o token ainda não acompanhou, há
janela de entrada na direção do spot.

Lógica:
  1. Monitora secs_to_end em [45, 240]
  2. Calcula momentum spot BTC nos últimos 15s (via Binance WebSocket)
  3. Compara com variação do token price no mesmo período
  4. Se spot moveu >= 5bps mas token lag >= 3bps → would_enter na direção do spot
  5. Exit em 30s ou quando token fecha o gap

Importante: o oráculo Chainlink que a Polymarket usa para resolução é diferente
do Binance spot. O Chainlink agrega múltiplas fontes e pode ter latência de
15-30 segundos. O Binance é mais rápido e pode prever a direção Chainlink.

Loga em logs/spot_scalp_paper_YYYYMMDD.jsonl. Nenhuma ordem real.
"""
from __future__ import annotations

import json
import time
from collections import deque
from datetime import date
from pathlib import Path
from typing import Dict, Deque, Optional, Tuple
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from market.binance_ws_v1 import BinanceTickFeed
from market.chainlink_oracle import ChainlinkBTCOracle
from market.rest_5m_shadow_public_v4 import (
    _build_slot_bundle,
    _compute_executable_metrics,
    _fetch_slot_state,
    _slot_snapshot,
)

CONFIG_PATH = Path(__file__).parent.parent / "agent" / "config.json"

def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f).get("spot_scalp", {})
    except Exception:
        return {}

def _log_path() -> Path:
    today = date.today().strftime("%Y%m%d")
    return Path("logs") / f"spot_scalp_paper_{today}.jsonl"

def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


class _SpotHistory:
    """Rastreia histórico de preço spot e preço do token para calcular lag."""

    def __init__(self, window_secs: float = 15.0):
        self._window = window_secs
        self._spot: Deque[Tuple[float, float]] = deque()   # (ts, price)
        self._token: Dict[str, Deque[Tuple[float, float]]] = {}  # side → (ts, bid)

    def push_spot(self, ts: float, price: float) -> None:
        self._spot.append((ts, price))
        cutoff = ts - self._window * 2
        while self._spot and self._spot[0][0] < cutoff:
            self._spot.popleft()

    def push_token(self, slug: str, side: str, ts: float, bid: float) -> None:
        key = f"{slug}:{side}"
        if key not in self._token:
            self._token[key] = deque()
        self._token[key].append((ts, bid))
        cutoff = ts - self._window * 2
        while self._token[key] and self._token[key][0][0] < cutoff:
            self._token[key].popleft()

    def spot_momentum_bps(self, now: float) -> Optional[float]:
        """Variação % do spot nos últimos window_secs, em bps."""
        cutoff = now - self._window
        old = [p for ts, p in self._spot if ts <= cutoff]
        new = [p for ts, p in self._spot if ts > cutoff]
        if not old or not new:
            return None
        ref = old[-1]
        cur = new[-1]
        if ref <= 0:
            return None
        return round((cur - ref) / ref * 10000, 2)  # bps

    def token_momentum_bps(self, slug: str, side: str, now: float) -> Optional[float]:
        """Variação do token bid nos últimos window_secs, em bps do token."""
        key = f"{slug}:{side}"
        hist = self._token.get(key)
        if not hist:
            return None
        cutoff = now - self._window
        old = [b for ts, b in hist if ts <= cutoff]
        new = [b for ts, b in hist if ts > cutoff]
        if not old or not new:
            return None
        ref = old[-1]
        cur = new[-1]
        if ref <= 0:
            return None
        return round((cur - ref) / ref * 10000, 2)

    def lag_bps(self, slug: str, side: str, now: float) -> Optional[float]:
        """
        Lag = spot_momentum - token_momentum (na mesma direção).
        Positivo = spot subiu mais do que o token (token está atrasado para cima).
        Negativo = spot caiu mais do que o token (token está atrasado para baixo).
        """
        spot_mom = self.spot_momentum_bps(now)
        token_mom = self.token_momentum_bps(slug, side, now)
        if spot_mom is None or token_mom is None:
            return None
        return round(spot_mom - token_mom, 2)

    def evict_old_slugs(self, current_slug: str) -> None:
        stale = [k for k in self._token if not k.startswith(current_slug + ":")]
        for k in stale:
            del self._token[k]


class _ScalpPosition:
    __slots__ = ("slug", "side", "entry_bid", "entry_ts", "entry_secs",
                 "entry_spot", "entry_lag_bps", "target_bid", "stop_bid",
                 "max_bid", "current_bid")

    def __init__(self, slug: str, side: str, entry_bid: float, entry_ts: float,
                 entry_secs: int, entry_spot: float, entry_lag_bps: float,
                 target_ticks: int = 2, stop_ticks: int = 3):
        self.slug = slug
        self.side = side
        self.entry_bid = entry_bid
        self.entry_ts = entry_ts
        self.entry_secs = entry_secs
        self.entry_spot = entry_spot
        self.entry_lag_bps = entry_lag_bps
        self.target_bid = round(entry_bid + target_ticks * 0.01, 4)
        self.stop_bid = round(entry_bid - stop_ticks * 0.01, 4)
        self.max_bid = entry_bid
        self.current_bid = entry_bid

    def update(self, bid: float) -> None:
        self.current_bid = bid
        self.max_bid = max(self.max_bid, bid)

    def pnl(self, exit_bid: float, bet_usd: float) -> float:
        shares = bet_usd / self.entry_bid
        return round((exit_bid - self.entry_bid) * shares, 4)


def run_spot_scalp_paper(run_for: float = float("inf"), poll_secs: float = 1.0) -> None:
    log_path = _log_path()
    print(f"[SPOT_SCALP] Iniciando — modo PAPER ONLY", flush=True)
    print(f"[SPOT_SCALP] Log: {log_path}", flush=True)

    cfg = _load_config()
    MIN_SPOT_MOM_BPS   = float(cfg.get("min_spot_momentum_bps", 5.0))
    MOMENTUM_WINDOW    = float(cfg.get("momentum_window_secs", 15.0))
    MIN_SECS           = int(cfg.get("min_secs_to_end", 45))
    MAX_SECS           = int(cfg.get("max_secs_to_end", 240))
    TOKEN_LAG_BPS      = float(cfg.get("token_lag_threshold_bps", 3.0))
    MAX_HOLD_SECS      = int(cfg.get("max_hold_secs", 30))
    BET_USD            = float(cfg.get("paper_bet_size", 20))

    _append(log_path, {
        "type": "session_start",
        "ts": time.time(),
        "params": {
            "min_spot_momentum_bps": MIN_SPOT_MOM_BPS,
            "momentum_window_secs": MOMENTUM_WINDOW,
            "min_secs_to_end": MIN_SECS,
            "max_secs_to_end": MAX_SECS,
            "token_lag_threshold_bps": TOKEN_LAG_BPS,
            "max_hold_secs": MAX_HOLD_SECS,
            "paper_bet_usd": BET_USD,
            "oracle_source": "chainlink+binance",
        },
    })

    btc_feed = BinanceTickFeed()
    btc_feed.start()
    oracle = ChainlinkBTCOracle(cache_secs=5.0)
    hist = _SpotHistory(window_secs=MOMENTUM_WINDOW)

    position: Optional[_ScalpPosition] = None
    started_at = time.time()
    stats = {"would_enter": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    cooldowns: Dict[str, float] = {}

    while time.time() - started_at < run_for:
        now = time.time()
        cfg = _load_config()
        BET_USD          = float(cfg.get("paper_bet_size", 20))
        MIN_SPOT_MOM_BPS = float(cfg.get("min_spot_momentum_bps", 5.0))
        TOKEN_LAG_BPS    = float(cfg.get("token_lag_threshold_bps", 3.0))

        try:
            # Atualiza histórico spot
            spot_price = btc_feed.current_price()
            if spot_price:
                hist.push_spot(now, spot_price)

            slot_bundle = _build_slot_bundle()
            current_item = slot_bundle["queue"].get("current")
            if not current_item:
                time.sleep(poll_secs)
                continue

            slug = str(current_item.get("slug") or "")
            if not slug:
                time.sleep(poll_secs)
                continue

            raw_secs = current_item.get("seconds_to_end")
            current_secs = int(raw_secs) if raw_secs is not None else None

            hist.evict_old_slugs(slug)

            # Liquidar posição se mercado mudou, próximo da resolução ou max_hold
            if position and (position.slug != slug or
                             (current_secs is not None and current_secs <= MIN_SECS) or
                             (now - position.entry_ts >= MAX_HOLD_SECS)):
                reason = "market_changed" if position.slug != slug else \
                         "near_resolution" if (current_secs and current_secs <= MIN_SECS) else \
                         "max_hold_expired"
                exit_bid = position.current_bid
                exit_spot = btc_feed.current_price() or position.entry_spot
                pnl = position.pnl(exit_bid, BET_USD)
                stats["pnl"] += pnl
                outcome = "win" if pnl > 0 else "loss"
                stats[outcome + "s"] += 1
                _append(log_path, {
                    "type": "simulated_exit",
                    "ts": now,
                    "slug": position.slug,
                    "reason": reason,
                    "side": position.side,
                    "entry_bid": position.entry_bid,
                    "exit_bid": exit_bid,
                    "max_bid": position.max_bid,
                    "entry_spot": position.entry_spot,
                    "exit_spot": exit_spot,
                    "spot_delta": round(exit_spot - position.entry_spot, 2) if exit_spot else None,
                    "entry_secs": position.entry_secs,
                    "exit_secs": current_secs,
                    "hold_secs": round(now - position.entry_ts, 1),
                    "entry_lag_bps": position.entry_lag_bps,
                    "pnl": pnl,
                    "bet_usd": BET_USD,
                    "outcome": outcome,
                    "running_pnl": round(stats["pnl"], 4),
                })
                print(f"  EXIT {outcome.upper():4s} | {position.slug[-20:]} | "
                      f"entry={position.entry_bid:.3f} exit={exit_bid:.3f} pnl={pnl:+.4f} | {reason}", flush=True)
                position = None

            if current_secs is None or not (MIN_SECS <= current_secs <= MAX_SECS):
                time.sleep(poll_secs)
                continue

            # Lê preços do token
            slot_state = _fetch_slot_state(slot_bundle)
            snap = _slot_snapshot(slot_state, "current")
            metrics, _status = _compute_executable_metrics(snap)
            if metrics is None:
                time.sleep(poll_secs)
                continue

            up_bid   = float(metrics.get("up_bid", 0) or 0)
            down_bid = float(metrics.get("down_bid", 0) or 0)

            # Atualiza histórico de tokens
            hist.push_token(slug, "UP", now, up_bid)
            hist.push_token(slug, "DOWN", now, down_bid)

            # Calcula lag para cada lado
            spot_mom    = hist.spot_momentum_bps(now)
            lag_up      = hist.lag_bps(slug, "UP", now)
            lag_down    = hist.lag_bps(slug, "DOWN", now)
            chainlink_price = oracle.fetch() if spot_price else None

            snap_row = {
                "type": "scalp_snapshot",
                "ts": now,
                "slug": slug,
                "current_secs": current_secs,
                "spot_price": spot_price,
                "chainlink_price": chainlink_price,
                "spot_momentum_bps": spot_mom,
                "up_bid": round(up_bid, 4),
                "down_bid": round(down_bid, 4),
                "lag_up_bps": lag_up,
                "lag_down_bps": lag_down,
                "has_position": position is not None,
            }
            _append(log_path, snap_row)

            # Atualiza posição existente e checa target/stop
            if position and position.slug == slug:
                side_bid = up_bid if position.side == "UP" else down_bid
                position.update(side_bid)
                exit_reason = None
                if side_bid >= position.target_bid:
                    exit_reason = "target"
                elif side_bid <= position.stop_bid:
                    exit_reason = "stop"
                if exit_reason:
                    exit_spot = btc_feed.current_price() or position.entry_spot
                    pnl = position.pnl(side_bid, BET_USD)
                    stats["pnl"] += pnl
                    outcome = "win" if pnl > 0 else "loss"
                    stats[outcome + "s"] += 1
                    _append(log_path, {
                        "type": "simulated_exit",
                        "ts": now,
                        "slug": slug,
                        "reason": exit_reason,
                        "side": position.side,
                        "entry_bid": position.entry_bid,
                        "exit_bid": side_bid,
                        "max_bid": position.max_bid,
                        "entry_spot": position.entry_spot,
                        "exit_spot": exit_spot,
                        "entry_secs": position.entry_secs,
                        "exit_secs": current_secs,
                        "hold_secs": round(now - position.entry_ts, 1),
                        "entry_lag_bps": position.entry_lag_bps,
                        "pnl": pnl,
                        "bet_usd": BET_USD,
                        "outcome": outcome,
                        "running_pnl": round(stats["pnl"], 4),
                    })
                    print(f"  EXIT {outcome.upper():4s} | {slug[-20:]} | "
                          f"entry={position.entry_bid:.3f} exit={side_bid:.3f} pnl={pnl:+.4f} | {exit_reason}", flush=True)
                    position = None

            # Tenta nova entrada
            if position is None and spot_mom is not None and abs(spot_mom) >= MIN_SPOT_MOM_BPS:
                if cooldowns.get(slug, 0) > now:
                    pass
                elif spot_mom > 0 and lag_up is not None and lag_up >= TOKEN_LAG_BPS:
                    # Spot subiu, UP token ainda não acompanhou → comprar UP
                    entry_side = "UP"
                    entry_bid = up_bid
                    entry_lag = lag_up
                elif spot_mom < 0 and lag_down is not None and lag_down <= -TOKEN_LAG_BPS:
                    # Spot caiu, DOWN token ainda não acompanhou → comprar DOWN
                    entry_side = "DOWN"
                    entry_bid = down_bid
                    entry_lag = abs(lag_down)
                else:
                    entry_side = None

                if entry_side and entry_bid > 0.30:
                    position = _ScalpPosition(
                        slug=slug, side=entry_side, entry_bid=entry_bid,
                        entry_ts=now, entry_secs=current_secs,
                        entry_spot=spot_price or 0.0, entry_lag_bps=entry_lag,
                    )
                    stats["would_enter"] += 1
                    cooldowns[slug] = now + 60.0
                    _append(log_path, {
                        "type": "would_enter",
                        "ts": now,
                        "slug": slug,
                        "side": entry_side,
                        "entry_bid": entry_bid,
                        "target_bid": position.target_bid,
                        "stop_bid": position.stop_bid,
                        "spot_momentum_bps": spot_mom,
                        "token_lag_bps": entry_lag,
                        "current_secs": current_secs,
                        "spot_price": spot_price,
                        "chainlink_price": chainlink_price,
                        "total_would_enter": stats["would_enter"],
                    })
                    print(f"  ENTER | {slug[-20:]} | side={entry_side} bid={entry_bid:.3f} "
                          f"spot_mom={spot_mom:+.1f}bps lag={entry_lag:.1f}bps secs={current_secs}", flush=True)

        except Exception as e:
            _append(log_path, {"type": "error", "ts": now, "error": str(e)})

        time.sleep(poll_secs)

    print(f"\n[SPOT_SCALP] Encerrado | would_enter={stats['would_enter']} "
          f"wins={stats['wins']} losses={stats['losses']} pnl={stats['pnl']:+.4f}", flush=True)
