"""
market/live_market_maker_paper_v1.py

Market Maker Paper — Fase 1: zero ordens reais.

Estratégia: postar nos dois lados do book quando o mercado está incerto
(preço do token entre 0.38 e 0.62). Simula fill passivo e captura de spread.

Lógica:
  1. Observa mercados com secs_to_end em [60, 240] e mid próximo de 0.50
  2. Simula postar BUY no lado mais barato ao bid atual
  3. Simula fill quando o bid muda >= 1 tick (0.01)
  4. Simula saída ao target (bid + 2 ticks) ou stop (bid - 3 ticks)
  5. Força saída antes dos últimos 60s

Loga tudo em logs/market_maker_paper_YYYYMMDD.jsonl.
Nenhuma ordem real.
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Dict, Optional
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from market.rest_5m_shadow_public_v4 import (
    _build_slot_bundle,
    _compute_executable_metrics,
    _fetch_slot_state,
    _slot_snapshot,
)

CONFIG_PATH = Path(__file__).parent.parent / "agent" / "config.json"

def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f).get("market_maker", {})
    except Exception:
        return {}

def _log_path() -> Path:
    today = date.today().strftime("%Y%m%d")
    return Path("logs") / f"market_maker_paper_{today}.jsonl"

def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


class _MMPosition:
    """Posição simulada de market making."""
    __slots__ = ("slug", "side", "entry_bid", "entry_secs", "entry_ts",
                 "target_bid", "stop_bid", "current_bid", "max_bid", "min_bid")

    def __init__(self, slug: str, side: str, entry_bid: float, entry_secs: int,
                 target_ticks: int = 2, stop_ticks: int = 3):
        self.slug = slug
        self.side = side
        self.entry_bid = entry_bid
        self.entry_secs = entry_secs
        self.entry_ts = time.time()
        self.target_bid = round(entry_bid + target_ticks * 0.01, 4)
        self.stop_bid = round(entry_bid - stop_ticks * 0.01, 4)
        self.current_bid = entry_bid
        self.max_bid = entry_bid
        self.min_bid = entry_bid

    def update(self, bid: float) -> None:
        self.current_bid = bid
        self.max_bid = max(self.max_bid, bid)
        self.min_bid = min(self.min_bid, bid)

    def pnl(self, exit_bid: float, bet_usd: float) -> float:
        shares = bet_usd / self.entry_bid
        return round((exit_bid - self.entry_bid) * shares, 4)


def run_market_maker_paper(run_for: float = float("inf"), poll_secs: float = 1.0) -> None:
    log_path = _log_path()
    print(f"[MARKET_MAKER] Iniciando — modo PAPER ONLY", flush=True)
    print(f"[MARKET_MAKER] Log: {log_path}", flush=True)

    cfg = _load_config()
    MID_MIN         = float(cfg.get("mid_price_min", 0.38))
    MID_MAX         = float(cfg.get("mid_price_max", 0.62))
    MIN_SECS        = int(cfg.get("exit_before_secs", 60))
    MAX_SECS        = 240
    BET_USD         = float(cfg.get("paper_bet_size", 20))
    TARGET_TICKS    = 2
    STOP_TICKS      = 3
    MAX_HOLD_SECS   = int(cfg.get("max_hold_secs", 90))

    _append(log_path, {
        "type": "session_start",
        "ts": time.time(),
        "params": {
            "mid_min": MID_MIN,
            "mid_max": MID_MAX,
            "min_secs_to_exit": MIN_SECS,
            "max_secs_window": MAX_SECS,
            "paper_bet_usd": BET_USD,
            "target_ticks": TARGET_TICKS,
            "stop_ticks": STOP_TICKS,
            "max_hold_secs": MAX_HOLD_SECS,
        },
    })

    position: Optional[_MMPosition] = None
    started_at = time.time()
    stats = {"would_post": 0, "wins": 0, "losses": 0, "pnl": 0.0}

    while time.time() - started_at < run_for:
        now = time.time()
        cfg = _load_config()  # re-lê a cada ciclo para ajuste em tempo real
        BET_USD = float(cfg.get("paper_bet_size", 20))

        try:
            slot_bundle = _build_slot_bundle()
            current_item = slot_bundle["queue"].get("current")
            if not current_item:
                time.sleep(poll_secs)
                continue

            slug = str(current_item.get("slug") or "")
            if not slug:
                time.sleep(poll_secs)
                continue

            raw_secs = current_item.get("seconds_to_end")
            current_secs = int(raw_secs) if raw_secs is not None else None

            # Liquidar posição se mercado mudou ou próximo da resolução
            if position and (position.slug != slug or
                             (current_secs is not None and current_secs <= MIN_SECS) or
                             (now - position.entry_ts >= MAX_HOLD_SECS)):
                reason = "market_changed" if position.slug != slug else \
                         "near_resolution" if (current_secs and current_secs <= MIN_SECS) else \
                         "max_hold_expired"
                exit_bid = position.current_bid
                pnl = position.pnl(exit_bid, BET_USD)
                stats["pnl"] += pnl
                outcome = "win" if pnl > 0 else "loss"
                stats[outcome + "s"] += 1
                _append(log_path, {
                    "type": "simulated_exit",
                    "ts": now,
                    "slug": position.slug,
                    "reason": reason,
                    "side": position.side,
                    "entry_bid": position.entry_bid,
                    "exit_bid": exit_bid,
                    "max_bid": position.max_bid,
                    "min_bid": position.min_bid,
                    "entry_secs": position.entry_secs,
                    "exit_secs": current_secs,
                    "hold_secs": round(now - position.entry_ts, 1),
                    "pnl": pnl,
                    "bet_usd": BET_USD,
                    "outcome": outcome,
                    "running_pnl": round(stats["pnl"], 4),
                })
                print(f"  EXIT {outcome.upper():4s} | {position.slug[-20:]} | "
                      f"entry={position.entry_bid:.3f} exit={exit_bid:.3f} pnl={pnl:+.4f} | {reason}", flush=True)
                position = None

            if current_secs is None or not (MIN_SECS < current_secs <= MAX_SECS):
                time.sleep(poll_secs)
                continue

            # Lê preços
            slot_state = _fetch_slot_state(slot_bundle)
            snap = _slot_snapshot(slot_state, "current")
            metrics, _status = _compute_executable_metrics(snap)
            if metrics is None:
                time.sleep(poll_secs)
                continue

            up_bid   = float(metrics.get("up_bid", 0) or 0)
            down_bid = float(metrics.get("down_bid", 0) or 0)
            up_ask   = float(metrics.get("up_ask", 0) or 0)
            down_ask = float(metrics.get("down_ask", 0) or 0)

            # Apenas mercados genuinamente incertos
            up_mid   = (up_bid + up_ask) / 2 if up_ask else up_bid
            down_mid = (down_bid + down_ask) / 2 if down_ask else down_bid

            # Verificar se é mercado mid (soma das mids próxima de 1.00, cada lado entre MID_MIN e MID_MAX)
            is_mid_market = (MID_MIN <= up_mid <= MID_MAX and
                             MID_MIN <= down_mid <= MID_MAX and
                             abs(up_mid + down_mid - 1.0) < 0.05)

            snap_row = {
                "type": "mm_snapshot",
                "ts": now,
                "slug": slug,
                "current_secs": current_secs,
                "up_bid": round(up_bid, 4),
                "up_ask": round(up_ask, 4),
                "down_bid": round(down_bid, 4),
                "down_ask": round(down_ask, 4),
                "up_mid": round(up_mid, 4),
                "down_mid": round(down_mid, 4),
                "is_mid_market": is_mid_market,
                "has_position": position is not None,
            }
            _append(log_path, snap_row)

            # Atualiza posição existente
            if position and position.slug == slug:
                side_bid = up_bid if position.side == "UP" else down_bid
                position.update(side_bid)

                # Verifica target/stop
                exit_reason = None
                exit_bid = side_bid
                if side_bid >= position.target_bid:
                    exit_reason = "target"
                elif side_bid <= position.stop_bid:
                    exit_reason = "stop"

                if exit_reason:
                    pnl = position.pnl(exit_bid, BET_USD)
                    stats["pnl"] += pnl
                    outcome = "win" if pnl > 0 else "loss"
                    stats[outcome + "s"] += 1
                    _append(log_path, {
                        "type": "simulated_exit",
                        "ts": now,
                        "slug": slug,
                        "reason": exit_reason,
                        "side": position.side,
                        "entry_bid": position.entry_bid,
                        "exit_bid": exit_bid,
                        "max_bid": position.max_bid,
                        "min_bid": position.min_bid,
                        "entry_secs": position.entry_secs,
                        "exit_secs": current_secs,
                        "hold_secs": round(now - position.entry_ts, 1),
                        "pnl": pnl,
                        "bet_usd": BET_USD,
                        "outcome": outcome,
                        "running_pnl": round(stats["pnl"], 4),
                    })
                    print(f"  EXIT {outcome.upper():4s} | {slug[-20:]} | "
                          f"entry={position.entry_bid:.3f} exit={exit_bid:.3f} pnl={pnl:+.4f} | {exit_reason}", flush=True)
                    position = None

            # Abre nova posição se não há posição e mercado é mid
            if position is None and is_mid_market:
                # Entra no lado com bid mais baixo (underpriced)
                if up_bid < down_bid:
                    entry_side = "UP"
                    entry_bid = up_bid
                else:
                    entry_side = "DOWN"
                    entry_bid = down_bid

                if entry_bid >= 0.35:  # mínimo de liquidez esperado
                    position = _MMPosition(slug, entry_side, entry_bid, current_secs,
                                           TARGET_TICKS, STOP_TICKS)
                    stats["would_post"] += 1
                    _append(log_path, {
                        "type": "would_post",
                        "ts": now,
                        "slug": slug,
                        "side": entry_side,
                        "entry_bid": entry_bid,
                        "target_bid": position.target_bid,
                        "stop_bid": position.stop_bid,
                        "current_secs": current_secs,
                        "up_bid": up_bid,
                        "down_bid": down_bid,
                        "up_mid": up_mid,
                        "down_mid": down_mid,
                        "total_would_post": stats["would_post"],
                    })
                    print(f"  ENTER | {slug[-20:]} | side={entry_side} bid={entry_bid:.3f} "
                          f"target={position.target_bid:.3f} stop={position.stop_bid:.3f} "
                          f"secs={current_secs}", flush=True)

        except Exception as e:
            _append(log_path, {"type": "error", "ts": now, "error": str(e)})

        time.sleep(poll_secs)

    print(f"\n[MARKET_MAKER] Encerrado | would_post={stats['would_post']} "
          f"wins={stats['wins']} losses={stats['losses']} pnl={stats['pnl']:+.4f}", flush=True)
