"""
_sim_win_prob_all_slugs.py — Recalcula probabilidades sobre TODOS os slugs
observados (com ou sem entrada), usando snapshots dos logs de paper EE.

Para cada slug:
- EL side detectado (early_leader.early_side no pico)
- Vencedor real (up_bid ou down_bid perto de 1.0 nos ultimos snapshots)
- Inversao detectada (early_leader.inverted ou bid opp atingiu > threshold)
- OPP bid maximo visto durante o slug

Computa WR do EL original, WR do novo lider, por faixa de opp_bid.
"""
from __future__ import annotations
import json, glob, os
from collections import defaultdict

RESOLVE_THR = 0.10   # abaixo disso, lado "perdeu" no final


def load_all_snapshots():
    """Carrega snapshots de todos os logs ee_paper e ee_real_adapted."""
    slugs: dict[str, list[dict]] = defaultdict(list)  # slug -> snapshots

    def process_rows(rows):
        for r in rows:
            if r.get("type") != "snapshot":
                continue
            slug = r.get("current_slug") or r.get("slug")
            if slug:
                slugs[slug].append(r)

    fn = "logs/ee_real_adapted.jsonl"
    if os.path.exists(fn):
        with open(fn, encoding="utf-8") as f:
            process_rows([json.loads(l) for l in f if l.strip()])

    for d in sorted(glob.glob("logs/ee_paper_*")):
        if d.endswith(".log"):
            continue
        for fn in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
            try:
                with open(fn, encoding="utf-8") as f:
                    process_rows([json.loads(l) for l in f if l.strip()])
            except Exception:
                pass

    return slugs


def analyze_slug(snaps: list[dict]):
    """Retorna dict com analise do slug."""
    # Ordena por secs decrescente (240 -> 0)
    snaps = sorted(snaps, key=lambda r: r.get("current_secs", 0), reverse=True)

    # EL side: usa snap com mais amostras (secs ~240)
    el_side = None
    el_bid_240 = 0.0
    for r in snaps:
        el = r.get("early_leader") or {}
        if el.get("early_side") and (el.get("n_s240", 0) or 0) >= 3:
            el_side = el["early_side"]
            el_bid_240 = el.get("el_bid_240", 0.0)
            break
    if not el_side:
        # Tenta qualquer snap com early_side
        for r in snaps:
            el = r.get("early_leader") or {}
            if el.get("early_side"):
                el_side = el["early_side"]
                break

    if not el_side:
        return None  # sem EL detectado

    # Vencedor real: ultimos snapshots (secs <= 15)
    final_snaps = [r for r in snaps if r.get("current_secs", 99) <= 15]
    winner = None
    for r in reversed(final_snaps):
        exec_ = r.get("current_exec") or {}
        ub = exec_.get("up_bid", 0.5)
        db = exec_.get("down_bid", 0.5)
        if ub < RESOLVE_THR:
            winner = "DOWN"
            break
        elif db < RESOLVE_THR:
            winner = "UP"
            break
    if winner is None:
        return None  # nao conseguiu determinar vencedor

    el_won = winner == el_side

    # OPP bid maximo durante o slug (sinal de reversao)
    opp_bid_max = 0.0
    el_bid_min = 1.0
    inv_seen = False
    inv_bid_max = 0.0
    inv_strong = False
    entry_made = False

    for r in snaps:
        secs_r = r.get("current_secs", 0)
        exec_ = r.get("current_exec") or {}
        if el_side == "UP":
            el_b = exec_.get("up_bid", 0)
            opp_b = exec_.get("down_bid", 0)
        else:
            el_b = exec_.get("down_bid", 0)
            opp_b = exec_.get("up_bid", 0)

        # Exclui snapshots finais de resolucao (bids ja estao 0/1)
        # para nao confundir bid de resolucao com movimento durante a janela
        if secs_r > 15:
            if opp_b > opp_bid_max:
                opp_bid_max = opp_b
            if el_b > 0 and el_b < el_bid_min:
                el_bid_min = el_b

        # Inversao pelo campo early_leader
        el_field = r.get("early_leader") or {}
        if el_field.get("inverted") and el_field.get("inversion_side") != el_side:
            inv_seen = True
            ib = el_field.get("inversion_bid", 0.0)
            if ib > inv_bid_max:
                inv_bid_max = ib
            if el_field.get("inversion_strong"):
                inv_strong = True

        # Entrada EE feita
        if r.get("ee_position") is not None:
            entry_made = True

    return {
        "el_side": el_side,
        "winner": winner,
        "el_won": el_won,
        "el_bid_240": round(el_bid_240, 3),
        "opp_bid_max": round(opp_bid_max, 3),
        "el_bid_min": round(el_bid_min, 3),
        "inv_seen": inv_seen,
        "inv_bid_max": round(inv_bid_max, 3),
        "inv_strong": inv_strong,
        "entry_made": entry_made,
        "n_snaps": len(snaps),
    }


def pct(w, n):
    if n == 0:
        return "n/a"
    return f"{w/n*100:.0f}%"


def seg_stats(slugs, label=""):
    n = len(slugs)
    w = sum(1 for s in slugs if s["el_won"])
    print(f"  {label}: {w}/{n} = {pct(w, n)}")
    return w, n


def main():
    print("Carregando snapshots...")
    slug_snaps = load_all_snapshots()
    print(f"  Slugs com snapshots: {len(slug_snaps)}")

    all_slugs = []
    no_el = 0
    no_winner = 0

    for slug, snaps in slug_snaps.items():
        r = analyze_slug(snaps)
        if r is None:
            if any((s.get("early_leader") or {}).get("early_side") for s in snaps):
                no_winner += 1
            else:
                no_el += 1
            continue
        r["slug"] = slug
        all_slugs.append(r)

    n_total = len(all_slugs)
    print(f"  Slugs analisados: {n_total}")
    print(f"  Sem EL detectado: {no_el}")
    print(f"  Sem resolucao clara: {no_winner}")

    print("\n" + "=" * 62)
    print(f"ANALISE SOBRE TODOS OS {n_total} SLUGS OBSERVADOS")
    print("=" * 62)

    # 1) WR geral do EL
    won = sum(1 for s in all_slugs if s["el_won"])
    print(f"\n[1] WR DO EL ORIGINAL (base rate): {won}/{n_total} = {pct(won, n_total)}")

    # 2) WR por el_bid_240 (forca inicial do EL)
    print("\n[2] WR POR EL_BID_240 (forca inicial na janela 181-240s)")
    for lo, hi in [(0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 1.01)]:
        seg = [s for s in all_slugs if lo <= s["el_bid_240"] < hi]
        if seg:
            seg_stats(seg, f"el_bid_240 [{lo:.2f},{hi:.2f})")

    # 3) WR por opp_bid maximo observado no slug
    print("\n[3] WR POR OPP_BID MAXIMO OBSERVADO (chance do EL original ganhar)")
    for lo, hi in [(0.0, 0.25), (0.25, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.01)]:
        seg = [s for s in all_slugs if lo <= s["opp_bid_max"] < hi]
        if seg:
            w, n2 = seg_stats(seg, f"opp_max [{lo:.2f},{hi:.2f})")

    # 4) Inversao detectada — perspectivas
    print("\n[4] COM/SEM INVERSAO DETECTADA (campo early_leader.inverted)")
    inv_slugs  = [s for s in all_slugs if s["inv_seen"]]
    no_inv     = [s for s in all_slugs if not s["inv_seen"]]
    seg_stats(inv_slugs, "COM inversao: EL original ganhou")
    seg_stats(no_inv,    "SEM inversao: EL original ganhou")

    if inv_slugs:
        print("\n  Detalhe inversao — EL original:")
        seg_stats([s for s in inv_slugs if s["inv_strong"]],     "inv FORTE (bid>=0.72): EL ganhou")
        seg_stats([s for s in inv_slugs if not s["inv_strong"]], "inv moderada (<0.72): EL ganhou")

        print("\n  Perspectiva novo lider (reversal):")
        n_inv = len(inv_slugs)
        w_rev = sum(1 for s in inv_slugs if not s["el_won"])  # EL perdeu = reversal ganhou
        print(f"  Novo lider ganhou: {w_rev}/{n_inv} = {pct(w_rev, n_inv)}")

        print("\n  Por inv_bid_max (bid do opp no pico):")
        for lo, hi in [(0.55, 0.62), (0.62, 0.68), (0.68, 0.73), (0.73, 0.80), (0.80, 1.01)]:
            seg = [s for s in inv_slugs if lo <= s["inv_bid_max"] < hi]
            if seg:
                w_rev_seg = sum(1 for s in seg if not s["el_won"])
                print(f"    opp_max [{lo:.2f},{hi:.2f}): reversal {w_rev_seg}/{len(seg)} = {pct(w_rev_seg, len(seg))} | EL orig {len(seg)-w_rev_seg}/{len(seg)} = {pct(len(seg)-w_rev_seg, len(seg))}")

    # 5) WR em slugs onde houve entrada EE vs sem entrada
    print("\n[5] ENTRADA EE REALIZADA vs NAO REALIZADA")
    with_entry = [s for s in all_slugs if s["entry_made"]]
    no_entry   = [s for s in all_slugs if not s["entry_made"]]
    seg_stats(with_entry, "Com entrada EE")
    seg_stats(no_entry,   "Sem entrada (nao atingiu sinal)")

    # 6) Tabela completa opp_bid -> WR para mostrar na extensao
    print("\n[6] TABELA PARA EXTENSAO: EL WR por opp_bid durante janela (secs>15)")
    print("  opp_bid_max  | EL orig WR | Reversal WR |  n")
    thresholds = [
        (0.0,  0.30, "opp < 0.30: EL dominant"),
        (0.30, 0.40, "opp 0.30-0.40"),
        (0.40, 0.50, "opp 0.40-0.50"),
        (0.50, 0.60, "opp 0.50-0.60: reequilibrio"),
        (0.60, 0.70, "opp 0.60-0.70: inversao moderada"),
        (0.70, 0.80, "opp 0.70-0.80: inversao forte"),
        (0.80, 1.01, "opp >= 0.80: reversao dominante"),
    ]
    for lo, hi, lbl in thresholds:
        seg = [s for s in all_slugs if lo <= s["opp_bid_max"] < hi]
        if seg:
            w = sum(1 for s in seg if s["el_won"])
            wr_el = w / len(seg) * 100
            wr_rev = (len(seg) - w) / len(seg) * 100
            print(f"  [{lo:.2f},{hi:.2f}): EL={wr_el:5.1f}% | Rev={wr_rev:5.1f}%  n={len(seg):3d}  ({lbl})")

    # 7) Reversal: analise do NOVO LADO (flip strategy) por faixa de opp_bid_max
    # Apenas slugs onde opp atingiu > 0.55 (requisito minimo da faixa de flip)
    print("\n[7] REVERSAL (flip) — WR DO NOVO LIDER por faixa de bid atingido")
    print("  (slugs onde opp_bid_max >= 0.55 durante janela ativa)")
    flip_slugs = [s for s in all_slugs if s["opp_bid_max"] >= 0.55]
    if flip_slugs:
        total = len(flip_slugs)
        rev_won = sum(1 for s in flip_slugs if not s["el_won"])
        print(f"  Todos com opp>=0.55: reversal ganhou {rev_won}/{total} = {pct(rev_won, total)}")
        for lo, hi in [(0.55, 0.62), (0.62, 0.68), (0.68, 0.73), (0.73, 0.80), (0.80, 1.01)]:
            seg = [s for s in flip_slugs if lo <= s["opp_bid_max"] < hi]
            if seg:
                w = sum(1 for s in seg if not s["el_won"])
                print(f"  opp_max [{lo:.2f},{hi:.2f}): reversal {w}/{len(seg)} = {pct(w, len(seg))} | EL orig {len(seg)-w}/{len(seg)} = {pct(len(seg)-w, len(seg))}")

    # 8) Reversal por EL bid_240 inicial + opp_max (contexto combinado)
    print("\n[8] REVERSAL COMBINADO: el_bid_240 forte + opp_max crescendo")
    for el_lo, el_hi, el_lbl in [(0.55, 0.65, "EL fraco 0.55-0.65"), (0.65, 0.75, "EL medio 0.65-0.75"), (0.75, 1.01, "EL forte >=0.75")]:
        el_seg = [s for s in all_slugs if el_lo <= s["el_bid_240"] < el_hi]
        if el_seg:
            w_el = sum(1 for s in el_seg if s["el_won"])
            print(f"\n  {el_lbl} (n={len(el_seg)}, EL WR base={pct(w_el, len(el_seg))}):")
            for lo, hi in [(0.0, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 1.01)]:
                seg2 = [s for s in el_seg if lo <= s["opp_bid_max"] < hi]
                if seg2:
                    w = sum(1 for s in seg2 if s["el_won"])
                    print(f"    opp_max [{lo:.2f},{hi:.2f}): EL={pct(w, len(seg2))} n={len(seg2)}")

    # 9) Comparacao entrada EE vs universo completo
    print("\n[9] SELECAO DO GATE EE (slugs com entrada vs sem)")
    with_entry = [s for s in all_slugs if s["entry_made"]]
    no_entry   = [s for s in all_slugs if not s["entry_made"]]
    w_entry = sum(1 for s in with_entry if s["el_won"])
    w_no    = sum(1 for s in no_entry if s["el_won"])
    print(f"  Com entrada EE (gate ativo): {w_entry}/{len(with_entry)} = {pct(w_entry, len(with_entry))}")
    print(f"  Sem entrada (gate bloqueou): {w_no}/{len(no_entry)} = {pct(w_no, len(no_entry))}")
    print(f"  => Gate filtra {len(no_entry)} slugs com WR {pct(w_no, len(no_entry))} (vs {pct(w_entry, len(with_entry))} aprovados)")


if __name__ == "__main__":
    main()
