"""
analyze_ee_trailing_stop.py

Simula variantes de stop menos agressivas que o Profit Protect atual:
  1. Trailing stop (ativado após bid >= threshold) com pullback 1T/2T/3T/5T/10T
  2. Breakeven stop (move stop para entry após lucro mínimo)
  3. Trailing stop + breakeven combinados

Para cada variante calcula:
  - Saída ideal  (ao preço do stop — stop limit, não garante fill)
  - Saída real   (ao bid atual no poll — stop market, inclui slippage de gap)
  - Profit left on table (quanto os WIN trades deixam de ganhar vs resolução em 1.0)

Uso:
    python analyze_ee_trailing_stop.py [--log-dir logs/]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional


QTY = 6.0

TRAIL_ACTIVATE_THR  = 0.88    # trailing só ativa quando bid >= este valor
TRAIL_PULLBACKS     = [0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15]  # em $
BE_MIN_PROFITS      = [0.02, 0.04, 0.06]   # lucro mínimo antes de ativar breakeven


# ---------------------------------------------------------------------------
# Carregamento
# ---------------------------------------------------------------------------

def _load_session(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _collect_trades(rows: list[dict]) -> list[dict]:
    entries: dict[str, dict] = {}
    snaps_by_slug: dict[str, list[dict]] = {}
    trades = []
    for r in rows:
        t = r.get("type")
        if t == "ee_paper_entry":
            slug = r["slug"]
            entries[slug] = r
            snaps_by_slug[slug] = []
        elif t == "snapshot":
            slug = r.get("current_slug", "")
            if slug in entries:
                ep = r.get("ee_position")
                if ep and ep.get("state") in ("entry", "hedged"):
                    snaps_by_slug[slug].append({
                        "secs":   r.get("current_secs"),
                        "up_bid": (r.get("current_exec") or {}).get("up_bid", 0),
                        "dn_bid": (r.get("current_exec") or {}).get("down_bid", 0),
                        "state":  ep.get("state"),
                    })
        elif t == "ee_paper_closed":
            slug = r["slug"]
            if slug not in entries:
                continue
            ee = r.get("ee", {})
            if ee.get("outcome") in (None, "MISSED"):
                entries.pop(slug, None)
                snaps_by_slug.pop(slug, None)
                continue
            entry_r = entries.pop(slug)
            snaps   = snaps_by_slug.pop(slug, [])
            trades.append({
                "slug":       slug,
                "side":       ee["entry_side"],
                "ep":         ee["entry_ep"],
                "entry_secs": ee["entry_secs"],
                "outcome":    ee["outcome"],
                "pnl_actual": ee["pnl"],
                "el_vel":     entry_r.get("el", {}).get("el_vel", 0),
                "snaps":      snaps,
            })
    return trades


def _el(s: dict, side: str) -> float:
    return s["up_bid"] if side == "UP" else s["dn_bid"]


def _opp(s: dict, side: str) -> float:
    return s["dn_bid"] if side == "UP" else s["up_bid"]


# ---------------------------------------------------------------------------
# Simulações
# ---------------------------------------------------------------------------

def _resolve_natural(trade: dict) -> float:
    """PnL se deixarmos resolver naturalmente (sem nenhum stop)."""
    return trade["pnl_actual"]


def sim_trailing(trade: dict, activate_thr: float, pullback: float) -> dict:
    """
    Trailing stop ativado após bid >= activate_thr.
    Sai quando bid < peak - pullback.

    Retorna dict com:
      triggered   — True se o stop ativou
      exit_secs   — secs no momento da saída
      exit_bid    — bid real no poll (preço de mercado disponível)
      stop_price  — preço alvo do stop (pode não ser atingível por gap)
      peak        — peak bid visto até a saída
      gap         — diferença entre stop_price e exit_bid (slippage)
      pnl_ideal   — pnl se saiu ao stop_price (stop limit, otimista)
      pnl_real    — pnl se saiu ao exit_bid   (stop market, realista)
    """
    side, ep = trade["side"], trade["ep"]
    active   = False
    peak     = 0.0
    trail    = 0.0

    for s in trade["snaps"]:
        if s["state"] != "entry":
            continue
        secs = s["secs"] or 999
        bid  = _el(s, side)

        # Resolução normal (secs <= 35 e bid alto)
        if secs <= 35 and bid >= 0.85:
            pnl = (1.0 - ep) * QTY
            return {
                "triggered": False, "note": "WIN@resolution",
                "pnl_ideal": pnl, "pnl_real": pnl,
            }

        # Ativa trailing
        if not active and bid >= activate_thr:
            active = True

        if active:
            if bid > peak:
                peak  = bid
                trail = peak - pullback

            if bid < trail:
                gap        = trail - bid
                pnl_ideal  = (trail  - ep) * QTY
                pnl_real   = (bid    - ep) * QTY
                return {
                    "triggered":  True,
                    "exit_secs":  secs,
                    "exit_bid":   round(bid,   4),
                    "stop_price": round(trail, 4),
                    "peak":       round(peak,  4),
                    "gap":        round(gap,   4),
                    "pnl_ideal":  round(pnl_ideal, 4),
                    "pnl_real":   round(pnl_real,  4),
                }

    return {
        "triggered": False,
        "pnl_ideal": _resolve_natural(trade),
        "pnl_real":  _resolve_natural(trade),
    }


def sim_breakeven(trade: dict, min_profit: float) -> dict:
    """
    Breakeven stop: quando bid sobe min_profit acima da entrada,
    move o stop para a entrada. Sai (ao bid atual) se bid < entry.

    min_profit: ganho mínimo (e.g. 0.04 = 4T) antes de ativar.
    """
    side, ep = trade["side"], trade["ep"]
    activated = False

    for s in trade["snaps"]:
        if s["state"] != "entry":
            continue
        secs = s["secs"] or 999
        bid  = _el(s, side)

        if secs <= 35 and bid >= 0.85:
            pnl = (1.0 - ep) * QTY
            return {
                "triggered": False, "note": "WIN@resolution",
                "pnl_ideal": pnl, "pnl_real": pnl,
            }

        if bid >= ep + min_profit:
            activated = True

        if activated and bid < ep:
            gap       = ep - bid
            pnl_ideal = 0.0                       # saiu ao preço de entrada (ideal)
            pnl_real  = (bid - ep) * QTY          # saiu ao bid atual (real)
            return {
                "triggered":  True,
                "exit_secs":  secs,
                "exit_bid":   round(bid, 4),
                "stop_price": ep,
                "peak":       None,
                "gap":        round(gap, 4),
                "pnl_ideal":  round(pnl_ideal, 4),
                "pnl_real":   round(pnl_real,  4),
            }

    return {
        "triggered": False,
        "pnl_ideal": _resolve_natural(trade),
        "pnl_real":  _resolve_natural(trade),
    }


def sim_trailing_with_breakeven(
    trade: dict, activate_thr: float, pullback: float, min_profit: float
) -> dict:
    """
    Breakeven stop até ativar trailing, depois trailing com pullback.
    Ordem:
      1. Se bid >= entry + min_profit: ativa trailing a partir daí
      2. Trailing: sai quando bid < peak - pullback
      3. Se bid < entry antes de ativar trailing: sai ao entry (ideal) ou bid (real)
    """
    side, ep = trade["side"], trade["ep"]
    be_active  = False
    tr_active  = False
    peak       = 0.0
    trail      = 0.0

    for s in trade["snaps"]:
        if s["state"] != "entry":
            continue
        secs = s["secs"] or 999
        bid  = _el(s, side)

        if secs <= 35 and bid >= 0.85:
            pnl = (1.0 - ep) * QTY
            return {"triggered": False, "note": "WIN@resolution",
                    "pnl_ideal": pnl, "pnl_real": pnl}

        # Ativa trailing quando peak >= activate_thr
        if not tr_active and bid >= activate_thr:
            tr_active = True
            be_active = True  # breakeven também ativo a partir daqui

        if not be_active and bid >= ep + min_profit:
            be_active = True

        if tr_active:
            if bid > peak:
                peak  = bid
                trail = peak - pullback
            if bid < trail:
                gap       = trail - bid
                return {
                    "triggered": True, "mode": "trail",
                    "exit_secs": secs, "exit_bid": round(bid, 4),
                    "stop_price": round(trail, 4), "peak": round(peak, 4),
                    "gap": round(gap, 4),
                    "pnl_ideal": round((trail - ep) * QTY, 4),
                    "pnl_real":  round((bid   - ep) * QTY, 4),
                }
        elif be_active and bid < ep:
            gap = ep - bid
            return {
                "triggered": True, "mode": "breakeven",
                "exit_secs": secs, "exit_bid": round(bid, 4),
                "stop_price": ep, "peak": None,
                "gap": round(gap, 4),
                "pnl_ideal": 0.0,
                "pnl_real":  round((bid - ep) * QTY, 4),
            }

    return {"triggered": False,
            "pnl_ideal": _resolve_natural(trade),
            "pnl_real":  _resolve_natural(trade)}


# ---------------------------------------------------------------------------
# Profit left on table: quanto o WIN trade deixaria de ganhar
# ---------------------------------------------------------------------------

def profit_left(trade: dict, stop_secs: int, stop_bid: float) -> float:
    """Para um WIN trade: lucro perdido ao sair cedo em vez de resolver a 1.0."""
    if trade["outcome"] not in ("WIN",):
        return 0.0
    gain_full  = (1.0 - trade["ep"]) * QTY
    gain_early = (stop_bid - trade["ep"]) * QTY
    return round(gain_full - gain_early, 4)


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

SEP = "-" * 72


def _portfolio(results: list[dict], key: str) -> tuple[float, float, int]:
    total = round(sum(r[key] for r in results), 4)
    wins  = sum(1 for r in results if r[key] > 0)
    wr    = wins / len(results)
    return total, wr, wins


def run(log_dir: str = "logs/") -> None:
    base = Path(log_dir)
    all_trades: list[dict] = []
    for sd in sorted(base.glob("ee_paper_*/")):
        lp = sd / "ee_paper.jsonl"
        if not lp.exists():
            continue
        trades = _collect_trades(_load_session(lp))
        for t in trades:
            t["session"] = sd.name
        all_trades.extend(trades)

    if not all_trades:
        print("Nenhum trade encontrado.")
        return

    n = len(all_trades)
    pnl_base = sum(t["pnl_actual"] for t in all_trades)
    wr_base  = sum(1 for t in all_trades if t["pnl_actual"] > 0) / n

    print(f"\n{'='*72}")
    print(f"  TRAILING STOP & BREAKEVEN — Análise de Liquidez e Slippage")
    print(f"  {n} trades  |  QTY={QTY}  |  BASE WR={wr_base:.1%}  PnL={pnl_base:+.2f}")
    print(f"{'='*72}\n")

    # ---- Tabela de trades ----
    print("TRADES (resumo):")
    print(f"  {'#':>3}  {'Slug':>12}  {'Side':>4}  {'EP':>5}  {'secs':>5}  {'Outcome':>12}  {'PnL':>8}")
    print("  " + SEP)
    for i, t in enumerate(all_trades, 1):
        slug = t["slug"].replace("btc-updown-5m-1779", "…")
        print(f"  {i:>3}  {slug:>12}  {t['side']:>4}  {t['ep']:>5.2f}  "
              f"{t['entry_secs']:>5}  {t['outcome']:>12}  {t['pnl_actual']:>+8.2f}")
    print("  " + SEP)
    print(f"  BASE:  WR={wr_base:.1%}  PnL={pnl_base:+.2f}\n")

    # ---- Detalhe tick-a-tick dos WIN_HEDGE ----
    hedges = [t for t in all_trades if t["outcome"] == "WIN_HEDGE"]
    if hedges:
        print(f"\n{'='*72}")
        print("  TRAJECTÓRIA DETALHADA — WIN_HEDGE (casos críticos)")
        print(f"{'='*72}")
        for t in hedges:
            slug = t["slug"].replace("btc-updown-5m-1779", "…")
            print(f"\n  Slug: {slug}  Side={t['side']}  EP={t['ep']}  PnL={t['pnl_actual']:+.2f}")
            snaps_e = [s for s in t["snaps"] if s["state"] == "entry"]
            prev = t["ep"]
            print(f"  {'secs':>5}  {'EL bid':>8}  {'vs entry':>9}  {'gap':>8}  nota")
            for s in snaps_e:
                bid  = _el(s, t["side"])
                secs = s["secs"]
                gap  = bid - prev
                nota = ""
                if abs(gap) >= 0.10:
                    nota = f"  <- GAP {gap:+.2f}"
                if bid >= TRAIL_ACTIVATE_THR:
                    nota += "  [trail zone]"
                print(f"  {secs:>5}  {bid:>8.3f}  {bid-t['ep']:>+9.3f}  {gap:>+8.3f}{nota}")
                prev = bid

    # ---- Simulação: trailing stop ----
    print(f"\n{'='*72}")
    print(f"  TRAILING STOP — ativa quando bid >= {TRAIL_ACTIVATE_THR}")
    print(f"  (IDEAL = saída ao stop price | REAL = saída ao bid do poll = inclui gaps)")
    print(f"{'='*72}")
    print(f"\n  {'Pullback':>10}  {'Ativa':>6}  {'WR_ideal':>9}  {'PnL_ideal':>10}  "
          f"{'WR_real':>8}  {'PnL_real':>9}  {'Gap total':>10}  vs base")
    print("  " + SEP)

    for pb in TRAIL_PULLBACKS:
        results = [sim_trailing(t, TRAIL_ACTIVATE_THR, pb) for t in all_trades]
        pnl_i   = round(sum(r["pnl_ideal"] for r in results), 4)
        pnl_r   = round(sum(r["pnl_real"]  for r in results), 4)
        wri     = sum(1 for r in results if r["pnl_ideal"] > 0) / n
        wrr     = sum(1 for r in results if r["pnl_real"]  > 0) / n
        trigs   = sum(1 for r in results if r.get("triggered"))
        gap_tot = round(sum(r.get("gap", 0) * QTY for r in results if r.get("triggered")), 4)
        print(f"  {pb:>10.2f}  {trigs:>6}  {wri:>9.1%}  {pnl_i:>+10.2f}  "
              f"{wrr:>8.1%}  {pnl_r:>+9.2f}  {gap_tot:>+10.2f}  {pnl_r-pnl_base:>+8.2f}")

    # ---- Detalhe por trade — trailing 2T (candidato principal) ----
    PB_DETAIL = 0.02
    print(f"\n--- Detalhe por trade: trailing {PB_DETAIL:.2f} (ativado em bid>={TRAIL_ACTIVATE_THR}) ---")
    print(f"  {'#':>3}  {'Slug':>12}  {'Outcome':>12}  {'Trig?':>6}  "
          f"{'Peak':>6}  {'Stop':>6}  {'Exit bid':>9}  {'Gap':>7}  "
          f"{'PnL ideal':>10}  {'PnL real':>9}")
    print("  " + SEP)
    for i, t in enumerate(all_trades, 1):
        r    = sim_trailing(t, TRAIL_ACTIVATE_THR, PB_DETAIL)
        slug = t["slug"].replace("btc-updown-5m-1779", "…")
        trig = "SIM" if r.get("triggered") else "nao"
        peak = f"{r.get('peak',0):.3f}" if r.get("triggered") else "---"
        stop = f"{r.get('stop_price',0):.3f}" if r.get("triggered") else "---"
        ebid = f"{r.get('exit_bid',0):.3f}" if r.get("triggered") else "---"
        gap  = f"{r.get('gap',0):+.3f}"     if r.get("triggered") else "---"
        print(f"  {i:>3}  {slug:>12}  {t['outcome']:>12}  {trig:>6}  "
              f"{peak:>6}  {stop:>6}  {ebid:>9}  {gap:>7}  "
              f"{r['pnl_ideal']:>+10.2f}  {r['pnl_real']:>+9.2f}")

    # ---- Profit left on table ----
    print(f"\n--- Profit deixado na mesa nos WIN trades (trailing 2T vs resolução a 1.0) ---")
    print(f"  (pnl_real   = o que realmente recebemos se stop disparar e bid gapear)")
    print(f"  (pnl_resolv = (1.0 - entry) × qty   — o que ganharíamos sem stop)\n")
    print(f"  {'#':>3}  {'Slug':>12}  {'EP':>5}  {'PnL_resolv':>11}  {'PnL_real':>9}  {'Deixado':>9}  nota")
    print("  " + SEP)
    total_left = 0.0
    for i, t in enumerate(all_trades, 1):
        if t["outcome"] != "WIN":
            continue
        r    = sim_trailing(t, TRAIL_ACTIVATE_THR, PB_DETAIL)
        full = (1.0 - t["ep"]) * QTY
        left = full - r["pnl_real"]
        total_left += left
        slug = t["slug"].replace("btc-updown-5m-1779", "…")
        nota = "STOP ATIVOU" if r.get("triggered") else "resolveu normal"
        print(f"  {i:>3}  {slug:>12}  {t['ep']:>5.2f}  {full:>+11.2f}  "
              f"{r['pnl_real']:>+9.2f}  {left:>+9.2f}  {nota}")
    print("  " + SEP)
    print(f"  Total profit deixado na mesa nos WIN trades: {total_left:>+.2f}")

    # ---- Simulação: breakeven stop ----
    print(f"\n{'='*72}")
    print("  BREAKEVEN STOP")
    print("  (move stop para entry após lucro mínimo; sai ao bid real se bid < entry)")
    print(f"{'='*72}")
    print(f"\n  {'Min profit':>10}  {'Ativa':>6}  {'WR_ideal':>9}  {'PnL_ideal':>10}  "
          f"{'WR_real':>8}  {'PnL_real':>9}  {'Gap total':>10}  vs base")
    print("  " + SEP)

    for mp in BE_MIN_PROFITS:
        results = [sim_breakeven(t, mp) for t in all_trades]
        pnl_i   = round(sum(r["pnl_ideal"] for r in results), 4)
        pnl_r   = round(sum(r["pnl_real"]  for r in results), 4)
        wri     = sum(1 for r in results if r["pnl_ideal"] > 0) / n
        wrr     = sum(1 for r in results if r["pnl_real"]  > 0) / n
        trigs   = sum(1 for r in results if r.get("triggered"))
        gap_tot = round(sum(r.get("gap", 0) * QTY for r in results if r.get("triggered")), 4)
        print(f"  {mp:>10.2f}  {trigs:>6}  {wri:>9.1%}  {pnl_i:>+10.2f}  "
              f"{wrr:>8.1%}  {pnl_r:>+9.2f}  {gap_tot:>+10.2f}  {pnl_r-pnl_base:>+8.2f}")

    # ---- Simulação: trailing + breakeven combinados ----
    print(f"\n{'='*72}")
    print(f"  COMBINAÇÃO: Breakeven Stop + Trailing (ativa em bid>={TRAIL_ACTIVATE_THR})")
    print(f"{'='*72}")
    print(f"\n  {'Min profit':>10}  {'Pullback':>9}  {'Ativa':>6}  "
          f"{'WR_ideal':>9}  {'PnL_ideal':>10}  {'WR_real':>8}  {'PnL_real':>9}  vs base")
    print("  " + SEP)

    for mp in BE_MIN_PROFITS:
        for pb in [0.01, 0.02, 0.03, 0.05]:
            results = [sim_trailing_with_breakeven(t, TRAIL_ACTIVATE_THR, pb, mp)
                       for t in all_trades]
            pnl_i   = round(sum(r["pnl_ideal"] for r in results), 4)
            pnl_r   = round(sum(r["pnl_real"]  for r in results), 4)
            wri     = sum(1 for r in results if r["pnl_ideal"] > 0) / n
            wrr     = sum(1 for r in results if r["pnl_real"]  > 0) / n
            trigs   = sum(1 for r in results if r.get("triggered"))
            print(f"  {mp:>10.2f}  {pb:>9.2f}  {trigs:>6}  "
                  f"{wri:>9.1%}  {pnl_i:>+10.2f}  {wrr:>8.1%}  {pnl_r:>+9.2f}  "
                  f"{pnl_r-pnl_base:>+8.2f}")

    # ---- Comparativo final ----
    print(f"\n{'='*72}")
    print("  COMPARATIVO FINAL — todas as estratégias")
    print(f"{'='*72}\n")

    strategies: list[tuple[str, list[dict]]] = [
        ("Base (atual v1)",
         [{"pnl_ideal": t["pnl_actual"], "pnl_real": t["pnl_actual"]} for t in all_trades]),
        ("PP bid>=0.88 secs<=70",
         _sim_pp(all_trades, 0.88, 70)),
        ("Stop 0.67 fixo",
         _sim_fixed_stop(all_trades, 0.67)),
        (f"Trail 2T  (ativ>={TRAIL_ACTIVATE_THR}) — IDEAL",
         [{"pnl_ideal": sim_trailing(t, TRAIL_ACTIVATE_THR, 0.02)["pnl_ideal"],
           "pnl_real":  sim_trailing(t, TRAIL_ACTIVATE_THR, 0.02)["pnl_ideal"]}
          for t in all_trades]),
        (f"Trail 2T  (ativ>={TRAIL_ACTIVATE_THR}) — REAL",
         [{"pnl_ideal": sim_trailing(t, TRAIL_ACTIVATE_THR, 0.02)["pnl_real"],
           "pnl_real":  sim_trailing(t, TRAIL_ACTIVATE_THR, 0.02)["pnl_real"]}
          for t in all_trades]),
        (f"Trail 5T  (ativ>={TRAIL_ACTIVATE_THR}) — REAL",
         [{"pnl_ideal": sim_trailing(t, TRAIL_ACTIVATE_THR, 0.05)["pnl_real"],
           "pnl_real":  sim_trailing(t, TRAIL_ACTIVATE_THR, 0.05)["pnl_real"]}
          for t in all_trades]),
        ("Breakeven (min=0.04) — IDEAL",
         [{"pnl_ideal": sim_breakeven(t, 0.04)["pnl_ideal"],
           "pnl_real":  sim_breakeven(t, 0.04)["pnl_ideal"]}
          for t in all_trades]),
        ("Breakeven (min=0.04) — REAL",
         [{"pnl_ideal": sim_breakeven(t, 0.04)["pnl_real"],
           "pnl_real":  sim_breakeven(t, 0.04)["pnl_real"]}
          for t in all_trades]),
        ("BE+Trail2T (min=0.04) — IDEAL",
         [{"pnl_ideal": sim_trailing_with_breakeven(t, TRAIL_ACTIVATE_THR, 0.02, 0.04)["pnl_ideal"],
           "pnl_real":  sim_trailing_with_breakeven(t, TRAIL_ACTIVATE_THR, 0.02, 0.04)["pnl_ideal"]}
          for t in all_trades]),
        ("BE+Trail2T (min=0.04) — REAL",
         [{"pnl_ideal": sim_trailing_with_breakeven(t, TRAIL_ACTIVATE_THR, 0.02, 0.04)["pnl_real"],
           "pnl_real":  sim_trailing_with_breakeven(t, TRAIL_ACTIVATE_THR, 0.02, 0.04)["pnl_real"]}
          for t in all_trades]),
    ]

    print(f"  {'Estratégia':35}  {'WR':>6}  {'PnL':>9}  vs base")
    print("  " + SEP)
    for name, res in strategies:
        pnl = round(sum(r["pnl_real"] for r in res), 4)
        wr  = sum(1 for r in res if r["pnl_real"] > 0) / n
        print(f"  {name:35}  {wr:>6.1%}  {pnl:>+9.2f}  {pnl-pnl_base:>+8.2f}")

    # ---- Análise de liquidez / viabilidade ----
    print(f"\n{'='*72}")
    print("  ANÁLISE DE LIQUIDEZ — gaps vs preço alvo do stop")
    print("  (Gap = stop_price - exit_bid. Gap > 0 = slippage por falta de liquidez)")
    print(f"{'='*72}\n")
    print("  Trailing 2T — detalhe de slippage por trade onde stop disparou:\n")
    for i, t in enumerate(all_trades, 1):
        r = sim_trailing(t, TRAIL_ACTIVATE_THR, 0.02)
        if not r.get("triggered"):
            continue
        slug = t["slug"].replace("btc-updown-5m-1779", "…")
        gap  = r.get("gap", 0)
        pnl_diff = r["pnl_ideal"] - r["pnl_real"]
        viability = "FILL possivel" if gap < 0.02 else "GAP - nao preenchivel"
        print(f"  Trade {i:>2} ({slug}): stop={r['stop_price']:.3f}  "
              f"exit_real={r['exit_bid']:.3f}  gap={gap:+.3f}  "
              f"custo_gap=${pnl_diff:+.2f}  [{viability}]")


# ---------------------------------------------------------------------------
# Helpers para comparativo final
# ---------------------------------------------------------------------------

def _sim_pp(trades: list[dict], pp_bid: float, pp_secs: int) -> list[dict]:
    from analyze_ee_stop_vs_hedge import sim_profit_protect
    return [{"pnl_ideal": sim_profit_protect(t, pp_bid, pp_secs)["pnl"],
             "pnl_real":  sim_profit_protect(t, pp_bid, pp_secs)["pnl"]}
            for t in trades]


def _sim_fixed_stop(trades: list[dict], stop_level: float) -> list[dict]:
    from analyze_ee_stop_vs_hedge import sim_stop
    return [{"pnl_ideal": sim_stop(t, stop_level)["pnl"],
             "pnl_real":  sim_stop(t, stop_level)["pnl"]}
            for t in trades]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", default="logs/")
    args = parser.parse_args()
    run(log_dir=args.log_dir)
