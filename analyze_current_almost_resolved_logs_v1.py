from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    items = sorted(values)
    idx = max(0, min(len(items) - 1, round((len(items) - 1) * q)))
    return items[idx]


def _load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _summarize_windows(snapshot_rows: list[dict], poll_secs: float) -> dict:
    windows: list[dict] = []
    current: dict | None = None

    for row in snapshot_rows:
        signal = row.get("signal") or {}
        ts = _safe_float(row.get("ts"))
        current_secs = row.get("current_secs")
        allow = bool(signal.get("allow"))
        side = str(signal.get("side") or "")
        if allow:
            if current is None or current.get("side") != side:
                current = {
                    "side": side,
                    "start_ts": ts,
                    "end_ts": ts,
                    "start_secs_to_end": current_secs,
                    "end_secs_to_end": current_secs,
                    "max_buffer_bps": _safe_float(
                        signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps")
                    ),
                }
                windows.append(current)
            else:
                current["end_ts"] = ts
                current["end_secs_to_end"] = current_secs
                current["max_buffer_bps"] = max(
                    _safe_float(current.get("max_buffer_bps")),
                    _safe_float(signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps")),
                )
        else:
            current = None

    durations = []
    start_secs = []
    end_secs = []
    buffers = []
    for window in windows:
        duration = max(poll_secs, _safe_float(window["end_ts"]) - _safe_float(window["start_ts"]) + poll_secs)
        durations.append(duration)
        start_secs.append(_safe_float(window.get("start_secs_to_end")))
        end_secs.append(_safe_float(window.get("end_secs_to_end")))
        buffers.append(_safe_float(window.get("max_buffer_bps")))

    return {
        "window_count": len(windows),
        "median_window_secs": median(durations) if durations else None,
        "p10_window_secs": _quantile(durations, 0.10),
        "p90_window_secs": _quantile(durations, 0.90),
        "min_window_secs": min(durations) if durations else None,
        "max_window_secs": max(durations) if durations else None,
        "median_start_secs_to_end": median(start_secs) if start_secs else None,
        "median_end_secs_to_end": median(end_secs) if end_secs else None,
        "median_max_buffer_bps": median(buffers) if buffers else None,
    }


def _summarize_trades(rows: list[dict]) -> dict:
    entries = [row for row in rows if row.get("type") == "enter"]
    exits = [row for row in rows if row.get("type") == "exit"]
    durations = []
    entry_secs = []
    pnls = []

    paired = min(len(entries), len(exits))
    for idx in range(paired):
        entry = entries[idx]
        exit_row = exits[idx]
        durations.append(max(0.0, _safe_float(exit_row.get("ts")) - _safe_float(entry.get("ts"))))
        trade = exit_row.get("trade") or {}
        pnls.append(_safe_float(trade.get("pnl_ticks")))

    for entry in entries:
        signal = entry.get("signal") or {}
        entry_secs.append(_safe_float(signal.get("secs_to_end")))

    return {
        "entries": len(entries),
        "exits": len(exits),
        "median_trade_duration_secs": median(durations) if durations else None,
        "p90_trade_duration_secs": _quantile(durations, 0.90),
        "median_entry_secs_to_end": median(entry_secs) if entry_secs else None,
        "avg_pnl_ticks": round(sum(pnls) / len(pnls), 4) if pnls else None,
        "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4) if pnls else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze current almost-resolved paper logs for manual reaction windows")
    parser.add_argument("log_file", type=str, help="Path to JSONL log from diagnostics_current_almost_resolved_paper_v1.py")
    parser.add_argument("--poll-secs", type=float, default=2.0, help="Polling interval used during collection")
    args = parser.parse_args()

    path = Path(args.log_file)
    rows = _load_rows(path)
    snapshot_rows = [row for row in rows if row.get("type") == "snapshot"]

    print(
        json.dumps(
            {
                "log_file": str(path),
                "snapshot_count": len(snapshot_rows),
                "window_summary": _summarize_windows(snapshot_rows, max(0.1, float(args.poll_secs))),
                "trade_summary": _summarize_trades(rows),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
