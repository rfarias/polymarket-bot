from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from market.book_5m import fetch_market_metadata_from_slug
from market.current_market_ws_cache import CurrentMarketWsCache
from market.current_almost_resolved_signal_v1 import (
    CurrentAlmostResolvedConfigV1,
    evaluate_current_almost_resolved_v1,
    passive_capture_required_distance_v1,
)
from market.current_early_overresolved_signal_v1 import CurrentEarlyOverresolvedResearchV1
from market.current_scalp_signal_v1 import (
    BINANCE_PRICE_URL,
    TIMEOUT,
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
)
from market.rest_5m_shadow_public_v4 import DISPLAY_SPREAD_WIDE_THRESHOLD
from market.rest_5m_shadow_public_v5 import _compute_executable_metrics, _fetch_slot_state, _slot_snapshot
from market.slug_discovery import fetch_event_by_slug


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _variant_exit_hint(variant: str) -> str:
    if variant == "extreme_99_limit":
        return "hold ate resolucao; stop ignorado para esta variante"
    if variant == "controlled_late_entry":
        return "sair com lucro se range_15s alto, spot adverso ou buffer baixo"
    if variant == "resolved_pullback_limit":
        return "sair se range_15s >= 0.03 ou range_30s >= 0.025 ou spot adverso alto"
    if variant == "passive_extreme_liquidity_capture":
        return "saida no target; requisita tick-up no lider antes do fill"
    if variant == "dual_rich_late_limit":
        return "saida no target ou stop padrao; ambos os lados ricos"
    return "saida no target ou stop padrao"


def _slot_secs_to_end(item: dict | None) -> int | None:
    if not item:
        return None
    secs = _safe_float(item.get("seconds_to_end"), 0.0)
    if secs <= 0:
        return 0
    return int(secs)


def _round_down_to_current_5m_epoch(now_ts: int) -> int:
    return (now_ts // 300) * 300


def _parse_dt(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _fetch_current_item() -> Optional[dict]:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    slug = f"btc-updown-5m-{_round_down_to_current_5m_epoch(now_ts)}"
    event = fetch_event_by_slug(slug)
    if not event:
        return None
    markets = event.get("markets") or []
    if not markets:
        return None
    market = markets[0]
    if market.get("active") is not True or market.get("closed") is True:
        return None
    if market.get("acceptingOrders") is not True or market.get("enableOrderBook") is not True:
        return None
    end_dt = _parse_dt(event.get("endDate") or market.get("endDate"))
    if not end_dt:
        return None
    secs_to_end = (end_dt - datetime.now(timezone.utc)).total_seconds()
    if secs_to_end <= 0:
        return None
    return {
        "title": event.get("title"),
        "slug": slug,
        "market_slug": market.get("slug"),
        "seconds_to_end": round(secs_to_end),
        "endDate": event.get("endDate") or market.get("endDate"),
    }


def _fetch_fast_reference_price() -> Dict:
    try:
        res = requests.get(BINANCE_PRICE_URL, params={"symbol": "BTCUSDT"}, timeout=min(TIMEOUT, 2.0))
        res.raise_for_status()
        data = res.json() or {}
        value = _safe_float(data.get("price"), 0.0)
        if value > 0:
            return {"reference_price": value, "source_divergence_bps": 0.0, "sources": {"binance": value}}
    except Exception as exc:
        return {"reference_price": None, "source_divergence_bps": None, "sources": {"binance_error": str(exc)}}
    return {"reference_price": None, "source_divergence_bps": None, "sources": {}}


@dataclass
class ManualOverlaySnapshotV1:
    ok: bool
    title: str
    slug: str
    secs_to_end: Optional[int]
    spot_price: Optional[float]
    open_price: Optional[float]
    spot_move_5s_bps: Optional[float]
    spot_move_15s_bps: Optional[float]
    spot_move_30s_bps: Optional[float]
    trend_label: str
    trend_side: str
    reversal_risk: str
    safety_label: str
    setup_side: str
    setup_allowed: bool
    setup_reason: str
    entry_price: Optional[float]
    exit_price: Optional[float]
    price_to_beat_bps: Optional[float]
    price_to_beat_usd: Optional[float]
    buffer_bps: Optional[float]
    buffer_usd: Optional[float]
    leader_price: Optional[float]
    counter_price: Optional[float]
    leader_edge: Optional[float]
    executable_sum_asks: Optional[float]
    executable_sum_bids: Optional[float]
    manual_score: int
    reaction_deadline_secs: Optional[float]
    reaction_label: str
    watch_window_eta_secs: Optional[float]
    one_shot_ready: bool
    window_id: str
    hold_to_resolution: bool
    last_update_ts: float
    compute_started_ts: float
    compute_finished_ts: float
    compute_latency_ms: float
    price_to_beat_side: str
    suggested_action: str
    suggested_detail: str
    active_setup_name: str = ""
    active_setup_market: str = ""
    active_action: str = ""
    active_side: str = ""
    active_price: Optional[float] = None
    market_context: str = ""
    next_seconds_outlook: str = ""
    risk_plan: str = ""
    exit_alert: str = ""
    status_note: str = ""
    entry_criteria: Optional[list[dict[str, Any]]] = None

    def as_dict(self) -> Dict:
        return {
            "ok": self.ok,
            "title": self.title,
            "slug": self.slug,
            "secs_to_end": self.secs_to_end,
            "spot_price": self.spot_price,
            "open_price": self.open_price,
            "spot_move_5s_bps": self.spot_move_5s_bps,
            "spot_move_15s_bps": self.spot_move_15s_bps,
            "spot_move_30s_bps": self.spot_move_30s_bps,
            "trend_label": self.trend_label,
            "trend_side": self.trend_side,
            "reversal_risk": self.reversal_risk,
            "safety_label": self.safety_label,
            "setup_side": self.setup_side,
            "setup_allowed": self.setup_allowed,
            "setup_reason": self.setup_reason,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "price_to_beat_bps": self.price_to_beat_bps,
            "price_to_beat_usd": self.price_to_beat_usd,
            "buffer_bps": self.buffer_bps,
            "buffer_usd": self.buffer_usd,
            "leader_price": self.leader_price,
            "counter_price": self.counter_price,
            "leader_edge": self.leader_edge,
            "executable_sum_asks": self.executable_sum_asks,
            "executable_sum_bids": self.executable_sum_bids,
            "manual_score": self.manual_score,
            "reaction_deadline_secs": self.reaction_deadline_secs,
            "reaction_label": self.reaction_label,
            "watch_window_eta_secs": self.watch_window_eta_secs,
            "one_shot_ready": self.one_shot_ready,
            "window_id": self.window_id,
            "hold_to_resolution": self.hold_to_resolution,
            "last_update_ts": self.last_update_ts,
            "compute_started_ts": self.compute_started_ts,
            "compute_finished_ts": self.compute_finished_ts,
            "compute_latency_ms": self.compute_latency_ms,
            "price_to_beat_side": self.price_to_beat_side,
            "suggested_action": self.suggested_action,
            "suggested_detail": self.suggested_detail,
            "active_setup_name": self.active_setup_name,
            "active_setup_market": self.active_setup_market,
            "active_action": self.active_action,
            "active_side": self.active_side,
            "active_price": self.active_price,
            "market_context": self.market_context,
            "next_seconds_outlook": self.next_seconds_outlook,
            "risk_plan": self.risk_plan,
            "exit_alert": self.exit_alert,
            "status_note": self.status_note,
            "entry_criteria": self.entry_criteria or [],
        }


@dataclass
class ManualActionPlanV1:
    scope_id: str
    slug: str
    setup_name: str
    side: str
    entry_price: float
    target_price: float
    stop_price: float
    created_at: float
    expires_at: float
    status: str = "active"  # active | consumed | canceled


class ManualOverlayEngineV1:
    def __init__(
        self,
        *,
        scalp_cfg: Optional[CurrentScalpConfigV1] = None,
        signal_cfg: Optional[CurrentAlmostResolvedConfigV1] = None,
    ) -> None:
        self.scalp_cfg = scalp_cfg or CurrentScalpConfigV1()
        self.signal_cfg = signal_cfg or CurrentAlmostResolvedConfigV1()
        self.current_scalp = CurrentScalpResearchV1(cfg=self.scalp_cfg)
        self.early_overresolved = CurrentEarlyOverresolvedResearchV1.from_mode(qty=50, mode="flex")
        self.slot_bundle: Optional[Dict] = None
        self.slot_bundle_refreshed_at: float = 0.0
        self.current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}
        self.last_queue_reason: str = "not_initialized"
        self.last_window_slug: str = ""
        self.window_consumed_by_slug: dict[str, bool] = {}
        self.manual_action_plans: dict[str, ManualActionPlanV1] = {}
        self.hold_to_resolution_mode: bool = True
        self.use_fast_reference: bool = True
        self.market_ws = CurrentMarketWsCache()

    def _ensure_slot_bundle(self) -> Dict:
        now = time.time()
        queue = (self.slot_bundle or {}).get("queue") or {}
        current = queue.get("current")
        should_refresh = (
            self.slot_bundle is None
            or now - self.slot_bundle_refreshed_at >= 8.0
            or not current
            or _slot_secs_to_end(current) in (None, 0)
        )
        if should_refresh:
            current_item = _fetch_current_item()
            current_slot = None
            if current_item:
                meta = fetch_market_metadata_from_slug(str(current_item.get("slug") or ""))
                if meta:
                    self.market_ws.configure(str(current_item.get("slug") or ""), meta.get("token_mapping") or [])
                    current_slot = {"item": current_item, "meta": meta}
            self.slot_bundle = {
                "queue": {"current": current_item},
                "slots": {"current": current_slot},
            }
            self.slot_bundle_refreshed_at = now
            queue = self.slot_bundle.get("queue") or {}

        if queue.get("current"):
            self.last_queue_reason = "ok"
        else:
            self.last_queue_reason = "queue_empty_or_api_unavailable"
        return self.slot_bundle

    def _build_ws_slot_state(self, slot_bundle: Dict) -> Optional[Dict[str, Any]]:
        current_slot = (slot_bundle.get("slots") or {}).get("current")
        current_item = (slot_bundle.get("queue") or {}).get("current")
        if not current_slot or not current_item:
            return None
        snap = self.market_ws.snapshot(max_age_secs=2.0)
        if not snap:
            return None
        if str(snap.get("slug") or "") != str(current_item.get("slug") or ""):
            return None
        up = snap.get("up")
        down = snap.get("down")
        if not up or not down:
            return None

        def _display_from_ws(item: dict) -> tuple[Optional[float], Optional[float], str]:
            bid = _safe_float(item.get("best_bid"), 0.0)
            ask = _safe_float(item.get("best_ask"), 0.0)
            midpoint = round((bid + ask) / 2.0, 6) if bid > 0 and ask > 0 and ask >= bid else None
            spread = round(max(0.0, ask - bid), 6) if bid > 0 and ask > 0 and ask >= bid else None
            if midpoint is not None and spread is not None and spread <= DISPLAY_SPREAD_WIDE_THRESHOLD:
                return midpoint, spread, "midpoint"
            ltp = _safe_float(item.get("last_trade_price"), 0.0)
            if ltp > 0:
                return ltp, spread, "last_trade_price"
            return midpoint, spread, "midpoint_fallback" if midpoint is not None else "none"

        joined = []
        for item in (up, down):
            display_price, spread, display_source = _display_from_ws(item)
            joined.append(
                {
                    "outcome": item.get("outcome"),
                    "token_id": item.get("token_id"),
                    "best_bid": item.get("best_bid"),
                    "best_ask": item.get("best_ask"),
                    "midpoint": round((_safe_float(item.get("best_bid"), 0.0) + _safe_float(item.get("best_ask"), 0.0)) / 2.0, 6)
                    if _safe_float(item.get("best_bid"), 0.0) > 0 and _safe_float(item.get("best_ask"), 0.0) > 0 and _safe_float(item.get("best_ask"), 0.0) >= _safe_float(item.get("best_bid"), 0.0)
                    else None,
                    "spread": spread,
                    "display_price": display_price,
                    "display_source": display_source,
                    "last_trade_price": item.get("last_trade_price"),
                    "executable_buy": item.get("best_ask"),
                    "executable_sell": item.get("best_bid"),
                    "tick_size": item.get("tick_size"),
                    "min_order_size": item.get("min_order_size"),
                    "top_bids": item.get("top_bids") or [],
                    "top_asks": item.get("top_asks") or [],
                    "raw_book_id": item.get("token_id"),
                    "has_raw_book": True,
                }
            )
        return {
            "current": {
                "item": current_slot["item"],
                "meta": current_slot["meta"],
                "books": joined,
            }
        }

    def _ensure_open_reference(self, current_item: dict) -> None:
        slug = str(current_item.get("slug") or "")
        if not slug:
            return
        if slug == self.current_open_reference.get("slug"):
            return
        raw_event = fetch_event_by_slug(slug)
        market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
        event_start_time = market.get("eventStartTime") or raw_event.get("startTime") if raw_event else None
        open_ref = fetch_binance_open_price_for_event_start_v1(event_start_time)
        self.current_open_reference = {
            "slug": slug,
            "price": open_ref.get("open_price"),
            "event_start_time": event_start_time,
        }

    def _infer_trend(self, scalp_signal: Dict) -> tuple[str, str]:
        setup = str(scalp_signal.get("setup") or "")
        side = str(scalp_signal.get("side") or "NEUTRAL")
        d5 = _safe_float(scalp_signal.get("spot_delta_5s_bps"), 0.0)
        d15 = _safe_float(scalp_signal.get("spot_delta_15s_bps"), 0.0)
        distance = _safe_float(scalp_signal.get("distance_from_open_bps"), 0.0)

        if setup == "continuation" and side in ("UP", "DOWN"):
            return f"{side} continuation", side
        if setup == "reversal" and side in ("UP", "DOWN"):
            return f"{side} reversal", side
        if distance >= self.scalp_cfg.trend_distance_bps and d5 >= 0 and d15 >= 0:
            return "UP bias", "UP"
        if distance <= -self.scalp_cfg.trend_distance_bps and d5 <= 0 and d15 <= 0:
            return "DOWN bias", "DOWN"
        return "Neutral", "NEUTRAL"

    def _market_label(self, slug: str) -> str:
        slug_l = str(slug or "").lower()
        if "15m" in slug_l:
            return "BTC 15min"
        if "5m" in slug_l:
            return "BTC 5min"
        if "1h" in slug_l or "hour" in slug_l:
            return "BTC 1h"
        return "BTC current"

    def _setup_display_name(self, setup: str, variant: str = "") -> str:
        name = str(setup or "")
        variant = str(variant or "")
        if name == "early_overresolved_reversal":
            return "current overresolved reversal"
        if name == "almost_resolved":
            if variant == "dual_rich_late_limit":
                return "almost resolved dual rich"
            if variant == "controlled_late_entry":
                return "almost resolved late"
            if variant == "resolved_pullback_limit":
                return "almost resolved pullback"
            if variant == "extreme_99_limit":
                return "almost resolved extreme 99"
            if variant == "passive_extreme_liquidity_capture":
                return "almost resolved passive capture"
            return "almost resolved"
        if name == "continuation":
            return "scalp current continuation"
        if name == "reversal":
            return "scalp current reversal"
        if name == "center_scalp":
            return "scalp center book"
        if name == "mid_book_maker":
            return "scalp current maker"
        if name == "range_reversion":
            return "scalp range reversion"
        if name == "micro_imbalance_refill":
            return "scalp micro refill"
        return name.replace("_", " ").strip() or "sem setup"

    def _build_market_context(self, scalp_signal: Dict, almost_signal: Dict, early_signal: Dict, trend_label: str) -> str:
        spot_5s = _safe_float(scalp_signal.get("spot_delta_5s_bps"), 0.0)
        spot_15s = _safe_float(scalp_signal.get("spot_delta_15s_bps"), 0.0)
        market_5s = _safe_float(scalp_signal.get("market_delta_5s"), 0.0)
        range_15s = max(
            _safe_float(scalp_signal.get("market_range_15s"), 0.0),
            _safe_float(early_signal.get("market_range_window"), 0.0),
            _safe_float(almost_signal.get("market_range_15s"), 0.0),
        )
        if abs(range_15s) <= 0.01 and abs(spot_5s) <= 0.2 and abs(spot_15s) <= 0.5:
            return "book comprimido perto do centro, spot quase parado"
        if spot_5s > 0.5 and market_5s > 0:
            return f"spot puxando para cima e book acompanhando, {trend_label.lower()}"
        if spot_5s < -0.5 and market_5s < 0:
            return f"spot puxando para baixo e book acompanhando, {trend_label.lower()}"
        if abs(spot_5s) <= 0.25 and abs(spot_15s) <= 0.75 and range_15s >= 0.02:
            return "lateral curto com micro oscilacao no current"
        if abs(spot_15s) >= 2.0:
            return "spot esticado, risco de follow-through ou pullback rapido"
        return f"mercado misto, {trend_label.lower()}"

    def _build_next_seconds_outlook(self, scalp_signal: Dict, almost_signal: Dict, early_signal: Dict, selected: Dict) -> str:
        setup = str(selected.get("setup") or "")
        side = str(selected.get("side") or "")
        spot_5s = _safe_float(scalp_signal.get("spot_delta_5s_bps"), 0.0)
        market_5s = _safe_float(scalp_signal.get("market_delta_5s"), 0.0)
        leader_ask = _safe_float(early_signal.get("leader_ask"), 0.0)
        if setup == "early_overresolved_reversal":
            if side == "UP":
                return "agir so se up comecar a reagir no book"
            if side == "DOWN":
                return "agir so se down comecar a reagir no book"
        if setup == "almost_resolved":
            if side == "UP":
                return "lado up ainda parece lider para os proximos segundos"
            if side == "DOWN":
                return "lado down ainda parece lider para os proximos segundos"
        if setup in ("continuation", "reversal", "center_scalp", "mid_book_maker", "range_reversion", "micro_imbalance_refill"):
            if side == "UP" and (spot_5s > 0 or market_5s > 0):
                return "pressao curta favorece up nos proximos segundos"
            if side == "DOWN" and (spot_5s < 0 or market_5s < 0):
                return "pressao curta favorece down nos proximos segundos"
            if leader_ask >= 0.9:
                return "mercado esticado, buscar 1 tick e sair rapido"
            return "janela curta, agir rapido e nao carregar"
        return "sem direcao curta suficientemente clara"

    def _manual_odds_confirmed_for_early_reversal(self, early_signal: Dict, scalp_signal: Dict) -> tuple[bool, str]:
        side = str(early_signal.get("side") or "")
        market_5s = _safe_float(early_signal.get("market_delta_5s", scalp_signal.get("market_delta_5s")), 0.0)
        market_15s = _safe_float(early_signal.get("market_delta_15s", scalp_signal.get("market_delta_15s")), 0.0)
        spot_5s = _safe_float(early_signal.get("spot_delta_5s_bps", scalp_signal.get("spot_delta_5s_bps")), 0.0)
        leader_side = str(early_signal.get("leader_side") or "")
        leader_ask = _safe_float(early_signal.get("leader_ask"), 0.0)
        counter_ask = _safe_float(early_signal.get("counter_ask"), 0.0)

        if side == "UP":
            odds_turning = market_5s >= 0.01 and market_15s >= -0.005
            spot_not_fighting = spot_5s >= -0.10
            if odds_turning and spot_not_fighting:
                return True, "odds do up reagindo"
            return False, "up ainda nao reagiu nas odds"

        if side == "DOWN":
            odds_turning = market_5s <= -0.01 and market_15s <= 0.005
            spot_not_fighting = spot_5s <= 0.10
            if odds_turning and spot_not_fighting:
                return True, "odds do down reagindo"
            return False, "down ainda nao reagiu nas odds"

        if leader_side and leader_ask > 0 and counter_ask > 0:
            return False, f"lider {leader_side} ainda esticado"
        return False, "odds ainda contra a entrada"

    def _choose_active_setup(
        self,
        *,
        slug: str,
        current_secs: Optional[int],
        scalp_signal: Dict,
        almost_signal: Dict,
        early_signal: Dict,
        suggested_action: str,
        setup_side: str,
    ) -> Dict[str, Any]:
        market_label = self._market_label(slug)
        candidates: list[dict[str, Any]] = []

        if early_signal.get("allow"):
            early_ok, early_manual_reason = self._manual_odds_confirmed_for_early_reversal(early_signal, scalp_signal)
            if early_ok:
                candidates.append(
                    {
                        "priority": 100 if current_secs is not None and current_secs >= 90 else 70,
                        "setup": str(early_signal.get("setup") or ""),
                        "setup_name": self._setup_display_name(early_signal.get("setup")),
                        "market": market_label,
                        "action": f"comprar {early_signal.get('side')}".strip(),
                        "side": str(early_signal.get("side") or ""),
                        "price": early_signal.get("entry_price"),
                        "reason": str(early_signal.get("reason") or ""),
                    }
                )
            else:
                candidates.append(
                    {
                        "priority": 15,
                        "setup": str(early_signal.get("setup") or ""),
                        "setup_name": self._setup_display_name(early_signal.get("setup")),
                        "market": market_label,
                        "action": "aguardar",
                        "side": str(early_signal.get("side") or ""),
                        "price": None,
                        "reason": early_manual_reason,
                    }
                )

        scalp_setup = str(scalp_signal.get("setup") or "")
        if scalp_signal.get("allow") and scalp_setup != "no_edge":
            candidates.append(
                {
                    "priority": 80 if scalp_setup in ("reversal", "continuation") else 65,
                    "setup": scalp_setup,
                    "setup_name": self._setup_display_name(scalp_setup),
                    "market": market_label,
                    "action": f"comprar {scalp_signal.get('side')}".strip(),
                    "side": str(scalp_signal.get("side") or ""),
                    "price": scalp_signal.get("entry_price"),
                    "reason": str(scalp_signal.get("reason") or ""),
                }
            )

        if almost_signal.get("allow"):
            candidates.append(
                {
                    "priority": 90 if current_secs is not None and current_secs <= 60 else 60,
                    "setup": str(almost_signal.get("setup") or ""),
                    "setup_name": self._setup_display_name(almost_signal.get("setup"), almost_signal.get("setup_variant")),
                    "market": market_label,
                    "action": suggested_action.lower(),
                    "side": setup_side,
                    "price": almost_signal.get("entry_price"),
                    "reason": str(almost_signal.get("reason") or ""),
                }
            )

        if candidates:
            candidates.sort(key=lambda item: item["priority"], reverse=True)
            return candidates[0]

        fallback_price = scalp_signal.get("entry_price")
        fallback_side = str(scalp_signal.get("side") or setup_side or "")
        return {
            "priority": 0,
            "setup": str(scalp_signal.get("setup") or "no_edge"),
            "setup_name": self._setup_display_name(scalp_signal.get("setup")),
            "market": market_label,
            "action": suggested_action.lower(),
            "side": fallback_side,
            "price": fallback_price,
            "reason": str(scalp_signal.get("reason") or almost_signal.get("reason") or "sem setup"),
        }

    def _plan_scope_id(self, slug: str, setup_name: str, side: str) -> str:
        return f"{slug}:{setup_name}:{side}"

    def _current_side_ask(self, snap: Dict, side: str) -> Optional[float]:
        side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
        ask = _safe_float(side_book.get("executable_buy") or side_book.get("best_ask"), -1.0)
        return ask if ask > 0 else None

    def _apply_manual_plan(self, *, slug: str, snap: Dict, active_setup: Dict[str, Any], now_ts: float) -> Dict[str, Any]:
        setup_name = str(active_setup.get("setup_name") or "")
        action = str(active_setup.get("action") or "").lower()
        side = str(active_setup.get("side") or "")
        entry_price = _safe_float(active_setup.get("price"), -1.0)
        if not setup_name or side not in ("UP", "DOWN") or entry_price <= 0 or not (action.startswith("comprar") or action.startswith("limite")):
            return active_setup

        scope_id = self._plan_scope_id(slug, setup_name, side)
        current_ask = self._current_side_ask(snap, side)
        existing = self.manual_action_plans.get(scope_id)

        if existing and existing.slug != slug:
            existing = None

        if existing is None:
            plan = ManualActionPlanV1(
                scope_id=scope_id,
                slug=slug,
                setup_name=setup_name,
                side=side,
                entry_price=round(entry_price, 2),
                target_price=round(min(0.99, entry_price + 0.01), 2),
                stop_price=round(max(0.01, entry_price - 0.01), 2),
                created_at=now_ts,
                expires_at=now_ts + 8.0,
            )
            self.manual_action_plans[scope_id] = plan
            active_setup["action"] = f"comprar {side} {plan.entry_price:.2f}"
            active_setup["plan_message"] = f"alvo {plan.target_price:.2f}, stop {plan.stop_price:.2f}, valido 8s"
            return active_setup

        if existing.status != "active":
            active_setup["action"] = "aguardar"
            active_setup["price"] = None
            active_setup["plan_message"] = "plano anterior encerrado, nao perseguir novo preco"
            return active_setup

        if current_ask is not None and (current_ask <= existing.stop_price or current_ask >= existing.target_price):
            existing.status = "canceled"
            active_setup["action"] = "aguardar"
            active_setup["price"] = None
            active_setup["plan_message"] = f"plano cancelado, perdeu {existing.entry_price:.2f}/{existing.target_price:.2f}"
            return active_setup

        if now_ts > existing.expires_at:
            existing.status = "consumed"
            active_setup["action"] = "aguardar"
            active_setup["price"] = None
            active_setup["plan_message"] = "janela de acao expirou, nao perseguir"
            return active_setup

        active_setup["action"] = f"comprar {existing.side} {existing.entry_price:.2f}"
        active_setup["price"] = existing.entry_price
        active_setup["plan_message"] = f"alvo {existing.target_price:.2f}, stop {existing.stop_price:.2f}, valido {max(0, int(round(existing.expires_at - now_ts)))}s"
        return active_setup

    def _infer_reversal_risk(self, almost_signal: Dict, scalp_signal: Dict) -> str:
        side = str(almost_signal.get("side") or "")
        if not side:
            return "high"
        buffer_bps = _safe_float(
            almost_signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"),
            0.0,
        )
        adverse_bps = _safe_float(
            almost_signal.get("up_adverse_spot_bps" if side == "UP" else "down_adverse_spot_bps"),
            0.0,
        )
        distance_bps = max(0.0001, _safe_float(almost_signal.get("distance_to_price_to_beat_bps"), 0.0))
        scalp_setup = str(scalp_signal.get("setup") or "")
        scalp_side = str(scalp_signal.get("side") or "")

        if scalp_setup == "reversal" and scalp_side and scalp_side != side:
            return "high"
        if buffer_bps <= self.signal_cfg.paper_profit_take_on_reversal_buffer_bps:
            return "high"
        if adverse_bps >= distance_bps * self.signal_cfg.max_reversal_share_of_open_distance:
            return "high"
        if scalp_setup == "reversal" and scalp_side == side:
            return "medium"
        if buffer_bps <= max(self.signal_cfg.min_price_to_beat_buffer_bps * 2.0, 5.0):
            return "medium"
        return "low"

    def _infer_safety(self, almost_signal: Dict, scalp_signal: Dict, reversal_risk: str, trend_side: str) -> tuple[str, str]:
        if not almost_signal.get("allow"):
            return "BLOCKED", str(almost_signal.get("reason") or "setup_blocked")

        side = str(almost_signal.get("side") or "")
        reason = str(almost_signal.get("reason") or "")
        aligned = trend_side in ("NEUTRAL", side)
        fallback = "fallback" in reason

        if reversal_risk == "low" and aligned and not fallback:
            return "SAFE", "leader stable and buffer intact"
        if reversal_risk == "high":
            return "UNSAFE", "reversal pressure too high"
        return "CAUTION", "setup valid but context mixed"

    def _manual_score(self, almost_signal: Dict, scalp_signal: Dict, reversal_risk: str, trend_side: str) -> int:
        if not almost_signal.get("allow"):
            return 0
        side = str(almost_signal.get("side") or "")
        score = 50
        if trend_side == side:
            score += 12
        elif trend_side == "NEUTRAL":
            score += 4
        if reversal_risk == "low":
            score += 20
        elif reversal_risk == "medium":
            score += 8
        else:
            score -= 30

        buffer_bps = _safe_float(
            almost_signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"),
            0.0,
        )
        exit_distance = _safe_float(
            almost_signal.get("up_exit_distance" if side == "UP" else "down_exit_distance"),
            1.0,
        )
        edge = _safe_float(almost_signal.get("up_edge_vs_counter" if side == "UP" else "down_edge_vs_counter"), 0.0)
        secs = _safe_float(almost_signal.get("secs_to_end"), 0.0)

        if buffer_bps >= 6.0:
            score += 12
        elif buffer_bps >= 4.0:
            score += 6
        else:
            score -= 10

        if self.hold_to_resolution_mode:
            if exit_distance <= 0.01:
                score -= 26
            elif exit_distance <= 0.015:
                score -= 16
        else:
            if exit_distance <= 0.015:
                score -= 18
            elif exit_distance <= 0.025:
                score -= 8

        if edge >= 0.95:
            score += 8
        elif edge < 0.9:
            score -= 8

        if secs >= 55:
            score += 8
        elif secs <= 25:
            score -= 20
        elif secs <= 35:
            score -= 8

        return max(0, min(100, int(round(score))))

    def _reaction_deadline_secs(self, almost_signal: Dict) -> tuple[Optional[float], str]:
        if not almost_signal.get("allow"):
            return None, "wait"
        side = str(almost_signal.get("side") or "")
        secs_to_end = _safe_float(almost_signal.get("secs_to_end"), 0.0)
        exit_distance = _safe_float(
            almost_signal.get("up_exit_distance" if side == "UP" else "down_exit_distance"),
            1.0,
        )
        buffer_bps = _safe_float(
            almost_signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"),
            0.0,
        )

        if self.hold_to_resolution_mode:
            deadline = secs_to_end - 18.0
            if exit_distance <= 0.01:
                deadline -= 10.0
            elif exit_distance <= 0.015:
                deadline -= 6.0
            if buffer_bps <= 4.0:
                deadline -= 4.0
        else:
            deadline = secs_to_end - 15.0
            if exit_distance <= 0.015:
                deadline -= 10.0
            elif exit_distance <= 0.025:
                deadline -= 5.0
            if buffer_bps <= 4.0:
                deadline -= 5.0

        deadline = max(0.0, deadline)
        if deadline <= 5:
            return deadline, "act_now"
        if deadline <= 12:
            return deadline, "short"
        return deadline, "ok"

    def _window_state(self, slug: str, score: int, almost_signal: Dict) -> tuple[bool, str]:
        if slug != self.last_window_slug:
            self.last_window_slug = slug
            self.window_consumed_by_slug.setdefault(slug, False)
        if not almost_signal.get("allow"):
            return False, slug
        if score < 60:
            return False, slug
        if self.window_consumed_by_slug.get(slug, False):
            return False, slug
        self.window_consumed_by_slug[slug] = True
        return True, slug

    def _watch_window_eta_secs(self, secs_to_end: Optional[int]) -> Optional[float]:
        if secs_to_end is None:
            return None
        if secs_to_end > self.signal_cfg.max_secs_to_end:
            return float(secs_to_end - self.signal_cfg.max_secs_to_end)
        return 0.0

    def _fallback_side_from_context(self, scalp_signal: Dict, snap: Dict) -> str:
        scalp_side = str(scalp_signal.get("side") or "")
        if scalp_side in ("UP", "DOWN"):
            return scalp_side
        up = snap.get("up") or {}
        down = snap.get("down") or {}
        up_buy = _safe_float(up.get("executable_buy") or up.get("best_ask"), -1.0)
        down_buy = _safe_float(down.get("executable_buy") or down.get("best_ask"), -1.0)
        if up_buy >= down_buy:
            return "UP"
        if down_buy > up_buy:
            return "DOWN"
        distance = _safe_float(scalp_signal.get("distance_from_open_bps"), 0.0)
        return "UP" if distance >= 0 else "DOWN"

    def _build_entry_criteria(
        self,
        *,
        current_secs: Optional[int],
        setup_side: str,
        almost_signal: Dict,
        scalp_signal: Dict,
        reversal_risk: str,
        score: int,
    ) -> list[dict[str, Any]]:
        cfg = self.signal_cfg
        side = setup_side if setup_side in ("UP", "DOWN") else self._fallback_side_from_context(scalp_signal, {})
        leader_key = "up_buy" if side == "UP" else "down_buy"
        counter_key = "down_buy" if side == "UP" else "up_buy"
        buffer_bps_key = "up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"
        buffer_usd_key = "up_price_to_beat_buffer_usd" if side == "UP" else "down_price_to_beat_buffer_usd"
        adverse_5s = max(
            0.0,
            -_safe_float(scalp_signal.get("spot_delta_5s_bps"), 0.0)
            if side == "UP"
            else _safe_float(scalp_signal.get("spot_delta_5s_bps"), 0.0),
        )
        adverse_15s = max(
            0.0,
            -_safe_float(scalp_signal.get("spot_delta_15s_bps"), 0.0)
            if side == "UP"
            else _safe_float(scalp_signal.get("spot_delta_15s_bps"), 0.0),
        )
        adverse_30s = max(
            0.0,
            -_safe_float(scalp_signal.get("spot_delta_30s_bps"), 0.0)
            if side == "UP"
            else _safe_float(scalp_signal.get("spot_delta_30s_bps"), 0.0),
        )
        distance_bps = _safe_float(almost_signal.get("distance_to_price_to_beat_bps"), 0.0)
        if distance_bps <= 0:
            distance_bps = abs(_safe_float(scalp_signal.get("distance_from_open_bps"), 0.0))
        distance_usd = _safe_float(almost_signal.get("distance_to_price_to_beat_usd"), 0.0)
        if distance_usd <= 0:
            reference_price = _safe_float(scalp_signal.get("reference_price"), 0.0)
            opening_reference_price = _safe_float(scalp_signal.get("opening_reference_price"), 0.0)
            if reference_price > 0 and opening_reference_price > 0:
                distance_usd = abs(reference_price - opening_reference_price)
        recent_vol = _safe_float(almost_signal.get("resolved_pullback_recent_vol_floor_usd"), 0.0)
        leader_price = _safe_float(almost_signal.get(leader_key), -1.0)
        counter_price = _safe_float(almost_signal.get(counter_key), -1.0)
        score_key = "passive_capture_up_score" if side == "UP" else "passive_capture_down_score"
        passive_score = int(_safe_float(almost_signal.get(score_key), 0.0))
        market_range_30s = _safe_float(almost_signal.get("market_range_30s"), 0.0)
        buffer_bps = _safe_float(almost_signal.get(buffer_bps_key), 0.0)
        if buffer_bps <= 0 and distance_bps > 0:
            buffer_bps = max(0.0, distance_bps - max(adverse_5s, adverse_15s, adverse_30s))
        buffer_usd = _safe_float(almost_signal.get(buffer_usd_key), 0.0)
        if buffer_usd <= 0 and buffer_bps > 0:
            reference_price = _safe_float(scalp_signal.get("reference_price"), 0.0)
            if reference_price > 0:
                buffer_usd = reference_price * buffer_bps / 10000.0
        secs = None if current_secs is None else int(current_secs)
        distance_from_open_bps = _safe_float(scalp_signal.get("distance_from_open_bps"), 0.0)
        direction_ok = (
            (side == "UP" and distance_from_open_bps > 0)
            or (side == "DOWN" and distance_from_open_bps < 0)
        )
        depth_key = "up_depth_top3_both_sides" if side == "UP" else "down_depth_top3_both_sides"
        depth = _safe_float(almost_signal.get(depth_key), 0.0)
        required_distance = passive_capture_required_distance_v1(
            secs_to_end=secs or 0,
            recent_vol_floor_usd=recent_vol,
            cfg=cfg,
        )
        required_distance_usd = _safe_float(
            almost_signal.get("passive_capture_required_distance_usd"),
            _safe_float(required_distance.get("required_usd"), cfg.passive_capture_min_safe_distance_usd),
        )
        fixed_min_distance = _safe_float(
            almost_signal.get("passive_capture_fixed_min_distance_usd"),
            _safe_float(required_distance.get("fixed_min_usd"), cfg.passive_capture_min_safe_distance_usd),
        )
        vol_min_distance = _safe_float(
            almost_signal.get("passive_capture_vol_min_distance_usd"),
            _safe_float(required_distance.get("vol_min_usd"), recent_vol * cfg.passive_capture_min_distance_vs_recent_vol_mult),
        )
        vol_mult = _safe_float(
            almost_signal.get("passive_capture_vol_mult"),
            _safe_float(required_distance.get("vol_mult"), cfg.passive_capture_min_distance_vs_recent_vol_mult),
        )
        distance_tier = str(almost_signal.get("passive_capture_distance_tier") or required_distance.get("tier") or "-")
        safe_distance_ok = (
            distance_usd >= required_distance_usd
            and distance_bps >= cfg.passive_capture_min_safe_distance_bps
        )
        max_score_without_extra_distance = 82 if secs is not None and secs <= cfg.passive_capture_preferred_secs else 78
        distance_bonus = min(18, int(max(0.0, distance_usd - cfg.passive_capture_min_safe_distance_usd) / 8.0))

        def criterion(key: str, label: str, value: Any, accepted: str, ok: bool, detail: str = "") -> dict[str, Any]:
            return {
                "key": key,
                "label": label,
                "value": value,
                "accepted": accepted,
                "ok": bool(ok),
                "detail": detail,
            }

        return [
            criterion(
                "time_window",
                "Tempo para o fim",
                "-" if secs is None else f"{secs}s",
                f"{cfg.extreme_99_min_secs_to_end}-{cfg.passive_capture_max_secs}s",
                secs is not None and cfg.extreme_99_min_secs_to_end <= secs <= cfg.passive_capture_max_secs,
            ),
            criterion(
                "leader_price",
                "Lado quase resolvido",
                f"{side or '-'} {leader_price:.2f}" if leader_price >= 0 else "-",
                f">= {cfg.passive_capture_min_leader_price:.2f}; preferivel >= {cfg.passive_capture_preferred_leader_price:.2f}",
                side in ("UP", "DOWN") and leader_price >= cfg.passive_capture_min_leader_price,
            ),
            criterion(
                "counter_price",
                "Preco do lado contrario",
                f"{counter_price:.2f}" if counter_price >= 0 else "-",
                f"<= {cfg.passive_capture_max_counter_price:.2f}",
                counter_price >= 0 and counter_price <= cfg.passive_capture_max_counter_price,
            ),
            criterion(
                "price_to_beat_distance",
                "Distancia do price to beat",
                f"${distance_usd:.2f} / {distance_bps:.2f}bps",
                f">= ${required_distance_usd:.2f}, >= {cfg.passive_capture_min_safe_distance_bps:.2f}bps",
                safe_distance_ok,
                f"modelo hibrido {distance_tier}; piso ${fixed_min_distance:.2f}; vol {vol_mult:.1f}x = ${vol_min_distance:.2f}",
            ),
            criterion(
                "price_to_beat_direction",
                "Direcao do price to beat",
                f"{distance_from_open_bps:.2f}bps",
                "UP precisa estar acima do price to beat; DOWN precisa estar abaixo",
                direction_ok,
            ),
            criterion(
                "buffer",
                "Buffer apos reversao",
                f"${buffer_usd:.2f} / {buffer_bps:.2f}bps",
                f">= ${cfg.near_end_min_price_to_beat_buffer_usd:.2f} e >= {cfg.near_end_min_price_to_beat_buffer_bps:.2f}bps",
                buffer_usd >= cfg.near_end_min_price_to_beat_buffer_usd
                and buffer_bps >= cfg.near_end_min_price_to_beat_buffer_bps,
            ),
            criterion(
                "adverse_5s",
                "Reversao contra em 5s",
                f"{adverse_5s:.2f}bps",
                f"<= {cfg.passive_capture_max_adverse_spot_5s_bps:.2f}bps",
                adverse_5s <= cfg.passive_capture_max_adverse_spot_5s_bps,
            ),
            criterion(
                "adverse_15s",
                "Reversao contra em 15s",
                f"{adverse_15s:.2f}bps",
                f"<= {cfg.passive_capture_max_adverse_spot_15s_bps:.2f}bps",
                adverse_15s <= cfg.passive_capture_max_adverse_spot_15s_bps,
            ),
            criterion(
                "adverse_30s",
                "Reversao contra em 30s",
                f"{adverse_30s:.2f}bps",
                f"<= {cfg.passive_capture_max_adverse_spot_30s_bps:.2f}bps",
                adverse_30s <= cfg.passive_capture_max_adverse_spot_30s_bps,
            ),
            criterion(
                "market_range_30s",
                "Range do book em 30s",
                f"{market_range_30s:.3f}",
                f"<= {cfg.passive_capture_max_market_range_30s:.3f}",
                market_range_30s <= cfg.passive_capture_max_market_range_30s,
            ),
            criterion(
                "passive_score",
                "Score de captura",
                str(passive_score),
                f">= {cfg.passive_capture_min_score}",
                passive_score >= cfg.passive_capture_min_score,
                f"bonus distancia={distance_bonus}; base maxima sem folga extra={max_score_without_extra_distance}; profundidade={depth:.2f}/{cfg.min_depth_top3:.2f}",
            ),
            criterion(
                "reversal_risk",
                "Risco de reversao",
                reversal_risk,
                "precisa ser low ou medium",
                reversal_risk != "high",
            ),
            criterion(
                "runner_decision",
                "Decisao final do setup",
                str(almost_signal.get("reason") or "-"),
                "setup quase resolvido liberado; risco aceitavel",
                bool(almost_signal.get("allow")) and reversal_risk != "high" and score >= 55,
            ),
            criterion(
                "setup_variant",
                "Variante / regra de saida",
                str(almost_signal.get("setup_variant") or "standard"),
                "informa a regra de saida aplicavel para esta entrada",
                True,
                _variant_exit_hint(str(almost_signal.get("setup_variant") or "")),
            ),
        ]

    def _suggested_action(
        self,
        *,
        current_secs: Optional[int],
        setup_side: str,
        trend_side: str,
        almost_signal: Dict,
        scalp_signal: Dict,
        reversal_risk: str,
        score: int,
    ) -> tuple[str, str, str, str]:
        allow = bool(almost_signal.get("allow"))
        reason = str(almost_signal.get("reason") or "")
        side = setup_side if setup_side in ("UP", "DOWN") else trend_side if trend_side in ("UP", "DOWN") else "NEUTRAL"
        exit_distance = _safe_float(
            almost_signal.get("up_exit_distance" if side == "UP" else "down_exit_distance"),
            999.0,
        )
        buffer_bps = _safe_float(
            almost_signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"),
            0.0,
        )
        buffer_usd = _safe_float(
            almost_signal.get("up_price_to_beat_buffer_usd" if side == "UP" else "down_price_to_beat_buffer_usd"),
            0.0,
        )
        distance_bps = _safe_float(almost_signal.get("distance_to_price_to_beat_bps"), 0.0)
        distance_usd = _safe_float(almost_signal.get("distance_to_price_to_beat_usd"), 0.0)
        leader_price = _safe_float(almost_signal.get("up_buy" if side == "UP" else "down_buy"), -1.0)
        counter_price = _safe_float(almost_signal.get("down_buy" if side == "UP" else "up_buy"), -1.0)
        market_range_15s = _safe_float(almost_signal.get("market_range_15s"), 0.0)
        market_range_30s = _safe_float(almost_signal.get("market_range_30s"), 0.0)
        spot_delta_5s = _safe_float(scalp_signal.get("spot_delta_5s_bps"), 0.0)
        spot_delta_15s = _safe_float(scalp_signal.get("spot_delta_15s_bps"), 0.0)
        side_sign = 1.0 if side == "UP" else -1.0 if side == "DOWN" else 0.0
        adverse_5s = -side_sign * spot_delta_5s if side_sign else 0.0
        adverse_15s = -side_sign * spot_delta_15s if side_sign else 0.0
        controlled_late_window = (
            side in ("UP", "DOWN")
            and current_secs is not None
            and self.signal_cfg.controlled_late_min_secs <= current_secs <= self.signal_cfg.controlled_late_max_secs
            and self.signal_cfg.controlled_late_min_distance_usd <= distance_usd <= self.signal_cfg.controlled_late_max_distance_usd
            and self.signal_cfg.controlled_late_min_entry_price <= leader_price <= self.signal_cfg.controlled_late_max_entry_price
            and 0.0 <= counter_price <= self.signal_cfg.controlled_late_max_counter_price
            and market_range_30s > 0
            and market_range_30s <= max(self.signal_cfg.controlled_late_max_market_range_30s, distance_bps / 10000.0)
            and market_range_15s <= max(self.signal_cfg.controlled_late_max_market_range_15s, market_range_30s)
        )
        setup_variant = str(almost_signal.get("setup_variant") or "")
        exit_alert = "MANTER"
        if setup_variant == "extreme_99_limit":
            exit_alert = "HOLD ATE RESOLUCAO | stop ignorado para extreme 99"
        elif side in ("UP", "DOWN"):
            if adverse_5s >= 1.2 or adverse_15s >= 1.8 or reversal_risk == "high":
                exit_alert = "SAIR NA PRIMEIRA OSCILACAO CONTRA"
            elif setup_variant == "controlled_late_entry" and market_range_15s >= self.signal_cfg.controlled_late_max_market_range_15s:
                exit_alert = "REALIZAR LUCRO | range alto para entrada tardia"
            elif setup_variant == "resolved_pullback_limit" and (
                market_range_15s >= self.signal_cfg.near_end_max_market_range_15s
                or market_range_30s >= self.signal_cfg.paper_profit_take_on_market_range_30s
            ):
                exit_alert = "SAIR | pullback esgotou | range deteriorando"
            elif market_range_15s >= 0.02 or market_range_30s >= 0.03:
                exit_alert = "REALIZAR RAPIDO EM OSCILACAO"
            elif current_secs is not None and current_secs <= 12 and buffer_bps >= 4.0:
                exit_alert = "HOLD ATE O FINAL SE DISTANCIA ABRIR"

        if not allow:
            if reason == "outside_time_window" and current_secs is not None and current_secs > self.signal_cfg.max_secs_to_end:
                if side in ("UP", "DOWN"):
                    return f"OBSERVAR {side}", "aguarde a janela operacional e confirme continuidade do spot", "SEM ENTRADA", exit_alert
                return "AGUARDAR", "fora da janela operacional", "SEM ENTRADA", exit_alert
            if (
                controlled_late_window
                and adverse_5s <= self.signal_cfg.controlled_late_max_adverse_spot_5s_bps
                and adverse_15s <= self.signal_cfg.controlled_late_max_adverse_spot_15s_bps
                and buffer_bps >= self.signal_cfg.controlled_late_min_buffer_bps
                and buffer_usd >= self.signal_cfg.controlled_late_min_buffer_usd
            ):
                return (
                    f"COMPRAR {side}",
                    "ENTRADA PEQUENA 95-98 | HOLD SE DISTANCIA ABRIR",
                    "RISCO PEQUENO | SAIR RAPIDO EM OSCILACAO CONTRA",
                    exit_alert,
                )
            if (
                side in ("UP", "DOWN")
                and current_secs is not None
                and current_secs <= self.signal_cfg.resolved_pullback_max_secs
                and leader_price >= self.signal_cfg.resolved_pullback_min_leader_price
                and counter_price <= self.signal_cfg.resolved_pullback_max_counter_price
                and reversal_risk != "high"
            ):
                return (
                    f"LIMITE {side} 0.98",
                    "somente com cotacao atual em 0.99, apos ja ter tocado 0.99, e com micro-reversao controlada para buscar o 0.98",
                    "RISCO CONTROLADO | STOP 0.96 | ACEITAR FILL PARCIAL OU NENHUM FILL",
                    "CANCELAR SE O SPOT MOSTRAR REVERSAO OU SE PERDER O ESTADO DE QUASE RESOLVIDO",
                )
            if reversal_risk == "high" or reason in ("invalid_book_both_sides_rich", "leader_not_stable_enough_or_not_priced_for_ticks"):
                return "EVITAR", "spot ou book sem estabilidade suficiente para entrada manual", "SEM ENTRADA", exit_alert
            if side in ("UP", "DOWN") and distance_bps >= max(3.0, self.signal_cfg.min_price_to_beat_distance_bps * 0.6):
                return f"OBSERVAR {side}", "direcao provável existe, mas ainda sem confirmação para clicar", "SEM ENTRADA", exit_alert
            return "AGUARDAR", "sem vantagem clara agora", "SEM ENTRADA", exit_alert

        if reversal_risk == "high" or score < 55:
            return "EVITAR", "setup existe, mas o risco de reversão ainda está alto", "SEM ENTRADA", exit_alert
        if side not in ("UP", "DOWN"):
            return "AGUARDAR", "lado ainda indefinido", "SEM ENTRADA", exit_alert
        if setup_variant == "extreme_99_limit":
            return (
                f"COMPRAR {side}",
                "ENTRADA 0.99 | HOLD ATE RESOLUCAO SEMPRE",
                "RISCO ELEVADO | NAO USAR STOP | SEGURAR ATE FIM",
                exit_alert,
            )
        if controlled_late_window:
            return (
                f"COMPRAR {side}",
                "ENTRADA PEQUENA 95-98 | HOLD SE DISTANCIA ABRIR",
                "RISCO PEQUENO | REDUZIR RAPIDO SE OSCILAR CONTRA",
                exit_alert,
            )
        if self.hold_to_resolution_mode and exit_distance > 0.015 and buffer_bps >= max(4.0, self.signal_cfg.min_price_to_beat_buffer_bps):
            return f"COMPRAR {side}", "HOLD ATE RESOLUCAO", "RISCO NORMAL | SEGURAR ENQUANTO DISTANCIA ABRIR", exit_alert
        return f"COMPRAR {side}", "BATER 0.99", "RISCO NORMAL | REALIZAR NO PRIMEIRO PRECO BOM", exit_alert

    def read_snapshot(self) -> ManualOverlaySnapshotV1:
        compute_started_ts = time.time()
        now = compute_started_ts
        try:
            slot_bundle = self._ensure_slot_bundle()
            current_item = slot_bundle["queue"].get("current")
            if not current_item:
                compute_finished_ts = time.time()
                return ManualOverlaySnapshotV1(
                    ok=False,
                    title="Current slot unavailable",
                    slug="",
                    secs_to_end=None,
                    spot_price=None,
                    open_price=None,
                    spot_move_5s_bps=None,
                    spot_move_15s_bps=None,
                    spot_move_30s_bps=None,
                    trend_label="Unavailable",
                    trend_side="NEUTRAL",
                    reversal_risk="high",
                    safety_label="BLOCKED",
                    setup_side="",
                    setup_allowed=False,
                    setup_reason="current_slot_unavailable",
                    entry_price=None,
                    exit_price=None,
                    price_to_beat_bps=None,
                    price_to_beat_usd=None,
                    buffer_bps=None,
                    buffer_usd=None,
                    leader_price=None,
                    counter_price=None,
                    leader_edge=None,
                    executable_sum_asks=None,
                    executable_sum_bids=None,
                    manual_score=0,
                    reaction_deadline_secs=None,
                    reaction_label="wait",
                    watch_window_eta_secs=None,
                    one_shot_ready=False,
                    window_id="",
                    hold_to_resolution=self.hold_to_resolution_mode,
                    last_update_ts=compute_finished_ts,
                    compute_started_ts=compute_started_ts,
                    compute_finished_ts=compute_finished_ts,
                compute_latency_ms=round(max(0.0, compute_finished_ts - compute_started_ts) * 1000.0, 1),
                price_to_beat_side="NEUTRAL",
                suggested_action="AGUARDAR",
                suggested_detail="feed atual indisponivel",
                active_setup_name="sem setup",
                active_setup_market="BTC 5min",
                active_action="aguardar",
                active_side="",
                active_price=None,
                market_context="feed atual indisponivel",
                next_seconds_outlook="sem leitura curta disponivel",
                risk_plan="SEM ENTRADA",
                exit_alert="AGUARDAR NOVO SNAPSHOT",
                status_note=f"Current missing: {self.last_queue_reason}. This is data feed/API absence, not a setup signal.",
            )

            self._ensure_open_reference(current_item)
            with ThreadPoolExecutor(max_workers=2) as pool:
                ws_slot_state = self._build_ws_slot_state(slot_bundle)
                slot_state_future = None if ws_slot_state is not None else pool.submit(_fetch_slot_state, slot_bundle)
                reference_future = pool.submit(_fetch_fast_reference_price)
                slot_state = ws_slot_state if ws_slot_state is not None else slot_state_future.result()
                reference = reference_future.result()
            current_snap = _slot_snapshot(slot_state, "current")
            current_exec, _ = _compute_executable_metrics(current_snap)
            current_secs = _slot_secs_to_end(current_item)
            scalp_signal = self.current_scalp.evaluate(
                snap=current_snap,
                secs_to_end=current_secs,
                event_start_time=self.current_open_reference.get("event_start_time"),
                now_ts=now,
                reference_price=reference.get("reference_price"),
                source_divergence_bps=reference.get("source_divergence_bps"),
                opening_reference_price=self.current_open_reference.get("price"),
            )
            early_signal = self.early_overresolved.evaluate(
                timeframe="5m",
                slug=str(current_item.get("slug") or ""),
                snap=current_snap,
                secs_to_end=current_secs,
                event_start_time=self.current_open_reference.get("event_start_time"),
                opening_reference_price=self.current_open_reference.get("price"),
                reference_payload=reference,
                now_ts=now,
            )
            almost_signal = evaluate_current_almost_resolved_v1(
                snap=current_snap,
                secs_to_end=current_secs,
                reference_signal=scalp_signal,
                cfg=self.signal_cfg,
            )

            trend_label, trend_side = self._infer_trend(scalp_signal)
            setup_side = str(almost_signal.get("side") or self._fallback_side_from_context(scalp_signal, current_snap))
            reversal_risk = self._infer_reversal_risk(almost_signal, scalp_signal)
            safety_label, status_note = self._infer_safety(almost_signal, scalp_signal, reversal_risk, trend_side)
            watch_window_eta_secs = self._watch_window_eta_secs(current_secs)
            price_to_beat_bps = almost_signal.get("distance_to_price_to_beat_bps")
            if price_to_beat_bps is None:
                price_to_beat_bps = round(abs(_safe_float(scalp_signal.get("distance_from_open_bps"), 0.0)), 4)
            price_to_beat_usd = almost_signal.get("distance_to_price_to_beat_usd")
            if price_to_beat_usd is None:
                ref = _safe_float(scalp_signal.get("reference_price"), 0.0)
                open_ref = _safe_float(scalp_signal.get("opening_reference_price"), 0.0)
                price_to_beat_usd = round(abs(ref - open_ref), 4) if ref > 0 and open_ref > 0 else None
            buffer_bps = almost_signal.get("up_price_to_beat_buffer_bps" if setup_side == "UP" else "down_price_to_beat_buffer_bps")
            buffer_usd = almost_signal.get("up_price_to_beat_buffer_usd" if setup_side == "UP" else "down_price_to_beat_buffer_usd")
            if buffer_bps is None:
                adverse_bps = max(
                    0.0,
                    -_safe_float(scalp_signal.get("spot_delta_5s_bps"), 0.0) if setup_side == "UP" else _safe_float(scalp_signal.get("spot_delta_5s_bps"), 0.0),
                    -_safe_float(scalp_signal.get("spot_delta_15s_bps"), 0.0) if setup_side == "UP" else _safe_float(scalp_signal.get("spot_delta_15s_bps"), 0.0),
                    -_safe_float(scalp_signal.get("spot_delta_30s_bps"), 0.0) if setup_side == "UP" else _safe_float(scalp_signal.get("spot_delta_30s_bps"), 0.0),
                )
                if price_to_beat_bps is not None:
                    buffer_bps = round(max(0.0, _safe_float(price_to_beat_bps) - adverse_bps), 4)
            if buffer_usd is None and buffer_bps is not None:
                ref = _safe_float(scalp_signal.get("reference_price"), 0.0)
                buffer_usd = round(ref * _safe_float(buffer_bps) / 10000.0, 4) if ref > 0 else None
            if setup_side == "UP":
                leader_price = almost_signal.get("up_buy")
                counter_price = almost_signal.get("down_buy")
            elif setup_side == "DOWN":
                leader_price = almost_signal.get("down_buy")
                counter_price = almost_signal.get("up_buy")
            else:
                up_buy = almost_signal.get("up_buy")
                down_buy = almost_signal.get("down_buy")
                if _safe_float(up_buy, -1.0) >= _safe_float(down_buy, -1.0):
                    leader_price = up_buy
                    counter_price = down_buy
                else:
                    leader_price = down_buy
                    counter_price = up_buy
            leader_edge = almost_signal.get("up_edge_vs_counter" if setup_side == "UP" else "down_edge_vs_counter")
            score = self._manual_score(almost_signal, scalp_signal, reversal_risk, trend_side)
            reaction_deadline_secs, reaction_label = self._reaction_deadline_secs(almost_signal)

            exit_distance = _safe_float(
                almost_signal.get("up_exit_distance" if setup_side == "UP" else "down_exit_distance"),
                1.0,
            )
            if almost_signal.get("allow") and exit_distance <= 0.01:
                safety_label = "UNSAFE"
                status_note = "Too close to resolution. Discard visually for manual hold."
                score = min(score, 30)
            elif almost_signal.get("allow") and exit_distance <= 0.015:
                safety_label = "CAUTION"
                status_note = "Late in the move for hold-to-resolution."
                score = min(score, 45)

            one_shot_ready, window_id = self._window_state(str(current_item.get("slug") or ""), score, almost_signal)
            if one_shot_ready:
                status_note = f"One manual hold-to-resolution window available for this 5m market. Reaction budget: {int(round(reaction_deadline_secs or 0))}s."
            elif not almost_signal.get("allow") and str(almost_signal.get("reason") or "") == "outside_time_window":
                if current_secs is not None and current_secs > self.signal_cfg.max_secs_to_end:
                    status_note = f"Waiting for setup window. Starts in {int(round(watch_window_eta_secs or 0))}s."
                elif current_secs is not None and current_secs < self.signal_cfg.min_secs_to_end:
                    status_note = "Too late for this market. Wait for next 5m window."
            price_to_beat_side = setup_side if setup_side in ("UP", "DOWN") else trend_side if trend_side in ("UP", "DOWN") else "NEUTRAL"
            suggested_action, suggested_detail, risk_plan, exit_alert = self._suggested_action(
                current_secs=current_secs,
                setup_side=setup_side,
                trend_side=trend_side,
                almost_signal=almost_signal,
                scalp_signal=scalp_signal,
                reversal_risk=reversal_risk,
                score=score,
            )
            active_setup = self._choose_active_setup(
                slug=str(current_item.get("slug") or ""),
                current_secs=current_secs,
                scalp_signal=scalp_signal,
                almost_signal=almost_signal,
                early_signal=early_signal,
                suggested_action=suggested_action,
                setup_side=setup_side,
            )
            active_setup = self._apply_manual_plan(
                slug=str(current_item.get("slug") or ""),
                snap=current_snap,
                active_setup=active_setup,
                now_ts=now,
            )
            entry_criteria = self._build_entry_criteria(
                current_secs=current_secs,
                setup_side=setup_side,
                almost_signal=almost_signal,
                scalp_signal=scalp_signal,
                reversal_risk=reversal_risk,
                score=score,
            )
            market_context = self._build_market_context(scalp_signal, almost_signal, early_signal, trend_label)
            next_seconds_outlook = self._build_next_seconds_outlook(scalp_signal, almost_signal, early_signal, active_setup)
            if exit_alert:
                suggested_detail = f"{suggested_detail} | {exit_alert}"
            if active_setup.get("plan_message"):
                suggested_detail = str(active_setup.get("plan_message"))
            compute_finished_ts = time.time()

            return ManualOverlaySnapshotV1(
                ok=True,
                title=str(current_item.get("title") or "Current"),
                slug=str(current_item.get("slug") or ""),
                secs_to_end=current_secs,
                spot_price=reference.get("reference_price"),
                open_price=self.current_open_reference.get("price"),
                spot_move_5s_bps=scalp_signal.get("spot_delta_5s_bps"),
                spot_move_15s_bps=scalp_signal.get("spot_delta_15s_bps"),
                spot_move_30s_bps=scalp_signal.get("spot_delta_30s_bps"),
                trend_label=trend_label,
                trend_side=trend_side,
                reversal_risk=reversal_risk,
                safety_label=safety_label,
                setup_side=setup_side,
                setup_allowed=bool(almost_signal.get("allow")),
                setup_reason=str(almost_signal.get("reason") or ""),
                entry_price=almost_signal.get("entry_price"),
                exit_price=almost_signal.get("exit_price"),
                price_to_beat_bps=price_to_beat_bps,
                price_to_beat_usd=price_to_beat_usd,
                buffer_bps=buffer_bps,
                buffer_usd=buffer_usd,
                leader_price=leader_price,
                counter_price=counter_price,
                leader_edge=leader_edge,
                executable_sum_asks=current_exec.get("sum_asks") if current_exec else None,
                executable_sum_bids=current_exec.get("sum_bids") if current_exec else None,
                manual_score=score,
                reaction_deadline_secs=reaction_deadline_secs,
                reaction_label=reaction_label,
                watch_window_eta_secs=watch_window_eta_secs,
                one_shot_ready=one_shot_ready,
                window_id=window_id,
                hold_to_resolution=self.hold_to_resolution_mode,
                last_update_ts=compute_finished_ts,
                compute_started_ts=compute_started_ts,
                compute_finished_ts=compute_finished_ts,
                compute_latency_ms=round(max(0.0, compute_finished_ts - compute_started_ts) * 1000.0, 1),
                price_to_beat_side=price_to_beat_side,
                suggested_action=suggested_action,
                suggested_detail=suggested_detail,
                active_setup_name=str(active_setup.get("setup_name") or "sem setup"),
                active_setup_market=str(active_setup.get("market") or self._market_label(str(current_item.get("slug") or ""))),
                active_action=str(active_setup.get("action") or suggested_action.lower()),
                active_side=str(active_setup.get("side") or ""),
                active_price=active_setup.get("price"),
                market_context=market_context,
                next_seconds_outlook=next_seconds_outlook,
                risk_plan=risk_plan,
                exit_alert=exit_alert,
                status_note=status_note,
                entry_criteria=entry_criteria,
            )
        except Exception as exc:
            compute_finished_ts = time.time()
            return ManualOverlaySnapshotV1(
                ok=False,
                title="Overlay error",
                slug="",
                secs_to_end=None,
                spot_price=None,
                open_price=None,
                spot_move_5s_bps=None,
                spot_move_15s_bps=None,
                spot_move_30s_bps=None,
                trend_label="Unavailable",
                trend_side="NEUTRAL",
                reversal_risk="high",
                safety_label="BLOCKED",
                setup_side="",
                setup_allowed=False,
                setup_reason=type(exc).__name__,
                entry_price=None,
                exit_price=None,
                price_to_beat_bps=None,
                price_to_beat_usd=None,
                buffer_bps=None,
                buffer_usd=None,
                leader_price=None,
                counter_price=None,
                leader_edge=None,
                executable_sum_asks=None,
                executable_sum_bids=None,
                manual_score=0,
                reaction_deadline_secs=None,
                reaction_label="wait",
                watch_window_eta_secs=None,
                one_shot_ready=False,
                window_id="",
                hold_to_resolution=self.hold_to_resolution_mode,
                last_update_ts=compute_finished_ts,
                compute_started_ts=compute_started_ts,
                compute_finished_ts=compute_finished_ts,
                compute_latency_ms=round(max(0.0, compute_finished_ts - compute_started_ts) * 1000.0, 1),
                price_to_beat_side="NEUTRAL",
                suggested_action="AGUARDAR",
                suggested_detail="erro no cálculo do snapshot",
                active_setup_name="sem setup",
                active_setup_market="BTC 5min",
                active_action="aguardar",
                active_side="",
                active_price=None,
                market_context="erro ao ler book e spot",
                next_seconds_outlook="sem leitura curta disponivel",
                status_note=str(exc),
            )
