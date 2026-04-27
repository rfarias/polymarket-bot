from __future__ import annotations

import argparse
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Directional shadow runner wrapper")
    parser.add_argument("--seconds", type=int, default=300, help="Run duration")
    parser.add_argument("--poll-secs", type=float, default=1.0, help="Polling interval")
    parser.add_argument("--log-dir", type=str, default=None, help="Optional session log directory")
    args = parser.parse_args()

    argv = [sys.executable, "diagnostics_directional_shadow_runner_v1.py", "--seconds", str(args.seconds), "--poll-secs", str(args.poll_secs)]
    if args.log_dir:
        argv.extend(["--log-dir", args.log_dir])
    return int(subprocess.call(argv))


if __name__ == "__main__":
    raise SystemExit(main())
