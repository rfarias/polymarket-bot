from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path
from pprint import pprint

from diagnostics_current_almost_resolved_guardian_v1 import (
    GuardianConfig,
    StopExitState,
    _append_jsonl,
    _bid_for_side,
    _current_slug_slot_bundle,
    _decision,
    _enrich_guardian_signal_metrics,
    _event_open_reference,
    _execute_or_simulate_stop_cycle,
    _guardian_ask_for_side,
    _guardian_counter_ask,
    _side_adverse_bps,
    _side_buffer_bps,
    _tick_size_from_snap,
)
from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, evaluate_current_almost_resolved_v1
from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_external_btc_reference_v1,
)
from market.rest_5m_shadow_public_v5 import _compute_executable_metrics, _fetch_slot_state, _slot_snapshot


def _build_default_log_path() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"current_almost_resolved_guardian_matrix_{ts}.jsonl"


def _parse_positions(raw: str) -> list[tuple[str, float]]:
    positions: list[tuple[str, float]] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        side, _, entry = part.partition(":")
        side = side.strip().upper()
        if side not in ("UP", "DOWN") or not entry:
            raise ValueError(f"Invalid position '{part}'. Use SIDE:ENTRY, e.g. UP:0.96")
        positions.append((side, float(entry)))
    if not positions:
        raise ValueError("At least one --positions item is required")
    return positions


def _guardian_cfg(args, side: str, entry_price: float) -> GuardianConfig:
    return GuardianConfig(
        side=side,
        entry_price=float(entry_price),
        qty=float(args.qty),
        price_stop=args.price_stop,
        max_loss_ticks=int(args.max_loss_ticks),
        deadline_exit_secs=int(args.deadline_exit_secs),
        warn_buffer_bps=float(args.warn_buffer_bps),
        stop_buffer_bps=float(args.stop_buffer_bps),
        stop_buffer_usd=float(args.stop_buffer_usd),
        adverse_share_stop=float(args.adverse_share_stop),
        adverse_5s_stop_bps=float(args.adverse_5s_stop_bps),
        adverse_15s_stop_bps=float(args.adverse_15s_stop_bps),
        counter_stop_price=float(args.counter_stop_price),
        counter_hard_stop_price=float(args.counter_hard_stop_price),
        market_range_30s_stop=float(args.market_range_30s_stop),
        protective_grace_min_secs=int(args.protective_grace_min_secs),
        protective_grace_min_distance_bps=float(args.protective_grace_min_distance_bps),
        protective_grace_min_distance_usd=float(args.protective_grace_min_distance_usd),
        protective_grace_min_buffer_bps=float(args.protective_grace_min_buffer_bps),
        protective_grace_min_buffer_usd=float(args.protective_grace_min_buffer_usd),
        protective_grace_max_counter_price=float(args.protective_grace_max_counter_price),
        protective_grace_max_adverse_share=float(args.protective_grace_max_adverse_share),
        hard_drawdown_ticks=int(args.hard_drawdown_ticks),
        exit_slippage_ticks=int(args.exit_slippage_ticks),
        limit_first_secs=float(args.limit_first_secs),
        exit_retry_secs=float(args.exit_retry_secs),
        min_market_order_qty=float(args.min_market_order_qty),
        execute_stop=False,
        beep=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate guardian stop decisions for multiple hypothetical positions")
    parser.add_argument("--positions", default="UP:0.96,UP:0.90,DOWN:0.96,DOWN:0.90")
    parser.add_argument("--qty", type=float, default=5.0)
    parser.add_argument("--slug", type=str, default=None)
    parser.add_argument("--seconds", type=int, default=600, help="Run duration. Use 0 to run indefinitely.")
    parser.add_argument("--poll-secs", type=float, default=2.0)
    parser.add_argument("--log-file", type=str, default=None)
    parser.add_argument("--price-stop", type=float, default=None)
    parser.add_argument("--max-loss-ticks", type=int, default=3)
    parser.add_argument("--deadline-exit-secs", type=int, default=8)
    parser.add_argument("--warn-buffer-bps", type=float, default=4.0)
    parser.add_argument("--stop-buffer-bps", type=float, default=1.2)
    parser.add_argument("--stop-buffer-usd", type=float, default=8.0)
    parser.add_argument("--adverse-share-stop", type=float, default=0.55)
    parser.add_argument("--adverse-5s-stop-bps", type=float, default=1.2)
    parser.add_argument("--adverse-15s-stop-bps", type=float, default=1.8)
    parser.add_argument("--counter-stop-price", type=float, default=0.12)
    parser.add_argument("--counter-hard-stop-price", type=float, default=0.35)
    parser.add_argument("--market-range-30s-stop", type=float, default=0.035)
    parser.add_argument("--protective-grace-min-secs", type=int, default=25)
    parser.add_argument("--protective-grace-min-distance-bps", type=float, default=6.0)
    parser.add_argument("--protective-grace-min-distance-usd", type=float, default=45.0)
    parser.add_argument("--protective-grace-min-buffer-bps", type=float, default=4.0)
    parser.add_argument("--protective-grace-min-buffer-usd", type=float, default=30.0)
    parser.add_argument("--protective-grace-max-counter-price", type=float, default=0.18)
    parser.add_argument("--protective-grace-max-adverse-share", type=float, default=0.45)
    parser.add_argument("--hard-drawdown-ticks", type=int, default=20)
    parser.add_argument("--exit-slippage-ticks", type=int, default=2)
    parser.add_argument("--limit-first-secs", type=float, default=2.0)
    parser.add_argument("--exit-retry-secs", type=float, default=1.0)
    parser.add_argument("--min-market-order-qty", type=float, default=5.0)
    args = parser.parse_args()

    positions = _parse_positions(args.positions)
    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()
    signal_cfg = CurrentAlmostResolvedConfigV1()
    scalp_cfg = CurrentScalpConfigV1()
    current_scalp = CurrentScalpResearchV1(cfg=scalp_cfg, history_secs=180)
    open_reference_cache: dict[str, object | None] = {}
    exit_states = {f"{side}:{entry}": StopExitState() for side, entry in positions}

    print("[GUARDIAN_MATRIX_POSITIONS]")
    pprint(positions)
    print("[LOG_FILE]")
    print(log_path)

    started_at = time.time()
    while args.seconds <= 0 or time.time() - started_at < args.seconds:
        now = time.time()
        try:
            slot_bundle = _current_slug_slot_bundle(args.slug)
            current_item = slot_bundle["queue"].get("current")
            if not current_item:
                row = {"type": "snapshot", "ts": now, "status": "WARN", "reason": "current_slot_unavailable"}
                _append_jsonl(log_path, row)
                print("[MATRIX_WARN] current_slot_unavailable")
                time.sleep(max(0.25, float(args.poll_secs)))
                continue

            slug = str(current_item.get("slug") or "")
            open_ref = _event_open_reference(slug, open_reference_cache)
            slot_state = _fetch_slot_state(slot_bundle)
            snap = _slot_snapshot(slot_state, "current")
            executable, executable_reason = _compute_executable_metrics(snap)
            secs_to_end = int(current_item.get("seconds_to_end") or 0)
            reference = fetch_external_btc_reference_v1()
            scalp_signal = current_scalp.evaluate(
                snap=snap,
                secs_to_end=secs_to_end,
                event_start_time=open_ref.get("event_start_time"),
                now_ts=now,
                reference_price=reference.get("reference_price"),
                source_divergence_bps=reference.get("source_divergence_bps"),
                opening_reference_price=open_ref.get("price"),
            )
            base_signal = evaluate_current_almost_resolved_v1(
                snap=snap,
                secs_to_end=secs_to_end,
                reference_signal=scalp_signal,
                cfg=signal_cfg,
            )

            rows = []
            counts = {"HOLD": 0, "WARN": 0, "STOP": 0}
            for side, entry_price in positions:
                cfg = _guardian_cfg(args, side, entry_price)
                signal = _enrich_guardian_signal_metrics(base_signal, scalp_signal, side)
                tick_size = _tick_size_from_snap(snap, side)
                bid_now = _bid_for_side(executable, side)
                decision, reasons = _decision(
                    cfg=cfg,
                    signal=signal,
                    scalp_signal=scalp_signal,
                    bid_now=bid_now,
                    tick_size=tick_size,
                    secs_to_end=secs_to_end,
                )
                key = f"{side}:{entry_price}"
                state = exit_states[key]
                stop_execution = None
                if state.active and decision != "STOP":
                    decision = "STOP"
                    reasons = ["exit_already_active"]
                if decision == "STOP" and ((not state.active) or now - state.last_attempt_at >= cfg.exit_retry_secs):
                    stop_execution = _execute_or_simulate_stop_cycle(
                        cfg=cfg,
                        state=state,
                        snap=snap,
                        bid_now=bid_now,
                        tick_size=tick_size,
                        slug=slug,
                        reasons=reasons,
                        now=now,
                    )
                counts[decision] = counts.get(decision, 0) + 1
                rows.append(
                    {
                        "key": key,
                        "side": side,
                        "entry_price": entry_price,
                        "status": decision,
                        "reasons": reasons,
                        "bid_now": bid_now,
                        "pnl_ticks": round((bid_now - entry_price) / tick_size, 4) if bid_now > 0 and tick_size > 0 else None,
                        "leader_buy": _guardian_ask_for_side(signal, scalp_signal, side),
                        "counter_buy": _guardian_counter_ask(signal, scalp_signal, side),
                        "buffer_bps": _side_buffer_bps(signal, side),
                        "adverse_bps": _side_adverse_bps(signal, side),
                        "exit_state": asdict(state),
                        "stop_execution": stop_execution,
                    }
                )

            row = {
                "type": "snapshot",
                "ts": now,
                "slug": slug,
                "secs_to_end": secs_to_end,
                "counts": counts,
                "executable_reason": executable_reason,
                "reference": reference,
                "open_reference": open_ref,
                "current_scalp_context": scalp_signal,
                "almost_resolved_signal": base_signal,
                "positions": rows,
            }
            _append_jsonl(log_path, row)
            print(f"[MATRIX] secs={secs_to_end} counts={counts} slug={slug}")
            for item in rows:
                print(
                    f"  {item['key']} {item['status']} bid={item['bid_now']} pnl_ticks={item['pnl_ticks']} "
                    f"buffer={item['buffer_bps']} adverse={item['adverse_bps']} reasons={';'.join(item['reasons']) or '-'}"
                )
        except Exception as exc:
            row = {"type": "snapshot", "ts": time.time(), "status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
            _append_jsonl(log_path, row)
            print(f"[MATRIX_ERROR] {type(exc).__name__}: {exc}")
        time.sleep(max(0.25, float(args.poll_secs)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
