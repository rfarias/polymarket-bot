from __future__ import annotations

import re
from typing import Any, Dict, Optional

from market.setup1_dryrun_executor import Setup1DryRunExecutor


class Setup1DryRunExecutorV2(Setup1DryRunExecutor):
    def _normalize_reason(self, reason: str) -> str:
        normalized = reason
        normalized = re.sub(r"secs_to_end\s+\d+\s+<\s+360", "secs_to_end_below_min_next1", normalized)
        normalized = re.sub(r"secs_to_end\s+\d+\s+<\s+600", "secs_to_end_below_min_next2", normalized)
        normalized = re.sub(r"exit_gap_total\s+[0-9.]+\s+>\s+0.03", "exit_gap_total_above_max", normalized)
        normalized = re.sub(r"sum_bids\s+[0-9.]+\s+<\s+0.99", "sum_bids_below_min", normalized)
        return normalized

    def _decision_key(self, decision: str, reason: str, details: Optional[Dict[str, Any]]) -> str:
        normalized = None
        if details:
            normalized = {
                "slot_name": details.get("slot_name"),
                "sum_asks": details.get("sum_asks"),
                "sum_bids": details.get("sum_bids"),
                "exit_gap_total": details.get("exit_gap_total"),
            }
        normalized_reason = self._normalize_reason(reason)
        return f"{decision}|{normalized_reason}|{normalized}"
