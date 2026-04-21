from __future__ import annotations

import argparse
import time
from pprint import pprint

from market.book_5m import fetch_market_metadata_from_slug
from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.queue_5m_v5 import build_5m_queue_v5
from market.rest_5m_shadow_public_v4 import _compute_executable_metrics, _current_secs_to_end, _fetch_slot_state, _slot_snapshot
from market.slug_discovery import fetch_event_by_slug


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose scalp edge on the current BTC 5m market")
    parser.add_argument("--seconds", type=int, default=30, help="How long to run the diagnostic")
    parser.add_argument("--poll-secs", type=float, default=2.0, help="Polling interval in seconds")
    args = parser.parse_args()

    cfg = CurrentScalpConfigV1()
    research = CurrentScalpResearchV1(cfg=cfg)

    queue = build_5m_queue_v5()
    current_item = queue.get("current")
    if not current_item:
        print("[CURRENT_SCALP] current slot unavailable")
        return 1

    raw_event = fetch_event_by_slug(current_item["slug"])
    if not raw_event:
        print("[CURRENT_SCALP] failed to fetch raw event")
        return 1

    meta = fetch_market_metadata_from_slug(current_item["slug"])
    if not meta:
        print("[CURRENT_SCALP] failed to fetch market metadata")
        return 1

    market = (raw_event.get("markets") or [{}])[0]
    event_start_time = market.get("eventStartTime") or raw_event.get("startTime")
    open_ref = fetch_binance_open_price_for_event_start_v1(event_start_time)

    print("[CURRENT_SCALP_CONFIG]")
    pprint(cfg.as_dict())
    print("[CURRENT_MARKET]")
    pprint(
        {
            "title": current_item.get("title"),
            "slug": current_item.get("slug"),
            "seconds_to_end": current_item.get("seconds_to_end"),
            "event_start_time": event_start_time,
            "resolution_source": market.get("resolutionSource") or raw_event.get("resolutionSource"),
            "open_reference": open_ref,
        }
    )

    slot_bundle = {
        "queue": {"current": current_item},
        "slots": {"current": {"item": current_item, "meta": meta}},
    }
    started_at = time.time()

    while time.time() - started_at < args.seconds:
        slot_state = _fetch_slot_state(slot_bundle)
        snap = _slot_snapshot(slot_state, "current")
        executable, executable_reason = _compute_executable_metrics(snap)
        ref = fetch_external_btc_reference_v1()
        secs_to_end = _current_secs_to_end(current_item.get("seconds_to_end"), started_at)
        signal = research.evaluate(
            snap=snap,
            secs_to_end=secs_to_end,
            event_start_time=event_start_time,
            now_ts=time.time(),
            reference_price=ref.get("reference_price"),
            source_divergence_bps=ref.get("source_divergence_bps"),
            opening_reference_price=open_ref.get("open_price"),
        )

        print("\n===== CURRENT SCALP EDGE V1 =====")
        print(
            f"secs_to_end={secs_to_end} | executable_reason={executable_reason} | "
            f"setup={signal.get('setup')} side={signal.get('side')} allow={signal.get('allow')}"
        )
        if executable:
            print(
                f"EXECUTABLE up_bid={executable['up_bid']} up_ask={executable['up_ask']} "
                f"down_bid={executable['down_bid']} down_ask={executable['down_ask']} "
                f"sum_asks={executable['sum_asks']} sum_bids={executable['sum_bids']}"
            )
        print("[REFERENCE]")
        pprint(ref)
        print("[SIGNAL]")
        pprint(signal)

        time.sleep(max(0.5, float(args.poll_secs)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
