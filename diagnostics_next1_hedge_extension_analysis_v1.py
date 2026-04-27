from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
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
    created_next1_secs: int
    created_current_secs: Optional[int]
    up_price: float
    down_price: float
    sum_asks: float
    sum_bids: float
    edge_asks: float
    edge_bids: float
    current_up_mid: float
    current_down_mid: float
    spot_delta_15s_bps: float
    spot_delta_5s_bps: float
    continuation_label: str
    current_strength: str
    spot_regime: str
    shape: str
    up_fill_360: int = 0
    down_fill_360: int = 0
    up_fill_300: int = 0
    down_fill_300: int = 0
    snapshots_to_360: int = 0
    snapshots_360_to_300: int = 0
    up_improvements_after_360: int = 0
    down_improvements_after_360: int = 0
    first_balanced_partial_qty: int = 0

    def status_360(self) -> str:
        if self.up_fill_360 >= 5 and self.down_fill_360 >= 5:
            return "hedged_by_360"
        if self.up_fill_360 > 0 and self.down_fill_360 > 0:
            return "partial_both_by_360"
        if self.up_fill_360 > 0 or self.down_fill_360 > 0:
            return "single_leg_only_by_360"
        return "no_fill_by_360"

    def status_300(self) -> str:
        if self.up_fill_300 >= 5 and self.down_fill_300 >= 5:
            return "hedged_by_300"
        if self.up_fill_300 > 0 and self.down_fill_300 > 0:
            return "partial_both_by_300"
        if self.up_fill_300 > 0 or self.down_fill_300 > 0:
            return "single_leg_only_by_300"
        return "no_fill_by_300"

    def extension_helped(self) -> bool:
        return self.status_360() != "hedged_by_360" and self.status_300() == "hedged_by_300"


def _extract_plan_case(path: Path) -> Optional[PlanCase]:
    rows = list(_jsonl(path))
    for row in rows:
        if row.get("type") != "snapshot":
            continue
        arb = row.get("next1_arb") or {}
        logs = arb.get("logs") or []
        if not any("plan_created" in str(item) for item in logs):
            continue
        metrics = arb.get("metrics") or {}
        scalp = row.get("next1_scalp") or {}
        case = PlanCase(
            file=path.name,
            event_slug=str(row.get("next1_slug") or ""),
            created_ts=_safe_float(row.get("ts"), 0.0),
            created_next1_secs=_safe_int(row.get("next1_secs"), 0),
            created_current_secs=row.get("current_secs"),
            up_price=_safe_float(metrics.get("up_ask"), 0.0),
            down_price=_safe_float(metrics.get("down_ask"), 0.0),
            sum_asks=_safe_float(metrics.get("sum_asks"), 0.0),
            sum_bids=_safe_float(metrics.get("sum_bids"), 0.0),
            edge_asks=_safe_float(metrics.get("edge_asks"), 0.0),
            edge_bids=_safe_float(metrics.get("edge_bids"), 0.0),
            current_up_mid=_safe_float(scalp.get("current_up_mid"), 0.5),
            current_down_mid=_safe_float(scalp.get("current_down_mid"), 0.5),
            spot_delta_15s_bps=_safe_float(scalp.get("spot_delta_15s_bps"), 0.0),
            spot_delta_5s_bps=_safe_float(scalp.get("spot_delta_5s_bps"), 0.0),
            continuation_label=str(arb.get("continuation_label") or "unknown"),
            current_strength=_bucket_current_strength(_safe_float(scalp.get("current_up_mid"), 0.5)),
            spot_regime=_bucket_spot_regime(_safe_float(scalp.get("spot_delta_15s_bps"), 0.0)),
            shape=_bucket_shape(_safe_float(metrics.get("up_ask"), 0.0), _safe_float(metrics.get("down_ask"), 0.0)),
        )
        _simulate_case(case, rows)
        return case
    return None


def _apply_fill(price_now: float, order_price: float, remaining: int) -> int:
    if remaining <= 0 or price_now <= 0 or order_price <= 0:
        return 0
    if price_now > order_price:
        return 0
    if abs(price_now - order_price) < 1e-9:
        return min(1, remaining)
    return min(2, remaining)


def _simulate_case(case: PlanCase, rows: List[dict]) -> None:
    started = False
    up_fill = 0
    down_fill = 0
    for row in rows:
        if row.get("type") != "snapshot":
            continue
        if str(row.get("next1_slug") or "") != case.event_slug:
            continue
        ts = _safe_float(row.get("ts"), 0.0)
        if not started:
            if abs(ts - case.created_ts) > 1e-6:
                continue
            started = True
        arb = row.get("next1_arb") or {}
        metrics = arb.get("metrics") or {}
        next1_secs = _safe_int(row.get("next1_secs"), 0)
        if next1_secs <= 0:
            continue
        up_added = _apply_fill(_safe_float(metrics.get("up_ask"), 0.0), case.up_price, 5 - up_fill)
        down_added = _apply_fill(_safe_float(metrics.get("down_ask"), 0.0), case.down_price, 5 - down_fill)
        up_fill += up_added
        down_fill += down_added
        if up_fill > 0 and down_fill > 0:
            case.first_balanced_partial_qty = max(case.first_balanced_partial_qty, min(up_fill, down_fill))

        if next1_secs > 360:
            case.snapshots_to_360 += 1
            case.up_fill_360 = up_fill
            case.down_fill_360 = down_fill
        elif next1_secs > 300:
            case.snapshots_360_to_300 += 1
            if up_added > 0:
                case.up_improvements_after_360 += up_added
            if down_added > 0:
                case.down_improvements_after_360 += down_added
        if next1_secs > 300:
            case.up_fill_300 = up_fill
            case.down_fill_300 = down_fill
        else:
            break

    if case.up_fill_300 == 0 and case.down_fill_300 == 0:
        case.up_fill_300 = up_fill
        case.down_fill_300 = down_fill
    if case.up_fill_360 == 0 and case.down_fill_360 == 0:
        case.up_fill_360 = min(case.up_fill_300, up_fill)
        case.down_fill_360 = min(case.down_fill_300, down_fill)


def _summarize_cases(cases: List[PlanCase]) -> dict:
    if not cases:
        return {}
    status_360 = Counter(case.status_360() for case in cases)
    status_300 = Counter(case.status_300() for case in cases)
    extension_helped = [case for case in cases if case.extension_helped()]
    no_help = [case for case in cases if not case.extension_helped()]
    hedged_360 = [case for case in cases if case.status_360() == "hedged_by_360"]
    partial_360 = [case for case in cases if case.status_360() == "partial_both_by_360"]
    unhedged_300 = [case for case in cases if case.status_300() != "hedged_by_300"]
    by_shape = Counter(case.shape for case in extension_helped)
    by_strength = Counter(case.current_strength for case in extension_helped)
    by_spot = Counter(case.spot_regime for case in extension_helped)
    by_label = Counter(case.continuation_label for case in extension_helped)

    def avg(values: List[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    def profile(bucket: List[PlanCase]) -> dict:
        return {
            "count": len(bucket),
            "by_shape": dict(Counter(case.shape for case in bucket)),
            "by_current_strength": dict(Counter(case.current_strength for case in bucket)),
            "by_spot_regime": dict(Counter(case.spot_regime for case in bucket)),
            "by_continuation_label": dict(Counter(case.continuation_label for case in bucket)),
            "avg_sum_asks": avg([case.sum_asks for case in bucket]),
            "avg_sum_bids": avg([case.sum_bids for case in bucket]),
            "avg_edge_asks": avg([case.edge_asks for case in bucket]),
            "avg_edge_bids": avg([case.edge_bids for case in bucket]),
            "avg_current_up_mid": avg([case.current_up_mid for case in bucket]),
            "avg_spot_delta_15s_bps": avg([case.spot_delta_15s_bps for case in bucket]),
            "avg_up_fill_360": avg([case.up_fill_360 for case in bucket]),
            "avg_down_fill_360": avg([case.down_fill_360 for case in bucket]),
        }

    return {
        "plan_count": len(cases),
        "status_by_360": dict(status_360),
        "status_by_300": dict(status_300),
        "hedged_frequency_by_360": round(status_360.get("hedged_by_360", 0) / len(cases), 4),
        "hedged_frequency_by_300": round(status_300.get("hedged_by_300", 0) / len(cases), 4),
        "additional_hedges_if_wait_to_current": {
            "count": len(extension_helped),
            "frequency": round(len(extension_helped) / len(cases), 4),
        },
        "profiles": {
            "hedged_by_360": profile(hedged_360),
            "partial_both_by_360": profile(partial_360),
            "not_hedged_by_300": profile(unhedged_300),
        },
        "extension_helped_patterns": {
            "by_shape": dict(by_shape),
            "by_current_strength": dict(by_strength),
            "by_spot_regime": dict(by_spot),
            "by_continuation_label": dict(by_label),
            "avg_first_balanced_partial_qty_at_or_before_360": avg([case.first_balanced_partial_qty for case in extension_helped]),
            "avg_up_fill_at_360": avg([case.up_fill_360 for case in extension_helped]),
            "avg_down_fill_at_360": avg([case.down_fill_360 for case in extension_helped]),
            "avg_up_improvement_after_360": avg([case.up_improvements_after_360 for case in extension_helped]),
            "avg_down_improvement_after_360": avg([case.down_improvements_after_360 for case in extension_helped]),
        },
        "non_helped_patterns": {
            "avg_up_fill_at_360": avg([case.up_fill_360 for case in no_help]),
            "avg_down_fill_at_360": avg([case.down_fill_360 for case in no_help]),
        },
        "sample_extension_helped": [
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
                "up_improvements_after_360": case.up_improvements_after_360,
                "down_improvements_after_360": case.down_improvements_after_360,
                "current_up_mid": round(case.current_up_mid, 4),
                "spot_delta_15s_bps": round(case.spot_delta_15s_bps, 4),
            }
            for case in extension_helped[:12]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze how often next1 hedge would complete if both legs stayed open until current start")
    parser.add_argument("--logs-root", type=str, default="logs", help="Logs root directory")
    parser.add_argument("--output", type=str, default=None, help="Optional JSON output path")
    args = parser.parse_args()

    root = Path(args.logs_root)
    cases = []
    for path in _round_files(root):
        case = _extract_plan_case(path)
        if case is not None:
            cases.append(case)

    report = _summarize_cases(cases)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
