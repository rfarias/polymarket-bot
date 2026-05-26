"""
Análise de regime de mercado para o AR (almost resolved) paper.

Para cada trade (evento 'exit'), busca o snapshot mais próximo ao momento
da entrada (trade.created_at) e extrai métricas de mercado disponíveis:
  entry_price          — preço de entrada
  setup_variant        — variante do sinal
  secs_to_end          — segundos restantes no candle na entrada
  distance_bps         — distância BTC ao price-to-beat em bps
  source_divergence    — divergência entre exchanges na entrada
  spot_range_60s       — range BTC nos últimos 60s (volatilidade)
  spot_delta_60s       — variação direcional BTC 60s
  exit_reason          — como saiu (profit_protect, target, stop, timeout)
  pnl_ticks            — resultado em ticks

Uso:
  python _analise_sessoes_ar.py
  python _analise_sessoes_ar.py --logs "logs/current_almost_resolved_paper_*.jsonl"
  python _analise_sessoes_ar.py --min-trades 3
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TZ_LOCAL = timezone(timedelta(hours=-3))
SEP = "=" * 72
SEP2 = "-" * 72


# ---------------------------------------------------------------------------
# Carregamento
# ---------------------------------------------------------------------------

def load_all(pattern: str) -> list[dict]:
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"Nenhum log encontrado: {pattern}")

    # Índice de snapshots por arquivo → list[dict] (para busca por ts)
    all_snaps: dict[str, list[dict]] = {}
    all_exits: list[dict] = []

    for f in files:
        snaps = []
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
                if t == "snapshot":
                    snaps.append(ev)
                elif t == "exit":
                    ev["_file"] = f
                    all_exits.append(ev)
        all_snaps[f] = snaps

    trades = []
    for ev in all_exits:
        f       = ev["_file"]
        trade   = ev.get("trade") or {}
        created = trade.get("created_at") or ev.get("ts") or 0

        # Snapshot mais próximo antes/durante a entrada
        snaps = all_snaps.get(f, [])
        before = [s for s in snaps if s["ts"] <= created + 0.5]
        snap   = before[-1] if before else {}

        sig  = snap.get("signal") or {}
        sc   = snap.get("current_scalp_context") or {}
        ref  = snap.get("reference") or {}

        pnl_ticks = trade.get("pnl_ticks") or 0.0
        pnl_quote = trade.get("pnl_quote") or 0.0
        win = 1 if pnl_ticks > 0 else 0

        ts = created
        dt = datetime.fromtimestamp(ts, tz=TZ_LOCAL) if ts else None

        trades.append({
            "file":             f.split("\\")[-1].split("/")[-1],
            "session":          f.split("\\")[-1].split("/")[-1].replace("current_almost_resolved_paper_","").replace(".jsonl",""),
            "slug":             snap.get("current_slug", ""),
            "side":             trade.get("side", ""),
            "entry_price":      trade.get("entry_price"),
            "exit_price":       trade.get("exit_price"),
            "exit_reason":      trade.get("exit_reason", ""),
            "setup_variant":    trade.get("setup_variant") or sig.get("setup_variant", ""),
            "pnl_ticks":        pnl_ticks,
            "pnl_quote":        pnl_quote,
            "win":              win,
            "entry_distance_bps": trade.get("entry_distance_bps"),
            "entry_buffer_bps": trade.get("entry_buffer_bps"),
            # Métricas de mercado do snapshot na entrada
            "secs_to_end":      sig.get("secs_to_end") or sc.get("secs_to_end"),
            "distance_bps":     sig.get("distance_to_price_to_beat_bps"),
            "source_div":       ref.get("source_divergence_bps") or sc.get("source_divergence_bps"),
            "spot_range_60s":   sc.get("spot_range_60s_usd"),
            "spot_delta_60s":   sc.get("spot_delta_60s_bps"),
            "score":            sig.get("score") or snap.get("stats_so_far", {}).get("score"),
            "hour_local":       dt.hour if dt else None,
            "date_local":       dt.strftime("%Y-%m-%d") if dt else None,
            "ts":               ts,
        })

    return trades


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_avg(lst):
    vals = [v for v in lst if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None

def bucket(value, edges):
    if value is None:
        return "N/A"
    for i, e in enumerate(edges):
        if value < e:
            lo = edges[i-1] if i > 0 else float("-inf")
            return f"[{lo:.3g},{e:.3g})"
    return f"[{edges[-1]:.3g},+inf)"

def group_stats(group):
    n     = len(group)
    wins  = sum(t["win"] for t in group)
    ticks = sum(t["pnl_ticks"] for t in group)
    quote = sum(t["pnl_quote"] for t in group)
    wr    = wins / n * 100 if n else 0
    return n, wins, n-wins, wr, ticks, quote

def print_table(rows, header, min_n=2):
    print(f"\n  {header}")
    print(f"  {'Bucket':<22} {'N':>4} {'WR%':>6} {'Ticks':>8} {'AvgTick':>8}  Exit reasons")
    print(f"  {'-'*22} {'-'*4} {'-'*6} {'-'*8} {'-'*8}  {'-'*30}")
    for bkt, group in sorted(rows.items()):
        if bkt == "N/A" or len(group) < min_n:
            continue
        n, w, l, wr, ticks, _ = group_stats(group)
        avg_t = ticks / n
        reasons = defaultdict(int)
        for t in group:
            reasons[t["exit_reason"]] += 1
        r_str = " ".join(f"{k[:3]}={v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
        flag = " ◄BAD" if wr < 80 else ""
        print(f"  {bkt:<22} {n:>4} {wr:>5.1f}% {ticks:>+8.1f} {avg_t:>+8.2f}  {r_str}{flag}")


def analyze_field(trades, field, edges, header, min_n=2):
    groups = defaultdict(list)
    for t in trades:
        groups[bucket(t.get(field), edges)].append(t)
    print_table(groups, header, min_n)


def analyze_categorical(trades, field, header, min_n=2):
    groups = defaultdict(list)
    for t in trades:
        groups[str(t.get(field) or "N/A")].append(t)
    rows = {k: v for k, v in groups.items() if len(v) >= min_n}
    print_table(rows, header, min_n)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", default="logs/current_almost_resolved_paper_*.jsonl")
    ap.add_argument("--min-trades", type=int, default=2)
    args = ap.parse_args()

    trades = load_all(args.logs)
    n      = len(trades)
    wins   = sum(t["win"] for t in trades)
    ticks  = sum(t["pnl_ticks"] for t in trades)
    quote  = sum(t["pnl_quote"] for t in trades)
    mt     = args.min_trades

    # Por sessão
    by_session: dict[str, list] = defaultdict(list)
    for t in trades:
        by_session[t["session"]].append(t)

    print(SEP)
    print("  ANÁLISE AR PAPER — current_almost_resolved")
    print(f"  {len(by_session)} sessões  |  {n} trades  |  W={wins} L={n-wins}  "
          f"WR={wins/n*100:.1f}%  Ticks={ticks:+.0f}  PnL=${quote:+.2f}")
    print(SEP)

    # ------------------------------------------------------------------
    # 1. Ranking de sessões
    # ------------------------------------------------------------------
    print("\n[1] RANKING DE SESSOES (por WR e ticks)")
    sessions = []
    for sid, ts in by_session.items():
        sn, sw, sl, swr, sticks, squote = group_stats(ts)
        avg_ep   = safe_avg([t["entry_price"] for t in ts])
        avg_dist = safe_avg([t["distance_bps"] for t in ts])
        avg_div  = safe_avg([t["source_div"] for t in ts])
        avg_rng  = safe_avg([t["spot_range_60s"] for t in ts])
        first_ts = min(t["ts"] for t in ts if t["ts"])
        dt = datetime.fromtimestamp(first_ts, tz=TZ_LOCAL)
        sessions.append((sid, sn, swr, sticks, squote, avg_ep, avg_dist, avg_div, avg_rng,
                         dt.strftime("%Y-%m-%d %H:%M"), ts))
    sessions.sort(key=lambda x: -x[2])  # por WR

    print(f"\n  {'Sessão':<45} {'N':>4} {'WR%':>6} {'Ticks':>7} {'ep':>5} "
          f"{'dist_bps':>9} {'src_div':>8} {'rng60':>7}")
    print(f"  {'-'*45} {'-'*4} {'-'*6} {'-'*7} {'-'*5} {'-'*9} {'-'*8} {'-'*7}")
    for s in sessions:
        sid, sn, swr, sticks, sq, ep, dist, div, rng, dt, _ = s
        def f(v): return f"{v:.2f}" if v is not None else " N/A"
        mark = "  *** RUIM" if swr < 80 and sn >= 3 else ""
        print(f"  {sid:<45} {sn:>4} {swr:>5.1f}% {sticks:>+7.1f} {f(ep):>5} "
              f"{f(dist):>9} {f(div):>8} {f(rng):>7}{mark}")

    # ------------------------------------------------------------------
    # 2. Por variante de setup
    # ------------------------------------------------------------------
    print(f"\n\n[2] POR SETUP_VARIANT")
    analyze_categorical(trades, "setup_variant", "setup_variant", mt)

    # ------------------------------------------------------------------
    # 3. Por exit_reason
    # ------------------------------------------------------------------
    print(f"\n\n[3] POR EXIT_REASON")
    analyze_categorical(trades, "exit_reason", "exit_reason", mt)

    # ------------------------------------------------------------------
    # 4. Zonas de risco — métricas de mercado
    # ------------------------------------------------------------------
    print(f"\n\n[4] ZONAS DE RISCO — MÉTRICAS DE MERCADO NA ENTRADA")

    analyze_field(trades, "entry_price",
                  [0.88, 0.91, 0.94, 0.97, 0.985],
                  "entry_price (preço de entrada)", mt)

    analyze_field(trades, "secs_to_end",
                  [20, 35, 50, 70, 100, 150],
                  "secs_to_end (segundos restantes no candle)", mt)

    analyze_field(trades, "distance_bps",
                  [5, 8, 12, 16, 20, 30],
                  "distance_to_price_to_beat_bps (distância BTC ao threshold)", mt)

    analyze_field(trades, "source_div",
                  [3, 6, 10, 15, 20, 30],
                  "source_divergence_bps (divergência entre exchanges)", mt)

    analyze_field(trades, "spot_range_60s",
                  [10, 20, 30, 50, 80, 120],
                  "spot_range_60s_usd (volatilidade BTC 60s)", mt)

    analyze_field(trades, "entry_distance_bps",
                  [5, 8, 10, 12, 15, 20],
                  "entry_distance_bps (distância da ordem ao mid)", mt)

    # ------------------------------------------------------------------
    # 5. Por hora do dia
    # ------------------------------------------------------------------
    print(f"\n\n[5] POR HORA LOCAL (UTC-3)")
    hour_groups = defaultdict(list)
    for t in trades:
        h = t.get("hour_local")
        if h is not None:
            hour_groups[h].append(t)

    print(f"\n  {'Hora':<8} {'N':>4} {'WR%':>6} {'Ticks':>8} {'AvgTick':>8}  Exit reasons")
    print(f"  {'-'*8} {'-'*4} {'-'*6} {'-'*8} {'-'*8}  {'-'*30}")
    for h in sorted(hour_groups):
        group = hour_groups[h]
        if len(group) < mt:
            continue
        n2, w, l, wr, ticks2, _ = group_stats(group)
        avg_t = ticks2 / n2
        reasons = defaultdict(int)
        for t in group:
            reasons[t["exit_reason"]] += 1
        r_str = " ".join(f"{k[:3]}={v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
        flag = " ◄BAD" if wr < 80 and n2 >= 3 else ""
        print(f"  {h:02d}h      {n2:>4} {wr:>5.1f}% {ticks2:>+8.1f} {avg_t:>+8.2f}  {r_str}{flag}")

    # ------------------------------------------------------------------
    # 6. Perfil dos perdedores
    # ------------------------------------------------------------------
    losers  = [t for t in trades if t["win"] == 0]
    winners = [t for t in trades if t["win"] == 1]

    print(f"\n\n[6] PERFIL DOS TRADES PERDEDORES ({len(losers)}) vs VENCEDORES ({len(winners)})")
    def fstats(group, field):
        vals = [t[field] for t in group if t.get(field) is not None]
        if not vals: return "N/A"
        return f"avg={sum(vals)/len(vals):.2f} [{min(vals):.1f}-{max(vals):.1f}]"

    print(f"\n  {'Campo':<25} {'Perdas':>22} {'Vitórias':>22}")
    print(f"  {'-'*25} {'-'*22} {'-'*22}")
    for field, label in [
        ("entry_price",       "entry_price"),
        ("secs_to_end",       "secs_to_end"),
        ("distance_bps",      "distance_bps"),
        ("source_div",        "source_divergence"),
        ("spot_range_60s",    "spot_range_60s"),
        ("entry_distance_bps","entry_distance_bps"),
    ]:
        print(f"  {label:<25} {fstats(losers,field):>22} {fstats(winners,field):>22}")

    if losers:
        print(f"\n  Detalhes dos trades com perda:")
        print(f"  {'Sessão':<35} {'ep':>5} {'secs':>5} {'dist':>6} {'div':>6} {'rng':>6} {'var':<25} {'exit':<15} {'ticks':>6}")
        print(f"  {'-'*35} {'-'*5} {'-'*5} {'-'*6} {'-'*6} {'-'*6} {'-'*25} {'-'*15} {'-'*6}")
        for t in sorted(losers, key=lambda x: x["pnl_ticks"]):
            def fv(v, fmt=".1f"): return f"{v:{fmt}}" if v is not None else "N/A"
            print(f"  {t['session']:<35} {fv(t['entry_price'],'.3f'):>5} "
                  f"{fv(t['secs_to_end'],'.0f'):>5} {fv(t['distance_bps'],'.1f'):>6} "
                  f"{fv(t['source_div'],'.1f'):>6} {fv(t['spot_range_60s'],'.1f'):>6} "
                  f"{str(t['setup_variant']):<25} {t['exit_reason']:<15} {t['pnl_ticks']:>+6.1f}")

    # ------------------------------------------------------------------
    # 7. Resumo acionável
    # ------------------------------------------------------------------
    print(f"\n\n[7] RESUMO ACIONÁVEL")
    print(f"""
  CARACTERÍSTICAS DO AR PAPER HOJE (107 trades, 100% WR):
    - BTC em faixa controlada, spot_range_60s baixo
    - source_divergence_bps geralmente < 15
    - Todas as variantes funcionaram (standard, dual_rich_late_limit, extreme_99_limit)

  ATENÇÃO: análise baseada principalmente em 1 sessão boa (26/05) + logs históricos
  sem pnl_quote (versões antigas do runner). Dataset limitado para conclusões firmes.

  PENDÊNCIA CRÍTICA:
    Os logs reais do runner (outro PC) têm trades com dinheiro real e condições
    de mercado mais diversas. Rodar este script nos logs reais antes de qualquer gate:
      python _analise_sessoes_ar.py --logs "logs/ar_real_*/ar_real.jsonl"
    (ajustar glob conforme nome dos arquivos no outro PC)
""")


if __name__ == "__main__":
    main()
