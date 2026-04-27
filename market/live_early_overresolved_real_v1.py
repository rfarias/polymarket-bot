from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from market.book_5m import fetch_books_for_tokens, fetch_market_metadata_from_slug
from market.broker_env import load_broker_env
from market.broker_types import BrokerOrderRequest
from market.current_early_overresolved_signal_v1 import CurrentEarlyOverresolvedResearchV1
from market.current_scalp_signal_v1 import fetch_binance_open_price_for_event_start_v1, fetch_external_btc_reference_v1
from market.live_guarded_config import load_live_guarded_config
from market.polymarket_broker_v3 import PolymarketBrokerV3
from market.operational_slots_v2 import fetch_operational_slots_v2
from market.public_market_data_v1 import fetch_midpoints
from market.public_market_data_v2 import fetch_token_executable_prices


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


def _apply_runtime_overrides(cfg) -> None:
    cfg.leader_price_min = _env_float("POLY_EARLY_OVERRESOLVED_MIN_LEADER_PRICE", cfg.leader_price_min)
    cfg.counter_price_max = _env_float("POLY_EARLY_OVERRESOLVED_MAX_COUNTER_PRICE", cfg.counter_price_max)
    cfg.min_secs_to_end_default = _env_int("POLY_EARLY_OVERRESOLVED_MIN_SECS_TO_END", cfg.min_secs_to_end_default)
    cfg.hard_floor_secs_to_end = _env_int("POLY_EARLY_OVERRESOLVED_HARD_FLOOR_SECS", cfg.hard_floor_secs_to_end)
    cfg.min_elapsed_default = _env_int("POLY_EARLY_OVERRESOLVED_MIN_ELAPSED_DEFAULT", cfg.min_elapsed_default)
    cfg.min_elapsed_by_tf_15m = _env_int("POLY_EARLY_OVERRESOLVED_MIN_ELAPSED_15M", cfg.min_elapsed_by_tf_15m)
    cfg.min_distance_from_open_bps_5m = _env_float("POLY_EARLY_OVERRESOLVED_MIN_DISTANCE_BPS_5M", cfg.min_distance_from_open_bps_5m)
    cfg.min_distance_from_open_bps_15m = _env_float("POLY_EARLY_OVERRESOLVED_MIN_DISTANCE_BPS_15M", cfg.min_distance_from_open_bps_15m)
    cfg.min_market_range_30s = _env_float("POLY_EARLY_OVERRESOLVED_MIN_MARKET_RANGE_30S", cfg.min_market_range_30s)
    cfg.min_market_range_60s_15m = _env_float("POLY_EARLY_OVERRESOLVED_MIN_MARKET_RANGE_60S_15M", cfg.min_market_range_60s_15m)
    cfg.min_spot_reversal_5s_bps = _env_float("POLY_EARLY_OVERRESOLVED_MIN_SPOT_REVERSAL_5S_BPS", cfg.min_spot_reversal_5s_bps)
    cfg.min_market_pullback_5s = _env_float("POLY_EARLY_OVERRESOLVED_MIN_MARKET_PULLBACK_5S", cfg.min_market_pullback_5s)
    cfg.max_spread_counter = _env_float("POLY_EARLY_OVERRESOLVED_MAX_COUNTER_SPREAD", cfg.max_spread_counter)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_log_dir() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"early_overresolved_real_{ts}"


def _state_path() -> Path:
    return Path("logs") / "early_overresolved_real_state.json"


def _save_state(path: Path, trade) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(trade), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state(path: Path):
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return LiveEarlyOverresolvedTradeState(**payload)
    except Exception:
        return None


def _clear_state(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


@dataclass
class LiveEarlyOverresolvedTradeState:
    mode: str = "idle"  # idle | pending_entry | open_position | pending_exit
    event_slug: Optional[str] = None
    timeframe: Optional[str] = None
    side: Optional[str] = None
    token_id: Optional[str] = None
    setup: Optional[str] = None
    entry_signal_reason: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    entry_price: Optional[float] = None
    entry_qty_requested: float = 0.0
    entry_qty_filled: float = 0.0
    exit_qty_filled: float = 0.0
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    best_bid: Optional[float] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_reason: Optional[str] = None

    @property
    def remaining_position_qty(self) -> float:
        return round(max(0.0, float(self.entry_qty_filled) - float(self.exit_qty_filled)), 6)


def _parse_dt(value: Optional[str]):
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _event_start_time_from_item(item: dict, timeframe: str) -> Optional[str]:
    start_time = item.get("event_start_time") or item.get("startTime")
    if start_time:
        return str(start_time)
    end_dt = _parse_dt(item.get("endDate"))
    if end_dt is None:
        return None
    from datetime import timedelta, timezone

    tf_secs = 300 if timeframe == "5m" else 900
    start_dt = end_dt - timedelta(seconds=tf_secs)
    return start_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _best_bid(book: dict) -> float:
    bids = book.get("bids") or []
    if not bids:
        return 0.0
    return _safe_float((bids[0] or {}).get("price"), 0.0)


def _best_ask(book: dict) -> float:
    asks = book.get("asks") or []
    if not asks:
        return 0.0
    return _safe_float((asks[0] or {}).get("price"), 0.0)


def _computed_spread(best_bid: float, best_ask: float) -> Optional[float]:
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    return round(best_ask - best_bid, 6)


def _raw_book_id(book: dict) -> str:
    return str(book.get("asset_id") or book.get("token_id") or book.get("id") or "")


def _fetch_snap_for_slug(slug: str) -> tuple[dict, str]:
    meta = fetch_market_metadata_from_slug(slug)
    if not meta:
        return {"up": None, "down": None}, "missing_meta"
    token_mapping = meta.get("token_mapping") or []
    token_ids = [str(x["token_id"]) for x in token_mapping if x.get("token_id")]
    raw_books = fetch_books_for_tokens(token_ids)
    midpoints = fetch_midpoints(token_ids)
    executable_prices = {token_id: fetch_token_executable_prices(token_id) for token_id in token_ids}
    by_id = {_raw_book_id(book): book for book in raw_books}
    joined = []
    for mapping in token_mapping:
        token_id = str(mapping["token_id"])
        book = by_id.get(token_id) or {}
        best_bid = _best_bid(book)
        best_ask = _best_ask(book)
        executable = executable_prices.get(token_id) or {}
        joined.append(
            {
                "outcome": mapping.get("outcome"),
                "token_id": token_id,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "midpoint": midpoints.get(token_id),
                "spread": _computed_spread(best_bid, best_ask),
                "executable_buy": executable.get("BUY"),
                "executable_sell": executable.get("SELL"),
                "tick_size": book.get("tick_size"),
                "top_bids": [{"price": lvl.get("price"), "size": lvl.get("size")} for lvl in (book.get("bids") or [])[:3]],
                "top_asks": [{"price": lvl.get("price"), "size": lvl.get("size")} for lvl in (book.get("asks") or [])[:3]],
            }
        )
    up = next((item for item in joined if str(item.get("outcome") or "").lower() == "up"), None)
    down = next((item for item in joined if str(item.get("outcome") or "").lower() == "down"), None)
    snap = {"up": up, "down": down}
    return snap, "ok" if up and down else "missing_up_or_down"


def _tick_size_from_snap(snap: dict, side: str) -> float:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(side_book.get("tick_size"), 0.01))


def _token_id_for_side(snap: dict, side: str) -> str:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return str(side_book.get("token_id") or "")


def _fetch_active_book(trade: LiveEarlyOverresolvedTradeState) -> Optional[dict]:
    if not trade.token_id:
        return None
    raw_books = fetch_books_for_tokens([trade.token_id])
    if not raw_books:
        return None
    return raw_books[0] if raw_books else None


def _mid_from_side(side_book: dict | None) -> Optional[float]:
    if not side_book:
        return None
    bid = _safe_float(side_book.get("executable_sell") or side_book.get("best_bid"), -1.0)
    ask = _safe_float(side_book.get("executable_buy") or side_book.get("best_ask"), -1.0)
    if bid <= 0 or ask <= 0:
        return None
    return round((min(bid, ask) + max(bid, ask)) / 2.0, 6)


def _top_ask_size(side_book: dict | None, ask_price: float) -> float:
    if not side_book or ask_price <= 0:
        return 0.0
    for level in list(side_book.get("top_asks") or [])[:3]:
        level_price = _safe_float(level.get("price"), -1.0)
        if level_price > 0 and abs(level_price - ask_price) < 0.0005:
            return max(0.0, _safe_float(level.get("size"), 0.0))
    levels = list(side_book.get("top_asks") or [])
    if levels:
        return max(0.0, _safe_float(levels[0].get("size"), 0.0))
    return 0.0


def _is_dead_locked_book(snap: dict) -> bool:
    up = snap.get("up") or {}
    down = snap.get("down") or {}
    up_mid = _mid_from_side(up)
    down_mid = _mid_from_side(down)
    up_ask = _safe_float(up.get("executable_buy") or up.get("best_ask"), 0.0)
    down_ask = _safe_float(down.get("executable_buy") or down.get("best_ask"), 0.0)
    up_bid = _safe_float(up.get("executable_sell") or up.get("best_bid"), 0.0)
    down_bid = _safe_float(down.get("executable_sell") or down.get("best_bid"), 0.0)
    if up_mid is None or down_mid is None:
        return True
    if abs(up_mid - 0.5) < 0.0005 and abs(down_mid - 0.5) < 0.0005:
        if up_ask >= 0.97 and down_ask >= 0.97 and up_bid <= 0.03 and down_bid <= 0.03:
            return True
    return False


def _real_entry_confirmed(signal: dict, snap: dict, qty: int) -> tuple[bool, str]:
    side = str(signal.get("side") or "")
    market_5s = _safe_float(signal.get("market_delta_5s"), 0.0)
    market_15s = _safe_float(signal.get("market_delta_15s"), 0.0)
    spot_5s = _safe_float(signal.get("spot_delta_5s_bps"), 0.0)
    entry_price = _safe_float(signal.get("entry_price"), 0.0)
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    top_ask_size = _top_ask_size(side_book, entry_price)

    if _is_dead_locked_book(snap):
        return False, "dead_locked_book"
    if top_ask_size < 1.0:
        return False, "no_top_ask_liquidity"
    if side == "UP":
        if market_5s < 0.0 and market_15s < 0.0:
            return False, "up_odds_still_falling"
        if market_5s < 0.01 and spot_5s < -0.10:
            return False, "up_not_confirmed_by_odds"
    elif side == "DOWN":
        if market_5s > 0.0 and market_15s > 0.0:
            return False, "down_odds_still_falling"
        if market_5s > -0.01 and spot_5s > 0.10:
            return False, "down_not_confirmed_by_odds"
    if top_ask_size < min(float(qty), 2.0):
        return True, "partial_liquidity_only"
    return True, "confirmed"


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


def _has_sufficient_collateral_for_entry(broker, *, entry_price: float, qty: float, buffer_usd: float = 0.25) -> bool:
    required = round(float(entry_price) * float(qty) + float(buffer_usd), 6)
    available = _collateral_balance_usd(broker)
    return available >= required


def _restore_trade_from_broker(broker, trade: LiveEarlyOverresolvedTradeState) -> LiveEarlyOverresolvedTradeState:
    if trade.mode == "idle":
        return trade
    entry_order = _get_order_status(broker, trade.entry_order_id)
    if entry_order is not None:
        trade.entry_qty_filled = max(trade.entry_qty_filled, _safe_float(getattr(entry_order, "size_matched", None), 0.0))
    exit_order = _get_order_status(broker, trade.exit_order_id)
    if exit_order is not None:
        trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
        status = str(getattr(exit_order, "status", "") or "").lower()
        if trade.remaining_position_qty <= 0 or status in ("filled", "closed", "resolved"):
            return LiveEarlyOverresolvedTradeState()
        trade.mode = "pending_exit"
    if trade.entry_qty_filled > 0 and trade.mode == "pending_entry":
        trade.mode = "open_position"
    token_balance = _token_balance_qty(broker, trade.token_id)
    if token_balance > 0 and trade.entry_qty_filled > 0:
        trade.entry_qty_filled = max(trade.entry_qty_filled, token_balance + float(trade.exit_qty_filled))
    return trade


def _post_entry_order(
    broker,
    *,
    signal: dict,
    snap: dict,
    qty: int,
    now: float,
) -> LiveEarlyOverresolvedTradeState:
    side = str(signal.get("side") or "")
    entry_price = _safe_float(signal.get("entry_price"), 0.0)
    trade = LiveEarlyOverresolvedTradeState(
        mode="pending_entry",
        event_slug=str(signal.get("event_slug") or ""),
        timeframe=str(signal.get("timeframe") or ""),
        side=side,
        token_id=_token_id_for_side(snap, side),
        setup=str(signal.get("setup") or ""),
        entry_signal_reason=str(signal.get("reason") or ""),
        entry_price=entry_price,
        entry_qty_requested=float(qty),
        target_price=_safe_float(signal.get("target_price"), 0.0),
        stop_price=_safe_float(signal.get("stop_price"), 0.0),
        created_at=now,
        updated_at=now,
        last_reason="entry_posted",
    )
    if not trade.token_id:
        raise RuntimeError(f"Missing token_id for side={side}")
    if not _has_sufficient_collateral_for_entry(broker, entry_price=entry_price, qty=qty):
        raise RuntimeError(
            f"Insufficient collateral for entry: required={round(entry_price * qty, 6)} available={_collateral_balance_usd(broker)}"
        )
    req = BrokerOrderRequest(
        token_id=trade.token_id,
        side="BUY",
        price=entry_price,
        size=float(qty),
        market_slug=trade.event_slug,
        outcome=side,
        client_order_key=f"early_overresolved:entry:{int(now)}:{side}",
    )
    order = broker.place_limit_order(req)
    trade.entry_order_id = order.order_id
    return trade


def _post_exit_order(
    broker,
    trade: LiveEarlyOverresolvedTradeState,
    *,
    exit_price: float,
    now: float,
    reason: str,
) -> LiveEarlyOverresolvedTradeState:
    token_balance_qty = _token_balance_qty(broker, trade.token_id)
    qty = min(trade.remaining_position_qty, token_balance_qty) if token_balance_qty > 0 else trade.remaining_position_qty
    if qty <= 0:
        trade.mode = "idle"
        trade.last_reason = "flat"
        trade.updated_at = now
        return trade
    try:
        broker.update_balance_allowance(asset_type="CONDITIONAL", token_id=trade.token_id)
    except Exception:
        pass
    req = BrokerOrderRequest(
        token_id=trade.token_id or "",
        side="SELL",
        price=float(exit_price),
        size=float(qty),
        market_slug=trade.event_slug,
        outcome=trade.side,
        client_order_key=f"early_overresolved:exit:{reason}:{int(now)}:{trade.side}",
    )
    order = broker.place_limit_order(req)
    trade.exit_order_id = order.order_id
    trade.mode = "pending_exit"
    trade.updated_at = now
    trade.last_reason = f"exit_posted:{reason}"
    return trade


def _force_risk_cleanup(broker, trade: LiveEarlyOverresolvedTradeState, log_path: Path, now: float, reason: str) -> None:
    try:
        _append_jsonl(log_path, {"type": "panic", "ts": now, "reason": reason, "trade": asdict(trade)})
        _cancel_if_live(broker, trade.entry_order_id)
        _cancel_if_live(broker, trade.exit_order_id)
        token_balance_qty = _token_balance_qty(broker, trade.token_id)
        panic_qty = min(trade.remaining_position_qty, token_balance_qty) if token_balance_qty > 0 else trade.remaining_position_qty
        if panic_qty > 0 and trade.token_id and trade.side:
            active_book = _fetch_active_book(trade)
            panic_bid = _best_bid(active_book or {})
            if panic_bid > 0:
                try:
                    broker.update_balance_allowance(asset_type="CONDITIONAL", token_id=trade.token_id)
                except Exception:
                    pass
                req = BrokerOrderRequest(
                    token_id=trade.token_id,
                    side="SELL",
                    price=float(panic_bid),
                    size=float(panic_qty),
                    market_slug=trade.event_slug,
                    outcome=trade.side,
                    client_order_key=f"early_overresolved:panic_exit:{int(now)}:{trade.side}",
                )
                order = broker.place_limit_order(req)
                _append_jsonl(log_path, {"type": "panic_exit_posted", "ts": now, "price": panic_bid, "order": order.as_dict()})
    except Exception as exc:
        _append_jsonl(log_path, {"type": "panic_error", "ts": now, "reason": reason, "error": f"{type(exc).__name__}: {exc}"})


def monitor_live_early_overresolved_real_v1(duration_seconds: Optional[int] = None, log_dir: Optional[str] = None) -> None:
    load_dotenv()
    guarded_cfg = load_live_guarded_config()
    broker_status = load_broker_env()

    timeframe = str(os.getenv("POLY_EARLY_OVERRESOLVED_REAL_TIMEFRAME", "5m")).strip().lower()
    mode = str(os.getenv("POLY_EARLY_OVERRESOLVED_REAL_MODE", "flex")).strip().lower()
    qty = _env_int("POLY_EARLY_OVERRESOLVED_REAL_QTY", 6)
    entry_timeout_secs = _env_int("POLY_EARLY_OVERRESOLVED_REAL_ENTRY_TIMEOUT_SECS", 12)
    poll_secs = max(0.5, float(os.getenv("POLY_EARLY_OVERRESOLVED_REAL_POLL_SECS", "1.0")))
    run_for = int(duration_seconds or _env_int("POLY_EARLY_OVERRESOLVED_REAL_RUN_SECONDS", 1800))
    research = CurrentEarlyOverresolvedResearchV1.from_mode(qty=qty, mode=mode)
    cfg = research.cfg
    _apply_runtime_overrides(cfg)

    print("[BROKER_ENV]", broker_status.as_dict())
    print("[LIVE_GUARDED_CONFIG]", guarded_cfg.as_dict())
    print("[EARLY_OVERRESOLVED_REAL_CONFIG]", {"timeframe": timeframe, "mode": mode, **cfg.as_dict()})

    if not guarded_cfg.enabled:
        print("[GUARD] Set POLY_GUARDED_ENABLED=true")
        return
    if guarded_cfg.shadow_only:
        print("[GUARD] Set POLY_GUARDED_SHADOW_ONLY=false")
        return
    if not guarded_cfg.real_posts_enabled:
        print("[GUARD] Set POLY_GUARDED_REAL_POSTS_ENABLED=true")
        return
    if not _env_bool("POLY_EARLY_OVERRESOLVED_REAL_ENABLED", False):
        print("[GUARD] Set POLY_EARLY_OVERRESOLVED_REAL_ENABLED=true to arm real early overresolved runner")
        return
    if not broker_status.ready_for_real_smoke:
        print("[GUARD] Broker env missing required credentials")
        return

    session_dir = Path(log_dir) if log_dir else _build_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "early_overresolved_real.jsonl"
    exception_path = session_dir / "exception.log"
    state_path = _state_path()

    print(
        "[EARLY_OVERRESOLVED_REAL_PARAMS]",
        {
            "qty": qty,
            "entry_timeout_secs": entry_timeout_secs,
            "position_timeout_secs": cfg.max_hold_secs_15m if timeframe == "15m" else cfg.max_hold_secs_5m,
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
    restored_trade = _load_state(state_path) or LiveEarlyOverresolvedTradeState()

    if restored_trade.mode != "idle":
        restored_trade = _restore_trade_from_broker(broker, restored_trade)
        print("[RESTORED_EARLY_OVERRESOLVED_TRADE]", asdict(restored_trade))
        if restored_trade.mode == "idle":
            _clear_state(state_path)
        else:
            allowed_ids = {x for x in (restored_trade.entry_order_id, restored_trade.exit_order_id) if x}
            startup_ids = {o.order_id for o in startup_orders}
            if startup_ids - allowed_ids:
                print("[GUARD] Refusing to start with open orders not owned by restored early overresolved state.")
                return
    elif startup_orders:
        print("[GUARD] Refusing to start with open orders while no early overresolved state is restored.")
        return

    trade = restored_trade
    current_open_reference: dict = {"slug": None, "price": None, "event_start_time": None}
    started_at = time.time()
    slots_cache: dict = {}
    slots_refreshed_at = 0.0

    while time.time() - started_at < run_for:
        now = time.time()
        try:
            if now - slots_refreshed_at >= 15.0 or not slots_cache:
                slots_cache = fetch_operational_slots_v2()
                slots_refreshed_at = now

            current_item = ((slots_cache.get(timeframe) or {}).get("current"))
            current_secs = int(current_item.get("seconds_to_end")) if current_item and current_item.get("seconds_to_end") is not None else None
            if current_item and current_item["slug"] != current_open_reference.get("slug"):
                start_time = _event_start_time_from_item(current_item, timeframe)
                opened = fetch_binance_open_price_for_event_start_v1(start_time) if start_time else {"open_price": None}
                current_open_reference = {"slug": current_item["slug"], "price": opened.get("open_price"), "event_start_time": start_time}

            reference = fetch_external_btc_reference_v1() if current_item else {}
            signal = {"setup": "early_overresolved_reversal", "allow": False, "reason": "missing_current"}
            snap = {"up": None, "down": None}
            snap_reason = "missing_current"
            real_gate_reason = None
            if current_item:
                snap, snap_reason = _fetch_snap_for_slug(current_item["slug"])
                if snap_reason == "ok":
                    signal = research.evaluate(
                        timeframe=timeframe,
                        slug=current_item["slug"],
                        snap=snap,
                        secs_to_end=current_secs,
                        event_start_time=current_open_reference.get("event_start_time"),
                        opening_reference_price=current_open_reference.get("price"),
                        reference_payload=reference,
                        now_ts=now,
                    )
                    signal["event_slug"] = current_item["slug"]
                    if signal.get("allow"):
                        real_gate_ok, real_gate_reason = _real_entry_confirmed(signal, snap, qty)
                        signal["real_gate_reason"] = real_gate_reason
                        if not real_gate_ok:
                            signal["allow"] = False
                            signal["reason"] = real_gate_reason
                elif snap_reason:
                    real_gate_reason = snap_reason

            active_book = _fetch_active_book(trade) if trade.mode in ("pending_entry", "open_position", "pending_exit") else None
            active_bid = _best_bid(active_book or {})

            snapshot = {
                "type": "snapshot",
                "ts": now,
                "current_slug": current_item.get("slug") if current_item else None,
                "current_secs": current_secs,
                "snap_reason": snap_reason,
                "real_gate_reason": real_gate_reason,
                "signal": signal,
                "trade": asdict(trade),
                "active_bid": active_bid,
            }
            _append_jsonl(log_path, snapshot)
            print(
                f"[EARLY_OVERRESOLVED_REAL] current_secs={current_secs} allow={signal.get('allow')} "
                f"side={signal.get('side')} reason={signal.get('reason')} mode={trade.mode} "
                f"qty={trade.entry_qty_filled}/{trade.remaining_position_qty}"
            )

            if trade.mode == "idle" and current_item and snap_reason == "ok" and signal.get("allow"):
                trade = _post_entry_order(
                    broker,
                    signal=signal,
                    snap=snap,
                    qty=qty,
                    now=now,
                )
                _save_state(state_path, trade)
                _append_jsonl(log_path, {"type": "enter", "ts": now, "signal": signal, "trade": asdict(trade)})
                time.sleep(poll_secs)
                continue

            if trade.mode in ("pending_entry", "open_position", "pending_exit"):
                entry_order = _get_order_status(broker, trade.entry_order_id)
                if entry_order is not None:
                    trade.entry_qty_filled = max(trade.entry_qty_filled, _safe_float(getattr(entry_order, "size_matched", None), 0.0))
                token_balance = _token_balance_qty(broker, trade.token_id)
                if token_balance > 0:
                    trade.entry_qty_filled = max(trade.entry_qty_filled, token_balance + float(trade.exit_qty_filled))
                trade.updated_at = now
                _save_state(state_path, trade)

            if trade.mode == "pending_entry":
                if trade.entry_qty_filled > 0:
                    order = _get_order_status(broker, trade.entry_order_id)
                    if order and getattr(order, "remaining_size", 0.0) > 0:
                        resp = _cancel_if_live(broker, trade.entry_order_id)
                        _append_jsonl(log_path, {"type": "entry_cancel_remainder", "ts": now, "response": resp})
                    trade.mode = "open_position"
                    trade.updated_at = now
                    trade.last_reason = "entry_fill_detected"
                    _save_state(state_path, trade)
                    _append_jsonl(log_path, {"type": "fill", "ts": now, "trade": asdict(trade)})
                elif now - trade.created_at >= entry_timeout_secs:
                    resp = _cancel_if_live(broker, trade.entry_order_id)
                    _append_jsonl(log_path, {"type": "entry_cancel_timeout", "ts": now, "response": resp, "trade": asdict(trade)})
                    trade = LiveEarlyOverresolvedTradeState()
                    _clear_state(state_path)

            if trade.mode == "open_position":
                active_book = _fetch_active_book(trade)
                active_bid = _best_bid(active_book or {})
                trade.best_bid = max(_safe_float(trade.best_bid, 0.0), active_bid)
                max_hold = cfg.max_hold_secs_15m if timeframe == "15m" else cfg.max_hold_secs_5m
                if active_bid > 0:
                    if active_bid >= _safe_float(trade.target_price):
                        trade = _post_exit_order(broker, trade, exit_price=active_bid, now=now, reason="target")
                    elif active_bid <= _safe_float(trade.stop_price):
                        trade = _post_exit_order(broker, trade, exit_price=active_bid, now=now, reason="stop")
                    elif current_secs is not None and current_secs <= cfg.hard_floor_secs_to_end:
                        trade = _post_exit_order(broker, trade, exit_price=active_bid, now=now, reason="expiry_near")
                    elif now - trade.created_at >= max_hold:
                        trade = _post_exit_order(broker, trade, exit_price=active_bid, now=now, reason="timeout")
                    if trade.mode == "pending_exit":
                        _save_state(state_path, trade)
                        _append_jsonl(log_path, {"type": "exit_posted", "ts": now, "trade": asdict(trade)})

            if trade.mode == "pending_exit":
                exit_order = _get_order_status(broker, trade.exit_order_id)
                if exit_order is not None:
                    trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
                    status = str(getattr(exit_order, "status", "") or "").lower()
                    if trade.remaining_position_qty <= 0 or status in ("filled", "closed", "resolved"):
                        _append_jsonl(log_path, {"type": "flat", "ts": now, "trade": asdict(trade)})
                        trade = LiveEarlyOverresolvedTradeState()
                        _clear_state(state_path)
                    elif now - trade.updated_at >= max_hold:
                        cancel_resp = _cancel_if_live(broker, trade.exit_order_id)
                        _append_jsonl(log_path, {"type": "exit_cancel_timeout", "ts": now, "cancel": cancel_resp, "trade": asdict(trade)})
                        trade = LiveEarlyOverresolvedTradeState()
                        _clear_state(state_path)

            time.sleep(poll_secs)

        except Exception as exc:
            trace = traceback.format_exc()
            exception_path.write_text(trace, encoding="utf-8")
            _append_jsonl(
                log_path,
                {
                    "type": "exception",
                    "ts": now,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": trace,
                    "trade": asdict(trade),
                },
            )
            _force_risk_cleanup(broker, trade, log_path, now, f"{type(exc).__name__}: {exc}")
            _save_state(state_path, trade)
            raise


if __name__ == "__main__":
    monitor_live_early_overresolved_real_v1()
