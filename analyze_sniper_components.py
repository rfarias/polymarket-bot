"""
analyze_sniper_components.py

Para cada slug que entra na janela do sniper (winner >= MIN_WINNER, loser <= MAX_LOSER,
secs MIN_SECS-MAX_SECS), analisa se houve reversão real e qual componente estava ativo.

Componentes analisados INDEPENDENTEMENTE (sem score composto):
  B  — Sinal B: winner_vel <= VEL_WEAK  (desaceleração do winner)
  E  — Sinal E: loser_bid subiu >= MOM_WEAK em relação ao snap anterior
  EL_BLOCK — EL gate bloqueia: EL confirma winner (F3 ok, EL side == winner)
  EL_BOOST — EL gate boost: EL aponta pro loser (F3 ok, EL side != winner)
  EL_INV   — EL inverteu (novo líder == winner; inv_bid > 0.55)
  PRECUR   — Precursor: el_cont_ok=False AND el_vel < VEL_PRECUR
  LOSER_FLAT — loser_bid <= FLAT_THR (abaixo de 0.05; pouca liquidez)

Para cada componente: mostra taxa de reversão quando ativo vs inativo.

Uso:
    python analyze_sniper_components.py
    python analyze_sniper_components.py --min-winner 0.88 --max-loser 0.15
    python analyze_sniper_components.py --log-dir logs
"""
from __future__ import annotations

import argparse, json, sys, io
from pathlib import Path
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Parâmetros da janela sniper
MIN_WINNER  = 0.88
MAX_LOSER   = 0.15
MIN_SECS    = 15
MAX_SECS    = 120

# Thresholds dos componentes
VEL_WEAK    = -0.020   # Sinal B fraco (winner desacelerando)
VEL_STRONG  = -0.050   # Sinal B forte
MOM_WEAK    = 0.005    # Sinal E fraco (loser_bid subindo)
MOM_STRONG  = 0.015    # Sinal E forte
VEL_PRECUR  = 0.05     # Precursor: el_vel abaixo disto = EL fraco
FLAT_THR    = 0.05     # Loser "flat" (low liquidity)


def _sf(v, d=0.0):
    try: return float(v)
    except: return d


# ---------------------------------------------------------------------------
# Carrega slugs dos logs do monitor
# ---------------------------------------------------------------------------

def load_slugs(log_dir="logs"):
    by_slug = defaultdict(list)
    for path in sorted(Path(log_dir).glob("market_monitor_*.jsonl")):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "monitor_snap" or d.get("slot") != "current":
                    continue
                slug = d.get("slug")
                secs = d.get("secs")
                if not slug or secs is None:
                    continue
                by_slug[slug].append({
                    "secs":        int(secs),
                    "up_bid":      _sf(d.get("up_bid")),
                    "down_bid":    _sf(d.get("down_bid")),
                    "winner_vel":  _sf(d.get("winner_vel")),
                    "el_leader":   d.get("el_leader"),      # "UP"/"DOWN"/None
                    "el_vel":      _sf(d.get("el_vel")),
                    "el_cont_ok":  bool(d.get("el_cont_ok")),
                    "el_bid_240":  _sf(d.get("el_bid_240")),
                    "el_bid_180":  _sf(d.get("el_bid_180")),
                    "ts":          _sf(d.get("ts")),
                })

    result = {}
    for slug, snaps in by_slug.items():
        seen = set()
        unique = []
        for s in sorted(snaps, key=lambda x: x["ts"]):
            if s["secs"] not in seen:
                seen.add(s["secs"])
                unique.append(s)
        result[slug] = unique
    return result


# ---------------------------------------------------------------------------
# EL state extractor (sem depender do sniper tracker)
# ---------------------------------------------------------------------------

def get_el_state(snaps):
    """
    Retorna dict:
      el_leader, f3_ok, el_vel, inverted, inv_side, inv_bid
    onde inverted=True quando o lado oposto ao EL original ultrapassa 0.55
    numa janela posterior (secs < 180).
    """
    snaps_by_secs = {s["secs"]: s for s in snaps}

    # Detecta EL na janela 181-240
    early = [s for s in snaps if 181 <= s["secs"] <= 240 and s.get("el_leader")]
    if not early:
        return None

    el_side = early[-1]["el_leader"]   # mais recente
    el_bid  = _sf(early[-1].get("el_bid_240") or
                  (early[-1]["up_bid"] if el_side == "UP" else early[-1]["down_bid"]))

    # F3: bid do EL nunca cai abaixo de 0.70 em secs 121-180
    s180 = [s for s in snaps if 121 <= s["secs"] <= 180]
    el_bids_180 = []
    for s in s180:
        b = s["up_bid"] if el_side == "UP" else s["down_bid"]
        el_bids_180.append(b)
    f3_ok = (min(el_bids_180) >= 0.70) if el_bids_180 else True

    # el_vel do monitor (pega da janela 121-180)
    vel_vals = [s["el_vel"] for s in s180 if s.get("el_vel") is not None]
    el_vel = vel_vals[-1] if vel_vals else None

    # Detecta inversão: lado oposto ultrapassa 0.55 em qualquer snap secs < 180
    opp_side = "DOWN" if el_side == "UP" else "UP"
    inv_bid = 0.0
    inv_secs = None
    for s in sorted(snaps, key=lambda x: x["secs"], reverse=True):  # secs desc = mais cedo no tempo
        if s["secs"] >= 180:
            continue
        opp_b = s["up_bid"] if opp_side == "UP" else s["down_bid"]
        if opp_b >= 0.55 and opp_b > inv_bid:
            inv_bid = opp_b
            inv_secs = s["secs"]

    return {
        "el_side":  el_side,
        "el_bid":   el_bid,
        "f3_ok":    f3_ok,
        "el_vel":   el_vel,
        "inverted": inv_bid >= 0.55,
        "inv_side": opp_side if inv_bid >= 0.55 else None,
        "inv_bid":  inv_bid,
        "inv_secs": inv_secs,
    }


# ---------------------------------------------------------------------------
# Analisa um slug: encontra primeira entrada na janela sniper e outcome
# ---------------------------------------------------------------------------

def analyze_slug(snaps, min_winner, max_loser):
    """
    Retorna dict com componentes ativos no momento da entrada (ou None).
    """
    snaps_sorted = sorted(snaps, key=lambda x: x["secs"], reverse=True)  # secs desc = mais cedo

    # Encontra primeira oportunidade de entrada na janela
    entry = None
    for s in snaps_sorted:
        if not (MIN_SECS <= s["secs"] <= MAX_SECS):
            continue
        winner_bid = max(s["up_bid"], s["down_bid"])
        loser_bid  = min(s["up_bid"], s["down_bid"])
        loser_price = round(1.0 - winner_bid, 4)
        if winner_bid >= min_winner and 0 < loser_price <= max_loser:
            entry = s
            entry_winner  = "UP" if s["up_bid"] >= s["down_bid"] else "DOWN"
            entry_loser   = "DOWN" if entry_winner == "UP" else "UP"
            entry_loser_price = loser_price
            break

    if entry is None:
        return None

    # Outcome: qual lado venceu ao final?
    final = [s for s in snaps if s["secs"] <= 10]
    if not final:
        return None
    last = max(final, key=lambda x: x["secs"])
    winner_final = "UP" if last["up_bid"] >= last["down_bid"] else "DOWN"
    reversal = (entry_loser == winner_final)   # loser tornou-se vencedor

    # EL state
    el = get_el_state(snaps)

    # ---- Componentes individuais no momento da entrada ----

    # Sinal B: winner_vel no snap de entrada
    win_vel = _sf(entry.get("winner_vel"))
    sinal_b_weak   = win_vel <= VEL_WEAK
    sinal_b_strong = win_vel <= VEL_STRONG

    # Sinal E: loser_bid subiu em relação ao snap anterior na janela
    # (usa snaps dentro da janela para calcular momentum)
    in_window = [s for s in snaps if MIN_SECS <= s["secs"] <= MAX_SECS]
    in_window_sorted = sorted(in_window, key=lambda x: x["secs"], reverse=True)
    # Encontra snap de entrada e o anterior (secs maior = mais cedo)
    entry_idx = next((i for i, s in enumerate(in_window_sorted) if s["secs"] == entry["secs"]), None)
    loser_mom = 0.0
    if entry_idx is not None and entry_idx + 1 < len(in_window_sorted):
        prev = in_window_sorted[entry_idx + 1]
        cur_loser  = entry["up_bid"]  if entry_loser == "UP" else entry["down_bid"]
        prev_loser = prev["up_bid"]   if entry_loser == "UP" else prev["down_bid"]
        loser_mom  = round(cur_loser - prev_loser, 4)
    sinal_e_weak   = loser_mom >= MOM_WEAK
    sinal_e_strong = loser_mom >= MOM_STRONG

    # EL gate
    el_block = False
    el_boost = False
    el_inv   = False
    el_inv_strong = False
    el_precursor  = False

    if el is not None:
        el_side = el["el_side"]
        f3_ok   = el["f3_ok"]
        inverted = el["inverted"]
        inv_bid  = el["inv_bid"]
        vel      = el["el_vel"]

        if not inverted:
            if f3_ok:
                if el_side == entry_winner:
                    el_block = True   # EL confirma winner → bloqueia
                else:
                    el_boost = True   # EL aponta pro loser → boost
        else:
            el_inv = True
            el_inv_strong = inv_bid >= 0.72
            # Precursor: EL fraco (el_vel baixo) + F3 falhou
            if not f3_ok and vel is not None and vel < VEL_PRECUR:
                el_precursor = True

    loser_flat = entry_loser_price <= FLAT_THR

    return {
        "slug":             entry.get("slug", ""),
        "entry_secs":       entry["secs"],
        "entry_winner":     entry_winner,
        "entry_loser":      entry_loser,
        "entry_loser_price": entry_loser_price,
        "win_vel":          win_vel,
        "loser_mom":        loser_mom,
        "reversal":         reversal,
        # componentes
        "sinal_b_weak":     sinal_b_weak,
        "sinal_b_strong":   sinal_b_strong,
        "sinal_e_weak":     sinal_e_weak,
        "sinal_e_strong":   sinal_e_strong,
        "el_block":         el_block,
        "el_boost":         el_boost,
        "el_inv":           el_inv,
        "el_inv_strong":    el_inv_strong,
        "el_precursor":     el_precursor,
        "loser_flat":       loser_flat,
        "el_info":          el,
    }


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

def pct(n, tot):
    return f"{100*n/tot:.1f}%" if tot else "—"


def report_component(label, rows, flag_key):
    active   = [r for r in rows if r[flag_key]]
    inactive = [r for r in rows if not r[flag_key]]
    rev_a    = sum(1 for r in active if r["reversal"])
    rev_i    = sum(1 for r in inactive if r["reversal"])
    wr_a = 100 * rev_a / len(active)   if active   else 0
    wr_i = 100 * rev_i / len(inactive) if inactive else 0
    lift = wr_a / wr_i if wr_i > 0 else float("inf")

    status = ""
    if len(active) >= 3:
        if wr_a > wr_i * 1.5:
            status = " ← SINAL ÚTIL (ativo boosted)"
        elif wr_a < wr_i * 0.67:
            status = " ← SINAL ÚTIL (ativo bloqueia)"

    print(f"  {label:<26}  ativo: n={len(active):>3} WR={wr_a:>5.1f}%"
          f"  inativo: n={len(inactive):>3} WR={wr_i:>5.1f}%"
          f"  lift={lift:>5.2f}x{status}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-winner", type=float, default=MIN_WINNER)
    ap.add_argument("--max-loser",  type=float, default=MAX_LOSER)
    ap.add_argument("--log-dir",    default="logs")
    ap.add_argument("--show-cases", action="store_true",
                    help="Mostra detalhes de cada slug analisado")
    args = ap.parse_args()

    print(f"Carregando logs de '{args.log_dir}'...")
    all_slugs = load_slugs(args.log_dir)
    print(f"Slugs únicos: {len(all_slugs)}")
    print()

    rows = []
    for slug, snaps in all_slugs.items():
        r = analyze_slug(snaps, args.min_winner, args.max_loser)
        if r is None:
            continue
        r["slug"] = slug
        rows.append(r)

    if not rows:
        print("Nenhum slug entrou na janela do sniper.")
        return

    total    = len(rows)
    n_rev    = sum(1 for r in rows if r["reversal"])
    baseline = 100 * n_rev / total

    print("=" * 72)
    print(f"  ANÁLISE DE COMPONENTES DO SNIPER")
    print(f"  Janela: winner>={args.min_winner} loser<={args.max_loser} secs {MIN_SECS}-{MAX_SECS}")
    print(f"  Slugs na janela: {total}  |  Reversões reais: {n_rev} ({baseline:.1f}%)")
    print("=" * 72)
    print()

    print("  Componente                  [ ativo ]            [ inativo ]          lift")
    print("  " + "-" * 68)
    report_component("B_fraco (vel<=-0.020)",      rows, "sinal_b_weak")
    report_component("B_forte (vel<=-0.050)",      rows, "sinal_b_strong")
    report_component("E_fraco (mom>=+0.005)",      rows, "sinal_e_weak")
    report_component("E_forte (mom>=+0.015)",      rows, "sinal_e_strong")
    report_component("EL_block (EL=winner+F3ok)",  rows, "el_block")
    report_component("EL_boost (EL=loser+F3ok)",   rows, "el_boost")
    report_component("EL_inv (inversão >=0.55)",   rows, "el_inv")
    report_component("EL_inv_forte (>=0.72)",      rows, "el_inv_strong")
    report_component("EL_precursor (vel<0.05+F3)", rows, "el_precursor")
    report_component("Loser flat (<= 0.05)",       rows, "loser_flat")
    print()

    # Detalhes por faixa de loser_price
    print("=" * 72)
    print("  REVERSÃO POR FAIXA DE LOSER_PRICE")
    print("=" * 72)
    buckets = [(0.01, 0.04), (0.04, 0.07), (0.07, 0.10), (0.10, 0.13), (0.13, 0.16)]
    print(f"  {'Faixa':>14}  {'n':>5}  {'Reversões':>10}  {'WR%':>7}")
    for lo, hi in buckets:
        bt = [r for r in rows if lo <= r["entry_loser_price"] < hi]
        if not bt:
            continue
        bw = sum(1 for r in bt if r["reversal"])
        print(f"  {lo:.2f}–{hi:.2f}         {len(bt):>5}  {bw:>10}  {100*bw/len(bt):>6.1f}%")
    print()

    # Combinações mais comuns nas reversões vs não-reversões
    print("=" * 72)
    print("  PERFIL DOS CASOS: reversão real vs sem reversão")
    print("=" * 72)
    for label, subset in [("Reversão=True", [r for r in rows if r["reversal"]]),
                           ("Reversão=False", [r for r in rows if not r["reversal"]])]:
        if not subset:
            continue
        n = len(subset)
        print(f"\n  [{label}] n={n}")
        for k in ["sinal_b_weak","sinal_b_strong","sinal_e_weak","sinal_e_strong",
                  "el_block","el_boost","el_inv","el_inv_strong","el_precursor","loser_flat"]:
            cnt = sum(1 for r in subset if r[k])
            print(f"    {k:<32} {cnt:>3}/{n}  ({100*cnt/n:>5.1f}%)")

    # Casos com reversão não cobertos por nenhum sinal positivo
    no_signal = [r for r in rows if r["reversal"]
                 and not r["sinal_b_weak"] and not r["sinal_e_weak"]
                 and not r["el_boost"] and not r["el_precursor"]]
    print()
    print("=" * 72)
    print(f"  REVERSÕES SEM NENHUM SINAL ATIVO: {len(no_signal)}/{n_rev}")
    print("=" * 72)
    if no_signal:
        print(f"  {'Slug':>30}  {'secs':>5}  {'loser_p':>8}  {'win_vel':>8}  {'loser_mom':>10}  EL")
        for r in no_signal:
            el = r["el_info"]
            el_str = "—"
            if el:
                el_str = f"{el['el_side']} f3={el['f3_ok']} inv={el['inverted']}({el['inv_bid']:.2f})"
            print(f"  {r['slug']:>30}  {r['entry_secs']:>5}  {r['entry_loser_price']:>8.3f}"
                  f"  {r['win_vel']:>8.3f}  {r['loser_mom']:>10.4f}  {el_str}")
    print()

    if args.show_cases:
        print("=" * 72)
        print("  TODOS OS CASOS DETALHADOS")
        print("=" * 72)
        print(f"  {'Slug':>30}  {'secs':>4}  {'lp':>5}  {'rev':>3}  B  E  ELblk  ELbst  ELinv")
        for r in rows:
            print(f"  {r['slug']:>30}  {r['entry_secs']:>4}  {r['entry_loser_price']:>5.3f}"
                  f"  {'Y' if r['reversal'] else 'N':>3}"
                  f"  {'B' if r['sinal_b_weak'] else '.'}  {'E' if r['sinal_e_weak'] else '.'}"
                  f"  {'X' if r['el_block'] else '.':>5}  {'↑' if r['el_boost'] else '.':>5}"
                  f"  {'I' if r['el_inv'] else '.':>5}")


if __name__ == "__main__":
    main()
