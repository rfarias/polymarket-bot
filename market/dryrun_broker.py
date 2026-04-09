from __future__ import annotations

from typing import Dict, List, Optional
import uuid

from market.broker_interface import BrokerInterface
from market.broker_types import BrokerHealth, BrokerOrder, BrokerOrderRequest


class DryRunBroker(BrokerInterface):
    def __init__(self, host: str = "dry-run://local"):
        self.mode = "dry_run"
        self.host = host
        self._orders: Dict[str, BrokerOrder] = {}

    def healthcheck(self) -> BrokerHealth:
        return BrokerHealth(ok=True, mode=self.mode, host=self.host, message="DryRunBroker ready")

    def get_open_orders(self, token_id: Optional[str] = None) -> List[BrokerOrder]:
        orders = [o for o in self._orders.values() if o.status in ("open", "partial")]
        if token_id:
            orders = [o for o in orders if o.token_id == token_id]
        return list(orders)

    def get_order(self, order_id: str) -> Optional[BrokerOrder]:
        return self._orders.get(order_id)

    def place_limit_order(self, req: BrokerOrderRequest) -> BrokerOrder:
        order_id = str(uuid.uuid4())
        order = BrokerOrder(
            order_id=order_id,
            token_id=req.token_id,
            side=req.side,
            price=float(req.price),
            original_size=float(req.size),
            size_matched=0.0,
            status="open",
            outcome=req.outcome,
            market_slug=req.market_slug,
            order_type=req.order_type,
            raw={"client_order_key": req.client_order_key, "mode": self.mode},
        )
        self._orders[order_id] = order
        return order

    def cancel_order(self, order_id: str) -> dict:
        order = self._orders.get(order_id)
        if not order:
            return {"canceled": [], "not_canceled": {order_id: "not_found"}}
        if order.status in ("filled", "canceled"):
            return {"canceled": [], "not_canceled": {order_id: f"already_{order.status}"}}
        order.status = "canceled"
        return {"canceled": [order_id], "not_canceled": {}}

    def cancel_market_orders(self, market: Optional[str] = None, asset_id: Optional[str] = None) -> dict:
        canceled = []
        for order in self._orders.values():
            if order.status not in ("open", "partial"):
                continue
            if market and order.market != market:
                continue
            if asset_id and order.token_id != asset_id:
                continue
            order.status = "canceled"
            canceled.append(order.order_id)
        return {"canceled": canceled, "not_canceled": {}}
