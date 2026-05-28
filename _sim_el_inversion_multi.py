"""
_sim_el_inversion_multi.py
Simula o setup Early Leader Inversion nos logs do multi_coin_observer.

Lógica:
  1. Detecta early leader na janela secs 181-240 (bid médio >= 0.55)
  2. Detecta inversão: lado oposto assume bid [0.60, 0.72] com gap >= 0.03
     dentro de secs [15, 60]
  3. Simula entrada ao bid do novo líder (otimista, sem slippage)
  4. Saída: TP=0.85, stop=0.55, ou secs<=5

Uso:
  python _sim_el_inversion_multi.py
  python _sim_el_inversion_multi.py --logs logs/multi_coin_observer_*/observer.jsonl
  python _sim_el_inversion_multi.py --tp 0.85 --stop 0.50

Relatório: WR, PnL, avg por coin, timeframe e faixa de parâmetros.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import io
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Parâmetros default (do repositório de pesquisa)
# ---------------------------------------------------------------------------
EL_MIN_BID       = 0.55
INV_MIN_NEW_BID  = 0.60
INV_MAX_NEW_BID  = 0.72
INV_MIN_FLIP_GAP = 0.03
INV_MAX_ENTRY    = 0.65   # bid máximo de entrada (proxy para maxEntryAsk)
INV_MIN_SECS     = 15
INV_MAX_SECS     = 60
TP_BID           = 0.85
STOP_BID         = 0.55
EXIT_SECS        = 5
STAKE            = 5.0   # USDC paper


# ---------------------------------------------------------------------------
# Estrutura de estado por slug
# ---------------------------------------------------------------------------

class _SlugState:
    def __init__(self, slug: str, coin: str, tf: str):
        self.slug    = slug
        self.coin    = coin
        self.tf      = tf
        self._s240: List[Dict] = []
        self.el_leader: Optional[str] = None
        self.el_bid_240: float = 0.0
        # posição aberta
        self.in_trade:    bool  = False
        self.entry_bid:   float = 0.0
        self.entry_secs:  int   = 0
        self.trade_side:  Optional[str] = None  # UP ou DOWN (novo líder)
        self.best_bid:    float = 0.0
        self.worst_bid:   float = 1.0
        # resultado
        self.closed_trades: List[Dict] = []

    # ---- EL detection ----
    def feed(self, secs: Optional[int], up_bid: float, down_bid: float) -> Optional[Dict]:
        """Processa um snap. Retorna trade fechado se ocorreu, ou None."""
        if secs is None:
            return None

        # Janela EL
        if 181 <= secs <= 240:
            self._s240.append({"up_bid": up_bid, "down_bid": down_bid})
            self._compute_el()
            return None

        # Dentro de trade aberto → monitorar saída
        if self.in_trade:
            return self._check_exit(secs, up_bid, down_bid)

        # Fora de trade → checar entrada por inversão
        if self.el_leader and INV_MIN_SECS <= secs <= INV_MAX_SECS:
            return self._check_entry(secs, up_bid, down_bid)

        return None

    def _compute_el(self) -> None:
        avg_up = sum(s["up_bid"]   for s in self._s240) / len(self._s240)
        avg_dn = sum(s["down_bid"] for s in self._s240) / len(self._s240)
        if avg_up >= EL_MIN_BID and avg_up >= avg_dn:
            self.el_leader  = "UP"
            self.el_bid_240 = round(avg_up, 4)
        elif avg_dn >= EL_MIN_BID and avg_dn > avg_up:
            self.el_leader  = "DOWN"
            self.el_bid_240 = round(avg_dn, 4)

    def _check_entry(self, secs: int, up_bid: float, down_bid: float) -> None:
        opp = "DOWN" if self.el_leader == "UP" else "UP"
        opp_bid = down_bid if opp == "DOWN" else up_bid
        el_bid  = up_bid  if self.el_leader == "UP" else down_bid
        gap = round(opp_bid - el_bid, 4)

        if (INV_MIN_NEW_BID <= opp_bid <= INV_MAX_NEW_BID
                and gap >= INV_MIN_FLIP_GAP
                and opp_bid <= INV_MAX_ENTRY):
            self.in_trade   = True
            self.entry_bid  = opp_bid
            self.entry_secs = secs
            self.trade_side = opp
            self.best_bid   = opp_bid
            self.worst_bid  = opp_bid
        return None

    def _check_exit(self, secs: int, up_bid: float, down_bid: float) -> Optional[Dict]:
        cur_bid = down_bid if self.trade_side == "DOWN" else up_bid
        self.best_bid  = max(self.best_bid,  cur_bid)
        self.worst_bid = min(self.worst_bid, cur_bid)

        reason = None
        exit_bid = cur_bid

        if cur_bid >= TP_BID:
            reason = "tp"
        elif cur_bid <= STOP_BID:
            reason = "stop"
        elif secs <= EXIT_SECS:
            reason = "near_end"

        if reason:
            shares  = STAKE / self.entry_bid
            pnl     = round(shares * exit_bid - STAKE, 4)
            trade   = {
                "slug":        self.slug,
                "coin":        self.coin,
                "tf":          self.tf,
                "el_leader":   self.el_leader,
                "el_bid_240":  self.el_bid_240,
                "new_leader":  self.trade_side,
                "entry_bid":   self.entry_bid,
                "entry_secs":  self.entry_secs,
                "exit_bid":    round(exit_bid, 4),
                "exit_secs":   secs,
                "exit_reason": reason,
                "best_bid":    round(self.best_bid, 4),
                "worst_bid":   round(self.worst_bid, 4),
                "pnl":         pnl,
                "win":         pnl > 0,
            }
            self.closed_trades.append(trade)
            self._reset_trade()
            return trade
        return None

    def _reset_trade(self) -> None:
        self.in_trade   = False
        self.entry_bid  = 0.0
        self.entry_secs = 0
        self.trade_side = None
        self.best_bid   = 0.0
        self.worst_bid  = 1.0

    def reset_candle(self) -> None:
        """Reinicia para novo candle (mesmo coin/tf, novo slug)."""
        self._s240      = []
        self.el_leader  = None
        self.el_bid_240 = 0.0
        self._reset_trade()


# ---------------------------------------------------------------------------
# Leitura de logs
# ---------------------------------------------------------------------------

def load_snaps(log_files: List[str]) -> List[Dict]:
    snaps = []
    for path in log_files:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("type") == "multi_snap":
                        snaps.append(d)
                except Exception:
                    pass
    snaps.sort(key=lambda x: x.get("ts", 0))
    return snaps


# ---------------------------------------------------------------------------
# Simulação
# ---------------------------------------------------------------------------

def simulate(snaps: List[Dict]) -> List[Dict]:
    states: Dict[Tuple[str, str], _SlugState] = {}   # (coin, tf) → state
    all_trades: List[Dict] = []

    for snap in snaps:
        coin = snap.get("coin", "")
        tf   = snap.get("tf", "")
        slug = snap.get("slug", "")
        secs = snap.get("secs")
        up_bid   = snap.get("up_bid", 0.0)
        down_bid = snap.get("down_bid", 0.0)

        key = (coin, tf)
        state = states.get(key)

        # Slug mudou → novo candle
        if state is None or state.slug != slug:
            if state and state.in_trade:
                # fecha trade aberto ao fim do candle anterior
                trade = {
                    "slug":        state.slug,
                    "coin":        state.coin,
                    "tf":          state.tf,
                    "el_leader":   state.el_leader,
                    "el_bid_240":  state.el_bid_240,
                    "new_leader":  state.trade_side,
                    "entry_bid":   state.entry_bid,
                    "entry_secs":  state.entry_secs,
                    "exit_bid":    state.entry_bid,   # sem data final
                    "exit_secs":   0,
                    "exit_reason": "candle_end",
                    "best_bid":    state.best_bid,
                    "worst_bid":   state.worst_bid,
                    "pnl":         round((state.entry_bid / state.entry_bid - 1) * STAKE, 4),
                    "win":         False,
                }
                all_trades.append(trade)
            states[key] = _SlugState(slug, coin, tf)
            state = states[key]

        trade = state.feed(secs, up_bid, down_bid)
        if trade:
            all_trades.append(trade)

    return all_trades


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

def _fmt_pct(n: int, tot: int) -> str:
    return f"{100 * n / tot:.1f}%" if tot else "—"


def report(trades: List[Dict]) -> None:
    if not trades:
        print("Nenhum trade fechado.")
        return

    SEP = "─" * 68

    def section(label: str, subset: List[Dict]) -> None:
        if not subset:
            return
        wins  = [t for t in subset if t["win"]]
        pnl   = sum(t["pnl"] for t in subset)
        n     = len(subset)
        wr    = 100 * len(wins) / n if n else 0
        avg   = pnl / n if n else 0
        stops = [t for t in subset if t["exit_reason"] == "stop"]
        tps   = [t for t in subset if t["exit_reason"] == "tp"]
        ends  = [t for t in subset if t["exit_reason"] in ("near_end", "candle_end")]
        print(f"  {label:<30} n={n:>4}  WR={wr:>5.1f}%  PnL={pnl:>+8.2f}  avg={avg:>+7.3f}")
        print(f"    TP={len(tps)}  STOP={len(stops)}  near_end={len(ends)}")

    print()
    print("=" * 68)
    print("  EL INVERSION MULTI — SIMULAÇÃO")
    print("=" * 68)
    print(f"  Trades fechados: {len(trades)}")
    print(f"  Params: entry bid [{INV_MIN_NEW_BID},{INV_MAX_NEW_BID}] gap>={INV_MIN_FLIP_GAP}"
          f" TP={TP_BID} stop={STOP_BID} secs [{INV_MIN_SECS},{INV_MAX_SECS}]")
    print(SEP)

    print("\n  Por coin + timeframe:")
    from itertools import groupby
    key_fn = lambda t: (t["coin"], t["tf"])
    for (coin, tf), grp in groupby(sorted(trades, key=key_fn), key=key_fn):
        section(f"{coin.upper()}-{tf}", list(grp))

    print(f"\n{SEP}")
    print("\n  Por motivo de saída:")
    for reason in ("tp", "stop", "near_end", "candle_end"):
        sub = [t for t in trades if t["exit_reason"] == reason]
        if sub:
            wins = [t for t in sub if t["win"]]
            pnl  = sum(t["pnl"] for t in sub)
            print(f"  {reason:<12}  n={len(sub):>4}  WR={_fmt_pct(len(wins), len(sub)):>6}  PnL={pnl:>+8.2f}")

    print(f"\n{SEP}")
    print("\n  Por faixa entry_bid:")
    for lo, hi in [(0.60, 0.62), (0.62, 0.64), (0.64, 0.66), (0.66, 0.69), (0.69, 0.72)]:
        sub = [t for t in trades if lo <= t["entry_bid"] < hi]
        if sub:
            wins = [t for t in sub if t["win"]]
            pnl  = sum(t["pnl"] for t in sub)
            print(f"  [{lo:.2f},{hi:.2f})  n={len(sub):>4}  WR={_fmt_pct(len(wins), len(sub)):>6}  PnL={pnl:>+8.2f}")

    print(f"\n{SEP}")
    print("\n  Por faixa entry_secs:")
    for lo, hi in [(60, 55), (55, 45), (45, 35), (35, 25), (25, 15)]:
        sub = [t for t in trades if hi <= t["entry_secs"] < lo]
        if sub:
            wins = [t for t in sub if t["win"]]
            pnl  = sum(t["pnl"] for t in sub)
            print(f"  secs [{hi},{lo})  n={len(sub):>4}  WR={_fmt_pct(len(wins), len(sub)):>6}  PnL={pnl:>+8.2f}")

    print(f"\n{SEP}")
    print("\n  Todos (total):")
    section("TOTAL", trades)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Simula EL Inversion nos logs do observer")
    parser.add_argument("--logs",  nargs="*", help="Arquivos JSONL (glob aceito)")
    parser.add_argument("--tp",    type=float, default=TP_BID,   help="Take profit bid")
    parser.add_argument("--stop",  type=float, default=STOP_BID, help="Stop bid")
    parser.add_argument("--stake", type=float, default=STAKE,    help="Stake USDC por trade")
    args = parser.parse_args()

    global TP_BID, STOP_BID, STAKE
    TP_BID   = args.tp
    STOP_BID = args.stop
    STAKE    = args.stake

    if args.logs:
        paths = []
        for pattern in args.logs:
            paths.extend(glob.glob(pattern, recursive=True))
    else:
        paths = sorted(glob.glob("logs/multi_coin_observer_*/observer.jsonl"))

    if not paths:
        print("Nenhum log encontrado. Rode o observer primeiro.")
        return 1

    print(f"[SIM] Arquivos: {len(paths)}")
    for p in paths:
        print(f"  {p}")

    snaps  = load_snaps(paths)
    print(f"[SIM] Snaps carregados: {len(snaps)}")

    trades = simulate(snaps)
    report(trades)
    return 0


if __name__ == "__main__":
    sys.exit(main())
