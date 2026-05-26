"""
Análise de regime de mercado para o EE paper.

Para cada trade fechado, extrai métricas disponíveis no momento da entrada
e agrupa por faixas para identificar condições que correlacionam com bons
ou maus resultados.

Métricas analisadas:
  el_vel          — velocidade do bid do early leader
  ep              — preço de entrada
  leader_spread   — separação líder vs counter no momento da entrada
  el_bid_180      — nível absoluto do bid do líder em 180s
  secs            — segundo do candle em que entrou
  n_s180          — amostras do líder nos últimos 180s (estabilidade)
  hour_local      — hora local (UTC-3) — proxy de liquidez BTC

Uso:
  python _sim_regime_ee.py
  python _sim_regime_ee.py --logs "logs/ee_paper_*/ee_paper.jsonl"
  python _sim_regime_ee.py --min-trades 3   # mínimo de trades por bucket
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# Força UTF-8 no stdout (Windows cp1252 não suporta caracteres de linha)
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TZ_LOCAL = timezone(timedelta(hours=-3))


# ---------------------------------------------------------------------------
# Carregamento
# ---------------------------------------------------------------------------

def load_trades(pattern: str) -> list[dict]:
    """Junta entry + close de cada trade em um único dicionário."""
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"Nenhum log encontrado: {pattern}")

    entries: dict[str, dict] = {}   # slug → entry event
    trades: list[dict] = []

    for f in files:
        with open(f, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue

                t = ev.get("type", "")
                slug = ev.get("slug", "")

                if t == "ee_paper_entry":
                    entries[slug] = ev

                elif t in ("ee_paper_closed", "ee_paper_stop_loss"):
                    entry = entries.pop(slug, None)
                    if entry is None:
                        continue

                    ee = ev.get("ee") or {}
                    el = entry.get("el") or {}
                    ep = entry.get("ep")

                    # Snapshot mais próximo ao entry está embutido na mesma
                    # linha de tempo; usamos os campos do entry + el diretamente.

                    # leader_spread: bid do líder - bid do counter no snapshot
                    # Reconstruímos a partir do ep e do lado.
                    # up_bid / down_bid não estão no entry; usamos ep como proxy
                    # do líder e inferimos o spread via session_id para snapshots.
                    # Por simplicidade, derivamos leader_spread de snap se disponível.
                    # (campo adicionado na lógica abaixo via snapshot pre-entry)

                    pnl = ee.get("pnl") or 0.0
                    outcome = ee.get("outcome") or (ev.get("type") or "")
                    win = 1 if pnl > 0 else 0

                    ts = entry.get("ts", 0)
                    dt = datetime.fromtimestamp(ts, tz=TZ_LOCAL)

                    trades.append({
                        "slug": slug,
                        "session": entry.get("session_id", ""),
                        "side": entry.get("side", ""),
                        "ep": ep,
                        "secs": entry.get("secs"),
                        "el_vel": el.get("el_vel"),
                        "el_bid_180": el.get("el_bid_180"),
                        "el_bid_240": el.get("el_bid_240"),
                        "n_s180": el.get("n_s180"),
                        "f3_ok": el.get("f3_ok"),
                        "pnl": pnl,
                        "outcome": outcome,
                        "win": win,
                        "hour_local": dt.hour,
                        "ts": ts,
                    })

    return trades


def attach_leader_spread(trades: list[dict], pattern: str) -> None:
    """
    Lê os snapshots para extrair up_bid/down_bid no momento da entrada
    e calcula leader_spread = bid_lider - bid_counter.
    """
    files = sorted(glob.glob(pattern))

    # índice: slug → list[snapshot]
    snaps_by_slug: dict[str, list[dict]] = defaultdict(list)
    for f in files:
        with open(f, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "snapshot":
                    slug = ev.get("current_slug", "")
                    snaps_by_slug[slug].append(ev)

    for trade in trades:
        slug = trade["slug"]
        entry_ts = trade["ts"]
        snaps = [s for s in snaps_by_slug.get(slug, []) if s["ts"] <= entry_ts + 0.01]
        if not snaps:
            trade["leader_spread"] = None
            continue
        snap = snaps[-1]
        exec_ = snap.get("current_exec") or {}
        up_bid = exec_.get("up_bid")
        down_bid = exec_.get("down_bid")
        if up_bid is None or down_bid is None:
            trade["leader_spread"] = None
            continue
        side = trade.get("side", "")
        if side == "UP":
            trade["leader_spread"] = round(up_bid - down_bid, 4)
        elif side == "DOWN":
            trade["leader_spread"] = round(down_bid - up_bid, 4)
        else:
            trade["leader_spread"] = round(abs(up_bid - down_bid), 4)


# ---------------------------------------------------------------------------
# Análise por bucket
# ---------------------------------------------------------------------------

def bucket(value, edges: list[float]) -> str:
    if value is None:
        return "N/A"
    for i, edge in enumerate(edges):
        if value < edge:
            lo = edges[i - 1] if i > 0 else float("-inf")
            return f"[{lo:.3g}, {edge:.3g})"
    lo = edges[-1]
    return f"[{lo:.3g}, +∞)"


def stats(group: list[dict]) -> dict:
    n = len(group)
    wins = sum(t["win"] for t in group)
    total_pnl = sum(t["pnl"] for t in group)
    avg_pnl = total_pnl / n if n else 0
    wr = wins / n * 100 if n else 0
    return {"n": n, "wins": wins, "losses": n - wins, "wr": wr,
            "total_pnl": total_pnl, "avg_pnl": avg_pnl}


def analyze(trades: list[dict], metric: str, edges: list[float],
            min_trades: int = 1, label: str | None = None) -> None:
    label = label or metric
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[bucket(t.get(metric), edges)].append(t)

    rows = []
    for bkt, group in sorted(groups.items()):
        s = stats(group)
        if s["n"] >= min_trades:
            rows.append((bkt, s))

    if not rows:
        return

    print(f"\n{'─'*60}")
    print(f"  {label.upper()}  (n total={len(trades)}, min_trades_bucket={min_trades})")
    print(f"{'─'*60}")
    print(f"  {'Bucket':<22} {'N':>4} {'W':>4} {'L':>4} {'WR%':>6} {'TotalPnL':>10} {'AvgPnL':>8}")
    print(f"  {'-'*22} {'-'*4} {'-'*4} {'-'*4} {'-'*6} {'-'*10} {'-'*8}")
    for bkt, s in rows:
        flag = " ◄" if s["wr"] < 50 and s["n"] >= min_trades else ""
        print(f"  {bkt:<22} {s['n']:>4} {s['wins']:>4} {s['losses']:>4} "
              f"{s['wr']:>5.1f}% {s['total_pnl']:>+10.2f} {s['avg_pnl']:>+8.2f}{flag}")


def analyze_hour(trades: list[dict], min_trades: int = 1) -> None:
    groups: dict[int, list[dict]] = defaultdict(list)
    for t in trades:
        h = t.get("hour_local")
        if h is not None:
            groups[h].append(t)

    rows = [(h, stats(g)) for h, g in sorted(groups.items()) if len(g) >= min_trades]
    if not rows:
        return

    print(f"\n{'─'*60}")
    print(f"  HORA LOCAL (UTC-3)  (n total={len(trades)})")
    print(f"{'─'*60}")
    print(f"  {'Hora':<10} {'N':>4} {'W':>4} {'L':>4} {'WR%':>6} {'TotalPnL':>10} {'AvgPnL':>8}")
    print(f"  {'-'*10} {'-'*4} {'-'*4} {'-'*4} {'-'*6} {'-'*10} {'-'*8}")
    for h, s in rows:
        flag = " ◄" if s["wr"] < 50 and s["n"] >= min_trades else ""
        print(f"  {h:02d}h       {s['n']:>4} {s['wins']:>4} {s['losses']:>4} "
              f"{s['wr']:>5.1f}% {s['total_pnl']:>+10.2f} {s['avg_pnl']:>+8.2f}{flag}")


def analyze_combined(trades: list[dict], min_trades: int = 1) -> None:
    """
    Combinação el_vel × ep: identifica zonas de risco cruzado.
    """
    VEL_THRESH = 0.13
    EP_THRESH = 0.85

    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        v = t.get("el_vel")
        e = t.get("ep")
        if v is None or e is None:
            continue
        vel_label = f"vel>={VEL_THRESH}" if v >= VEL_THRESH else f"vel<{VEL_THRESH}"
        ep_label  = f"ep>={EP_THRESH}" if e >= EP_THRESH else f"ep<{EP_THRESH}"
        groups[f"{vel_label} | {ep_label}"].append(t)

    rows = [(k, stats(g)) for k, g in sorted(groups.items()) if len(g) >= min_trades]
    if not rows:
        return

    print(f"\n{'─'*60}")
    print(f"  COMBINADO: el_vel × ep  (gate candidato: vel>=0.13)")
    print(f"{'─'*60}")
    print(f"  {'Zona':<35} {'N':>4} {'WR%':>6} {'TotalPnL':>10} {'AvgPnL':>8}")
    print(f"  {'-'*35} {'-'*4} {'-'*6} {'-'*10} {'-'*8}")
    for k, s in rows:
        flag = " ◄" if s["wr"] < 60 and s["n"] >= min_trades else ""
        print(f"  {k:<35} {s['n']:>4} {s['wr']:>5.1f}% {s['total_pnl']:>+10.2f} {s['avg_pnl']:>+8.2f}{flag}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logs", default="logs/ee_paper_*/ee_paper.jsonl",
                        help="Glob para os arquivos de log EE paper")
    parser.add_argument("--min-trades", type=int, default=2,
                        help="Mínimo de trades por bucket para exibir (default: 2)")
    args = parser.parse_args()

    print(f"Carregando: {args.logs}")
    trades = load_trades(args.logs)
    attach_leader_spread(trades, args.logs)

    n_total = len(trades)
    n_wins  = sum(t["win"] for t in trades)
    total_pnl = sum(t["pnl"] for t in trades)
    print(f"\n{'═'*60}")
    print(f"  TOTAL: {n_total} trades  W={n_wins} L={n_total-n_wins}  "
          f"WR={n_wins/n_total*100:.1f}%  PnL={total_pnl:+.2f}")
    print(f"  Período: {len(set(t['session'] for t in trades))} sessões")
    print(f"{'═'*60}")

    mt = args.min_trades

    analyze(trades, "el_vel",
            [0.08, 0.10, 0.12, 0.13, 0.15, 0.18, 0.22],
            min_trades=mt,
            label="EL_VEL (velocidade do bid líder)")

    analyze(trades, "ep",
            [0.80, 0.82, 0.84, 0.86, 0.88, 0.90],
            min_trades=mt,
            label="EP (preço de entrada)")

    analyze(trades, "leader_spread",
            [0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75],
            min_trades=mt,
            label="LEADER_SPREAD (bid_líder − bid_counter)")

    analyze(trades, "el_bid_180",
            [0.75, 0.80, 0.83, 0.86, 0.89, 0.92],
            min_trades=mt,
            label="EL_BID_180 (bid do líder em 180s)")

    analyze(trades, "secs",
            [100, 120, 140, 160, 180, 210],
            min_trades=mt,
            label="SECS (segundo do candle na entrada)")

    analyze(trades, "n_s180",
            [1, 3, 5, 7, 10],
            min_trades=mt,
            label="N_S180 (amostras do líder em 180s)")

    analyze_hour(trades, min_trades=mt)

    analyze_combined(trades, min_trades=mt)

    # Resumo das piores zonas
    print(f"\n{'═'*60}")
    print("  TRADES COM PERDA — campos no momento da entrada")
    print(f"{'═'*60}")
    losers = [t for t in trades if t["win"] == 0]
    print(f"  Total de perdas: {len(losers)}")
    if losers:
        print(f"  {'Sessao':<28} {'ep':>5} {'el_vel':>7} {'spread':>8} {'secs':>6} {'pnl':>7} {'outcome'}")
        print(f"  {'-'*28} {'-'*5} {'-'*7} {'-'*8} {'-'*6} {'-'*7} {'-'*15}")
        for t in sorted(losers, key=lambda x: x["pnl"]):
            s = t.get("leader_spread")
            spread_str = f"{s:.3f}" if s is not None else "  N/A"
            print(f"  {t['session']:<28} {t['ep']:>5.2f} {t['el_vel']:>7.4f} "
                  f"{spread_str:>8} {t['secs']:>6} {t['pnl']:>+7.2f}  {t['outcome']}")


if __name__ == "__main__":
    main()
