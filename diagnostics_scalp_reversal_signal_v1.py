from __future__ import annotations

from market.live_scalp_reversal_v1 import ScalpConfigV1, _choose_entry_candidate


def _snap(token: str = "tok"):
    return {
        "up": {
            "token_id": token,
            "best_bid": 0.33,
            "best_ask": 0.34,
            "top_bids": [{"price": 0.33, "size": 4}, {"price": 0.32, "size": 4}, {"price": 0.31, "size": 4}],
            "top_asks": [{"price": 0.34, "size": 4}, {"price": 0.35, "size": 4}, {"price": 0.36, "size": 4}],
        },
        "down": {
            "token_id": "tok2",
            "best_bid": 0.66,
            "best_ask": 0.67,
            "top_bids": [{"price": 0.66, "size": 4}, {"price": 0.65, "size": 4}, {"price": 0.64, "size": 4}],
            "top_asks": [{"price": 0.67, "size": 4}, {"price": 0.68, "size": 4}, {"price": 0.69, "size": 4}],
        },
    }


def main() -> int:
    cfg = ScalpConfigV1(enabled=True, allowed_slots=["next_1"])

    executable = {"up_ask": 0.34, "down_ask": 0.67, "up_bid": 0.33, "down_bid": 0.66}
    continuation_ok = {
        "label": "reversal_ok",
        "score": 1,
        "monotonic_ratio_60s": 0.6,
        "accel": 0.0,
        "delta60": 0.025,
        "depth_imbalance_top3": 0.1,
    }
    d1 = _choose_entry_candidate(_snap(), executable, continuation_ok, cfg)
    print("[ALLOW_CASE]", d1)
    assert d1["allow"]

    continuation_bad = {
        "label": "continuation_risk_high",
        "score": 5,
        "monotonic_ratio_60s": 0.95,
        "accel": 0.03,
        "delta60": 0.08,
        "depth_imbalance_top3": 0.4,
    }
    d2 = _choose_entry_candidate(_snap(), executable, continuation_bad, cfg)
    print("[BLOCK_CASE]", d2)
    assert not d2["allow"]

    print("[PASS] scalp reversal signal diagnostic v1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
