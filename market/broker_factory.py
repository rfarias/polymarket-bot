from __future__ import annotations

from market.dryrun_broker import DryRunBroker
from market.polymarket_broker import PolymarketBroker


def build_broker(*, dry_run: bool):
    if dry_run:
        return DryRunBroker()
    return PolymarketBroker.from_env()
