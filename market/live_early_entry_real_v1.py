"""
market/live_early_entry_real_v1.py

Runner real da estratégia Early Entry (EE) v2.
Combina a lógica v2 do paper runner com a execução de ordens do AR real runner.

Estratégia EE v2:
  - Detecta Early Leader (EL) em secs 181-240 (bid >= 0.55)
  - Filtro F3: bid EL >= 0.70 em secs 121-180 (cont_ok)
  - Filtro el_vel: crescimento bid >= 0.08 (bid_180 - bid_240)
  - Entra quando bid EL em [0.82, 0.86] e 30 <= secs <= 180 (GTC passiva)
  - Stop loss:     sai por FAK se bid EL < 0.65
  - Profit protect: sai por GTC se bid EL >= 0.88 e 36 <= secs <= 70
  - WIN:           hold to resolution (awaiting_redeem)

Shadow mode (sem -ArmReal / EE_REAL_POSTS_ENABLED=false):
  Loga o que faria mas não posta ordens reais.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from market.broker_env import load_broker_env
from market.broker_types import BrokerOrderRequest
from market.polymarket_broker_v3 import PolymarketBrokerV3
from market.rest_5m_shadow_public_v5 import (
    _build_slot_bundle,
    _compute_executable_metrics,
    _fetch_slot_state,
    _slot_snapshot,
)

# ---------------------------------------------------------------------------
# Parâmetros EE v2 — idênticos ao paper runner
# ---------------------------------------------------------------------------

EE_EL_MIN              = 0.55
EE_CONT_MIN            = 0.70
EE_VEL_MIN             = 0.13
EE_ENTRY_LO            = 0.82
EE_ENTRY_HI            = 0.86
EE_STOP_LEVEL          = 0.65   # stop loss: sai por FAK se bid EL < 0.65
EE_PROFIT_PROTECT_BID  = 0.88   # profit protect: sai por GTC se bid EL >= 0.88
EE_PROFIT_PROTECT_SECS = 70     # profit protect: só ativa quando 36 <= secs <= 70
EE_MAX_ENTRY_SECS      = 180
EE_MIN_ENTRY_SECS      = 30


# ---------------------------------------------------------------------------
# State dataclass
# ---------------------------------------------------------------------------

@dataclass
class EarlyEntryTradeState:
    mode: str = "idle"
    # idle | pending_entry | open_position | pending_exit | awaiting_redeem
    slug: Optional[str] = None
    side: Optional[str] = None
    token_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    entry_price: float = 0.0
    entry_qty_requested: float = 0.0
    entry_qty_filled: float = 0.0
    exit_price_posted: float = 0.0
    exit_qty_filled: float = 0.0
    exit_reason: str = ""
    # meta
    pnl: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    resolution_detected_at: float = 0.0
    last_reason: str = ""

    @property
    def remaining_qty(self) -> float:
        return round(max(0.0, self.entry_qty_filled - self.exit_qty_filled), 6)


# ---------------------------------------------------------------------------
# Utilitários (adaptados do AR runner)
# ---------------------------------------------------------------------------

def _sf(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(d)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_log_dir() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"ee_real_{ts}"


def _state_path() -> Path:
    return Path("logs") / "early_entry_real_state.json"


def _save_state(path: Path, trade: EarlyEntryTradeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(trade), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state(path: Path) -> Optional[EarlyEntryTradeState]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return EarlyEntryTradeState(**payload)
    except Exception:
        return None


def _clear_state(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _is_flat_qty(qty: float, epsilon: float = 0.000001) -> bool:
    return abs(float(qty)) <= float(epsilon)


def _token_id_for_side(snap: dict, side: str) -> str:
    book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return str(book.get("token_id") or "")


def _bid_for_side(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    key = "up_bid" if side == "UP" else "down_bid"
    return _sf(executable.get(key), 0.0)


def _opp_bid_for_side(executable: Optional[dict], side: str) -> float:
    opp = "DOWN" if side == "UP" else "UP"
    return _bid_for_side(executable, opp)


def _clamp_limit_price(price: float, *, tick_size: float) -> float:
    tick = max(0.001, _sf(tick_size, 0.001))
    bounded = min(max(tick, _sf(price, tick)), 1.0 - tick)
    return round(bounded, 6)


def _get_order_status(broker, order_id: Optional[str]):
    if not order_id:
        return None
    try:
        order = broker.get_order(order_id)
        if order is not None:
            return order
    except Exception:
        pass
    try:
        for order in broker.get_open_orders()[:50]:
            if order.order_id == order_id:
                return order
    except Exception:
        pass
    return None


def _cancel_open_orders_for_token(broker, token_id: str) -> list:
    cancelled = []
    try:
        for order in broker.get_open_orders()[:50]:
            order_asset = str(getattr(order, "token_id", None) or "")
            if order_asset and order_asset == token_id:
                try:
                    broker.cancel_order(order.order_id)
                    cancelled.append(order.order_id)
                except Exception:
                    pass
    except Exception:
        pass
    return cancelled


def _token_balance_qty(broker, token_id: Optional[str]) -> float:
    if not token_id:
        return 0.0
    try:
        payload = broker.get_balance_allowance(asset_type="CONDITIONAL", token_id=token_id)
        raw = float(payload.get("balance") or 0.0)
        return round(raw / 1_000_000.0, 6)
    except Exception:
        return 0.0


def _collateral_balance_usd(broker) -> float:
    try:
        payload = broker.get_balance_allowance(asset_type="COLLATERAL")
        raw = float(payload.get("balance") or 0.0)
        return round(raw / 1_000_000.0, 6)
    except Exception:
        return 0.0


def _has_sufficient_collateral(broker, *, entry_price: float, qty: float, buffer_usd: float = 0.25) -> bool:
    required = round(float(entry_price) * float(qty) + float(buffer_usd), 6)
    return _collateral_balance_usd(broker) >= required


# ---------------------------------------------------------------------------
# Early Leader Tracker (copiado do paper runner, sem modificações)
# ---------------------------------------------------------------------------

class _EarlyLeaderTracker:
    """Rastreia EL por slug — calcula el_vel e filtro F3 (cont_ok)."""

    def __init__(self) -> None:
        self._slug: Optional[str] = None
        self._s240: list = []
        self._s180: list = []
        self.early_leader: Optional[str] = None
        self.el_bid_240:   float = 0.0
        self.el_bid_180:   float = 0.0
        self.el_vel:       float = 0.0
        self.cont_ok:      bool  = False

    def update(self, slug: str, secs: Optional[int], up_bid: float, down_bid: float) -> None:
        if secs is None:
            return
        if self._slug != slug:
            self._reset(slug)
        snap = {"secs": secs, "up_bid": up_bid, "down_bid": down_bid}
        if 181 <= secs <= 240:
            self._s240.append(snap)
            self._compute_el()
        elif 121 <= secs <= 180:
            self._s180.append(snap)
            self._compute_f3()

    def _compute_el(self) -> None:
        if not self._s240:
            return
        avg_up = sum(s["up_bid"]   for s in self._s240) / len(self._s240)
        avg_dn = sum(s["down_bid"] for s in self._s240) / len(self._s240)
        if avg_up >= EE_EL_MIN:
            self.early_leader = "UP"
            self.el_bid_240   = round(avg_up, 4)
        elif avg_dn >= EE_EL_MIN:
            self.early_leader = "DOWN"
            self.el_bid_240   = round(avg_dn, 4)

    def _compute_f3(self) -> None:
        if not (self.early_leader and self._s180):
            return
        bids = [
            s["up_bid"] if self.early_leader == "UP" else s["down_bid"]
            for s in self._s180
        ]
        self.el_bid_180 = round(sum(bids) / len(bids), 4)
        self.el_vel     = round(self.el_bid_180 - self.el_bid_240, 4)
        self.cont_ok    = min(bids) >= EE_CONT_MIN

    def _reset(self, slug: str) -> None:
        self._slug        = slug
        self._s240        = []
        self._s180        = []
        self.early_leader = None
        self.el_bid_240   = 0.0
        self.el_bid_180   = 0.0
        self.el_vel       = 0.0
        self.cont_ok      = False

    @property
    def signal_ok(self) -> bool:
        return bool(self.early_leader and self.cont_ok and self.el_vel >= EE_VEL_MIN)

    def state_dict(self) -> dict:
        return {
            "early_side":  self.early_leader,
            "el_bid_240":  self.el_bid_240,
            "el_bid_180":  self.el_bid_180,
            "el_vel":      self.el_vel,
            "f3_ok":       self.cont_ok,
            "signal_ok":   self.signal_ok,
            "n_s180":      len(self._s180),
        }


# ---------------------------------------------------------------------------
# Funções de ordem
# ---------------------------------------------------------------------------

def _post_ee_entry(
    broker,
    *,
    log_path: Path,
    session_id: str,
    slug: str,
    snap: dict,
    executable: dict,
    side: str,
    qty: float,
    el_bid: float,
    el_state: dict,
    secs: int,
    now: float,
    poll_secs: float,
    real_posts: bool,
) -> Optional[EarlyEntryTradeState]:
    """Posta ordem GTC de compra e retorna novo estado pending_entry, ou None se bloqueado."""
    token_id = _token_id_for_side(snap, side)
    if not token_id:
        return None

    # Cancel de ordens stale (double-entry guard)
    stale = _cancel_open_orders_for_token(broker, token_id)
    if stale:
        _append_jsonl(log_path, {
            "type": "ee_real_pre_entry_stale_cancel",
            "ts": now, "session_id": session_id, "slug": slug,
            "token_id": token_id, "cancelled": stale,
        })
        time.sleep(max(poll_secs * 2, 1.0))

    # Verificação de balance residual
    pre_balance = _token_balance_qty(broker, token_id)
    if pre_balance > 0:
        _append_jsonl(log_path, {
            "type": "ee_real_entry_blocked",
            "ts": now, "session_id": session_id, "slug": slug,
            "reason": f"pre_entry_balance_nonzero:{round(pre_balance, 4)}",
            "token_id": token_id,
        })
        return None

    # Verificação de colateral
    if not _has_sufficient_collateral(broker, entry_price=el_bid, qty=qty):
        _append_jsonl(log_path, {
            "type": "ee_real_entry_blocked",
            "ts": now, "session_id": session_id, "slug": slug,
            "reason": "insufficient_collateral",
            "collateral": _collateral_balance_usd(broker),
            "required": round(el_bid * qty + 0.25, 4),
        })
        return None

    trade = EarlyEntryTradeState(
        mode="pending_entry",
        slug=slug,
        side=side,
        token_id=token_id,
        entry_price=round(el_bid, 6),
        entry_qty_requested=float(qty),
        created_at=now,
        updated_at=now,
        last_reason="entry_posted",
    )

    _append_jsonl(log_path, {
        "type": "ee_real_entry",
        "ts": now, "session_id": session_id, "slug": slug,
        "side": side, "ep": round(el_bid, 4), "secs": secs,
        "qty": qty, "token_id": token_id,
        "el": el_state,
        "shadow": not real_posts,
    })

    if not real_posts:
        print(f"[EE_REAL] [SHADOW] ENTRADA {side}  ep={el_bid:.3f}  secs={secs}  slug={slug[-20:]}")
        trade.last_reason = "entry_shadow"
        return trade

    req = BrokerOrderRequest(
        token_id=token_id,
        side="BUY",
        price=round(el_bid, 6),
        size=float(qty),
        order_type="GTC",
        market_slug=slug,
        outcome=side,
        client_order_key=f"ee_real:entry:{int(now)}:{side}",
    )
    order = broker.place_limit_order(req)
    trade.entry_order_id = order.order_id
    trade.entry_qty_filled = _sf(getattr(order, "size_matched", None), 0.0)
    trade.last_reason = f"entry_posted:gtc:matched={round(trade.entry_qty_filled, 4)}"
    print(f"[EE_REAL] ENTRADA {side}  ep={el_bid:.3f}  secs={secs}  order={order.order_id}  slug={slug[-20:]}")
    return trade


def _post_ee_exit(
    broker,
    trade: EarlyEntryTradeState,
    *,
    log_path: Path,
    session_id: str,
    exit_price: float,
    reason: str,
    now: float,
    real_posts: bool,
) -> EarlyEntryTradeState:
    """Posta ordem de venda (stop=FAK urgente, profit_protect=GTC passiva)."""
    qty = _token_balance_qty(broker, trade.token_id) if real_posts else trade.remaining_qty
    if qty <= 0:
        qty = trade.remaining_qty
    if _is_flat_qty(qty):
        trade.mode = "idle"
        trade.last_reason = "flat_before_exit"
        trade.updated_at = now
        return trade

    urgent = "stop" in reason or "reversal" in reason
    order_type = "FAK" if urgent else "GTC"

    tick_size = 0.001
    post_price = _clamp_limit_price(exit_price, tick_size=tick_size)

    _append_jsonl(log_path, {
        "type": "ee_real_exit_posted",
        "ts": now, "session_id": session_id, "slug": trade.slug,
        "reason": reason, "exit_price": round(post_price, 4),
        "order_type": order_type, "qty": round(qty, 4),
        "shadow": not real_posts,
    })
    print(f"[EE_REAL] EXIT {reason}  price={post_price:.3f}  type={order_type}  qty={qty:.2f}")

    trade.exit_price_posted = post_price
    trade.exit_reason = reason
    trade.mode = "pending_exit"
    trade.updated_at = now
    trade.last_reason = f"exit_posted:{reason}:{order_type.lower()}"

    if not real_posts:
        return trade

    req = BrokerOrderRequest(
        token_id=trade.token_id or "",
        side="SELL",
        price=post_price,
        size=float(qty),
        order_type=order_type,
        market_slug=trade.slug,
        outcome=trade.side,
        client_order_key=f"ee_real:exit:{reason}:{int(now)}:{trade.side}",
    )
    try:
        order = broker.place_limit_order(req)
        trade.exit_order_id = order.order_id
        trade.last_reason = f"exit_posted:{reason}:{order_type.lower()}:id={order.order_id}"
    except Exception as exc:
        trade.exit_order_id = None
        trade.last_reason = f"exit_post_failed:{reason}:{type(exc).__name__}:{exc}"
    return trade


def _mark_awaiting_redeem(
    trade: EarlyEntryTradeState, *, now: float, reason: str
) -> EarlyEntryTradeState:
    trade.mode = "awaiting_redeem"
    trade.exit_reason = reason
    trade.entry_order_id = None
    trade.exit_order_id = None
    trade.updated_at = now
    trade.resolution_detected_at = now
    trade.last_reason = reason
    return trade


# ---------------------------------------------------------------------------
# Runner principal
# ---------------------------------------------------------------------------

def run_early_entry_real_v1(
    duration_seconds: Optional[int] = None,
) -> None:
    load_dotenv()

    real_enabled  = str(os.getenv("EE_REAL_ENABLED", "false")).strip().lower() in ("1", "true", "yes")
    real_posts    = str(os.getenv("EE_REAL_POSTS_ENABLED", "false")).strip().lower() in ("1", "true", "yes")
    qty           = float(os.getenv("EE_REAL_QTY", "6"))
    poll_secs     = max(0.25, _sf(os.getenv("EE_REAL_POLL_SECS", "0.5")))
    run_for       = int(duration_seconds or _sf(os.getenv("EE_REAL_RUN_SECONDS", "3600"), 3600))

    session_dir = _build_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path    = session_dir / "ee_real.jsonl"
    session_id  = session_dir.name
    state_path  = _state_path()

    mode_label = "REAL" if real_posts else "SHADOW"
    print(f"[EE_REAL] session={session_id}  mode={mode_label}  qty={qty}  poll={poll_secs}s  run={run_for}s")
    print(f"[EE_REAL] stop<{EE_STOP_LEVEL}  pp>={EE_PROFIT_PROTECT_BID}@secs<={EE_PROFIT_PROTECT_SECS}")
    print(f"[EE_REAL] log={log_path}")

    if not real_enabled and real_posts:
        print("[EE_REAL] AVISO: EE_REAL_ENABLED=false — desativando posts mesmo com EE_REAL_POSTS_ENABLED=true")
        real_posts = False

    broker = None
    if real_posts:
        broker_env = load_broker_env()
        broker = PolymarketBrokerV3(broker_env)

    _append_jsonl(log_path, {
        "type": "startup", "ts": time.time(), "session_id": session_id,
        "mode": mode_label, "qty": qty, "poll_secs": poll_secs, "run_for": run_for,
        "params": {
            "EE_EL_MIN": EE_EL_MIN, "EE_CONT_MIN": EE_CONT_MIN,
            "EE_VEL_MIN": EE_VEL_MIN, "EE_ENTRY_LO": EE_ENTRY_LO,
            "EE_ENTRY_HI": EE_ENTRY_HI, "EE_STOP_LEVEL": EE_STOP_LEVEL,
            "EE_PROFIT_PROTECT_BID": EE_PROFIT_PROTECT_BID,
            "EE_PROFIT_PROTECT_SECS": EE_PROFIT_PROTECT_SECS,
        },
    })

    # Restaurar estado persistido (se o watchdog reiniciou)
    trade = _load_state(state_path) or EarlyEntryTradeState()
    if trade.mode != "idle":
        print(f"[EE_REAL] Estado restaurado: mode={trade.mode}  slug={trade.slug}  side={trade.side}")
        if broker and trade.mode not in ("awaiting_redeem",):
            # Sincronizar com broker
            if trade.entry_order_id and trade.mode == "pending_entry":
                order = _get_order_status(broker, trade.entry_order_id)
                if order:
                    filled = _sf(getattr(order, "size_matched", None), 0.0)
                    if filled > 0:
                        trade.entry_qty_filled = max(trade.entry_qty_filled, filled)
                        trade.mode = "open_position"
            token_bal = _token_balance_qty(broker, trade.token_id) if trade.token_id else 0.0
            if token_bal > 0:
                trade.entry_qty_filled = max(trade.entry_qty_filled, token_bal)
                if trade.mode == "pending_entry":
                    trade.mode = "open_position"
    elif broker:
        startup_orders = []
        try:
            startup_orders = broker.get_open_orders()[:20]
        except Exception:
            pass
        if startup_orders:
            print(f"[EE_REAL] GUARDA: {len(startup_orders)} ordens abertas sem estado — não entrando")
            _append_jsonl(log_path, {
                "type": "startup_guard",
                "ts": time.time(), "session_id": session_id,
                "open_orders": len(startup_orders),
                "reason": "open_orders_without_state",
            })
            return

    elt = _EarlyLeaderTracker()
    _last_slug = trade.slug or ""
    started_at = time.time()

    # acumuladores de sessão
    session_pnl    = 0.0
    session_wins   = 0
    session_losses = 0

    while time.time() - started_at < run_for:
        now = time.time()
        try:
            slot_bundle  = _build_slot_bundle()
            current_item = slot_bundle["queue"].get("current")
            if not current_item:
                time.sleep(poll_secs)
                continue

            slug      = str(current_item.get("slug") or "")
            secs_raw  = current_item.get("seconds_to_end")
            secs: Optional[int] = int(secs_raw) if secs_raw is not None else None

            slot_state   = _fetch_slot_state(slot_bundle)
            current_snap = _slot_snapshot(slot_state, "current")
            current_exec, _ = _compute_executable_metrics(current_snap)

            up_bid   = _sf((current_exec or {}).get("up_bid"))
            down_bid = _sf((current_exec or {}).get("down_bid"))

            if not slug or up_bid <= 0 or down_bid <= 0:
                time.sleep(poll_secs)
                continue

            # ── Slug mudou ────────────────────────────────────────────────────
            if slug != _last_slug:
                if trade.mode in ("pending_entry",):
                    # Cancela entrada pendente
                    if broker and trade.entry_order_id:
                        try:
                            broker.cancel_order(trade.entry_order_id)
                        except Exception:
                            pass
                    _append_jsonl(log_path, {
                        "type": "ee_real_entry_cancelled",
                        "ts": now, "session_id": session_id, "slug": _last_slug,
                        "reason": "slug_changed",
                    })
                    print(f"[EE_REAL] Entrada cancelada (slug mudou)  slug={_last_slug[-20:]}")
                    trade = EarlyEntryTradeState()
                    _clear_state(state_path)

                elif trade.mode == "awaiting_redeem":
                    # Slug rolou → resolução confirmada
                    pnl = round((1.0 - trade.entry_price) * trade.entry_qty_filled, 4)
                    trade.pnl = pnl
                    session_pnl += pnl
                    if pnl > 0:
                        session_wins += 1
                    else:
                        session_losses += 1
                    _append_jsonl(log_path, {
                        "type": "ee_real_closed",
                        "ts": now, "session_id": session_id, "slug": _last_slug,
                        "outcome": trade.exit_reason or "win_awaiting_redeem",
                        "entry_price": trade.entry_price,
                        "exit_price": 1.0,
                        "pnl": pnl,
                        "session_pnl": round(session_pnl, 4),
                    })
                    print(f"[EE_REAL] WIN/REDEEM  PnL={pnl:+.4f}  sess={session_pnl:+.4f}")
                    trade = EarlyEntryTradeState()
                    _clear_state(state_path)

                elif trade.mode == "open_position":
                    # Slug rolou com posição aberta — MISSED
                    _append_jsonl(log_path, {
                        "type": "ee_real_closed",
                        "ts": now, "session_id": session_id, "slug": _last_slug,
                        "outcome": "MISSED_SLUG_CHANGE", "pnl": 0.0,
                        "session_pnl": round(session_pnl, 4),
                    })
                    print(f"[EE_REAL] MISSED (slug mudou com posição aberta)  slug={_last_slug[-20:]}")
                    trade = EarlyEntryTradeState()
                    _clear_state(state_path)

                _last_slug = slug
                elt.update(slug, secs, up_bid, down_bid)
                time.sleep(poll_secs)
                continue

            # ── Atualiza EL tracker ───────────────────────────────────────────
            elt.update(slug, secs, up_bid, down_bid)
            el_side = elt.early_leader
            el_bid  = _bid_for_side(current_exec, el_side) if el_side else 0.0
            opp_bid = _opp_bid_for_side(current_exec, el_side) if el_side else 0.0

            # ── Gates de entrada (baseados em dados reais) ────────────────────
            _n_s180         = len(elt._s180)
            _n_s180_blocked = (_n_s180 < 3)
            _secs_blocked   = (secs is not None and secs > 155)
            _entry_blocked  = _n_s180_blocked or _secs_blocked
            _gate_reason    = (
                f"n_s180:{_n_s180}<3" if _n_s180_blocked else f"secs:{secs}>155"
            ) if _entry_blocked else ""

            # ── Modo idle: procurar entrada ───────────────────────────────────
            if trade.mode == "idle":
                if (
                    elt.signal_ok
                    and secs is not None
                    and EE_MIN_ENTRY_SECS <= secs <= EE_MAX_ENTRY_SECS
                    and EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI
                    and not _entry_blocked
                ):
                    _broker = broker if real_posts else None
                    new_trade = _post_ee_entry(
                        _broker or _MockBroker(),
                        log_path=log_path, session_id=session_id,
                        slug=slug, snap=current_snap, executable=current_exec,
                        side=el_side, qty=qty, el_bid=el_bid,
                        el_state=elt.state_dict(), secs=secs, now=now,
                        poll_secs=poll_secs, real_posts=real_posts,
                    )
                    if new_trade:
                        trade = new_trade
                        _save_state(state_path, trade)
                elif (
                    _entry_blocked
                    and elt.signal_ok
                    and secs is not None
                    and EE_MIN_ENTRY_SECS <= secs <= EE_MAX_ENTRY_SECS
                    and EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI
                ):
                    _append_jsonl(log_path, {
                        "type": "entry_blocked", "ts": now,
                        "session_id": session_id, "slug": slug,
                        "reason": f"gate:{_gate_reason}",
                        "ep": round(el_bid, 4), "n_s180": _n_s180, "secs": secs,
                    })
                    print(f"[EE_REAL] BLOCKED  gate={_gate_reason}  ep={el_bid:.3f}  secs={secs}")

            # ── Modo pending_entry: aguardar fill ─────────────────────────────
            elif trade.mode == "pending_entry":
                # Timeout: secs abaixo do mínimo sem fill
                if secs is not None and secs < EE_MIN_ENTRY_SECS:
                    if broker and trade.entry_order_id:
                        try:
                            broker.cancel_order(trade.entry_order_id)
                        except Exception:
                            pass
                    _append_jsonl(log_path, {
                        "type": "ee_real_entry_cancelled",
                        "ts": now, "session_id": session_id, "slug": slug,
                        "reason": f"timeout_secs:{secs}",
                    })
                    print(f"[EE_REAL] Entrada cancelada (timeout secs={secs})")
                    trade = EarlyEntryTradeState()
                    _clear_state(state_path)
                else:
                    # Checar fill via balance
                    if real_posts and trade.token_id:
                        token_bal = _token_balance_qty(broker, trade.token_id)
                        if token_bal > 0:
                            trade.entry_qty_filled = max(trade.entry_qty_filled, token_bal)
                            trade.mode = "open_position"
                            trade.updated_at = now
                            _append_jsonl(log_path, {
                                "type": "ee_real_filled",
                                "ts": now, "session_id": session_id, "slug": slug,
                                "entry_price": trade.entry_price,
                                "qty_filled": round(trade.entry_qty_filled, 4),
                                "secs": secs,
                            })
                            print(f"[EE_REAL] FILLED  ep={trade.entry_price:.3f}  qty={trade.entry_qty_filled:.2f}  secs={secs}")
                            _save_state(state_path, trade)
                    elif not real_posts and trade.mode == "pending_entry":
                        # Shadow: assume fill imediato
                        trade.entry_qty_filled = qty
                        trade.mode = "open_position"
                        trade.updated_at = now
                        _save_state(state_path, trade)

            # ── Modo open_position: monitorar saída ───────────────────────────
            if trade.mode == "open_position" and secs is not None and trade.token_id:
                # Prioridade 1: resolução final (secs <= 35)
                if secs <= 35:
                    if el_bid >= 0.85 and trade.side == el_side:
                        trade = _mark_awaiting_redeem(trade, now=now, reason="win_awaiting_redeem")
                        _append_jsonl(log_path, {
                            "type": "ee_real_win",
                            "ts": now, "session_id": session_id, "slug": slug,
                            "el_bid": round(el_bid, 4), "secs": secs,
                        })
                        print(f"[EE_REAL] WIN  el_bid={el_bid:.3f}  secs={secs}")
                        _save_state(state_path, trade)
                    elif opp_bid >= 0.85:
                        trade = _post_ee_exit(
                            broker or _MockBroker(), trade,
                            log_path=log_path, session_id=session_id,
                            exit_price=el_bid if el_bid > 0.001 else 0.001,
                            reason="reversal", now=now, real_posts=real_posts,
                        )
                        _save_state(state_path, trade)

                # Prioridade 2: profit protect (36 <= secs <= PP_SECS, el_bid >= 0.88)
                elif 36 <= secs <= EE_PROFIT_PROTECT_SECS and el_bid >= EE_PROFIT_PROTECT_BID:
                    trade = _post_ee_exit(
                        broker or _MockBroker(), trade,
                        log_path=log_path, session_id=session_id,
                        exit_price=el_bid, reason="profit_protect",
                        now=now, real_posts=real_posts,
                    )
                    _save_state(state_path, trade)

                # Prioridade 3: livro EL zerou + opp >= 0.85 → reversão confirmada
                # Sem este check, el_bid=0 não disparava nenhuma saída (era coberto
                # apenas pelo P1 em secs<=35). Aqui cobrimos secs > 35 também.
                elif el_bid <= 0 and opp_bid >= 0.85:
                    trade = _post_ee_exit(
                        broker or _MockBroker(), trade,
                        log_path=log_path, session_id=session_id,
                        exit_price=0.001,
                        reason="reversal_book_empty", now=now, real_posts=real_posts,
                    )
                    _save_state(state_path, trade)

                # Prioridade 4: stop FAK removido — causava fills catastróficos (0.42–0.61)
                # em livro fino quando o bid temporariamente cai abaixo de 0.65 e depois
                # se recupera. Paper não para e vence 89% dos casos. A proteção de
                # reversão genuína é coberta pelo check opp_bid >= 0.85.

            # ── Modo pending_exit: aguardar fill de saída ─────────────────────
            elif trade.mode == "pending_exit":
                token_bal = _token_balance_qty(broker, trade.token_id) if (real_posts and trade.token_id) else 0.0
                sold_out = _is_flat_qty(token_bal) if real_posts else True

                if sold_out or not real_posts:
                    # Trade fechado
                    if real_posts:
                        # Verifica fill real do exit order
                        exit_order = _get_order_status(broker, trade.exit_order_id)
                        if exit_order:
                            trade.exit_qty_filled = max(
                                trade.exit_qty_filled,
                                _sf(getattr(exit_order, "size_matched", None), 0.0),
                            )
                        if not sold_out and trade.exit_qty_filled < trade.entry_qty_filled * 0.5:
                            # GTC ainda não preencheu suficientemente — aguarda
                            pass
                        else:
                            sold_out = True

                    if sold_out or not real_posts:
                        pnl = round((trade.exit_price_posted - trade.entry_price) * trade.entry_qty_filled, 4)
                        if trade.exit_reason == "reversal":
                            pnl = round((0.0 - trade.entry_price) * trade.entry_qty_filled, 4)
                        trade.pnl = pnl
                        session_pnl += pnl
                        if pnl > 0:
                            session_wins += 1
                        else:
                            session_losses += 1
                        _append_jsonl(log_path, {
                            "type": "ee_real_closed",
                            "ts": now, "session_id": session_id, "slug": slug,
                            "outcome": trade.exit_reason.upper(),
                            "entry_price": trade.entry_price,
                            "exit_price": trade.exit_price_posted,
                            "pnl": pnl,
                            "session_pnl": round(session_pnl, 4),
                            "session_wins": session_wins,
                            "session_losses": session_losses,
                        })
                        wr = f"{session_wins/(session_wins+session_losses)*100:.0f}%" if (session_wins+session_losses) else "n/a"
                        print(
                            f"[EE_REAL] CLOSED {trade.exit_reason.upper():<16}  "
                            f"PnL={pnl:>+7.4f}  ep={trade.entry_price:.3f}→{trade.exit_price_posted:.3f}  "
                            f"sess={session_pnl:>+.4f}  {session_wins}W/{session_losses}L WR={wr}"
                        )
                        trade = EarlyEntryTradeState()
                        _clear_state(state_path)

                elif real_posts and trade.exit_order_id:
                    # PP GTC pendente: se secs <= 36, o mercado está encerrando → cancela e vai para redeem
                    if trade.exit_reason == "profit_protect" and secs is not None and secs <= 36:
                        try:
                            broker.cancel_order(trade.exit_order_id)
                        except Exception:
                            pass
                        trade = _mark_awaiting_redeem(trade, now=now, reason="pp_timeout_to_win")
                        _save_state(state_path, trade)

            # ── Modo awaiting_redeem ──────────────────────────────────────────
            elif trade.mode == "awaiting_redeem":
                if real_posts and trade.token_id:
                    token_bal = _token_balance_qty(broker, trade.token_id)
                    if _is_flat_qty(token_bal):
                        # Plataforma fez redeem automático
                        pnl = round((1.0 - trade.entry_price) * trade.entry_qty_filled, 4)
                        trade.pnl = pnl
                        session_pnl += pnl
                        session_wins += 1
                        _append_jsonl(log_path, {
                            "type": "ee_real_closed",
                            "ts": now, "session_id": session_id, "slug": slug,
                            "outcome": "WIN_REDEEMED",
                            "pnl": pnl,
                            "session_pnl": round(session_pnl, 4),
                        })
                        print(f"[EE_REAL] WIN_REDEEMED  PnL={pnl:+.4f}  sess={session_pnl:+.4f}")
                        trade = EarlyEntryTradeState()
                        _clear_state(state_path)

            # ── Snapshot ──────────────────────────────────────────────────────
            _append_jsonl(log_path, {
                "type": "snapshot", "ts": now,
                "session_id": session_id,
                "current_slug": slug, "current_secs": secs,
                "current_exec": {
                    "up_bid": round(up_bid, 4), "down_bid": round(down_bid, 4),
                },
                "early_leader": elt.state_dict(),
                "ee_mode": trade.mode,
            })

            n_t = session_wins + session_losses
            wr_d = f"{session_wins/n_t*100:.0f}%" if n_t else "n/a"
            print(
                f"[EE_REAL] {slug[-20:]:20}  secs={str(secs):>4}  "
                f"up={up_bid:.3f} dn={down_bid:.3f}  "
                f"EL={el_side or '---':4} vel={elt.el_vel:>+.3f}  "
                f"mode={trade.mode:<20}  sess={session_pnl:>+.4f} {wr_d}"
            )

        except KeyboardInterrupt:
            print("\n[EE_REAL] Interrompido.")
            break
        except Exception as exc:
            _append_jsonl(log_path, {
                "type": "error", "ts": now, "session_id": session_id,
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"[EE_REAL] ERRO: {type(exc).__name__}: {exc}")

        time.sleep(poll_secs)

    # Sumário final
    n = session_wins + session_losses
    wr = f"{session_wins / n * 100:.0f}%" if n else "n/a"
    avg = round(session_pnl / n, 4) if n else 0.0
    _append_jsonl(log_path, {
        "type": "session_summary", "ts": time.time(), "session_id": session_id,
        "trades": n, "wins": session_wins, "losses": session_losses,
        "win_rate": wr, "pnl_total": round(session_pnl, 4), "avg_pnl": avg,
    })
    print(f"\n[EE_REAL] === SESSÃO ENCERRADA ===")
    print(f"[EE_REAL] Trades: {n}  ({session_wins}W / {session_losses}L  WR={wr})")
    if n:
        print(f"[EE_REAL] PnL total: {session_pnl:>+.4f} USD  avg/trade: {avg:>+.4f}")
    print(f"[EE_REAL] Log: {log_path}")


# ---------------------------------------------------------------------------
# Mock broker para shadow mode sem broker real
# ---------------------------------------------------------------------------

class _MockBroker:
    """Broker no-op para shadow mode — nenhuma ordem real é postada."""
    def place_limit_order(self, req): return _MockOrder()
    def cancel_order(self, oid): pass
    def get_open_orders(self): return []
    def get_order(self, oid): return None
    def get_balance_allowance(self, asset_type: str = "CONDITIONAL", **kw):
        # Collateral: simular saldo suficiente; tokens: simular sem posição existente
        if asset_type == "COLLATERAL":
            return {"balance": 999_999_999}
        return {"balance": 0}


@dataclass
class _MockOrder:
    order_id: str = "shadow_order"
    size_matched: float = 0.0
