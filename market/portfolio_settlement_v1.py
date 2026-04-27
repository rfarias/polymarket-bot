from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import requests
from dotenv import load_dotenv
from eth_abi import encode
from eth_utils import keccak, to_checksum_address
from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.models import OperationType, SafeTransaction
from py_builder_signing_sdk.config import BuilderConfig
from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds


DATA_API_POSITIONS_URL = "https://data-api.polymarket.com/positions"
DEFAULT_RELAYER_URL = "https://relayer-v2.polymarket.com"
DEFAULT_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
DEFAULT_COLLATERAL_TOKEN = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
ZERO_BYTES32 = "0x" + ("00" * 32)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_log_dir() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"portfolio_settlement_{ts}"


def _bytes32(value: str) -> bytes:
    raw = str(value or "").strip()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 64:
        raise ValueError(f"invalid bytes32 value: {value}")
    return bytes.fromhex(raw)


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def _encode_call(signature: str, arg_types: list[str], arg_values: list[Any]) -> str:
    return "0x" + (_selector(signature) + encode(arg_types, arg_values)).hex()


def _resolve_builder_creds() -> tuple[Optional[BuilderApiKeyCreds], dict]:
    key = os.getenv("POLY_BUILDER_API_KEY") or os.getenv("POLY_API_KEY")
    secret = os.getenv("POLY_BUILDER_API_SECRET") or os.getenv("POLY_API_SECRET")
    passphrase = os.getenv("POLY_BUILDER_PASSPHRASE") or os.getenv("POLY_PASSPHRASE")
    source = "builder_env"
    if not os.getenv("POLY_BUILDER_API_KEY") and os.getenv("POLY_API_KEY"):
        source = "clob_fallback"
    if not (key and secret and passphrase):
        return None, {"configured": False, "source": None}
    return BuilderApiKeyCreds(key=key, secret=secret, passphrase=passphrase), {"configured": True, "source": source}


def _build_relayer() -> tuple[RelayClient, dict]:
    creds, meta = _resolve_builder_creds()
    if creds is None:
        raise RuntimeError("Missing builder/relayer credentials. Configure POLY_BUILDER_API_* or equivalent.")
    config = BuilderConfig(local_builder_creds=creds)
    client = RelayClient(
        relayer_url=os.getenv("POLY_PORTFOLIO_SETTLEMENT_RELAYER_URL", DEFAULT_RELAYER_URL),
        chain_id=_env_int("POLY_CHAIN_ID", 137),
        private_key=os.getenv("POLY_PRIVATE_KEY") or None,
        builder_config=config,
    )
    return client, meta


def _positions_user_address() -> str:
    addr = (
        os.getenv("POLY_PORTFOLIO_SETTLEMENT_USER_ADDRESS")
        or os.getenv("POLY_FUNDER")
        or os.getenv("FUNDER")
        or ""
    ).strip()
    if not addr:
        raise RuntimeError("Missing settlement user address. Configure POLY_PORTFOLIO_SETTLEMENT_USER_ADDRESS or POLY_FUNDER.")
    return addr


def _fetch_positions(*, user: str, mergeable: Optional[bool] = None, redeemable: Optional[bool] = None) -> list[dict]:
    params: dict[str, Any] = {
        "user": user,
        "sizeThreshold": 0,
        "limit": 500,
        "offset": 0,
    }
    if mergeable is not None:
        params["mergeable"] = str(bool(mergeable)).lower()
    if redeemable is not None:
        params["redeemable"] = str(bool(redeemable)).lower()
    response = requests.get(DATA_API_POSITIONS_URL, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _position_field(position: dict, *names: str, default=None):
    for name in names:
        if name in position and position.get(name) is not None:
            return position.get(name)
    return default


def _position_size(position: dict) -> float:
    return _safe_float(_position_field(position, "size", "balance", "amount", "quantity"), 0.0)


def _position_outcome_index(position: dict) -> Optional[int]:
    raw = _position_field(position, "outcomeIndex", "outcome_index")
    if raw is not None:
        return _safe_int(raw)
    index_set = _safe_int(_position_field(position, "indexSet", "index_set"), -1)
    if index_set == 1:
        return 0
    if index_set == 2:
        return 1
    return None


def _index_set_for_position(position: dict) -> Optional[int]:
    raw = _position_field(position, "indexSet", "index_set")
    if raw is not None:
        return _safe_int(raw)
    outcome_index = _position_outcome_index(position)
    if outcome_index == 0:
        return 1
    if outcome_index == 1:
        return 2
    return None


def _amount_units_from_size(size: float) -> int:
    return max(0, int(round(float(size) * 1_000_000.0)))


@dataclass
class SettlementAction:
    action_type: str  # redeem | merge
    condition_id: str
    index_sets: list[int]
    amount_units: int
    amount_shares: float
    token_ids: list[str]
    market_slug: Optional[str]
    title: Optional[str]
    outcome_labels: list[str]
    negative_risk: bool = False


@dataclass
class SettlementConfig:
    enabled: bool
    run_seconds: int
    poll_seconds: float
    relayer_url: str
    ctf_address: str
    collateral_token: str
    user_address: str
    max_actions_per_cycle: int
    skip_negative_risk: bool
    min_action_shares: float

    @classmethod
    def from_env(cls) -> "SettlementConfig":
        return cls(
            enabled=_env_bool("POLY_PORTFOLIO_SETTLEMENT_ENABLED", False),
            run_seconds=_env_int("POLY_PORTFOLIO_SETTLEMENT_RUN_SECONDS", 3600),
            poll_seconds=_env_float("POLY_PORTFOLIO_SETTLEMENT_POLL_SECS", 60.0),
            relayer_url=os.getenv("POLY_PORTFOLIO_SETTLEMENT_RELAYER_URL", DEFAULT_RELAYER_URL),
            ctf_address=to_checksum_address(os.getenv("POLY_PORTFOLIO_SETTLEMENT_CTF_ADDRESS", DEFAULT_CTF_ADDRESS)),
            collateral_token=to_checksum_address(os.getenv("POLY_PORTFOLIO_SETTLEMENT_COLLATERAL_TOKEN", DEFAULT_COLLATERAL_TOKEN)),
            user_address=_positions_user_address(),
            max_actions_per_cycle=_env_int("POLY_PORTFOLIO_SETTLEMENT_MAX_ACTIONS_PER_CYCLE", 10),
            skip_negative_risk=_env_bool("POLY_PORTFOLIO_SETTLEMENT_SKIP_NEG_RISK", True),
            min_action_shares=_env_float("POLY_PORTFOLIO_SETTLEMENT_MIN_ACTION_SHARES", 0.000001),
        )

    def as_dict(self) -> dict:
        return asdict(self)


def _redeem_actions(positions: Iterable[dict], cfg: SettlementConfig) -> list[SettlementAction]:
    actions: list[SettlementAction] = []
    seen: set[tuple[str, int]] = set()
    for position in positions:
        condition_id = str(_position_field(position, "conditionId", "condition_id") or "")
        if not condition_id:
            continue
        if not _boolish(_position_field(position, "redeemable", default=False)):
            continue
        negative_risk = _boolish(_position_field(position, "negativeRisk", "negative_risk", "negRisk", default=False))
        if cfg.skip_negative_risk and negative_risk:
            continue
        index_set = _index_set_for_position(position)
        if index_set is None:
            continue
        key = (condition_id, index_set)
        if key in seen:
            continue
        size = _position_size(position)
        if size < cfg.min_action_shares:
            continue
        seen.add(key)
        token_id = str(_position_field(position, "asset", "asset_id", "tokenId", "token_id") or "")
        actions.append(
            SettlementAction(
                action_type="redeem",
                condition_id=condition_id,
                index_sets=[int(index_set)],
                amount_units=0,
                amount_shares=round(size, 6),
                token_ids=[token_id] if token_id else [],
                market_slug=_position_field(position, "slug", "marketSlug", "market_slug"),
                title=_position_field(position, "title", "marketTitle", "market_title"),
                outcome_labels=[str(_position_field(position, "outcome", default=index_set))],
                negative_risk=negative_risk,
            )
        )
    return actions


def _merge_actions(positions: Iterable[dict], cfg: SettlementConfig) -> list[SettlementAction]:
    grouped: dict[str, dict[int, dict]] = {}
    meta: dict[str, dict] = {}
    for position in positions:
        condition_id = str(_position_field(position, "conditionId", "condition_id") or "")
        if not condition_id:
            continue
        if not _boolish(_position_field(position, "mergeable", default=False)):
            continue
        negative_risk = _boolish(_position_field(position, "negativeRisk", "negative_risk", "negRisk", default=False))
        if cfg.skip_negative_risk and negative_risk:
            continue
        outcome_index = _position_outcome_index(position)
        if outcome_index not in (0, 1):
            continue
        grouped.setdefault(condition_id, {})[int(outcome_index)] = position
        meta.setdefault(
            condition_id,
            {
                "market_slug": _position_field(position, "slug", "marketSlug", "market_slug"),
                "title": _position_field(position, "title", "marketTitle", "market_title"),
                "negative_risk": negative_risk,
            },
        )
    actions: list[SettlementAction] = []
    for condition_id, legs in grouped.items():
        if 0 not in legs or 1 not in legs:
            continue
        size = min(_position_size(legs[0]), _position_size(legs[1]))
        if size < cfg.min_action_shares:
            continue
        actions.append(
            SettlementAction(
                action_type="merge",
                condition_id=condition_id,
                index_sets=[1, 2],
                amount_units=_amount_units_from_size(size),
                amount_shares=round(size, 6),
                token_ids=[
                    str(_position_field(legs[0], "asset", "asset_id", "tokenId", "token_id") or ""),
                    str(_position_field(legs[1], "asset", "asset_id", "tokenId", "token_id") or ""),
                ],
                market_slug=meta[condition_id]["market_slug"],
                title=meta[condition_id]["title"],
                outcome_labels=[
                    str(_position_field(legs[0], "outcome", default="0")),
                    str(_position_field(legs[1], "outcome", default="1")),
                ],
                negative_risk=bool(meta[condition_id]["negative_risk"]),
            )
        )
    return actions


def _encode_redeem(action: SettlementAction, cfg: SettlementConfig) -> SafeTransaction:
    data = _encode_call(
        "redeemPositions(address,bytes32,bytes32,uint256[])",
        ["address", "bytes32", "bytes32", "uint256[]"],
        [
            cfg.collateral_token,
            _bytes32(ZERO_BYTES32),
            _bytes32(action.condition_id),
            [int(value) for value in action.index_sets],
        ],
    )
    return SafeTransaction(to=cfg.ctf_address, operation=OperationType.Call, data=data, value="0")


def _encode_merge(action: SettlementAction, cfg: SettlementConfig) -> SafeTransaction:
    data = _encode_call(
        "mergePositions(address,bytes32,bytes32,uint256[],uint256)",
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [
            cfg.collateral_token,
            _bytes32(ZERO_BYTES32),
            _bytes32(action.condition_id),
            [int(value) for value in action.index_sets],
            int(action.amount_units),
        ],
    )
    return SafeTransaction(to=cfg.ctf_address, operation=OperationType.Call, data=data, value="0")


def _transaction_for_action(action: SettlementAction, cfg: SettlementConfig) -> SafeTransaction:
    if action.action_type == "redeem":
        return _encode_redeem(action, cfg)
    if action.action_type == "merge":
        return _encode_merge(action, cfg)
    raise ValueError(f"unsupported action type: {action.action_type}")


def _plan_actions(cfg: SettlementConfig) -> dict:
    redeemable = _fetch_positions(user=cfg.user_address, redeemable=True)
    mergeable = _fetch_positions(user=cfg.user_address, mergeable=True)
    redeem_actions = _redeem_actions(redeemable, cfg)
    merge_actions = _merge_actions(mergeable, cfg)
    actions = merge_actions + redeem_actions
    return {
        "redeemable_positions": len(redeemable),
        "mergeable_positions": len(mergeable),
        "actions": actions[: cfg.max_actions_per_cycle],
        "skipped_actions": max(0, len(actions) - cfg.max_actions_per_cycle),
    }


def validate_portfolio_settlement_preflight() -> tuple[bool, str, dict]:
    load_dotenv()
    cfg = SettlementConfig.from_env()
    context: dict[str, Any] = {"config": cfg.as_dict()}
    if not cfg.enabled:
        return False, "Portfolio settlement requires POLY_PORTFOLIO_SETTLEMENT_ENABLED=true.", context
    if not (os.getenv("POLY_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")):
        return False, "Portfolio settlement requires POLY_PRIVATE_KEY.", context
    try:
        relayer, auth_meta = _build_relayer()
    except Exception as exc:
        context["relayer_error"] = str(exc)
        return False, f"Relayer setup failed: {exc}", context
    context["builder_auth"] = auth_meta
    try:
        expected_safe = relayer.get_expected_safe()
        deployed = relayer.get_deployed(expected_safe)
        context["expected_safe"] = expected_safe
        context["safe_matches_user"] = expected_safe.lower() == cfg.user_address.lower()
        context["safe_deployed"] = bool(deployed)
        if not deployed:
            return False, "Expected safe/proxy is not deployed.", context
        relayer.get_transactions()
        context["relayer_auth_ok"] = True
    except Exception as exc:
        context["relayer_auth_ok"] = False
        context["relayer_auth_error"] = str(exc)
        return False, f"Relayer auth failed: {exc}", context
    try:
        plan = _plan_actions(cfg)
        context["plan_preview"] = {
            "redeemable_positions": plan["redeemable_positions"],
            "mergeable_positions": plan["mergeable_positions"],
            "actions": [asdict(action) for action in plan["actions"]],
            "skipped_actions": plan["skipped_actions"],
        }
    except Exception as exc:
        context["plan_error"] = str(exc)
        return False, f"Position scan failed: {exc}", context
    return True, "Portfolio settlement preflight passed.", context


def run_portfolio_settlement_v1(*, duration_seconds: Optional[int] = None) -> None:
    load_dotenv()
    cfg = SettlementConfig.from_env()
    log_dir = _build_log_dir()
    log_path = log_dir / "portfolio_settlement.jsonl"
    started_at = time.time()
    relayer, auth_meta = _build_relayer()

    _append_jsonl(
        log_path,
        {
            "ts": time.time(),
            "type": "boot",
            "config": cfg.as_dict(),
            "builder_auth": auth_meta,
        },
    )

    while True:
        if duration_seconds is not None and time.time() - started_at >= duration_seconds:
            _append_jsonl(log_path, {"ts": time.time(), "type": "shutdown"})
            return
        try:
            plan = _plan_actions(cfg)
            _append_jsonl(
                log_path,
                {
                    "ts": time.time(),
                    "type": "scan",
                    "redeemable_positions": plan["redeemable_positions"],
                    "mergeable_positions": plan["mergeable_positions"],
                    "action_count": len(plan["actions"]),
                    "skipped_actions": plan["skipped_actions"],
                },
            )
            for action in plan["actions"]:
                tx = _transaction_for_action(action, cfg)
                _append_jsonl(log_path, {"ts": time.time(), "type": "action_submit", "action": asdict(action)})
                response = relayer.execute([tx], metadata=json.dumps({"action": action.action_type, "condition_id": action.condition_id}))
                final_tx = response.wait()
                _append_jsonl(
                    log_path,
                    {
                        "ts": time.time(),
                        "type": "action_result",
                        "action": asdict(action),
                        "transaction_id": response.transaction_id,
                        "transaction_hash": response.transaction_hash,
                        "final_tx": final_tx,
                    },
                )
        except Exception as exc:
            _append_jsonl(
                log_path,
                {
                    "ts": time.time(),
                    "type": "exception",
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
        time.sleep(max(5.0, cfg.poll_seconds))


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket portfolio settlement runner")
    parser.add_argument("--seconds", type=int, default=None, help="Optional run duration override")
    parser.add_argument("--preflight-only", action="store_true", help="Run only preflight checks and exit")
    args = parser.parse_args()

    ok, msg, context = validate_portfolio_settlement_preflight()
    print("[PORTFOLIO_SETTLEMENT_PREFLIGHT]")
    print(json.dumps(context, ensure_ascii=False, indent=2))
    print(f"[RESULT] {msg}")
    if not ok:
        return 1
    if args.preflight_only:
        return 0
    run_portfolio_settlement_v1(duration_seconds=args.seconds or SettlementConfig.from_env().run_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
