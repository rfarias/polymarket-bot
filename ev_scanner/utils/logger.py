from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOGS_DIR = Path(__file__).parent.parent / "logs"
_DECISIONS_LOG = _LOGS_DIR / "ev_scanner_decisions.jsonl"


def _ensure_logs_dir() -> None:
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _setup_log_path(setup_name: str) -> Path:
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _LOGS_DIR / f"{setup_name}_paper_{date_str}.jsonl"


def log_event(setup_name: str, event: dict[str, Any]) -> None:
    _ensure_logs_dir()
    now = time.time()
    row = {
        "ts": now,
        "ts_human": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "setup": setup_name,
        **event,
    }
    line = json.dumps(row, ensure_ascii=False, default=str)
    with open(_setup_log_path(setup_name), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    with open(_DECISIONS_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_open_positions(setup_name: str) -> list[dict]:
    """Return simulated entries that don't yet have a matching simulated_exit."""
    path = _setup_log_path(setup_name)
    if not path.exists():
        # Check previous day too
        yesterday = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = _LOGS_DIR / f"{setup_name}_paper_{yesterday}.jsonl"
    if not path.exists():
        return []
    entries: dict[str, dict] = {}
    exits: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            key = row.get("market_slug", "") + "|" + str(row.get("bucket") or row.get("entry_side") or "")
            if row.get("type") == "simulated_entry":
                entries[key] = row
            elif row.get("type") == "simulated_exit":
                exits.add(key)
    except Exception:
        pass
    return [v for k, v in entries.items() if k not in exits]


def read_all_positions(setup_name: str, days_back: int = 30) -> list[dict]:
    """Return entries, exits and price updates across all log files for this setup."""
    rows = []
    for f in sorted(_LOGS_DIR.glob(f"{setup_name}_paper_*.jsonl")):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row.get("type") in ("simulated_entry", "simulated_exit", "price_update"):
                    rows.append(row)
        except Exception:
            pass
    return rows


def read_open_position_keys(setup_name: str) -> set[str]:
    """Return slug|bucket (or slug|outcome) keys with open simulated positions."""
    entries: set[str] = set()
    exits: set[str] = set()
    for row in read_all_positions(setup_name):
        slug      = row.get("market_slug", "")
        secondary = str(row.get("bucket") or row.get("outcome") or "")
        key = f"{slug}|{secondary}"
        if row.get("type") == "simulated_entry":
            entries.add(key)
        elif row.get("type") == "simulated_exit":
            exits.add(key)
    return entries - exits
