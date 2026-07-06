"""
diagnostics_lag_continuation_paper_v1.py

Paper runner do setup "lag continuation" (ver market/lag_continuation_signal_v1.py).
Portado de polymarket-overlay-indicator (lagContinuationPaperService.ts).

Estrategia: BTC distante do preco de abertura da janela de 5min (priceToBeat)
+ momentum na mesma direcao perto do fim da janela -> aposta na continuacao ate
a resolucao. Sem stop, sem take-profit — sai por tempo (exit_seconds_to_end)
ou no rollover do slot (bid ao vivo).
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint
from typing import Optional

from market.current_scalp_signal_v1 import (
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.lag_continuation_signal_v1 import LagContinuationConfigV1, evaluate_lag_continuation_v1
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _compute_executable_metrics, _fetch_slot_state, _slot_snapshot
from market.slug_discovery import fetch_event_by_slug


@dataclass
class PaperTrade:
    mode: str = "idle"
    slug: Optional[str] = None
    side: Optional[str] = None
    entry_ask: Optional[float] = None
    stake: float = 0.0
    shares: float = 0.0
    created_at: float = 0.0
    entry_secs: Optional[int] = None
    dominant_side: Optional[str] = None
    signed_distance_bps: Optional[float] = None
    price_to_beat: Optional[float] = None
    btc_price_at_entry: Optional[float] = None
    momentum_bps: Optional[float] = None
    exit_bid: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_default_log_path() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"lag_continuation_paper_{ts}.jsonl"


def _slot_secs_to_end(item: Optional[dict]) -> Optional[int]:
    if not item:
        return None
    try:
        return max(0, int(float(item.get("seconds_to_end"))))
    except Exception:
        return None


def _bid_for_side(executable: Optional[dict], side: str) -> Optional[float]:
    if not executable:
        return None
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"))


def _paper_enter(signal: dict, *, stake: float, now: float, slug: str, secs_to_end: Optional[int]) -> PaperTrade:
    entry_ask = _safe_float(signal.get("entry_ask"), 0.0) or 0.0
    shares = stake / entry_ask if entry_ask > 0 else 0.0
    return PaperTrade(
        mode="open",
        slug=slug,
        side=str(signal.get("side") or ""),
        entry_ask=entry_ask,
        stake=stake,
        shares=shares,
        created_at=now,
        entry_secs=secs_to_end,
        dominant_side=str(signal.get("dominant_side") or ""),
        signed_distance_bps=signal.get("signed_distance_bps"),
        price_to_beat=signal.get("price_to_beat"),
        btc_price_at_entry=signal.get("btc_price"),
        momentum_bps=signal.get("momentum_bps"),
    )


def _close_trade(trade: PaperTrade, *, exit_bid: Optional[float], reason: str) -> PaperTrade:
    trade.mode = "idle"
    trade.exit_bid = exit_bid if exit_bid is not None else trade.entry_ask
    trade.exit_reason = reason
    trade.pnl = round(trade.shares * (trade.exit_bid or 0.0) - trade.stake, 4)
    return trade


def _trade_stats(completed: list[dict]) -> dict:
    pnls = [_safe_float(t.get("pnl"), 0.0) or 0.0 for t in completed]
    total = round(sum(pnls), 4)
    count = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    return {
        "completed_trades": count,
        "wins": wins,
        "losses": losses,
        "win_rate": round(100.0 * wins / count, 2) if count else 0.0,
        "total_pnl": total,
        "avg_pnl": round(total / count, 4) if count else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Paper-trade lag continuation (BTC vs preco de abertura da janela 5m) — portado do overlay-indicator"
    )
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--poll-secs", type=float, default=1.0)
    parser.add_argument("--stake", type=float, default=6.0)
    parser.add_argument("--min-seconds-to-end", type=int, default=30)
    parser.add_argument("--max-seconds-to-end", type=int, default=120)
    parser.add_argument("--min-signed-distance-bps", type=float, default=-30.0)
    parser.add_argument("--max-signed-distance-bps", type=float, default=30.0)
    parser.add_argument("--momentum-window-sec", type=float, default=30.0)
    parser.add_argument("--min-momentum-bps", type=float, default=4.0)
    parser.add_argument("--max-entry-ask", type=float, default=0.70)
    parser.add_argument("--exit-seconds-to-end", type=int, default=5)
    parser.add_argument("--exclude-seconds-to-end-min", type=int, default=75)
    parser.add_argument("--exclude-seconds-to-end-max", type=int, default=90)
    parser.add_argument("--log-file", type=str, default=None)
    args = parser.parse_args()

    cfg = LagContinuationConfigV1(
        min_seconds_to_end=args.min_seconds_to_end,
        max_seconds_to_end=args.max_seconds_to_end,
        min_signed_distance_bps=args.min_signed_distance_bps,
        max_signed_distance_bps=args.max_signed_distance_bps,
        momentum_window_sec=args.momentum_window_sec,
        min_momentum_bps=args.min_momentum_bps,
        max_entry_ask=args.max_entry_ask,
        exit_seconds_to_end=args.exit_seconds_to_end,
        exclude_seconds_to_end_min=args.exclude_seconds_to_end_min,
        exclude_seconds_to_end_max=args.exclude_seconds_to_end_max,
    )
    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()
    trade = PaperTrade()
    completed: list[dict] = []
    blocked_reasons = Counter()
    allowed_sides = Counter()
    exit_reasons = Counter()
    price_to_beat_cache: dict[str, Optional[float]] = {}
    spot_samples: deque = deque()
    executable = None

    print("[LAG_CONTINUATION_CONFIG]")
    pprint(cfg.as_dict())
    print("[LOG_FILE]")
    print(log_path)

    started_at = time.time()
    while time.time() - started_at < args.seconds:
        now = time.time()
        slot_bundle = _build_slot_bundle()
        current_item = slot_bundle["queue"].get("current")
        if not current_item:
            print("[LAG_CONTINUATION] current slot unavailable")
            time.sleep(max(0.5, float(args.poll_secs)))
            continue

        current_slug = str(current_item.get("slug") or "")
        current_secs = _slot_secs_to_end(current_item)

        if current_slug not in price_to_beat_cache:
            raw_event = fetch_event_by_slug(current_slug) if current_slug else None
            market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
            event_start_time = market.get("eventStartTime") or (raw_event or {}).get("startTime")
            open_ref = (
                fetch_binance_open_price_for_event_start_v1(event_start_time)
                if event_start_time
                else {"open_price": None}
            )
            price_to_beat_cache[current_slug] = open_ref.get("open_price")

        price_to_beat = price_to_beat_cache.get(current_slug)

        slot_state = _fetch_slot_state(slot_bundle)
        snap = _slot_snapshot(slot_state, "current")
        executable, executable_reason = _compute_executable_metrics(snap)

        ref = fetch_external_btc_reference_v1()
        btc_price = ref.get("reference_price")

        if btc_price is not None:
            spot_samples.append((now, btc_price))
        cutoff = now - max(60.0, cfg.momentum_window_sec * 2)
        while spot_samples and spot_samples[0][0] < cutoff:
            spot_samples.popleft()

        momentum_bps = None
        if btc_price is not None:
            ref_ts = now - cfg.momentum_window_sec
            ref_sample = next((p for (t, p) in spot_samples if t >= ref_ts), None)
            if ref_sample:
                momentum_bps = (btc_price - ref_sample) / ref_sample * 10_000

        signal = evaluate_lag_continuation_v1(
            secs_to_end=current_secs,
            btc_price=btc_price,
            price_to_beat=price_to_beat,
            momentum_bps=momentum_bps,
            up_ask=_safe_float((executable or {}).get("up_ask")),
            down_ask=_safe_float((executable or {}).get("down_ask")),
            cfg=cfg,
        )
        signal["event_slug"] = current_slug

        if signal.get("allow"):
            allowed_sides[str(signal.get("side") or "NONE")] += 1
        else:
            blocked_reasons[str(signal.get("reason") or "unknown")] += 1

        if trade.mode == "open" and trade.slug != current_slug:
            exit_bid = _bid_for_side(executable, trade.side or "")
            trade = _close_trade(trade, exit_bid=exit_bid, reason="slot_rollover")
            completed.append(asdict(trade))
            exit_reasons[str(trade.exit_reason or "unknown")] += 1
            _append_jsonl(log_path, {"type": "exit", "ts": now, "trade": completed[-1]})
            trade = PaperTrade()

        if trade.mode == "idle" and signal.get("allow"):
            trade = _paper_enter(signal, stake=args.stake, now=now, slug=current_slug, secs_to_end=current_secs)
            _append_jsonl(log_path, {"type": "enter", "ts": now, "signal": signal, "trade": asdict(trade)})
        elif trade.mode == "open":
            exit_bid = _bid_for_side(executable, trade.side or "")
            if current_secs is not None and current_secs <= cfg.exit_seconds_to_end and exit_bid is not None:
                trade = _close_trade(trade, exit_bid=exit_bid, reason="time_exit")
                completed.append(asdict(trade))
                exit_reasons[str(trade.exit_reason or "unknown")] += 1
                _append_jsonl(log_path, {"type": "exit", "ts": now, "trade": completed[-1]})
                trade = PaperTrade()

        _append_jsonl(
            log_path,
            {
                "type": "snapshot",
                "ts": now,
                "current_slug": current_slug,
                "current_secs": current_secs,
                "executable_reason": executable_reason,
                "btc_price": btc_price,
                "price_to_beat": price_to_beat,
                "momentum_bps": momentum_bps,
                "signal": signal,
                "trade": asdict(trade),
            },
        )

        print(
            f"[LAG_CONTINUATION] secs={current_secs} allow={signal.get('allow')} "
            f"side={signal.get('side')} reason={signal.get('reason')} mode={trade.mode}"
        )
        time.sleep(max(0.5, float(args.poll_secs)))

    if trade.mode == "open":
        trade = _close_trade(trade, exit_bid=trade.entry_ask, reason="session_end")
        completed.append(asdict(trade))
        exit_reasons[str(trade.exit_reason or "unknown")] += 1
        _append_jsonl(log_path, {"type": "exit", "ts": time.time(), "trade": completed[-1]})

    summary = {
        "stats": _trade_stats(completed),
        "allowed_sides": dict(allowed_sides),
        "exit_reasons": dict(exit_reasons),
        "top_blocked_reasons": blocked_reasons.most_common(12),
        "log_file": str(log_path),
        "config": cfg.as_dict(),
    }
    _append_jsonl(log_path, {"type": "summary", "ts": time.time(), "summary": summary})
    print("[LAG_CONTINUATION_SUMMARY]")
    pprint(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
