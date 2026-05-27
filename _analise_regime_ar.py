"""
Análise de regime de mercado para o AR paper — versão estendida.

Cruza cada trade com:
  - Sessão de mercado  (Asia 00-08h / Europa 09-15h / US 16-23h local UTC-3)
  - Timeframe          (5m vs 15m — extraído do slug)
  - Momentum BTC 60s   (spot_delta_60s_bps) — proxy de tendência curta
  - Nível BTC          (reference_price em faixas) — proxy de regime macro
  - Volatilidade       (spot_range_60s_usd)

Uso:
  python _analise_regime_ar.py
  python _analise_regime_ar.py --logs "logs/current_almost_resolved_paper_*.jsonl"
  python _analise_regime_ar.py --min-trades 3
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TZ_LOCAL = timezone(timedelta(hours=-3))
SEP  = "=" * 76
SEP2 = "-" * 76


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_avg(lst):
    vals = [v for v in lst if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None

def session_label(hour: int | None) -> str:
    if hour is None:
        return "N/A"
    if 0 <= hour < 9:
        return "Asia    (00-08h)"
    if 9 <= hour < 16:
        return "Europa  (09-15h)"
    return "US      (16-23h)"

def momentum_label(delta: float | None) -> str:
    if delta is None:
        return "N/A"
    if delta <= -8:
        return "queda_forte  (<=−8)"
    if delta <= -3:
        return "queda_leve   (−8,−3)"
    if delta < 3:
        return "flat         (−3,+3)"
    if delta < 8:
        return "alta_leve    (+3,+8)"
    return "alta_forte   (>=+8)"

def tf_label(slug: str) -> str:
    if "15m" in slug:
        return "15m"
    if "5m" in slug:
        return "5m"
    return "N/A"

def btc_level_label(price: float | None) -> str:
    if price is None:
        return "N/A"
    if price < 60_000:
        return "<60k"
    if price < 70_000:
        return "60-70k"
    if price < 80_000:
        return "70-80k"
    if price < 90_000:
        return "80-90k"
    if price < 100_000:
        return "90-100k"
    return ">=100k"

def btc_500_label(price: float | None) -> str:
    """Bucket de $500 para granularidade de 15m/1h no nível BTC."""
    if price is None:
        return "N/A"
    b = int(price // 500) * 500
    lo_k, lo_d = b // 1000, (b % 1000) // 100
    hi_k, hi_d = (b + 500) // 1000, ((b + 500) % 1000) // 100
    return f"{lo_k}.{lo_d}k-{hi_k}.{hi_d}k"

def trend_15m_label(delta: float | None) -> str:
    """Tendência BTC nos últimos 15min (bps). Proxy de contexto 15min chart."""
    if delta is None:
        return "N/A"
    if delta <= -20:
        return "cai_forte  (<=−20)"
    if delta <= -5:
        return "cai_leve   (−20,−5)"
    if delta < 5:
        return "flat       (−5,+5)"
    if delta < 20:
        return "sobe_leve  (+5,+20)"
    return "sobe_forte (>=+20)"

def trend_1h_label(delta: float | None) -> str:
    """Tendência BTC na última 1h (bps). Proxy de contexto 1h chart."""
    if delta is None:
        return "N/A"
    if delta <= -40:
        return "cai_forte  (<=−40)"
    if delta <= -10:
        return "cai_leve   (−40,−10)"
    if delta < 10:
        return "flat       (−10,+10)"
    if delta < 40:
        return "sobe_leve  (+10,+40)"
    return "sobe_forte (>=+40)"

def range_pos_label(pct_from_top: float | None) -> str:
    """Posição no range 2h: 0=no topo (resistência), 1=no fundo (suporte)."""
    if pct_from_top is None:
        return "N/A"
    if pct_from_top < 0.15:
        return "topo   (<15%)"    # perto de resistência
    if pct_from_top < 0.35:
        return "alto   (15-35%)"
    if pct_from_top < 0.65:
        return "meio   (35-65%)"
    if pct_from_top < 0.85:
        return "baixo  (65-85%)"
    return "fundo  (>85%)"       # perto de suporte

def vol_label(rng: float | None) -> str:
    if rng is None:
        return "N/A"
    if rng < 15:
        return "baixa   (<15)"
    if rng < 35:
        return "media   (15-35)"
    if rng < 70:
        return "alta    (35-70)"
    return "extrema (>=70)"

def group_stats(group: list[dict]):
    n     = len(group)
    wins  = sum(t["win"] for t in group)
    ticks = sum(t["pnl_ticks"] for t in group)
    quote = sum(t["pnl_quote"] for t in group)
    wr    = wins / n * 100 if n else 0.0
    return n, wins, n - wins, wr, ticks, quote

def print_group(label: str, group: list[dict], min_n: int, extra: str = "", label_w: int = 28):
    if len(group) < min_n:
        return
    n, w, l, wr, ticks, quote = group_stats(group)
    avg_t = ticks / n
    reasons = defaultdict(int)
    for t in group:
        reasons[t["exit_reason"]] += 1
    r_str = " ".join(f"{k[:3]}={v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
    flag = "  *** RUIM" if wr < 80 and n >= min_n else ""
    avg_ep = safe_avg([t["entry_price"] for t in group])
    print(f"  {label:<{label_w}} N={n:>4}  WR={wr:>5.1f}%  ticks={ticks:>+7.1f}  avg={avg_t:>+5.2f}  "
          f"ep={avg_ep or 0:>.3f}  {r_str}{flag}{extra}")


# ---------------------------------------------------------------------------
# Carregamento
# ---------------------------------------------------------------------------

def load_trades(pattern: str) -> list[dict]:
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"Nenhum log encontrado: {pattern}")

    all_snaps_by_file: dict[str, list[dict]] = {}
    all_exits: list[dict] = []
    global_timeline: list[tuple[float, float]] = []  # (ts, btc_price)

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
                    # Acumula na timeline global de preço BTC
                    _ref = ev.get("reference") or {}
                    _sc  = ev.get("current_scalp_context") or {}
                    _rp  = _ref.get("reference_price") or _sc.get("reference_price")
                    _ts  = ev.get("ts")
                    if _rp and _ts:
                        global_timeline.append((float(_ts), float(_rp)))
                elif t == "exit":
                    ev["_file"] = f
                    all_exits.append(ev)
        all_snaps_by_file[f] = snaps

    global_timeline.sort()
    gtl_ts = [x[0] for x in global_timeline]
    gtl_px = [x[1] for x in global_timeline]

    def _px_at(ts_target: float):
        """Preço BTC mais recente <= ts_target na timeline global."""
        if not gtl_ts or ts_target < gtl_ts[0]:
            return None
        idx = bisect.bisect_right(gtl_ts, ts_target) - 1
        return gtl_px[idx] if idx >= 0 else None

    def _delta_bps(ts_trade: float, lookback_s: float):
        """Delta de preço BTC (bps) entre ts_trade e ts_trade - lookback_s."""
        px_now = _px_at(ts_trade)
        px_old = _px_at(ts_trade - lookback_s)
        if px_now is None or px_old is None or px_old == 0:
            return None
        return (px_now - px_old) / px_old * 10_000

    def _range_pos(ts_trade: float, px_trade: float, window_s: float = 7200):
        """Posição do preço no range dos últimos window_s segundos.
        Retorna 0.0 (no topo) até 1.0 (no fundo). None se dados insuficientes."""
        lo_ts  = ts_trade - window_s
        idx_lo = bisect.bisect_left(gtl_ts, lo_ts)
        idx_hi = bisect.bisect_right(gtl_ts, ts_trade)
        if idx_hi - idx_lo < 5:
            return None
        window_px = gtl_px[idx_lo:idx_hi]
        hi, lo = max(window_px), min(window_px)
        if hi <= lo:
            return None
        pos = (hi - px_trade) / (hi - lo)
        return max(0.0, min(1.0, pos))  # clamp

    trades = []
    for ev in all_exits:
        f     = ev["_file"]
        trade = ev.get("trade") or {}
        created = trade.get("created_at") or ev.get("ts") or 0

        snaps  = all_snaps_by_file.get(f, [])
        before = [s for s in snaps if s["ts"] <= created + 0.5]
        snap   = before[-1] if before else {}

        sig = snap.get("signal") or {}
        sc  = snap.get("current_scalp_context") or {}
        ref = snap.get("reference") or {}

        pnl_ticks = trade.get("pnl_ticks") or 0.0
        pnl_quote = trade.get("pnl_quote") or 0.0
        win = 1 if pnl_ticks > 0 else 0

        ts = created
        dt = datetime.fromtimestamp(ts, tz=TZ_LOCAL) if ts else None
        hour = dt.hour if dt else None

        slug = snap.get("current_slug") or sig.get("current_slug") or ""

        ref_price = ref.get("reference_price") or sc.get("reference_price")

        d15m = _delta_bps(ts, 900)     # tendência 15min
        d1h  = _delta_bps(ts, 3600)    # tendência 1h
        rpos = _range_pos(ts, ref_price) if ref_price else None

        trades.append({
            "slug":           slug,
            "timeframe":      tf_label(slug),
            "side":           trade.get("side", ""),
            "entry_price":    trade.get("entry_price"),
            "exit_price":     trade.get("exit_price"),
            "exit_reason":    trade.get("exit_reason", ""),
            "setup_variant":  trade.get("setup_variant") or sig.get("setup_variant", ""),
            "source":         trade.get("source", ""),
            "pnl_ticks":      pnl_ticks,
            "pnl_quote":      pnl_quote,
            "win":            win,
            # Contexto de mercado
            "hour":           hour,
            "session":        session_label(hour),
            "secs_to_end":    sig.get("secs_to_end") or sc.get("secs_to_end"),
            "distance_bps":   sig.get("distance_to_price_to_beat_bps"),
            "source_div":     ref.get("source_divergence_bps") or sc.get("source_divergence_bps"),
            "spot_range_60s": sc.get("spot_range_60s_usd") or sig.get("spot_range_60s_usd"),
            "spot_delta_60s": sc.get("spot_delta_60s_bps"),
            "spot_delta_15s": sc.get("spot_delta_15s_bps"),
            "btc_price":      ref_price,
            "delta_15m_bps":  d15m,
            "delta_1h_bps":   d1h,
            "range_pos":      rpos,
            # Labels
            "session_lbl":    session_label(hour),
            "momentum_lbl":   momentum_label(sc.get("spot_delta_60s_bps")),
            "btc_level_lbl":  btc_level_label(ref_price),
            "btc_500_lbl":    btc_500_label(ref_price),
            "vol_lbl":        vol_label(sc.get("spot_range_60s_usd") or sig.get("spot_range_60s_usd")),
            "trend_15m_lbl":  trend_15m_label(d15m),
            "trend_1h_lbl":   trend_1h_label(d1h),
            "range_pos_lbl":  range_pos_label(rpos),
            "ts":             ts,
        })

    return trades


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", default="logs/current_almost_resolved_paper_*.jsonl")
    ap.add_argument("--min-trades", type=int, default=3)
    args = ap.parse_args()

    trades = load_trades(args.logs)
    n      = len(trades)
    wins   = sum(t["win"] for t in trades)
    ticks  = sum(t["pnl_ticks"] for t in trades)
    quote  = sum(t["pnl_quote"] for t in trades)
    mt     = args.min_trades
    losers = [t for t in trades if not t["win"]]

    print(SEP)
    print("  ANÁLISE DE REGIME AR — current_almost_resolved paper")
    print(f"  {n} trades  |  W={wins} L={n-wins}  WR={wins/n*100:.1f}%  "
          f"Ticks={ticks:+.1f}  PnL=${quote:+.2f}")
    print(SEP)

    # ------------------------------------------------------------------
    # 1. Por sessão de mercado
    # ------------------------------------------------------------------
    print("\n[1] POR SESSÃO DE MERCADO (local UTC-3)")
    by_sess = defaultdict(list)
    for t in trades:
        by_sess[t["session_lbl"]].append(t)
    for lbl in ["Asia    (00-08h)", "Europa  (09-15h)", "US      (16-23h)"]:
        print_group(lbl, by_sess.get(lbl, []), 1)

    # ------------------------------------------------------------------
    # 2. Por hora dentro da sessão
    # ------------------------------------------------------------------
    print("\n[2] POR HORA LOCAL")
    by_hour = defaultdict(list)
    for t in trades:
        if t["hour"] is not None:
            by_hour[t["hour"]].append(t)
    for h in sorted(by_hour):
        print_group(f"{h:02d}h", by_hour[h], mt)

    # ------------------------------------------------------------------
    # 3. Por timeframe (5m vs 15m)
    # ------------------------------------------------------------------
    print("\n[3] POR TIMEFRAME (extraído do slug)")
    by_tf = defaultdict(list)
    for t in trades:
        by_tf[t["timeframe"]].append(t)
    for lbl in sorted(by_tf):
        print_group(lbl, by_tf[lbl], 1)

    # ------------------------------------------------------------------
    # 4. Por nível absoluto BTC (faixas 10k)
    # ------------------------------------------------------------------
    print("\n[4] POR NÍVEL BTC (reference_price, faixas 10k)")
    by_btc = defaultdict(list)
    for t in trades:
        by_btc[t["btc_level_lbl"]].append(t)
    for lbl in ["<60k","60-70k","70-80k","80-90k","90-100k",">=100k"]:
        print_group(lbl, by_btc.get(lbl, []), mt)

    # ------------------------------------------------------------------
    # 4b. Por nível BTC em faixas de $500 (proxy de contexto 15m/1h)
    # ------------------------------------------------------------------
    print("\n[4b] POR NÍVEL BTC $500 (proxy contexto 15min/1h)")
    print("     Lógica: variação de $500 no gráfico 15m/1h impacta fortemente")
    print("     o candle de 5min que o setup opera.")
    by_btc500 = defaultdict(list)
    for t in trades:
        by_btc500[t["btc_500_lbl"]].append(t)
    # Ordena por nível (ex: "74.5k-75.0k" < "75.0k-75.5k" lexicograficamente)
    for lbl in sorted(k for k in by_btc500 if k != "N/A"):
        print_group(lbl, by_btc500[lbl], mt)
    if "N/A" in by_btc500 and len(by_btc500["N/A"]) >= mt:
        print_group("N/A", by_btc500["N/A"], mt)

    # ------------------------------------------------------------------
    # 4c. Tendência BTC 15min — proxy de contexto 15min chart
    # ------------------------------------------------------------------
    print("\n[4c] TENDÊNCIA BTC 15min (delta de preço 15min antes da entrada)")
    print("     > 0 = BTC subindo no 15min;  < 0 = caindo;  flat = lateralizando")
    by_t15 = defaultdict(list)
    for t in trades:
        by_t15[t["trend_15m_lbl"]].append(t)
    for lbl in ["cai_forte  (<=−20)", "cai_leve   (−20,−5)", "flat       (−5,+5)",
                "sobe_leve  (+5,+20)", "sobe_forte (>=+20)", "N/A"]:
        print_group(lbl, by_t15.get(lbl, []), mt)

    # ------------------------------------------------------------------
    # 4d. Tendência BTC 1h — proxy de contexto 1h chart
    # ------------------------------------------------------------------
    print("\n[4d] TENDÊNCIA BTC 1h (delta de preço 1h antes da entrada)")
    print("     Tendência primária: define se o setup está a favor ou contra")
    by_t1h = defaultdict(list)
    for t in trades:
        by_t1h[t["trend_1h_lbl"]].append(t)
    for lbl in ["cai_forte  (<=−40)", "cai_leve   (−40,−10)", "flat       (−10,+10)",
                "sobe_leve  (+10,+40)", "sobe_forte (>=+40)", "N/A"]:
        print_group(lbl, by_t1h.get(lbl, []), mt)

    # ------------------------------------------------------------------
    # 4e. Posição no range 2h — onde o BTC está estruturalmente
    # ------------------------------------------------------------------
    print("\n[4e] POSIÇÃO NO RANGE 2h (0%=topo/resistência, 100%=fundo/suporte)")
    print("     Topo do range = perto de resistência; Fundo = perto de suporte")
    by_rpos = defaultdict(list)
    for t in trades:
        by_rpos[t["range_pos_lbl"]].append(t)
    for lbl in ["topo   (<15%)", "alto   (15-35%)", "meio   (35-65%)",
                "baixo  (65-85%)", "fundo  (>85%)", "N/A"]:
        print_group(lbl, by_rpos.get(lbl, []), mt)

    # ------------------------------------------------------------------
    # 5. Por momentum BTC 60s
    # ------------------------------------------------------------------
    print("\n[5] POR MOMENTUM BTC 60s (spot_delta_60s_bps)")
    by_mom = defaultdict(list)
    for t in trades:
        by_mom[t["momentum_lbl"]].append(t)
    for lbl in ["queda_forte  (<=−8)", "queda_leve   (−8,−3)", "flat         (−3,+3)",
                "alta_leve    (+3,+8)", "alta_forte   (>=+8)"]:
        print_group(lbl, by_mom.get(lbl, []), mt)

    # ------------------------------------------------------------------
    # 6. Por volatilidade BTC 60s
    # ------------------------------------------------------------------
    print("\n[6] POR VOLATILIDADE BTC 60s (spot_range_60s_usd)")
    by_vol = defaultdict(list)
    for t in trades:
        by_vol[t["vol_lbl"]].append(t)
    for lbl in ["baixa   (<15)", "media   (15-35)", "alta    (35-70)", "extrema (>=70)"]:
        print_group(lbl, by_vol.get(lbl, []), mt)

    # ------------------------------------------------------------------
    # 7. Cross: sessão × momentum
    # ------------------------------------------------------------------
    print("\n[7] CROSS: SESSÃO × MOMENTUM BTC")
    by_cross = defaultdict(list)
    for t in trades:
        key = f"{t['session_lbl'][:6].strip()} | {t['momentum_lbl']}"
        by_cross[key].append(t)
    for key in sorted(by_cross, key=lambda k: -len(by_cross[k])):
        print_group(key, by_cross[key], mt)

    # ------------------------------------------------------------------
    # 8. Cross: timeframe × sessão
    # ------------------------------------------------------------------
    print("\n[8] CROSS: TIMEFRAME × SESSÃO")
    by_tf_sess = defaultdict(list)
    for t in trades:
        key = f"{t['timeframe']} | {t['session_lbl'][:6].strip()}"
        by_tf_sess[key].append(t)
    for key in sorted(by_tf_sess):
        print_group(key, by_tf_sess[key], mt)

    # ------------------------------------------------------------------
    # 9. Cross: posição no range × tendência BTC
    #    Hipótese central: UP binary perto de resistência + BTC caindo = risco
    # ------------------------------------------------------------------
    print("\n[9] CROSS: POSIÇÃO NO RANGE 2h × TENDÊNCIA 15min")
    print("    Hipótese: UP binary no topo do range (resistência) + BTC caindo = maior risco")
    by_pos_t15 = defaultdict(list)
    for t in trades:
        if t["range_pos_lbl"] != "N/A" and t["trend_15m_lbl"] != "N/A":
            key = f"{t['range_pos_lbl']} | {t['trend_15m_lbl']}"
            by_pos_t15[key].append(t)
    pos_order = ["topo   (<15%)", "alto   (15-35%)", "meio   (35-65%)",
                 "baixo  (65-85%)", "fundo  (>85%)"]
    t15_order = ["cai_forte  (<=−20)", "cai_leve   (−20,−5)", "flat       (−5,+5)",
                 "sobe_leve  (+5,+20)", "sobe_forte (>=+20)"]
    for pos in pos_order:
        for t15 in t15_order:
            key = f"{pos} | {t15}"
            if key in by_pos_t15:
                print_group(key, by_pos_t15[key], mt, label_w=46)

    bad_pos_t15 = [(k, g) for k, g in by_pos_t15.items()
                   if len(g) >= mt and group_stats(g)[3] < 90.0]
    if bad_pos_t15:
        print(f"\n    Combos com WR < 90% (N >= {mt}):")
        for k, g in sorted(bad_pos_t15, key=lambda x: group_stats(x[1])[3]):
            n2, w, l, wr, tks, _ = group_stats(g)
            print(f"      {k}  ->  N={n2}  WR={wr:.1f}%  ticks={tks:+.1f}")
    else:
        print(f"\n    Nenhum combo com WR < 90% (N >= {mt}).")

    # Cross: posição × tendência 1h (tendência primária)
    print("\n    --- Cross: POSIÇÃO NO RANGE 2h × TENDÊNCIA 1h (tendência primária) ---")
    by_pos_t1h = defaultdict(list)
    for t in trades:
        if t["range_pos_lbl"] != "N/A" and t["trend_1h_lbl"] != "N/A":
            key = f"{t['range_pos_lbl']} | {t['trend_1h_lbl']}"
            by_pos_t1h[key].append(t)
    t1h_order = ["cai_forte  (<=−40)", "cai_leve   (−40,−10)", "flat       (−10,+10)",
                 "sobe_leve  (+10,+40)", "sobe_forte (>=+40)"]
    for pos in pos_order:
        for t1h in t1h_order:
            key = f"{pos} | {t1h}"
            if key in by_pos_t1h:
                print_group(key, by_pos_t1h[key], mt, label_w=46)

    bad_pos_t1h = [(k, g) for k, g in by_pos_t1h.items()
                   if len(g) >= mt and group_stats(g)[3] < 90.0]
    if bad_pos_t1h:
        print(f"\n    Combos com WR < 90% (N >= {mt}) — tendência 1h:")
        for k, g in sorted(bad_pos_t1h, key=lambda x: group_stats(x[1])[3]):
            n2, w, l, wr, tks, _ = group_stats(g)
            print(f"      {k}  ->  N={n2}  WR={wr:.1f}%  ticks={tks:+.1f}")
    else:
        print(f"\n    Nenhum combo WR < 90% com tendência 1h (N >= {mt}).")

    # ------------------------------------------------------------------
    # 10. Cross: nível BTC $500 × momentum (contexto 15m/1h × tendência 60s)
    # ------------------------------------------------------------------
    print("\n[10] CROSS: NÍVEL BTC $500 × MOMENTUM 60s")
    print("    Identifica zonas de preço × direção BTC onde o setup sofre.")
    by_btc_mom = defaultdict(list)
    for t in trades:
        if t["btc_500_lbl"] != "N/A" and t["momentum_lbl"] != "N/A":
            key = f"{t['btc_500_lbl']} | {t['momentum_lbl']}"
            by_btc_mom[key].append(t)
    # Ordena: primeiro por nível BTC, depois por momentum
    for key in sorted(by_btc_mom):
        print_group(key, by_btc_mom[key], mt, label_w=42)

    # Resumo: quais combos têm WR < 90%
    bad_combos = [(k, g) for k, g in by_btc_mom.items()
                  if len(g) >= mt and group_stats(g)[3] < 90.0]
    if bad_combos:
        print(f"\n    Combos com WR < 90% (N >= {mt}):")
        for k, g in sorted(bad_combos, key=lambda x: group_stats(x[1])[3]):
            n2, w, l, wr, tks, _ = group_stats(g)
            print(f"      {k}  ->  N={n2}  WR={wr:.1f}%  ticks={tks:+.1f}")
    else:
        print(f"\n    Nenhum combo com WR < 90% (N >= {mt}).")

    # ------------------------------------------------------------------
    # 11. Perfil das perdas — piores condições
    # ------------------------------------------------------------------
    print(f"\n[11] PERFIL DAS PERDAS ({len(losers)} trades perdedores)")
    if not losers:
        print("  Nenhuma perda no dataset.")
    else:
        print(f"\n  {'Campo':<22} {'Perdas (avg)':>18}  {'Vitórias (avg)':>18}")
        print(f"  {'-'*22} {'-'*18}  {'-'*18}")
        for field, label in [
            ("entry_price",    "entry_price"),
            ("secs_to_end",    "secs_to_end"),
            ("distance_bps",   "distance_bps"),
            ("source_div",     "source_div"),
            ("spot_range_60s", "spot_range_60s"),
            ("spot_delta_60s", "spot_delta_60s"),
            ("btc_price",      "btc_price"),
        ]:
            def fv(grp, f):
                vals = [t[f] for t in grp if t.get(f) is not None]
                return f"avg={sum(vals)/len(vals):.1f} [{min(vals):.0f}-{max(vals):.0f}]" if vals else "N/A"
            winners_list = [t for t in trades if t["win"]]
            print(f"  {label:<22} {fv(losers, field):>18}  {fv(winners_list, field):>18}")

        print(f"\n  Detalhes individuais (ordenado por pnl_ticks):")
        print(f"  {'Hora':>5}  {'TF':>4}  {'Sessão':>10}  {'ep':>5}  {'secs':>5}  "
              f"{'delta60':>8}  {'rng60':>6}  {'div':>5}  {'exit':<14}  {'ticks':>6}")
        print(f"  {'-'*5}  {'-'*4}  {'-'*10}  {'-'*5}  {'-'*5}  {'-'*8}  {'-'*6}  {'-'*5}  {'-'*14}  {'-'*6}")
        for t in sorted(losers, key=lambda x: x["pnl_ticks"]):
            h   = f"{t['hour']:02d}h" if t["hour"] is not None else "N/A"
            ep  = f"{t['entry_price']:.3f}" if t["entry_price"] is not None else "N/A"
            sec = f"{t['secs_to_end']:.0f}" if t["secs_to_end"] is not None else "N/A"
            d60 = f"{t['spot_delta_60s']:+.1f}" if t["spot_delta_60s"] is not None else "N/A"
            r60 = f"{t['spot_range_60s']:.1f}" if t["spot_range_60s"] is not None else "N/A"
            div = f"{t['source_div']:.1f}" if t["source_div"] is not None else "N/A"
            sess = t["session_lbl"][:6].strip()
            print(f"  {h:>5}  {t['timeframe']:>4}  {sess:>10}  {ep:>5}  {sec:>5}  "
                  f"{d60:>8}  {r60:>6}  {div:>5}  {t['exit_reason']:<14}  {t['pnl_ticks']:>+6.1f}")

    # ------------------------------------------------------------------
    # 12. Resumo acionável
    # ------------------------------------------------------------------
    print(f"\n[12] RESUMO ACIONÁVEL")

    # Sessão com pior WR
    worst_sess = sorted(
        [(lbl, g) for lbl, g in by_sess.items() if len(g) >= mt],
        key=lambda x: group_stats(x[1])[3]
    )
    # Momentum com pior WR
    worst_mom = sorted(
        [(lbl, g) for lbl, g in by_mom.items() if len(g) >= mt],
        key=lambda x: group_stats(x[1])[3]
    )
    # Vol com pior WR
    worst_vol = sorted(
        [(lbl, g) for lbl, g in by_vol.items() if len(g) >= mt],
        key=lambda x: group_stats(x[1])[3]
    )
    # BTC $500 com pior WR
    worst_btc500 = sorted(
        [(lbl, g) for lbl, g in by_btc500.items() if len(g) >= mt and lbl != "N/A"],
        key=lambda x: group_stats(x[1])[3]
    )
    # Tendência 15min com pior WR
    worst_t15 = sorted(
        [(lbl, g) for lbl, g in by_t15.items() if len(g) >= mt and lbl != "N/A"],
        key=lambda x: group_stats(x[1])[3]
    )
    # Tendência 1h com pior WR
    worst_t1h = sorted(
        [(lbl, g) for lbl, g in by_t1h.items() if len(g) >= mt and lbl != "N/A"],
        key=lambda x: group_stats(x[1])[3]
    )
    # Posição no range com pior WR
    worst_rpos = sorted(
        [(lbl, g) for lbl, g in by_rpos.items() if len(g) >= mt and lbl != "N/A"],
        key=lambda x: group_stats(x[1])[3]
    )

    print()
    print("  PIORES CONDIÇÕES IDENTIFICADAS:")
    if worst_sess:
        lbl, g = worst_sess[0]
        n2, w, l, wr, tks, _ = group_stats(g)
        print(f"    Sessão mais fraca    : {lbl.strip():<25}  N={n2}  WR={wr:.1f}%  ticks={tks:+.1f}")
    if worst_mom:
        lbl, g = worst_mom[0]
        n2, w, l, wr, tks, _ = group_stats(g)
        print(f"    Momentum 60s pior    : {lbl.strip():<25}  N={n2}  WR={wr:.1f}%  ticks={tks:+.1f}")
    if worst_t15:
        lbl, g = worst_t15[0]
        n2, w, l, wr, tks, _ = group_stats(g)
        print(f"    Tendência 15min pior : {lbl.strip():<25}  N={n2}  WR={wr:.1f}%  ticks={tks:+.1f}")
    if worst_t1h:
        lbl, g = worst_t1h[0]
        n2, w, l, wr, tks, _ = group_stats(g)
        print(f"    Tendência 1h pior    : {lbl.strip():<25}  N={n2}  WR={wr:.1f}%  ticks={tks:+.1f}")
    if worst_rpos:
        lbl, g = worst_rpos[0]
        n2, w, l, wr, tks, _ = group_stats(g)
        print(f"    Posição range pior   : {lbl.strip():<25}  N={n2}  WR={wr:.1f}%  ticks={tks:+.1f}")
    if worst_btc500:
        lbl, g = worst_btc500[0]
        n2, w, l, wr, tks, _ = group_stats(g)
        print(f"    Nível BTC $500 pior  : {lbl.strip():<25}  N={n2}  WR={wr:.1f}%  ticks={tks:+.1f}")

    # Combos estruturais mais perigosos (posição × tendência 15min)
    worst_struct = sorted(
        [(k, g) for k, g in by_pos_t15.items() if len(g) >= mt],
        key=lambda x: group_stats(x[1])[3]
    )
    if worst_struct:
        k, g = worst_struct[0]
        n2, w, l, wr, tks, _ = group_stats(g)
        print(f"\n    COMBO ESTRUTURAL MAIS PERIGOSO (range × tendência 15min):")
        print(f"      {k}  N={n2}  WR={wr:.1f}%  ticks={tks:+.1f}")

    # Combo BTC $500 × momentum mais perigoso
    worst_combo = sorted(
        [(k, g) for k, g in by_btc_mom.items() if len(g) >= mt],
        key=lambda x: group_stats(x[1])[3]
    )
    if worst_combo:
        k, g = worst_combo[0]
        n2, w, l, wr, tks, _ = group_stats(g)
        print(f"\n    COMBO NÍVEL×MOMENTUM MAIS PERIGOSO:")
        print(f"      {k}  N={n2}  WR={wr:.1f}%  ticks={tks:+.1f}")

    print()
    print("  NOTA: dataset é de paper local (condições de mercado limitadas).")
    print("  Para conclusões firmes, replicar nos logs reais do outro PC.")
    print()


if __name__ == "__main__":
    main()
