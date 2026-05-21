"""
run_market_monitor.py

Monitor contínuo de mercado + spot BTC.

Coleta em tempo real:
  - Books UP/DOWN dos mercados 5m ativos (current + next_1 + next_2)
  - Spot BTC (Binance + Coinbase)
  - Oracle Chainlink (preço de resolução)
  - Momentum spot e lag spot→token (via CurrentScalpResearchV1)

Sinaliza padrões para cada setup:
  [AR]  almost_resolved  — winner >= 0.88, secs <= 120
  [MM]  market_maker     — mid in [0.38, 0.62], secs in [60, 240]
  [SC]  spot_scalp       — spot_delta_15s >= 2bps, lag >= 2bps
  [RS]  reversal_sniper  — winner >= 0.88, loser <= 0.15, winner caindo

Log JSONL em logs/market_monitor_YYYYMMDD_HHMMSS.jsonl

Uso:
  python run_market_monitor.py
  python run_market_monitor.py --poll 0.5
  python run_market_monitor.py --no-log
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))

# Force UTF-8 stdout on Windows so box-drawing chars print correctly
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from market.rest_5m_shadow_public_v5 import (
    _build_slot_bundle,
    _compute_executable_metrics,
    _fetch_slot_state,
    _slot_snapshot,
)
from market.current_scalp_signal_v1 import (
    CurrentScalpResearchV1,
    fetch_external_btc_reference_v1,
)
from market.chainlink_oracle import ChainlinkBTCOracle

try:
    from market.btc_chart_context_v1 import fetch_btc_chart_context
    _HAS_CHART = True
except Exception:
    _HAS_CHART = False

# ---------------------------------------------------------------------------
# Thresholds de sinal
# ---------------------------------------------------------------------------

AR_MIN_WINNER   = 0.88
AR_MAX_SECS     = 120
MM_MID_MIN      = 0.38
MM_MID_MAX      = 0.62
MM_MIN_SECS     = 60
MM_MAX_SECS     = 240
SC_MIN_SPOT_BPS = 2.0
SC_MIN_LAG_BPS  = 2.0
SC_MIN_SECS     = 45
SC_MAX_SECS     = 240
RS_MIN_WINNER   = 0.88
RS_MAX_LOSER    = 0.15
RS_VELOCITY_MIN = 0.005  # queda mínima do winner em unidades absolutas/s (≈50 bps/s, ~1 cent em 2s)

# Early Entry (EE) — entrada antecipada com EL estável
EE_EL_MIN    = 0.55   # bid mínimo para detectar early leader em 240-181s
EE_CONT_MIN  = 0.70   # bid mínimo de continuidade do EL em 180-121s
EE_VEL_MIN   = 0.08   # crescimento mínimo do EL bid (bid_180 - bid_240)
EE_ENTRY_LO  = 0.82   # faixa de entrada: baixo
EE_ENTRY_HI  = 0.86   # faixa de entrada: alto
EE_HEDGE_THR = 0.50   # cruzamento para ativar hedge (compra lado oposto)
EE_MAX_SECS  = 180    # secs máximo para entrada EE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sf(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


def _end_date_to_start_iso(end_date_iso: str, duration_secs: int = 300) -> str:
    """Deriva o startDate subtraindo duração do endDate."""
    try:
        end_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        start_dt = end_dt - timedelta(seconds=duration_secs)
        return start_dt.isoformat().replace("+00:00", "Z")
    except Exception:
        return end_date_iso


def _executable_prices(snap: Dict) -> Tuple[float, float, float, float]:
    """Retorna (up_bid, up_ask, down_bid, down_ask) usando preços executáveis."""
    up   = snap.get("up")   or {}
    down = snap.get("down") or {}
    up_bid   = _sf(up.get("executable_buy")  or up.get("best_bid"),   0.0)
    up_ask   = _sf(up.get("executable_sell") or up.get("best_ask"),   0.0)
    down_bid = _sf(down.get("executable_buy")  or down.get("best_bid"), 0.0)
    down_ask = _sf(down.get("executable_sell") or down.get("best_ask"), 0.0)
    return up_bid, up_ask, down_bid, down_ask


def _depth_top3(snap: Dict) -> Tuple[float, float]:
    """Retorna (sum_bids_depth, sum_asks_depth) top 3 níveis."""
    def _sum(levels):
        return sum(_sf((l or {}).get("size"), 0) for l in (levels or [])[:3])
    up   = snap.get("up")   or {}
    down = snap.get("down") or {}
    bids = _sum(up.get("top_bids")) + _sum(down.get("top_bids"))
    asks = _sum(up.get("top_asks")) + _sum(down.get("top_asks"))
    return bids, asks


# ---------------------------------------------------------------------------
# Histórico de bids por slug (velocity do winner)
# ---------------------------------------------------------------------------

class _BidHistory:
    def __init__(self, maxlen: int = 30):
        from collections import deque
        self._up: deque   = deque(maxlen=maxlen)   # (ts, bid)
        self._down: deque = deque(maxlen=maxlen)

    def push(self, ts: float, up_bid: float, down_bid: float) -> None:
        self._up.append((ts, up_bid))
        self._down.append((ts, down_bid))

    def velocity(self, side: str, window_secs: float = 10.0) -> float:
        hist = self._up if side == "UP" else self._down
        if len(hist) < 2:
            return 0.0
        now_ts, now_bid = hist[-1]
        cutoff = now_ts - window_secs
        ref_ts, ref_bid = hist[0]
        for ts, bid in hist:
            if ts >= cutoff:
                ref_ts, ref_bid = ts, bid
                break
        elapsed = now_ts - ref_ts
        if elapsed < 0.5:
            return 0.0
        return round((now_bid - ref_bid) / elapsed, 6)


# ---------------------------------------------------------------------------
# Early Leader Tracker — acumula snaps por janela e computa EL + F3 + vel
# ---------------------------------------------------------------------------

class _EarlyLeaderTracker:
    def __init__(self):
        self._slug: Optional[str] = None
        self._s240: list = []  # snaps secs 181-240
        self._s180: list = []  # snaps secs 121-180
        self.early_leader:  Optional[str] = None
        self.el_bid_240:    float = 0.0
        self.el_bid_180:    float = 0.0
        self.el_vel:        float = 0.0   # bid_180 - bid_240
        self.cont_ok:       bool  = False

    def update(self, slug: str, secs: Optional[int],
               up_bid: float, down_bid: float) -> None:
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
            self.early_leader = "UP";   self.el_bid_240 = avg_up
        elif avg_dn >= EE_EL_MIN:
            self.early_leader = "DOWN"; self.el_bid_240 = avg_dn

    def _compute_f3(self) -> None:
        if not (self.early_leader and self._s180):
            return
        bids = [s["up_bid"] if self.early_leader == "UP" else s["down_bid"]
                for s in self._s180]
        self.el_bid_180 = sum(bids) / len(bids)
        self.el_vel     = round(self.el_bid_180 - self.el_bid_240, 4)
        self.cont_ok    = min(bids) >= EE_CONT_MIN

    def _reset(self, slug: str) -> None:
        self._slug         = slug
        self._s240         = []
        self._s180         = []
        self.early_leader  = None
        self.el_bid_240    = 0.0
        self.el_bid_180    = 0.0
        self.el_vel        = 0.0
        self.cont_ok       = False

    @property
    def signal_ok(self) -> bool:
        """True quando EL estável (F3) e velocidade suficiente."""
        return bool(
            self.early_leader
            and self.cont_ok
            and self.el_vel >= EE_VEL_MIN
        )


# ---------------------------------------------------------------------------
# Early Entry Position — máquina de estado da posição paper EE
# ---------------------------------------------------------------------------

class _EEPosition:
    """
    Estados:
      none    — sem posição
      entry   — comprado no EL side entre 0.82-0.86
      hedged  — cruzou 0.50; comprado também no lado oposto
      closed  — mercado resolvido ou saída
    """
    def __init__(self):
        self.state:       str            = "none"
        self.entry_side:  Optional[str]  = None
        self.entry_ep:    float          = 0.0
        self.entry_ts:    float          = 0.0
        self.entry_secs:  int            = 0
        self.hedge_ep:    float          = 0.0
        self.hedge_ts:    float          = 0.0
        self.hedge_secs:  int            = 0
        self.outcome:     str            = ""
        self.pnl:         float          = 0.0
        self.qty:         float          = 3.0   # metade do qty padrão
        self._just_closed: bool          = False

    def open_entry(self, side: str, ep: float, ts: float, secs: int) -> None:
        self.state       = "entry"
        self.entry_side  = side
        self.entry_ep    = ep
        self.entry_ts    = ts
        self.entry_secs  = secs

    def open_hedge(self, ep: float, ts: float, secs: int) -> None:
        self.state      = "hedged"
        self.hedge_ep   = ep
        self.hedge_ts   = ts
        self.hedge_secs = secs

    def close(self, outcome: str, exit_ep: float, hedge_exit_ep: float = 0.0) -> None:
        self.state        = "closed"
        self.outcome      = outcome
        self._just_closed = True
        pnl_entry = (exit_ep - self.entry_ep) * self.qty
        pnl_hedge = (hedge_exit_ep - self.hedge_ep) * self.qty if self.hedge_ep > 0 else 0.0
        self.pnl  = round(pnl_entry + pnl_hedge, 4)

    def reset(self) -> None:
        self.__init__()


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------

def _make_log_path() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = Path("logs") / f"market_monitor_{ts}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _append_log(path: Optional[Path], row: dict) -> None:
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

_R = "\033[0m"
_B = "\033[1m"
_G = "\033[92m"
_Y = "\033[93m"
_RE = "\033[91m"
_C = "\033[96m"
_D = "\033[2m"


def _c(txt: str, col: str) -> str:
    return f"{col}{txt}{_R}"


def _bid_colored(bid: float) -> str:
    if bid >= AR_MIN_WINNER:
        return _c(f"{bid:.3f}", _G)
    if bid <= RS_MAX_LOSER and bid > 0:
        return _c(f"{bid:.3f}", _RE)
    return f"{bid:.3f}"


def _flag(on: bool, tag: str) -> str:
    return _c(f"[{tag}]", _G if on else _D)


def _print_panel(
    cycle: int,
    now_str: str,
    log_name: str,
    spot_ref: float,
    spot_bnb: float,
    spot_cb: float,
    spot_div: float,
    oracle_price: Optional[float],
    oracle_stale: Optional[float],
    chart_summary: Optional[str],
    slots_data: list,           # list of dicts com tudo por slot
    recent: Deque[str],
) -> None:
    os.system("cls" if os.name == "nt" else "clear")

    # header
    spot_s = _c(f"${spot_ref:,.2f}", _C) if spot_ref else _c("Spot=?", _D)
    div_s  = f"div={spot_div:.1f}bps" if spot_div else ""
    bnb_s  = f"BNB={spot_bnb:,.1f}" if spot_bnb else ""
    cb_s   = f"CB={spot_cb:,.1f}" if spot_cb else ""
    if oracle_price:
        ora_s = f"Oracle=${oracle_price:,.2f}"
        if oracle_stale and oracle_stale > 30:
            ora_s = _c(ora_s + f" stale={oracle_stale:.0f}s", _Y)
    else:
        ora_s = _c("Oracle=?", _D)

    print(_c(f"╔══ MARKET MONITOR  {now_str}  ciclo={cycle} ══╗", _B))
    print(f"  {spot_s}  {bnb_s}  {cb_s}  {div_s}  {ora_s}")
    if chart_summary:
        print(_c(f"  {chart_summary}", _D))
    print()

    if not slots_data:
        print("  Aguardando mercados...")
    else:
        for sd in slots_data:
            slot     = sd["slot"]
            slug     = sd["slug"]
            secs     = sd["secs"]
            up_bid   = sd["up_bid"];   up_ask   = sd["up_ask"]
            down_bid = sd["down_bid"]; down_ask = sd["down_ask"]
            sig_ar   = sd["sig_ar"];   sig_mm   = sd["sig_mm"]
            sig_sc   = sd["sig_sc"];   sig_rs   = sd["sig_rs"]
            sc_ev    = sd.get("sc_ev") or {}

            secs_s = f"{secs}s" if secs is not None else "?"
            print(f"  {_c(slot.upper(), _B):15s} {slug[-35:]:35s}  secs={secs_s:>5}")
            print(f"    UP   bid={_bid_colored(up_bid)}  ask={up_ask:.3f}  "
                  f"spread={up_ask-up_bid:.3f}")
            print(f"    DOWN bid={_bid_colored(down_bid)}  ask={down_ask:.3f}  "
                  f"spread={down_ask-down_bid:.3f}")

            sig_ee_disp = sd.get("sig_ee", False)
            ee_pos_disp = sd.get("ee_pos")
            ee_active   = ee_pos_disp and ee_pos_disp.state in ("entry", "hedged")
            flags = " ".join([
                _flag(sig_ar, "AR"),
                _flag(sig_mm, "MM"),
                _flag(sig_sc, "SC"),
                _flag(sig_rs, "RS"),
                _flag(sig_ee_disp or ee_active, "EE"),
            ])
            print(f"    Sinais: {flags}")

            # detalhes ativos
            if sig_ar:
                side   = sd.get("ar_side", "?")
                wbid   = sd.get("ar_winner_bid", 0)
                lbid   = sd.get("ar_loser_bid", 0)
                print(_c(f"    → [AR] {side} winner={wbid:.3f} loser={lbid:.3f}", _G))

            if sig_rs:
                side = sd.get("rs_side", "?")
                vel  = sd.get("rs_vel", 0)
                lbid = sd.get("rs_loser_bid", 0)
                print(_c(f"    → [RS] {side} vel={vel:+.4f} loser={lbid:.3f}", _Y))

            if sig_mm:
                mu = sd.get("mm_mid_up", 0); md = sd.get("mm_mid_down", 0)
                print(_c(f"    → [MM] mid_up={mu:.3f} mid_dn={md:.3f}", _G))

            if sig_sc:
                d15 = sd.get("sc_d15", 0); lag = sd.get("sc_lag", 0)
                side = sd.get("sc_side", "?")
                print(_c(f"    → [SC] {side} spot15s={d15:+.1f}bps lag={lag:.1f}bps", _G))

            # EE — early entry
            ee_pos = ee_pos_disp
            if sig_ee_disp or (ee_pos and ee_pos.state not in ("none", "closed")):
                el  = sd.get("el_leader", "?")
                vel = sd.get("el_vel", 0)
                b180 = sd.get("el_bid_180", 0)
                print(_c(f"    → [EE] EL={el} bid180={b180:.3f} vel={vel:+.3f}", _C))
            if ee_pos and ee_pos.state == "entry":
                wb = sd.get("ee_el_bid", 0)
                print(_c(f"    → [EE] POSIÇÃO ABERTA  {ee_pos.entry_side} "
                          f"ep={ee_pos.entry_ep:.3f}  bid_atual={wb:.3f}  "
                          f"qty={ee_pos.qty}", _G))
            if ee_pos and ee_pos.state == "hedged":
                wb  = sd.get("ee_el_bid", 0)
                wbh = sd.get("ee_hedge_bid", 0)
                print(_c(f"    → [EE] HEDGE ATIVO  orig={ee_pos.entry_side} "
                          f"ep={ee_pos.entry_ep:.3f}→{wb:.3f}  "
                          f"hedge_ep={ee_pos.hedge_ep:.3f}→{wbh:.3f}", _Y))
            if ee_pos and ee_pos.state == "closed":
                col = _G if ee_pos.pnl >= 0 else _RE
                print(_c(f"    → [EE] FECHADO  {ee_pos.outcome}  "
                          f"PnL=${ee_pos.pnl:+.4f}", col))

            # métricas de scalp sempre visíveis
            if sc_ev:
                d5  = _sf(sc_ev.get("spot_delta_5s_bps"),  0)
                d15 = _sf(sc_ev.get("spot_delta_15s_bps"), 0)
                d30 = _sf(sc_ev.get("spot_delta_30s_bps"), 0)
                md5 = _sf(sc_ev.get("market_delta_5s"),    0)
                rng = _sf(sc_ev.get("market_range_15s"),   0)
                dist= _sf(sc_ev.get("distance_from_open_bps"), 0)
                print(f"    Δspot: 5s={d5:+.1f} 15s={d15:+.1f} 30s={d30:+.1f}bps  "
                      f"mktΔ5s={md5:+.4f}  rng15s={rng:.4f}  dist={dist:+.1f}bps")
            print()

    # eventos recentes
    if recent:
        print(_c("  Recentes:", _D))
        for line in recent:
            print(f"  {_D}{line}{_R}")
        print()

    print(_c(f"  Log: {log_name}  |  Ctrl+C para encerrar", _D))


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def run_monitor(poll_secs: float = 1.0, log: bool = True) -> None:
    log_path = _make_log_path() if log else None
    oracle   = ChainlinkBTCOracle()

    # estado persistente
    bid_hists:      Dict[str, _BidHistory]             = {}
    scalp_research: Dict[str, CurrentScalpResearchV1]  = {}
    opening_ref:    Dict[str, float]                   = {}  # slug → spot na abertura
    el_trackers:    Dict[str, _EarlyLeaderTracker]     = {}  # slug → EL tracker
    ee_positions:   Dict[str, _EEPosition]             = {}  # slug → posição EE paper
    recent: Deque[str] = deque(maxlen=10)

    # cache de refresh
    last_slot_refresh   = 0.0
    last_oracle_refresh = 0.0
    last_chart_refresh  = 0.0

    slot_bundle: Dict[str, Any] = {}
    oracle_price: Optional[float] = None
    oracle_stale: Optional[float] = None
    chart_summary: Optional[str]  = None

    spot_ref = spot_bnb = spot_cb = spot_div = 0.0
    cycle = 0

    print("Iniciando monitor...")

    while True:
        try:
            now = time.time()
            cycle += 1

            # --- Slot bundle (mercados ativos) ---
            if now - last_slot_refresh >= 30.0:
                try:
                    slot_bundle = _build_slot_bundle()
                    last_slot_refresh = now
                    # inicializa helpers para slugs novos
                    for sname in ("current", "next_1", "next_2"):
                        slot_info = (slot_bundle.get("slots") or {}).get(sname) or {}
                        item = slot_info.get("item") or {}
                        slug = item.get("slug") or item.get("market_slug") or ""
                        if slug:
                            if slug not in scalp_research:
                                scalp_research[slug] = CurrentScalpResearchV1(history_secs=90)
                            if slug not in bid_hists:
                                bid_hists[slug] = _BidHistory()
                except Exception as e:
                    recent.appendleft(f"[WARN] slot_bundle: {e}")

            # --- Spot BTC ---
            try:
                ref = fetch_external_btc_reference_v1()
                spot_ref = _sf(ref.get("reference_price"), 0)
                srcs     = ref.get("sources") or {}
                spot_bnb = _sf(srcs.get("binance"),  0)
                spot_cb  = _sf(srcs.get("coinbase"), 0)
                spot_div = _sf(ref.get("source_divergence_bps"), 0)
            except Exception as e:
                recent.appendleft(f"[WARN] spot: {e}")

            # --- Oracle ---
            if now - last_oracle_refresh >= 5.0:
                try:
                    op, _, stale = oracle.get_price()
                    oracle_price = op
                    oracle_stale = stale
                    last_oracle_refresh = now
                except Exception as e:
                    recent.appendleft(f"[WARN] oracle: {e}")

            # --- Chart context ---
            if _HAS_CHART and now - last_chart_refresh >= 60.0 and oracle_price and spot_ref:
                try:
                    ctx = fetch_btc_chart_context(
                        current_price=oracle_price,
                        entry_ts=now,
                        event_end_ts=now + 300,
                    )
                    trend = getattr(ctx, "trend_5m", "?")
                    bias  = getattr(ctx, "last_candle_bias", 0.0)
                    pat   = getattr(ctx, "last_candle_pattern", "")
                    al    = getattr(ctx, "is_15m_aligned", False)
                    chart_summary = f"trend5m={trend} bias={bias:+.2f} candle={pat} 15m_aligned={al}"
                    last_chart_refresh = now
                except Exception as e:
                    recent.appendleft(f"[WARN] chart: {e}")

            # --- Slot state (books) ---
            if not slot_bundle:
                time.sleep(poll_secs)
                continue

            try:
                slot_state = _fetch_slot_state(slot_bundle)
            except Exception as e:
                recent.appendleft(f"[WARN] slot_state: {e}")
                time.sleep(poll_secs)
                continue

            slots_info = slot_bundle.get("slots") or {}

            # --- Processa cada slot ---
            slots_data = []

            for sname in ("current", "next_1", "next_2"):
                slot_info = slots_info.get(sname) or {}
                item      = slot_info.get("item") or {}
                slug      = item.get("slug") or item.get("market_slug") or ""
                secs      = item.get("seconds_to_end")
                end_date  = item.get("endDate") or ""

                if not slug:
                    continue

                raw_snap = _slot_snapshot(slot_state, sname)
                if not raw_snap:
                    continue

                # preços executáveis
                up_bid, up_ask, down_bid, down_ask = _executable_prices(raw_snap)
                depth_bids, depth_asks = _depth_top3(raw_snap)

                # bid history + velocity
                bh = bid_hists.setdefault(slug, _BidHistory())
                bh.push(now, up_bid, down_bid)

                # opening_reference para scalp
                if slug not in opening_ref and spot_ref > 0:
                    opening_ref[slug] = spot_ref

                # scalp research evaluate
                sr = scalp_research.get(slug)
                sc_ev = {}
                if sr and spot_ref > 0:
                    try:
                        start_iso = _end_date_to_start_iso(end_date, 300)
                        sc_ev = sr.evaluate(
                            snap=raw_snap,
                            secs_to_end=secs,
                            event_start_time=start_iso,
                            now_ts=now,
                            reference_price=spot_ref,
                            source_divergence_bps=spot_div,
                            opening_reference_price=opening_ref.get(slug, spot_ref),
                        )
                    except Exception as e:
                        recent.appendleft(f"[WARN] scalp_eval {slug[-16:]}: {e}")

                # --- Early Leader Tracker ---
                elt = el_trackers.setdefault(slug, _EarlyLeaderTracker())
                elt.update(slug, secs, up_bid, down_bid)

                # bid do lado do EL (para lógica EE)
                el_bid = (up_bid   if elt.early_leader == "UP"   else
                          down_bid if elt.early_leader == "DOWN" else 0.0)
                # bid do lado oposto (para hedge)
                opp_bid = (down_bid if elt.early_leader == "UP"   else
                           up_bid   if elt.early_leader == "DOWN" else 0.0)

                # --- Early Entry Position ---
                ee = ee_positions.setdefault(slug, _EEPosition())

                # Reset se o mercado terminou e já está em closed há algum tempo
                if ee.state == "closed" and secs is not None and secs > 200:
                    ee.reset()

                # Sinal EE: EL estável + el_vel >= 0.08 + bid EL na faixa
                sig_ee = bool(
                    elt.signal_ok
                    and secs is not None and 30 <= secs <= EE_MAX_SECS
                    and EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI
                    and ee.state == "none"
                )

                # Abrir posição
                if sig_ee:
                    ee.open_entry(elt.early_leader, el_bid, now, secs or 0)
                    recent.appendleft(
                        _c(f"[EE] ENTRADA {elt.early_leader} ep={el_bid:.3f} "
                           f"vel={elt.el_vel:+.3f} secs={secs}", _C)
                    )

                # Monitorar posição aberta
                if ee.state == "entry" and secs is not None:
                    # Cruzou 0.50 → hedge
                    if el_bid < EE_HEDGE_THR and opp_bid > 0:
                        ee.open_hedge(opp_bid, now, secs)
                        recent.appendleft(
                            _c(f"[EE] HEDGE {elt.early_leader} cruzou 0.50  "
                               f"opp_ep={opp_bid:.3f} secs={secs}", _Y)
                        )
                    # Resolveu
                    elif secs <= 35:
                        if el_bid >= 0.85:
                            ee.close("WIN", 1.0)
                            recent.appendleft(
                                _c(f"[EE] WIN  PnL=${ee.pnl:+.4f}  "
                                   f"ep={ee.entry_ep:.3f}", _G)
                            )
                        elif el_bid <= 0.05:
                            ee.close("REVERSAL", el_bid)
                            recent.appendleft(
                                _c(f"[EE] REVERSAL  PnL=${ee.pnl:+.4f}  "
                                   f"ep={ee.entry_ep:.3f}", _RE)
                            )

                # Monitorar posição hedgeada
                if ee.state == "hedged" and secs is not None and secs <= 35:
                    # Descobre quem venceu
                    if up_bid >= 0.85:
                        win_side = "UP"
                    elif down_bid >= 0.85:
                        win_side = "DOWN"
                    else:
                        win_side = None
                    if win_side:
                        if win_side == ee.entry_side:
                            ee.close("WIN_BOUNCE", 1.0, 0.0)
                        else:
                            ee.close("WIN_HEDGE", 0.0, 1.0)
                        recent.appendleft(
                            _c(f"[EE] {ee.outcome}  PnL=${ee.pnl:+.4f}  "
                               f"ep={ee.entry_ep:.3f} hedge_ep={ee.hedge_ep:.3f}", _G)
                        )

                # --- Sinais ---
                winner_side: Optional[str] = None
                if up_bid >= down_bid and up_bid >= AR_MIN_WINNER:
                    winner_side = "UP"
                elif down_bid > up_bid and down_bid >= AR_MIN_WINNER:
                    winner_side = "DOWN"
                loser_bid = down_bid if winner_side == "UP" else up_bid if winner_side else 0.0
                winner_bid = up_bid if winner_side == "UP" else down_bid if winner_side else 0.0

                mid_up   = round((up_bid + up_ask) / 2, 4) if up_ask > 0 else up_bid
                mid_down = round((down_bid + down_ask) / 2, 4) if down_ask > 0 else down_bid

                # AR
                sig_ar = bool(winner_side and secs is not None and 0 < secs <= AR_MAX_SECS)

                # MM
                sig_mm = bool(
                    secs is not None and MM_MIN_SECS <= secs <= MM_MAX_SECS
                    and MM_MID_MIN <= mid_up <= MM_MID_MAX
                    and MM_MID_MIN <= mid_down <= MM_MID_MAX
                )

                # SC
                d15 = _sf(sc_ev.get("spot_delta_15s_bps"), 0)
                md15 = _sf(sc_ev.get("market_delta_15s"), 0) * 100
                lag = abs(d15) - abs(md15)
                sig_sc = bool(
                    secs is not None and SC_MIN_SECS <= secs <= SC_MAX_SECS
                    and abs(d15) >= SC_MIN_SPOT_BPS and lag >= SC_MIN_LAG_BPS
                )
                sc_side = "UP" if d15 > 0 else "DOWN" if d15 < 0 else None

                # RS
                winner_vel = bh.velocity(winner_side or "UP", window_secs=10.0) if winner_side else 0.0
                sig_rs = bool(
                    winner_side
                    and winner_bid >= RS_MIN_WINNER
                    and loser_bid <= RS_MAX_LOSER
                    and winner_vel <= -RS_VELOCITY_MIN
                )

                # log
                log_row = {
                    "type": "monitor_snap",
                    "ts": now,
                    "cycle": cycle,
                    "slot": sname,
                    "slug": slug,
                    "secs": secs,
                    "up_bid": up_bid, "up_ask": up_ask,
                    "down_bid": down_bid, "down_ask": down_ask,
                    "mid_up": mid_up, "mid_down": mid_down,
                    "depth_bids": round(depth_bids, 2),
                    "depth_asks": round(depth_asks, 2),
                    "spot": spot_ref, "spot_bnb": spot_bnb, "spot_cb": spot_cb,
                    "spot_div_bps": spot_div,
                    "oracle": oracle_price, "oracle_stale": oracle_stale,
                    "sig_ar": sig_ar, "sig_mm": sig_mm,
                    "sig_sc": sig_sc, "sig_rs": sig_rs,
                    "winner_side": winner_side,
                    "winner_vel": winner_vel,
                    "mm_mid_up": mid_up, "mm_mid_down": mid_down,
                    "sc_side": sc_side,
                    "sc_d5": _sf(sc_ev.get("spot_delta_5s_bps"), 0),
                    "sc_d15": d15,
                    "sc_d30": _sf(sc_ev.get("spot_delta_30s_bps"), 0),
                    "sc_lag": lag,
                    "sc_mkt_d5": _sf(sc_ev.get("market_delta_5s"), 0),
                    "sc_rng15": _sf(sc_ev.get("market_range_15s"), 0),
                    "sc_dist_bps": _sf(sc_ev.get("distance_from_open_bps"), 0),
                    # Early Entry (EE)
                    "el_leader":   elt.early_leader,
                    "el_bid_240":  round(elt.el_bid_240, 4),
                    "el_bid_180":  round(elt.el_bid_180, 4),
                    "el_vel":      round(elt.el_vel, 4),
                    "el_cont_ok":  elt.cont_ok,
                    "sig_ee":      sig_ee,
                    "ee_state":    ee.state,
                    "ee_side":     ee.entry_side,
                    "ee_entry_ep": round(ee.entry_ep, 4),
                    "ee_hedge_ep": round(ee.hedge_ep, 4),
                    "ee_outcome":  ee.outcome if ee._just_closed else "",
                    "ee_pnl":      round(ee.pnl, 4) if ee._just_closed else 0.0,
                }
                _append_log(log_path, log_row)
                if ee._just_closed:
                    ee._just_closed = False

                # eventos recentes
                if sig_ar:
                    recent.appendleft(
                        f"[AR] {slug[-26:]} {winner_side} win={winner_bid:.3f} "
                        f"los={loser_bid:.3f} secs={secs}"
                    )
                if sig_rs:
                    recent.appendleft(
                        _c(f"[RS] {slug[-26:]} {winner_side} vel={winner_vel:+.4f} "
                           f"los={loser_bid:.3f} secs={secs}", _Y)
                    )
                if sig_sc:
                    recent.appendleft(
                        f"[SC] {slug[-26:]} {sc_side} d15={d15:+.1f}bps lag={lag:.1f}bps"
                    )

                slots_data.append({
                    "slot": sname, "slug": slug, "secs": secs,
                    "up_bid": up_bid, "up_ask": up_ask,
                    "down_bid": down_bid, "down_ask": down_ask,
                    "sig_ar": sig_ar, "sig_mm": sig_mm,
                    "sig_sc": sig_sc, "sig_rs": sig_rs,
                    "ar_side": winner_side, "ar_winner_bid": winner_bid,
                    "ar_loser_bid": loser_bid,
                    "rs_side": winner_side, "rs_vel": winner_vel,
                    "rs_loser_bid": loser_bid,
                    "mm_mid_up": mid_up, "mm_mid_down": mid_down,
                    "sc_d15": d15, "sc_lag": lag, "sc_side": sc_side,
                    "sc_ev": sc_ev,
                    # EE
                    "sig_ee": sig_ee, "ee_pos": ee,
                    "el_leader": elt.early_leader,
                    "el_vel": elt.el_vel, "el_bid_180": elt.el_bid_180,
                    "ee_el_bid": el_bid, "ee_hedge_bid": opp_bid,
                })

            # --- Display ---
            _print_panel(
                cycle=cycle,
                now_str=datetime.now().strftime("%H:%M:%S"),
                log_name=log_path.name if log_path else "—",
                spot_ref=spot_ref, spot_bnb=spot_bnb, spot_cb=spot_cb, spot_div=spot_div,
                oracle_price=oracle_price, oracle_stale=oracle_stale,
                chart_summary=chart_summary,
                slots_data=slots_data,
                recent=recent,
            )

        except KeyboardInterrupt:
            print("\nMonitor encerrado.")
            if log_path:
                print(f"Log: {log_path}")
            break
        except Exception as e:
            recent.appendleft(f"[ERR] {e}")

        time.sleep(poll_secs)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--poll",   type=float, default=1.0,  help="Intervalo de poll em segundos (padrão: 1.0)")
    ap.add_argument("--no-log", action="store_true",       help="Não grava log JSONL")
    args = ap.parse_args()
    run_monitor(poll_secs=args.poll, log=not args.no_log)
