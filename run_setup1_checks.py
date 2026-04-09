import argparse
import asyncio
import os
import platform
import sys
import time
import traceback
from pathlib import Path

from diagnostics_setup1_dryrun_executor import main as run_deterministic_checks
from market.ws_5m_dryrun_live_v2 import monitor_setup1_dryrun_live_v2


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


def _build_default_log_path() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"setup1_checks_{ts}.log"


def _print_header(args, log_path: Path):
    print("[CONFIG] Setup1 unified checks runner")
    print(f"[CONFIG] Python={sys.version.split()[0]}")
    print(f"[CONFIG] Platform={platform.platform()}")
    print(f"[CONFIG] WorkingDir={os.getcwd()}")
    print(f"[CONFIG] LogFile={log_path}")
    print(f"[CONFIG] RunDeterministic={not args.skip_deterministic}")
    print(f"[CONFIG] RunLive={not args.skip_live}")
    print(f"[CONFIG] LiveSeconds={args.live_seconds}")


def _run_deterministic_stage():
    print("\n" + "=" * 80)
    print("[STAGE] Deterministic dry-run executor diagnostics")
    print("=" * 80)
    run_deterministic_checks()
    print("[STAGE] Deterministic diagnostics finished")


async def _run_live_stage(live_seconds: int):
    print("\n" + "=" * 80)
    print("[STAGE] Live setup1 dry-run monitor v2")
    print("=" * 80)
    await monitor_setup1_dryrun_live_v2(duration_seconds=live_seconds)
    print("[STAGE] Live dry-run monitor finished")


def main():
    parser = argparse.ArgumentParser(description="Run Setup1 checks and save a unified log")
    parser.add_argument("--live-seconds", type=int, default=60, help="Duration for live dry-run monitor")
    parser.add_argument("--skip-deterministic", action="store_true", help="Skip deterministic diagnostics")
    parser.add_argument("--skip-live", action="store_true", help="Skip live dry-run monitor")
    parser.add_argument("--log-file", type=str, default=None, help="Optional custom log file path")
    args = parser.parse_args()

    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("w", encoding="utf-8") as fh:
        tee = Tee(sys.stdout, fh)
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = tee
        sys.stderr = tee
        exit_code = 0
        try:
            _print_header(args, log_path)
            if not args.skip_deterministic:
                _run_deterministic_stage()
            if not args.skip_live:
                asyncio.run(_run_live_stage(args.live_seconds))
            print("\n[RESULT] Setup1 unified checks finished successfully")
        except Exception as exc:
            exit_code = 1
            print("\n[ERROR] Unified checks failed")
            print(f"[ERROR] {type(exc).__name__}: {exc}")
            print(traceback.format_exc())
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    print(f"[DONE] Log saved to: {log_path}")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
