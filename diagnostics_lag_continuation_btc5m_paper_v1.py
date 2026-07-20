"""
diagnostics_lag_continuation_btc5m_paper_v1.py

Paper runner do setup "lag continuation" adaptado para BTC em candle de 5min
puro (sem Polymarket) — ver market/lag_continuation_btc5m_signal_v1.py.

So gera sinal e simula PnL de uma posicao LONG/SHORT direta em BTC, com custo
de execucao explicito (fee_bps_round_trip). Nao posta nenhuma ordem real —
nenhum broker de exchange esta integrado. Paper only, conforme regras do
CLAUDE.md (avancar para real exige confirmacao explicita e validacao previa
com 50+ trades resolvidos, mesmo criterio dos outros setups deste repo).
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

from market.btc5m_price_feed_v1 import (
    fetch_current_5m_window_v1,
    fetch_external_btc_reference_v1,
    seconds_to_end_v1,
)
from market.lag_continuation_btc5m_signal_v1 import (
    LagContinuationBTC5mConfigV1,
    evaluate_lag_continuation_btc5m_v1,
)


@dataclass
class PaperPositionV1:
    mode: str = "idle"
    window_start_ts: Optional[float] = None
    position_side: Optional[str] = None
    entry_price: Optional[float] = None
    stake: float = 0.0
    size_btc: float = 0.0
    created_at: float = 0.0
    entry_secs: Optional[int] = None
    dominant_side: Optional[str] = None
    signed_distance_bps: Optional[float] = None
    price_to_beat: Optional[float] = None
    momentum_bps: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_gross: Optional[float] = None
    fee_cost: Optional[float] = None
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
    return Path("logs") / f"lag_continuation_btc5m_paper_{ts}.jsonl"


def _paper_enter(signal: dict, *, stake: float, now: float, window_start_ts: float, secs_to_end: Optional[int]) -> PaperPositionV1:
    entry_price = _safe_float(signal.get("entry_price"), 0.0) or 0.0
    size_btc = stake / entry_price if entry_price > 0 else 0.0
    return PaperPositionV1(
        mode="open",
        window_start_ts=window_start_ts,
        position_side=str(signal.get("position_side") or ""),
        entry_price=entry_price,
        stake=stake,
        size_btc=size_btc,
        created_at=now,
        entry_secs=secs_to_end,
        dominant_side=str(signal.get("dominant_side") or ""),
        signed_distance_bps=signal.get("signed_distance_bps"),
        price_to_beat=signal.get("price_to_beat"),
        momentum_bps=signal.get("momentum_bps"),
    )


def _close_position(pos: PaperPositionV1, *, exit_price: Optional[float], reason: str, cfg: LagContinuationBTC5mConfigV1) -> PaperPositionV1:
    pos.mode = "idle"
    pos.exit_price = exit_price if exit_price is not None else pos.entry_price
    pos.exit_reason = reason
    entry = pos.entry_price or 0.0
    exitp = pos.exit_price or 0.0
    direction = 1.0 if pos.position_side == "LONG" else -1.0
    pnl_gross = pos.size_btc * direction * (exitp - entry)
    fee_cost = pos.stake * (cfg.fee_bps_round_trip / 10_000.0)
    pos.pnl_gross = round(pnl_gross, 4)
    pos.fee_cost = round(fee_cost, 4)
    pos.pnl = round(pnl_gross - fee_cost, 4)
    return pos


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
        description="Paper-trade lag continuation em BTC 5min puro (sem Polymarket) — LONG/SHORT direto no spot"
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
    parser.add_argument("--exit-seconds-to-end", type=int, default=5)
    parser.add_argument("--exclude-seconds-to-end-min", type=int, default=75)
    parser.add_argument("--exclude-seconds-to-end-max", type=int, default=90)
    parser.add_argument("--fee-bps-round-trip", type=float, default=8.0)
    parser.add_argument("--log-file", type=str, default=None)
    args = parser.parse_args()

    cfg = LagContinuationBTC5mConfigV1(
        min_seconds_to_end=args.min_seconds_to_end,
        max_seconds_to_end=args.max_seconds_to_end,
        min_signed_distance_bps=args.min_signed_distance_bps,
        max_signed_distance_bps=args.max_signed_distance_bps,
        momentum_window_sec=args.momentum_window_sec,
        min_momentum_bps=args.min_momentum_bps,
        exit_seconds_to_end=args.exit_seconds_to_end,
        exclude_seconds_to_end_min=args.exclude_seconds_to_end_min,
        exclude_seconds_to_end_max=args.exclude_seconds_to_end_max,
        fee_bps_round_trip=args.fee_bps_round_trip,
    )
    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()
    pos = PaperPositionV1()
    completed: list[dict] = []
    blocked_reasons = Counter()
    allowed_sides = Counter()
    exit_reasons = Counter()
    window_open_cache: dict[float, Optional[float]] = {}
    spot_samples: deque = deque()

    print("[LAG_CONTINUATION_BTC5M_CONFIG]")
    pprint(cfg.as_dict())
    print("[LOG_FILE]")
    print(log_path)

    started_at = time.time()
    while time.time() - started_at < args.seconds:
        now = time.time()
        window = fetch_current_5m_window_v1(now)
        current_secs = seconds_to_end_v1(window.window_end_ts, now)

        if window.window_start_ts not in window_open_cache:
            window_open_cache[window.window_start_ts] = window.open_price if window.ok else None
        price_to_beat = window_open_cache.get(window.window_start_ts)

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

        signal = evaluate_lag_continuation_btc5m_v1(
            secs_to_end=current_secs,
            btc_price=btc_price,
            price_to_beat=price_to_beat,
            momentum_bps=momentum_bps,
            cfg=cfg,
        )
        signal["window_start_ts"] = window.window_start_ts

        if signal.get("allow"):
            allowed_sides[str(signal.get("position_side") or "NONE")] += 1
        else:
            blocked_reasons[str(signal.get("reason") or "unknown")] += 1

        if pos.mode == "open" and pos.window_start_ts != window.window_start_ts:
            pos = _close_position(pos, exit_price=btc_price, reason="window_rollover", cfg=cfg)
            completed.append(asdict(pos))
            exit_reasons[str(pos.exit_reason or "unknown")] += 1
            _append_jsonl(log_path, {"type": "exit", "ts": now, "position": completed[-1]})
            pos = PaperPositionV1()

        if pos.mode == "idle" and signal.get("allow"):
            pos = _paper_enter(signal, stake=args.stake, now=now, window_start_ts=window.window_start_ts, secs_to_end=current_secs)
            _append_jsonl(log_path, {"type": "enter", "ts": now, "signal": signal, "position": asdict(pos)})
        elif pos.mode == "open":
            if current_secs is not None and current_secs <= cfg.exit_seconds_to_end and btc_price is not None:
                pos = _close_position(pos, exit_price=btc_price, reason="time_exit", cfg=cfg)
                completed.append(asdict(pos))
                exit_reasons[str(pos.exit_reason or "unknown")] += 1
                _append_jsonl(log_path, {"type": "exit", "ts": now, "position": completed[-1]})
                pos = PaperPositionV1()

        _append_jsonl(
            log_path,
            {
                "type": "snapshot",
                "ts": now,
                "window_start_ts": window.window_start_ts,
                "current_secs": current_secs,
                "window_reason": window.reason,
                "btc_price": btc_price,
                "price_to_beat": price_to_beat,
                "momentum_bps": momentum_bps,
                "signal": signal,
                "position": asdict(pos),
            },
        )

        print(
            f"[LAG_CONTINUATION_BTC5M] secs={current_secs} allow={signal.get('allow')} "
            f"side={signal.get('position_side')} reason={signal.get('reason')} mode={pos.mode}"
        )
        time.sleep(max(0.5, float(args.poll_secs)))

    if pos.mode == "open":
        final_ref = fetch_external_btc_reference_v1()
        pos = _close_position(pos, exit_price=final_ref.get("reference_price"), reason="session_end", cfg=cfg)
        completed.append(asdict(pos))
        exit_reasons[str(pos.exit_reason or "unknown")] += 1
        _append_jsonl(log_path, {"type": "exit", "ts": time.time(), "position": completed[-1]})

    summary = {
        "stats": _trade_stats(completed),
        "allowed_sides": dict(allowed_sides),
        "exit_reasons": dict(exit_reasons),
        "top_blocked_reasons": blocked_reasons.most_common(12),
        "log_file": str(log_path),
        "config": cfg.as_dict(),
    }
    _append_jsonl(log_path, {"type": "summary", "ts": time.time(), "summary": summary})
    print("[LAG_CONTINUATION_BTC5M_SUMMARY]")
    pprint(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
