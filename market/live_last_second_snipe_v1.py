"""
market/live_last_second_snipe_v1.py

Last-Second Snipe runner for BTC 5min Polymarket markets.

Strategy:
  - Sleep until T-10s of each 5-min window
  - Poll every 2s from T-10s to T-5s, computing 7-indicator composite
  - Spike detection: execute immediately if score shifts >= 1.5 between polls
  - T-5s deadline: execute with best signal seen, never skip window
  - Order: FAK market buy → GTC limit at $0.95 if no asks

Modes: safe | aggressive | degen (Requisito 8)
Dry run: full analysis + simulated execution + result logging (Requisito 11)
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from market.binance_ws_v1 import BinanceTickFeed, fetch_binance_price_at
from market.last_second_snipe_signal_v1 import (
    SnipeMarket,
    SnipeSignal,
    compute_snipe_signal,
    estimate_token_price,
    fetch_snipe_market,
    token_price_from_market,
)
from market.polymarket_broker_v3 import PolymarketBrokerV3
from market.polymarket_broker_v3 import BrokerOrderRequest


# ---------------------------------------------------------------------------
# Mode config (Requisito 8)
# ---------------------------------------------------------------------------

@dataclass
class SnipeMode:
    name: str
    min_confidence: float
    bet_fraction: float        # fraction of bankroll per trade (safe only)
    all_in: bool = False       # degen: entire bankroll
    profits_only: bool = False # aggressive: bet only accrued profit

    @classmethod
    def safe(cls) -> "SnipeMode":
        return cls(name="safe", min_confidence=0.30, bet_fraction=0.25)

    @classmethod
    def aggressive(cls) -> "SnipeMode":
        return cls(name="aggressive", min_confidence=0.20, bet_fraction=0.0, profits_only=True)

    @classmethod
    def degen(cls) -> "SnipeMode":
        return cls(name="degen", min_confidence=0.0, bet_fraction=1.0, all_in=True)

    @classmethod
    def from_name(cls, name: str) -> "SnipeMode":
        return {"safe": cls.safe, "aggressive": cls.aggressive, "degen": cls.degen}[name.lower()]()


def compute_bet_size(
    mode: SnipeMode,
    bankroll: float,
    starting_bankroll: float,
    min_bet: float = 1.0,
) -> float:
    if mode.all_in:
        return max(bankroll, min_bet)
    if mode.profits_only:
        profit = bankroll - starting_bankroll
        return max(profit, min_bet) if profit > min_bet else min_bet
    return max(bankroll * mode.bet_fraction, min_bet)


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class SnipeTrade:
    window_ts: int
    slug: str
    side: str
    signal_score: float
    confidence: float
    window_delta_pct: float
    token_price: float
    bet_size: float
    shares: float
    order_type: str          # "FAK" | "GTC" | "DRY_RUN"
    order_id: str = ""
    result: str = "pending"  # "win" | "loss" | "pending" | "cancelled"
    pnl_usd: float = 0.0
    resolve_price: float = 0.0
    ts: float = 0.0
    mode: str = "safe"
    dry_run: bool = False


def _append_log(log_path: Path, record: dict) -> None:
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Spike detection
# ---------------------------------------------------------------------------

_SPIKE_THRESHOLD = 1.5


def _detect_spike(prev_score: Optional[float], curr_score: float) -> bool:
    if prev_score is None:
        return False
    return abs(curr_score - prev_score) >= _SPIKE_THRESHOLD


# ---------------------------------------------------------------------------
# Market order execution (Requisito 7)
# ---------------------------------------------------------------------------

def _place_order(
    broker: PolymarketBrokerV3,
    market: SnipeMarket,
    side: str,
    bet_size: float,
    log_path: Path,
    dry_run: bool = False,
) -> SnipeTrade:
    """
    Attempt FAK market order; if no asks, fall back to GTC limit at $0.95.
    Returns a SnipeTrade record.
    """
    token_id = market.up_token_id if side == "UP" else market.down_token_id
    token_price = token_price_from_market(market, side)
    shares = round(bet_size / max(token_price, 0.01), 2)

    trade = SnipeTrade(
        window_ts=market.window_ts,
        slug=market.slug,
        side=side,
        signal_score=0.0,       # filled by caller
        confidence=0.0,
        window_delta_pct=0.0,
        token_price=token_price,
        bet_size=bet_size,
        shares=shares,
        order_type="DRY_RUN" if dry_run else "FAK",
        ts=time.time(),
    )

    if dry_run:
        print(f"[DRY-RUN] Would buy {shares:.2f} {side} shares @ ${token_price:.3f} = ${bet_size:.2f}")
        _append_log(log_path, {"type": "dry_run_order", "ts": trade.ts, **asdict(trade)})
        return trade

    # --- Attempt FAK market order ---
    try:
        order = broker.place_market_order(
            token_id=token_id,
            side="BUY",
            amount=bet_size,
            order_type="FAK",
            market_slug=market.slug,
            outcome=side,
        )
        trade.order_id = order.order_id
        trade.order_type = "FAK"
        matched = getattr(order, "size_matched", 0.0) or 0.0
        if matched > 0:
            trade.shares = matched
            print(f"[SNIPE] FAK filled: {matched:.2f} {side} @ ${token_price:.3f}")
            _append_log(log_path, {"type": "order_placed", "order_type": "FAK", **asdict(trade)})
            return trade
        print(f"[SNIPE] FAK unfilled (no asks) — falling back to GTC @ $0.95")
    except Exception as exc:
        print(f"[SNIPE] FAK error: {exc!r} — falling back to GTC")

    # --- Fallback: GTC limit at $0.95 ---
    gtc_price = 0.95
    gtc_shares = max(round(bet_size / gtc_price, 2), 5.0)  # min 5 shares
    try:
        req = BrokerOrderRequest(
            token_id=token_id,
            side="BUY",
            price=gtc_price,
            size=gtc_shares,
            order_type="GTC",
            market_slug=market.slug,
            outcome=side,
        )
        order = broker.place_limit_order(req)
        trade.order_id = order.order_id
        trade.order_type = "GTC"
        trade.token_price = gtc_price
        trade.shares = gtc_shares
        print(f"[SNIPE] GTC posted: {gtc_shares:.2f} {side} @ $0.95 — order {order.order_id}")
        _append_log(log_path, {"type": "order_placed", "order_type": "GTC", **asdict(trade)})
    except Exception as exc:
        print(f"[SNIPE] GTC error: {exc!r}")
        trade.result = "cancelled"
        trade.order_type = "FAILED"
    return trade


# ---------------------------------------------------------------------------
# Resolution check (Requisito 10)
# ---------------------------------------------------------------------------

def _check_resolution(
    tick_feed: BinanceTickFeed,
    market: SnipeMarket,
    timeout_secs: float = 60.0,
) -> str:
    """
    Wait until close_time + buffer, then determine outcome from Binance price.
    Returns "UP" or "DOWN".
    """
    deadline = market.close_time + 10
    now = time.time()
    if now < deadline:
        time.sleep(deadline - now)

    # Primary: compare window open price vs price at close
    close_price = tick_feed.current_price()
    open_price = fetch_binance_price_at(market.window_ts)

    if open_price and close_price:
        return "UP" if close_price >= open_price else "DOWN"

    # Fallback: current tick direction
    return "UP" if tick_feed.tick_direction_score(10) >= 0 else "DOWN"


# ---------------------------------------------------------------------------
# Main snipe loop — one window (Requisito 2, 4, 5)
# ---------------------------------------------------------------------------

def snipe_one_window(
    tick_feed: BinanceTickFeed,
    broker: Optional[PolymarketBrokerV3],
    mode: SnipeMode,
    bankroll: float,
    starting_bankroll: float,
    log_path: Path,
    *,
    dry_run: bool = False,
    min_bet: float = 1.0,
) -> Optional[SnipeTrade]:
    """
    Execute the full snipe cycle for the current 5-min window.
    Returns the SnipeTrade if an order was placed, else None.
    """
    now = time.time()
    wts = int(now) - (int(now) % 300)
    close_time = wts + 300

    # ---- Sleep until T-10s (Requisito 2) ----
    sleep_until = close_time - 10
    wait = sleep_until - time.time()
    if wait > 0:
        print(f"[SNIPE] Window {wts} — sleeping {wait:.1f}s until T-10s")
        time.sleep(wait)

    # ---- Fetch market data ----
    market = fetch_snipe_market(wts)
    if market is None:
        print(f"[SNIPE] Market not found for {wts} — skipping")
        return None
    if not market.active:
        print(f"[SNIPE] Market inactive for {wts} — skipping")
        return None

    # ---- Fetch window open price ----
    window_open_price = fetch_binance_price_at(wts)
    if not window_open_price:
        window_open_price = tick_feed.current_price()
    print(f"[SNIPE] Window open: ${window_open_price:,.2f}  current: ${tick_feed.current_price():,.2f}")

    # ---- Snipe loop: T-10s to T-5s (Requisito 5) ----
    best_signal: Optional[SnipeSignal] = None
    prev_score: Optional[float] = None
    execute_signal: Optional[SnipeSignal] = None
    execute_reason = ""

    while True:
        remaining = close_time - time.time()
        if remaining <= 5.0:
            # T-5s deadline — use best seen
            execute_signal = best_signal
            execute_reason = "deadline"
            break

        sig = compute_snipe_signal(tick_feed, window_open_price)
        print(
            f"[SNIPE] T-{remaining:.1f}s  score={sig.score:+.2f}  conf={sig.confidence:.0%}"
            f"  Δ={sig.window_delta_pct:+.4f}%  side={sig.side}  tick={sig.tick_trend:+.2f}"
        )

        # Track best signal (by absolute score)
        if best_signal is None or abs(sig.score) > abs(best_signal.score):
            best_signal = sig

        # Spike detection — execute immediately
        if _detect_spike(prev_score, sig.score):
            execute_signal = sig
            execute_reason = f"spike(Δ={sig.score - prev_score:+.2f})"
            print(f"[SNIPE] SPIKE DETECTED — executing immediately")
            break

        # Confidence threshold met
        if sig.confidence >= mode.min_confidence:
            execute_signal = sig
            execute_reason = f"conf={sig.confidence:.0%}>={mode.min_confidence:.0%}"
            break

        prev_score = sig.score
        time.sleep(2.0)

    if execute_signal is None:
        execute_signal = best_signal

    if execute_signal is None:
        print(f"[SNIPE] No signal — skipping window")
        return None

    print(
        f"[SNIPE] EXECUTE ({execute_reason})  {execute_signal.side}"
        f"  score={execute_signal.score:+.2f}  conf={execute_signal.confidence:.0%}"
    )

    # ---- Bet sizing ----
    bet_size = compute_bet_size(mode, bankroll, starting_bankroll, min_bet)

    # ---- Place order ----
    trade = _place_order(
        broker=broker,
        market=market,
        side=execute_signal.side,
        bet_size=bet_size,
        log_path=log_path,
        dry_run=dry_run,
    )
    trade.signal_score = execute_signal.score
    trade.confidence = execute_signal.confidence
    trade.window_delta_pct = execute_signal.window_delta_pct
    trade.mode = mode.name
    trade.dry_run = dry_run

    return trade


# ---------------------------------------------------------------------------
# Continuous runner (Requisito 2 — loop across windows)
# ---------------------------------------------------------------------------

def run_last_second_snipe(
    broker: Optional[PolymarketBrokerV3],
    mode: SnipeMode,
    starting_bankroll: float,
    *,
    dry_run: bool = False,
    min_bet: float = 1.0,
    max_windows: int = 0,
    log_dir: str = "logs",
) -> None:
    """
    Main continuous runner. Iterates across 5-min windows indefinitely
    (or until max_windows if set).

    Parameters
    ----------
    broker : PolymarketBrokerV3 | None
        None is accepted only in dry_run mode.
    mode : SnipeMode
        Trading mode (safe/aggressive/degen).
    starting_bankroll : float
        Initial capital in USDC.
    dry_run : bool
        Simulate without placing real orders.
    min_bet : float
        Minimum bet size in USDC.
    max_windows : int
        0 = run forever.
    log_dir : str
        Directory for JSONL log files.
    """
    import os
    from datetime import datetime

    if not dry_run and broker is None:
        raise ValueError("broker required for live trading")

    log_path = Path(log_dir) / f"snipe_{mode.name}{'_dry' if dry_run else ''}_{int(time.time())}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # ---- Start Binance WebSocket ----
    tick_feed = BinanceTickFeed()
    tick_feed.start()
    print("[SNIPE] Waiting for Binance WebSocket connection...")
    if not tick_feed.wait_for_first_tick(timeout=15):
        print("[SNIPE] WARNING: Binance WS not connected — proceeding anyway")

    bankroll = starting_bankroll
    trades_done = 0
    wins = 0
    losses = 0

    _append_log(log_path, {
        "type": "session_start",
        "ts": time.time(),
        "mode": mode.name,
        "dry_run": dry_run,
        "starting_bankroll": starting_bankroll,
    })

    print(f"\n{'='*60}")
    print(f"  LAST-SECOND SNIPE  mode={mode.name}  {'DRY-RUN' if dry_run else 'LIVE'}")
    print(f"  bankroll=${bankroll:.2f}  log={log_path}")
    print(f"{'='*60}\n")

    try:
        while True:
            if max_windows > 0 and trades_done >= max_windows:
                break

            trade = snipe_one_window(
                tick_feed=tick_feed,
                broker=broker,
                mode=mode,
                bankroll=bankroll,
                starting_bankroll=starting_bankroll,
                log_path=log_path,
                dry_run=dry_run,
                min_bet=min_bet,
            )

            if trade is None:
                # No trade placed — wait for next window
                now = time.time()
                next_window = (int(now) - (int(now) % 300)) + 300
                wait = next_window - now
                if wait > 0:
                    time.sleep(min(wait, 10))
                continue

            trades_done += 1

            # ---- Wait for resolution and record outcome ----
            if not dry_run:
                now = time.time()
                market_close = trade.window_ts + 300
                if time.time() < market_close + 5:
                    time.sleep(market_close + 5 - time.time())

                # Determine outcome
                open_p = fetch_binance_price_at(trade.window_ts)
                close_p = tick_feed.current_price()
                if open_p and close_p:
                    result = "UP" if close_p >= open_p else "DOWN"
                    trade.result = "win" if result == trade.side else "loss"
                    if trade.result == "win":
                        pnl = trade.shares * (1.0 - trade.token_price)
                        trade.pnl_usd = pnl
                        bankroll += pnl
                        wins += 1
                    else:
                        trade.pnl_usd = -trade.bet_size
                        bankroll -= trade.bet_size
                        losses += 1
                    trade.resolve_price = close_p or 0.0
            else:
                # Dry run: resolve from tick feed
                market_close = trade.window_ts + 300
                wait = market_close + 5 - time.time()
                if wait > 0:
                    print(f"[DRY-RUN] Waiting {wait:.0f}s for resolution...")
                    time.sleep(max(wait, 0))
                open_p = fetch_binance_price_at(trade.window_ts)
                close_p = tick_feed.current_price()
                if open_p and close_p:
                    result = "UP" if close_p >= open_p else "DOWN"
                    trade.result = "win" if result == trade.side else "loss"
                    sim_price = estimate_token_price(abs(trade.window_delta_pct))
                    if trade.result == "win":
                        pnl = trade.shares * (1.0 - sim_price)
                        trade.pnl_usd = round(pnl, 4)
                        bankroll += pnl
                        wins += 1
                    else:
                        trade.pnl_usd = -trade.bet_size
                        bankroll = max(bankroll - trade.bet_size, 0.0)
                        losses += 1
                    trade.resolve_price = close_p or 0.0

            _append_log(log_path, {"type": "trade_result", **asdict(trade)})

            total = wins + losses
            wr = wins / total * 100 if total > 0 else 0
            print(
                f"[SNIPE] {trade.result.upper()}  PnL={trade.pnl_usd:+.2f}  "
                f"bankroll=${bankroll:.2f}  WR={wr:.0f}% ({wins}W/{losses}L)"
            )

            # Safety: stop if bankroll too low (dry run resets)
            if bankroll < min_bet:
                if dry_run:
                    print(f"[DRY-RUN] Bankroll depleted — resetting to ${starting_bankroll:.2f}")
                    bankroll = starting_bankroll
                else:
                    print(f"[SNIPE] Bankroll ${bankroll:.2f} below min ${min_bet:.2f} — stopping")
                    break

            # ---- Wait for start of next window ----
            now = time.time()
            next_window = (trade.window_ts + 300) - (int(time.time() - trade.window_ts - 300) % 300)
            next_window = int(time.time()) - (int(time.time()) % 300) + 300
            wait = next_window - time.time()
            if wait > 5:
                time.sleep(wait - 5)  # wake up 5s early to fetch market data

    except KeyboardInterrupt:
        print("\n[SNIPE] Stopped by user")
    finally:
        tick_feed.stop()
        _append_log(log_path, {
            "type": "session_end",
            "ts": time.time(),
            "final_bankroll": bankroll,
            "trades": trades_done,
            "wins": wins,
            "losses": losses,
        })
        print(f"\n[SNIPE] Session ended — {trades_done} trades, bankroll=${bankroll:.2f}")
