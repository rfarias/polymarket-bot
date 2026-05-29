"""
_sim_win_prob_recalc.py — Recalcula as probabilidades de win usadas na extensao.

Analisa:
  1. Trades EE por faixa de bid do EL (valida numeros atuais na extensao)
  2. Trades COM inversao detectada -> WR do lado original (titular da posicao)
  3. Simulação inversa: WR do lado da inversao (quem entrou no novo lider)

Fontes: logs/ee_paper_*/ee_*.jsonl + logs/ee_real_adapted.jsonl
"""
from __future__ import annotations
import json, glob, os
from collections import defaultdict


# ---------------------------------------------------------------------------
# Carga de todos os logs EE paper + real
# ---------------------------------------------------------------------------
def load_all():
    all_rows_by_file = []
    # Real adaptado
    fn = "logs/ee_real_adapted.jsonl"
    if os.path.exists(fn):
        with open(fn, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        all_rows_by_file.append(rows)

    # Paper por sessão
    for d in sorted(glob.glob("logs/ee_paper_*")):
        if d.endswith(".log"):
            continue
        for fn in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
            try:
                with open(fn, encoding="utf-8") as f:
                    rows = [json.loads(l) for l in f if l.strip()]
                all_rows_by_file.append(rows)
            except Exception:
                pass
    return all_rows_by_file


def build_trades(all_rows_by_file):
    """Junta entry + snapshots + closed por sessao/slug."""
    trades = []
    for rows in all_rows_by_file:
        entries = {}
        closed_map = {}
        snaps = defaultdict(list)
        for r in rows:
            t = r.get("type")
            if t == "ee_paper_entry":
                entries[r["slug"]] = r
            elif t == "ee_paper_closed":
                closed_map[r["slug"]] = r
            elif t == "snapshot":
                slug = r.get("current_slug") or r.get("slug")
                if slug:
                    snaps[slug].append(r)

        for slug, entry in entries.items():
            if slug not in closed_map:
                continue
            cl = closed_map[slug]
            outcome = cl.get("ee", {}).get("outcome", "UNKNOWN")
            won = outcome in ("WIN", "WIN_HEDGE", "WIN_BOUNCE", "PROFIT_PROTECT")
            ep = entry.get("ep", 0.0)
            secs = entry.get("secs")
            el = entry.get("el") or {}
            entry_side = entry.get("side")

            # Analisa inversoes nos snapshots durante a posicao
            inv_seen = False
            inv_bid_max = 0.0
            inv_strong_seen = False
            # Bid do EL durante posicao (sequencia)
            el_bids_during = []
            opp_bids_during = []

            for s in snaps.get(slug, []):
                # Inversion from early_leader field
                el_field = s.get("early_leader") or {}
                if el_field.get("inverted") and el_field.get("inversion_side") != entry_side:
                    inv_seen = True
                    ib = el_field.get("inversion_bid", 0.0)
                    if ib > inv_bid_max:
                        inv_bid_max = ib
                    if el_field.get("inversion_strong"):
                        inv_strong_seen = True

                # Bids durante posicao (do lado EL e do lado oposto)
                ctx = s.get("current_scalp_context") or {}
                if entry_side == "UP":
                    el_bids_during.append(ctx.get("up_bid", 0))
                    opp_bids_during.append(ctx.get("down_bid", 0))
                elif entry_side == "DOWN":
                    el_bids_during.append(ctx.get("down_bid", 0))
                    opp_bids_during.append(ctx.get("up_bid", 0))

            # Maximo bid do opp visto durante posicao
            opp_max = max(opp_bids_during) if opp_bids_during else 0.0
            el_min = min((b for b in el_bids_during if b > 0), default=0.0)

            trades.append({
                "slug": slug,
                "outcome": outcome,
                "won": won,
                "ep": ep,
                "entry_secs": secs,
                "el_vel": el.get("el_vel", 0.0),
                "el_bid_240": el.get("el_bid_240", 0.0),
                "el_bid_180": el.get("el_bid_180", 0.0),
                "inv_seen": inv_seen,
                "inv_bid_max": inv_bid_max,
                "inv_strong": inv_strong_seen,
                "opp_max_during": round(opp_max, 3),
                "el_min_during": round(el_min, 3),
            })
    return trades


def pct(w, n):
    if n == 0:
        return "n/a"
    return f"{w/n*100:.0f}%"


def seg_stats(trades, label=""):
    n = len(trades)
    w = sum(1 for t in trades if t["won"])
    print(f"  {label}: {w}/{n} = {pct(w,n)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    all_rows = load_all()
    trades = build_trades(all_rows)
    n = len(trades)

    print("=" * 60)
    print(f"TOTAL TRADES ANALISADOS: {n}")
    print("=" * 60)

    # 1) Win rate geral
    won_all = sum(1 for t in trades if t["won"])
    print(f"\n[1] WR GERAL: {won_all}/{n} = {pct(won_all, n)}")

    # 2) WR por bid de entrada (ep) — valida numeros da extensao
    print("\n[2] WR POR BID DE ENTRADA (ep)")
    ranges = [
        (0.82, 0.84, "82-84% (zona EE padrao)"),
        (0.84, 0.87, "84-87%"),
        (0.87, 0.92, "87-92%"),
        (0.65, 0.82, "65-82% (fora janela EE)"),
    ]
    for lo, hi, lbl in ranges:
        seg = [t for t in trades if lo <= t["ep"] < hi]
        seg_stats(seg, f"ep [{lo:.2f},{hi:.2f}) {lbl}")

    # 3) WR por el_vel
    print("\n[3] WR POR EL_VEL NO ENTRY")
    for lo, hi, lbl in [(0.13, 99, "vel>=0.13"), (0.08, 0.13, "vel 0.08-0.13"), (0.0, 0.08, "vel<0.08")]:
        seg = [t for t in trades if lo <= t["el_vel"] < hi]
        seg_stats(seg, lbl)

    # 4) Inversao: WR do lado original
    print("\n[4] COM INVERSAO DETECTADA — WR DO LADO ORIGINAL (posicao atual)")
    inv = [t for t in trades if t["inv_seen"]]
    no_inv = [t for t in trades if not t["inv_seen"]]
    seg_stats(inv, "COM inversao")
    seg_stats(no_inv, "SEM inversao")

    if inv:
        print("\n  Detalhe por forca da inversao:")
        seg_stats([t for t in inv if t["inv_strong"]], "inv_strong (bid>=0.72)")
        seg_stats([t for t in inv if not t["inv_strong"]], "inv_moderada (<0.72)")

        print("\n  Detalhe por inv_bid_max (opp no pico):")
        for lo, hi in [(0.55, 0.62), (0.62, 0.68), (0.68, 0.73), (0.73, 0.80), (0.80, 1.01)]:
            seg = [t for t in inv if lo <= t["inv_bid_max"] < hi]
            if seg:
                seg_stats(seg, f"opp_max [{lo:.2f},{hi:.2f})")

    # 5) WR do opp_max durante posicao (todos os trades, nao so inversao)
    print("\n[5] WR POR OPP_MAX VISTO DURANTE POSICAO (todos os trades)")
    for lo, hi in [(0.0, 0.30), (0.30, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 1.01)]:
        seg = [t for t in trades if lo <= t["opp_max_during"] < hi]
        if seg:
            seg_stats(seg, f"opp_max [{lo:.2f},{hi:.2f})")

    # 6) Inversao vs WR do novo lado (perspectiva do entrante na inversao)
    print("\n[6] INVERSAO — PERSPECTIVA DO NOVO LADO (quem entraria na reversao)")
    if inv:
        inv_won_by_opp = [t for t in inv if not t["won"]]  # original perdeu = novo ganhou
        inv_lost_by_opp = [t for t in inv if t["won"]]     # original ganhou = novo perdeu
        opp_wins = len(inv_won_by_opp)
        opp_total = len(inv)
        print(f"  Novo lado ganhou: {opp_wins}/{opp_total} = {pct(opp_wins, opp_total)}")
        print("\n  Detalhe por forca:")
        for seg, lbl in [
            ([t for t in inv if t["inv_strong"]], "FORTE (bid>=0.72)"),
            ([t for t in inv if not t["inv_strong"]], "moderada (<0.72)"),
        ]:
            if seg:
                opp_w = sum(1 for t in seg if not t["won"])
                print(f"    {lbl}: novo lado {opp_w}/{len(seg)} = {pct(opp_w, len(seg))}")

    # 7) WR por faixa de bid EL bid_180 (bid na janela F3)
    print("\n[7] WR POR EL_BID_180 (bid na janela F3)")
    for lo, hi in [(0.0, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 1.01)]:
        seg = [t for t in trades if lo <= t["el_bid_180"] < hi]
        if seg:
            seg_stats(seg, f"bid_180 [{lo:.2f},{hi:.2f})")

    print("\n" + "=" * 60)
    print("Legenda outcomes:")
    from collections import Counter
    oc = Counter(t["outcome"] for t in trades)
    for k, v in sorted(oc.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
