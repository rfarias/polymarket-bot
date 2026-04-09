from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from market.broker_interface import BrokerInterface
from market.broker_types import BrokerHealth, BrokerOrder, BrokerOrderRequest

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, OpenOrderParams
    from py_clob_client.order_builder.constants import BUY, SELL
except Exception:  # pragma: no cover - handled at runtime in user env
    ClobClient = None
    OrderArgs = None
    OrderType = None
    OpenOrderParams = None
    BUY = "BUY"
    SELL = "SELL"


class PolymarketBroker(BrokerInterface):
    def __init__(
        self,
        *,
        host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
        private_key: Optional[str] = None,
        funder: Optional[str] = None,
        signature_type: int = 1,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_passphrase: Optional[str] = None,
    ):
        self.mode = "real"
        self.host = host
        self.chain_id = int(chain_id)
        self.private_key = private_key
        self.funder = funder
        self.signature_type = int(signature_type)
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self._client = None

    @classmethod
    def from_env(cls) -> "PolymarketBroker":
        return cls(
            host=os.getenv("POLY_HOST", "https://clob.polymarket.com"),
            chain_id=int(os.getenv("POLY_CHAIN_ID", "137")),
            private_key=os.getenv("POLY_PRIVATE_KEY") or os.getenv("PRIVATE_KEY"),
            funder=os.getenv("POLY_FUNDER") or os.getenv("FUNDER"),
            signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "1")),
            api_key=os.getenv("POLY_API_KEY"),
            api_secret=os.getenv("POLY_API_SECRET"),
            api_passphrase=os.getenv("POLY_PASSPHRASE"),
        )

    def _require_sdk(self) -> None:
        if ClobClient is None:
            raise RuntimeError("py-clob-client is not installed. Run: pip install py-clob-client")

    def connect(self) -> None:
        self._require_sdk()
        if not self.private_key:
            raise RuntimeError("POLY_PRIVATE_KEY (or PRIVATE_KEY) is required for authenticated trading")

        kwargs: Dict[str, Any] = {
            "host": self.host,
            "key": self.private_key,
            "chain_id": self.chain_id,
            "signature_type": self.signature_type,
        }
        if self.funder:
            kwargs["funder"] = self.funder

        # Keep positional compatibility risk low by calling with explicit names.
        self._client = ClobClient(**kwargs)

        if self.api_key and self.api_secret and self.api_passphrase:
            self._client.set_api_creds(
                {
                    "api_key": self.api_key,
                    "secret": self.api_secret,
                    "passphrase": self.api_passphrase,
                }
            )
        else:
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)

    @property
    def client(self):
        if self._client is None:
            self.connect()
        return self._client

    def healthcheck(self) -> BrokerHealth:
        try:
            ok = self.client.get_ok()
            server_time = None
            if hasattr(self.client, "get_server_time"):
                server_time = self.client.get_server_time()
            return BrokerHealth(ok=bool(ok), mode=self.mode, host=self.host, message="PolymarketBroker ready", server_time=server_time)
        except Exception as exc:
            return BrokerHealth(ok=False, mode=self.mode, host=self.host, message=f"healthcheck_failed: {exc}")

    def _map_order(self, raw: Dict[str, Any]) -> BrokerOrder:
        return BrokerOrder(
            order_id=str(raw.get("id") or raw.get("order_id") or ""),
            token_id=str(raw.get("asset_id") or raw.get("token_id") or ""),
            side=str(raw.get("side") or ""),
            price=float(raw.get("price") or 0.0),
            original_size=float(raw.get("original_size") or raw.get("size") or 0.0),
            size_matched=float(raw.get("size_matched") or 0.0),
            status=str(raw.get("status") or "unknown"),
            outcome=raw.get("outcome"),
            market=raw.get("market"),
            order_type=raw.get("order_type"),
            raw=raw,
        )

    def get_open_orders(self, token_id: Optional[str] = None) -> List[BrokerOrder]:
        params = OpenOrderParams(asset_id=token_id) if (OpenOrderParams is not None and token_id) else (OpenOrderParams() if OpenOrderParams is not None else None)
        if hasattr(self.client, "get_orders"):
            raw_orders = self.client.get_orders(params) if params is not None else self.client.get_orders()
        elif hasattr(self.client, "get_open_orders"):
            raw_orders = self.client.get_open_orders({"asset_id": token_id} if token_id else None)
        else:
            raise RuntimeError("CLOB client missing open-order method")
        return [self._map_order(o) for o in raw_orders]

    def get_order(self, order_id: str) -> Optional[BrokerOrder]:
        if hasattr(self.client, "get_order"):
            raw = self.client.get_order(order_id)
            return self._map_order(raw) if raw else None
        for order in self.get_open_orders():
            if order.order_id == order_id:
                return order
        return None

    def place_limit_order(self, req: BrokerOrderRequest) -> BrokerOrder:
        side = BUY if str(req.side).upper() == "BUY" else SELL
        order_args = OrderArgs(token_id=req.token_id, price=float(req.price), size=float(req.size), side=side)
        signed = self.client.create_order(order_args)
        order_type = getattr(OrderType, str(req.order_type).upper(), OrderType.GTC)
        resp = self.client.post_order(signed, order_type)
        if isinstance(resp, dict):
            return self._map_order(resp)
        return BrokerOrder(
            order_id=str(resp),
            token_id=req.token_id,
            side=str(req.side).upper(),
            price=float(req.price),
            original_size=float(req.size),
            status="posted",
            market_slug=req.market_slug,
            outcome=req.outcome,
            order_type=req.order_type,
            raw={"response": resp},
        )

    def cancel_order(self, order_id: str) -> dict:
        if hasattr(self.client, "cancel"):
            return self.client.cancel(order_id)
        if hasattr(self.client, "cancel_order"):
            return self.client.cancel_order(order_id)
        raise RuntimeError("CLOB client missing cancel method")

    def cancel_market_orders(self, market: Optional[str] = None, asset_id: Optional[str] = None) -> dict:
        if hasattr(self.client, "cancel_market_orders"):
            return self.client.cancel_market_orders({"market": market, "asset_id": asset_id})
        if hasattr(self.client, "cancel_all") and market is None and asset_id is None:
            return self.client.cancel_all()
        raise RuntimeError("CLOB client missing market cancel method")
