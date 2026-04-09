from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from market.broker_types import BrokerHealth, BrokerOrder, BrokerOrderRequest


class BrokerInterface(ABC):
    mode: str

    @abstractmethod
    def healthcheck(self) -> BrokerHealth:
        raise NotImplementedError

    @abstractmethod
    def get_open_orders(self, token_id: Optional[str] = None) -> List[BrokerOrder]:
        raise NotImplementedError

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[BrokerOrder]:
        raise NotImplementedError

    @abstractmethod
    def place_limit_order(self, req: BrokerOrderRequest) -> BrokerOrder:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def cancel_market_orders(self, market: Optional[str] = None, asset_id: Optional[str] = None) -> dict:
        raise NotImplementedError
