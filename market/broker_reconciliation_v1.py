from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


CLIENT_KEY_FIELDS = (
    "client_order_key",
    "clientOrderKey",
    "client_order_id",
    "clientOrderId",
    "client_id",
    "clientId",
)


def _extract_client_key_from_obj(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        for key in CLIENT_KEY_FIELDS:
            value = obj.get(key)
            if value:
                return str(value)
        for value in obj.values():
            found = _extract_client_key_from_obj(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _extract_client_key_from_obj(item)
            if found:
                return found
    return None


def extract_client_order_key_from_order(order) -> Optional[str]:
    raw = getattr(order, "raw", None)
    if raw:
        found = _extract_client_key_from_obj(raw)
        if found:
            return found
    for attr in CLIENT_KEY_FIELDS:
        value = getattr(order, attr, None)
        if value:
            return str(value)
    return None


def build_known_order_index(executor) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    by_order_id: Dict[str, Dict[str, str]] = {}
    by_client_key: Dict[str, Dict[str, str]] = {}
    for plan_id, legs in (executor.plan_broker_orders or {}).items():
        for leg, payload in (legs or {}).items():
            order = (payload or {}).get("order") or {}
            order_id = order.get("order_id")
            if order_id:
                by_order_id[str(order_id)] = {"plan_id": plan_id, "leg": leg}
            request = (payload or {}).get("request") or {}
            client_key = request.get("client_order_key")
            if client_key:
                by_client_key[str(client_key)] = {"plan_id": plan_id, "leg": leg}
    return by_order_id, by_client_key


def reconcile_executor_with_broker_open_orders(executor, broker_open_orders: List[Any]) -> Dict[str, Any]:
    by_order_id, by_client_key = build_known_order_index(executor)

    tracked: List[Dict[str, Any]] = []
    external: List[Dict[str, Any]] = []
    unknown_client_key: List[Dict[str, Any]] = []

    for order in broker_open_orders or []:
        order_id = str(getattr(order, "order_id", "") or "")
        token_id = str(getattr(order, "token_id", "") or "")
        status = str(getattr(order, "status", "") or "")
        side = str(getattr(order, "side", "") or "")
        price = float(getattr(order, "price", 0.0) or 0.0)
        remaining = float(getattr(order, "remaining_size", 0.0) or 0.0)
        client_key = extract_client_order_key_from_order(order)

        matched = None
        if order_id and order_id in by_order_id:
            matched = by_order_id[order_id]
        elif client_key and client_key in by_client_key:
            matched = by_client_key[client_key]

        record = {
            "order_id": order_id,
            "token_id": token_id,
            "side": side,
            "price": price,
            "status": status,
            "remaining_size": remaining,
            "client_order_key": client_key,
        }

        if matched:
            record.update(matched)
            tracked.append(record)
            plan_id = matched["plan_id"]
            leg = matched["leg"]
            payload = (executor.plan_broker_orders.get(plan_id) or {}).get(leg)
            if payload is not None:
                payload["mode"] = getattr(executor.broker, "mode", payload.get("mode"))
                payload["order"] = order.as_dict()
        else:
            if client_key:
                unknown_client_key.append(record)
            external.append(record)

    tracked_plan_ids = sorted({row["plan_id"] for row in tracked})
    tracked_order_ids = sorted({row["order_id"] for row in tracked if row.get("order_id")})

    return {
        "tracked_count": len(tracked),
        "external_count": len(external),
        "unknown_client_key_count": len(unknown_client_key),
        "tracked_plan_ids": tracked_plan_ids,
        "tracked_order_ids": tracked_order_ids,
        "tracked": tracked,
        "external": external,
        "unknown_client_key": unknown_client_key,
    }
