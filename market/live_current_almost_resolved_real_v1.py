from __future__ import annotations

import json
import os
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from market.book_5m import fetch_books_for_tokens
from market.broker_env import load_broker_env
from market.btc_chart_context_v1 import BtcChartContext, fetch_btc_chart_context
from market.broker_types import BrokerOrderRequest
from market.chainlink_oracle import ChainlinkBTCOracle
from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, evaluate_current_almost_resolved_v1
from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.live_guarded_config import load_live_guarded_config
from market.polymarket_broker_v3 import PolymarketBrokerV3
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _compute_executable_metrics, _fetch_slot_state, _slot_snapshot
from market.slug_discovery import fetch_event_by_slug


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _exception_message(exc: Exception) -> str:
    for attr in ("error_msg", "msg"):
        value = getattr(exc, attr, None)
        if value:
            return str(value)
    text = str(exc)
    return text if text else repr(exc)


def _is_fak_no_match_exception(exc: Exception) -> bool:
    text = _exception_message(exc).lower()
    return "no orders found to match with fak order" in text


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Parâmetros Early Entry (EE) v2
# ---------------------------------------------------------------------------
EE_EL_MIN              = 0.55
EE_CONT_MIN            = 0.70
EE_VEL_MIN             = 0.13
EE_ENTRY_LO            = 0.82
EE_ENTRY_HI            = 0.86
EE_MAX_ENTRY_SECS      = 180
EE_MIN_ENTRY_SECS      = 30
EE_STOP_LEVEL          = 0.65
EE_PROFIT_PROTECT_BID  = 0.88
EE_PROFIT_PROTECT_SECS = 70
EE_HEDGE_THR           = 0.50


def _build_log_dir() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"current_almost_resolved_real_{ts}"


def _state_path() -> Path:
    return Path("logs") / "current_almost_resolved_real_state.json"


def _counter_reversal_state_path() -> Path:
    return Path("logs") / "counter_reversal_real_state.json"


def _state_file_active(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return str(payload.get("mode") or "idle") != "idle"


def _save_state(path: Path, trade) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(trade), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state(path: Path):
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return LiveCurrentAlmostResolvedTradeState(**payload)
    except Exception:
        return None


def _clear_state(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


@dataclass
class LiveCurrentAlmostResolvedTradeState:
    mode: str = "idle"  # idle | pending_entry | open_position | pending_exit | exit_pending_confirm | awaiting_redeem
    event_slug: Optional[str] = None
    side: Optional[str] = None
    token_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    entry_order_style: str = "unknown"  # patient_limit | passive_limit | aggressive_limit | direct_limit | unknown
    entry_order_type: str = "GTC"
    entry_initial_size_matched: float = 0.0
    setup_variant: Optional[str] = None
    entry_price: Optional[float] = None
    entry_qty_requested: float = 0.0
    entry_qty_filled: float = 0.0
    exit_qty_filled: float = 0.0
    exit_price_posted: Optional[float] = None
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    best_bid: Optional[float] = None
    hold_to_resolution: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    confirm_started_at: float = 0.0
    confirm_polls: int = 0
    resolution_detected_at: float = 0.0
    redeem_attempted_at: float = 0.0
    redeem_required: bool = False
    last_reason: Optional[str] = None
    hedge_token_id: Optional[str] = None
    hedge_order_id: Optional[str] = None
    hedge_price: float = 0.0
    hedge_qty_filled: float = 0.0

    @property
    def remaining_position_qty(self) -> float:
        return round(max(0.0, float(self.entry_qty_filled) - float(self.exit_qty_filled)), 6)


def _trade_summary(trade: LiveCurrentAlmostResolvedTradeState) -> dict:
    return asdict(trade)


def _tick_size_from_snap(snap: dict, side: str) -> float:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(side_book.get("tick_size"), 0.01))


def _token_id_for_side(snap: dict, side: str) -> str:
    if side == "UP":
        side_book = snap.get("up") or {}
    elif side == "DOWN":
        side_book = snap.get("down") or {}
    else:
        return ""
    return str(side_book.get("token_id") or "")


def _bid_for_side(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _ask_for_side(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_ask" if side == "UP" else "down_ask"), 0.0)


def _side_winning(side: str, signed_distance_from_open_bps: float) -> bool:
    return (side == "UP" and signed_distance_from_open_bps > 0) or (side == "DOWN" and signed_distance_from_open_bps < 0)


def _fetch_active_book(trade: LiveCurrentAlmostResolvedTradeState) -> Optional[dict]:
    if not trade.token_id:
        return None
    raw_books = fetch_books_for_tokens([trade.token_id])
    if not raw_books:
        return None
    return raw_books[0]


def _best_bid(book: dict) -> float:
    bids = book.get("bids") or []
    if not bids:
        return 0.0
    return _safe_float((bids[0] or {}).get("price"), 0.0)


def _get_order_status(broker, order_id: Optional[str]):
    if not order_id:
        return None
    try:
        order = broker.get_order(order_id)
        if order is not None:
            return order
    except Exception:
        pass
    try:
        for order in broker.get_open_orders()[:50]:
            if order.order_id == order_id:
                return order
    except Exception:
        pass
    return None


def _cancel_if_live(broker, order_id: Optional[str]) -> Optional[dict]:
    if not order_id:
        return None
    order = _get_order_status(broker, order_id)
    status = str(getattr(order, "status", "") or "").lower()
    if status in ("filled", "canceled", "cancelled", "closed", "resolved", "rejected"):
        return None
    return broker.cancel_order(order_id)


def _cancel_open_orders_for_token(broker, token_id: str) -> list:
    """Cancela qualquer ordem GTC aberta no CLOB para este token_id.

    Evita preenchimentos duplos quando uma ordem anterior (de outra iteração ou
    restart do watchdog) ainda está ativa no livro no momento de uma nova entrada.
    Retorna lista de order_ids cancelados.
    """
    cancelled = []
    try:
        for order in broker.get_open_orders()[:50]:
            order_asset = str(getattr(order, "token_id", None) or "")
            if order_asset and order_asset == token_id:
                try:
                    broker.cancel_order(order.order_id)
                    cancelled.append(order.order_id)
                except Exception:
                    pass
    except Exception:
        pass
    return cancelled


def _token_balance_qty(broker, token_id: Optional[str]) -> float:
    if not token_id:
        return 0.0
    try:
        payload = broker.get_balance_allowance(asset_type="CONDITIONAL", token_id=token_id)
        raw_balance = float(payload.get("balance") or 0.0)
        return round(raw_balance / 1_000_000.0, 6)
    except Exception:
        return 0.0


def _collateral_balance_usd(broker) -> float:
    try:
        payload = broker.get_balance_allowance(asset_type="COLLATERAL")
        raw_balance = float(payload.get("balance") or 0.0)
        return round(raw_balance / 1_000_000.0, 6)
    except Exception:
        return 0.0


def _is_flat_qty(qty: float, epsilon: float = 0.000001) -> bool:
    return abs(float(qty)) <= float(epsilon)


class _EarlyLeaderTracker:
    """Rastreia o early leader (EL) por slug ao longo dos polls.

    Janelas:
      - secs 181-240: detecta qual lado lidera com bid >= early_min (EL)
      - secs 121-180: verifica se EL nunca caiu abaixo de cont_min (F3)
      - secs < 180:   detecta inversão (quando o líder troca de lado)

    Achados nos logs reais:
      EL baseline 78.6%  |  EL+F3 93.4%  |  EL>=0.72 inverte → novo lado 100% (19/19)
    """

    def __init__(self, early_min: float = 0.55, cont_min: float = 0.70, strong_min: float = 0.72):
        self._early_min = early_min
        self._cont_min = cont_min
        self._strong_min = strong_min
        self._by_slug: dict = {}

    def update(self, slug: str, secs: Optional[int], up_bid: float, down_bid: float) -> None:
        if secs is None or up_bid <= 0 or down_bid <= 0:
            return
        d = self._by_slug.setdefault(slug, {
            "early_side": None, "early_bid": 0.0, "early_secs": None,
            "f3_ok": None, "f3_started": False,
            "inverted": False, "inversion_side": None,
            "inversion_bid": 0.0, "inversion_secs": None,
            "_inversion_logged": False,
        })
        leader_bid = max(up_bid, down_bid)
        leader_side = "UP" if up_bid >= down_bid else "DOWN"

        if 181 <= secs <= 240 and d["early_side"] is None:
            if leader_bid >= self._early_min:
                d["early_side"] = leader_side
                d["early_bid"] = round(leader_bid, 4)
                d["early_secs"] = secs

        if 121 <= secs <= 180 and d["early_side"] is not None:
            el_bid_now = up_bid if d["early_side"] == "UP" else down_bid
            if not d["f3_started"]:
                d["f3_started"] = True
                d["f3_ok"] = True
            if el_bid_now < self._cont_min:
                d["f3_ok"] = False

        if secs < 180 and d["early_side"] is not None and not d["inverted"]:
            if leader_side != d["early_side"] and leader_bid >= self._early_min:
                d["inverted"] = True
                d["inversion_side"] = leader_side
                d["inversion_bid"] = round(leader_bid, 4)
                d["inversion_secs"] = secs

    def state(self, slug: str) -> dict:
        d = self._by_slug.get(slug) or {}
        inv_bid = d.get("inversion_bid", 0.0)
        return {
            "early_side": d.get("early_side"),
            "early_bid": d.get("early_bid", 0.0),
            "early_secs": d.get("early_secs"),
            "f3_ok": d.get("f3_ok"),
            "inverted": bool(d.get("inverted")),
            "inversion_side": d.get("inversion_side"),
            "inversion_bid": inv_bid,
            "inversion_secs": d.get("inversion_secs"),
            "inversion_strong": bool(d.get("inverted") and inv_bid >= self._strong_min),
        }

    def inversion_logged(self, slug: str) -> bool:
        return bool((self._by_slug.get(slug) or {}).get("_inversion_logged"))

    def mark_inversion_logged(self, slug: str) -> None:
        if slug in self._by_slug:
            self._by_slug[slug]["_inversion_logged"] = True

    def evict_old(self, current_slug: str) -> None:
        for slug in [k for k in self._by_slug if k != current_slug]:
            del self._by_slug[slug]


class _EarlyLeaderTrackerEE:
    """Rastreia EL por slug para a estratégia Early Entry — calcula el_vel e F3 (cont_ok).

    Janelas:
      secs 181-240: detecta qual lado lidera (early_leader, bid_240)
      secs 121-180: verifica continuidade (cont_ok) e calcula el_vel
    """

    def __init__(self) -> None:
        self._slug: Optional[str] = None
        self._s240: list = []
        self._s180: list = []
        self.early_leader: Optional[str] = None
        self.el_bid_240:   float = 0.0
        self.el_bid_180:   float = 0.0
        self.el_vel:       float = 0.0
        self.cont_ok:      bool  = False

    def update(self, slug: str, secs: Optional[int], up_bid: float, down_bid: float) -> None:
        if secs is None:
            return
        if self._slug != slug:
            self._reset(slug)
        snap = {"secs": secs, "up_bid": up_bid, "down_bid": down_bid}
        if 181 <= secs <= 240:
            self._s240.append(snap)
            self._compute_el()
        elif 121 <= secs <= 180:
            self._s180.append(snap)
            self._compute_f3()

    def _compute_el(self) -> None:
        if not self._s240:
            return
        avg_up = sum(s["up_bid"]   for s in self._s240) / len(self._s240)
        avg_dn = sum(s["down_bid"] for s in self._s240) / len(self._s240)
        if avg_up >= EE_EL_MIN:
            self.early_leader = "UP"
            self.el_bid_240   = round(avg_up, 4)
        elif avg_dn >= EE_EL_MIN:
            self.early_leader = "DOWN"
            self.el_bid_240   = round(avg_dn, 4)

    def _compute_f3(self) -> None:
        if not (self.early_leader and self._s180):
            return
        bids = [
            s["up_bid"] if self.early_leader == "UP" else s["down_bid"]
            for s in self._s180
        ]
        self.el_bid_180 = round(sum(bids) / len(bids), 4)
        self.el_vel     = round(self.el_bid_180 - self.el_bid_240, 4)
        self.cont_ok    = min(bids) >= EE_CONT_MIN

    def _reset(self, slug: str) -> None:
        self._slug        = slug
        self._s240        = []
        self._s180        = []
        self.early_leader = None
        self.el_bid_240   = 0.0
        self.el_bid_180   = 0.0
        self.el_vel       = 0.0
        self.cont_ok      = False

    @property
    def signal_ok(self) -> bool:
        return bool(self.early_leader and self.cont_ok and self.el_vel >= EE_VEL_MIN)

    def state_dict(self) -> dict:
        return {
            "early_side":  self.early_leader,
            "el_bid_240":  self.el_bid_240,
            "el_bid_180":  self.el_bid_180,
            "el_vel":      self.el_vel,
            "f3_ok":       self.cont_ok,
            "signal_ok":   self.signal_ok,
            "n_s240":      len(self._s240),
            "n_s180":      len(self._s180),
        }


def _is_match_status(status: Optional[str]) -> bool:
    return str(status or "").lower() in ("matched", "filled", "closed", "resolved")


def _clamp_limit_price(price: float, *, tick_size: float) -> float:
    tick = max(0.001, _safe_float(tick_size, 0.001))
    bounded = min(max(tick, _safe_float(price, tick)), 1.0 - tick)
    return round(bounded, 6)


def _side_book(snap: dict, side: str) -> dict:
    return (snap.get("up") if side == "UP" else snap.get("down")) or {}


def _bid_levels_for_exit(snap: dict, side: str) -> list:
    """Exit liquidity for a position in `side` comes from the counter side's asks.
    In Polymarket binary CLOBs, selling DOWN is matched by UP asks (and vice versa).
    Converts counter-ask prices to equivalent same-side bid prices (1 - counter_ask)."""
    counter = "UP" if side == "DOWN" else "DOWN"
    levels = (_side_book(snap, counter).get("top_asks") or [])
    result = []
    for level in levels:
        if isinstance(level, dict):
            p = _safe_float(level.get("price"), 0.0)
            s = _safe_float(level.get("size"), 0.0)
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            p = _safe_float(level[0], 0.0)
            s = _safe_float(level[1], 0.0)
        else:
            continue
        if p <= 0.0 or p >= 1.0 or s <= 0.0:
            continue
        result.append({"price": round(1.0 - p, 6), "size": s})
    result.sort(key=lambda lvl: lvl["price"], reverse=True)
    return result


def _exit_liquidity_risk(
    snap: dict,
    executable: Optional[dict],
    *,
    side: str,
    entry_price: float,
    stop_price: float,
    qty: float,
    tick_size: float,
) -> dict:
    side = side if side in ("UP", "DOWN") else "UP"
    entry_price = _safe_float(entry_price, 0.0)
    stop_price = _safe_float(stop_price, 0.0)
    qty = max(0.0, _safe_float(qty, 0.0))
    tick_size = max(0.001, _safe_float(tick_size, 0.01))
    best_bid = _bid_for_side(executable, side)
    levels = _bid_levels_for_exit(snap, side)

    qty_at_or_above_stop = 0.0
    qty_at_best_three = 0.0
    remaining = qty
    notional = 0.0
    worst_price_for_qty = 0.0
    observed_bid_depth = 0.0

    for idx, level in enumerate(levels):
        price = _safe_float(level.get("price"), 0.0)
        size = _safe_float(level.get("size"), 0.0)
        if price <= 0 or size <= 0:
            continue
        observed_bid_depth += size
        if price >= stop_price:
            qty_at_or_above_stop += size
        if idx < 3:
            qty_at_best_three += size
        if remaining > 0:
            take = min(remaining, size)
            notional += take * price
            remaining = round(remaining - take, 6)
            worst_price_for_qty = price

    if qty <= 0:
        vwap_exit_for_qty = best_bid
        enough_depth_for_qty = True
    elif remaining <= 0:
        vwap_exit_for_qty = round(notional / qty, 6)
        enough_depth_for_qty = True
    else:
        vwap_exit_for_qty = round(notional / max(0.000001, qty - remaining), 6) if notional > 0 else 0.0
        enough_depth_for_qty = False

    theoretical_stop_loss_ticks = (
        round(max(0.0, entry_price - stop_price) / tick_size, 4) if entry_price > 0 and stop_price > 0 else None
    )
    best_bid_loss_ticks = (
        round(max(0.0, entry_price - best_bid) / tick_size, 4) if entry_price > 0 and best_bid > 0 else None
    )
    pessimistic_exit_loss_ticks = (
        round(max(0.0, entry_price - vwap_exit_for_qty) / tick_size, 4)
        if entry_price > 0 and vwap_exit_for_qty > 0
        else None
    )
    worst_level_loss_ticks = (
        round(max(0.0, entry_price - worst_price_for_qty) / tick_size, 4)
        if entry_price > 0 and worst_price_for_qty > 0
        else None
    )

    return {
        "side": side,
        "entry_price": round(entry_price, 6) if entry_price > 0 else None,
        "stop_price": round(stop_price, 6) if stop_price > 0 else None,
        "qty": round(qty, 6),
        "best_bid": round(best_bid, 6) if best_bid > 0 else None,
        "bid_levels_seen": len(levels),
        "observed_bid_depth": round(observed_bid_depth, 6),
        "qty_at_or_above_stop": round(qty_at_or_above_stop, 6),
        "qty_at_best_three": round(qty_at_best_three, 6),
        "vwap_exit_for_qty": vwap_exit_for_qty if vwap_exit_for_qty > 0 else None,
        "worst_price_for_qty": round(worst_price_for_qty, 6) if worst_price_for_qty > 0 else None,
        "enough_depth_for_qty": bool(enough_depth_for_qty),
        "theoretical_stop_loss_ticks": theoretical_stop_loss_ticks,
        "best_bid_loss_ticks": best_bid_loss_ticks,
        "pessimistic_exit_loss_ticks": pessimistic_exit_loss_ticks,
        "worst_level_loss_ticks": worst_level_loss_ticks,
        "exit_depth_covers_stop": bool(qty <= 0 or qty_at_or_above_stop >= qty),
    }


def _should_await_platform_redeem(
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    current_secs: Optional[int],
    current_slug: Optional[str],
    hold_winner_to_resolution: bool,
) -> bool:
    if not hold_winner_to_resolution:
        return False
    if not trade.token_id or not trade.side:
        return False
    event_rolled = bool(current_slug and trade.event_slug and current_slug != trade.event_slug)
    settle_window = current_secs is not None and current_secs <= 1
    return event_rolled or settle_window


def _passive_entry_price_for_signal(signal: dict, *, bid_now: float, tick_size: float) -> float:
    signal_entry = _safe_float(signal.get("entry_price"), 0.0)
    tick = max(0.001, _safe_float(tick_size, 0.001))
    anchor = _safe_float(bid_now, 0.0) if _safe_float(bid_now, 0.0) > 0 else signal_entry
    return round(max(tick, anchor - tick), 6)


def _runaway_chase_params(
    signal: dict,
    trade: "LiveCurrentAlmostResolvedTradeState",
    *,
    current_secs: Optional[int],
    max_chase_price: float = 0.98,
    max_chase_distance: float = 0.04,
    min_secs: int = 25,
) -> Optional[dict]:
    """
    Returns chase parameters if the market ran in the correct direction while a
    passive entry was pending (signal now invalid because price moved away from
    the limit). Returns None if conditions aren't met.

    Conditions:
    - current buy price > original entry price + 1 tick (market moved right)
    - chase via ask price <= max_chase_price (at least 1 cent headroom to $1)
    - chase distance <= max_chase_distance (don't overpay for stale momentum)
    - enough time remaining (>= min_secs)
    - no missing midpoint context
    """
    side = str(trade.side or "")
    if side not in ("UP", "DOWN"):
        return None
    original_entry = _safe_float(trade.entry_price, 0.0)
    if original_entry <= 0:
        return None

    buy_key = "up_buy" if side == "UP" else "down_buy"
    ask_key = "up_sell" if side == "UP" else "down_sell"
    current_buy = _safe_float(signal.get(buy_key), 0.0)
    current_ask = _safe_float(signal.get(ask_key), 0.0) or current_buy + 0.01

    chase_distance = current_buy - original_entry
    if chase_distance < 0.01:
        return None
    if chase_distance > max_chase_distance:
        return None
    if current_ask <= 0 or current_ask > max_chase_price:
        return None
    if current_secs is None or current_secs < min_secs:
        return None
    if bool(signal.get("missing_market_midpoint_context")):
        return None

    return {"ask": round(current_ask, 6), "buy": round(current_buy, 6), "chase_distance": round(chase_distance, 4)}


def _safe_to_chase_aggressive_entry(
    signal: dict,
    *,
    ask_now: float,
    current_secs: Optional[int],
    max_price: float,
) -> bool:
    if ask_now <= 0 or ask_now > max_price:
        return False
    if current_secs is None or current_secs > 35:
        return False
    if str(signal.get("setup_variant") or "") != "passive_extreme_liquidity_capture":
        return False
    side = str(signal.get("side") or "").lower()
    if bool(signal.get("missing_market_midpoint_context")):
        return False
    if side and bool(signal.get(f"{side}_counter_alert")):
        return False
    return bool(signal.get("resolved_pullback_safe_distance_ok")) or bool(signal.get("passive_capture_safe_distance_ok"))


def _is_transient_service_not_ready_exception(exc: Exception) -> bool:
    message = _exception_message(exc).lower()
    return "service not ready" in message or "status_code=425" in message or "status code=425" in message


def _has_sufficient_collateral_for_entry(broker, *, entry_price: float, qty: float, buffer_usd: float = 0.25) -> bool:
    required = round(float(entry_price) * float(qty) + float(buffer_usd), 6)
    return _collateral_balance_usd(broker) >= required


def _sync_entry_order(broker, trade: LiveCurrentAlmostResolvedTradeState) -> LiveCurrentAlmostResolvedTradeState:
    order = _get_order_status(broker, trade.entry_order_id)
    if order is not None:
        trade.entry_qty_filled = max(trade.entry_qty_filled, _safe_float(getattr(order, "size_matched", None), 0.0))
    token_balance = _token_balance_qty(broker, trade.token_id)
    if token_balance > 0:
        trade.entry_qty_filled = max(trade.entry_qty_filled, token_balance + float(trade.exit_qty_filled))
    return trade


def _restore_trade_from_broker(broker, trade: LiveCurrentAlmostResolvedTradeState) -> LiveCurrentAlmostResolvedTradeState:
    if trade.mode in ("idle", "awaiting_redeem"):
        return trade
    trade = _sync_entry_order(broker, trade)
    exit_order = _get_order_status(broker, trade.exit_order_id)
    if exit_order is not None:
        trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
        status = str(getattr(exit_order, "status", "") or "").lower()
        if _is_flat_qty(_token_balance_qty(broker, trade.token_id)) or trade.remaining_position_qty <= 0:
            return LiveCurrentAlmostResolvedTradeState()
        trade.mode = "pending_exit"
    if trade.entry_qty_filled > 0 and trade.mode == "pending_entry":
        trade.mode = "open_position"
    return trade


def _should_hold_to_resolution(signal: dict, *, bid_now: float, secs_to_end: Optional[int], cfg: CurrentAlmostResolvedConfigV1, side: str) -> bool:
    buffer_bps = _safe_float(signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"), 0.0)
    open_distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0))
    market_range_30s = _safe_float(signal.get("market_range_30s"), 0.0)
    return (
        secs_to_end is not None
        and secs_to_end <= cfg.paper_hold_to_resolution_secs
        and bid_now >= cfg.paper_hold_to_resolution_min_price
        and buffer_bps >= cfg.paper_hold_to_resolution_min_buffer_bps
        and open_distance_bps >= cfg.paper_hold_to_resolution_min_open_distance_bps
        and market_range_30s <= cfg.paper_profit_take_on_market_range_30s
    )


def _exit_reason(
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    bid_now: float,
    tick_size: float,
    now: float,
    secs_to_end: Optional[int],
    signal: dict,
    cfg: CurrentAlmostResolvedConfigV1,
    flatten_deadline_secs: int,
    hold_winner_to_resolution: bool,
) -> Optional[str]:
    if bid_now <= 0 or trade.entry_price is None:
        return None
    # Mid-book reversal: winner bid cruzou o ponto de equilíbrio (0.50).
    # Tese AR invalida — mercado virou 50/50. Saída imediata independente do stop padrão.
    if bid_now <= 0.50:
        return "mid_book_reversal"
    side = trade.side or "UP"
    trade.best_bid = max(_safe_float(trade.best_bid), bid_now)
    pnl_ticks_now = (bid_now - float(trade.entry_price)) / tick_size if tick_size > 0 else 0.0
    buffer_bps = _safe_float(signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"), 0.0)
    open_distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0))
    market_range_15s = _safe_float(signal.get("market_range_15s"), 0.0)
    market_range_30s = _safe_float(signal.get("market_range_30s"), 0.0)
    edge_vs_counter = _safe_float(signal.get("up_edge_vs_counter" if side == "UP" else "down_edge_vs_counter"), 0.0)
    adverse_spot_bps = _safe_float(signal.get("up_adverse_spot_bps" if side == "UP" else "down_adverse_spot_bps"), 0.0)

    if not hold_winner_to_resolution and secs_to_end is not None and secs_to_end <= flatten_deadline_secs:
        return "deadline_flatten"
    if not hold_winner_to_resolution and bid_now >= _safe_float(trade.target_price):
        return "target"
    setup_variant = str(trade.setup_variant or signal.get("setup_variant") or "")
    if (
        setup_variant == "controlled_late_entry"
        and pnl_ticks_now > 0
        and (
            market_range_15s >= cfg.controlled_late_max_market_range_15s
            or adverse_spot_bps >= cfg.controlled_late_max_adverse_spot_15s_bps
            or buffer_bps <= cfg.paper_structural_stop_buffer_bps
            or open_distance_bps <= cfg.min_price_to_beat_distance_bps
        )
    ):
        return "controlled_late_profit_take"
    if (
        setup_variant == "resolved_pullback_limit"
        and (
            market_range_15s >= cfg.near_end_max_market_range_15s
            or market_range_30s >= cfg.paper_profit_take_on_market_range_30s
            or adverse_spot_bps >= cfg.controlled_late_max_adverse_spot_15s_bps
        )
    ):
        return "resolved_pullback_exit"
    if bid_now <= _safe_float(trade.stop_price):
        return "stop"
    if setup_variant == "extreme_99_limit":
        trade.hold_to_resolution = True
        return None
    if (
        pnl_ticks_now >= cfg.paper_profit_take_min_ticks
        and (
            (not hold_winner_to_resolution and secs_to_end is not None and secs_to_end <= cfg.paper_profit_take_late_secs)
            or buffer_bps <= cfg.paper_profit_take_on_reversal_buffer_bps
            or market_range_30s >= cfg.paper_profit_take_on_market_range_30s
            or adverse_spot_bps >= open_distance_bps * cfg.max_reversal_share_of_open_distance
        )
    ):
        return "profit_protect"
    if (
        not hold_winner_to_resolution
        and pnl_ticks_now > 0
        and not trade.hold_to_resolution
        and secs_to_end is not None
        and secs_to_end <= cfg.paper_hold_to_resolution_secs
    ):
        return "late_profit_take"
    # In a binary CLOB, up_ask + down_ask ≈ 1.0. If both sides show prices > 0.50
    # (sum > 1.10), the snapshot is stale/corrupt. Skip structural_stop in that case
    # to avoid exiting profitable positions based on bad data.
    _snap_up_ask = _safe_float(signal.get("up_buy"), 0.0)
    _snap_down_ask = _safe_float(signal.get("down_buy"), 0.0)
    _snap_data_ok = not (_snap_up_ask > 0 and _snap_down_ask > 0 and _snap_up_ask + _snap_down_ask > 1.10)
    if _snap_data_ok and (
        buffer_bps <= cfg.paper_structural_stop_buffer_bps
        or market_range_30s >= cfg.paper_structural_stop_market_range_30s
        or edge_vs_counter <= cfg.paper_structural_stop_edge_vs_counter
        or (signal.get("side") not in (None, side) and signal.get("allow"))
    ):
        return "structural_stop"
    if not hold_winner_to_resolution and not trade.hold_to_resolution and now - trade.created_at >= cfg.max_hold_secs:
        return "timeout"
    return None


def _ee_exit_reason(
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    el_bid: float,
    opp_bid: float,
    secs_to_end: Optional[int],
) -> Optional[str]:
    """Lógica de saída para trades early_entry (v2: stop 0.65, PP 0.88@secs<=70)."""
    if trade.entry_price is None:
        return None
    # Livro EL zerou completamente: não ignorar — verifica se opp confirma reversão.
    # Sem este check, el_bid=0 retornava None e a posição segurava até perda total.
    if el_bid <= 0:
        if opp_bid >= 0.85:
            return "ee_reversal"   # reversão clara; sai a 0.001 em vez de perda total
        if opp_bid > 0:
            return "ee_hedge_gap"  # opp tem liquidez; hedge captura parte da perda
        return None                # ambos os lados sem livro; nada a fazer
    if secs_to_end is not None and secs_to_end <= 35:
        if el_bid >= 0.85:
            return "ee_win"
        if opp_bid >= 0.85:
            return "ee_reversal"
    if secs_to_end is not None and 36 <= secs_to_end <= EE_PROFIT_PROTECT_SECS and el_bid >= EE_PROFIT_PROTECT_BID:
        return "ee_profit_protect"
    # ee_stop removido: FAK em livro fino gerava fills catastróficos (0.42–0.61)
    # em dips temporários onde o bid se recupera (paper WR 89% sem stop).
    # Proteção de reversão genuína via ee_reversal em secs_to_end <= 35.
    if el_bid < EE_HEDGE_THR and opp_bid > 0:
        return "ee_hedge_gap"
    return None


def _post_entry_order(
    broker,
    *,
    signal: dict,
    snap: dict,
    qty: int,
    tick_size: float,
    now: float,
    cfg: CurrentAlmostResolvedConfigV1,
    entry_price_override: Optional[float] = None,
    order_style: str = "direct_limit",
    aggressive_entry_fak: bool = True,
) -> LiveCurrentAlmostResolvedTradeState:
    side = str(signal.get("side") or "")
    entry_price = _safe_float(entry_price_override, _safe_float(signal.get("entry_price"), 0.0))
    order_type = "FAK" if order_style == "aggressive_limit" and aggressive_entry_fak else "GTC"
    trade = LiveCurrentAlmostResolvedTradeState(
        mode="pending_entry",
        event_slug=str(signal.get("event_slug") or ""),
        side=side,
        token_id=_token_id_for_side(snap, side),
        entry_order_style=order_style,
        entry_order_type=order_type,
        setup_variant=str(signal.get("setup_variant") or "standard"),
        entry_price=entry_price,
        entry_qty_requested=float(qty),
        target_price=round(min(0.99, _safe_float(signal.get("exit_price"), cfg.target_exit_price)), 6),
        stop_price=round(
            max(
                0.01,
                _safe_float(signal.get("stop_price"), entry_price - cfg.stop_ticks * tick_size),
            ),
            6,
        ),
        created_at=now,
        updated_at=now,
        last_reason="entry_posted",
    )
    if not trade.token_id:
        raise RuntimeError(f"Missing token_id for side={side}")
    if qty < 5:
        raise RuntimeError("Current almost resolved real requires qty >= 5.")
    if not _has_sufficient_collateral_for_entry(broker, entry_price=entry_price, qty=qty):
        raise RuntimeError(
            f"Insufficient collateral for entry: required={round(entry_price * qty, 6)} available={_collateral_balance_usd(broker)}"
        )
    req = BrokerOrderRequest(
        token_id=trade.token_id,
        side="BUY",
        price=entry_price,
        size=float(qty),
        order_type=order_type,
        market_slug=trade.event_slug,
        outcome=side,
        client_order_key=f"current_almost_resolved:entry:{order_style}:{int(now)}:{side}",
    )
    order = broker.place_limit_order(req)
    trade.entry_order_id = order.order_id
    trade.entry_initial_size_matched = _safe_float(getattr(order, "size_matched", None), 0.0)
    trade.entry_qty_filled = max(trade.entry_qty_filled, trade.entry_initial_size_matched)
    trade.last_reason = f"entry_posted:{order_style}:{order_type.lower()}:matched={round(trade.entry_initial_size_matched, 6)}"
    return trade


def _post_exit_order(
    broker,
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    exit_price: float,
    now: float,
    reason: str,
    min_limit_exit_qty: float,
) -> LiveCurrentAlmostResolvedTradeState:
    token_balance_qty = _token_balance_qty(broker, trade.token_id)
    qty = token_balance_qty if token_balance_qty > 0 else trade.remaining_position_qty
    if _is_flat_qty(qty):
        trade.mode = "idle"
        trade.last_reason = "flat"
        trade.updated_at = now
        return trade
    try:
        broker.update_balance_allowance(asset_type="CONDITIONAL", token_id=trade.token_id)
    except Exception:
        pass
    if qty < float(min_limit_exit_qty) and hasattr(broker, "place_market_order"):
        try:
            order = broker.place_market_order(
                token_id=trade.token_id or "",
                side="SELL",
                amount=float(qty),
                order_type="FAK",
                market_slug=trade.event_slug,
                outcome=trade.side,
            )
            trade.exit_order_id = order.order_id
            trade.mode = "pending_exit"
            trade.updated_at = now
            trade.last_reason = f"exit_posted:{reason}:market_fak"
            return trade
        except Exception as exc:
            trade.mode = "pending_exit"
            trade.exit_order_id = None
            trade.updated_at = now
            trade.last_reason = f"close_failed_residual_position:{round(qty, 6)}:{type(exc).__name__}:{_exception_message(exc)}"
            return trade
    active_book = _fetch_active_book(trade)
    tick_size = _safe_float((active_book or {}).get("tick_size"), 0.001)
    post_price = _clamp_limit_price(float(exit_price) if exit_price > 0 else tick_size, tick_size=tick_size)
    # Urgent exits (stop-type) use FAK to guarantee immediate fill rather than leaving
    # a GTC resting in the book while the market continues to move against the position.
    # Non-urgent exits (profit targets) use GTC so the order can rest at the desired price.
    _urgent = any(k in reason for k in ("stop", "deadline_flatten", "resolved_pullback", "controlled_late_profit", "fill_signal_invalid", "near_win", "oracle_margin", "mid_book"))
    req = BrokerOrderRequest(
        token_id=trade.token_id or "",
        side="SELL",
        price=post_price,
        size=float(qty),
        order_type="FAK" if _urgent else ("GTC" if qty >= float(min_limit_exit_qty) else "FAK"),
        market_slug=trade.event_slug,
        outcome=trade.side,
        client_order_key=f"current_almost_resolved:exit:{reason}:{int(now)}:{trade.side}",
    )
    try:
        order = broker.place_limit_order(req)
        trade.exit_order_id = order.order_id
        trade.exit_price_posted = post_price
        trade.mode = "pending_exit"
        trade.updated_at = now
        trade.last_reason = f"exit_posted:{reason}:{req.order_type.lower()}"
    except Exception as exc:
        trade.mode = "pending_exit"
        trade.exit_order_id = None
        trade.updated_at = now
        trade.last_reason = f"close_failed_residual_position:{round(qty, 6)}:{type(exc).__name__}:{_exception_message(exc)}"
    return trade


def _check_emergency_exit(
    side: str,
    active_bid: float,
    secs_to_end: Optional[int],
    oracle_price: Optional[float],
    oracle_open_price: Optional[float],
    oracle_staleness_secs: float,
    *,
    near_win_enabled: bool,
    near_win_threshold: float,
    near_win_secs: int,
    oracle_margin_enabled: bool,
    oracle_margin_bps_threshold: float,
    oracle_margin_secs: int,
    max_oracle_staleness_secs: float = 120.0,
) -> Optional[str]:
    """
    Retorna razão de saída de emergência (FAK imediato) ou None.

    Regra 1 — near_win_exit:
      Token >= threshold (default 0.995) com <= near_win_secs restantes.
      Trava o lucro quase-certo sem esperar o evento binário da resolução.

    Regra 2 — oracle_margin_exit:
      O oráculo Chainlink indica que a margem até a fronteira de resolução
      é < oracle_margin_bps_threshold (default 2 bps ≈ $1.57 em $78k BTC).
      Saída antes que a reversão on-chain propague para a Polymarket.
    """
    if secs_to_end is None or secs_to_end <= 0:
        return None

    # Regra 1: near-win lock-in
    if near_win_enabled and secs_to_end <= near_win_secs and active_bid >= near_win_threshold:
        return f"near_win_exit:bid={round(active_bid, 4)}:secs={secs_to_end}"

    # Regra 2: margem do oráculo perigosamente pequena
    if (
        oracle_margin_enabled
        and oracle_price is not None
        and oracle_open_price is not None
        and oracle_open_price > 0
        and oracle_staleness_secs <= max_oracle_staleness_secs
        and secs_to_end <= oracle_margin_secs
    ):
        if side == "DOWN":
            # DOWN ganha se oracle_price < oracle_open_price
            margin_bps = (oracle_open_price - oracle_price) / oracle_open_price * 10_000
        elif side == "UP":
            # UP ganha se oracle_price > oracle_open_price
            margin_bps = (oracle_price - oracle_open_price) / oracle_open_price * 10_000
        else:
            margin_bps = None

        if margin_bps is not None and margin_bps < oracle_margin_bps_threshold:
            return f"oracle_margin_exit:{round(margin_bps, 2)}bps:oracle={round(oracle_price, 2)}:open={round(oracle_open_price, 2)}"

    return None


def _mark_awaiting_redeem(
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    now: float,
    reason: str,
) -> LiveCurrentAlmostResolvedTradeState:
    trade.mode = "awaiting_redeem"
    trade.entry_order_id = None
    trade.exit_order_id = None
    trade.updated_at = now
    trade.resolution_detected_at = now
    trade.redeem_required = True
    trade.last_reason = reason
    return trade


def _attempt_redeem_if_available(broker, trade: LiveCurrentAlmostResolvedTradeState) -> Optional[dict]:
    for method_name in ("redeem_positions", "redeem_position", "claim", "claim_rewards"):
        method = getattr(broker, method_name, None)
        if callable(method):
            try:
                return {"method": method_name, "response": method(trade)}
            except TypeError:
                try:
                    return {"method": method_name, "response": method()}
                except Exception as exc:
                    return {"method": method_name, "error": f"{type(exc).__name__}: {exc}"}
            except Exception as exc:
                return {"method": method_name, "error": f"{type(exc).__name__}: {exc}"}
    client = getattr(broker, "client", None)
    for method_name in ("redeem_positions", "redeem_position", "claim", "claim_rewards"):
        method = getattr(client, method_name, None)
        if callable(method):
            try:
                return {"method": f"client.{method_name}", "response": method()}
            except Exception as exc:
                return {"method": f"client.{method_name}", "error": f"{type(exc).__name__}: {exc}"}
    return None


def _force_risk_cleanup(broker, trade: LiveCurrentAlmostResolvedTradeState, log_path: Path, now: float, reason: str, min_limit_exit_qty: float) -> None:
    try:
        _append_jsonl(log_path, {"type": "panic", "ts": now, "reason": reason, "trade": _trade_summary(trade)})
        if trade.mode == "awaiting_redeem":
            _append_jsonl(log_path, {"type": "panic_skip_awaiting_redeem", "ts": now, "reason": reason, "trade": _trade_summary(trade)})
            return
        _cancel_if_live(broker, trade.entry_order_id)
        _cancel_if_live(broker, trade.exit_order_id)
        token_balance_qty = _token_balance_qty(broker, trade.token_id)
        panic_qty = token_balance_qty if token_balance_qty > 0 else trade.remaining_position_qty
        if not _is_flat_qty(panic_qty) and trade.token_id and trade.side:
            active_book = _fetch_active_book(trade)
            panic_bid = _best_bid(active_book or {})
            _post_exit_order(
                broker,
                trade,
                exit_price=panic_bid if panic_bid > 0 else 0.01,
                now=now,
                reason="panic",
                min_limit_exit_qty=min_limit_exit_qty,
            )
            _append_jsonl(log_path, {"type": "panic_exit_attempted", "ts": now, "panic_bid": panic_bid, "trade": _trade_summary(trade)})
    except Exception as exc:
        _append_jsonl(log_path, {"type": "panic_error", "ts": now, "reason": reason, "error": f"{type(exc).__name__}: {exc}"})


def _shutdown_reconcile(
    broker,
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    min_limit_exit_qty: float,
    state_path: Path,
    log_path: Path,
    session_id: str,
    now: float,
) -> LiveCurrentAlmostResolvedTradeState:
    if trade.mode == "awaiting_redeem":
        _save_state(state_path, trade)
        _append_jsonl(
            log_path,
            {
                "type": "shutdown_awaiting_redeem",
                "ts": now,
                "session_id": session_id,
                "trade": _trade_summary(trade),
            },
        )
        return trade

    try:
        reconciled = _restore_trade_from_broker(broker, trade)
    except Exception:
        reconciled = trade

    try:
        open_orders = [o.as_dict() for o in broker.get_open_orders()[:50]]
    except Exception:
        open_orders = []

    if reconciled.mode == "pending_entry":
        cancel_resp = _cancel_if_live(broker, reconciled.entry_order_id)
        reconciled = _restore_trade_from_broker(broker, reconciled)
        _append_jsonl(
            log_path,
            {
                "type": "shutdown_entry_cancel",
                "ts": now,
                "session_id": session_id,
                "cancel": cancel_resp,
                "trade": _trade_summary(reconciled),
            },
        )

    token_balance = _token_balance_qty(broker, reconciled.token_id)
    if reconciled.mode != "idle" and not _is_flat_qty(token_balance) and reconciled.token_id and reconciled.side:
        active_book = _fetch_active_book(reconciled)
        shutdown_bid = _best_bid(active_book or {})
        reconciled = _post_exit_order(
            broker,
            reconciled,
            exit_price=shutdown_bid if shutdown_bid > 0 else 0.01,
            now=now,
            reason="shutdown_flatten",
            min_limit_exit_qty=min_limit_exit_qty,
        )
        _append_jsonl(
            log_path,
            {
                "type": "shutdown_exit_posted",
                "ts": now,
                "session_id": session_id,
                "bid": shutdown_bid,
                "token_balance_qty": token_balance,
                "trade": _trade_summary(reconciled),
            },
        )
        try:
            open_orders = [o.as_dict() for o in broker.get_open_orders()[:50]]
        except Exception:
            open_orders = []
        token_balance = _token_balance_qty(broker, reconciled.token_id)

    if reconciled.mode == "idle" or (not open_orders and _is_flat_qty(token_balance)):
        _append_jsonl(
            log_path,
            {
                "type": "shutdown_flat",
                "ts": now,
                "session_id": session_id,
                "open_orders": open_orders,
                "token_balance_qty": token_balance,
                "trade": _trade_summary(reconciled),
            },
        )
        _clear_state(state_path)
        return LiveCurrentAlmostResolvedTradeState()

    _save_state(state_path, reconciled)
    _append_jsonl(
        log_path,
        {
            "type": "shutdown_non_idle",
            "ts": now,
            "session_id": session_id,
            "open_orders": open_orders,
            "token_balance_qty": token_balance,
            "trade": _trade_summary(reconciled),
        },
    )
    return reconciled


def monitor_live_current_almost_resolved_real_v1(duration_seconds: Optional[int] = None, log_dir: Optional[str] = None) -> None:
    load_dotenv()
    guarded_cfg = load_live_guarded_config()
    broker_status = load_broker_env()
    signal_cfg = CurrentAlmostResolvedConfigV1()
    scalp_cfg = CurrentScalpConfigV1()

    print("[BROKER_ENV]", broker_status.as_dict())
    print("[LIVE_GUARDED_CONFIG]", guarded_cfg.as_dict())
    print("[CURRENT_ALMOST_RESOLVED_CONFIG]", signal_cfg.as_dict())
    print("[CURRENT_SCALP_CONTEXT_CONFIG]", scalp_cfg.as_dict())

    if not guarded_cfg.enabled:
        print("[GUARD] Set POLY_GUARDED_ENABLED=true")
        return
    if guarded_cfg.shadow_only:
        print("[GUARD] Set POLY_GUARDED_SHADOW_ONLY=false")
        return
    if not guarded_cfg.real_posts_enabled:
        print("[GUARD] Set POLY_GUARDED_REAL_POSTS_ENABLED=true")
        return
    if not _env_bool("POLY_CURRENT_ALMOST_RESOLVED_REAL_ENABLED", False):
        print("[GUARD] Set POLY_CURRENT_ALMOST_RESOLVED_REAL_ENABLED=true to arm real current almost resolved")
        return
    if not broker_status.ready_for_real_smoke:
        print("[GUARD] Broker env missing required credentials")
        return

    qty = _env_int("POLY_CURRENT_ALMOST_RESOLVED_QTY", 6)
    entry_timeout_secs = _env_float("POLY_CURRENT_ALMOST_RESOLVED_ENTRY_TIMEOUT_SECS", 2.0)
    passive_capture_only = _env_bool("POLY_CURRENT_ALMOST_RESOLVED_PASSIVE_CAPTURE_ONLY", True)
    hybrid_entry_enabled = _env_bool("POLY_CURRENT_ALMOST_RESOLVED_HYBRID_ENTRY", True)
    hybrid_aggressive_after_secs = _env_float("POLY_CURRENT_ALMOST_RESOLVED_HYBRID_AGGRESSIVE_AFTER_SECS", 2.0)
    hybrid_aggressive_max_price = _env_float("POLY_CURRENT_ALMOST_RESOLVED_HYBRID_AGGRESSIVE_MAX_PRICE", 0.99)
    aggressive_entry_fak = _env_bool("POLY_CURRENT_ALMOST_RESOLVED_AGGRESSIVE_ENTRY_FAK", True)
    runaway_chase_max_price = _env_float("POLY_CURRENT_ALMOST_RESOLVED_RUNAWAY_CHASE_MAX_PRICE", 0.98)
    runaway_chase_max_distance = _env_float("POLY_CURRENT_ALMOST_RESOLVED_RUNAWAY_CHASE_MAX_DISTANCE", 0.04)
    runaway_chase_min_secs = _env_int("POLY_CURRENT_ALMOST_RESOLVED_RUNAWAY_CHASE_MIN_SECS", 25)
    hold_winner_to_resolution = _env_bool("POLY_CURRENT_ALMOST_RESOLVED_HOLD_WINNER_TO_RESOLUTION", True)
    resolution_settle_secs = _env_int("POLY_CURRENT_ALMOST_RESOLVED_RESOLUTION_SETTLE_SECS", 1)
    auto_redeem_enabled = _env_bool("POLY_CURRENT_ALMOST_RESOLVED_AUTO_REDEEM_ENABLED", False)
    dust_archive_qty = _env_float("POLY_CURRENT_ALMOST_RESOLVED_DUST_ARCHIVE_QTY", 0.01)
    loss_writeoff_timeout_secs = _env_float("POLY_CURRENT_ALMOST_RESOLVED_LOSS_WRITEOFF_TIMEOUT_SECS", 3600.0)
    exit_repost_secs = _env_float("POLY_CURRENT_ALMOST_RESOLVED_EXIT_REPOST_SECS", 1.0)
    flatten_deadline_secs = _env_int("POLY_CURRENT_ALMOST_RESOLVED_FLATTEN_DEADLINE_SECS", 2)
    min_limit_exit_qty = _env_float("POLY_CURRENT_ALMOST_RESOLVED_MIN_LIMIT_EXIT_QTY", 5.0)
    poll_secs = max(0.25, _env_float("POLY_CURRENT_ALMOST_RESOLVED_POLL_SECS", 0.5))
    run_for = int(duration_seconds or _env_int("POLY_CURRENT_ALMOST_RESOLVED_RUN_SECONDS", 1800))
    # --- Chainlink oracle + saídas de emergência ---
    chainlink_enabled = _env_bool("POLY_CHAINLINK_ENABLED", True)
    chainlink_rpc_url = os.getenv("POLY_CHAINLINK_RPC_URL", "")
    near_win_exit_enabled = _env_bool("POLY_NEAR_WIN_EXIT_ENABLED", True)
    near_win_exit_threshold = _env_float("POLY_NEAR_WIN_EXIT_THRESHOLD", 0.995)
    near_win_exit_secs = _env_int("POLY_NEAR_WIN_EXIT_SECS", 15)
    oracle_margin_exit_enabled = _env_bool("POLY_ORACLE_MARGIN_EXIT_ENABLED", True)
    oracle_margin_exit_bps = _env_float("POLY_ORACLE_MARGIN_EXIT_BPS", 2.0)
    oracle_margin_exit_secs = _env_int("POLY_ORACLE_MARGIN_EXIT_SECS", 30)
    ee_enabled = _env_bool("EE_REAL_ENABLED", False)
    ee_posts_enabled = _env_bool("EE_REAL_POSTS_ENABLED", False)
    session_dir = Path(log_dir) if log_dir else _build_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "current_almost_resolved_real.jsonl"
    exception_path = session_dir / "exception.log"
    state_path = _state_path()
    session_id = session_dir.name
    blocked_entry_events: dict[str, str] = {}
    _reentry_blocked_until: dict[str, float] = {}
    reentry_cooldown_secs: float = 25.0

    print(
        "[CURRENT_ALMOST_RESOLVED_REAL_PARAMS]",
        {
            "qty": qty,
            "entry_timeout_secs": entry_timeout_secs,
            "passive_capture_only": passive_capture_only,
            "hybrid_entry_enabled": hybrid_entry_enabled,
            "hybrid_aggressive_after_secs": hybrid_aggressive_after_secs,
            "hybrid_aggressive_max_price": hybrid_aggressive_max_price,
            "aggressive_entry_fak": aggressive_entry_fak,
            "hold_winner_to_resolution": hold_winner_to_resolution,
            "resolution_settle_secs": resolution_settle_secs,
            "auto_redeem_enabled": auto_redeem_enabled,
            "dust_archive_qty": dust_archive_qty,
            "loss_writeoff_timeout_secs": loss_writeoff_timeout_secs,
            "exit_repost_secs": exit_repost_secs,
            "flatten_deadline_secs": flatten_deadline_secs,
            "min_limit_exit_qty": min_limit_exit_qty,
            "poll_secs": poll_secs,
            "run_for": run_for,
            "log_path": str(log_path),
            "state_path": str(state_path),
        },
    )

    broker = PolymarketBrokerV3.from_env()
    health = broker.healthcheck()
    print("[BROKER_HEALTH]", health.as_dict())
    if not health.ok:
        print("[GUARD] Broker healthcheck failed")
        return

    startup_orders = broker.get_open_orders()[:50]
    print("[BROKER_OPEN_ORDERS_STARTUP]", [o.as_dict() for o in startup_orders])
    restored_trade = _load_state(state_path) or LiveCurrentAlmostResolvedTradeState()
    _append_jsonl(
        log_path,
        {
            "type": "startup",
            "ts": time.time(),
            "session_id": session_id,
            "startup_orders": [o.as_dict() for o in startup_orders],
            "restored_trade": _trade_summary(restored_trade),
            "ee_enabled": ee_enabled,
            "ee_posts_enabled": ee_posts_enabled,
        },
    )

    if restored_trade.mode != "idle":
        # Reconciliar awaiting_redeem imediatamente: tokens podem ter zerado enquanto runner estava parado
        if restored_trade.mode == "awaiting_redeem" and restored_trade.token_id:
            _su_bal = _token_balance_qty(broker, restored_trade.token_id)
            _su_collateral = _collateral_balance_usd(broker)
            if _is_flat_qty(_su_bal):
                _su_ep  = _safe_float(restored_trade.entry_price, 0.0)
                _su_qty = _safe_float(restored_trade.entry_qty_filled, 0.0)
                _su_rsn = str(restored_trade.last_reason or "")
                if "resolution_win" in _su_rsn:
                    _su_exit = 1.0
                elif "resolution_loss" in _su_rsn:
                    _su_exit = 0.0
                else:
                    _su_exit = None
                _su_pnl = round((_su_exit - _su_ep) * _su_qty, 4) if _su_exit is not None and _su_ep > 0 and _su_qty > 0 else None
                _append_jsonl(log_path, {
                    "type": "redeem_flat", "ts": time.time(), "session_id": session_id,
                    "source": "startup_reconcile",
                    "token_balance_qty": _su_bal, "collateral_balance_usd": _su_collateral,
                    "exit_price": _su_exit, "pnl_usd": _su_pnl,
                    "trade": _trade_summary(restored_trade),
                })
                _append_jsonl(log_path, {
                    "type": "trade_closed", "ts": time.time(), "session_id": session_id,
                    "source": "startup_reconcile",
                    "side": restored_trade.side, "entry_price": _su_ep,
                    "exit_price": _su_exit, "qty": _su_qty, "pnl_usd": _su_pnl,
                    "entry_slug": restored_trade.event_slug,
                    "last_reason": _su_rsn,
                })
                restored_trade = LiveCurrentAlmostResolvedTradeState()
                _clear_state(state_path)
        if restored_trade.mode != "idle":
            restored_trade = _restore_trade_from_broker(broker, restored_trade)
        print("[RESTORED_CURRENT_ALMOST_RESOLVED_TRADE]", asdict(restored_trade))
        if restored_trade.mode == "idle":
            _clear_state(state_path)
        else:
            allowed_ids = {x for x in (restored_trade.entry_order_id, restored_trade.exit_order_id) if x}
            startup_ids = {o.order_id for o in startup_orders}
            if startup_ids - allowed_ids:
                print("[GUARD] Refusing to start with open orders not owned by restored current almost resolved state.")
                return
            _save_state(state_path, restored_trade)
    elif startup_orders:
        print("[GUARD] Refusing to start with open orders while no current almost resolved state is restored.")
        return

    current_scalp = CurrentScalpResearchV1(cfg=scalp_cfg)
    trade = restored_trade
    current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}
    last_leader_price_by_key: dict[str, float] = {}
    leader_velocity_history: dict[str, list[tuple[float, float]]] = {}
    _bid_history: dict[str, deque] = {}        # keyed "slug:SIDE", tracks leader buy price per poll
    _loser_bid_history: dict[str, deque] = {}  # keyed "slug:LOSER_SIDE", tracks loser buy price for scalp
    _el_tracker = _EarlyLeaderTracker(early_min=0.55, cont_min=0.70, strong_min=0.72)
    _ee_tracker = _EarlyLeaderTrackerEE()
    started_at = time.time()
    _health_check_polls = max(1, int(60.0 / max(0.25, poll_secs)))
    _health_poll_counter = 0
    _platform_paused_until = 0.0

    oracle = (
        ChainlinkBTCOracle(rpc_urls=[chainlink_rpc_url] if chainlink_rpc_url else None)
        if chainlink_enabled
        else None
    )
    _oracle_open_prices: dict[str, float] = {}
    _last_oracle_price: Optional[float] = None
    _last_oracle_staleness: float = float("inf")
    _last_known_slug: Optional[str] = None
    _last_chart_ctx: Optional[BtcChartContext] = None
    _last_chart_ctx_bucket: int = -1

    while time.time() - started_at < run_for:
        now = time.time()
        try:
            _health_poll_counter += 1
            if _health_poll_counter >= _health_check_polls:
                _health_poll_counter = 0
                try:
                    _loop_health = broker.healthcheck()
                    if not _loop_health.ok:
                        _platform_paused_until = now + 120.0
                        _append_jsonl(log_path, {"type": "loop_health_failed", "ts": now, "session_id": session_id, "health": _loop_health.as_dict(), "paused_until": _platform_paused_until})
                        print(f"[GUARD] Broker healthcheck failed in loop — entries paused 120s: {_loop_health.as_dict()}")
                except Exception as _hc_exc:
                    _platform_paused_until = now + 120.0
                    _append_jsonl(log_path, {"type": "loop_health_error", "ts": now, "session_id": session_id, "error": f"{type(_hc_exc).__name__}: {_hc_exc}", "paused_until": _platform_paused_until})

            slot_bundle = _build_slot_bundle()
            current_item = slot_bundle["queue"].get("current")
            current_secs = int(current_item.get("seconds_to_end")) if current_item and current_item.get("seconds_to_end") is not None else None
            slot_state = _fetch_slot_state(slot_bundle)
            current_snap = _slot_snapshot(slot_state, "current")
            current_exec, current_exec_reason = _compute_executable_metrics(current_snap)

            if current_item and current_item.get("slug") != current_open_reference.get("slug"):
                raw_event = fetch_event_by_slug(str(current_item.get("slug") or ""))
                market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
                event_start_time = market.get("eventStartTime") or raw_event.get("startTime") if raw_event else None
                open_ref = fetch_binance_open_price_for_event_start_v1(event_start_time) if event_start_time else {"open_price": None}
                current_open_reference = {
                    "slug": current_item.get("slug"),
                    "price": open_ref.get("open_price"),
                    "event_start_time": event_start_time,
                }

            reference = fetch_external_btc_reference_v1() if current_item else {}
            current_scalp_signal = (
                current_scalp.evaluate(
                    snap=current_snap,
                    secs_to_end=current_secs,
                    event_start_time=current_open_reference.get("event_start_time"),
                    now_ts=now,
                    reference_price=reference.get("reference_price"),
                    source_divergence_bps=reference.get("source_divergence_bps"),
                    opening_reference_price=current_open_reference.get("price"),
                )
                if current_item
                else {"setup": "no_edge", "allow": False, "reason": "missing_current"}
            )
            signal = (
                evaluate_current_almost_resolved_v1(
                    snap=current_snap,
                    secs_to_end=current_secs,
                    reference_signal=current_scalp_signal,
                    cfg=signal_cfg,
                )
                if current_item
                else {"setup": "almost_resolved", "allow": False, "reason": "missing_current"}
            )
            if current_item:
                signal["event_slug"] = current_item.get("slug")
                signal["signed_distance_from_open_bps"] = current_scalp_signal.get("distance_from_open_bps")
                signal["guardian_hold_winner_to_resolution"] = bool(hold_winner_to_resolution)

            # Update rolling 60s leader velocity history for both sides
            _event_slug_for_vel = str(current_item.get("slug") or "") if current_item else ""
            if _event_slug_for_vel:
                for _vel_side, _vel_key in (("UP", "up_buy"), ("DOWN", "down_buy")):
                    _vel_price = _safe_float(signal.get(_vel_key), 0.0)
                    if _vel_price > 0:
                        _hist_key = f"{_event_slug_for_vel}:{_vel_side}"
                        _hist = leader_velocity_history.setdefault(_hist_key, [])
                        _hist.append((now, _vel_price))
                        leader_velocity_history[_hist_key] = [(t, p) for t, p in _hist if now - t <= 60.0]

            active_book = _fetch_active_book(trade) if trade.mode in ("pending_entry", "open_position", "pending_exit", "exit_pending_confirm") else None
            active_bid = _best_bid(active_book or {})
            if trade.side and current_exec:
                exec_bid = _bid_for_side(current_exec, trade.side)
                if exec_bid > 0:
                    active_bid = max(active_bid, exec_bid)
            counter_reversal_active = _state_file_active(_counter_reversal_state_path())

            # Bid deceleration gate — tracking de preço líder por slug para detectar pico de momentum
            _decel_gate_up: Optional[dict] = None
            _decel_gate_down: Optional[dict] = None
            if current_item:
                _csid = str(current_item.get("slug") or "")
                # Evict entries from previous slugs
                for _dk in [k for k in _bid_history if not k.startswith(_csid + ":")]:
                    del _bid_history[_dk]
                for _ds, _dp in (
                    ("UP", _safe_float(signal.get("up_buy"), 0.0)),
                    ("DOWN", _safe_float(signal.get("down_buy"), 0.0)),
                ):
                    if _dp <= 0:
                        continue
                    _dkey = f"{_csid}:{_ds}"
                    if _dkey not in _bid_history:
                        _bid_history[_dkey] = deque(maxlen=6)
                    _bid_history[_dkey].append(_dp)
                    _dh = list(_bid_history[_dkey])
                    _dp3 = _dh[-4] if len(_dh) >= 4 else None
                    _vel = round(_dp - _dp3, 6) if _dp3 is not None else None
                    _dgate = {
                        "active_bid_now": round(_dp, 6),
                        "active_bid_3polls_ago": round(_dp3, 6) if _dp3 is not None else None,
                        "bid_velocity": _vel,
                        "would_block": bool(_vel is not None and _vel < -0.005),
                        "threshold": -0.005,
                    }
                    if _ds == "UP":
                        _decel_gate_up = _dgate
                    else:
                        _decel_gate_down = _dgate
            _sig_side_decel = str(signal.get("side") or "")
            _decel_gate_for_entry = (
                _decel_gate_up if _sig_side_decel == "UP"
                else _decel_gate_down if _sig_side_decel == "DOWN"
                else None
            )

            # Loser bid momentum — tracker para análise do reversal_scalp (Sinal E)
            _loser_bid_info: Optional[dict] = None
            if current_item:
                _up_b = _safe_float(signal.get("up_buy"), 0.0)
                _dn_b = _safe_float(signal.get("down_buy"), 0.0)
                if _up_b > 0 and _dn_b > 0:
                    _csid2 = str(current_item.get("slug") or "")
                    _winner_s = "UP" if _up_b >= _dn_b else "DOWN"
                    _loser_s = "DOWN" if _winner_s == "UP" else "UP"
                    _loser_bid_now = _dn_b if _loser_s == "DOWN" else _up_b
                    _lkey = f"{_csid2}:{_loser_s}"
                    # Evict old slugs
                    for _ldk in [k for k in _loser_bid_history if not k.startswith(_csid2 + ":")]:
                        del _loser_bid_history[_ldk]
                    if _lkey not in _loser_bid_history:
                        _loser_bid_history[_lkey] = deque(maxlen=6)
                    _loser_bid_history[_lkey].append(_loser_bid_now)
                    _lhist = list(_loser_bid_history[_lkey])
                    _loser_2ago = _lhist[-3] if len(_lhist) >= 3 else None
                    _loser_vel = round(_loser_bid_now - _loser_2ago, 6) if _loser_2ago is not None else None
                    _sinal_e_score = 0
                    if _loser_vel is not None:
                        if _loser_vel > 0.015:
                            _sinal_e_score = 2
                        elif _loser_vel > 0.005:
                            _sinal_e_score = 1
                    _loser_bid_info = {
                        "loser_side": _loser_s,
                        "loser_bid": round(_loser_bid_now, 4),
                        "loser_momentum_2polls": _loser_vel,
                        "sinal_e_score": _sinal_e_score,
                    }

            # Early leader tracking — atualiza estado EL do slug atual
            _el_slug = str(current_item.get("slug") or "") if current_item else ""
            if _el_slug and current_secs is not None:
                # Usar current_exec (livro ao vivo) em vez do sinal AR — disponível em todos os secs
                _el_up = _safe_float((current_exec or {}).get("up_bid"), 0.0)
                _el_dn = _safe_float((current_exec or {}).get("down_bid"), 0.0)
                _el_tracker.update(_el_slug, current_secs, _el_up, _el_dn)
                _el_tracker.evict_old(_el_slug)
                _ee_tracker.update(_el_slug, current_secs, _el_up, _el_dn)
            _el_state = _el_tracker.state(_el_slug) if _el_slug else {}

            # Logar inversão forte ao detectar (uma vez por slug)
            if _el_state.get("inverted") and not _el_tracker.inversion_logged(_el_slug):
                _append_jsonl(log_path, {
                    "type": "el_inversion",
                    "ts": now,
                    "session_id": session_id,
                    "slug": _el_slug,
                    "early_leader": _el_state,
                    "strong": _el_state.get("inversion_strong"),
                    "current_secs": current_secs,
                })
                _el_tracker.mark_inversion_logged(_el_slug)

            # Chainlink oracle — query e rastreamento de preço de abertura por slug
            if oracle is not None:
                _last_oracle_price, _, _last_oracle_staleness = oracle.get_price()
                _current_slug_now = current_item.get("slug") if current_item else None
                if _current_slug_now and _current_slug_now != _last_known_slug:
                    if _last_oracle_price is not None:
                        _oracle_open_prices[_current_slug_now] = _last_oracle_price
                    _last_known_slug = _current_slug_now

            # Chart context — fetch once per 5min BTC candle (cached per bucket)
            _ctx_bucket_now = int(now / 300)
            if _ctx_bucket_now != _last_chart_ctx_bucket and signal.get("allow") and current_item:
                _ref_price_ctx = _safe_float((reference or {}).get("reference_price"), _last_oracle_price or 80_000.0)
                _event_slug_ctx = str(current_item.get("slug") or "")
                _event_end_ctx = (float(_event_slug_ctx.split("-")[-1]) + 300.0) if _event_slug_ctx else now + 300.0
                try:
                    _last_chart_ctx = fetch_btc_chart_context(
                        current_price=_ref_price_ctx,
                        entry_ts=now,
                        event_end_ts=_event_end_ctx,
                    )
                    _last_chart_ctx_bucket = _ctx_bucket_now
                except Exception:
                    pass  # keep previous context on failure

            snapshot = {
                "type": "snapshot",
                "ts": now,
                "session_id": session_id,
                "current_slug": current_item.get("slug") if current_item else None,
                "current_secs": current_secs,
                "current_exec_reason": current_exec_reason,
                "reference": reference,
                "current_scalp_context": current_scalp_signal,
                "signal": signal,
                "trade": _trade_summary(trade),
                "active_bid": active_bid,
                "counter_reversal_active": counter_reversal_active,
                "oracle_price": _last_oracle_price,
                "oracle_open_price": _oracle_open_prices.get(current_item.get("slug") if current_item else None),
                "oracle_staleness_secs": round(_last_oracle_staleness, 1) if _last_oracle_staleness < 1e6 else None,
                "platform_paused_until": round(_platform_paused_until, 1) if _platform_paused_until > now else None,
                "btc_chart_context": _last_chart_ctx.summary() if _last_chart_ctx else None,
                "bid_decel_gate": {"up": _decel_gate_up, "down": _decel_gate_down},
                "loser_bid_tracker": _loser_bid_info,
                "early_leader": _el_state,
                "ee_tracker": _ee_tracker.state_dict() if ee_enabled else None,
            }
            _append_jsonl(log_path, snapshot)
            print(
                f"[CURRENT_ALMOST_RESOLVED_REAL] current_secs={current_secs} allow={signal.get('allow')} "
                f"side={signal.get('side')} mode={trade.mode} qty={trade.entry_qty_filled}/{trade.remaining_position_qty}"
            )

            if trade.mode == "idle" and now < _platform_paused_until:
                time.sleep(poll_secs)
                continue

            if trade.mode == "idle" and current_item and signal.get("allow") and not counter_reversal_active:
                event_slug = str(signal.get("event_slug") or current_item.get("slug") or "")
                if passive_capture_only and str(signal.get("setup_variant") or "") != "passive_extreme_liquidity_capture":
                    _append_jsonl(
                        log_path,
                        {
                            "type": "entry_blocked",
                            "ts": now,
                            "session_id": session_id,
                            "reason": "passive_capture_only",
                            "signal": signal,
                        },
                    )
                    time.sleep(poll_secs)
                    continue
                # passive_extreme_liquidity_capture: bloqueado sempre.
                # Único trade histórico registrou perda total de -$9.80;
                # sem evidência de edge positivo em nenhuma faixa de secs.
                if str(signal.get("setup_variant") or "") == "passive_extreme_liquidity_capture":
                    _append_jsonl(
                        log_path,
                        {
                            "type": "entry_blocked",
                            "ts": now,
                            "session_id": session_id,
                            "reason": f"passive_extreme_blocked:secs={current_secs}",
                            "signal": signal,
                        },
                    )
                    time.sleep(poll_secs)
                    continue
                # Setups com <30s restantes: book fino, stop não preenche no preço
                # correto. Validado em logs reais — evita slippage e entradas erráticas.
                # Isentos: controlled_late_entry (edge confirmado em secs < 30 nos logs)
                #          resolved_pullback_limit (entra propositalmente nos segundos finais)
                if (
                    str(signal.get("setup_variant") or "") not in (
                        "controlled_late_entry", "resolved_pullback_limit"
                    )
                    and current_secs is not None
                    and current_secs < 30
                ):
                    _append_jsonl(
                        log_path,
                        {
                            "type": "entry_blocked",
                            "ts": now,
                            "session_id": session_id,
                            "reason": f"entry_too_late:secs={current_secs}",
                            "signal": signal,
                        },
                    )
                    time.sleep(poll_secs)
                    continue
                # EXC.OPOSTO — bloquear quando Early Leader contradiz o sinal AR.
                # EL baseline 78.6%; quando EL ≠ AR side, probabilidade de AR errar aumenta.
                # Nos dados simulados (outra máquina): remove 25 trades ruins, +$2.76 PnL.
                _el_side_now = _el_state.get("early_side")
                _ar_side_now = str(signal.get("side") or "")
                _el_inverted = _el_state.get("inverted", False)
                _el_inversion_side = _el_state.get("inversion_side")
                # Exceção: se EL inverteu e AR segue o novo líder, NÃO bloquear.
                # Dados: novo líder após inversão EL >= 0.60 vence 71-100%.
                _exc_oposto_active = (
                    _el_side_now and _ar_side_now
                    and _el_side_now != _ar_side_now
                    and not (_el_inverted and _el_inversion_side == _ar_side_now)
                )
                if _exc_oposto_active:
                    _append_jsonl(log_path, {
                        "type": "entry_blocked",
                        "ts": now,
                        "session_id": session_id,
                        "reason": f"exc_oposto:el={_el_side_now}:ar={_ar_side_now}",
                        "early_leader": _el_state,
                        "signal": signal,
                    })
                    time.sleep(poll_secs)
                    continue

                # dual_rich_late_limit: bloquear quando oracle contradiz o lado entrado.
                # Ambas as perdas em Fase 3 ocorreram com oracle_div > 0 entrando DOWN.
                # Threshold conservador: 5 bps para evitar bloqueios em mercados planos.
                if str(signal.get("setup_variant") or "") == "dual_rich_late_limit":
                    _slug_for_oracle = current_item.get("slug") if current_item else None
                    _oracle_open_ref = _oracle_open_prices.get(_slug_for_oracle)
                    if _last_oracle_price is not None and _oracle_open_ref and _oracle_open_ref > 0:
                        _oracle_div_bps = (_last_oracle_price - _oracle_open_ref) / _oracle_open_ref * 10_000
                        _entered_side = str(signal.get("side") or "")
                        _oracle_contradicts = (
                            (_entered_side == "DOWN" and _oracle_div_bps > 5.0)
                            or (_entered_side == "UP" and _oracle_div_bps < -5.0)
                        )
                        if _oracle_contradicts:
                            _append_jsonl(
                                log_path,
                                {
                                    "type": "entry_blocked",
                                    "ts": now,
                                    "session_id": session_id,
                                    "reason": f"dual_rich_oracle_direction:div={round(_oracle_div_bps,1)}bps:side={_entered_side}",
                                    "signal": signal,
                                },
                            )
                            time.sleep(poll_secs)
                            continue
                if event_slug and event_slug in blocked_entry_events:
                    _append_jsonl(
                        log_path,
                        {
                            "type": "entry_blocked",
                            "ts": now,
                            "session_id": session_id,
                            "reason": f"event_entry_cooldown:{blocked_entry_events[event_slug]}",
                            "signal": signal,
                        },
                    )
                    time.sleep(poll_secs)
                    continue
                if event_slug and _reentry_blocked_until.get(event_slug, 0.0) > now:
                    _remaining = round(_reentry_blocked_until[event_slug] - now, 1)
                    _append_jsonl(
                        log_path,
                        {
                            "type": "entry_blocked",
                            "ts": now,
                            "session_id": session_id,
                            "reason": "reentry_cooldown",
                            "cooldown_remaining_secs": _remaining,
                            "signal": signal,
                        },
                    )
                    time.sleep(poll_secs)
                    continue
                side = str(signal.get("side") or "")
                tick_size = _tick_size_from_snap(current_snap, side)
                signal_entry_price = _safe_float(signal.get("entry_price"), 0.0)
                entry_price = signal_entry_price
                order_style = "direct_limit"
                if hybrid_entry_enabled:
                    current_bid = _bid_for_side(current_exec, side)
                    entry_price = _passive_entry_price_for_signal(
                        signal,
                        bid_now=current_bid,
                        tick_size=tick_size,
                    )
                    order_style = "passive_limit"

                # tick_up_confirmed: for passive entries, only proceed if leader_price
                # has ticked up since the last poll on this (slug, side) pair. Prevents
                # posting passive orders on stale signals that have already lost momentum.
                is_passive_signal = (
                    str(signal.get("setup_variant") or "") == "passive_extreme_liquidity_capture"
                    and str(signal.get("execution_style") or "") == "post_only"
                )
                leader_price = _safe_float(signal.get("leader_price"), 0.0)
                leader_key = f"{event_slug}:{side}"
                prev_leader_price = last_leader_price_by_key.get(leader_key)
                tick_up_confirmed = (
                    not is_passive_signal
                    or prev_leader_price is None
                    or leader_price > prev_leader_price
                )
                if side in ("UP", "DOWN") and leader_price > 0:
                    last_leader_price_by_key[leader_key] = leader_price
                if not tick_up_confirmed:
                    _append_jsonl(
                        log_path,
                        {
                            "type": "entry_blocked",
                            "ts": now,
                            "session_id": session_id,
                            "reason": "passive_tick_up_required",
                            "leader_price": leader_price,
                            "prev_leader_price": prev_leader_price,
                            "signal": signal,
                        },
                    )
                    time.sleep(poll_secs)
                    continue

                # Block entry if leader moved too fast (dual-window velocity filter).
                # Blocks if: vel_range_30s >= 0.04  OR  vel_range_60s >= 0.10
                # Old single rule (>= 0.06 on 60s) was too aggressive for spikes
                # that already faded — market_range_15s/30s is near 0 but 60s window
                # still "sees" the old move, blocking clean setups unnecessarily.
                _vel_hist = leader_velocity_history.get(leader_key, [])
                if len(_vel_hist) >= 2:
                    _vel_prices_60 = [p for _, p in _vel_hist]
                    _vel_prices_30 = [p for t, p in _vel_hist if now - t <= 30.0]
                    _vel_range_60 = max(_vel_prices_60) - min(_vel_prices_60)
                    _vel_range_30 = (max(_vel_prices_30) - min(_vel_prices_30)) if len(_vel_prices_30) >= 2 else 0.0
                    if _vel_range_60 >= 0.10 or _vel_range_30 >= 0.04:
                        _append_jsonl(
                            log_path,
                            {
                                "type": "entry_blocked",
                                "ts": now,
                                "session_id": session_id,
                                "reason": "leader_velocity_too_high",
                                "velocity_range": round(_vel_range_60, 4),
                                "velocity_range_30s": round(_vel_range_30, 4),
                                "velocity_window_secs": 60.0,
                                "threshold": 0.10,
                                "threshold_30s": 0.04,
                                "signal": signal,
                            },
                        )
                        time.sleep(poll_secs)
                        continue

                # Gates por variante: bloqueiam cenários historicamente negativos.
                # Derivados de 142 trades históricos (flat+trade_closed+redeem_flat).
                #
                # standard: ep >= 0.97 causa perda catastrófica quando o mercado resolve
                # errado (6 × 0.97 = $5.82 em risco vs. win máximo de $0.12). Adicionalmente
                # d_bps < 12 indica margem insuficiente — WR cai abaixo de breakeven.
                # Com ep<0.97 AND d_bps>=12: 14 trades históricos, WR=93%, PnL=+$1.63.
                #
                # dual_rich_late_limit: ep >= 0.985 são entradas a 0.99 onde o preço
                # de saída máximo também é 0.99 — geram breakeven (19/29 trades) ou stop.
                # Com ep<0.985: 7 trades, WR=100%, PnL=+$1.86.
                #
                # IMPORTANTE: usa entry_price (após ajuste hybrid), NÃO signal_entry_price.
                # Para dual_rich: signal.entry_price=0.98 mas hybrid posta a 0.99 (bid atual).
                # Verificar signal_entry_price bloquearia apenas 0 casos — o bug real.
                _gate_variant = str(signal.get("setup_variant") or "")
                _gate_ep      = entry_price
                _gate_d_bps   = _safe_float(signal.get("distance_to_price_to_beat_bps"), None)
                _gate_reason  = None
                if _gate_variant == "standard":
                    if _gate_ep >= 0.97:
                        _gate_reason = f"variant_gate:standard:ep_high:{_gate_ep:.3f}>=0.97"
                    elif _gate_d_bps is not None and _gate_d_bps < 12.0:
                        _gate_reason = f"variant_gate:standard:d_bps_low:{round(_gate_d_bps,1)}<12"
                elif _gate_variant == "dual_rich_late_limit":
                    if _gate_ep >= 0.985:
                        _gate_reason = f"variant_gate:dual_rich:ep_high:{_gate_ep:.3f}>=0.985"
                if _gate_reason:
                    _append_jsonl(log_path, {
                        "type": "entry_blocked",
                        "ts": now,
                        "session_id": session_id,
                        "reason": _gate_reason,
                        "entry_price": _gate_ep,
                        "d_bps": _gate_d_bps,
                        "signal": signal,
                    })
                    time.sleep(poll_secs)
                    continue

                planned_exit_risk = _exit_liquidity_risk(
                    current_snap,
                    current_exec,
                    side=side,
                    entry_price=entry_price,
                    stop_price=round(max(0.01, _safe_float(signal.get("stop_price"), entry_price - signal_cfg.stop_ticks * tick_size)), 6),
                    qty=float(qty),
                    tick_size=tick_size,
                )

                # Guarda contra ordens GTC obsoletas: cancela qualquer ordem aberta
                # para o token_id desta entrada antes de postar a nova.
                # Sem isso, uma ordem de iteração anterior ainda ativa no CLOB pode
                # preencher junto com a nova, dobrando a exposição sem log de entrada.
                _pre_entry_token_id = _token_id_for_side(current_snap, side)
                _stale_cancelled = _cancel_open_orders_for_token(broker, _pre_entry_token_id)
                if _stale_cancelled:
                    _append_jsonl(log_path, {
                        "type": "pre_entry_stale_cancel",
                        "ts": now,
                        "session_id": session_id,
                        "token_id": _pre_entry_token_id,
                        "cancelled_order_ids": _stale_cancelled,
                        "signal": signal,
                    })
                    # Ordens in-flight (sendo executadas no momento do cancel) não
                    # aparecem em get_open_orders() mas ainda podem preencher. O sleep
                    # aqui dá tempo para o fill assentar na blockchain antes do
                    # balance check — evita o double-entry onde ambos preenchem juntos.
                    time.sleep(max(poll_secs * 2, 1.0))

                # Guarda contra posição residual: se já há tokens na conta para este
                # token_id, não entrar — significa que uma entrada anterior não foi
                # registrada corretamente e ainda há exposição aberta.
                _pre_entry_balance = _token_balance_qty(broker, _pre_entry_token_id)
                if _pre_entry_balance > 0:
                    _append_jsonl(log_path, {
                        "type": "entry_blocked",
                        "ts": now,
                        "session_id": session_id,
                        "reason": f"pre_entry_balance_nonzero:{round(_pre_entry_balance, 4)}",
                        "token_id": _pre_entry_token_id,
                        "signal": signal,
                    })
                    time.sleep(poll_secs)
                    continue

                try:
                    trade = _post_entry_order(
                        broker,
                        signal=signal,
                        snap=current_snap,
                        qty=qty,
                        tick_size=tick_size,
                        now=now,
                        cfg=signal_cfg,
                        entry_price_override=entry_price,
                        order_style=order_style,
                        aggressive_entry_fak=aggressive_entry_fak,
                    )
                except Exception as exc:
                    if _is_transient_service_not_ready_exception(exc):
                        _append_jsonl(
                            log_path,
                            {
                                "type": "entry_transient_error",
                                "ts": now,
                                "session_id": session_id,
                                "stage": "initial_entry",
                                "error": f"{type(exc).__name__}: {_exception_message(exc)}",
                                "signal": signal,
                                "entry_order_style": order_style,
                                "posted_entry_price": entry_price,
                            },
                        )
                        time.sleep(poll_secs)
                        continue
                    if order_style == "aggressive_limit" and aggressive_entry_fak and _is_fak_no_match_exception(exc):
                        if event_slug:
                            blocked_entry_events[event_slug] = "initial_fak_no_match"
                        _append_jsonl(
                            log_path,
                            {
                                "type": "entry_fak_no_match",
                                "ts": now,
                                "session_id": session_id,
                                "stage": "initial_entry",
                                "error": f"{type(exc).__name__}: {_exception_message(exc)}",
                                "signal": signal,
                                "entry_order_style": order_style,
                                "posted_entry_price": entry_price,
                            },
                        )
                        time.sleep(poll_secs)
                        continue
                    raise
                _save_state(state_path, trade)
                _append_jsonl(
                    log_path,
                    {
                        "type": "enter",
                        "ts": now,
                        "session_id": session_id,
                        "signal": signal,
                        "hybrid_entry_enabled": hybrid_entry_enabled,
                        "entry_order_style": order_style,
                        "signal_entry_price": signal_entry_price,
                        "posted_entry_price": entry_price,
                        "planned_exit_risk": planned_exit_risk,
                        "btc_chart_context": _last_chart_ctx.summary() if _last_chart_ctx else None,
                        "btc_chart_penalty": round(_last_chart_ctx.get_penalty_for_side(side), 3) if _last_chart_ctx and side else None,
                        "bid_decel_gate": _decel_gate_for_entry,
                        "trade": _trade_summary(trade),
                    },
                )
                time.sleep(poll_secs)
                continue
            if trade.mode == "idle" and current_item and signal.get("allow") and counter_reversal_active:
                _append_jsonl(
                    log_path,
                    {
                        "type": "entry_blocked",
                        "ts": now,
                        "session_id": session_id,
                        "reason": "counter_reversal_active",
                        "signal": signal,
                    },
                )

            # Early Entry — bloco de entrada independente do sinal AR
            if trade.mode == "idle" and ee_enabled and current_item and not counter_reversal_active:
                _ee_up  = _safe_float((current_exec or {}).get("up_bid"), 0.0)
                _ee_dn  = _safe_float((current_exec or {}).get("down_bid"), 0.0)
                _ee_sl  = _ee_tracker.early_leader
                _ee_bid = (_ee_up if _ee_sl == "UP" else _ee_dn) if _ee_sl else 0.0
                # Gates de entrada baseados em análise dos logs reais (33 trades)
                _ee_n_s180         = len(_ee_tracker._s180)
                _ee_n_s180_blocked = (_ee_n_s180 < 3)
                _ee_secs_blocked   = (current_secs is not None and current_secs > 155)
                _ee_entry_blocked  = _ee_n_s180_blocked or _ee_secs_blocked
                _ee_gate_reason    = (
                    f"n_s180:{_ee_n_s180}<3" if _ee_n_s180_blocked else f"secs:{current_secs}>155"
                ) if _ee_entry_blocked else ""
                if (
                    _ee_tracker.signal_ok
                    and current_secs is not None
                    and EE_MIN_ENTRY_SECS <= current_secs <= EE_MAX_ENTRY_SECS
                    and EE_ENTRY_LO <= _ee_bid <= EE_ENTRY_HI
                    and not _ee_entry_blocked
                ):
                    _ee_event_slug = str(current_item.get("slug") or "")
                    _ee_token_id   = _token_id_for_side(current_snap, _ee_sl)
                    _ee_tick       = _tick_size_from_snap(current_snap, _ee_sl)
                    _ee_stale = _cancel_open_orders_for_token(broker, _ee_token_id)
                    if _ee_stale:
                        _append_jsonl(log_path, {
                            "type": "pre_entry_stale_cancel", "ts": now,
                            "session_id": session_id, "source": "early_entry",
                            "token_id": _ee_token_id, "cancelled_order_ids": _ee_stale,
                        })
                        time.sleep(max(poll_secs * 2, 1.0))
                    _ee_balance = _token_balance_qty(broker, _ee_token_id)
                    if _ee_balance > 0:
                        _append_jsonl(log_path, {
                            "type": "entry_blocked", "ts": now,
                            "session_id": session_id,
                            "reason": f"ee_pre_entry_balance_nonzero:{round(_ee_balance, 4)}",
                            "source": "early_entry",
                        })
                    elif not _has_sufficient_collateral_for_entry(broker, entry_price=_ee_bid, qty=float(qty)):
                        _append_jsonl(log_path, {
                            "type": "entry_blocked", "ts": now,
                            "session_id": session_id,
                            "reason": "ee_insufficient_collateral",
                            "source": "early_entry",
                        })
                    elif not ee_posts_enabled:
                        # Shadow mode: loga sinal EE sem postar ordem real
                        _append_jsonl(log_path, {
                            "type": "ee_shadow_entry", "ts": now,
                            "session_id": session_id,
                            "slug": _ee_event_slug,
                            "side": _ee_sl, "ep": round(_ee_bid, 4),
                            "secs": current_secs,
                            "el": _ee_tracker.state_dict(),
                        })
                        print(
                            f"[EE_SHADOW] {_ee_sl}  ep={_ee_bid:.3f}  "
                            f"vel={_ee_tracker.el_vel:+.3f}  secs={current_secs}"
                        )
                    else:
                        _ee_signal = {
                            "side": _ee_sl,
                            "event_slug": _ee_event_slug,
                            "entry_price": _ee_bid,
                            "setup_variant": "early_entry",
                            "stop_price": EE_STOP_LEVEL,
                            "exit_price": EE_PROFIT_PROTECT_BID,
                        }
                        try:
                            trade = _post_entry_order(
                                broker,
                                signal=_ee_signal,
                                snap=current_snap,
                                qty=qty,
                                tick_size=_ee_tick,
                                now=now,
                                cfg=signal_cfg,
                                entry_price_override=_ee_bid,
                                order_style="direct_limit",
                                aggressive_entry_fak=False,
                            )
                            _save_state(state_path, trade)
                            _append_jsonl(log_path, {
                                "type": "ee_real_entry", "ts": now,
                                "session_id": session_id,
                                "slug": _ee_event_slug,
                                "side": _ee_sl, "ep": round(_ee_bid, 4),
                                "secs": current_secs,
                                "el": _ee_tracker.state_dict(),
                                "trade": _trade_summary(trade),
                            })
                            print(
                                f"[EE_REAL] ENTRADA {_ee_sl}  ep={_ee_bid:.3f}  "
                                f"vel={_ee_tracker.el_vel:+.3f}  secs={current_secs}"
                            )
                        except Exception as _ee_exc:
                            _append_jsonl(log_path, {
                                "type": "ee_entry_error", "ts": now,
                                "session_id": session_id,
                                "error": f"{type(_ee_exc).__name__}: {_exception_message(_ee_exc)}",
                                "slug": _ee_event_slug,
                            })
                elif (
                    _ee_entry_blocked
                    and _ee_tracker.signal_ok
                    and current_secs is not None
                    and EE_MIN_ENTRY_SECS <= current_secs <= EE_MAX_ENTRY_SECS
                    and EE_ENTRY_LO <= _ee_bid <= EE_ENTRY_HI
                ):
                    _append_jsonl(log_path, {
                        "type": "ee_entry_blocked", "ts": now,
                        "session_id": session_id,
                        "slug": str(current_item.get("slug") or ""),
                        "reason": f"gate:{_ee_gate_reason}",
                        "ep": round(_ee_bid, 4), "n_s180": _ee_n_s180, "secs": current_secs,
                    })
                    print(f"[EE_REAL] GATE_BLOCKED  {_ee_gate_reason}  ep={_ee_bid:.3f}  secs={current_secs}")

            if trade.mode in ("pending_entry", "open_position", "exit_pending_confirm"):
                trade = _sync_entry_order(broker, trade)
                trade.updated_at = now
                _save_state(state_path, trade)

            if trade.mode == "pending_entry":
                if trade.entry_qty_filled > 0:
                    resp = _cancel_if_live(broker, trade.entry_order_id)
                    trade.mode = "open_position"
                    trade.updated_at = now
                    trade.last_reason = "entry_fill_detected"
                    _save_state(state_path, trade)
                    # Detect fills where the signal is already invalid or bid already breached stop.
                    # Log separately so these can be identified in analysis.
                    _fill_bid = active_bid
                    _fill_stop = _safe_float(trade.stop_price, 0.0)
                    _fill_signal_ok = bool(signal.get("allow")) and signal.get("side") == trade.side
                    _fill_at_stop = _fill_stop > 0 and _fill_bid > 0 and _fill_bid <= _fill_stop
                    _fill_event = "fill_on_invalid_signal" if (not _fill_signal_ok or _fill_at_stop) else "fill"
                    _append_jsonl(log_path, {"type": _fill_event, "ts": now, "session_id": session_id, "cancel_remainder": resp, "fill_bid": _fill_bid, "fill_signal_ok": _fill_signal_ok, "fill_at_stop": _fill_at_stop, "trade": _trade_summary(trade)})
                    # Detecta double entry: ordem GTC obsoleta preencheu junto com a nova.
                    # Threshold 1.5x: margem para fills parciais legítimos vs. double entry claro.
                    _double_entry_threshold = _safe_float(trade.entry_qty_requested, 0.0) * 1.5
                    if _double_entry_threshold > 0 and trade.entry_qty_filled > _double_entry_threshold:
                        _append_jsonl(log_path, {
                            "type": "double_entry_detected",
                            "ts": now,
                            "session_id": session_id,
                            "entry_qty_requested": trade.entry_qty_requested,
                            "entry_qty_filled": trade.entry_qty_filled,
                            "excess_qty": round(trade.entry_qty_filled - _safe_float(trade.entry_qty_requested, 0.0), 4),
                            "trade": _trade_summary(trade),
                        })
                elif trade.entry_order_style in ("patient_limit", "passive_limit"):
                    current_slug = str(current_item.get("slug") or "") if current_item else ""
                    signal_still_valid = (
                        bool(current_item)
                        and not counter_reversal_active
                        and bool(signal.get("allow"))
                        and signal.get("side") == trade.side
                        and signal.get("event_slug") == trade.event_slug
                    )
                    event_rolled = bool(current_slug and trade.event_slug and current_slug != trade.event_slug)
                    last_second = current_secs is not None and current_secs <= resolution_settle_secs
                    if event_rolled or last_second or not signal_still_valid:
                        cancel_reason = (
                            "passive_event_rolled"
                            if event_rolled
                            else "passive_last_second"
                            if last_second
                            else "passive_signal_invalidated"
                        )

                        # Runaway aggressive chase: signal became invalid because market
                        # moved in the correct direction without revisiting our passive limit.
                        # Cancel the passive and replace with a FAK at the current ask.
                        _runaway = None
                        if (
                            cancel_reason == "passive_signal_invalidated"
                            and hybrid_entry_enabled
                            and trade.entry_qty_filled <= 0
                        ):
                            _runaway = _runaway_chase_params(
                                signal,
                                trade,
                                current_secs=current_secs,
                                max_chase_price=runaway_chase_max_price,
                                max_chase_distance=runaway_chase_max_distance,
                                min_secs=runaway_chase_min_secs,
                            )

                        if _runaway and trade.entry_qty_filled <= 0:
                            _runaway_cancel_resp = _cancel_if_live(broker, trade.entry_order_id)
                            # Re-check: did the passive fill during cancel?
                            _synced = _sync_entry_order(broker, trade)
                            if _synced.entry_qty_filled > 0:
                                trade = _synced
                                trade.mode = "open_position"
                                trade.updated_at = now
                                trade.last_reason = "runaway_passive_filled_before_chase"
                                _save_state(state_path, trade)
                                _append_jsonl(log_path, {"type": "fill", "ts": now, "session_id": session_id, "cancel_remainder": _runaway_cancel_resp, "fill_bid": active_bid, "fill_signal_ok": False, "fill_at_stop": False, "trade": _trade_summary(trade)})
                                time.sleep(poll_secs)
                                continue
                            _runaway_from = _trade_summary(trade)
                            _runaway_chase_price = _runaway["ask"]
                            _runaway_tick_size = _tick_size_from_snap(current_snap, trade.side or "UP")
                            # Preserve original trade side — signal may have lost it if market
                            # moved outside the entry window during the cancel/recheck cycle.
                            _chase_signal = {**signal, "side": trade.side}
                            try:
                                trade = _post_entry_order(
                                    broker,
                                    signal=_chase_signal,
                                    snap=current_snap,
                                    qty=qty,
                                    tick_size=_runaway_tick_size,
                                    now=now,
                                    cfg=signal_cfg,
                                    entry_price_override=_runaway_chase_price,
                                    order_style="aggressive_limit",
                                    aggressive_entry_fak=True,
                                )
                                _save_state(state_path, trade)
                                _append_jsonl(log_path, {
                                    "type": "entry_runaway_chase",
                                    "ts": now,
                                    "session_id": session_id,
                                    "cancel": _runaway_cancel_resp,
                                    "from_trade": _runaway_from,
                                    "chase_price": _runaway_chase_price,
                                    "chase_distance": _runaway["chase_distance"],
                                    "runaway": _runaway,
                                    "signal": signal,
                                    "trade": _trade_summary(trade),
                                })
                            except Exception as _runaway_exc:
                                _is_no_match = aggressive_entry_fak and _is_fak_no_match_exception(_runaway_exc)
                                if trade.event_slug:
                                    blocked_entry_events[str(trade.event_slug)] = "runaway_fak_no_match" if _is_no_match else "runaway_chase_error"
                                _append_jsonl(log_path, {
                                    "type": "entry_runaway_chase_failed",
                                    "ts": now,
                                    "session_id": session_id,
                                    "cancel": _runaway_cancel_resp,
                                    "from_trade": _runaway_from,
                                    "chase_price": _runaway_chase_price,
                                    "error": f"{type(_runaway_exc).__name__}: {_exception_message(_runaway_exc)}",
                                    "signal": signal,
                                })
                                trade = LiveCurrentAlmostResolvedTradeState()
                                _clear_state(state_path)
                            time.sleep(poll_secs)
                            continue

                        resp = _cancel_if_live(broker, trade.entry_order_id)
                        if trade.event_slug:
                            blocked_entry_events[trade.event_slug] = cancel_reason
                        _append_jsonl(
                            log_path,
                            {
                                "type": "entry_cancel",
                                "ts": now,
                                "session_id": session_id,
                                "reason": cancel_reason,
                                "response": resp,
                                "trade": _trade_summary(trade),
                                "signal": signal,
                            },
                        )
                        trade = LiveCurrentAlmostResolvedTradeState()
                        _clear_state(state_path)
                    elif hybrid_entry_enabled and now - trade.created_at >= hybrid_aggressive_after_secs:
                        aggressive_price = _ask_for_side(current_exec, trade.side or "")
                        if aggressive_price <= 0:
                            aggressive_price = _safe_float(signal.get("entry_price"), 0.0)
                        if _safe_to_chase_aggressive_entry(
                            signal,
                            ask_now=aggressive_price,
                            current_secs=current_secs,
                            max_price=hybrid_aggressive_max_price,
                        ):
                            previous_entry_order_id = trade.entry_order_id
                            cancel_resp = _cancel_if_live(broker, trade.entry_order_id)
                            previous_order = _get_order_status(broker, previous_entry_order_id)
                            previous_status = str(getattr(previous_order, "status", "") or "").lower() if previous_order is not None else ""
                            if previous_order is not None and previous_status in ("filled", "matched", "closed", "resolved"):
                                trade = _sync_entry_order(broker, trade)
                                _save_state(state_path, trade)
                                _append_jsonl(
                                    log_path,
                                    {
                                        "type": "entry_replace_blocked_previous_filled",
                                        "ts": now,
                                        "session_id": session_id,
                                        "cancel": cancel_resp,
                                        "previous_order": previous_order.as_dict(),
                                        "trade": _trade_summary(trade),
                                        "signal": signal,
                                    },
                                )
                                time.sleep(poll_secs)
                                continue
                            if previous_order is not None and previous_status not in ("canceled", "cancelled", "rejected"):
                                _append_jsonl(
                                    log_path,
                                    {
                                        "type": "entry_replace_blocked_cancel_unconfirmed",
                                        "ts": now,
                                        "session_id": session_id,
                                        "cancel": cancel_resp,
                                        "previous_order": previous_order.as_dict(),
                                        "aggressive_price": aggressive_price,
                                        "trade": _trade_summary(trade),
                                        "signal": signal,
                                    },
                                )
                                time.sleep(poll_secs)
                                continue
                            replaced_from = _trade_summary(trade)
                            tick_size = _tick_size_from_snap(current_snap, trade.side or "UP")
                            try:
                                trade = _post_entry_order(
                                    broker,
                                    signal=signal,
                                    snap=current_snap,
                                    qty=qty,
                                    tick_size=tick_size,
                                    now=now,
                                    cfg=signal_cfg,
                                    entry_price_override=round(aggressive_price, 6),
                                    order_style="aggressive_limit",
                                    aggressive_entry_fak=aggressive_entry_fak,
                                )
                            except Exception as exc:
                                if _is_transient_service_not_ready_exception(exc):
                                    trade = LiveCurrentAlmostResolvedTradeState()
                                    _clear_state(state_path)
                                    _append_jsonl(
                                        log_path,
                                        {
                                            "type": "entry_transient_error",
                                            "ts": now,
                                            "session_id": session_id,
                                            "stage": "replace_aggressive",
                                            "cancel": cancel_resp,
                                            "from_trade": replaced_from,
                                            "aggressive_price": aggressive_price,
                                            "error": f"{type(exc).__name__}: {_exception_message(exc)}",
                                            "signal": signal,
                                        },
                                    )
                                    time.sleep(poll_secs)
                                    continue
                                if aggressive_entry_fak and _is_fak_no_match_exception(exc):
                                    if replaced_from.get("event_slug"):
                                        blocked_entry_events[str(replaced_from.get("event_slug"))] = "replace_fak_no_match"
                                    trade = LiveCurrentAlmostResolvedTradeState()
                                    _clear_state(state_path)
                                    _append_jsonl(
                                        log_path,
                                        {
                                            "type": "entry_fak_no_match",
                                            "ts": now,
                                            "session_id": session_id,
                                            "stage": "replace_aggressive",
                                            "cancel": cancel_resp,
                                            "from_trade": replaced_from,
                                            "aggressive_price": aggressive_price,
                                            "error": f"{type(exc).__name__}: {_exception_message(exc)}",
                                            "signal": signal,
                                        },
                                    )
                                    time.sleep(poll_secs)
                                    continue
                                raise
                            _save_state(state_path, trade)
                            _append_jsonl(
                                log_path,
                                {
                                    "type": "entry_replace_aggressive_limit",
                                    "ts": now,
                                    "session_id": session_id,
                                    "cancel": cancel_resp,
                                    "from_trade": replaced_from,
                                    "aggressive_price": aggressive_price,
                                    "signal": signal,
                                    "trade": _trade_summary(trade),
                                },
                            )
                elif now - trade.created_at >= entry_timeout_secs or (
                    current_secs is not None
                    and current_secs
                    <= (
                        resolution_settle_secs
                        if trade.setup_variant == "extreme_99_limit"
                        else signal_cfg.min_secs_to_end
                    )
                ):
                    resp = _cancel_if_live(broker, trade.entry_order_id)
                    if trade.event_slug:
                        blocked_entry_events[trade.event_slug] = "entry_timeout_no_fill"
                    _append_jsonl(log_path, {"type": "entry_cancel", "ts": now, "session_id": session_id, "reason": "entry_timeout_no_fill", "response": resp, "trade": _trade_summary(trade)})
                    trade = LiveCurrentAlmostResolvedTradeState()
                    _clear_state(state_path)

            if trade.mode == "open_position" and trade.side:
                # External-close detection: if balance hit 0 while we think we're open,
                # the position was closed externally (market resolved as loss, manual exit,
                # or a resolution event not yet reflected in the slot bundle).
                # Use a 20s grace period to absorb blockchain propagation lag after entry.
                _ext_balance = _token_balance_qty(broker, trade.token_id)
                _secs_since_entry = now - _safe_float(trade.created_at, now)
                if _is_flat_qty(_ext_balance) and _secs_since_entry >= 20.0:
                    _ext_ep = _safe_float(trade.entry_price, 0.0)
                    _ext_qty = _safe_float(trade.entry_qty_filled, 0.0)
                    _ext_pnl = round((0.0 - _ext_ep) * _ext_qty, 4) if _ext_ep > 0 and _ext_qty > 0 else None
                    _append_jsonl(
                        log_path,
                        {
                            "type": "external_close_detected",
                            "ts": now,
                            "session_id": session_id,
                            "token_balance_qty": _ext_balance,
                            "secs_since_entry": round(_secs_since_entry, 1),
                            "exit_price": 0.0,
                            "pnl_usd": _ext_pnl,
                            "trade": _trade_summary(trade),
                        },
                    )
                    print(f"[GUARD] External close detected — balance=0 after {round(_secs_since_entry, 1)}s. Resetting to idle.")
                    if trade.event_slug and _safe_float(trade.entry_qty_filled) > 0:
                        _reentry_blocked_until[str(trade.event_slug)] = now + reentry_cooldown_secs
                    trade = LiveCurrentAlmostResolvedTradeState()
                    _clear_state(state_path)
                    time.sleep(poll_secs)
                    continue

                tick_size = _tick_size_from_snap(current_snap, trade.side)
                if _should_hold_to_resolution(signal, bid_now=active_bid, secs_to_end=current_secs, cfg=signal_cfg, side=trade.side):
                    trade.hold_to_resolution = True
                signed_distance = _safe_float(signal.get("signed_distance_from_open_bps"), 0.0)
                reached_resolution = (
                    hold_winner_to_resolution
                    and (
                        (current_secs is not None and current_secs <= resolution_settle_secs)
                        or (current_item and trade.event_slug and current_item.get("slug") != trade.event_slug)
                    )
                )
                platform_redeem_path = _should_await_platform_redeem(
                    trade,
                    current_secs=current_secs,
                    current_slug=str(current_item.get("slug") or "") if current_item else None,
                    hold_winner_to_resolution=hold_winner_to_resolution,
                )
                if reached_resolution or platform_redeem_path:
                    side_won = _side_winning(trade.side, signed_distance)
                    awaiting_reason = (
                        "platform_redeem_path_awaiting_final_balance"
                        if platform_redeem_path and not reached_resolution
                        else "resolution_win_awaiting_redeem" if side_won else "resolution_loss_or_unknown_awaiting_final_balance"
                    )
                    trade = _mark_awaiting_redeem(
                        trade,
                        now=now,
                        reason=awaiting_reason,
                    )
                    _save_state(state_path, trade)
                    _append_jsonl(
                        log_path,
                        {
                            "type": "awaiting_redeem",
                            "ts": now,
                            "session_id": session_id,
                            "side_won": side_won,
                            "signed_distance_from_open_bps": signed_distance,
                            "active_bid": active_bid,
                            "platform_redeem_path": platform_redeem_path,
                            "trade": _trade_summary(trade),
                        },
                    )
                else:
                    _emerg_reason = _check_emergency_exit(
                        side=trade.side,
                        active_bid=active_bid,
                        secs_to_end=current_secs,
                        oracle_price=_last_oracle_price,
                        oracle_open_price=_oracle_open_prices.get(trade.event_slug),
                        oracle_staleness_secs=_last_oracle_staleness,
                        near_win_enabled=near_win_exit_enabled,
                        near_win_threshold=near_win_exit_threshold,
                        near_win_secs=near_win_exit_secs,
                        oracle_margin_enabled=oracle_margin_exit_enabled,
                        oracle_margin_bps_threshold=oracle_margin_exit_bps,
                        oracle_margin_secs=oracle_margin_exit_secs,
                    )
                    if _emerg_reason and trade.setup_variant != "early_entry":
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=active_bid,
                            now=now,
                            reason=_emerg_reason,
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                        _save_state(state_path, trade)
                        _append_jsonl(log_path, {"type": "exit_posted", "ts": now, "session_id": session_id, "reason": _emerg_reason, "trade": _trade_summary(trade)})
                    elif trade.setup_variant == "early_entry":
                        # EE v2 exit logic: stop 0.65, PP 0.88@secs<=70, win, reversal, hedge
                        _ee_opp_side_now = "DOWN" if (trade.side or "UP") == "UP" else "UP"
                        _ee_opp_bid_now  = _bid_for_side(current_exec, _ee_opp_side_now)
                        # Verificar fill de hedge se pendente
                        if trade.hedge_order_id and not trade.hedge_qty_filled and trade.hedge_token_id:
                            _hbal = _token_balance_qty(broker, trade.hedge_token_id)
                            if _hbal > 0:
                                trade.hedge_qty_filled = _hbal
                                trade.updated_at = now
                                trade = _mark_awaiting_redeem(trade, now=now, reason="ee_hedge_filled_awaiting_redeem")
                                _save_state(state_path, trade)
                                _append_jsonl(log_path, {
                                    "type": "ee_hedge_filled", "ts": now,
                                    "session_id": session_id,
                                    "hedge_qty": round(_hbal, 6),
                                    "hedge_price": trade.hedge_price,
                                    "trade": _trade_summary(trade),
                                })
                                print(f"[EE_REAL] HEDGE_FILLED  qty={round(_hbal, 6)}  price={trade.hedge_price}")
                        else:
                            ee_reason = _ee_exit_reason(
                                trade,
                                el_bid=active_bid,
                                opp_bid=_ee_opp_bid_now,
                                secs_to_end=current_secs,
                            )
                            if ee_reason == "ee_win":
                                trade = _mark_awaiting_redeem(trade, now=now, reason="resolution_win_awaiting_redeem")
                                _save_state(state_path, trade)
                                _append_jsonl(log_path, {
                                    "type": "ee_win", "ts": now, "session_id": session_id,
                                    "el_bid": round(active_bid, 4), "secs": current_secs,
                                    "trade": _trade_summary(trade),
                                })
                                print(f"[EE_REAL] WIN  el_bid={active_bid:.3f}  secs={current_secs}")
                            elif ee_reason in ("ee_reversal", "ee_stop"):
                                trade = _post_exit_order(
                                    broker, trade, exit_price=active_bid, now=now,
                                    reason=ee_reason, min_limit_exit_qty=min_limit_exit_qty,
                                )
                                _save_state(state_path, trade)
                                _append_jsonl(log_path, {
                                    "type": "exit_posted", "ts": now, "session_id": session_id,
                                    "reason": ee_reason, "trade": _trade_summary(trade),
                                })
                                print(f"[EE_REAL] {ee_reason.upper()}  el_bid={active_bid:.3f}  secs={current_secs}")
                            elif ee_reason == "ee_profit_protect":
                                trade = _post_exit_order(
                                    broker, trade, exit_price=active_bid, now=now,
                                    reason=ee_reason, min_limit_exit_qty=min_limit_exit_qty,
                                )
                                _save_state(state_path, trade)
                                _append_jsonl(log_path, {
                                    "type": "exit_posted", "ts": now, "session_id": session_id,
                                    "reason": ee_reason, "trade": _trade_summary(trade),
                                })
                                print(f"[EE_REAL] PROFIT_PROTECT  el_bid={active_bid:.3f}  secs={current_secs}")
                            elif ee_reason == "ee_hedge_gap":
                                _h_ep  = _safe_float(trade.entry_price, 0.0)
                                _h_qty = _safe_float(trade.entry_qty_filled, 0.0)
                                _stop_pnl  = (active_bid - _h_ep) * _h_qty
                                _hedge_pnl = (1.0 - _h_ep - _ee_opp_bid_now) * _h_qty
                                if _hedge_pnl > _stop_pnl and _ee_opp_bid_now > 0 and _h_qty > 0:
                                    _hedge_side = _ee_opp_side_now
                                    _htok   = _token_id_for_side(current_snap, _hedge_side)
                                    _htick  = _tick_size_from_snap(current_snap, _hedge_side)
                                    _hprice = _clamp_limit_price(_ee_opp_bid_now, tick_size=_htick)
                                    _hreq   = BrokerOrderRequest(
                                        token_id=_htok, side="BUY", price=_hprice,
                                        size=_h_qty, order_type="GTC",
                                        market_slug=trade.event_slug, outcome=_hedge_side,
                                        client_order_key=f"ee_hedge:{int(now)}:{_hedge_side}",
                                    )
                                    try:
                                        _hord = broker.place_limit_order(_hreq)
                                        trade.hedge_token_id = _htok
                                        trade.hedge_order_id = _hord.order_id
                                        trade.hedge_price    = _hprice
                                        trade.updated_at     = now
                                        trade.last_reason    = f"ee_hedge_posted:{_hedge_side}:{_hprice}"
                                        _save_state(state_path, trade)
                                        _append_jsonl(log_path, {
                                            "type": "ee_hedge_posted", "ts": now,
                                            "session_id": session_id,
                                            "hedge_side": _hedge_side, "hedge_price": _hprice,
                                            "stop_pnl": round(_stop_pnl, 4),
                                            "hedge_pnl": round(_hedge_pnl, 4),
                                            "trade": _trade_summary(trade),
                                        })
                                        print(f"[EE_REAL] HEDGE  opp={_hedge_side}  opp_bid={_ee_opp_bid_now:.3f}")
                                    except Exception as _hg_exc:
                                        trade = _post_exit_order(
                                            broker, trade, exit_price=active_bid, now=now,
                                            reason="ee_stop_hedge_fallback",
                                            min_limit_exit_qty=min_limit_exit_qty,
                                        )
                                        _save_state(state_path, trade)
                                        _append_jsonl(log_path, {
                                            "type": "exit_posted", "ts": now,
                                            "session_id": session_id,
                                            "reason": "ee_stop_hedge_fallback",
                                            "error": f"{type(_hg_exc).__name__}: {_exception_message(_hg_exc)}",
                                            "trade": _trade_summary(trade),
                                        })
                                else:
                                    trade = _post_exit_order(
                                        broker, trade, exit_price=active_bid, now=now,
                                        reason="ee_stop_hedge_gap",
                                        min_limit_exit_qty=min_limit_exit_qty,
                                    )
                                    _save_state(state_path, trade)
                                    _append_jsonl(log_path, {
                                        "type": "exit_posted", "ts": now,
                                        "session_id": session_id,
                                        "reason": "ee_stop_hedge_gap",
                                        "trade": _trade_summary(trade),
                                    })
                    else:
                        # Verificar fill de hedge AR pendente
                        if trade.hedge_order_id and not trade.hedge_qty_filled and trade.hedge_token_id:
                            _hbal = _token_balance_qty(broker, trade.hedge_token_id)
                            if _hbal > 0:
                                trade.hedge_qty_filled = _hbal
                                trade.updated_at = now
                                trade = _mark_awaiting_redeem(trade, now=now, reason="ar_hedge_filled_awaiting_redeem")
                                _save_state(state_path, trade)
                                _append_jsonl(log_path, {
                                    "type": "ar_hedge_filled", "ts": now,
                                    "session_id": session_id,
                                    "hedge_qty": round(_hbal, 6),
                                    "hedge_price": trade.hedge_price,
                                    "trade": _trade_summary(trade),
                                })
                        else:
                            reason = _exit_reason(
                                trade,
                                bid_now=active_bid,
                                tick_size=tick_size,
                                now=now,
                                secs_to_end=current_secs,
                                signal=signal,
                                cfg=signal_cfg,
                                flatten_deadline_secs=flatten_deadline_secs,
                                hold_winner_to_resolution=hold_winner_to_resolution,
                            )
                            if reason:
                                if reason == "mid_book_reversal" and active_bid < 0.50 and current_exec:
                                    # bid cruzou 0.50 — calcular se hedge supera stop
                                    _ar_opp_side = "DOWN" if (trade.side or "UP") == "UP" else "UP"
                                    _ar_opp_bid  = _bid_for_side(current_exec, _ar_opp_side)
                                    _ar_ep   = _safe_float(trade.entry_price, 0.0)
                                    _ar_qty  = _safe_float(trade.entry_qty_filled, 0.0)
                                    _ar_stop_pnl  = (active_bid - _ar_ep) * _ar_qty
                                    _ar_hedge_pnl = (1.0 - _ar_ep - _ar_opp_bid) * _ar_qty if _ar_opp_bid > 0 else float("-inf")
                                    if _ar_hedge_pnl > _ar_stop_pnl and _ar_opp_bid > 0 and _ar_qty > 0:
                                        _ar_opp_tok  = _token_id_for_side(current_snap, _ar_opp_side)
                                        _ar_opp_tick = _tick_size_from_snap(current_snap, _ar_opp_side)
                                        _ar_hprice   = _clamp_limit_price(_ar_opp_bid, tick_size=_ar_opp_tick)
                                        try:
                                            _ar_hreq = BrokerOrderRequest(
                                                token_id=_ar_opp_tok, side="BUY", price=_ar_hprice,
                                                size=_ar_qty, order_type="GTC",
                                                market_slug=trade.event_slug, outcome=_ar_opp_side,
                                                client_order_key=f"ar_hedge:{int(now)}:{_ar_opp_side}",
                                            )
                                            _ar_hord = broker.place_limit_order(_ar_hreq)
                                            trade.hedge_token_id = _ar_opp_tok
                                            trade.hedge_order_id = _ar_hord.order_id
                                            trade.hedge_price    = _ar_hprice
                                            trade.updated_at     = now
                                            trade.last_reason    = f"ar_hedge_posted:{_ar_opp_side}:{_ar_hprice}"
                                            _save_state(state_path, trade)
                                            _append_jsonl(log_path, {
                                                "type": "ar_hedge_posted", "ts": now,
                                                "session_id": session_id,
                                                "hedge_side": _ar_opp_side, "hedge_price": _ar_hprice,
                                                "stop_pnl": round(_ar_stop_pnl, 4),
                                                "hedge_pnl": round(_ar_hedge_pnl, 4),
                                                "trade": _trade_summary(trade),
                                            })
                                        except Exception as _ar_hg_exc:
                                            trade = _post_exit_order(
                                                broker, trade, exit_price=active_bid, now=now,
                                                reason="mid_book_reversal_hedge_fallback",
                                                min_limit_exit_qty=min_limit_exit_qty,
                                            )
                                            _save_state(state_path, trade)
                                            _append_jsonl(log_path, {
                                                "type": "exit_posted", "ts": now,
                                                "session_id": session_id,
                                                "reason": "mid_book_reversal_hedge_fallback",
                                                "error": f"{type(_ar_hg_exc).__name__}: {_exception_message(_ar_hg_exc)}",
                                                "trade": _trade_summary(trade),
                                            })
                                    else:
                                        trade = _post_exit_order(
                                            broker, trade, exit_price=active_bid, now=now,
                                            reason="mid_book_reversal",
                                            min_limit_exit_qty=min_limit_exit_qty,
                                        )
                                        _save_state(state_path, trade)
                                        _append_jsonl(log_path, {"type": "exit_posted", "ts": now, "session_id": session_id, "reason": "mid_book_reversal", "trade": _trade_summary(trade)})
                                else:
                                    trade = _post_exit_order(
                                        broker,
                                        trade,
                                        exit_price=active_bid,
                                        now=now,
                                        reason=reason,
                                        min_limit_exit_qty=min_limit_exit_qty,
                                    )
                                    _save_state(state_path, trade)
                                    _append_jsonl(log_path, {"type": "exit_posted", "ts": now, "session_id": session_id, "reason": reason, "trade": _trade_summary(trade)})

            if trade.mode == "awaiting_redeem":
                token_balance_qty = _token_balance_qty(broker, trade.token_id)
                collateral_balance = _collateral_balance_usd(broker)
                redeem_result = None
                if auto_redeem_enabled and now - _safe_float(trade.redeem_attempted_at, 0.0) >= 10.0:
                    redeem_result = _attempt_redeem_if_available(broker, trade)
                    trade.redeem_attempted_at = now
                    trade.updated_at = now
                    _save_state(state_path, trade)
                _append_jsonl(
                    log_path,
                    {
                        "type": "awaiting_redeem_status",
                        "ts": now,
                        "session_id": session_id,
                        "token_balance_qty": token_balance_qty,
                        "collateral_balance_usd": collateral_balance,
                        "auto_redeem_enabled": auto_redeem_enabled,
                        "redeem_result": redeem_result,
                        "trade": _trade_summary(trade),
                    },
                )
                if _is_flat_qty(token_balance_qty):
                    # Guard: the balance API can return 0 for ~10-15s after a fill
                    # while the blockchain state propagates. If we know the position
                    # was filled (entry_qty_filled > 0), wait for the API to catch up
                    # before declaring the trade closed — a premature reset here leaves
                    # real shares unmanaged.
                    known_filled = _safe_float(trade.entry_qty_filled, 0.0) > 0
                    secs_since_resolution = now - _safe_float(trade.resolution_detected_at, now)
                    if known_filled and secs_since_resolution < 15.0:
                        _append_jsonl(log_path, {"type": "awaiting_redeem_balance_lag", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "entry_qty_filled": trade.entry_qty_filled, "secs_since_resolution": round(secs_since_resolution, 2), "trade": _trade_summary(trade)})
                    else:
                        _rdm_ep = _safe_float(trade.entry_price, 0.0)
                        _rdm_qty = _safe_float(trade.entry_qty_filled, 0.0)
                        _rdm_reason = str(trade.last_reason or "")
                        _rdm_exit_price: Optional[float] = None
                        if "resolution_win" in _rdm_reason:
                            _rdm_exit_price = 1.0
                        elif "resolution_loss" in _rdm_reason or (collateral_balance == 0 and _rdm_qty > 0):
                            _rdm_exit_price = 0.0
                        if trade.hedge_token_id and _safe_float(trade.hedge_price) > 0:
                            # EE hedge: net PnL = (1.0 - entry_price - hedge_price) * qty
                            _rdm_pnl = round((1.0 - _rdm_ep - _safe_float(trade.hedge_price)) * _rdm_qty, 4) if _rdm_ep > 0 and _rdm_qty > 0 else None
                        else:
                            _rdm_pnl = round((_rdm_exit_price - _rdm_ep) * _rdm_qty, 4) if _rdm_exit_price is not None and _rdm_ep > 0 and _rdm_qty > 0 else None
                        _append_jsonl(log_path, {"type": "redeem_flat", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "collateral_balance_usd": collateral_balance, "exit_price": _rdm_exit_price, "pnl_usd": _rdm_pnl, "trade": _trade_summary(trade)})
                        _append_jsonl(log_path, {
                            "type": "trade_closed", "ts": now, "session_id": session_id,
                            "source": "redeem_flat",
                            "side": trade.side, "entry_price": _rdm_ep,
                            "exit_price": _rdm_exit_price, "qty": _rdm_qty, "pnl_usd": _rdm_pnl,
                            "entry_slug": trade.event_slug,
                            "last_reason": _rdm_reason,
                        })
                        if trade.event_slug and _safe_float(trade.entry_qty_filled) > 0:
                            _reentry_blocked_until[str(trade.event_slug)] = now + reentry_cooldown_secs
                        trade = LiveCurrentAlmostResolvedTradeState()
                        _clear_state(state_path)
                elif 0 < token_balance_qty <= dust_archive_qty:
                    _append_jsonl(
                        log_path,
                        {
                            "type": "redeem_dust_archived",
                            "ts": now,
                            "session_id": session_id,
                            "token_balance_qty": token_balance_qty,
                            "dust_archive_qty": dust_archive_qty,
                            "trade": _trade_summary(trade),
                        },
                    )
                    if trade.event_slug and _safe_float(trade.entry_qty_filled) > 0:
                        _reentry_blocked_until[str(trade.event_slug)] = now + reentry_cooldown_secs
                    trade = LiveCurrentAlmostResolvedTradeState()
                    _clear_state(state_path)
                else:
                    _secs_since_res = now - _safe_float(trade.resolution_detected_at, now)
                    _loss_reason = "resolution_loss" in str(trade.last_reason or "")
                    if _loss_reason and _secs_since_res >= loss_writeoff_timeout_secs:
                        _wo_ep = _safe_float(trade.entry_price, 0.0)
                        _wo_qty = _safe_float(trade.entry_qty_filled, 0.0)
                        _wo_pnl = round((0.0 - _wo_ep) * _wo_qty, 4) if _wo_ep > 0 and _wo_qty > 0 else None
                        _append_jsonl(
                            log_path,
                            {
                                "type": "resolution_loss_writeoff",
                                "ts": now,
                                "session_id": session_id,
                                "token_balance_qty": token_balance_qty,
                                "collateral_balance_usd": collateral_balance,
                                "secs_since_resolution": round(_secs_since_res, 1),
                                "loss_writeoff_timeout_secs": loss_writeoff_timeout_secs,
                                "exit_price": 0.0,
                                "pnl_usd": _wo_pnl,
                                "trade": _trade_summary(trade),
                            },
                        )
                        _append_jsonl(log_path, {"type": "trade_closed", "ts": now, "session_id": session_id, "source": "resolution_loss_writeoff", "side": trade.side, "entry_price": _wo_ep, "exit_price": 0.0, "qty": _wo_qty, "pnl_usd": _wo_pnl, "entry_slug": trade.event_slug, "last_reason": str(trade.last_reason or "")})
                        if trade.event_slug and _safe_float(trade.entry_qty_filled) > 0:
                            _reentry_blocked_until[str(trade.event_slug)] = now + reentry_cooldown_secs
                        trade = LiveCurrentAlmostResolvedTradeState()
                        _clear_state(state_path)
                    elif not _loss_reason and _secs_since_res >= 120.0:
                        # Saldo > dust mas sem motivo de perda e já passou 2 min:
                        # a plataforma provavelmente já processou o redeem mas a API de
                        # balance está desatualizada. Força um refresh on-chain e re-verifica.
                        try:
                            broker.update_balance_allowance(asset_type="CONDITIONAL", token_id=trade.token_id)
                        except Exception:
                            pass
                        _fresh_bal = _token_balance_qty(broker, trade.token_id)
                        _st_ep = _safe_float(trade.entry_price, 0.0)
                        _st_filled_qty = _safe_float(trade.entry_qty_filled, 0.0)
                        _st_sold_qty = _safe_float(trade.exit_qty_filled, 0.0)
                        _st_sold_price = _safe_float(trade.exit_price_posted, 0.0)
                        _pct_remaining = _fresh_bal / max(0.000001, _st_filled_qty)
                        _win_like = "win" in str(trade.last_reason or "").lower()
                        _stale_exit_price = 1.0 if _win_like else None
                        if _is_flat_qty(_fresh_bal) or _pct_remaining < 0.10:
                            # Redeem confirmado (fresh balance zero) ou residual < 10% do total
                            _st_residual = max(0.0, _st_filled_qty - _st_sold_qty)
                            if _stale_exit_price is not None and _st_ep > 0 and _st_filled_qty > 0:
                                _st_pnl = round(
                                    (_st_sold_price - _st_ep) * _st_sold_qty
                                    + (_stale_exit_price - _st_ep) * _st_residual,
                                    4,
                                )
                            elif _st_ep > 0 and _st_sold_qty > 0 and _st_sold_price > 0:
                                _st_pnl = round((_st_sold_price - _st_ep) * _st_sold_qty, 4)
                            else:
                                _st_pnl = None
                            _append_jsonl(log_path, {
                                "type": "stale_redeem_closed",
                                "ts": now, "session_id": session_id,
                                "token_balance_cached": token_balance_qty,
                                "token_balance_fresh": _fresh_bal,
                                "pct_remaining": round(_pct_remaining, 4),
                                "secs_since_resolution": round(_secs_since_res, 1),
                                "exit_price_inferred": _stale_exit_price,
                                "pnl_usd": _st_pnl,
                                "trade": _trade_summary(trade),
                            })
                            _append_jsonl(log_path, {
                                "type": "trade_closed", "ts": now, "session_id": session_id,
                                "source": "stale_redeem_closed",
                                "side": trade.side, "entry_price": _st_ep,
                                "exit_price": _stale_exit_price, "qty": _st_filled_qty,
                                "pnl_usd": _st_pnl, "entry_slug": trade.event_slug,
                                "last_reason": str(trade.last_reason or ""),
                            })
                            if trade.event_slug and _st_filled_qty > 0:
                                _reentry_blocked_until[str(trade.event_slug)] = now + reentry_cooldown_secs
                            trade = LiveCurrentAlmostResolvedTradeState()
                            _clear_state(state_path)

            if trade.mode == "pending_exit":
                token_balance_qty = _token_balance_qty(broker, trade.token_id)
                exit_order = _get_order_status(broker, trade.exit_order_id)
                if exit_order is None:
                    if _is_flat_qty(token_balance_qty):
                        _append_jsonl(log_path, {"type": "flat", "ts": now, "session_id": session_id, "exit_order": None, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                        _tc_ep = _safe_float(trade.entry_price, 0.0); _tc_xp = _safe_float(trade.exit_price_posted, 0.0); _tc_qty = _safe_float(trade.entry_qty_filled, 0.0)
                        _append_jsonl(log_path, {"type": "trade_closed", "ts": now, "session_id": session_id, "source": "flat", "side": trade.side, "entry_price": _tc_ep, "exit_price": _tc_xp if _tc_xp else None, "qty": _tc_qty, "pnl_usd": round((_tc_xp - _tc_ep) * _tc_qty, 4) if _tc_xp and _tc_ep and _tc_qty else None, "entry_slug": trade.event_slug, "last_reason": trade.last_reason})
                        if trade.event_slug and _safe_float(trade.entry_qty_filled) > 0:
                            _reentry_blocked_until[str(trade.event_slug)] = now + reentry_cooldown_secs
                        trade = LiveCurrentAlmostResolvedTradeState()
                        _clear_state(state_path)
                    elif _should_await_platform_redeem(
                        trade,
                        current_secs=current_secs,
                        current_slug=str(current_item.get("slug") or "") if current_item else None,
                        hold_winner_to_resolution=hold_winner_to_resolution,
                    ):
                        trade = _mark_awaiting_redeem(
                            trade,
                            now=now,
                            reason=f"pending_exit_platform_redeem_path:{round(token_balance_qty, 6)}",
                        )
                        _save_state(state_path, trade)
                        _append_jsonl(log_path, {"type": "awaiting_redeem", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "active_bid": active_bid, "trade": _trade_summary(trade)})
                    elif active_bid > 0 and _safe_float(trade.stop_price, 0.0) > 0 and active_bid <= _safe_float(trade.stop_price, 0.0):
                        # Bid abaixo do stop enquanto sem ordem de saída ativa — agir imediatamente
                        # sem esperar exit_repost_secs. Calcular hedge vs stop.
                        _pe_opp_side = "DOWN" if (trade.side or "UP") == "UP" else "UP"
                        _pe_opp_bid  = _bid_for_side(current_exec, _pe_opp_side) if current_exec else 0.0
                        _pe_ep   = _safe_float(trade.entry_price, 0.0)
                        _pe_qty  = token_balance_qty
                        _pe_stop_pnl  = (active_bid - _pe_ep) * _pe_qty
                        _pe_hedge_pnl = (1.0 - _pe_ep - _pe_opp_bid) * _pe_qty if _pe_opp_bid > 0 else float("-inf")
                        _pe_do_hedge  = (
                            active_bid < 0.50
                            and _pe_hedge_pnl > _pe_stop_pnl
                            and _pe_opp_bid > 0
                            and _pe_qty > 0
                            and current_snap is not None
                        )
                        if _pe_do_hedge:
                            _pe_opp_tok  = _token_id_for_side(current_snap, _pe_opp_side)
                            _pe_opp_tick = _tick_size_from_snap(current_snap, _pe_opp_side)
                            _pe_hprice   = _clamp_limit_price(_pe_opp_bid, tick_size=_pe_opp_tick)
                            try:
                                _pe_hreq = BrokerOrderRequest(
                                    token_id=_pe_opp_tok, side="BUY", price=_pe_hprice,
                                    size=_pe_qty, order_type="GTC",
                                    market_slug=trade.event_slug, outcome=_pe_opp_side,
                                    client_order_key=f"ar_hedge_pe:{int(now)}:{_pe_opp_side}",
                                )
                                _pe_hord = broker.place_limit_order(_pe_hreq)
                                trade.hedge_token_id = _pe_opp_tok
                                trade.hedge_order_id = _pe_hord.order_id
                                trade.hedge_price    = _pe_hprice
                                trade.exit_order_id  = None
                                trade.updated_at     = now
                                trade.last_reason    = f"pending_exit_ar_hedge_posted:{_pe_opp_side}:{_pe_hprice}"
                                # Voltar para open_position para monitorar fill do hedge
                                trade.mode = "open_position"
                                _save_state(state_path, trade)
                                _append_jsonl(log_path, {
                                    "type": "ar_hedge_posted", "ts": now,
                                    "session_id": session_id,
                                    "from": "pending_exit_stop_recheck",
                                    "hedge_side": _pe_opp_side, "hedge_price": _pe_hprice,
                                    "stop_pnl": round(_pe_stop_pnl, 4),
                                    "hedge_pnl": round(_pe_hedge_pnl, 4),
                                    "trade": _trade_summary(trade),
                                })
                            except Exception as _pe_hg_exc:
                                trade = _post_exit_order(
                                    broker, trade, exit_price=active_bid, now=now,
                                    reason="stop_loss_pending_exit_recheck_hedge_fallback",
                                    min_limit_exit_qty=min_limit_exit_qty,
                                )
                                _save_state(state_path, trade)
                                _append_jsonl(log_path, {
                                    "type": "exit_posted", "ts": now,
                                    "session_id": session_id,
                                    "reason": "stop_loss_pending_exit_recheck_hedge_fallback",
                                    "error": f"{type(_pe_hg_exc).__name__}: {_exception_message(_pe_hg_exc)}",
                                    "trade": _trade_summary(trade),
                                })
                        else:
                            trade = _post_exit_order(
                                broker, trade, exit_price=active_bid, now=now,
                                reason="stop_loss_pending_exit_recheck",
                                min_limit_exit_qty=min_limit_exit_qty,
                            )
                            _save_state(state_path, trade)
                            _append_jsonl(log_path, {
                                "type": "exit_posted", "ts": now,
                                "session_id": session_id,
                                "reason": "stop_loss_pending_exit_recheck",
                                "trade": _trade_summary(trade),
                            })
                    elif now - trade.updated_at >= exit_repost_secs:
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=active_bid if active_bid > 0 else 0.01,
                            now=now,
                            reason="retry_residual",
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                        _save_state(state_path, trade)
                        _append_jsonl(log_path, {"type": "exit_repost", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                else:
                    trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
                    status = str(getattr(exit_order, "status", "") or "").lower()
                    if _is_flat_qty(token_balance_qty) or trade.remaining_position_qty <= 0:
                        _append_jsonl(log_path, {"type": "flat", "ts": now, "session_id": session_id, "exit_order": exit_order.as_dict(), "exit_status": status, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                        _tc_ep = _safe_float(trade.entry_price, 0.0); _tc_xp = _safe_float(trade.exit_price_posted, 0.0); _tc_qty = _safe_float(trade.entry_qty_filled, 0.0)
                        _append_jsonl(log_path, {"type": "trade_closed", "ts": now, "session_id": session_id, "source": "flat", "side": trade.side, "entry_price": _tc_ep, "exit_price": _tc_xp if _tc_xp else None, "qty": _tc_qty, "pnl_usd": round((_tc_xp - _tc_ep) * _tc_qty, 4) if _tc_xp and _tc_ep and _tc_qty else None, "entry_slug": trade.event_slug, "last_reason": trade.last_reason})
                        if trade.event_slug and _safe_float(trade.entry_qty_filled) > 0:
                            _reentry_blocked_until[str(trade.event_slug)] = now + reentry_cooldown_secs
                        trade = LiveCurrentAlmostResolvedTradeState()
                        _clear_state(state_path)
                    elif _is_match_status(status) and token_balance_qty > 0:
                        trade.mode = "exit_pending_confirm"
                        trade.last_reason = f"exit_match_pending_confirm:{round(token_balance_qty, 6)}"
                        trade.confirm_started_at = now
                        trade.confirm_polls = 1
                        _save_state(state_path, trade)
                        _append_jsonl(log_path, {"type": "exit_match_pending_confirm", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                    elif now - trade.updated_at >= exit_repost_secs:
                        cancel_resp = _cancel_if_live(broker, trade.exit_order_id)
                        trade.exit_order_id = None
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=active_bid if active_bid > 0 else 0.01,
                            now=now,
                            reason="repost",
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                        _save_state(state_path, trade)
                        _append_jsonl(log_path, {"type": "exit_repost", "ts": now, "session_id": session_id, "cancel": cancel_resp, "trade": _trade_summary(trade)})

            if trade.mode == "exit_pending_confirm":
                token_balance_qty = _token_balance_qty(broker, trade.token_id)
                if _is_flat_qty(token_balance_qty):
                    _append_jsonl(log_path, {"type": "flat", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                    _tc_ep = _safe_float(trade.entry_price, 0.0); _tc_xp = _safe_float(trade.exit_price_posted, 0.0); _tc_qty = _safe_float(trade.entry_qty_filled, 0.0)
                    _append_jsonl(log_path, {"type": "trade_closed", "ts": now, "session_id": session_id, "source": "flat", "side": trade.side, "entry_price": _tc_ep, "exit_price": _tc_xp if _tc_xp else None, "qty": _tc_qty, "pnl_usd": round((_tc_xp - _tc_ep) * _tc_qty, 4) if _tc_xp and _tc_ep and _tc_qty else None, "entry_slug": trade.event_slug, "last_reason": trade.last_reason})
                    if trade.event_slug and _safe_float(trade.entry_qty_filled) > 0:
                        _reentry_blocked_until[str(trade.event_slug)] = now + reentry_cooldown_secs
                    trade = LiveCurrentAlmostResolvedTradeState()
                    _clear_state(state_path)
                elif now - trade.confirm_started_at >= 2.0 or trade.confirm_polls >= 2:
                    trade.mode = "pending_exit"
                    trade.exit_order_id = None
                    trade.updated_at = now - exit_repost_secs
                    trade.last_reason = f"residual_position_after_exit:{round(token_balance_qty, 6)}"
                    _save_state(state_path, trade)
                    _append_jsonl(log_path, {"type": "residual_position_after_exit", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                else:
                    trade.confirm_polls += 1
                    _save_state(state_path, trade)

            time.sleep(poll_secs)

        except Exception as exc:
            trace = traceback.format_exc()
            exception_path.write_text(trace, encoding="utf-8")
            _append_jsonl(
                log_path,
                {
                    "type": "exception",
                    "ts": now,
                    "session_id": session_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": trace,
                    "trade": _trade_summary(trade),
                },
            )
            _force_risk_cleanup(broker, trade, log_path, now, f"{type(exc).__name__}: {exc}", min_limit_exit_qty)
            _save_state(state_path, trade)
            raise

    _shutdown_reconcile(
        broker,
        trade,
        min_limit_exit_qty=min_limit_exit_qty,
        state_path=state_path,
        log_path=log_path,
        session_id=session_id,
        now=time.time(),
    )


if __name__ == "__main__":
    monitor_live_current_almost_resolved_real_v1()
