from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Optional

from analyze_near_resolved_reversals_v1 import Sample, _load_samples, _safe_float


SLUG_TS_RE = re.compile(r"btc-updown-5m-(\d+)")


def _slug_ts(slug: str) -> Optional[int]:
    m = SLUG_TS_RE.search(str(slug or ""))
    return int(m.group(1)) if m else None


def _side(distance_bps: Optional[float], deadzone_bps: float = 0.0) -> Optional[str]:
    if distance_bps is None:
        return None
    if distance_bps > deadzone_bps:
        return "UP"
    if distance_bps < -deadzone_bps:
        return "DOWN"
    return None


def _mid(a: Any, b: Any) -> Optional[float]:
    x = _safe_float(a)
    y = _safe_float(b)
    if x is None or y is None or x <= 0 or y <= 0:
        return None
    lo, hi = min(x, y), max(x, y)
    if hi - lo > 0.25:
        return None
    return round((lo + hi) / 2.0, 6)


def _slug_summary(samples: list[Sample], *, start_min_secs: int, start_max_secs: int, min_start_abs_bps: float, deadzone_bps: float) -> Optional[dict[str, Any]]:
    if not samples:
        return None
    start = next(
        (
            s for s in samples
            if s.secs_to_end is not None
            and start_min_secs <= s.secs_to_end <= start_max_secs
            and s.distance_from_open_bps is not None
            and abs(s.distance_from_open_bps) >= min_start_abs_bps
            and _side(s.distance_from_open_bps, deadzone_bps) is not None
        ),
        None,
    )
    final = next((s for s in reversed(samples) if _side(s.distance_from_open_bps, deadzone_bps) is not None), None)
    if start is None or final is None:
        return None
    start_side = _side(start.distance_from_open_bps, deadzone_bps)
    final_side = _side(final.distance_from_open_bps, deadzone_bps)
    if start_side is None or final_side is None:
        return None
    return {
        "slug": start.slug,
        "slug_ts": _slug_ts(start.slug),
        "start_side": start_side,
        "final_side": final_side,
        "same_side": start_side == final_side,
        "start_secs_to_end": start.secs_to_end,
        "start_abs_bps": abs(start.distance_from_open_bps or 0.0),
        "start_reference_price": start.reference_price,
        "start_opening_price": start.opening_price,
        "final_distance_bps": final.distance_from_open_bps,
        "final_reference_price": final.reference_price,
        "final_secs_to_end": final.secs_to_end,
    }


def _parse_next1_rows(paths: list[str]) -> dict[str, dict[str, Any]]:
    by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pattern in paths:
        for file_name in glob.glob(pattern, recursive=True):
            path = Path(file_name)
            if not path.is_file() or path.suffix.lower() != ".jsonl":
                continue
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as fh:
                    for line_no, line in enumerate(fh, 1):
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        if row.get("type") not in (None, "snapshot"):
                            continue
                        slug = str(row.get("next1_slug") or "")
                        if not slug:
                            continue
                        next1_scalp = row.get("next1_scalp") if isinstance(row.get("next1_scalp"), dict) else {}
                        metrics = {}
                        if isinstance(row.get("next1_arb"), dict):
                            metrics = row["next1_arb"].get("metrics") if isinstance(row["next1_arb"].get("metrics"), dict) else {}
                        up_mid = _safe_float(next1_scalp.get("next1_up_mid"))
                        down_mid = _safe_float(next1_scalp.get("next1_down_mid"))
                        if up_mid is None:
                            up_mid = _mid(metrics.get("up_bid"), metrics.get("up_ask"))
                        if down_mid is None:
                            down_mid = _mid(metrics.get("down_bid"), metrics.get("down_ask"))
                        if up_mid is None or down_mid is None:
                            continue
                        side = "UP" if up_mid > down_mid else "DOWN" if down_mid > up_mid else None
                        edge = abs(float(up_mid) - float(down_mid))
                        by_slug[slug].append(
                            {
                                "slug": slug,
                                "ts": _safe_float(row.get("ts"), 0.0),
                                "line_no": line_no,
                                "path": str(path),
                                "next1_secs": row.get("next1_secs"),
                                "up_mid": up_mid,
                                "down_mid": down_mid,
                                "next1_side": side,
                                "next1_edge": round(edge, 6),
                            }
                        )
            except Exception as exc:
                print(f"[WARN] failed to read {path}: {type(exc).__name__}: {exc}")
    out: dict[str, dict[str, Any]] = {}
    for slug, rows in by_slug.items():
        rows.sort(key=lambda r: (_safe_float(r.get("next1_secs"), 999999.0) or 999999.0, -(r.get("ts") or 0.0)))
        out[slug] = rows[0]
    return out


def _rate(rows: list[dict[str, Any]], key: str) -> Optional[float]:
    if not rows:
        return None
    return round(sum(1 for r in rows if r.get(key)) / len(rows), 6)


def _med(values) -> Optional[float]:
    nums = [_safe_float(v) for v in values]
    nums = [v for v in nums if v is not None and math.isfinite(v)]
    return round(median(nums), 6) if nums else None


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({k for row in rows for k in row})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze direction predictors: previous candle and next1 pre-current odds")
    parser.add_argument("--current-paths", nargs="+", default=["logs/current_almost_resolved*.jsonl", "logs/rigid_resolved_tick*.jsonl"])
    parser.add_argument("--next1-paths", nargs="+", default=["logs/all_setups_paper_until_20260427_0700_v2/*.jsonl", "logs/next1_scalp*.jsonl"])
    parser.add_argument("--start-min-secs", type=int, default=210)
    parser.add_argument("--start-max-secs", type=int, default=285)
    parser.add_argument("--min-start-abs-bps", type=float, default=0.5)
    parser.add_argument("--deadzone-bps", type=float, default=0.0)
    parser.add_argument("--out-prefix", type=str, default="logs/direction_predictors_v1")
    args = parser.parse_args()

    by_slug = _load_samples(args.current_paths, 0)
    summaries = {}
    for slug, samples in by_slug.items():
        s = _slug_summary(
            samples,
            start_min_secs=args.start_min_secs,
            start_max_secs=args.start_max_secs,
            min_start_abs_bps=args.min_start_abs_bps,
            deadzone_bps=args.deadzone_bps,
        )
        if s is not None and s.get("slug_ts") is not None:
            summaries[slug] = s

    prev_rows: list[dict[str, Any]] = []
    by_ts = {int(s["slug_ts"]): s for s in summaries.values() if s.get("slug_ts") is not None}
    for s in summaries.values():
        prev = by_ts.get(int(s["slug_ts"]) - 300)
        if not prev:
            continue
        row = dict(s)
        row.update(
            {
                "prev_slug": prev["slug"],
                "prev_start_side": prev["start_side"],
                "prev_final_side": prev["final_side"],
                "prev_same_side": prev["same_side"],
                "current_start_continues_prev_final": s["start_side"] == prev["final_side"],
                "current_start_reverses_prev_final": s["start_side"] != prev["final_side"],
                "final_continues_prev_final": s["final_side"] == prev["final_side"],
            }
        )
        prev_rows.append(row)

    next1_by_slug = _parse_next1_rows(args.next1_paths)
    next1_rows: list[dict[str, Any]] = []
    for slug, s in summaries.items():
        n = next1_by_slug.get(slug)
        if not n or n.get("next1_side") is None:
            continue
        row = dict(s)
        row.update(
            {
                "next1_side": n.get("next1_side"),
                "next1_up_mid": n.get("up_mid"),
                "next1_down_mid": n.get("down_mid"),
                "next1_edge": n.get("next1_edge"),
                "next1_secs": n.get("next1_secs"),
                "next1_path": n.get("path"),
                "next1_predicts_start": n.get("next1_side") == s["start_side"],
                "next1_predicts_final": n.get("next1_side") == s["final_side"],
                "next1_predicts_same_side_case": n.get("next1_side") == s["final_side"] and s["same_side"],
            }
        )
        next1_rows.append(row)

    same_side_rows = [r for r in prev_rows if r.get("same_side")]
    reversed_current_rows = [r for r in prev_rows if not r.get("same_side")]
    next1_edge_rows = [r for r in next1_rows if (_safe_float(r.get("next1_edge")) or 0.0) >= 0.02]

    summary = {
        "slug_count": len(summaries),
        "previous_candle_rows": len(prev_rows),
        "same_side_rows_with_prev": len(same_side_rows),
        "previous_candle_for_same_side": {
            "prev_final_same_as_current_start_rate": _rate(same_side_rows, "current_start_continues_prev_final"),
            "prev_final_opposite_current_start_rate": _rate(same_side_rows, "current_start_reverses_prev_final"),
            "prev_candle_itself_same_side_rate": _rate(same_side_rows, "prev_same_side"),
            "final_continues_prev_final_rate": _rate(same_side_rows, "final_continues_prev_final"),
        },
        "previous_candle_for_current_reversals": {
            "n": len(reversed_current_rows),
            "prev_final_same_as_current_start_rate": _rate(reversed_current_rows, "current_start_continues_prev_final"),
            "prev_final_opposite_current_start_rate": _rate(reversed_current_rows, "current_start_reverses_prev_final"),
            "prev_candle_itself_same_side_rate": _rate(reversed_current_rows, "prev_same_side"),
            "final_continues_prev_final_rate": _rate(reversed_current_rows, "final_continues_prev_final"),
        },
        "next1_rows": len(next1_rows),
        "next1_all": {
            "predicts_start_rate": _rate(next1_rows, "next1_predicts_start"),
            "predicts_final_rate": _rate(next1_rows, "next1_predicts_final"),
            "median_next1_edge": _med(r.get("next1_edge") for r in next1_rows),
            "median_next1_secs": _med(r.get("next1_secs") for r in next1_rows),
        },
        "next1_edge_ge_0_02": {
            "n": len(next1_edge_rows),
            "predicts_start_rate": _rate(next1_edge_rows, "next1_predicts_start"),
            "predicts_final_rate": _rate(next1_edge_rows, "next1_predicts_final"),
            "median_next1_edge": _med(r.get("next1_edge") for r in next1_edge_rows),
        },
        "config": vars(args),
    }

    out = Path(args.out_prefix)
    prev_csv = out.with_suffix(".previous_candle.csv")
    next1_csv = out.with_suffix(".next1.csv")
    summary_json = out.with_suffix(".summary.json")
    _write_csv(prev_csv, prev_rows)
    _write_csv(next1_csv, next1_rows)
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[DIRECTION_PREDICTORS]")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[PREVIOUS_CANDLE_CSV] {prev_csv}")
    print(f"[NEXT1_CSV] {next1_csv}")
    print(f"[SUMMARY_JSON] {summary_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
