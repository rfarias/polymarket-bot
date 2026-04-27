from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from market.book_5m import fetch_books_for_tokens, fetch_market_metadata_from_slug
from market.current_early_overresolved_signal_v1 import CurrentEarlyOverresolvedResearchV1
from market.current_scalp_signal_v1 import fetch_binance_open_price_for_event_start_v1, fetch_external_btc_reference_v1
from market.operational_slots_v2 import fetch_operational_slots_v2
from market.public_market_data_v1 import fetch_midpoints
from market.public_market_data_v2 import fetch_token_executable_prices


@dataclass
class ShadowTradeStateV1:
    mode: str = "idle"
    timeframe: Optional[str] = None
    slug: Optional[str] = None
    side: Optional[str] = None
    entry_price: Optional[float] = None
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    qty_requested: int = 0
    qty_shadow_filled: int = 0
    fill_reason: Optional[str] = None
    created_at: float = 0.0
    last_reason: Optional[str] = None


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


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _default_log_path() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"early_overresolved_shadow_{ts}.jsonl"


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _event_start_time_from_item(item: Dict, timeframe: str) -> Optional[str]:
    start_time = item.get("event_start_time") or item.get("startTime")
    if start_time:
        return str(start_time)
    end_dt = _parse_dt(item.get("endDate"))
    if end_dt is None:
        return None
    tf_secs = 300 if timeframe == "5m" else 900
    start_dt = end_dt - timedelta(seconds=tf_secs)
    return start_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _best_bid(book: Dict) -> Optional[float]:
    bids = book.get("bids") or []
    if not bids:
        return None
    try:
        return float(bids[0]["price"])
    except Exception:
        return None


def _best_ask(book: Dict) -> Optional[float]:
    asks = book.get("asks") or []
    if not asks:
        return None
    try:
        return float(asks[0]["price"])
    except Exception:
        return None


def _computed_spread(best_bid: Optional[float], best_ask: Optional[float]) -> Optional[float]:
    if best_bid is None or best_ask is None:
        return None
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    return round(best_ask - best_bid, 6)


def _raw_book_id(book: Dict) -> str:
    return str(book.get("asset_id") or book.get("token_id") or book.get("id") or "")


def _fetch_snap_for_slug(slug: str) -> Tuple[dict, str]:
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


def _top_ask_size(side_book: dict | None, ask_price: float) -> float:
    if not side_book:
        return 0.0
    for level in list(side_book.get("top_asks") or [])[:3]:
        level_price = _safe_float(level.get("price"), -1.0)
        if level_price > 0 and abs(level_price - ask_price) < 0.0005:
            return max(0.0, _safe_float(level.get("size"), 0.0))
    levels = list(side_book.get("top_asks") or [])
    if levels:
        return max(0.0, _safe_float(levels[0].get("size"), 0.0))
    return 0.0


def _simulate_aggressive_fill_qty(*, desired_qty: int, side_book: dict | None, entry_price: float, signal: dict) -> tuple[int, str]:
    if desired_qty <= 0 or side_book is None or entry_price <= 0:
        return 0, "invalid_fill_request"
    ask_size = _top_ask_size(side_book, entry_price)
    spread = _safe_float(side_book.get("spread"), 0.01)
    leader_ask = _safe_float(signal.get("leader_ask"), 0.0)
    counter_ask = _safe_float(signal.get("counter_ask"), 0.0)
    overextension = max(0.0, leader_ask - counter_ask)
    fill_factor = 0.65
    if spread <= 0.01:
        fill_factor += 0.15
    if overextension >= 0.50:
        fill_factor += 0.15
    elif overextension >= 0.35:
        fill_factor += 0.08
    filled_qty = int(min(float(desired_qty), ask_size * fill_factor))
    if filled_qty >= desired_qty:
        return desired_qty, "full_top_book_fill"
    if filled_qty > 0:
        return filled_qty, "partial_top_book_fill_cancel_rest"
    return 0, "no_top_book_liquidity"


def monitor_live_early_overresolved_shadow_v1(duration_seconds: Optional[int] = None) -> None:
    enabled = _env_bool("POLY_EARLY_OVERRESOLVED_SHADOW_ENABLED", True)
    if not enabled:
        print("[EARLY_OVERRESOLVED_SHADOW_GUARD] Set POLY_EARLY_OVERRESOLVED_SHADOW_ENABLED=true")
        return

    run_seconds = int(duration_seconds or _env_int("POLY_EARLY_OVERRESOLVED_SHADOW_RUN_SECONDS", 300))
    poll_secs = max(1.0, _env_float("POLY_EARLY_OVERRESOLVED_SHADOW_POLL_SECS", 6.0))
    timeframe = str(os.getenv("POLY_EARLY_OVERRESOLVED_SHADOW_TIMEFRAME", "5m")).strip().lower()
    mode = str(os.getenv("POLY_EARLY_OVERRESOLVED_SHADOW_MODE", "flex")).strip().lower()
    qty = max(1, _env_int("POLY_EARLY_OVERRESOLVED_SHADOW_QTY", 50))
    log_path = _default_log_path()

    research = CurrentEarlyOverresolvedResearchV1.from_mode(qty=qty, mode=mode)
    trade = ShadowTradeStateV1()
    open_ref: Dict[str, Optional[float | str]] = {"slug": None, "price": None, "event_start_time": None}

    print("[EARLY_OVERRESOLVED_SHADOW_CONFIG]")
    print(json.dumps({"run_seconds": run_seconds, "poll_secs": poll_secs, "timeframe": timeframe, "mode": mode, "qty": qty}, ensure_ascii=False, indent=2))
    print("[LOG_FILE]")
    print(log_path)

    started_at = time.time()
    slots_cache: Dict[str, Dict] = {}
    slots_refreshed_at = 0.0

    while time.time() - started_at < run_seconds:
        now = time.time()
        if now - slots_refreshed_at >= 20.0 or not slots_cache:
            slots_cache = fetch_operational_slots_v2()
            slots_refreshed_at = now

        current_item = ((slots_cache.get(timeframe) or {}).get("current"))
        reference_payload = fetch_external_btc_reference_v1()
        if not current_item:
            _append_jsonl(log_path, {"type": "snapshot", "ts": now, "timeframe": timeframe, "reason": "missing_current"})
            time.sleep(poll_secs)
            continue

        slug = str(current_item.get("slug") or "")
        event_start_time = _event_start_time_from_item(current_item, timeframe)
        if open_ref.get("slug") != slug:
            open_payload = fetch_binance_open_price_for_event_start_v1(event_start_time) if event_start_time else {"open_price": None}
            open_ref = {"slug": slug, "price": open_payload.get("open_price"), "event_start_time": event_start_time}
            if trade.slug and trade.slug != slug:
                print(f"[SHADOW_RESET_ROLLOVER] old_slug={trade.slug} new_slug={slug}")
                trade = ShadowTradeStateV1()

        snap, snap_reason = _fetch_snap_for_slug(slug)
        if snap_reason != "ok":
            _append_jsonl(log_path, {"type": "snapshot", "ts": now, "timeframe": timeframe, "slug": slug, "reason": snap_reason})
            time.sleep(poll_secs)
            continue

        secs_to_end = current_item.get("seconds_to_end")
        try:
            secs_to_end = max(0, int(secs_to_end)) if secs_to_end is not None else None
        except Exception:
            secs_to_end = None

        signal = research.evaluate(
            timeframe=timeframe,
            slug=slug,
            snap=snap,
            secs_to_end=secs_to_end,
            event_start_time=str(open_ref.get("event_start_time") or ""),
            opening_reference_price=_safe_float(open_ref.get("price"), 0.0) or None,
            reference_payload=reference_payload,
            now_ts=now,
        )

        if trade.mode == "idle" and signal.get("allow"):
            side = str(signal.get("side") or "")
            side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
            filled_qty, fill_reason = _simulate_aggressive_fill_qty(
                desired_qty=qty,
                side_book=side_book,
                entry_price=_safe_float(signal.get("entry_price"), 0.0),
                signal=signal,
            )
            if filled_qty > 0:
                trade = ShadowTradeStateV1(
                    mode="open",
                    timeframe=timeframe,
                    slug=slug,
                    side=side,
                    entry_price=_safe_float(signal.get("entry_price"), 0.0),
                    target_price=_safe_float(signal.get("target_price"), 0.0),
                    stop_price=_safe_float(signal.get("stop_price"), 0.0),
                    qty_requested=qty,
                    qty_shadow_filled=filled_qty,
                    fill_reason=fill_reason,
                    created_at=now,
                    last_reason=str(signal.get("reason") or ""),
                )
                print(f"[SHADOW_ENTRY] side={side} entry={trade.entry_price} target={trade.target_price} stop={trade.stop_price} qty={filled_qty}/{qty} fill={fill_reason}")
                _append_jsonl(log_path, {"type": "enter", "ts": now, "timeframe": timeframe, "slug": slug, "signal": signal, "trade": asdict(trade)})
            else:
                print(f"[SHADOW_BLOCK_FILL] reason={fill_reason} side={side} entry={signal.get('entry_price')}")
                _append_jsonl(log_path, {"type": "blocked_fill", "ts": now, "timeframe": timeframe, "slug": slug, "signal": signal, "fill_reason": fill_reason, "requested_qty": qty})

        elif trade.mode == "open":
            side_book = (snap.get("up") if trade.side == "UP" else snap.get("down")) or {}
            bid_now = _safe_float(side_book.get("executable_sell") or side_book.get("best_bid"), 0.0)
            max_hold = research.cfg.max_hold_secs_15m if timeframe == "15m" else research.cfg.max_hold_secs_5m
            exit_reason = None
            exit_price = None
            if bid_now >= _safe_float(trade.target_price, 0.0):
                exit_reason = "target"
                exit_price = _safe_float(trade.target_price, bid_now)
            elif bid_now <= _safe_float(trade.stop_price, 0.0):
                exit_reason = "stop"
                exit_price = _safe_float(trade.stop_price, bid_now)
            elif secs_to_end is not None and secs_to_end <= research.cfg.hard_floor_secs_to_end:
                exit_reason = "late_exit"
                exit_price = bid_now if bid_now > 0 else trade.entry_price
            elif now - trade.created_at >= max_hold:
                exit_reason = "timeout"
                exit_price = bid_now if bid_now > 0 else trade.entry_price

            if exit_reason:
                pnl_ticks = round((float(exit_price) - float(trade.entry_price or 0.0)) / 0.01, 4)
                pnl_usd = round((float(exit_price) - float(trade.entry_price or 0.0)) * float(trade.qty_shadow_filled), 4)
                print(f"[SHADOW_EXIT] reason={exit_reason} side={trade.side} exit={exit_price} pnl_ticks={pnl_ticks} pnl_usd={pnl_usd}")
                _append_jsonl(
                    log_path,
                    {
                        "type": "exit",
                        "ts": now,
                        "timeframe": timeframe,
                        "slug": slug,
                        "signal": signal,
                        "trade": {**asdict(trade), "exit_price": exit_price, "exit_reason": exit_reason, "pnl_ticks": pnl_ticks, "pnl_usd": pnl_usd},
                    },
                )
                trade = ShadowTradeStateV1()

        _append_jsonl(log_path, {"type": "snapshot", "ts": now, "timeframe": timeframe, "slug": slug, "secs_to_end": secs_to_end, "signal": signal, "trade": asdict(trade)})
        time.sleep(poll_secs)

    print("[EARLY_OVERRESOLVED_SHADOW_END]")

