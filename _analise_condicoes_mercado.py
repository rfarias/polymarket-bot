"""
Análise das condições de mercado que condicionam sucesso/falha do AR paper e EE paper.
Compara features no momento da entrada entre wins, losses e profit_protect.
"""
import json
import glob
import os
import sys
from collections import defaultdict
import statistics

sys.stdout.reconfigure(encoding="utf-8")

# ── helpers ──────────────────────────────────────────────────────────────────

def load_jsonl(path):
    lines = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except Exception:
                    pass
    return lines

def pct(n, total):
    return f"{n/total*100:.1f}%" if total else "N/A"

def fmt_stats(values, label=""):
    if not values:
        return f"{label}: N/A"
    mn = min(values)
    mx = max(values)
    avg = statistics.mean(values)
    med = statistics.median(values)
    return f"{label}: min={mn:.2f} avg={avg:.2f} med={med:.2f} max={mx:.2f} (n={len(values)})"

# ── AR PAPER ─────────────────────────────────────────────────────────────────

def analyze_ar_paper():
    print("\n" + "="*70)
    print("AR PAPER — análise de condições de entrada")
    print("="*70)

    ar_files = sorted(glob.glob("logs/current_almost_resolved_paper_202605*.jsonl"))
    print(f"Arquivos: {len(ar_files)}")

    enter_events = []
    exit_map = {}   # slug+side+ts -> exit event

    for fpath in ar_files:
        lines = load_jsonl(fpath)
        enters = [l for l in lines if l.get("type") == "enter"]
        exits  = [l for l in lines if l.get("type") in ("exit", "trade_exit", "closed")]
        # também pode ser em stats_so_far changes — pegar snapshots com trade que fechou
        # Estratégia: pegar snapshot com trade.exit_reason != null
        closed_snaps = [l for l in lines if l.get("trade", {}).get("exit_reason")]
        enter_events.extend(enters)

    print(f"\nEventos 'enter' encontrados: {len(enter_events)}")

    # Agrupar por outcome (pnl_ticks no snapshot de fechamento)
    wins, losses, flats = [], [], []

    for fpath in ar_files:
        lines = load_jsonl(fpath)
        enters = {id(e): e for e in lines if e.get("type") == "enter"}
        # Pegar snapshots com trade fechado (exit_reason preenchido)
        closed = [l for l in lines if l.get("trade", {}).get("exit_reason") is not None
                  and l.get("trade", {}).get("mode") != "open"]

        # Pegar todos os enter events com seu resultado
        # Simplificação: usar os stats_so_far e rastrear enter → próximo snapshot com trade fechado
        enter_list = [l for l in lines if l.get("type") == "enter"]
        closed_list = [l for l in lines if (l.get("trade", {}).get("pnl_ticks") is not None
                       and l.get("trade", {}).get("mode") == "idle")]

        for enter in enter_list:
            ts_enter = enter.get("ts", 0)
            signal = enter.get("signal", {})
            ref = enter.get("reference", {})
            scalp = enter.get("current_scalp_context", {})
            trade = enter.get("trade", {})

            # Encontrar snapshot de fechamento mais próximo após ts_enter
            closes_after = [l for l in closed_list if l.get("ts", 0) > ts_enter]
            if not closes_after:
                continue
            close_snap = closes_after[0]
            t = close_snap.get("trade", {})
            pnl = t.get("pnl_ticks")
            exit_reason = t.get("exit_reason", "")

            rec = {
                "secs_to_end": signal.get("secs_to_end"),
                "signed_distance_bps": signal.get("signed_distance_from_open_bps"),
                "market_range_15s": signal.get("market_range_15s"),
                "market_range_30s": signal.get("market_range_30s"),
                "market_range_60s": signal.get("market_range_60s"),
                "spot_range_60s_usd": signal.get("spot_range_60s_usd"),
                "source_divergence_bps": ref.get("source_divergence_bps") if ref else signal.get("source_divergence_bps"),
                "passive_score_side": (signal.get("passive_capture_down_score") if trade.get("side") == "DOWN"
                                       else signal.get("passive_capture_up_score")),
                "edge_vs_counter": (signal.get("down_edge_vs_counter") if trade.get("side") == "DOWN"
                                    else signal.get("up_edge_vs_counter")),
                "setup_variant": enter.get("signal", {}).get("setup_variant"),
                "source": trade.get("source"),
                "entry_price": trade.get("entry_price"),
                "reentry_after_pp": enter.get("reentry_after_pp", False),
                "pnl_ticks": pnl,
                "exit_reason": exit_reason,
            }

            if pnl is None:
                continue
            if pnl > 0:
                wins.append(rec)
            elif pnl < 0:
                losses.append(rec)
            else:
                flats.append(rec)

    total = len(wins) + len(losses) + len(flats)
    print(f"\nResultados extraídos: {total} (W={len(wins)} L={len(losses)} flat={len(flats)})")
    if not total:
        print("Nenhum resultado — verifique estrutura do log")
        return

    print(f"\nWR: {pct(len(wins), total)}")

    def field_stats(records, field):
        vals = [r[field] for r in records if r.get(field) is not None]
        return vals

    features = ["secs_to_end", "signed_distance_bps", "market_range_15s",
                "market_range_30s", "market_range_60s", "spot_range_60s_usd",
                "source_divergence_bps", "passive_score_side", "edge_vs_counter"]

    print("\n── Features: WINS vs LOSSES ──")
    for feat in features:
        w = field_stats(wins, feat)
        l = field_stats(losses, feat)
        if not w and not l:
            continue
        print(f"  {feat}:")
        print(f"    WIN  : {fmt_stats(w)}")
        print(f"    LOSS : {fmt_stats(l)}")

    print("\n── Por setup_variant ──")
    variants = set(r.get("setup_variant") for r in wins+losses+flats)
    for v in sorted(variants):
        recs = [r for r in wins+losses+flats if r.get("setup_variant") == v]
        w_v = [r for r in recs if r["pnl_ticks"] > 0]
        l_v = [r for r in recs if r["pnl_ticks"] < 0]
        pnl_total = sum(r["pnl_ticks"] for r in recs)
        print(f"  {v}: {len(recs)} trades  W={len(w_v)} L={len(l_v)} WR={pct(len(w_v),len(recs))}  PnL={pnl_total:.0f} ticks")
        secs = [r["secs_to_end"] for r in recs if r.get("secs_to_end")]
        dist = [abs(r["signed_distance_bps"]) for r in recs if r.get("signed_distance_bps")]
        if secs:
            print(f"    secs_to_end: avg={statistics.mean(secs):.0f} min={min(secs)} max={max(secs)}")
        if dist:
            print(f"    |distance_bps|: avg={statistics.mean(dist):.1f} min={min(dist):.1f} max={max(dist):.1f}")

    print("\n── Por source ──")
    sources = set(r.get("source") for r in wins+losses+flats)
    for s in sorted(sources):
        recs = [r for r in wins+losses+flats if r.get("source") == s]
        w_s = [r for r in recs if r["pnl_ticks"] > 0]
        l_s = [r for r in recs if r["pnl_ticks"] < 0]
        pnl_total = sum(r["pnl_ticks"] for r in recs)
        print(f"  {s}: {len(recs)} trades  W={len(w_s)} L={len(l_s)} WR={pct(len(w_s),len(recs))}  PnL={pnl_total:.0f} ticks")

    if losses:
        print("\n── Detalhes das perdas (AR) ──")
        for r in losses:
            print(f"  entry={r.get('entry_price')} secs={r.get('secs_to_end')} "
                  f"dist={r.get('signed_distance_bps'):.1f} bps  "
                  f"range60={r.get('market_range_60s')} spot60usd={r.get('spot_range_60s_usd')} "
                  f"variant={r.get('setup_variant')} src={r.get('source')} "
                  f"exit={r.get('exit_reason')} pnl={r.get('pnl_ticks')}")


# ── EE PAPER ─────────────────────────────────────────────────────────────────

def analyze_ee_paper():
    print("\n" + "="*70)
    print("EE PAPER — análise de condições de entrada")
    print("="*70)

    ee_dirs = sorted(glob.glob("logs/ee_paper_202605*"))
    print(f"Sessões: {len(ee_dirs)}")

    all_closed = []
    all_entries = []
    snapshot_by_slug = {}  # slug -> list of snapshots (para buscar n_s180, el_vel no momento da entrada)

    for d in ee_dirs:
        fpath = os.path.join(d, "ee_paper.jsonl")
        if not os.path.exists(fpath):
            continue
        lines = load_jsonl(fpath)
        closed = [l for l in lines if l.get("type") == "ee_paper_closed"]
        entries = [l for l in lines if l.get("type") == "ee_paper_entry"]
        snaps = [l for l in lines if l.get("type") == "snapshot"]
        all_closed.extend(closed)
        all_entries.extend(entries)
        for s in snaps:
            slug = s.get("current_slug", "")
            if slug not in snapshot_by_slug:
                snapshot_by_slug[slug] = []
            snapshot_by_slug[slug].append(s)

    print(f"Closed: {len(all_closed)}  Entries: {len(all_entries)}")

    # Agregar por outcome
    by_outcome = defaultdict(list)
    for c in all_closed:
        ee = c.get("ee", {})
        outcome = ee.get("outcome", "UNKNOWN")
        slug = c.get("slug", "")
        entry_secs = ee.get("entry_secs")
        entry_ep = ee.get("entry_ep")
        entry_side = ee.get("entry_side")
        pnl = ee.get("pnl", 0)

        # Buscar snapshot no momento da entrada para pegar n_s180, el_vel
        snaps = snapshot_by_slug.get(slug, [])
        # Pegar snapshot mais próximo do entry_secs
        snap_at_entry = None
        if snaps and entry_secs is not None:
            best = None
            best_diff = 9999
            for s in snaps:
                secs = s.get("current_secs", 9999)
                diff = abs(secs - entry_secs)
                if diff < best_diff:
                    best_diff = diff
                    best = s
            snap_at_entry = best

        rec = {
            "outcome": outcome,
            "slug": slug,
            "entry_secs": entry_secs,
            "entry_ep": entry_ep,
            "entry_side": entry_side,
            "pnl": pnl,
        }

        if snap_at_entry:
            el = snap_at_entry.get("early_leader", {})
            rec["n_s180"] = el.get("n_s180")
            rec["n_s240"] = el.get("n_s240")
            rec["el_vel"] = el.get("el_vel")
            exec_ctx = snap_at_entry.get("current_exec", {})
            rec["el_bid"] = el.get("el_bid_180")
            rec["up_bid_snap"] = exec_ctx.get("up_bid")
            rec["down_bid_snap"] = exec_ctx.get("down_bid")

        by_outcome[outcome].append(rec)

    total = len(all_closed)
    print(f"\nTotal trades: {total}")
    for outcome, recs in sorted(by_outcome.items()):
        pnl_total = sum(r["pnl"] for r in recs)
        print(f"  {outcome}: {len(recs)} ({pct(len(recs), total)})  PnL={pnl_total:.2f}")

    features = ["entry_secs", "entry_ep", "n_s180", "n_s240", "el_vel"]

    print("\n── Features por outcome ──")
    for feat in features:
        print(f"\n  {feat}:")
        for outcome in ["WIN", "PROFIT_PROTECT", "STOP_LOSS"]:
            recs = by_outcome.get(outcome, [])
            vals = [r[feat] for r in recs if r.get(feat) is not None]
            print(f"    {outcome:16s}: {fmt_stats(vals)}")

    # Análise do stop: o que tinha de diferente nas entradas que viraram stop?
    print("\n── Stops vs resto: análise detalhada ──")
    stops = by_outcome.get("STOP_LOSS", [])
    outros = by_outcome.get("WIN", []) + by_outcome.get("PROFIT_PROTECT", [])
    print(f"  Stops: {len(stops)}  Outros (W+PP): {len(outros)}")

    for feat in features:
        s_vals = [r[feat] for r in stops if r.get(feat) is not None]
        o_vals = [r[feat] for r in outros if r.get(feat) is not None]
        if not s_vals and not o_vals:
            continue
        print(f"  {feat}:")
        print(f"    STOP : {fmt_stats(s_vals)}")
        print(f"    OUTRO: {fmt_stats(o_vals)}")

    # Distribuição n_s180 nos stops
    print("\n── Distribuição n_s180 nos STOPS ──")
    n180_stops = [r.get("n_s180") for r in stops if r.get("n_s180") is not None]
    if n180_stops:
        dist = defaultdict(int)
        for v in n180_stops:
            dist[v] += 1
        for k in sorted(dist):
            mark = " ← gate atual bloqueia" if k < 3 else ""
            print(f"  n_s180={k}: {dist[k]}{mark}")
    else:
        print("  (n_s180 não disponível nos logs — snapshots não coletados por slug)")

    # Distribuição entry_secs nos stops
    print("\n── Distribuição entry_secs nos STOPS ──")
    secs_stops = [r.get("entry_secs") for r in stops if r.get("entry_secs") is not None]
    if secs_stops:
        for bucket in [(0,30),(30,60),(60,90),(90,120),(120,150),(150,181)]:
            lo, hi = bucket
            cnt = sum(1 for s in secs_stops if lo <= s < hi)
            cnt_w = sum(1 for r in by_outcome.get("WIN",[]) if r.get("entry_secs") and lo <= r["entry_secs"] < hi)
            cnt_pp = sum(1 for r in by_outcome.get("PROFIT_PROTECT",[]) if r.get("entry_secs") and lo <= r["entry_secs"] < hi)
            total_bucket = cnt + cnt_w + cnt_pp
            mark = " ← gate atual bloqueia" if hi <= 155 else ""
            if total_bucket > 0:
                wr_b = pct(cnt_w + cnt_pp, total_bucket)
                print(f"  secs [{lo:3d}-{hi:3d}): STOP={cnt} W={cnt_w} PP={cnt_pp}  WR={wr_b}{mark}")

    # Análise de horário (hora UTC dos stops)
    print("\n── Horário UTC dos STOPS ──")
    from datetime import datetime
    hora_stops = defaultdict(int)
    hora_outros = defaultdict(int)
    for r in stops:
        # slug = btc-updown-5m-TIMESTAMP
        try:
            ts = int(r["slug"].split("-")[-1])
            hora = datetime.utcfromtimestamp(ts).hour
            hora_stops[hora] += 1
        except Exception:
            pass
    for r in outros:
        try:
            ts = int(r["slug"].split("-")[-1])
            hora = datetime.utcfromtimestamp(ts).hour
            hora_outros[hora] += 1
        except Exception:
            pass

    all_hours = sorted(set(list(hora_stops.keys()) + list(hora_outros.keys())))
    for h in all_hours:
        s_cnt = hora_stops.get(h, 0)
        o_cnt = hora_outros.get(h, 0)
        total_h = s_cnt + o_cnt
        wr_h = pct(o_cnt, total_h)
        bar = "█" * s_cnt + "░" * o_cnt
        print(f"  {h:02d}h UTC: STOP={s_cnt:2d} W+PP={o_cnt:2d}  WR={wr_h}  {bar}")

    # Análise cruzada: horário UTC dos AR trades (wins)
    print("\n── Horário UTC dos AR paper wins (comparação) ──")
    print("  (dados baseados nos arquivos atuais de hoje)")


# ── CORRELAÇÃO AR x REGIME DE MERCADO ────────────────────────────────────────

def analyze_ar_regime():
    """Analisa se o sucesso do AR está correlacionado com regime de mercado."""
    print("\n" + "="*70)
    print("AR PAPER — regime de mercado nas entradas (wins vs losses)")
    print("="*70)

    ar_files = sorted(glob.glob("logs/current_almost_resolved_paper_202605*.jsonl"))

    all_records = []
    for fpath in ar_files:
        lines = load_jsonl(fpath)
        enter_list = [l for l in lines if l.get("type") == "enter"]
        closed_list = [l for l in lines if (l.get("trade", {}).get("pnl_ticks") is not None
                       and l.get("trade", {}).get("mode") == "idle")]

        for enter in enter_list:
            ts_enter = enter.get("ts", 0)
            signal = enter.get("signal", {})
            ref = enter.get("reference", {})
            trade = enter.get("trade", {})

            closes_after = [l for l in closed_list if l.get("ts", 0) > ts_enter]
            if not closes_after:
                continue
            t = closes_after[0].get("trade", {})
            pnl = t.get("pnl_ticks")
            if pnl is None:
                continue

            record = {
                "pnl": pnl,
                "outcome": "win" if pnl > 0 else ("loss" if pnl < 0 else "flat"),
                "secs_to_end": signal.get("secs_to_end"),
                "abs_distance_bps": abs(signal.get("signed_distance_from_open_bps") or 0),
                "source_div_bps": ref.get("source_divergence_bps") if ref else None,
                "market_range_15s": signal.get("market_range_15s"),
                "market_range_30s": signal.get("market_range_30s"),
                "market_range_60s": signal.get("market_range_60s"),
                "spot_range_60s_usd": signal.get("spot_range_60s_usd"),
                "up_score": signal.get("passive_capture_up_score"),
                "down_score": signal.get("passive_capture_down_score"),
                "setup_variant": signal.get("setup_variant"),
                "source": trade.get("source"),
                "entry_price": trade.get("entry_price"),
                "side": trade.get("side"),
                "reentry": enter.get("reentry_after_pp", False),
            }
            all_records.append(record)

    print(f"Total entradas analisadas: {len(all_records)}")
    wins   = [r for r in all_records if r["outcome"] == "win"]
    losses = [r for r in all_records if r["outcome"] == "loss"]
    flats  = [r for r in all_records if r["outcome"] == "flat"]
    print(f"Win={len(wins)} Loss={len(losses)} Flat={len(flats)}")

    if losses:
        print("\n── PERDAS — contexto completo ──")
        for r in losses:
            print(f"  side={r['side']}  entry={r['entry_price']}  secs={r['secs_to_end']}  "
                  f"dist={r['abs_distance_bps']:.1f}bps  "
                  f"range60={r['market_range_60s']}  spot60=${r['spot_range_60s_usd']}  "
                  f"src_div={r['source_div_bps']}  "
                  f"variant={r['setup_variant']}  src={r['source']}  "
                  f"reentry={r['reentry']}  pnl={r['pnl']}")

    # Thresholds que separam wins de losses
    print("\n── Análise de thresholds (features que discriminam loss) ──")
    numeric_feats = ["secs_to_end", "abs_distance_bps", "source_div_bps",
                     "market_range_15s", "market_range_30s", "market_range_60s",
                     "spot_range_60s_usd"]
    for feat in numeric_feats:
        w_vals = [r[feat] for r in wins if r.get(feat) is not None]
        l_vals = [r[feat] for r in losses if r.get(feat) is not None]
        if not w_vals:
            continue
        print(f"\n  {feat}:")
        print(f"    WIN  ({len(w_vals)}): avg={statistics.mean(w_vals):.2f} "
              f"med={statistics.median(w_vals):.2f} "
              f"p10={sorted(w_vals)[max(0,len(w_vals)//10)]:.2f} "
              f"p90={sorted(w_vals)[min(len(w_vals)-1, 9*len(w_vals)//10)]:.2f}")
        if l_vals:
            print(f"    LOSS ({len(l_vals)}): avg={statistics.mean(l_vals):.2f} "
                  f"med={statistics.median(l_vals):.2f}")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    analyze_ar_paper()
    analyze_ar_regime()
    analyze_ee_paper()
