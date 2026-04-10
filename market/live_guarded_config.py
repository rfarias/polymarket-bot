from __future__ import annotations

from dataclasses import dataclass, asdict
import os
from typing import Dict

from dotenv import load_dotenv


@dataclass
class LiveGuardedConfig:
    enabled: bool
    shadow_only: bool
    real_posts_enabled: bool
    max_active_plans: int
    allow_next_2: bool
    min_shares_per_leg: int
    deadline_trigger_secs: int
    require_signal: str
    run_seconds: int

    def as_dict(self) -> Dict:
        return asdict(self)


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def load_live_guarded_config() -> LiveGuardedConfig:
    load_dotenv()
    return LiveGuardedConfig(
        enabled=_as_bool("POLY_GUARDED_ENABLED", False),
        shadow_only=_as_bool("POLY_GUARDED_SHADOW_ONLY", True),
        real_posts_enabled=_as_bool("POLY_GUARDED_REAL_POSTS_ENABLED", False),
        max_active_plans=int(os.getenv("POLY_GUARDED_MAX_ACTIVE_PLANS", "1")),
        allow_next_2=_as_bool("POLY_GUARDED_ALLOW_NEXT_2", False),
        min_shares_per_leg=int(os.getenv("POLY_GUARDED_MIN_SHARES", "5")),
        deadline_trigger_secs=int(os.getenv("POLY_GUARDED_DEADLINE_TRIGGER_SECS", "330")),
        require_signal=str(os.getenv("POLY_GUARDED_REQUIRE_SIGNAL", "armed")).strip().lower(),
        run_seconds=int(os.getenv("POLY_GUARDED_RUN_SECONDS", "60")),
    )
