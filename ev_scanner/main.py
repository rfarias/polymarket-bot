"""
ev_scanner/main.py — EV+ Scanner orquestrador

Roda em loop contínuo, acionando cada setup no seu intervalo configurado.
Todos os setups operam em PAPER ONLY — nenhuma ordem real.

Uso:
    python -m ev_scanner.main
    python -m ev_scanner.main --setup weather        # rodar só um setup
    python -m ev_scanner.main --once                 # rodar uma vez e sair
"""
from __future__ import annotations

import argparse
import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ev_scanner.setups import weather, fed_rate, soccer, nba_nfl
from ev_scanner.utils.logger import log_event, read_all_positions
from ev_scanner.utils.resolver import check_and_log_resolutions

_CONFIG_PATH = Path(__file__).parent / "config.json"


def _load_config() -> dict:
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


def _stats_summary(setup_name: str) -> dict:
    positions = read_all_positions(setup_name)
    entries = [p for p in positions if p.get("type") == "simulated_entry"]
    exits   = [p for p in positions if p.get("type") == "simulated_exit"]
    wins    = sum(1 for e in exits if float(e.get("pnl_simulated", 0) or 0) > 0)
    pnl     = sum(float(e.get("pnl_simulated", 0) or 0) for e in exits)
    return {
        "entries": len(entries),
        "resolved": len(exits),
        "wins": wins,
        "wr": round(wins / len(exits), 3) if exits else None,
        "pnl_simulated": round(pnl, 2),
    }


def _run_setup(name: str, module, config: dict, min_volume: float) -> None:
    setup_cfg = {**config, **config.get(name, {})}
    try:
        results  = module.run_scan(setup_cfg, min_volume=min_volume)
        new_res  = check_and_log_resolutions(name)
        stats    = _stats_summary(name)
        res_note = f" (+{new_res} resolved)" if new_res else ""
        print(f"[{name.upper()}] scan done — ev_found={len(results)} | "
              f"entries={stats['entries']} resolved={stats['resolved']} "
              f"wr={stats['wr']} pnl=${stats['pnl_simulated']}{res_note}")
    except Exception as e:
        log_event(name, {"type": "error", "error": str(e), "traceback": traceback.format_exc()})
        print(f"[{name.upper()}] ERROR: {e}")


def run_once(setup_filter: Optional[str] = None) -> None:
    cfg        = _load_config()
    min_volume = float(cfg.get("min_market_volume", 500))

    setups = {
        "weather":  (weather,  cfg.get("weather",  {}).get("active", True)),
        "fed_rate": (fed_rate, cfg.get("fed_rate",  {}).get("active", True)),
        "soccer":   (soccer,   cfg.get("soccer",   {}).get("active", True)),
        "nba_nfl":  (nba_nfl,  cfg.get("nba_nfl",  {}).get("active", True)),
    }

    for name, (module, active) in setups.items():
        if not active:
            continue
        if setup_filter and name != setup_filter:
            continue
        _run_setup(name, module, cfg, min_volume)


def run_loop(setup_filter: Optional[str] = None) -> None:
    cfg        = _load_config()
    min_volume = float(cfg.get("min_market_volume", 500))

    # Track last run time per setup
    last_run: dict[str, float] = {}

    setups = {
        "weather":  (weather,  cfg.get("weather",  {})),
        "fed_rate": (fed_rate, cfg.get("fed_rate",  {})),
        "soccer":   (soccer,   cfg.get("soccer",   {})),
        "nba_nfl":  (nba_nfl,  cfg.get("nba_nfl",  {})),
    }

    print(f"[EV_SCANNER] iniciando loop — {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    log_event("ev_scanner", {"type": "loop_start", "setups": list(setups.keys())})

    while True:
        now = time.time()
        cfg = _load_config()  # reload on each tick for live config changes

        for name, (module, setup_cfg) in setups.items():
            if not setup_cfg.get("active", True):
                continue
            if setup_filter and name != setup_filter:
                continue

            interval_secs = float(setup_cfg.get("check_interval_minutes", 60)) * 60
            last = last_run.get(name, 0)

            if now - last >= interval_secs:
                print(f"\n[EV_SCANNER] running {name} @ {datetime.now(timezone.utc).strftime('%H:%M:%SZ')}")
                merged_cfg = {**cfg, **setup_cfg}
                _run_setup(name, module, merged_cfg, min_volume)
                last_run[name] = time.time()

        time.sleep(60)  # check every minute if any setup is due


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EV+ Scanner — paper only")
    parser.add_argument("--setup", default=None, help="Run only this setup (weather/soccer/fed_rate/nba_nfl)")
    parser.add_argument("--once",  action="store_true", help="Run once and exit")
    args = parser.parse_args()

    if args.once:
        run_once(setup_filter=args.setup)
    else:
        run_loop(setup_filter=args.setup)
