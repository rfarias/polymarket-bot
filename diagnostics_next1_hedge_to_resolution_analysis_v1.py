from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except Exception:
                continue


def _round_files(root: Path) -> List[Path]:
    return sorted(root.glob("all_setups_paper*/round_*.jsonl"))


def _bucket_spot_regime(spot_15s_bps: float) -> str:
    if spot_15s_bps <= -5.0:
        return "down_5bps_plus"
    if spot_15s_bps <= -2.0:
        return "down_2_5bps"
    if spot_15s_bps < -0.5:
        return "down_0.5_2bps"
    if spot_15s_bps <= 0.5:
        return "flat_pm_0.5bps"
    if spot_15s_bps < 2.0:
        return "up_0.5_2bps"
    if spot_15s_bps < 5.0:
        return "up_2_5bps"
    return "up_5bps_plus"


def _bucket_current_strength(current_up_mid: float) -> str:
    if current_up_mid >= 0.85 or current_up_mid <= 0.15:
        return "extreme"
    if current_up_mid >= 0.70 or current_up_mid <= 0.30:
        return "strong"
    if current_up_mid >= 0.60 or current_up_mid <= 0.40:
        return "medium"
    return "neutral"


def _bucket_shape(up_ask: float, down_ask: float) -> str:
    lo = min(up_ask, down_ask)
    hi = max(up_ask, down_ask)
    if lo >= 0.49 and hi <= 0.50:
        return "49_50"
    if lo >= 0.48 and hi <= 0.50:
        return "48_50"
    if lo >= 0.47 and hi <= 0.51:
        return "47_51"
    if lo <= 0.47 and hi >= 0.54:
        return "47_54_plus"
    return "other"


@dataclass
class PlanCase:
    file: str
    event_slug: str
    created_ts: float
    up_price: float
    down_price: float
    sum_asks: float
    sum_bids: float
    edge_asks: float
    edge_bids: float
    current_up_mid: float
    spot_delta_15s_bps: float
    continuation_label: str
    current_strength: str
    spot_regime: str
    shape: str
    up_fill_360: int = 0
    down_fill_360: int = 0
    up_fill_300: int = 0
    down_fill_300: int = 0
    up_fill_resolve: int = 0
    down_fill_resolve: int = 0
    first_hedged_stage: str = "never"
    up_improvements_after_360: int = 0
    down_improvements_after_360: int = 0
    up_improvements_after_300: int = 0
    down_improvements_after_300: int = 0
    next1_phase_snapshots: int = 0
    current_phase_snapshots: int = 0

    def status(self, up_fill: int, down_fill: int, label: str) -> str:
        if up_fill >= 5 and down_fill >= 5:
            return f"hedged_by_{label}"
        if up_fill > 0 and down_fill > 0:
            return f"partial_both_by_{label}"
        if up_fill > 0 or down_fill > 0:
            return f"single_leg_only_by_{label}"
        return f"no_fill_by_{label}"


def _apply_fill(price_now: float, order_price: float, remaining: int) -> int:
    if remaining <= 0 or price_now <= 0 or order_price <= 0:
        return 0
    if price_now > order_price:
        return 0
    if abs(price_now - order_price) < 1e-9:
        return min(1, remaining)
    return min(2, remaining)


def _build_event_timelines(root: Path) -> Dict[str, List[dict]]:
    timelines: Dict[str, List[dict]] = {}
    for path in _round_files(root):
        for row in _jsonl(path):
            if row.get("type") != "snapshot":
                continue
            current_slug = str(row.get("current_slug") or "")
            next1_slug = str(row.get("next1_slug") or "")
            if current_slug:
                timelines.setdefault(current_slug, []).append(
                    {
                        "ts": _safe_float(row.get("ts"), 0.0),
                        "phase": "current",
                        "event_slug": current_slug,
                        "secs_to_end": _safe_int(row.get("current_secs"), 0),
                        "up_ask": _safe_float(((row.get("current_scalp") or {}).get("up_ask")), 0.0),
                        "down_ask": _safe_float(((row.get("current_scalp") or {}).get("down_ask")), 0.0),
                    }
                )
            if next1_slug:
                metrics = ((row.get("next1_arb") or {}).get("metrics") or {})
                timelines.setdefault(next1_slug, []).append(
                    {
                        "ts": _safe_float(row.get("ts"), 0.0),
                        "phase": "next1",
                        "event_slug": next1_slug,
                        "secs_to_end": _safe_int(row.get("next1_secs"), 0),
                        "up_ask": _safe_float(metrics.get("up_ask"), 0.0),
                        "down_ask": _safe_float(metrics.get("down_ask"), 0.0),
                    }
                )
    for slug, rows in timelines.items():
        dedup = {}
        for row in rows:
            dedup[(row["ts"], row["phase"])] = row
        timelines[slug] = sorted(dedup.values(), key=lambda item: item["ts"])
    return timelines


def _extract_plan_cases(root: Path) -> List[PlanCase]:
    cases: List[PlanCase] = []
    for path in _round_files(root):
        for row in _jsonl(path):
            if row.get("type") != "snapshot":
                continue
            arb = row.get("next1_arb") or {}
            logs = arb.get("logs") or []
            if not any("plan_created" in str(item) for item in logs):
                continue
            metrics = arb.get("metrics") or {}
            scalp = row.get("next1_scalp") or {}
            cases.append(
                PlanCase(
                    file=path.name,
                    event_slug=str(row.get("next1_slug") or ""),
                    created_ts=_safe_float(row.get("ts"), 0.0),
                    up_price=_safe_float(metrics.get("up_ask"), 0.0),
                    down_price=_safe_float(metrics.get("down_ask"), 0.0),
                    sum_asks=_safe_float(metrics.get("sum_asks"), 0.0),
                    sum_bids=_safe_float(metrics.get("sum_bids"), 0.0),
                    edge_asks=_safe_float(metrics.get("edge_asks"), 0.0),
                    edge_bids=_safe_float(metrics.get("edge_bids"), 0.0),
                    current_up_mid=_safe_float(scalp.get("current_up_mid"), 0.5),
                    spot_delta_15s_bps=_safe_float(scalp.get("spot_delta_15s_bps"), 0.0),
                    continuation_label=str(arb.get("continuation_label") or "unknown"),
                    current_strength=_bucket_current_strength(_safe_float(scalp.get("current_up_mid"), 0.5)),
                    spot_regime=_bucket_spot_regime(_safe_float(scalp.get("spot_delta_15s_bps"), 0.0)),
                    shape=_bucket_shape(_safe_float(metrics.get("up_ask"), 0.0), _safe_float(metrics.get("down_ask"), 0.0)),
                )
            )
            break
    return cases


def _simulate_case(case: PlanCase, timeline: List[dict]) -> None:
    up_fill = 0
    down_fill = 0
    started = False
    for row in timeline:
        ts = _safe_float(row.get("ts"), 0.0)
        if ts + 1e-6 < case.created_ts:
            continue
        started = True
        phase = str(row.get("phase") or "")
        secs_to_end = _safe_int(row.get("secs_to_end"), 0)
        if phase == "next1":
            case.next1_phase_snapshots += 1
        elif phase == "current":
            case.current_phase_snapshots += 1

        up_added = _apply_fill(_safe_float(row.get("up_ask"), 0.0), case.up_price, 5 - up_fill)
        down_added = _apply_fill(_safe_float(row.get("down_ask"), 0.0), case.down_price, 5 - down_fill)
        up_fill += up_added
        down_fill += down_added

        if phase == "next1" and secs_to_end > 360:
            case.up_fill_360 = up_fill
            case.down_fill_360 = down_fill
        elif phase == "next1" and 300 < secs_to_end <= 360:
            if up_added > 0:
                case.up_improvements_after_360 += up_added
            if down_added > 0:
                case.down_improvements_after_360 += down_added
            case.up_fill_300 = up_fill
            case.down_fill_300 = down_fill
        elif phase == "next1" and secs_to_end <= 300:
            case.up_fill_300 = up_fill
            case.down_fill_300 = down_fill
            if up_added > 0:
                case.up_improvements_after_300 += up_added
            if down_added > 0:
                case.down_improvements_after_300 += down_added
        elif phase == "current":
            if up_added > 0:
                case.up_improvements_after_300 += up_added
            if down_added > 0:
                case.down_improvements_after_300 += down_added

        case.up_fill_resolve = up_fill
        case.down_fill_resolve = down_fill

        if case.first_hedged_stage == "never" and up_fill >= 5 and down_fill >= 5:
            if phase == "next1" and secs_to_end > 360:
                case.first_hedged_stage = "by_360"
            elif phase == "next1":
                case.first_hedged_stage = "after_360_before_300"
            else:
                case.first_hedged_stage = "during_current"

        if phase == "current" and secs_to_end <= 5:
            break

    if not started:
        return
    if case.up_fill_300 == 0 and case.down_fill_300 == 0:
        case.up_fill_300 = case.up_fill_resolve
        case.down_fill_300 = case.down_fill_resolve
    if case.up_fill_360 == 0 and case.down_fill_360 == 0:
        case.up_fill_360 = min(case.up_fill_resolve, case.up_fill_300)
        case.down_fill_360 = min(case.down_fill_resolve, case.down_fill_300)


def _avg(values: List[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _profile(cases: List[PlanCase]) -> dict:
    return {
        "count": len(cases),
        "by_shape": dict(Counter(case.shape for case in cases)),
        "by_current_strength": dict(Counter(case.current_strength for case in cases)),
        "by_spot_regime": dict(Counter(case.spot_regime for case in cases)),
        "by_continuation_label": dict(Counter(case.continuation_label for case in cases)),
        "avg_sum_asks": _avg([case.sum_asks for case in cases]),
        "avg_sum_bids": _avg([case.sum_bids for case in cases]),
        "avg_current_up_mid": _avg([case.current_up_mid for case in cases]),
        "avg_spot_delta_15s_bps": _avg([case.spot_delta_15s_bps for case in cases]),
        "avg_up_fill_360": _avg([case.up_fill_360 for case in cases]),
        "avg_down_fill_360": _avg([case.down_fill_360 for case in cases]),
        "avg_up_fill_300": _avg([case.up_fill_300 for case in cases]),
        "avg_down_fill_300": _avg([case.down_fill_300 for case in cases]),
        "avg_up_fill_resolve": _avg([case.up_fill_resolve for case in cases]),
        "avg_down_fill_resolve": _avg([case.down_fill_resolve for case in cases]),
        "avg_next1_phase_snapshots": _avg([case.next1_phase_snapshots for case in cases]),
        "avg_current_phase_snapshots": _avg([case.current_phase_snapshots for case in cases]),
    }


def _summarize(cases: List[PlanCase]) -> dict:
    status_360 = Counter(case.status(case.up_fill_360, case.down_fill_360, "360") for case in cases)
    status_300 = Counter(case.status(case.up_fill_300, case.down_fill_300, "300") for case in cases)
    status_resolve = Counter(case.status(case.up_fill_resolve, case.down_fill_resolve, "resolve") for case in cases)

    additional_to_300 = [
        case
        for case in cases
        if case.status(case.up_fill_360, case.down_fill_360, "360") != "hedged_by_360"
        and case.status(case.up_fill_300, case.down_fill_300, "300") == "hedged_by_300"
    ]
    additional_to_resolve = [
        case
        for case in cases
        if case.status(case.up_fill_300, case.down_fill_300, "300") != "hedged_by_300"
        and case.status(case.up_fill_resolve, case.down_fill_resolve, "resolve") == "hedged_by_resolve"
    ]
    hedged_only_during_current = [case for case in cases if case.first_hedged_stage == "during_current"]
    never_hedged = [case for case in cases if case.status(case.up_fill_resolve, case.down_fill_resolve, "resolve") != "hedged_by_resolve"]

    return {
        "plan_count": len(cases),
        "status_by_360": dict(status_360),
        "status_by_300": dict(status_300),
        "status_by_resolve": dict(status_resolve),
        "hedged_frequency_by_360": round(status_360.get("hedged_by_360", 0) / len(cases), 4) if cases else 0.0,
        "hedged_frequency_by_300": round(status_300.get("hedged_by_300", 0) / len(cases), 4) if cases else 0.0,
        "hedged_frequency_by_resolve": round(status_resolve.get("hedged_by_resolve", 0) / len(cases), 4) if cases else 0.0,
        "additional_hedges_waiting_360_to_300": {
            "count": len(additional_to_300),
            "frequency": round(len(additional_to_300) / len(cases), 4) if cases else 0.0,
        },
        "additional_hedges_waiting_300_to_resolve": {
            "count": len(additional_to_resolve),
            "frequency": round(len(additional_to_resolve) / len(cases), 4) if cases else 0.0,
        },
        "profiles": {
            "hedged_by_360": _profile([case for case in cases if case.status(case.up_fill_360, case.down_fill_360, "360") == "hedged_by_360"]),
            "hedged_only_during_current": _profile(hedged_only_during_current),
            "never_hedged_even_by_resolve": _profile(never_hedged),
        },
        "sample_hedged_only_during_current": [
            {
                "file": case.file,
                "event_slug": case.event_slug,
                "shape": case.shape,
                "current_strength": case.current_strength,
                "spot_regime": case.spot_regime,
                "continuation_label": case.continuation_label,
                "sum_asks": case.sum_asks,
                "sum_bids": case.sum_bids,
                "up_price": case.up_price,
                "down_price": case.down_price,
                "up_fill_360": case.up_fill_360,
                "down_fill_360": case.down_fill_360,
                "up_fill_300": case.up_fill_300,
                "down_fill_300": case.down_fill_300,
                "up_fill_resolve": case.up_fill_resolve,
                "down_fill_resolve": case.down_fill_resolve,
                "up_improvements_after_300": case.up_improvements_after_300,
                "down_improvements_after_300": case.down_improvements_after_300,
                "current_up_mid": round(case.current_up_mid, 4),
                "spot_delta_15s_bps": round(case.spot_delta_15s_bps, 4),
            }
            for case in hedged_only_during_current[:12]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze next1 hedge completion if both legs remain live until the event resolves as current")
    parser.add_argument("--logs-root", type=str, default="logs", help="Logs root directory")
    parser.add_argument("--output", type=str, default=None, help="Optional JSON output path")
    args = parser.parse_args()

    root = Path(args.logs_root)
    timelines = _build_event_timelines(root)
    cases = _extract_plan_cases(root)
    for case in cases:
        timeline = timelines.get(case.event_slug) or []
        _simulate_case(case, timeline)

    report = _summarize(cases)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
