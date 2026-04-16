from __future__ import annotations

from market.continuation_filter_v1 import ContinuationRiskFilterV1


def _make_snap(base_price: float, bid_depth: float, ask_depth: float):
    lvl_bid = [{"price": round(base_price - 0.01, 4), "size": bid_depth / 6}] * 3
    lvl_ask = [{"price": round(base_price + 0.01, 4), "size": ask_depth / 6}] * 3
    return {
        "up": {
            "display_price": base_price,
            "top_bids": lvl_bid,
            "top_asks": lvl_ask,
        },
        "down": {
            "display_price": round(1 - base_price, 4),
            "top_bids": lvl_bid,
            "top_asks": lvl_ask,
        },
    }


def main() -> int:
    f = ContinuationRiskFilterV1()
    ts = 1_000_000.0

    # Warm-up + strong continuation up move with ask depletion.
    out = None
    for i in range(35):
        p = 0.45 + (0.0022 * i)
        bid = 24.0 + (0.2 * i)
        ask = 24.0 - (0.35 * i)
        out = f.update_and_classify(slot_name="next_1", snap=_make_snap(p, bid, ask), now_ts=ts + (i * 2))

    print("[STRONG_CONTINUATION_CASE]", out)
    assert out is not None
    assert out["label"] in ("continuation_risk_high", "continuation_risk_medium")

    # Neutral/stable case should not be blocked.
    g = ContinuationRiskFilterV1()
    out2 = None
    for i in range(35):
        p = 0.5 + (0.0001 if i % 2 == 0 else -0.0001)
        bid = 20.0
        ask = 20.0
        out2 = g.update_and_classify(slot_name="next_1", snap=_make_snap(p, bid, ask), now_ts=ts + (i * 2))

    print("[NEUTRAL_CASE]", out2)
    assert out2 is not None
    assert out2["label"] in ("reversal_ok", "continuation_risk_low")
    assert not out2["block_entry"]

    print("[PASS] continuation filter diagnostic v1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
