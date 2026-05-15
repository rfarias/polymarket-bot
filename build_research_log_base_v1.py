from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _nested(row: dict[str, Any], *keys: str) -> Any:
    cur: Any = row
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _infer_runner(path: Path) -> str:
    text = str(path).replace("\\", "/").lower()
    if "almost_resolved_gray_zone" in text:
        return "current_almost_resolved_gray_zone"
    if "almost" in text and "resolved" in text:
        return "current_almost_resolved"
    if "counter_reversal" in text:
        return "counter_reversal"
    if "trend_trailing" in text:
        return "current_trend_trailing"
    if "rigid_resolved_tick" in text:
        return "rigid_resolved_tick"
    if "guardian" in text:
        return "guardian"
    if "15m" in text:
        return "current_15m"
    if "next1" in text or "next_1" in text:
        return "next1"
    if "all_setups" in text:
        return "all_setups"
    return "unknown"


def _infer_slug(row: dict[str, Any]) -> str:
    for key in ("current_slug", "slug", "event_slug", "market_slug"):
        value = row.get(key)
        if value:
            return str(value)
    for key in ("signal", "trade", "order"):
        value = _nested(row, key, "current_slug") or _nested(row, key, "event_slug") or _nested(row, key, "slug")
        if value:
            return str(value)
    return ""


def _infer_secs(row: dict[str, Any]) -> int | None:
    for key in ("current_secs", "secs_to_end", "seconds_to_end"):
        value = _safe_int(row.get(key))
        if value is not None:
            return value
    for key in ("signal", "trade"):
        for inner in ("secs_to_end", "current_secs", "entry_secs"):
            value = _safe_int(_nested(row, key, inner))
            if value is not None:
                return value
    return None


def _side_prices(signal: dict[str, Any]) -> tuple[str, float | None, float | None]:
    side = str(signal.get("side") or "")
    up_buy = _safe_float(signal.get("up_buy"))
    down_buy = _safe_float(signal.get("down_buy"))
    if side in ("UP", "DOWN"):
        leader = up_buy if side == "UP" else down_buy
        counter = down_buy if side == "UP" else up_buy
        return side, leader, counter
    if up_buy is not None and down_buy is not None:
        return ("UP", up_buy, down_buy) if up_buy >= down_buy else ("DOWN", down_buy, up_buy)
    leader_side = str(signal.get("leader_side") or "")
    if leader_side in ("UP", "DOWN"):
        return leader_side, _safe_float(signal.get("leader_price")), _safe_float(signal.get("counter_price"))
    return side, _safe_float(signal.get("leader_price")), _safe_float(signal.get("counter_price"))


def _normalize(path: Path, line_no: int, row: dict[str, Any]) -> dict[str, Any]:
    signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
    trade = row.get("trade") if isinstance(row.get("trade"), dict) else {}
    ctx = row.get("current_scalp_context") if isinstance(row.get("current_scalp_context"), dict) else {}
    side, leader_price, counter_price = _side_prices(signal)
    entry_price = _safe_float(trade.get("entry_price"), _safe_float(signal.get("entry_price")))
    exit_price = _safe_float(trade.get("exit_price"))
    pnl_ticks = _safe_float(trade.get("pnl_ticks"), _safe_float(row.get("pnl_ticks")))
    signed_distance = _safe_float(signal.get("signed_distance_from_open_bps"), _safe_float(ctx.get("distance_from_open_bps")))

    return {
        "source_file": str(path),
        "source_line": line_no,
        "runner": _infer_runner(path),
        "type": str(row.get("type") or ""),
        "ts": _safe_float(row.get("ts")),
        "slug": _infer_slug(row),
        "secs_to_end": _infer_secs(row),
        "setup": str(signal.get("setup") or trade.get("setup") or ""),
        "setup_variant": str(signal.get("setup_variant") or trade.get("setup_variant") or ""),
        "allow": bool(signal.get("allow")) if signal else None,
        "reason": str(signal.get("reason") or row.get("reason") or trade.get("exit_reason") or ""),
        "side": side,
        "leader_price": leader_price,
        "counter_price": counter_price,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_reason": str(trade.get("exit_reason") or row.get("exit_reason") or ""),
        "pnl_ticks": pnl_ticks,
        "distance_from_open_bps": signed_distance,
        "distance_to_price_to_beat_bps": _safe_float(signal.get("distance_to_price_to_beat_bps")),
        "distance_to_price_to_beat_usd": _safe_float(signal.get("distance_to_price_to_beat_usd")),
        "buffer_bps": _safe_float(
            signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps")
        ),
        "buffer_usd": _safe_float(
            signal.get("up_price_to_beat_buffer_usd" if side == "UP" else "down_price_to_beat_buffer_usd")
        ),
        "spot_delta_5s_bps": _safe_float(ctx.get("spot_delta_5s_bps"), _safe_float(signal.get("spot_delta_5s_bps"))),
        "spot_delta_15s_bps": _safe_float(ctx.get("spot_delta_15s_bps"), _safe_float(signal.get("spot_delta_15s_bps"))),
        "spot_delta_30s_bps": _safe_float(ctx.get("spot_delta_30s_bps"), _safe_float(signal.get("spot_delta_30s_bps"))),
        "market_range_30s": _safe_float(signal.get("market_range_30s"), _safe_float(ctx.get("market_range_30s"))),
        "green_hold_ready": row.get("green_hold_ready"),
        "gray_target_stop_ready": row.get("gray_target_stop_ready"),
        "raw": row,
    }


def _iter_jsonl_files(root: Path, include_globs: list[str], exclude_parts: list[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in include_globs:
        files.extend(root.glob(pattern))
    unique = sorted({p.resolve() for p in files if p.is_file()})
    filtered = []
    for path in unique:
        text = str(path).replace("\\", "/").lower()
        if any(part.lower() in text for part in exclude_parts):
            continue
        filtered.append(path)
    return filtered


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a normalized research JSONL base from all runner logs")
    parser.add_argument("--logs-root", default="logs")
    parser.add_argument("--output", default="logs/research_base/research_events_all_v1.jsonl")
    parser.add_argument("--summary", default="logs/research_base/research_events_all_v1_summary.json")
    parser.add_argument("--include", action="append", default=["**/*.jsonl"], help="Glob under logs root; can be repeated")
    parser.add_argument("--exclude", action="append", default=["research_base"], help="Path fragment to exclude; can be repeated")
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument("--keep-raw", action="store_true", help="Keep original row inside normalized event")
    args = parser.parse_args()

    root = Path(args.logs_root)
    files = _iter_jsonl_files(root, list(args.include), list(args.exclude))
    if args.max_files > 0:
        files = files[: int(args.max_files)]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    counts = Counter()
    file_rows: dict[str, int] = {}
    bad_rows: list[dict[str, Any]] = []
    total = 0
    with out.open("w", encoding="utf-8") as fh_out:
        for path in files:
            rows_for_file = 0
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line_no, line in enumerate(fh, start=1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                            if not isinstance(row, dict):
                                raise ValueError("row_is_not_object")
                            norm = _normalize(path, line_no, row)
                            if not args.keep_raw:
                                norm.pop("raw", None)
                            fh_out.write(json.dumps(norm, ensure_ascii=False, separators=(",", ":")) + "\n")
                            total += 1
                            rows_for_file += 1
                            counts[f"runner:{norm['runner']}"] += 1
                            counts[f"type:{norm['type']}"] += 1
                        except Exception as exc:
                            if len(bad_rows) < 50:
                                bad_rows.append({"file": str(path), "line": line_no, "error": f"{type(exc).__name__}: {exc}"})
            except Exception as exc:
                bad_rows.append({"file": str(path), "line": None, "error": f"{type(exc).__name__}: {exc}"})
            file_rows[str(path)] = rows_for_file

    summary = {
        "output": str(out),
        "files_scanned": len(files),
        "events": total,
        "counts": dict(counts.most_common()),
        "file_rows_top": sorted(file_rows.items(), key=lambda item: item[1], reverse=True)[:50],
        "bad_rows_sample": bad_rows,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
