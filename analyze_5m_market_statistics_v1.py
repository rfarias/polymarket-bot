from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional

from analyze_near_resolved_reversals_v1 import Sample, _load_samples, _safe_float


def _sign(value: Optional[float], deadzone_bps: float = 0.0) -> Optional[str]:
    if value is None:
        return None
    if value > deadzone_bps:
        return "UP"
    if value < -deadzone_bps:
        return "DOWN"
    return None


def _distance_usd(sample: Sample) -> Optional[float]:
    if sample.reference_price is None or sample.opening_price is None:
        return None
    return abs(float(sample.reference_price) - float(sample.opening_price))


def _distance_vs_range(sample: Sample) -> Optional[float]:
    distance = _distance_usd(sample)
    if distance is None or sample.spot_range_60s_usd is None or sample.spot_range_60s_usd <= 0:
        return None
    return distance / float(sample.spot_range_60s_usd)


def _side_price(sample: Sample, side: Optional[str]) -> Optional[float]:
    if side not in ("UP", "DOWN"):
        return None
    return sample.side_price(side)


def _counter_price(sample: Sample, side: Optional[str]) -> Optional[float]:
    if side not in ("UP", "DOWN"):
        return None
    return sample.counter_price(side)


def _median(values: Iterable[Any]) -> Optional[float]:
    nums = []
    for value in values:
        v = _safe_float(value)
        if v is not None and math.isfinite(v):
            nums.append(v)
    if not nums:
        return None
    return round(median(nums), 6)


def _rate_row(name: str, rows: list[dict[str, Any]], *, event_key: str = "reversed") -> dict[str, Any]:
    n = len(rows)
    event_count = sum(1 for row in rows if row.get(event_key))
    return {
        "bucket": name,
        "n": n,
        f"{event_key}_count": event_count,
        f"{event_key}_rate": round(event_count / n, 6) if n else None,
        "median_touch_secs_to_end": _median(row.get("touch_secs_to_end") for row in rows),
        "median_touch_distance_usd": _median(row.get("touch_distance_usd") for row in rows),
        "median_touch_abs_bps": _median(row.get("touch_abs_bps") for row in rows),
        "median_touch_distance_vs_range": _median(row.get("touch_distance_vs_range") for row in rows),
        "median_touch_leader_price": _median(row.get("touch_leader_price") for row in rows),
        "median_touch_counter_price": _median(row.get("touch_counter_price") for row in rows),
    }


def _first_touch_rows(
    by_slug: dict[str, list[Sample]],
    *,
    usd_thresholds: list[float],
    bps_thresholds: list[float],
    vol_thresholds: list[float],
    max_secs_to_end: int,
    min_secs_to_end: int,
    deadzone_bps: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    specs: list[tuple[str, float]] = []
    specs.extend((f"usd>={x:g}", float(x)) for x in usd_thresholds)
    specs.extend((f"bps>={x:g}", float(x)) for x in bps_thresholds)
    specs.extend((f"vol>={x:g}", float(x)) for x in vol_thresholds)

    for slug, samples in by_slug.items():
        usable = [
            sample for sample in samples
            if sample.distance_from_open_bps is not None
            and sample.secs_to_end is not None
            and min_secs_to_end <= sample.secs_to_end <= max_secs_to_end
        ]
        if len(usable) < 2:
            continue
        final = next((sample for sample in reversed(samples) if _sign(sample.distance_from_open_bps, deadzone_bps) is not None), None)
        final_side = _sign(final.distance_from_open_bps, deadzone_bps) if final else None
        if final_side is None:
            continue

        for label, threshold in specs:
            if label.startswith("usd"):
                touch = next((sample for sample in usable if (_distance_usd(sample) or 0.0) >= threshold), None)
            elif label.startswith("bps"):
                touch = next((sample for sample in usable if abs(sample.distance_from_open_bps or 0.0) >= threshold), None)
            else:
                touch = next((sample for sample in usable if (_distance_vs_range(sample) or 0.0) >= threshold), None)
            if touch is None:
                continue

            touch_side = _sign(touch.distance_from_open_bps, deadzone_bps)
            if touch_side is None:
                continue
            after = [sample for sample in samples if sample.ts > touch.ts]
            crossed_after_touch = any(_sign(sample.distance_from_open_bps, deadzone_bps) not in (None, touch_side) for sample in after)
            reversed_final = final_side != touch_side
            rows.append(
                {
                    "slug": slug,
                    "bucket": label,
                    "touch_side": touch_side,
                    "final_side": final_side,
                    "reversed": bool(reversed_final),
                    "crossed_after_touch": bool(crossed_after_touch),
                    "touch_secs_to_end": touch.secs_to_end,
                    "touch_distance_usd": round(_distance_usd(touch) or 0.0, 6),
                    "touch_abs_bps": round(abs(touch.distance_from_open_bps or 0.0), 6),
                    "touch_distance_vs_range": round(_distance_vs_range(touch), 6) if _distance_vs_range(touch) is not None else None,
                    "touch_leader_price": _side_price(touch, touch_side),
                    "touch_counter_price": _counter_price(touch, touch_side),
                    "final_distance_bps": final.distance_from_open_bps,
                    "final_reference_price": final.reference_price,
                    "final_secs_to_end": final.secs_to_end,
                    "path": touch.path,
                    "line_no": touch.line_no,
                }
            )
    return rows


def _start_finish_rows(
    by_slug: dict[str, list[Sample]],
    *,
    start_min_secs_to_end: int,
    start_max_secs_to_end: int,
    min_start_abs_bps: float,
    deadzone_bps: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for slug, samples in by_slug.items():
        start = next(
            (
                sample for sample in samples
                if sample.secs_to_end is not None
                and start_min_secs_to_end <= sample.secs_to_end <= start_max_secs_to_end
                and abs(sample.distance_from_open_bps or 0.0) >= min_start_abs_bps
                and _sign(sample.distance_from_open_bps, deadzone_bps) is not None
            ),
            None,
        )
        final = next((sample for sample in reversed(samples) if _sign(sample.distance_from_open_bps, deadzone_bps) is not None), None)
        if start is None or final is None:
            continue
        start_side = _sign(start.distance_from_open_bps, deadzone_bps)
        final_side = _sign(final.distance_from_open_bps, deadzone_bps)
        if start_side is None or final_side is None:
            continue
        rows.append(
            {
                "slug": slug,
                "start_side": start_side,
                "final_side": final_side,
                "same_side": start_side == final_side,
                "start_secs_to_end": start.secs_to_end,
                "start_distance_usd": round(_distance_usd(start) or 0.0, 6),
                "start_abs_bps": round(abs(start.distance_from_open_bps or 0.0), 6),
                "start_distance_vs_range": round(_distance_vs_range(start), 6) if _distance_vs_range(start) is not None else None,
                "start_leader_price": _side_price(start, start_side),
                "start_counter_price": _counter_price(start, start_side),
                "final_distance_bps": final.distance_from_open_bps,
                "final_secs_to_end": final.secs_to_end,
                "path": start.path,
                "line_no": start.line_no,
            }
        )
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _summary(first_touch_rows: list[dict[str, Any]], start_finish_rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_bucket = defaultdict(list)
    for row in first_touch_rows:
        by_bucket[row["bucket"]].append(row)
    touch_summary = [_rate_row(bucket, rows, event_key="reversed") for bucket, rows in sorted(by_bucket.items())]
    cross_summary = [_rate_row(bucket, rows, event_key="crossed_after_touch") for bucket, rows in sorted(by_bucket.items())]

    start_groups = {
        "all": start_finish_rows,
        "UP": [row for row in start_finish_rows if row.get("start_side") == "UP"],
        "DOWN": [row for row in start_finish_rows if row.get("start_side") == "DOWN"],
    }
    start_summary = []
    for name, rows in start_groups.items():
        n = len(rows)
        same = sum(1 for row in rows if row.get("same_side"))
        start_summary.append(
            {
                "bucket": name,
                "n": n,
                "same_side_count": same,
                "same_side_rate": round(same / n, 6) if n else None,
                "median_start_secs_to_end": _median(row.get("start_secs_to_end") for row in rows),
                "median_start_abs_bps": _median(row.get("start_abs_bps") for row in rows),
                "median_start_distance_usd": _median(row.get("start_distance_usd") for row in rows),
                "median_start_distance_vs_range": _median(row.get("start_distance_vs_range") for row in rows),
            }
        )

    return {
        "first_touch_total": len(first_touch_rows),
        "start_finish_total": len(start_finish_rows),
        "first_touch_reversal_summary": touch_summary,
        "first_touch_cross_summary": cross_summary,
        "start_finish_summary": start_summary,
        "touch_side_counts": Counter(row.get("touch_side") for row in first_touch_rows),
        "final_side_counts": Counter(row.get("final_side") for row in start_finish_rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Market statistics by 5m slug from saved JSONL logs")
    parser.add_argument(
        "--paths",
        nargs="+",
        default=[
            "logs/current_almost_resolved*.jsonl",
            "logs/rigid_resolved_tick*.jsonl",
            "logs/dual_*/*.jsonl",
        ],
    )
    parser.add_argument("--usd-thresholds", nargs="+", type=float, default=[20, 30, 40, 50, 60, 70, 80, 100])
    parser.add_argument("--bps-thresholds", nargs="+", type=float, default=[2, 4, 6, 8, 10, 12])
    parser.add_argument("--vol-thresholds", nargs="+", type=float, default=[0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0])
    parser.add_argument("--touch-min-secs-to-end", type=int, default=5)
    parser.add_argument("--touch-max-secs-to-end", type=int, default=295)
    parser.add_argument("--start-min-secs-to-end", type=int, default=210)
    parser.add_argument("--start-max-secs-to-end", type=int, default=285)
    parser.add_argument("--min-start-abs-bps", type=float, default=0.5)
    parser.add_argument("--deadzone-bps", type=float, default=0.0)
    parser.add_argument("--max-lines-per-file", type=int, default=0)
    parser.add_argument("--out-prefix", type=str, default="logs/market_statistics_v1")
    args = parser.parse_args()

    by_slug = _load_samples(args.paths, args.max_lines_per_file)
    touch_rows = _first_touch_rows(
        by_slug,
        usd_thresholds=args.usd_thresholds,
        bps_thresholds=args.bps_thresholds,
        vol_thresholds=args.vol_thresholds,
        max_secs_to_end=args.touch_max_secs_to_end,
        min_secs_to_end=args.touch_min_secs_to_end,
        deadzone_bps=args.deadzone_bps,
    )
    start_rows = _start_finish_rows(
        by_slug,
        start_min_secs_to_end=args.start_min_secs_to_end,
        start_max_secs_to_end=args.start_max_secs_to_end,
        min_start_abs_bps=args.min_start_abs_bps,
        deadzone_bps=args.deadzone_bps,
    )
    summary = _summary(touch_rows, start_rows)
    summary["slug_count"] = len(by_slug)
    summary["sample_count"] = sum(len(v) for v in by_slug.values())
    summary["config"] = vars(args)

    out_prefix = Path(args.out_prefix)
    touch_csv = out_prefix.with_suffix(".first_touch.csv")
    start_csv = out_prefix.with_suffix(".start_finish.csv")
    summary_json = out_prefix.with_suffix(".summary.json")
    _write_csv(touch_csv, touch_rows)
    _write_csv(start_csv, start_rows)
    summary_json.parent.mkdir(parents=True, exist_ok=True)
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=dict), encoding="utf-8")

    print("[MARKET_STATISTICS]")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=dict))
    print(f"[FIRST_TOUCH_CSV] {touch_csv}")
    print(f"[START_FINISH_CSV] {start_csv}")
    print(f"[SUMMARY_JSON] {summary_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
