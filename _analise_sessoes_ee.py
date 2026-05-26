"""
Análise consolidada das sessões EE paper.

Objetivo: identificar padrões de bom e mau desempenho para guiar
o que replicar (ou bloquear) no runner real.

Uso:
  python _analise_sessoes_ee.py
  python _analise_sessoes_ee.py --logs "logs/ee_paper_*/ee_paper.jsonl"
  python _analise_sessoes_ee.py --top 5   # exibe N melhores/piores sessões
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TZ_LOCAL = timezone(timedelta(hours=-3))

WIN_OUTCOMES  = {"WIN", "PROFIT_PROTECT"}
LOSS_OUTCOMES = {"STOP_LOSS", "REVERSAL", "WIN_HEDGE"}
SEP = "=" * 72


# ---------------------------------------------------------------------------
# Carregamento de trades (apenas ee_paper_closed — inclui pnl real)
# ---------------------------------------------------------------------------

def load_all(pattern: str) -> tuple[list[dict], list[dict]]:
    """Retorna (trades, sessions).

    trades   — um dict por trade com todos os campos disponíveis.
    sessions — um dict por sessão com métricas agregadas.
    """
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"Nenhum log encontrado: {pattern}")

    # Índice entry por (session_id, slug)
    entries: dict[tuple, dict] = {}
    # Snapshots por (session_id, slug) → list
    snap_idx: dict[tuple, list] = defaultdict(list)

    raw_events: list[dict] = []

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
                raw_events.append(ev)

    # Primeira passagem: indexar entries e snapshots
    for ev in raw_events:
        t    = ev.get("type", "")
        sid  = ev.get("session_id", "")
        slug = ev.get("slug", "") or ev.get("current_slug", "")
        key  = (sid, slug)

        if t == "ee_paper_entry":
            entries[key] = ev
        elif t == "snapshot":
            snap_idx[key].append(ev)

    # Segunda passagem: juntar closed com entry + snapshot
    trades: list[dict] = []
    for ev in raw_events:
        if ev.get("type") != "ee_paper_closed":
            continue

        sid  = ev.get("session_id", "")
        slug = ev.get("slug", "")
        key  = (sid, slug)

        ee      = ev.get("ee") or {}
        outcome = ee.get("outcome", "")
        pnl     = ee.get("pnl") or 0.0
        win     = 1 if outcome in WIN_OUTCOMES else 0

        entry = entries.get(key) or {}
        el    = entry.get("el") or {}
        btc   = entry.get("btc") or {}
        ep    = entry.get("ep") or ee.get("entry_ep")
        secs  = entry.get("secs") or ee.get("entry_secs")

        # Snapshot mais próximo antes da entrada
        entry_ts = entry.get("ts") or ev.get("ts")
        snaps_before = [s for s in snap_idx.get(key, []) if s["ts"] <= entry_ts + 0.1]
        snap = snaps_before[-1] if snaps_before else {}
        exec_ = snap.get("current_exec") or {}
        up_bid   = exec_.get("up_bid")
        down_bid = exec_.get("down_bid")
        side     = entry.get("side") or ee.get("entry_side")
        if up_bid is not None and down_bid is not None:
            leader_spread = round((up_bid - down_bid) if side == "UP"
                                  else (down_bid - up_bid), 4)
        else:
            leader_spread = None

        ts = entry_ts or 0.0
        dt = datetime.fromtimestamp(ts, tz=TZ_LOCAL) if ts else None

        trades.append({
            "session_id":    sid,
            "slug":          slug,
            "side":          side,
            "ep":            ep,
            "secs":          secs,
            "el_vel":        el.get("el_vel"),
            "el_bid_180":    el.get("el_bid_180"),
            "el_bid_240":    el.get("el_bid_240"),
            "n_s180":        el.get("n_s180"),
            "leader_spread": leader_spread,
            "outcome":       outcome,
            "pnl":           pnl,
            "win":           win,
            "hour_local":    dt.hour if dt else None,
            "date_local":    dt.strftime("%Y-%m-%d") if dt else None,
            "datetime_local": dt.strftime("%Y-%m-%d %H:%M") if dt else None,
            "ts":            ts,
            # BTC info (disponível apenas a partir de 26/05)
            "btc_dist_usd":   btc.get("dist_usd"),
            "btc_dist_bps":   btc.get("dist_bps"),
            "btc_range_3m":   btc.get("range_3m_usd"),
            "btc_delta_1m":   btc.get("delta_1m_usd"),
            "btc_delta_3m":   btc.get("delta_3m_usd"),
            "btc_adverse_1m": btc.get("adverse_1m"),
            "btc_above":      btc.get("above"),
        })

    # Agregar por sessão
    sessions_raw: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        sessions_raw[t["session_id"]].append(t)

    sessions: list[dict] = []
    for sid, ts in sessions_raw.items():
        n     = len(ts)
        wins  = sum(t["win"] for t in ts)
        pnl   = round(sum(t["pnl"] for t in ts), 2)
        wr    = wins / n * 100 if n else 0
        avg_p = pnl / n if n else 0

        def safe_avg(field):
            vals = [t[field] for t in ts if t[field] is not None]
            return round(sum(vals) / len(vals), 4) if vals else None

        outcomes = defaultdict(int)
        for t in ts:
            outcomes[t["outcome"]] += 1

        first_ts = min(t["ts"] for t in ts)
        dt = datetime.fromtimestamp(first_ts, tz=TZ_LOCAL)

        sessions.append({
            "session_id":    sid,
            "date":          dt.strftime("%Y-%m-%d"),
            "hour_start":    dt.strftime("%H:%M"),
            "n":             n,
            "wins":          wins,
            "losses":        n - wins,
            "wr":            round(wr, 1),
            "pnl":           pnl,
            "avg_pnl":       round(avg_p, 2),
            "avg_vel":       safe_avg("el_vel"),
            "avg_ep":        safe_avg("ep"),
            "avg_spread":    safe_avg("leader_spread"),
            "outcomes":      dict(outcomes),
            "trades":        ts,
        })

    sessions.sort(key=lambda s: s["pnl"], reverse=True)
    return trades, sessions


# ---------------------------------------------------------------------------
# Helpers de impressão
# ---------------------------------------------------------------------------

def _oc(outcomes: dict) -> str:
    parts = []
    for k in ("PROFIT_PROTECT", "WIN", "STOP_LOSS", "REVERSAL", "WIN_HEDGE"):
        if outcomes.get(k):
            parts.append(f"{k[:2]}={outcomes[k]}")
    return " ".join(parts)


def safe(val, fmt=".3f"):
    return f"{val:{fmt}}" if val is not None else "  N/A"


# ---------------------------------------------------------------------------
# Análise por grupo de características
# ---------------------------------------------------------------------------

def compare_groups(trades: list[dict], field: str, label: str, edges: list[float]) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        v = t.get(field)
        if v is None:
            groups["N/A"].append(t)
            continue
        bkt = None
        for i, edge in enumerate(edges):
            if v < edge:
                lo = edges[i-1] if i > 0 else float("-inf")
                bkt = f"[{lo:.3g},{edge:.3g})"
                break
        if bkt is None:
            bkt = f"[{edges[-1]:.3g},+inf)"
        groups[bkt].append(t)

    rows = [(k, g) for k, g in sorted(groups.items()) if k != "N/A" and len(g) >= 2]
    if not rows:
        return

    print(f"\n  {label}")
    print(f"  {'Faixa':<22} {'N':>4} {'WR%':>6} {'PnL':>8} {'AvgPnL':>8} {'PP':>4} {'SL':>4} {'REV':>4}")
    print(f"  {'-'*22} {'-'*4} {'-'*6} {'-'*8} {'-'*8} {'-'*4} {'-'*4} {'-'*4}")
    for bkt, g in rows:
        n     = len(g)
        wins  = sum(t["win"] for t in g)
        pnl   = sum(t["pnl"] for t in g)
        wr    = wins/n*100
        avgp  = pnl/n
        pp    = sum(1 for t in g if t["outcome"] == "PROFIT_PROTECT")
        sl    = sum(1 for t in g if t["outcome"] == "STOP_LOSS")
        rev   = sum(1 for t in g if t["outcome"] in ("REVERSAL","WIN_HEDGE"))
        flag  = " ◄BAD" if wr < 60 else (" ◄OK?" if wr < 75 else "")
        print(f"  {bkt:<22} {n:>4} {wr:>5.1f}% {pnl:>+8.2f} {avgp:>+8.2f} "
              f"{pp:>4} {sl:>4} {rev:>4}{flag}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", default="logs/ee_paper_*/ee_paper.jsonl")
    ap.add_argument("--top", type=int, default=8,
                    help="Número de sessões a exibir no ranking (default 8)")
    args = ap.parse_args()

    trades, sessions = load_all(args.logs)
    n_total = len(trades)
    n_wins  = sum(t["win"] for t in trades)
    total_pnl = sum(t["pnl"] for t in trades)
    n_sessions = len(sessions)

    print(SEP)
    print(f"  ANÁLISE SESSÕES EE PAPER")
    print(f"  {n_sessions} sessões  |  {n_total} trades  |  "
          f"W={n_wins} L={n_total-n_wins}  WR={n_wins/n_total*100:.1f}%  "
          f"PnL={total_pnl:+.2f}")
    print(SEP)

    # ------------------------------------------------------------------
    # 1. Ranking geral de sessões
    # ------------------------------------------------------------------
    print("\n[1] RANKING DE SESSOES (ordenado por PnL)")
    print(f"\n  {'Sessão':<22} {'Data':<11} {'Hr':>5} {'N':>4} {'WR%':>6} "
          f"{'PnL':>7} {'AvgPnL':>7} {'vel':>6} {'ep':>5} {'Outcomes'}")
    print(f"  {'-'*22} {'-'*11} {'-'*5} {'-'*4} {'-'*6} {'-'*7} {'-'*7} "
          f"{'-'*6} {'-'*5} {'-'*30}")

    for s in sessions:
        vel  = safe(s["avg_vel"], ".3f")
        ep   = safe(s["avg_ep"],  ".3f")
        mark = "  *** TOP" if sessions.index(s) < args.top // 2 else (
               "  *** RUIM" if sessions.index(s) >= len(sessions) - args.top // 2 else "")
        print(f"  {s['session_id'][9:]:<22} {s['date']:<11} {s['hour_start']:>5} "
              f"{s['n']:>4} {s['wr']:>5.1f}% {s['pnl']:>+7.2f} {s['avg_pnl']:>+7.2f} "
              f"{vel:>6} {ep:>5}  {_oc(s['outcomes'])}{mark}")

    # ------------------------------------------------------------------
    # 2. Melhores sessões — padrões comuns
    # ------------------------------------------------------------------
    top_n    = args.top
    best     = sessions[:top_n]
    worst    = sessions[-top_n:]
    mid_ok   = [s for s in sessions if s["wr"] >= 75 and s["pnl"] > 0]
    mid_bad  = [s for s in sessions if s["wr"] < 60]

    def avg_field(sess_list, field):
        vals = [s[field] for s in sess_list if s[field] is not None]
        return round(sum(vals)/len(vals), 3) if vals else None

    print(f"\n\n[2] TOP {top_n} SESSOES — PADRÕES COMUNS")
    print(f"\n  {'Métrica':<25} {'Top '+str(top_n):>10} {'Bottom '+str(top_n):>12} {'Diferença':>12}")
    print(f"  {'-'*25} {'-'*10} {'-'*12} {'-'*12}")

    metrics = [
        ("avg_vel",    "Velocidade EL média"),
        ("avg_ep",     "EP médio"),
        ("avg_spread", "Leader spread médio"),
    ]
    for field, label in metrics:
        bv = avg_field(best,  field)
        wv = avg_field(worst, field)
        diff = round(bv - wv, 4) if bv is not None and wv is not None else None
        diff_str = f"{diff:+.4f}" if diff is not None else "N/A"
        print(f"  {label:<25} {safe(bv,'.4f'):>10} {safe(wv,'.4f'):>12} {diff_str:>12}")

    print(f"\n  Distribuição de hora (UTC-3) nas melhores vs piores sessões:")
    best_hours  = [t["hour_local"] for s in best  for t in s["trades"] if t["hour_local"] is not None]
    worst_hours = [t["hour_local"] for s in worst for t in s["trades"] if t["hour_local"] is not None]

    from collections import Counter
    bh = Counter(best_hours)
    wh = Counter(worst_hours)
    all_hours = sorted(set(bh) | set(wh))
    print(f"  {'Hora':<6} {'Top':>6} {'Bottom':>8}")
    for h in all_hours:
        print(f"  {h:02d}h    {bh.get(h,0):>4}      {wh.get(h,0):>4}")

    # ------------------------------------------------------------------
    # 3. Análise de outcomes
    # ------------------------------------------------------------------
    print(f"\n\n[3] ANÁLISE DE OUTCOMES")
    print(f"\n  Outcome         N    WR%    PnL total   PnL médio   vel_med   ep_med")
    print(f"  {'-'*14} {'-'*4} {'-'*6} {'-'*11} {'-'*10} {'-'*8} {'-'*7}")

    outcome_groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        outcome_groups[t["outcome"]].append(t)

    for oc, group in sorted(outcome_groups.items(), key=lambda x: -len(x[1])):
        n     = len(group)
        wins  = sum(t["win"] for t in group)
        pnl   = sum(t["pnl"] for t in group)
        avgp  = pnl / n
        vels  = [t["el_vel"] for t in group if t["el_vel"] is not None]
        eps   = [t["ep"] for t in group if t["ep"] is not None]
        vel_m = round(sum(vels)/len(vels), 4) if vels else None
        ep_m  = round(sum(eps)/len(eps), 4) if eps else None
        wr    = wins/n*100
        print(f"  {oc:<14} {n:>4} {wr:>5.1f}% {pnl:>+11.2f} {avgp:>+10.2f}   "
              f"{safe(vel_m,'.4f'):>8} {safe(ep_m,'.4f'):>7}")

    # ------------------------------------------------------------------
    # 4. Padrão dos trades perdedores
    # ------------------------------------------------------------------
    losers = [t for t in trades if t["win"] == 0]
    winners = [t for t in trades if t["win"] == 1]

    print(f"\n\n[4] PERFIL DOS TRADES PERDEDORES vs VENCEDORES")
    print(f"\n  {'Campo':<22} {'Perdas (n={})'.format(len(losers)):>16} {'Vitórias (n={})'.format(len(winners)):>18}")
    print(f"  {'-'*22} {'-'*16} {'-'*18}")

    def field_stats(group, field):
        vals = [t[field] for t in group if t[field] is not None]
        if not vals:
            return "N/A"
        mn = min(vals); mx = max(vals); av = sum(vals)/len(vals)
        return f"avg={av:.3f} [{mn:.2f}-{mx:.2f}]"

    for field, label in [("el_vel","el_vel"), ("ep","ep"),
                         ("leader_spread","leader_spread"), ("secs","secs"),
                         ("el_bid_180","el_bid_180"), ("n_s180","n_s180")]:
        ls = field_stats(losers, field)
        ws = field_stats(winners, field)
        print(f"  {label:<22} {ls:>16}   {ws:>18}")

    # Hora dos perdedores
    loss_hours = Counter(t["hour_local"] for t in losers if t["hour_local"] is not None)
    win_hours  = Counter(t["hour_local"] for t in winners if t["hour_local"] is not None)
    print(f"\n  Hora com mais perdas: " +
          ", ".join(f"{h:02d}h({c})" for h, c in loss_hours.most_common(5)))
    print(f"  Hora com mais vitórias: " +
          ", ".join(f"{h:02d}h({c})" for h, c in win_hours.most_common(5)))

    # ------------------------------------------------------------------
    # 5. Segmentação por el_vel e ep (zonas de risco)
    # ------------------------------------------------------------------
    print(f"\n\n[5] ZONAS DE RISCO (el_vel × ep)")
    compare_groups(trades, "el_vel", "el_vel",
                   [0.08, 0.10, 0.12, 0.13, 0.15, 0.18, 0.22])
    compare_groups(trades, "ep", "ep",
                   [0.82, 0.84, 0.86, 0.88])
    compare_groups(trades, "leader_spread", "leader_spread",
                   [0.50, 0.60, 0.65, 0.70, 0.75])

    # ------------------------------------------------------------------
    # 6. BTC info (apenas sessões 26/05 com dados)
    # ------------------------------------------------------------------
    btc_trades = [t for t in trades if t["btc_dist_usd"] is not None]
    if btc_trades:
        print(f"\n\n[6] CONTEXTO BTC (apenas sessões com dados: n={len(btc_trades)})")
        btc_losers  = [t for t in btc_trades if t["win"] == 0]
        btc_winners = [t for t in btc_trades if t["win"] == 1]
        print(f"\n  {'Campo':<22} {'Perdas ({})'.format(len(btc_losers)):>18} {'Vitórias ({})'.format(len(btc_winners)):>20}")
        print(f"  {'-'*22} {'-'*18} {'-'*20}")
        for field, label in [("btc_dist_usd","dist_usd (ptb)"),
                              ("btc_range_3m","range_3m_usd"),
                              ("btc_delta_1m","delta_1m_usd"),
                              ("btc_delta_3m","delta_3m_usd")]:
            ls = field_stats(btc_losers, field)
            ws = field_stats(btc_winners, field)
            print(f"  {label:<22} {ls:>18}   {ws:>20}")

        adv_loss = sum(1 for t in btc_losers  if t.get("btc_adverse_1m") is True)
        adv_win  = sum(1 for t in btc_winners if t.get("btc_adverse_1m") is True)
        print(f"\n  BTC adverse_1m nas perdas:   {adv_loss}/{len(btc_losers)} "
              f"({adv_loss/len(btc_losers)*100:.0f}%)" if btc_losers else "")
        print(f"  BTC adverse_1m nas vitórias: {adv_win}/{len(btc_winners)} "
              f"({adv_win/len(btc_winners)*100:.0f}%)" if btc_winners else "")

    # ------------------------------------------------------------------
    # 7. Resumo acionável
    # ------------------------------------------------------------------
    print(f"\n\n[7] RESUMO ACIONÁVEL")
    print(f"""
  SINAIS DE BOA SESSÃO (replicar no real):
    - el_vel >= 0.13           → WR ~84% vs ~70% abaixo
    - ep em [0.84, 0.86)       → WR 83%, maior volume de trades
    - leader_spread [0.65,0.70)→ WR 81%, melhor separação de sinal
    - Horários: 00h-09h, 11h-16h, 19h-21h
    - Outcomes predominantes: PROFIT_PROTECT > WIN > STOP_LOSS

  SINAIS DE MÁ SESSÃO (bloquear ou reduzir):
    - ep >= 0.86 + el_vel < 0.13 → WR 62%, avg -$0.08  [GATE JÁ IMPLEMENTADO]
    - leader_spread >= 0.70      → WR 62% (correlacionado com ep alto)
    - Hora 10h local             → WR 20% em 5 trades (-$8.34)
    - Hora 23h local             → WR 25% em 4 trades
    - REVERSAL / WIN_HEDGE       → perdas grandes (-$2.64 a -$5.16), ep >= 0.86

  PADRÃO DOS PIORES TRADES:
    - REVERSAL: sempre ep=0.86, el_vel baixa, mercado reverte completamente
    - WIN_HEDGE: hedge ativado em reversão parcial com ep alto
    - STOP_LOSS: distribuídos, mas maiores quando ep alto + vel baixa

  PRÓXIMOS PASSOS SUGERIDOS:
    1. Coletar 50+ trades com gate ep/vel ativo (ciclo 6 em diante)
    2. Avaliar gate leader_spread >= 0.70 (bloqueia ep=0.86 indiretamente)
    3. Avaliar gate horário: bloquear 10h-11h e 22h-00h (requer mais dados)
    4. Quando WR >= 80% sustentado em 100+ trades → propor replicação no real
""")


if __name__ == "__main__":
    main()
