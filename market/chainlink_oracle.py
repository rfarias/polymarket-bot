"""
Leitura do preço BTC/USD do oráculo Chainlink no Polygon Mainnet.
Usa JSON-RPC puro sobre HTTP — não depende de web3.py, só de requests.

Chainlink BTC/USD Polygon Mainnet: 0xc907E116054Ad103354f2D350FD2514433D57F6F
latestRoundData() selector:         0xfeaf968c
Retorno ABI: (roundId uint80, answer int256, startedAt uint256,
              updatedAt uint256, answeredInRound uint80)
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

import requests

# Chainlink BTC/USD proxy aggregator no Polygon Mainnet
DEFAULT_CONTRACT = "0xc907E116054Ad103354f2D350FD2514433D57F6F"
# keccak256("latestRoundData()")[:4]
_SELECTOR = "0xfeaf968c"
_DECIMALS = 8

# RPCs públicas do Polygon — usadas em fallback sequencial
DEFAULT_RPC_URLS: list[str] = [
    "https://1rpc.io/matic",
    "https://polygon.drpc.org",
]


def _decode_response(hex_result: str) -> Tuple[float, float]:
    """
    Decodifica a resposta ABI de latestRoundData().
    Cada slot tem 32 bytes big-endian.
    Layout: roundId | answer (int256) | startedAt | updatedAt | answeredInRound
    """
    raw = bytes.fromhex(hex_result.removeprefix("0x"))
    if len(raw) < 160:
        raise ValueError(f"Resposta muito curta: {len(raw)} bytes")
    answer = int.from_bytes(raw[32:64], "big", signed=True)
    updated_at = int.from_bytes(raw[96:128], "big")
    price_usd = float(answer) / (10 ** _DECIMALS)
    return price_usd, float(updated_at)


class ChainlinkBTCOracle:
    """
    Leitor leve do oráculo Chainlink — sem web3.py.
    Cache interno evita chamadas repetidas dentro do intervalo configurado.
    """

    def __init__(
        self,
        rpc_urls: Optional[list[str]] = None,
        contract: str = DEFAULT_CONTRACT,
        cache_secs: float = 5.0,
        timeout_secs: float = 3.0,
    ) -> None:
        self._rpc_urls = rpc_urls or DEFAULT_RPC_URLS
        self._contract = contract
        self._cache_secs = cache_secs
        self._timeout = timeout_secs

        self._last_price: Optional[float] = None
        self._last_updated_at: float = 0.0
        self._last_query_ts: float = 0.0
        self._consecutive_errors: int = 0

    def _eth_call(self, rpc_url: str) -> Tuple[float, float]:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {"to": self._contract, "data": _SELECTOR},
                "latest",
            ],
            "id": 1,
        }
        resp = requests.post(rpc_url, json=payload, timeout=self._timeout)
        resp.raise_for_status()
        result = resp.json().get("result", "")
        if not result or result == "0x":
            raise ValueError("eth_call retornou resultado vazio")
        return _decode_response(result)

    def get_price(self, force: bool = False) -> Tuple[Optional[float], float, float]:
        """
        Retorna (price_usd, oracle_updated_at, staleness_secs).
        Usa cache se estiver fresco. Em caso de erro, retorna o cache anterior.
        """
        now = time.time()
        if (
            not force
            and self._last_price is not None
            and (now - self._last_query_ts) < self._cache_secs
        ):
            return self._last_price, self._last_updated_at, now - self._last_updated_at

        for rpc_url in self._rpc_urls:
            try:
                price, updated_at = self._eth_call(rpc_url)
                self._last_price = price
                self._last_updated_at = updated_at
                self._last_query_ts = now
                self._consecutive_errors = 0
                return price, updated_at, now - updated_at
            except Exception:
                continue

        self._consecutive_errors += 1
        staleness = (now - self._last_updated_at) if self._last_updated_at > 0 else float("inf")
        return self._last_price, self._last_updated_at, staleness

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors
