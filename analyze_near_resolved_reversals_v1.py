from __future__ import annotations

import argparse
import csv
import glob
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Optional


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    v = _safe_float(value)
    if v is None:
        return default
    return int(round(v))


def _nested(row: dict, *keys: str) -> Any:
    cur: Any = row
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first_dict(*values: Any) -> dict:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


@dataclass
class Sample:
    path: str
    line_no: int
    ts: float
    slug: str
    secs_to_end: Optional[int]
    reference_price: Optional[float]
    opening_price: Optional[float]
    distance_from_open_bps: Optional[float]
    source_divergence_bps: Optional[float]
    spot_delta_5s_bps: Optional[float]
    spot_delta_15s_bps: Optional[float]
    spot_delta_30s_bps: Optional[float]
    spot_delta_60s_bps: Optional[float]
    market_delta_5s: Optional[float]
    market_delta_15s: Optional[float]
    market_range_15s: Optional[float]
    market_range_30s: Optional[float]
    market_range_60s: Optional[float]
    spot_range_60s_usd: Optional[float]
    up_price: Optional[float]
    down_price: Optional[float]
    up_bid: Optional[float]
    down_bid: Optional[float]
    up_ask: Optional[float]
    down_ask: Optional[float]
    up_depth: Optional[float]
    down_depth: Optional[float]
    signal_reason: str
    signal_variant: str

    def side_price(self, side: str) -> Optional[float]:
        return self.up_price if side == "UP" else self.down_price

    def counter_price(self, side: str) -> Optional[float]:
        return self.down_price if side == "UP" else self.up_price

    def side_winning(self, side: str) -> Optional[bool]:
        if self.distance_from_open_bps is None:
            return None
        if side == "UP":
            return self.distance_from_open_bps > 0
        return self.distance_from_open_bps < 0

    def price_to_beat_direction(self) -> str:
        if self.distance_from_open_bps is None:
            return "UNKNOWN"
        if self.distance_from_open_bps > 0:
            return "ABOVE_OPEN_UP_WINNING"
        if self.distance_from_open_bps < 0:
            return "BELOW_OPEN_DOWN_WINNING"
        return "AT_OPEN"


def _generic_signal_side(signal: dict) -> str:
    raw = str(signal.get("side") or signal.get("target_side") or "").upper()
    return raw if raw in ("UP", "DOWN") else ""


def _price_from_signal(signal: dict, context: dict, side: str) -> Optional[float]:
    lower = side.lower()
    for key in (f"{lower}_buy", f"{lower}_ask"):
        v = _safe_float(signal.get(key))
        if v is not None and 0.0 <= v <= 1.0:
            return v
    signal_side = _generic_signal_side(signal)
    if signal_side == side:
        for key in ("leader_price", "leader_ask", "leader_bid"):
            v = _safe_float(signal.get(key))
            if v is not None and 0.0 <= v <= 1.0:
                return v
    elif signal_side in ("UP", "DOWN"):
        for key in ("counter_price", "counter_ask", "counter_bid"):
            v = _safe_float(signal.get(key))
            if v is not None and 0.0 <= v <= 1.0:
                return v
    for key in (f"{lower}_ask", f"{lower}_bid", f"{lower}_mid"):
        v = _safe_float(context.get(key))
        if v is not None and 0.0 <= v <= 1.0:
            return v
    return None


def _extract_sample(row: dict, path: str, line_no: int) -> Optional[Sample]:
    if row.get("type") not in (None, "snapshot"):
        return None
    context = _first_dict(row.get("current_scalp_context"), row.get("scalp_context"), row.get("context_signal"))
    signal = _first_dict(row.get("signal"), row.get("almost_resolved_signal"), row.get("winner_signal"))
    reference = _first_dict(row.get("reference"))
    open_reference = _first_dict(row.get("open_reference"))
    slug = str(
        row.get("current_slug")
        or row.get("slug")
        or open_reference.get("slug")
        or ""
    )
    if not slug:
        return None

    ts = _safe_float(row.get("ts"))
    if ts is None:
        return None

    reference_price = _safe_float(context.get("reference_price"), _safe_float(reference.get("reference_price")))
    opening_price = _safe_float(context.get("opening_reference_price"), _safe_float(open_reference.get("price")))
    distance_from_open_bps = _safe_float(context.get("distance_from_open_bps"))
    if distance_from_open_bps is None and reference_price and opening_price:
        distance_from_open_bps = ((reference_price / opening_price) - 1.0) * 10000.0

    up_price = _price_from_signal(signal, context, "UP")
    down_price = _price_from_signal(signal, context, "DOWN")
    if up_price is None and down_price is not None:
        up_price = round(max(0.0, min(1.0, 1.0 - down_price)), 6)
    if down_price is None and up_price is not None:
        down_price = round(max(0.0, min(1.0, 1.0 - up_price)), 6)
    if up_price is None or down_price is None:
        return None

    return Sample(
        path=path,
        line_no=line_no,
        ts=ts,
        slug=slug,
        secs_to_end=_safe_int(row.get("current_secs"), _safe_int(row.get("secs_to_end"), _safe_int(context.get("secs_to_end"), _safe_int(signal.get("secs_to_end"))))),
        reference_price=reference_price,
        opening_price=opening_price,
        distance_from_open_bps=distance_from_open_bps,
        source_divergence_bps=_safe_float(context.get("source_divergence_bps"), _safe_float(reference.get("source_divergence_bps"))),
        spot_delta_5s_bps=_safe_float(context.get("spot_delta_5s_bps")),
        spot_delta_15s_bps=_safe_float(context.get("spot_delta_15s_bps")),
        spot_delta_30s_bps=_safe_float(context.get("spot_delta_30s_bps")),
        spot_delta_60s_bps=_safe_float(context.get("spot_delta_60s_bps")),
        market_delta_5s=_safe_float(context.get("market_delta_5s")),
        market_delta_15s=_safe_float(context.get("market_delta_15s")),
        market_range_15s=_safe_float(context.get("market_range_15s")),
        market_range_30s=_safe_float(context.get("market_range_30s")),
        market_range_60s=_safe_float(context.get("market_range_60s")),
        spot_range_60s_usd=_safe_float(context.get("spot_range_60s_usd")),
        up_price=up_price,
        down_price=down_price,
        up_bid=_safe_float(context.get("up_bid"), _safe_float(signal.get("up_sell"))),
        down_bid=_safe_float(context.get("down_bid"), _safe_float(signal.get("down_sell"))),
        up_ask=_safe_float(context.get("up_ask"), _safe_float(signal.get("up_buy"))),
        down_ask=_safe_float(context.get("down_ask"), _safe_float(signal.get("down_buy"))),
        up_depth=_safe_float(signal.get("up_depth_top3_both_sides"), _safe_float(context.get("combined_depth_top3"))),
        down_depth=_safe_float(signal.get("down_depth_top3_both_sides"), _safe_float(context.get("combined_depth_top3"))),
        signal_reason=str(signal.get("reason") or ""),
        signal_variant=str(signal.get("setup_variant") or signal.get("variant") or signal.get("phase") or ""),
    )


def _load_samples(paths: list[str], max_lines_per_file: int) -> dict[str, list[Sample]]:
    by_slug: dict[str, list[Sample]] = defaultdict(list)
    for pattern in paths:
        for file_name in glob.glob(pattern, recursive=True):
            path = Path(file_name)
            if not path.is_file() or path.suffix.lower() != ".jsonl":
                continue
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as fh:
                    for line_no, line in enumerate(fh, 1):
                        if max_lines_per_file > 0 and line_no > max_lines_per_file:
                            break
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        sample = _extract_sample(row, str(path), line_no)
                        if sample is not None:
                            by_slug[sample.slug].append(sample)
            except Exception as exc:
                print(f"[WARN] failed to read {path}: {type(exc).__name__}: {exc}")
    for slug in list(by_slug):
        by_slug[slug].sort(key=lambda s: s.ts)
    return by_slug


def _find_before(samples: list[Sample], ts: float, lookback_secs: float) -> Optional[Sample]:
    target = ts - lookback_secs
    candidate = None
    for sample in samples:
        if sample.ts <= target:
            candidate = sample
        else:
            break
    return candidate


def _feature_row(prefix: str, sample: Optional[Sample], side: str) -> dict[str, Any]:
    if sample is None:
        return {}
    side_price = sample.side_price(side)
    counter = sample.counter_price(side)
    adverse_5 = None
    adverse_15 = None
    adverse_30 = None
    if side == "UP":
        adverse_5 = -sample.spot_delta_5s_bps if sample.spot_delta_5s_bps is not None else None
        adverse_15 = -sample.spot_delta_15s_bps if sample.spot_delta_15s_bps is not None else None
        adverse_30 = -sample.spot_delta_30s_bps if sample.spot_delta_30s_bps is not None else None
    else:
        adverse_5 = sample.spot_delta_5s_bps
        adverse_15 = sample.spot_delta_15s_bps
        adverse_30 = sample.spot_delta_30s_bps
    return {
        f"{prefix}_ts": sample.ts,
        f"{prefix}_secs_to_end": sample.secs_to_end,
        f"{prefix}_side_price": side_price,
        f"{prefix}_counter_price": counter,
        f"{prefix}_edge_vs_counter": round((side_price or 0.0) - (counter or 0.0), 6) if side_price is not None and counter is not None else None,
        f"{prefix}_reference_price": sample.reference_price,
        f"{prefix}_opening_price": sample.opening_price,
        f"{prefix}_distance_from_open_bps": sample.distance_from_open_bps,
        f"{prefix}_price_to_beat_direction": sample.price_to_beat_direction(),
        f"{prefix}_source_divergence_bps": sample.source_divergence_bps,
        f"{prefix}_spot_delta_5s_bps": sample.spot_delta_5s_bps,
        f"{prefix}_spot_delta_15s_bps": sample.spot_delta_15s_bps,
        f"{prefix}_spot_delta_30s_bps": sample.spot_delta_30s_bps,
        f"{prefix}_adverse_5s_bps": adverse_5,
        f"{prefix}_adverse_15s_bps": adverse_15,
        f"{prefix}_adverse_30s_bps": adverse_30,
        f"{prefix}_market_delta_5s": sample.market_delta_5s,
        f"{prefix}_market_delta_15s": sample.market_delta_15s,
        f"{prefix}_market_range_15s": sample.market_range_15s,
        f"{prefix}_market_range_30s": sample.market_range_30s,
        f"{prefix}_market_range_60s": sample.market_range_60s,
        f"{prefix}_spot_range_60s_usd": sample.spot_range_60s_usd,
        f"{prefix}_distance_vs_spot_range_60s": (
            round(abs(sample.reference_price - sample.opening_price) / sample.spot_range_60s_usd, 6)
            if sample.reference_price is not None
            and sample.opening_price is not None
            and sample.spot_range_60s_usd is not None
            and sample.spot_range_60s_usd > 0
            else None
        ),
        f"{prefix}_signal_reason": sample.signal_reason,
        f"{prefix}_signal_variant": sample.signal_variant,
        f"{prefix}_path": sample.path,
        f"{prefix}_line_no": sample.line_no,
    }


def _detect_events(
    by_slug: dict[str, list[Sample]],
    *,
    near_price: float,
    max_counter_at_peak: float,
    require_winner_at_peak: bool,
    deep_drawdown_price: float,
    min_drop: float,
    max_secs_to_end: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for slug, samples in by_slug.items():
        if len(samples) < 2:
            continue
        for side in ("UP", "DOWN"):
            near_samples = [
                s for s in samples
                if (s.secs_to_end is None or s.secs_to_end <= max_secs_to_end)
                and s.side_price(side) is not None
                and s.side_price(side) >= near_price
                and (s.counter_price(side) is None or s.counter_price(side) <= max_counter_at_peak)
                and (not require_winner_at_peak or s.side_winning(side) is True)
            ]
            if not near_samples:
                continue
            peak = max(near_samples, key=lambda s: (s.side_price(side) or 0.0, s.ts))
            after = [s for s in samples if s.ts > peak.ts]
            if not after:
                continue
            peak_price = peak.side_price(side) or 0.0
            crossed = next((s for s in after if s.side_winning(side) is False), None)
            drawdown = next(
                (
                    s for s in after
                    if s.side_price(side) is not None
                    and (s.side_price(side) <= deep_drawdown_price or peak_price - s.side_price(side) >= min_drop)
                ),
                None,
            )
            worst_after = min(after, key=lambda s: s.side_price(side) if s.side_price(side) is not None else 999.0)
            best_counter_after = max(
                after,
                key=lambda s: s.counter_price(side) if s.counter_price(side) is not None else -1.0,
            )
            final = samples[-1]
            event_type = "survived"
            event_sample = final
            if crossed is not None:
                event_type = "crossed_price_to_beat"
                event_sample = crossed
            elif drawdown is not None:
                event_type = "deep_drawdown"
                event_sample = drawdown

            row: dict[str, Any] = {
                "slug": slug,
                "side": side,
                "event_type": event_type,
                "peak_price": peak_price,
                "worst_after_price": worst_after.side_price(side),
                "max_drop_after_peak": round(peak_price - (worst_after.side_price(side) or peak_price), 6),
                "best_counter_after_peak": best_counter_after.counter_price(side),
                "best_counter_delay_secs": round(best_counter_after.ts - peak.ts, 3),
                "event_delay_secs": round(event_sample.ts - peak.ts, 3),
                "event_path": event_sample.path,
            }
            row.update(_feature_row("peak", peak, side))
            row.update(_feature_row("pre_5s", _find_before(samples, event_sample.ts, 5), side))
            row.update(_feature_row("pre_15s", _find_before(samples, event_sample.ts, 15), side))
            row.update(_feature_row("pre_30s", _find_before(samples, event_sample.ts, 30), side))
            row.update(_feature_row("event", event_sample, side))
            row.update(_feature_row("final", final, side))
            events.append(row)
    return events


def _num(values: list[Any]) -> list[float]:
    out = []
    for value in values:
        v = _safe_float(value)
        if v is not None:
            out.append(v)
    return out


def _stats(values: list[Any]) -> dict[str, Any]:
    nums = _num(values)
    if not nums:
        return {"n": 0}
    nums.sort()
    return {
        "n": len(nums),
        "median": round(median(nums), 6),
        "min": round(nums[0], 6),
        "max": round(nums[-1], 6),
    }


def _summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_type = Counter(e.get("event_type") for e in events)
    fields = [
        "peak_side_price",
        "peak_counter_price",
        "peak_edge_vs_counter",
        "peak_distance_from_open_bps",
        "peak_secs_to_end",
        "pre_5s_adverse_5s_bps",
        "pre_15s_adverse_15s_bps",
        "pre_30s_adverse_30s_bps",
        "pre_15s_market_range_15s",
        "pre_30s_market_range_30s",
        "pre_30s_spot_range_60s_usd",
        "pre_30s_distance_vs_spot_range_60s",
        "pre_30s_source_divergence_bps",
        "event_delay_secs",
        "max_drop_after_peak",
        "best_counter_after_peak",
        "best_counter_delay_secs",
    ]
    grouped: dict[str, Any] = {}
    for event_type in sorted(by_type):
        subset = [e for e in events if e.get("event_type") == event_type]
        grouped[event_type] = {
            "count": len(subset),
            "stats": {field: _stats([e.get(field) for e in subset]) for field in fields},
            "top_peak_reasons": Counter(str(e.get("peak_signal_reason") or "") for e in subset).most_common(10),
        }
    return {"total_events": len(events), "by_type": dict(by_type), "groups": grouped}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen = set()
    preferred = [
        "slug", "side", "event_type", "peak_price", "worst_after_price", "max_drop_after_peak", "event_delay_secs",
        "best_counter_after_peak", "best_counter_delay_secs",
        "peak_secs_to_end", "peak_side_price", "peak_counter_price", "peak_edge_vs_counter", "peak_distance_from_open_bps",
        "pre_5s_adverse_5s_bps", "pre_15s_adverse_15s_bps", "pre_30s_adverse_30s_bps",
        "pre_15s_market_range_15s", "pre_30s_market_range_30s", "event_side_price", "event_counter_price",
        "event_distance_from_open_bps", "peak_spot_range_60s_usd", "peak_distance_vs_spot_range_60s", "event_path",
    ]
    for key in preferred + sorted({k for row in rows for k in row}):
        if key not in seen:
            fields.append(key)
            seen.add(key)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze near-resolved BTC up/down reversals from saved JSONL logs")
    parser.add_argument(
        "--paths",
        nargs="+",
        default=[
            "logs/current_almost_resolved*.jsonl",
            "logs/rigid_resolved_tick*.jsonl",
            "logs/current_15m_shadow*/*.jsonl",
        ],
    )
    parser.add_argument("--near-price", type=float, default=0.90)
    parser.add_argument("--max-counter-at-peak", type=float, default=0.25)
    parser.add_argument("--allow-not-winning-at-peak", action="store_true")
    parser.add_argument("--deep-drawdown-price", type=float, default=0.75)
    parser.add_argument("--min-drop", type=float, default=0.20)
    parser.add_argument("--max-secs-to-end", type=int, default=90)
    parser.add_argument("--max-lines-per-file", type=int, default=0)
    parser.add_argument("--out-prefix", type=str, default="logs/near_resolved_reversal_analysis_v1")
    args = parser.parse_args()

    by_slug = _load_samples(args.paths, args.max_lines_per_file)
    events = _detect_events(
        by_slug,
        near_price=float(args.near_price),
        max_counter_at_peak=float(args.max_counter_at_peak),
        require_winner_at_peak=not bool(args.allow_not_winning_at_peak),
        deep_drawdown_price=float(args.deep_drawdown_price),
        min_drop=float(args.min_drop),
        max_secs_to_end=int(args.max_secs_to_end),
    )
    summary = _summary(events)
    summary["slug_count"] = len(by_slug)
    summary["sample_count"] = sum(len(v) for v in by_slug.values())
    summary["config"] = {
        "paths": args.paths,
        "near_price": args.near_price,
        "max_counter_at_peak": args.max_counter_at_peak,
        "require_winner_at_peak": not bool(args.allow_not_winning_at_peak),
        "deep_drawdown_price": args.deep_drawdown_price,
        "min_drop": args.min_drop,
        "max_secs_to_end": args.max_secs_to_end,
    }

    out_prefix = Path(args.out_prefix)
    csv_path = out_prefix.with_suffix(".events.csv")
    json_path = out_prefix.with_suffix(".summary.json")
    _write_csv(csv_path, events)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("[NEAR_RESOLVED_REVERSAL_ANALYSIS]")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[EVENTS_CSV] {csv_path}")
    print(f"[SUMMARY_JSON] {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
