from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from market.broker_interface import BrokerInterface
from market.broker_types import BrokerHealth, BrokerOrder, BrokerOrderRequest

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds,
        OrderArgs,
        OrderType,
        OpenOrderParams,
        BalanceAllowanceParams,
        AssetType,
        MarketOrderArgs,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
except Exception:  # pragma: no cover
    ClobClient = None
    ApiCreds = None
    OrderArgs = None
    OrderType = None
    OpenOrderParams = None
    BalanceAllowanceParams = None
    AssetType = None
    MarketOrderArgs = None
    BUY = "BUY"
    SELL = "SELL"


class PolymarketBrokerV3(BrokerInterface):
    def __init__(
        self,
        *,
        host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
        private_key: Optional[str] = None,
        funder: Optional[str] = None,
        signature_type: int = 0,
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
    def from_env(cls) -> "PolymarketBrokerV3":
        load_dotenv()
        return cls(
            host=os.getenv("POLY_HOST", "https://clob.polymarket.com"),
            chain_id=int(os.getenv("POLY_CHAIN_ID", "137")),
            private_key=os.getenv("POLY_PRIVATE_KEY") or os.getenv("PRIVATE_KEY"),
            funder=os.getenv("POLY_FUNDER") or os.getenv("FUNDER"),
            signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "0")),
            api_key=os.getenv("POLY_API_KEY"),
            api_secret=os.getenv("POLY_API_SECRET"),
            api_passphrase=os.getenv("POLY_PASSPHRASE"),
        )

    def _require_sdk(self) -> None:
        if ClobClient is None or ApiCreds is None:
            raise RuntimeError("py-clob-client is not installed. Run: pip install py-clob-client")

    def _build_api_creds(self):
        if self.api_key and self.api_secret and self.api_passphrase:
            return ApiCreds(
                api_key=self.api_key,
                api_secret=self.api_secret,
                api_passphrase=self.api_passphrase,
            )
        return None

    def connect(self) -> None:
        self._require_sdk()
        if not self.private_key:
            raise RuntimeError("POLY_PRIVATE_KEY (or PRIVATE_KEY) is required for authenticated trading")

        api_creds = self._build_api_creds()
        kwargs: Dict[str, Any] = {
            "host": self.host,
            "key": self.private_key,
            "chain_id": self.chain_id,
            "signature_type": self.signature_type,
        }
        if self.funder:
            kwargs["funder"] = self.funder
        if api_creds is not None:
            kwargs["creds"] = api_creds

        self._client = ClobClient(**kwargs)
        if api_creds is None:
            self._client.set_api_creds(self._client.create_or_derive_api_creds())

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
            return BrokerHealth(ok=bool(ok), mode=self.mode, host=self.host, message="PolymarketBrokerV3 ready", server_time=server_time)
        except Exception as exc:
            return BrokerHealth(ok=False, mode=self.mode, host=self.host, message=f"healthcheck_failed: {exc}")

    def _map_order(self, raw: Dict[str, Any]) -> BrokerOrder:
        return BrokerOrder(
            order_id=str(raw.get("id") or raw.get("order_id") or raw.get("orderID") or ""),
            token_id=str(raw.get("asset_id") or raw.get("token_id") or raw.get("assetID") or ""),
            side=str(raw.get("side") or ""),
            price=float(raw.get("price") or 0.0),
            original_size=float(raw.get("original_size") or raw.get("size") or 0.0),
            size_matched=float(raw.get("size_matched") or 0.0),
            status=str(raw.get("status") or "unknown"),
            outcome=raw.get("outcome"),
            market=raw.get("market"),
            market_slug=raw.get("market_slug"),
            order_type=raw.get("order_type"),
            raw=raw,
        )

    def get_open_orders(self, token_id: Optional[str] = None) -> List[BrokerOrder]:
        params = OpenOrderParams(asset_id=token_id) if (OpenOrderParams is not None and token_id) else (OpenOrderParams() if OpenOrderParams is not None else None)
        raw_orders = self.client.get_orders(params) if params is not None else self.client.get_orders()
        return [self._map_order(o) for o in raw_orders]

    def get_order(self, order_id: str) -> Optional[BrokerOrder]:
        if hasattr(self.client, "get_order"):
            raw = self.client.get_order(order_id)
            return self._map_order(raw) if raw else None
        for order in self.get_open_orders():
            if order.order_id == order_id:
                return order
        return None

    def get_trades(self, params: Optional[Any] = None) -> List[Dict[str, Any]]:
        if not hasattr(self.client, "get_trades"):
            raise RuntimeError("CLOB client missing get_trades")
        return self.client.get_trades(params)

    def place_limit_order(self, req: BrokerOrderRequest) -> BrokerOrder:
        side = BUY if str(req.side).upper() == "BUY" else SELL
        order_args = OrderArgs(token_id=req.token_id, price=float(req.price), size=float(req.size), side=side)
        signed = self.client.create_order(order_args)
        order_type = getattr(OrderType, str(req.order_type).upper(), OrderType.GTC)
        resp = self.client.post_order(signed, order_type)
        if isinstance(resp, dict):
            order_id = str(resp.get("orderID") or resp.get("order_id") or resp.get("id") or "")
            if order_id:
                return BrokerOrder(
                    order_id=order_id,
                    token_id=req.token_id,
                    side=str(req.side).upper(),
                    price=float(req.price),
                    original_size=float(req.size),
                    size_matched=0.0,
                    status=str(resp.get("status") or "posted").lower(),
                    outcome=req.outcome,
                    market_slug=req.market_slug,
                    order_type=req.order_type,
                    raw=resp,
                )
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

    def place_market_order(
        self,
        *,
        token_id: str,
        side: str,
        amount: float,
        order_type: str = "FAK",
        market_slug: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> BrokerOrder:
        if MarketOrderArgs is None or OrderType is None:
            raise RuntimeError("py-clob-client market order helpers unavailable")
        normalized_side = str(side).upper()
        market_args = MarketOrderArgs(
            token_id=token_id,
            amount=float(amount),
            side=normalized_side,
            order_type=getattr(OrderType, str(order_type).upper(), OrderType.FAK),
        )
        signed = self.client.create_market_order(market_args)
        resp = self.client.post_order(signed, market_args.order_type)
        if isinstance(resp, dict):
            order_id = str(resp.get("orderID") or resp.get("order_id") or resp.get("id") or "")
            if order_id:
                return BrokerOrder(
                    order_id=order_id,
                    token_id=token_id,
                    side=normalized_side,
                    price=float(resp.get("price") or 0.0),
                    original_size=float(resp.get("original_size") or resp.get("size") or amount),
                    size_matched=float(resp.get("size_matched") or 0.0),
                    status=str(resp.get("status") or "posted").lower(),
                    outcome=outcome,
                    market_slug=market_slug,
                    order_type=str(order_type).upper(),
                    raw=resp,
                )
            return self._map_order(resp)
        return BrokerOrder(
            order_id=str(resp),
            token_id=token_id,
            side=normalized_side,
            price=0.0,
            original_size=float(amount),
            status="posted",
            market_slug=market_slug,
            outcome=outcome,
            order_type=str(order_type).upper(),
            raw={"response": resp},
        )

    def cancel_order(self, order_id: str) -> dict:
        return self.client.cancel(order_id)

    def cancel_market_orders(self, market: Optional[str] = None, asset_id: Optional[str] = None) -> dict:
        if hasattr(self.client, "cancel_market_orders"):
            return self.client.cancel_market_orders({"market": market, "asset_id": asset_id})
        if hasattr(self.client, "cancel_all") and market is None and asset_id is None:
            return self.client.cancel_all()
        raise RuntimeError("CLOB client missing market cancel method")

    def get_balance_allowance(self, *, asset_type: str, token_id: Optional[str] = None) -> Dict[str, Any]:
        if BalanceAllowanceParams is None or AssetType is None:
            raise RuntimeError("py-clob-client balance allowance helpers unavailable")
        normalized_type = str(asset_type or "").strip().upper()
        params = BalanceAllowanceParams(
            asset_type=getattr(AssetType, normalized_type),
            token_id=token_id,
        )
        return self.client.get_balance_allowance(params)

    def update_balance_allowance(self, *, asset_type: str, token_id: Optional[str] = None) -> Dict[str, Any]:
        if BalanceAllowanceParams is None or AssetType is None:
            raise RuntimeError("py-clob-client balance allowance helpers unavailable")
        normalized_type = str(asset_type or "").strip().upper()
        params = BalanceAllowanceParams(
            asset_type=getattr(AssetType, normalized_type),
            token_id=token_id,
        )
        return self.client.update_balance_allowance(params)
