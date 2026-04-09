from __future__ import annotations

from dataclasses import dataclass, asdict
import os
from typing import Dict, List, Optional

from dotenv import load_dotenv


@dataclass
class BrokerEnvStatus:
    mode: str
    host: str
    chain_id: int
    signature_type: int
    has_private_key: bool
    has_funder: bool
    has_api_key: bool
    has_api_secret: bool
    has_api_passphrase: bool
    missing_required: List[str]
    warnings: List[str]

    @property
    def ready_for_real_smoke(self) -> bool:
        return len(self.missing_required) == 0

    def as_dict(self) -> Dict:
        payload = asdict(self)
        payload["ready_for_real_smoke"] = self.ready_for_real_smoke
        return payload


def load_broker_env() -> BrokerEnvStatus:
    load_dotenv()

    host = os.getenv("POLY_HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("POLY_CHAIN_ID", "137"))
    signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))
    private_key = os.getenv("POLY_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
    funder = os.getenv("POLY_FUNDER") or os.getenv("FUNDER")
    api_key = os.getenv("POLY_API_KEY")
    api_secret = os.getenv("POLY_API_SECRET")
    api_passphrase = os.getenv("POLY_PASSPHRASE")
    mode = os.getenv("POLY_MODE", "real").lower()

    missing: List[str] = []
    warnings: List[str] = []

    if mode not in ("real", "dry_run"):
        warnings.append(f"Unknown POLY_MODE={mode}; expected real or dry_run")

    if not private_key:
        missing.append("POLY_PRIVATE_KEY")
    if not api_key:
        missing.append("POLY_API_KEY")
    if not api_secret:
        missing.append("POLY_API_SECRET")
    if not api_passphrase:
        missing.append("POLY_PASSPHRASE")

    if signature_type not in (0, 1, 2):
        warnings.append(f"Unexpected POLY_SIGNATURE_TYPE={signature_type}; expected 0, 1, or 2")

    if signature_type in (1, 2) and not funder:
        warnings.append("POLY_FUNDER is usually required for proxy/email/browser-wallet style accounts")

    return BrokerEnvStatus(
        mode=mode,
        host=host,
        chain_id=chain_id,
        signature_type=signature_type,
        has_private_key=bool(private_key),
        has_funder=bool(funder),
        has_api_key=bool(api_key),
        has_api_secret=bool(api_secret),
        has_api_passphrase=bool(api_passphrase),
        missing_required=missing,
        warnings=warnings,
    )
