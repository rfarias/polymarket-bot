"""
market/live_el_flip_paper_v1.py

Runner paper da estratégia EL Flip (Early Leader Inversion).

Tese: quando o early leader original perde a liderança e o lado oposto assume
com bid em [0.60, 0.72] com gap >= 0.35 (diferença para o EL original), o novo
líder vence ~90% das vezes. Estratégia: comprar o novo líder, segurar até resolução.

Parâmetros validados:
  - gap >= 0.35 (dominante): WR 90%, avg +$0.408 (110 trades, market_monitor 20-21/05)
  - Sem stop: segura até resolução (stop piora WR por falso acionamento)
  - TP opcional: 0.85 (saída antecipada quando vencedor claro)

Documentado: TESTES_ANALISE_EL.md seção 15 + simulação de confirmação 29/05.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from market.rest_5m_shadow_public_v5 import (
    _build_slot_bundle,
    _fetch_slot_state,
    _slot_snapshot,
)

# ---------------------------------------------------------------------------
# Parâmetros
# ---------------------------------------------------------------------------
EL_MIN_BID   = 0.55   # mínimo para detectar early leader em secs 181-240
FLIP_GAP     = 0.35   # gap mínimo (opp_bid - orig_bid) para flip dominante
ENTRY_LO     = 0.60   # bid mínimo do novo líder na entrada
ENTRY_HI     = 0.72   # bid máximo do novo líder na entrada
TP_BID       = 0.85   # take profit antecipado (antes de secs <= 5)
EXIT_SECS    = 5      # secs para saída final por resolução
QTY          = 6.0


def _build_log_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"el_flip_paper_{ts}"


# ---------------------------------------------------------------------------
# Tracker por slug
# ---------------------------------------------------------------------------

class _ELFlipTracker:
    """Detecta EL original + inversão dominante para um slug BTC 5m."""

    def __init__(self) -> None:
        self._slug: Optional[str] = None
        self._s240: list = []          # bids na janela 181-240s
        self.el_side: Optional[str]  = None   # lado original (UP/DOWN)
        self.el_bid_240: float       = 0.0
        self.in_trade: bool          = False
        self.entry_price: float      = 0.0
        self.entry_secs:  int        = 0
        self.trade_side: Optional[str] = None  # novo líder (flip)
        self.best_bid:  float        = 0.0
        self.worst_bid: float        = 1.0

    def update(
        self,
        slug: str,
        secs: Optional[int],
        up_bid: float,
        down_bid: float,
    ) -> Optional[str]:
        """Processa snap. Retorna 'ENTRY', 'WIN_RESOLVE', 'LOSS_RESOLVE' ou None."""
        if secs is None:
            return None
        if self._slug != slug:
            self._reset(slug)

        # ── Fase 1: detectar EL original (secs 181-240) ──────────────────
        if 181 <= secs <= 240:
            self._s240.append({"up": up_bid, "dn": down_bid})
            self._compute_el()

        # ── Fase 2: detectar flip (após EL conhecido, qualquer secs) ─────
        if self.el_side and not self.in_trade:
            orig_bid = up_bid   if self.el_side == "UP"   else down_bid
            opp_bid  = down_bid if self.el_side == "UP"   else up_bid
            opp_side = "DOWN"   if self.el_side == "UP"   else "UP"
            gap = round(opp_bid - orig_bid, 4)
            if ENTRY_LO <= opp_bid <= ENTRY_HI and gap >= FLIP_GAP:
                self.in_trade    = True
                self.entry_price = round(opp_bid, 4)
                self.entry_secs  = secs
                self.trade_side  = opp_side
                self.best_bid    = opp_bid
                self.worst_bid   = opp_bid
                return "ENTRY"

        # ── Fase 3: monitorar posição aberta ─────────────────────────────
        if self.in_trade and self.trade_side:
            my_bid  = up_bid   if self.trade_side == "UP"   else down_bid
            opp_bid = down_bid if self.trade_side == "UP"   else up_bid
            self.best_bid  = max(self.best_bid,  my_bid)
            self.worst_bid = min(self.worst_bid, my_bid)

            # TP antecipado
            if my_bid >= TP_BID:
                return "TP_WIN"

            # Saída por resolução final
            if secs <= EXIT_SECS:
                if my_bid >= 0.90:
                    return "WIN_RESOLVE"
                if opp_bid >= 0.90:
                    return "LOSS_RESOLVE"

        return None

    def _compute_el(self) -> None:
        if not self._s240:
            return
        avg_up = sum(s["up"] for s in self._s240) / len(self._s240)
        avg_dn = sum(s["dn"] for s in self._s240) / len(self._s240)
        if avg_up >= EL_MIN_BID and avg_up >= avg_dn:
            self.el_side    = "UP"
            self.el_bid_240 = round(avg_up, 4)
        elif avg_dn >= EL_MIN_BID and avg_dn > avg_up:
            self.el_side    = "DOWN"
            self.el_bid_240 = round(avg_dn, 4)

    def _reset(self, slug: str) -> None:
        self._slug       = slug
        self._s240       = []
        self.el_side     = None
        self.el_bid_240  = 0.0
        self.in_trade    = False
        self.entry_price = 0.0
        self.entry_secs  = 0
        self.trade_side  = None
        self.best_bid    = 0.0
        self.worst_bid   = 1.0

    def state_dict(self) -> dict:
        return {
            "el_side":    self.el_side,
            "el_bid_240": self.el_bid_240,
            "in_trade":   self.in_trade,
            "trade_side": self.trade_side,
            "entry_price": self.entry_price,
            "entry_secs":  self.entry_secs,
        }


# ---------------------------------------------------------------------------
# Runner principal
# ---------------------------------------------------------------------------

def run_el_flip_paper_v1(
    run_seconds: int = 3600,
    poll_secs: float = 1.5,
    qty: float = QTY,
) -> None:
    log_dir  = _build_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "el_flip_paper.jsonl"

    print(f"[EL_FLIP] Log: {log_path}")
    print(f"[EL_FLIP] Parâmetros: gap>={FLIP_GAP}  entry=[{ENTRY_LO},{ENTRY_HI}]  TP={TP_BID}  qty={qty}")

    trackers:  dict[str, _ELFlipTracker] = {}
    session_id = f"el_flip_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    session_pnl = 0.0
    session_entries = session_wins = session_losses = 0
    deadline = time.time() + run_seconds

    def _log(event: dict) -> None:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    _log({"type": "startup", "ts": time.time(), "session_id": session_id,
          "params": {"flip_gap": FLIP_GAP, "entry_lo": ENTRY_LO,
                     "entry_hi": ENTRY_HI, "tp": TP_BID, "qty": qty}})

    while time.time() < deadline:
        try:
            slot_bundle = _build_slot_bundle()
        except Exception as exc:
            print(f"[EL_FLIP] bundle error: {exc}")
            time.sleep(poll_secs)
            continue

        for slot_name in ("current",):
            try:
                slot_state = _fetch_slot_state(slot_bundle, slot_name)
                snap       = _slot_snapshot(slot_state, slot_name)
            except Exception:
                continue

            if not snap:
                continue

            slug    = snap.get("slug", "")
            secs    = snap.get("secs_to_end")
            up_bid  = float(snap.get("up_bid")   or 0)
            down_bid = float(snap.get("down_bid") or 0)

            if not slug or not secs:
                continue

            tracker = trackers.setdefault(slug, _ELFlipTracker())
            result  = tracker.update(slug, secs, up_bid, down_bid)

            if result == "ENTRY":
                session_entries += 1
                flip_gap = round(
                    (down_bid if tracker.el_side == "UP" else up_bid) -
                    (up_bid   if tracker.el_side == "UP" else down_bid), 4)
                _log({
                    "type": "el_flip_entry", "ts": time.time(),
                    "session_id": session_id, "slug": slug,
                    "el_side": tracker.el_side, "el_bid_240": tracker.el_bid_240,
                    "new_side": tracker.trade_side,
                    "ep": tracker.entry_price, "secs": secs,
                    "gap": flip_gap, "qty": qty,
                })
                print(f"[EL_FLIP] ENTRY  {tracker.trade_side}  ep={tracker.entry_price:.3f}"
                      f"  gap={flip_gap:.3f}  secs={secs}  slug=..{slug[-12:]}")

            elif result in ("WIN_RESOLVE", "TP_WIN", "LOSS_RESOLVE") and tracker.in_trade:
                is_win = result in ("WIN_RESOLVE", "TP_WIN")
                if result == "TP_WIN":
                    exit_price = TP_BID
                elif result == "WIN_RESOLVE":
                    exit_price = 1.0
                else:
                    exit_price = 0.0
                pnl = round((exit_price - tracker.entry_price) * qty, 4)
                session_pnl += pnl
                if is_win:
                    session_wins += 1
                else:
                    session_losses += 1
                _log({
                    "type": "el_flip_closed", "ts": time.time(),
                    "session_id": session_id, "slug": slug,
                    "outcome": result, "new_side": tracker.trade_side,
                    "ep": tracker.entry_price, "xp": exit_price,
                    "entry_secs": tracker.entry_secs, "exit_secs": secs,
                    "pnl_usd": pnl, "qty": qty,
                    "best_bid": round(tracker.best_bid, 4),
                    "worst_bid": round(tracker.worst_bid, 4),
                    "session_pnl": round(session_pnl, 4),
                    "session_wins": session_wins,
                    "session_losses": session_losses,
                })
                wr = 100 * session_wins / (session_wins + session_losses) if (session_wins + session_losses) else 0
                print(f"[EL_FLIP] {result}  pnl={pnl:+.2f}  ep={tracker.entry_price:.3f}"
                      f"  xp={exit_price:.3f}  secs={secs}"
                      f"  session: W={session_wins} L={session_losses} WR={wr:.0f}% PnL={session_pnl:+.2f}")
                tracker._reset(slug)

        # Display compacto
        in_trade_slugs = [(s, t) for s, t in trackers.items() if t.in_trade]
        if in_trade_slugs:
            slug, t = in_trade_slugs[0]
            print(f"[EL_FLIP] holding  {t.trade_side}  ep={t.entry_price:.3f}"
                  f"  best={t.best_bid:.3f}  worst={t.worst_bid:.3f}"
                  f"  secs=?  slug=..{slug[-12:]}")

        time.sleep(poll_secs)

    print(f"\n[EL_FLIP] Sessão encerrada. "
          f"Entradas={session_entries}  W={session_wins}  L={session_losses}  PnL={session_pnl:+.2f}")
