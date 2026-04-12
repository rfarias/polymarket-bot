from __future__ import annotations

from typing import Any, Dict, List, Tuple

from market.broker_reconciliation_v1 import reconcile_executor_with_broker_open_orders


SAFE_EXTERNAL_STATUSES = {"filled", "canceled", "cancelled", "closed", "resolved"}


def evaluate_startup_guard(executor, broker_open_orders: List[Any]) -> Tuple[bool, Dict[str, Any]]:
    reconcile = reconcile_executor_with_broker_open_orders(executor, broker_open_orders)

    blocking_external = [
        row for row in (reconcile.get("external") or [])
        if str(row.get("status") or "").lower() not in SAFE_EXTERNAL_STATUSES
    ]
    blocking_unknown = [
        row for row in (reconcile.get("unknown_client_key") or [])
        if str(row.get("status") or "").lower() not in SAFE_EXTERNAL_STATUSES
    ]

    allowed = len(blocking_external) == 0 and len(blocking_unknown) == 0
    report = {
        "allowed": allowed,
        "tracked_count": reconcile.get("tracked_count", 0),
        "external_count": reconcile.get("external_count", 0),
        "unknown_client_key_count": reconcile.get("unknown_client_key_count", 0),
        "blocking_external": blocking_external,
        "blocking_unknown_client_key": blocking_unknown,
        "tracked_plan_ids": reconcile.get("tracked_plan_ids", []),
        "tracked_order_ids": reconcile.get("tracked_order_ids", []),
        "reconcile": reconcile,
    }
    return allowed, report
