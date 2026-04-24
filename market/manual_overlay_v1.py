from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, Optional

import requests

from market.book_5m import fetch_market_metadata_from_slug
from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, evaluate_current_almost_resolved_v1
from market.current_scalp_signal_v1 import (
    BINANCE_PRICE_URL,
    TIMEOUT,
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
)
from market.rest_5m_shadow_public_v5 import _compute_executable_metrics, _fetch_slot_state, _slot_snapshot
from market.slug_discovery import fetch_event_by_slug


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


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
        res = requests.get(BINANCE_PRICE_URL, params={"symbol": "BTCUSDT"}, timeout=TIMEOUT)
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
    status_note: str

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
            "status_note": self.status_note,
        }


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
        self.slot_bundle: Optional[Dict] = None
        self.slot_bundle_refreshed_at: float = 0.0
        self.current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}
        self.last_queue_reason: str = "not_initialized"
        self.last_window_slug: str = ""
        self.window_consumed_by_slug: dict[str, bool] = {}
        self.hold_to_resolution_mode: bool = True
        self.use_fast_reference: bool = True

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

    def read_snapshot(self) -> ManualOverlaySnapshotV1:
        now = time.time()
        try:
            slot_bundle = self._ensure_slot_bundle()
            current_item = slot_bundle["queue"].get("current")
            if not current_item:
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
                    last_update_ts=now,
                    status_note=f"Current missing: {self.last_queue_reason}. This is data feed/API absence, not a setup signal.",
                )

            self._ensure_open_reference(current_item)
            slot_state = _fetch_slot_state(slot_bundle)
            current_snap = _slot_snapshot(slot_state, "current")
            current_exec, _ = _compute_executable_metrics(current_snap)
            current_secs = _slot_secs_to_end(current_item)
            reference = _fetch_fast_reference_price() if self.use_fast_reference else _fetch_fast_reference_price()
            scalp_signal = self.current_scalp.evaluate(
                snap=current_snap,
                secs_to_end=current_secs,
                event_start_time=self.current_open_reference.get("event_start_time"),
                now_ts=now,
                reference_price=reference.get("reference_price"),
                source_divergence_bps=reference.get("source_divergence_bps"),
                opening_reference_price=self.current_open_reference.get("price"),
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
                last_update_ts=now,
                status_note=status_note,
            )
        except Exception as exc:
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
                last_update_ts=now,
                status_note=str(exc),
            )
