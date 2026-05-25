"""
simulate_reversal_sniper_early_exit.py

Simula a camada de saida antecipada do reversal_sniper sobre os logs
historicos do current_almost_resolved.

Como o Sinal A (100bps oracle) raramente dispara em janelas de 15-120s,
a simulacao usa tres modos de entrada configurados abaixo:

  ENTRY_MODE = "universe"  --> entra no primeiro poll da janela (sem filtro de sinal)
  ENTRY_MODE = "sinal_b"   --> entra quando winner bid desacelera (score >= 2)
  ENTRY_MODE = "sniper"    --> fiel ao sniper (score >= 4, Sinal A + B)

Para cada entrada simulada:
  - Rastreia loser bid ate resolucao
  - Aplica as 3 condicoes de early exit
  - Calcula pnl_hold vs pnl_early_exit e ev_delta
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Configuracao do modo de simulacao
# ---------------------------------------------------------------------------

ENTRY_MODE = "universe"   # "universe" | "sinal_b" | "sniper"

# Constantes do sniper
MIN_WINNER_BID      = 0.88
MAX_LOSER_PRICE     = 0.15
MIN_SECS            = 15
MAX_SECS            = 120

SCORE_THRESHOLD_SNIPER  = 4
SCORE_THRESHOLD_SINAL_B = 2
COOLDOWN_SECS       = 60.0
BID_DECEL_POLLS     = 3
LOSER_HISTORY_POLLS = 2

SINAL_A_BPS         = 100.0
SINAL_B_WEAK_VEL    = -0.020
SINAL_B_STRONG_VEL  = -0.050

PAPER_BET_SIZE      = 20.0

EARLY_EXIT_PROFIT_MULT   = 1.80
EARLY_EXIT_PULLBACK_GATE = 1.30
EARLY_EXIT_PULLBACK_FRAC = 0.60

LOGS_DIR = Path(__file__).parent / "logs"

# ---------------------------------------------------------------------------
# Leitura de logs
# ---------------------------------------------------------------------------

def _read_session(folder: Path) -> List[dict]:
    jfile = folder / "current_almost_resolved_real.jsonl"
    if not jfile.exists():
        return []
    events = []
    with open(jfile, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _extract_per_slug(events: List[dict]) -> Dict[str, dict]:
    """
    slug -> {
      "polls": [{"ts", "current_secs", "up_bid", "down_bid",
                  "oracle_price", "oracle_open"}],
      "outcome": "UP" | "DOWN" | None
    }
    """
    per_slug: Dict[str, dict] = {}
    session_oracle_open: Optional[float] = None  # primeiro oracle da sessao

    for ev in events:
        evtype = ev.get("type")

        if evtype == "snapshot":
            slug = ev.get("current_slug") or ""
            if not slug:
                continue
            scs      = ev.get("current_scalp_context") or {}
            up_bid   = scs.get("up_bid")
            down_bid = scs.get("down_bid")
            oracle   = ev.get("oracle_price")
            o_open   = ev.get("oracle_open_price")
            secs     = ev.get("current_secs")

            if up_bid is None or down_bid is None:
                continue

            # referencia oracle: preferir campo do log, senao rastrear primeiro da sessao
            if oracle is not None and session_oracle_open is None:
                session_oracle_open = oracle
            ref_open = o_open if o_open is not None else session_oracle_open

            if slug not in per_slug:
                per_slug[slug] = {"polls": [], "outcome": None}

            per_slug[slug]["polls"].append({
                "ts":           ev.get("ts", 0.0),
                "current_secs": secs,
                "up_bid":       float(up_bid),
                "down_bid":     float(down_bid),
                "oracle_price": float(oracle) if oracle is not None else None,
                "oracle_open":  float(ref_open) if ref_open is not None else None,
            })

        elif evtype in ("resolution", "fill_on_invalid"):
            slug = ev.get("event_slug") or ev.get("current_slug") or ""
            if not slug:
                continue
            side = ev.get("resolved_side") or ev.get("winner_side")
            if side and slug in per_slug:
                per_slug[slug]["outcome"] = side

    return per_slug


def _infer_outcome(polls: List[dict]) -> Optional[str]:
    if not polls:
        return None
    last = max(polls, key=lambda p: p["ts"])
    ub, db = last["up_bid"], last["down_bid"]
    if ub >= 0.95:
        return "UP"
    if db >= 0.95:
        return "DOWN"
    if ub > db + 0.20:
        return "UP"
    if db > ub + 0.20:
        return "DOWN"
    return None

# ---------------------------------------------------------------------------
# Rastreadores de sinal
# ---------------------------------------------------------------------------

def _sinal_b_score(velocity: Optional[float]) -> int:
    if velocity is None:
        return 0
    if velocity < SINAL_B_STRONG_VEL:
        return 3
    if velocity < SINAL_B_WEAK_VEL:
        return 2
    return 0


def _sinal_a_score(winner_side: str, bps: Optional[float]) -> int:
    if bps is None:
        return 0
    if winner_side == "UP" and bps < -SINAL_A_BPS:
        return 2
    if winner_side == "DOWN" and bps > SINAL_A_BPS:
        return 2
    return 0


def _compute_bps(oracle: Optional[float], oracle_open: Optional[float]) -> Optional[float]:
    if oracle is None or oracle_open is None or oracle_open <= 0:
        return None
    return round((oracle - oracle_open) / oracle_open * 10_000, 2)

# ---------------------------------------------------------------------------
# Simulador por slug
# ---------------------------------------------------------------------------

def simulate_slug(slug: str, polls: List[dict], outcome: Optional[str]) -> Optional[dict]:
    polls = sorted(polls, key=lambda p: p["ts"])

    # janela de interesse
    in_window = [p for p in polls
                 if p["current_secs"] is not None
                 and MIN_SECS <= p["current_secs"] <= MAX_SECS]
    if len(in_window) < 3:
        return None

    winner = outcome or _infer_outcome(polls)
    if winner is None:
        return None

    # rastrear historico de bid para Sinal B
    bid_hist: Deque[Tuple[float, float]] = deque(maxlen=6)
    oracle_open_slug: Optional[float] = None
    cooldown_until: float = 0.0
    position: Optional[dict] = None

    for p in polls:
        ts       = p["ts"]
        secs     = p["current_secs"]
        up_bid   = p["up_bid"]
        down_bid = p["down_bid"]
        oracle   = p["oracle_price"]
        o_open   = p["oracle_open"]

        # winner/loser
        winner_side = "UP" if up_bid >= down_bid else "DOWN"
        winner_bid  = up_bid if winner_side == "UP" else down_bid
        loser_side  = "DOWN" if winner_side == "UP" else "UP"
        loser_bid   = down_bid if loser_side == "DOWN" else up_bid
        loser_price = round(1.0 - winner_bid, 4)

        # oracle open por slug (primeiro valor disponivel)
        if oracle is not None and oracle_open_slug is None:
            oracle_open_slug = o_open or oracle
        bps = _compute_bps(oracle, oracle_open_slug)

        # sinais
        sa = _sinal_a_score(winner_side, bps)

        bid_hist.append((ts, winner_bid))
        vel: Optional[float] = None
        if len(bid_hist) >= BID_DECEL_POLLS + 1:
            vel = round(winner_bid - list(bid_hist)[-BID_DECEL_POLLS - 1][1], 6)
        sb = _sinal_b_score(vel)
        total = sa + sb

        # atualizar posicao ativa
        if position is not None:
            ps = position["entry_loser_side"]
            pb = up_bid if ps == "UP" else down_bid

            position["max_loser_bid_seen"] = max(position["max_loser_bid_seen"], pb)
            position["min_score_during_hold"] = min(position["min_score_during_hold"], total)

            if secs is not None and secs <= 20 and position["loser_bid_at_t20s"] is None:
                position["loser_bid_at_t20s"] = pb

            if (position["signal_btc_divergence_faded_at"] is None
                    and position.get("entry_sinal_a_score", 0) > 0
                    and sa == 0):
                position["signal_btc_divergence_faded_at"] = ts

            # early exit check
            if not position["early_exit_triggered"]:
                _entry = position["entry_loser_price"]
                _max   = position["max_loser_bid_seen"]
                why: Optional[str] = None

                if pb >= _entry * EARLY_EXIT_PROFIT_MULT and sa == 0:
                    why = "partial_profit_signal_faded"
                elif total < 2 and pb > _entry:
                    why = "score_collapsed_take_profit"
                elif _max >= _entry * EARLY_EXIT_PULLBACK_GATE and pb < _max * EARLY_EXIT_PULLBACK_FRAC:
                    why = "dynamic_stop_pullback"

                if why:
                    position["early_exit_triggered"] = True
                    position["early_exit_reason"]    = why
                    position["early_exit_bid"]       = pb
                    position["early_exit_ts"]        = ts

        # skip fora da janela (continua tracking se posicao ativa)
        if secs is None or not (MIN_SECS <= secs <= MAX_SECS):
            continue

        # skip fora da zona de monitoramento (continua se posicao ativa)
        if winner_bid < MIN_WINNER_BID or loser_price <= 0 or loser_price > MAX_LOSER_PRICE:
            continue

        # decidir entrada
        if position is None and ts >= cooldown_until:
            if ENTRY_MODE == "universe":
                should_enter = True
            elif ENTRY_MODE == "sinal_b":
                should_enter = total >= SCORE_THRESHOLD_SINAL_B
            else:  # sniper
                should_enter = total >= SCORE_THRESHOLD_SNIPER

            if should_enter:
                position = {
                    "slug":                slug,
                    "entered_at":          ts,
                    "entry_loser_price":   loser_price,
                    "entry_secs":          secs,
                    "entry_score":         total,
                    "entry_sinal_a_score": sa,
                    "entry_winner_side":   winner_side,
                    "entry_loser_side":    loser_side,
                    "max_loser_bid_seen":  loser_price,
                    "loser_bid_at_t20s":   None,
                    "signal_btc_divergence_faded_at": None,
                    "min_score_during_hold": total,
                    "early_exit_triggered": False,
                    "early_exit_reason":    None,
                    "early_exit_bid":       None,
                    "early_exit_ts":        None,
                }
                cooldown_until = ts + COOLDOWN_SECS

    if position is None:
        return None

    entry     = position["entry_loser_price"]
    loser_won = (winner == position["entry_loser_side"])
    pnl_hold  = round((1.0 - entry) if loser_won else -entry, 4)

    pnl_early: Optional[float] = None
    if position["early_exit_triggered"]:
        pnl_early = round(position["early_exit_bid"] - entry, 4)

    return {
        "slug":                slug,
        "entry_loser_price":   entry,
        "entry_secs":          position["entry_secs"],
        "entry_score":         position["entry_score"],
        "entry_sinal_a_score": position["entry_sinal_a_score"],
        "entry_winner_side":   position["entry_winner_side"],
        "entry_loser_side":    position["entry_loser_side"],
        "loser_won":           loser_won,
        "max_loser_bid_seen":  round(position["max_loser_bid_seen"], 4),
        "loser_bid_at_t20s":   position["loser_bid_at_t20s"],
        "min_score_during_hold": position["min_score_during_hold"],
        "early_exit_triggered": position["early_exit_triggered"],
        "early_exit_reason":   position["early_exit_reason"],
        "early_exit_bid":      position["early_exit_bid"],
        "pnl_hold":            pnl_hold,
        "pnl_early_exit":      pnl_early,
        "ev_delta":            round(pnl_early - pnl_hold, 4) if pnl_early is not None else None,
    }

# ---------------------------------------------------------------------------
# Relatorio
# ---------------------------------------------------------------------------

def _pct(n, d, fmt=".1f") -> str:
    if not d:
        return "--"
    return f"{n/d*100:{fmt}}%"


def run() -> None:
    folders = sorted(
        p for p in LOGS_DIR.iterdir()
        if p.is_dir() and p.name.startswith("current_almost_resolved_real_")
    )
    print(f"Sessoes: {len(folders)}")

    slug_data: Dict[str, dict] = {}
    for folder in folders:
        events = _read_session(folder)
        per_slug = _extract_per_slug(events)
        for slug, data in per_slug.items():
            if slug not in slug_data:
                slug_data[slug] = {"polls": [], "outcome": None}
            slug_data[slug]["polls"].extend(data["polls"])
            if data["outcome"] and not slug_data[slug]["outcome"]:
                slug_data[slug]["outcome"] = data["outcome"]

    print(f"Slugs unicos: {len(slug_data)}")

    results: List[dict] = []
    for slug, data in slug_data.items():
        # deduplicar polls por ts
        seen_ts: set = set()
        unique_polls = []
        for p in data["polls"]:
            if p["ts"] not in seen_ts:
                seen_ts.add(p["ts"])
                unique_polls.append(p)
        r = simulate_slug(slug, unique_polls, data["outcome"])
        if r is not None:
            results.append(r)

    n = len(results)
    if n == 0:
        print("Nenhum trade simulado. Verifique os filtros de entrada.")
        return

    wins   = sum(1 for r in results if r["loser_won"])
    losses = n - wins

    pnl_hold_total = sum(r["pnl_hold"] for r in results)
    ev_hold_per    = pnl_hold_total / n

    with_early    = [r for r in results if r["early_exit_triggered"]]
    pnl_early_total = sum(
        (r["pnl_early_exit"] if r["early_exit_triggered"] else r["pnl_hold"])
        for r in results
    )
    ev_early_per = pnl_early_total / n

    # calcular em $ assumindo bet_size fixo e entry_price medio
    avg_entry = sum(r["entry_loser_price"] for r in results) / n
    scale = PAPER_BET_SIZE / avg_entry  # shares simuladas

    print()
    print("=" * 65)
    print(f"  SIMULACAO REVERSAL SNIPER  modo={ENTRY_MODE}  n={n}")
    print("=" * 65)
    print(f"  Win rate (loser ganhou): {_pct(wins, n)}  ({wins}/{n})")
    print(f"  Entry loser price medio: {avg_entry:.4f}")
    print(f"  Shares simuladas / trade: {scale:.1f}  (${PAPER_BET_SIZE})")
    print()
    print(f"  -- HOLD ATE RESOLUCAO --")
    print(f"  PnL total (p/share):  {pnl_hold_total:+.4f}")
    print(f"  PnL total ($):        ${pnl_hold_total * scale:+.2f}")
    print(f"  EV por trade ($):     ${ev_hold_per * scale:+.3f}")
    print()
    print(f"  -- COM SAIDA ANTECIPADA --")
    print(f"  Trades que sairam cedo: {len(with_early)} ({_pct(len(with_early), n)})")
    print(f"  PnL total ($):        ${pnl_early_total * scale:+.2f}")
    print(f"  EV por trade ($):     ${ev_early_per * scale:+.3f}")
    delta_total = (pnl_early_total - pnl_hold_total) * scale
    print(f"  Delta vs hold ($):    ${delta_total:+.2f}  ({'melhor' if delta_total > 0 else 'pior'} que hold)")
    print()

    # breakdown por razao
    if with_early:
        reasons: Dict[str, List] = defaultdict(list)
        for r in with_early:
            reasons[r["early_exit_reason"]].append(r)
        print("  -- EARLY EXIT POR RAZAO --")
        for reason, rs in sorted(reasons.items()):
            wins_r  = sum(1 for r in rs if r["loser_won"])
            pnl_e   = sum(r["pnl_early_exit"] for r in rs)
            pnl_h   = sum(r["pnl_hold"] for r in rs)
            delta_r = (pnl_e - pnl_h) * scale
            avg_e   = pnl_e / len(rs) * scale
            avg_h   = pnl_h / len(rs) * scale
            print(f"  {reason}")
            print(f"    n={len(rs)}  win_rate_sem_exit={_pct(wins_r, len(rs))}")
            print(f"    avg_pnl_early=${avg_e:+.3f}  avg_pnl_hold=${avg_h:+.3f}  delta_total=${delta_r:+.2f}")
        print()

    # distribuicao de max_loser_bid
    max_bids = [r["max_loser_bid_seen"] for r in results]
    entries  = [r["entry_loser_price"] for r in results]
    max_rets = [(m / e - 1) * 100 for m, e in zip(max_bids, entries)]
    buckets  = [(0, 50), (50, 100), (100, 200), (200, 500), (500, 9999)]
    print("  -- DISTRIBUICAO MAX RETURN DISPONIVEL (loser bid) --")
    for lo, hi in buckets:
        cnt = sum(1 for r in max_rets if lo <= r < hi)
        print(f"  {lo:4d}% - {hi if hi < 9999 else '999+'}%:  {cnt:4d}  ({_pct(cnt, n)})")

    # pct de eventos onde early exit teria sido util (ev_delta > 0)
    positive_delta = [r for r in results if r["ev_delta"] is not None and r["ev_delta"] > 0]
    negative_delta = [r for r in results if r["ev_delta"] is not None and r["ev_delta"] < 0]
    print()
    print(f"  Early exit foi MELHOR que hold:  {len(positive_delta)}/{len(with_early)}")
    print(f"  Early exit foi PIOR que hold:    {len(negative_delta)}/{len(with_early)}")
    print()

    # campos de diagnostico
    with_t20 = [r for r in results if r["loser_bid_at_t20s"] is not None]
    avg_t20  = (sum(r["loser_bid_at_t20s"] for r in with_t20) / len(with_t20)) if with_t20 else None
    faded    = [r for r in results if r["min_score_during_hold"] < 2]
    print("  -- DIAGNOSTICO --")
    if avg_t20 is not None:
        print(f"  loser_bid_at_t20s medio: {avg_t20:.4f}  (de {len(with_t20)} trades)")
    print(f"  Trades com score < 2 durante hold: {len(faded)}/{n}")
    print()

    # tabela detalhada (top 30 por max_return)
    results_sorted = sorted(results, key=lambda r: r["max_loser_bid_seen"] / r["entry_loser_price"], reverse=True)
    print("  -- TOP 30 por max return disponivel --")
    hdr = f"  {'slug':30s} {'entry':6s} {'secs':5s} {'loser_won':9s} {'max_bid':8s} {'max_ret%':8s} {'early_exit_reason':32s} {'pnl_h':7s} {'pnl_e':7s}"
    print(hdr)
    print("  " + "-" * 115)
    for r in results_sorted[:30]:
        slug_s  = r["slug"][-30:]
        max_ret = (r["max_loser_bid_seen"] / r["entry_loser_price"] - 1) * 100
        ph = f"{r['pnl_hold']:+.4f}"
        pe = f"{r['pnl_early_exit']:+.4f}" if r["pnl_early_exit"] is not None else "      -"
        reason  = r["early_exit_reason"] or "hold_to_resolution"
        won     = "YES" if r["loser_won"] else "no "
        print(f"  {slug_s:30s} {r['entry_loser_price']:6.4f} {str(r['entry_secs'] or '?'):5s} {won:9s} "
              f"{r['max_loser_bid_seen']:8.4f} {max_ret:8.1f}% {reason:32s} {ph:7s} {pe:7s}")


if __name__ == "__main__":
    run()
