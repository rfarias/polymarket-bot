"""
market/live_early_entry_paper_v1.py

Runner paper da estratégia Early Entry (EE).
Roda em paralelo com o runner AR real sem postar ordens reais.

Estratégia:
  - Detecta Early Leader (EL) em secs 181-240 (bid >= 0.55)
  - Filtro F3: bid EL >= 0.70 em secs 121-180 (cont_ok)
  - Filtro el_vel: crescimento bid >= 0.08 (bid_180 - bid_240)
  - Entra quando bid EL em 0.82-0.86 e 30 <= secs <= 180
  - Hold to resolution (sem stop)
  - Hedge: compra lado oposto quando bid EL cruza abaixo de 0.50
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from market.rest_5m_shadow_public_v5 import (
    _build_slot_bundle,
    _compute_executable_metrics,
    _fetch_slot_state,
    _slot_snapshot,
)
from market.slug_discovery import fetch_event_by_slug
from market.current_scalp_signal_v1 import (
    fetch_external_btc_reference_v1,
    fetch_binance_open_price_for_event_start_v1,
)

# ---------------------------------------------------------------------------
# Parâmetros EE
# ---------------------------------------------------------------------------

EE_EL_MIN    = 0.55   # bid mínimo para detectar early leader em secs 181-240
EE_CONT_MIN  = 0.70   # bid mínimo de continuidade em secs 121-180
EE_VEL_MIN   = 0.08   # crescimento mínimo do bid EL (bid_180 - bid_240)
EE_ENTRY_LO  = 0.82   # faixa de entrada: mínimo
EE_ENTRY_HI  = 0.86   # faixa de entrada: máximo
EE_HEDGE_THR           = 0.50   # fallback de hedge (raramente atingido com stop ativo)
EE_MAX_SECS            = 180    # secs máximo para entrada EE
EE_STOP_LEVEL          = 0.65   # stop loss: saída se bid EL cair abaixo deste nível
EE_PROFIT_PROTECT_BID  = 0.88   # profit protect: saída antecipada quando bid EL >= este valor
EE_PROFIT_PROTECT_SECS = 70     # profit protect: só ativa quando 36 <= secs <= este valor


# ---------------------------------------------------------------------------
# Utilitários
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
    return Path("logs") / f"ee_paper_{ts}"


# ---------------------------------------------------------------------------
# Early Leader Tracker (versão completa com el_vel e signal_ok)
# ---------------------------------------------------------------------------

class _EarlyLeaderTracker:
    """Rastreia EL por slug — calcula el_vel e filtro F3 (cont_ok).

    Janelas:
      secs 181-240: detecta qual lado lidera (early_leader, bid_240)
      secs 121-180: verifica continuidade (cont_ok) e calcula el_vel
    """

    def __init__(self) -> None:
        self._slug: Optional[str] = None
        self._s240: list = []   # snaps em secs 181-240
        self._s180: list = []   # snaps em secs 121-180
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
        """True quando EL estável (F3 ok) e velocidade suficiente."""
        return bool(
            self.early_leader
            and self.cont_ok
            and self.el_vel >= EE_VEL_MIN
        )

    def state_dict(self) -> dict:
        return {
            "early_side":  self.early_leader,
            "el_bid_240":  self.el_bid_240,
            "el_bid_180":  self.el_bid_180,
            "el_vel":      self.el_vel,
            "f3_ok":       self.cont_ok,
            "signal_ok":   self.signal_ok,
            "n_s240":      len(self._s240),
            "n_s180":      len(self._s180),
        }


# ---------------------------------------------------------------------------
# Early Entry Position — máquina de estado
# ---------------------------------------------------------------------------

class _EEPosition:
    """Estados: none → entry → hedged → closed."""

    def __init__(self, qty: float = 6.0) -> None:
        self.qty:          float          = qty
        self.state:        str            = "none"
        self.entry_side:   Optional[str]  = None
        self.entry_ep:     float          = 0.0
        self.entry_ts:     float          = 0.0
        self.entry_secs:   int            = 0
        self.hedge_ep:     float          = 0.0
        self.hedge_ts:     float          = 0.0
        self.hedge_secs:   int            = 0
        self.outcome:      str            = ""
        self.pnl:          float          = 0.0
        self._just_closed: bool           = False

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
        qty = self.qty
        self.__init__(qty=qty)

    def as_dict(self) -> dict:
        return {
            "state":       self.state,
            "entry_side":  self.entry_side,
            "entry_ep":    round(self.entry_ep, 4),
            "entry_secs":  self.entry_secs,
            "hedge_ep":    round(self.hedge_ep, 4) if self.hedge_ep else None,
            "hedge_secs":  self.hedge_secs if self.hedge_ep else None,
            "outcome":     self.outcome,
            "pnl":         self.pnl,
            "qty":         self.qty,
        }


# ---------------------------------------------------------------------------
# Runner principal
# ---------------------------------------------------------------------------

def run_early_entry_paper_v1(
    duration_seconds: Optional[int] = None,
    log_dir: Optional[str] = None,
) -> None:
    load_dotenv()

    qty       = float(os.getenv("EE_PAPER_QTY", "6"))
    poll_secs = max(0.25, _sf(os.getenv("EE_PAPER_POLL_SECS", "0.5")))
    run_for   = int(duration_seconds or _sf(os.getenv("EE_PAPER_RUN_SECONDS", "3600"), 3600))

    session_dir = Path(log_dir) if log_dir else _build_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path   = session_dir / "ee_paper.jsonl"
    session_id = session_dir.name

    print(f"[EE_PAPER] session={session_id}  qty={qty}  poll={poll_secs}s  run={run_for}s")
    print(f"[EE_PAPER] log={log_path}")
    print(
        f"[EE_PAPER] params: EL_MIN={EE_EL_MIN}  CONT>={EE_CONT_MIN}  "
        f"VEL>={EE_VEL_MIN}  entry=[{EE_ENTRY_LO},{EE_ENTRY_HI}]  "
        f"stop<{EE_STOP_LEVEL}  pp>={EE_PROFIT_PROTECT_BID}@secs<={EE_PROFIT_PROTECT_SECS}"
    )

    _append_jsonl(log_path, {
        "type": "startup", "ts": time.time(), "session_id": session_id,
        "params": {
            "qty": qty, "poll_secs": poll_secs, "run_for": run_for,
            "EE_EL_MIN": EE_EL_MIN, "EE_CONT_MIN": EE_CONT_MIN,
            "EE_VEL_MIN": EE_VEL_MIN, "EE_ENTRY_LO": EE_ENTRY_LO,
            "EE_ENTRY_HI": EE_ENTRY_HI, "EE_HEDGE_THR": EE_HEDGE_THR,
            "EE_MAX_SECS": EE_MAX_SECS,
            "EE_STOP_LEVEL": EE_STOP_LEVEL,
            "EE_PROFIT_PROTECT_BID": EE_PROFIT_PROTECT_BID,
            "EE_PROFIT_PROTECT_SECS": EE_PROFIT_PROTECT_SECS,
        },
    })

    elt        = _EarlyLeaderTracker()
    ee         = _EEPosition(qty=qty)
    started_at = time.time()
    _last_slug = ""

    # cache do preço de abertura do candle (price to beat) — atualizado uma vez por slug
    _open_ref: dict = {"slug": None, "price": None, "event_start_time": None}

    # acumuladores de sessão
    session_pnl    = 0.0
    session_wins   = 0
    session_losses = 0
    session_trades: list = []

    while time.time() - started_at < run_for:
        now = time.time()
        try:
            slot_bundle  = _build_slot_bundle()
            current_item = slot_bundle["queue"].get("current")
            if not current_item:
                time.sleep(poll_secs)
                continue

            slug = str(current_item.get("slug") or "")
            secs_raw = current_item.get("seconds_to_end")
            secs: Optional[int] = int(secs_raw) if secs_raw is not None else None

            slot_state   = _fetch_slot_state(slot_bundle)
            current_snap = _slot_snapshot(slot_state, "current")
            current_exec, _ = _compute_executable_metrics(current_snap)

            up_bid   = _sf((current_exec or {}).get("up_bid"))
            down_bid = _sf((current_exec or {}).get("down_bid"))

            if not slug or up_bid <= 0 or down_bid <= 0:
                time.sleep(poll_secs)
                continue

            # --- Slug mudou: fechar posição aberta sem resolução (MISSED) ---
            if slug != _last_slug:
                if ee.state in ("entry", "hedged"):
                    ee.close("MISSED", 0.0)
                    _append_jsonl(log_path, {
                        "type": "ee_paper_closed", "ts": now,
                        "session_id": session_id, "slug": _last_slug,
                        "ee": ee.as_dict(),
                        "session_pnl": round(session_pnl, 4),
                    })
                    print(f"[EE_PAPER] MISSED (slug mudou)  slug={_last_slug[-20:]}")
                ee.reset()
                _last_slug = slug

                # Busca o preço de abertura do candle (price to beat) para o novo slug
                try:
                    raw_event = fetch_event_by_slug(slug)
                    mkt = (raw_event.get("markets") or [{}])[0] if raw_event else {}
                    est = mkt.get("eventStartTime") or (raw_event.get("startTime") if raw_event else None)
                    open_ref = fetch_binance_open_price_for_event_start_v1(est) if est else {}
                    _open_ref = {"slug": slug, "price": open_ref.get("open_price"), "event_start_time": est}
                except Exception:
                    _open_ref = {"slug": slug, "price": None, "event_start_time": None}

            # --- Atualiza EL tracker ---
            elt.update(slug, secs, up_bid, down_bid)

            el_side = elt.early_leader
            el_bid  = (up_bid  if el_side == "UP"   else down_bid) if el_side else 0.0
            opp_bid = (down_bid if el_side == "UP"  else up_bid)   if el_side else 0.0

            # --- Sinal de entrada EE ---
            # Gate de regime: ep alto com vel baixa tem WR=62% e avg=-$0.08 (137 trades paper).
            # Com vel>=0.13 o WR sobe para 89% mesmo em ep alto.
            _ep_vel_blocked = (el_bid >= 0.86 and elt.el_vel < 0.13)

            if (
                ee.state == "none"
                and elt.signal_ok
                and secs is not None and 30 <= secs <= EE_MAX_SECS
                and EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI
                and not _ep_vel_blocked
            ):
                ee.open_entry(el_side, el_bid, now, secs)

                # Captura dist ao price to beat + momentum/volatilidade BTC (apenas logging)
                btc_info: dict = {}
                try:
                    import requests as _req
                    BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

                    ref = fetch_external_btc_reference_v1()
                    btc_spot = ref.get("reference_price")
                    ptb      = _open_ref.get("price")
                    if btc_spot and ptb:
                        dist_usd = round(abs(btc_spot - ptb), 2)
                        dist_bps = round(abs(btc_spot - ptb) / ptb * 10000, 2)
                        btc_info = {
                            "spot":      round(btc_spot, 2),
                            "threshold": round(ptb, 2),
                            "dist_usd":  dist_usd,
                            "dist_bps":  dist_bps,
                            "above":     btc_spot > ptb,
                        }
                    else:
                        btc_info = {"spot": btc_spot, "threshold": ptb, "dist_usd": None, "dist_bps": None}

                    # Momentum: 3 candles 1m antes da entrada
                    start_ms = (int(now // 60) - 3) * 60 * 1000
                    km = _req.get(BINANCE_KLINES, params={
                        "symbol": "BTCUSDT", "interval": "1m",
                        "startTime": start_ms, "limit": 3,
                    }, timeout=5)
                    krows = km.json() or []
                    if len(krows) >= 3:
                        closes  = [float(krows[i][4]) for i in range(3)]
                        highs   = [float(krows[i][2]) for i in range(3)]
                        lows    = [float(krows[i][3]) for i in range(3)]
                        ref_k   = closes[-1]
                        d1m     = round(closes[2] - closes[1], 2)
                        d3m     = round(closes[2] - closes[0], 2)
                        rng3m   = round(max(highs) - min(lows), 2)
                        btc_info.update({
                            "delta_1m_usd":  d1m,
                            "delta_3m_usd":  d3m,
                            "delta_1m_bps":  round(d1m / ref_k * 10000, 2) if ref_k else None,
                            "delta_3m_bps":  round(d3m / ref_k * 10000, 2) if ref_k else None,
                            "range_3m_usd":  rng3m,
                            "range_3m_bps":  round(rng3m / ref_k * 10000, 2) if ref_k else None,
                            "adverse_1m":    (d1m < 0 if el_side == "UP" else d1m > 0),
                            "adverse_3m":    (d3m < 0 if el_side == "UP" else d3m > 0),
                        })
                except Exception:
                    btc_info.setdefault("dist_usd", None)

                _append_jsonl(log_path, {
                    "type": "ee_paper_entry", "ts": now,
                    "session_id": session_id, "slug": slug,
                    "side": el_side, "ep": round(el_bid, 4), "secs": secs,
                    "el": elt.state_dict(),
                    "btc": btc_info,
                })
                dist_str = f"  dist={btc_info['dist_usd']}usd" if btc_info.get("dist_usd") is not None else ""
                adv_str  = "  ADV!" if btc_info.get("adverse_1m") else ""
                mom_str  = (f"  d1m={btc_info['delta_1m_usd']:+.0f}usd  rng={btc_info['range_3m_usd']:.0f}usd"
                            if btc_info.get("delta_1m_usd") is not None else "")
                print(
                    f"[EE_PAPER] ENTRADA {el_side}  ep={el_bid:.3f}  "
                    f"vel={elt.el_vel:+.3f}  secs={secs}  slug={slug[-20:]}"
                    f"{dist_str}{mom_str}{adv_str}"
                )

            elif (
                ee.state == "none"
                and elt.signal_ok
                and secs is not None and 30 <= secs <= EE_MAX_SECS
                and EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI
                and _ep_vel_blocked
            ):
                _append_jsonl(log_path, {
                    "type": "entry_blocked", "ts": now,
                    "session_id": session_id, "slug": slug,
                    "reason": f"regime_gate:ep={el_bid:.3f}>=0.86,vel={elt.el_vel:.4f}<0.13",
                    "ep": round(el_bid, 4), "el_vel": elt.el_vel, "secs": secs,
                })
                print(
                    f"[EE_PAPER] BLOQUEADO  ep={el_bid:.3f}  "
                    f"vel={elt.el_vel:+.3f}  secs={secs}  slug={slug[-20:]}"
                )

            # --- Monitorar posição em entry ---
            if ee.state == "entry" and secs is not None:
                if secs <= 35:
                    # Resolução normal — prioridade máxima perto do fim
                    if el_bid >= 0.85:
                        ee.close("WIN", 1.0)
                    elif opp_bid >= 0.85:
                        ee.close("REVERSAL", 0.0)
                elif 36 <= secs <= EE_PROFIT_PROTECT_SECS and el_bid >= EE_PROFIT_PROTECT_BID:
                    # Profit protect: captura ganho antes de possível crash brusco
                    ee.close("PROFIT_PROTECT", el_bid)
                    _append_jsonl(log_path, {
                        "type": "ee_paper_profit_protect", "ts": now,
                        "session_id": session_id, "slug": slug,
                        "el_bid": round(el_bid, 4), "secs": secs,
                    })
                    print(
                        f"[EE_PAPER] PROFIT_PROTECT  el_bid={el_bid:.3f}  secs={secs}"
                    )
                elif el_bid < EE_STOP_LEVEL:
                    # Stop loss: limita perda máxima (saída ao preço atual)
                    ee.close("STOP_LOSS", el_bid)
                    _append_jsonl(log_path, {
                        "type": "ee_paper_stop_loss", "ts": now,
                        "session_id": session_id, "slug": slug,
                        "el_bid": round(el_bid, 4), "secs": secs,
                    })
                    print(
                        f"[EE_PAPER] STOP_LOSS  el_bid={el_bid:.3f}  secs={secs}"
                    )
                elif el_bid < EE_HEDGE_THR and opp_bid > 0:
                    # Hedge: fallback (bid saltou acima do stop e caiu abaixo de 0.50 em um poll)
                    ee.open_hedge(opp_bid, now, secs)
                    _append_jsonl(log_path, {
                        "type": "ee_paper_hedge", "ts": now,
                        "session_id": session_id, "slug": slug,
                        "el_bid": round(el_bid, 4),
                        "opp_ep": round(opp_bid, 4), "secs": secs,
                    })
                    print(
                        f"[EE_PAPER] HEDGE  el_bid={el_bid:.3f}  "
                        f"opp_ep={opp_bid:.3f}  secs={secs}"
                    )

            # --- Monitorar posição hedgeada ---
            if ee.state == "hedged" and secs is not None and secs <= 35:
                winner: Optional[str] = None
                if up_bid >= 0.85:
                    winner = "UP"
                elif down_bid >= 0.85:
                    winner = "DOWN"
                if winner:
                    if winner == ee.entry_side:
                        ee.close("WIN_BOUNCE", 1.0, 0.0)  # EL recuperou; hedge perdeu
                    else:
                        ee.close("WIN_HEDGE", 0.0, 1.0)   # EL perdeu; hedge salvou

            # --- Registrar fechamento ---
            if ee._just_closed:
                ee._just_closed = False
                session_pnl += ee.pnl
                if ee.pnl > 0:
                    session_wins += 1
                elif ee.pnl < 0:
                    session_losses += 1
                session_trades.append({
                    "slug": slug, "ts": now,
                    "side": ee.entry_side, "ep": ee.entry_ep,
                    "entry_secs": ee.entry_secs, "outcome": ee.outcome,
                    "pnl": ee.pnl, "el_vel": elt.el_vel,
                })
                _append_jsonl(log_path, {
                    "type": "ee_paper_closed", "ts": now,
                    "session_id": session_id, "slug": slug,
                    "ee": ee.as_dict(),
                    "session_pnl": round(session_pnl, 4),
                    "session_wins": session_wins,
                    "session_losses": session_losses,
                })
                n_now = session_wins + session_losses
                wr_now = f"{session_wins / n_now * 100:.0f}%" if n_now else "n/a"
                print(
                    f"[EE_PAPER] {ee.outcome:<12}  PnL={ee.pnl:>+7.4f}  "
                    f"ep={ee.entry_ep:.3f}  sess={session_pnl:>+.4f}  "
                    f"{session_wins}W/{session_losses}L WR={wr_now}"
                )
                ee.reset()

            # --- Snapshot (compatível com _sim_ee_runner.py) ---
            _append_jsonl(log_path, {
                "type": "snapshot", "ts": now,
                "session_id": session_id,
                "current_slug": slug,
                "current_secs": secs,
                "current_exec": {
                    "up_bid": round(up_bid, 4),
                    "down_bid": round(down_bid, 4),
                },
                "early_leader": elt.state_dict(),
                "ee_position": ee.as_dict() if ee.state != "none" else None,
            })

            # linha de status por poll
            wr_disp = f"{session_wins/(session_wins+session_losses)*100:.0f}%" if (session_wins+session_losses) else "n/a"
            print(
                f"[EE_PAPER] {slug[-20:]:20}  secs={str(secs):>4}  "
                f"up={up_bid:.3f} dn={down_bid:.3f}  "
                f"EL={el_side or '---':4} vel={elt.el_vel:>+.3f}  "
                f"ee={ee.state:<7}  sess={session_pnl:>+.4f} {wr_disp}"
            )

        except KeyboardInterrupt:
            print("\n[EE_PAPER] Interrompido pelo usuário.")
            break
        except Exception as exc:
            _append_jsonl(log_path, {
                "type": "error", "ts": now, "session_id": session_id,
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"[EE_PAPER] ERRO: {type(exc).__name__}: {exc}")

        time.sleep(poll_secs)

    # --- Sumário final ---
    n = session_wins + session_losses
    wr = f"{session_wins / n * 100:.0f}%" if n else "n/a"
    avg = round(session_pnl / n, 4) if n else 0.0
    summary = {
        "type": "session_summary", "ts": time.time(),
        "session_id": session_id,
        "trades": n, "wins": session_wins, "losses": session_losses,
        "win_rate": wr, "pnl_total": round(session_pnl, 4), "avg_pnl": avg,
        "trade_log": session_trades,
    }
    _append_jsonl(log_path, summary)

    print(f"\n[EE_PAPER] === SESSÃO ENCERRADA ===")
    print(f"[EE_PAPER] Trades: {n}  ({session_wins}W / {session_losses}L  WR={wr})")
    if n:
        print(f"[EE_PAPER] PnL total: {session_pnl:>+.4f} USD  avg/trade: {avg:>+.4f}")
    else:
        print("[EE_PAPER] Sem trades nesta sessão.")
    print(f"[EE_PAPER] Log: {log_path}")
