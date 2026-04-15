from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List


ROOT = Path(__file__).resolve().parent


@dataclass
class SuiteCaseResult:
    name: str
    command: List[str]
    ok: bool
    returncode: int
    duration_seconds: float
    stdout: str
    stderr: str

    def as_dict(self):
        return asdict(self)


CASES = [
    {
        "name": "broker_reconcile_v1",
        "command": [sys.executable, "diagnostics_broker_reconcile_v1.py"],
    },
    {
        "name": "broker_status_sync_v1",
        "command": [sys.executable, "diagnostics_broker_status_sync_v1.py"],
    },
    {
        "name": "broker_status_sync_v2",
        "command": [sys.executable, "diagnostics_broker_status_sync_v2.py"],
    },
    {
        "name": "balanced_hedge_hold_v1",
        "command": [sys.executable, "diagnostics_balanced_hedge_hold_v1.py"],
    },
]


def run_case(name: str, command: List[str]) -> SuiteCaseResult:
    started = time.perf_counter()
    proc = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    duration = time.perf_counter() - started
    return SuiteCaseResult(
        name=name,
        command=command,
        ok=(proc.returncode == 0),
        returncode=proc.returncode,
        duration_seconds=round(duration, 3),
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def print_case_result(result: SuiteCaseResult) -> None:
    status = "PASS" if result.ok else "FAIL"
    print(f"\n===== CASE {result.name} | {status} | {result.duration_seconds}s =====")
    print(f"[COMMAND] {' '.join(result.command)}")
    if result.stdout:
        print("[STDOUT]")
        print(result.stdout.rstrip())
    else:
        print("[STDOUT] <empty>")
    if result.stderr:
        print("[STDERR]")
        print(result.stderr.rstrip())


def print_summary(results: List[SuiteCaseResult]) -> None:
    passed = sum(1 for r in results if r.ok)
    failed = len(results) - passed
    print("\n===== REGRESSION SUITE SUMMARY =====")
    print(f"TOTAL={len(results)} PASS={passed} FAIL={failed}")
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        print(f"- {r.name}: {status} | rc={r.returncode} | {r.duration_seconds}s")


def main() -> int:
    print("[TEST] Starting diagnostics regression suite v1...")
    results: List[SuiteCaseResult] = []
    for case in CASES:
        result = run_case(case["name"], case["command"])
        results.append(result)
        print_case_result(result)

    print_summary(results)
    if any(not r.ok for r in results):
        print("[RESULT] Regression suite finished with failures")
        return 1

    print("[RESULT] Regression suite finished successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
