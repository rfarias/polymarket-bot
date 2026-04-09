from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, Optional


@dataclass
class BrokerOrderRequest:
    token_id: str
    side: str  # BUY or SELL
    price: float
    size: float
    order_type: str = "GTC"
    market_slug: Optional[str] = None
    outcome: Optional[str] = None
    client_order_key: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BrokerOrder:
    order_id: str
    token_id: str
    side: str
    price: float
    original_size: float
    size_matched: float = 0.0
    status: str = "open"
    outcome: Optional[str] = None
    market: Optional[str] = None
    market_slug: Optional[str] = None
    order_type: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def remaining_size(self) -> float:
        return max(0.0, float(self.original_size) - float(self.size_matched))

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["remaining_size"] = self.remaining_size
        return payload


@dataclass
class BrokerHealth:
    ok: bool
    mode: str
    host: str
    message: str
    server_time: Optional[Any] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)
