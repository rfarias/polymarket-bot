"""
_sim_el_flip.py
Simula a estrategia Early Leader Inversion (EL Flip).

Detecta quando o lado dominante original (EL) perde a lideranca e entra no
NOVO lider quando bid esta em [entry_lo, entry_hi] e o gap >= flip_gap.

Base teorica (Secao 3, TESTES_ANALISE_EL.md):
  EL inverte com bid 0.60-0.72 -> novo lider vence ~90% das vezes (41 casos)

Uso:
    python _sim_el_flip.py
    python _sim_el_flip.py --logs "logs/ee_real_*/ee_real.jsonl"
    python _sim_el_flip.py --logs "test_data/ee_paper/**/*.jsonl"
    python _sim_el_flip.py --flip-gap 0.05 --entry-lo 0.60 --entry-hi 0.75
    python _sim_el_flip.py --no-stop
"""
from __future__ import annotations
import json, argparse
from pathlib import Path
from collections import defaultdict

QTY = 6.0

_ap = argparse.ArgumentParser()
_ap.add_argument('--logs',     default='logs/ee_paper_*/ee_paper.jsonl')
_ap.add_argument('--flip-gap', type=float, default=0.03,
                 help='Gap minimo opp_bid - orig_bid para considerar flip')
_ap.add_argument('--entry-lo', type=float, default=0.60)
_ap.add_argument('--entry-hi', type=float, default=0.72)
_ap.add_argument('--tp',       type=float, default=0.85)
_ap.add_argument('--stop',     type=float, default=0.45)
_ap.add_argument('--no-stop',  action='store_true',
                 help='Segurar ate resolucao sem stop (hold mode)')
_args = _ap.parse_args()

FLIP_GAP = _args.flip_gap
ENTRY_LO = _args.entry_lo
ENTRY_HI = _args.entry_hi
TP       = _args.tp
STOP     = _args.stop
NO_STOP  = _args.no_stop

# ---------------------------------------------------------------------------
# Carregar snapshots
# ---------------------------------------------------------------------------
print("Carregando snapshots...")
files = sorted(Path(".").glob(_args.logs))
print(f"  Arquivos: {len(files)}")

slug_snaps: dict[str, list] = defaultdict(list)
for lp in files:
    for line in lp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("type") != "snapshot":
            continue
        sl = r.get("current_slug", "")
        if not sl:
            continue
        slug_snaps[sl].append({
            "ts":     r.get("ts", 0),
            "secs":   r.get("current_secs"),
            "up_bid": r.get("current_exec", {}).get("up_bid", 0),
            "dn_bid": r.get("current_exec", {}).get("down_bid", 0),
            "el":     r.get("early_leader", {}),
        })

for sl in slug_snaps:
    slug_snaps[sl].sort(key=lambda s: s["ts"])

print(f"  Slugs unicos: {len(slug_snaps)}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bid(snap: dict, side: str) -> float:
    return snap["up_bid"] if side == "UP" else snap["dn_bid"]


def _other(side: str) -> str:
    return "DOWN" if side == "UP" else "UP"


def _detect_flip(snaps: list) -> dict | None:
    """Retorna info do primeiro flip valido (opp cruza orig + gap, na faixa)."""
    original_side: str | None = None
    for i, snap in enumerate(snaps):
        side = snap["el"].get("early_side")
        if original_side is None:
            if side is not None:
                original_side = side
            continue
        secs = snap["secs"]
        if secs is None:
            continue
        new_side = _other(original_side)
        orig_bid = _bid(snap, original_side)
        opp_bid  = _bid(snap, new_side)
        if opp_bid >= orig_bid + FLIP_GAP and ENTRY_LO <= opp_bid <= ENTRY_HI:
            return {
                "entry_idx":     i,
                "original_side": original_side,
                "new_side":      new_side,
                "ep":            round(opp_bid, 4),
                "secs":          secs,
                "gap":           round(opp_bid - orig_bid, 4),
            }
    return None


def _simulate_exit(snaps_after: list, ep: float, new_side: str) -> tuple[str, float]:
    """Simula saida apos entrada no flip."""
    opp_side = _other(new_side)
    for snap in snaps_after:
        secs = snap["secs"]
        if secs is None:
            continue
        nb = _bid(snap, new_side)
        ob = _bid(snap, opp_side)
        if not NO_STOP and nb <= STOP:
            return "STOP_LOSS", round((STOP - ep) * QTY, 4)
        if nb >= TP:
            return "TP_WIN", round((TP - ep) * QTY, 4)
        if secs <= 3:
            if nb >= 0.90:
                return "WIN_RESOLVE", round((1.0 - ep) * QTY, 4)
            elif ob >= 0.90:
                return "LOSS_RESOLVE", round((0.0 - ep) * QTY, 4)
    return "MISSED", 0.0


# ---------------------------------------------------------------------------
# Processar todos os slugs
# ---------------------------------------------------------------------------
n_no_el   = 0
n_no_flip = 0
results: list[dict] = []

for slug, snaps in slug_snaps.items():
    has_el = any(s["el"].get("early_side") is not None for s in snaps)
    if not has_el:
        n_no_el += 1
        continue

    flip = _detect_flip(snaps)
    if flip is None:
        n_no_flip += 1
        continue

    snaps_after = snaps[flip["entry_idx"] + 1:]
    outcome, pnl = _simulate_exit(snaps_after, flip["ep"], flip["new_side"])
    results.append({
        "slug":          slug,
        "original_side": flip["original_side"],
        "new_side":      flip["new_side"],
        "ep":            flip["ep"],
        "secs":          flip["secs"],
        "gap":           flip["gap"],
        "outcome":       outcome,
        "pnl":           pnl,
    })

valid = [r for r in results if r["outcome"] != "MISSED"]


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
HDR = f"  {'Cenario':<38}  {'n':>4}  {'WR':>7}  {'stop%':>6}  {'PnL':>9}  {'avg':>8}"
SEP = "  " + "-" * 76

def _row(label: str, grp: list, width: int = 38) -> None:
    if not grp:
        print(f"  {label:<{width}}  {'(vazio)'}")
        return
    n     = len(grp)
    wins  = sum(1 for r in grp if r["pnl"] > 0)
    stops = sum(1 for r in grp if r["outcome"] == "STOP_LOSS")
    pnl   = round(sum(r["pnl"] for r in grp), 2)
    print(f"  {label:<{width}}  {n:>4}  {wins/n:>7.1%}  {stops/n:>6.1%}  "
          f"{pnl:>+9.2f}  {pnl/n:>+8.3f}")


mode_str = "HOLD-TO-RESOLVE (sem stop)" if NO_STOP else f"TP={TP} / Stop={STOP}"
print(f"\n{'='*80}")
print(f"  EL FLIP — Early Leader Inversion")
print(f"  flip_gap>={FLIP_GAP}  entry=[{ENTRY_LO},{ENTRY_HI}]  {mode_str}")
print(f"{'='*80}")
print(f"  Total slugs no log:          {len(slug_snaps)}")
print(f"  Slugs sem EL estabelecido:   {n_no_el}")
print(f"  Slugs com EL, sem flip:      {n_no_flip}")
print(f"  Slugs com flip na faixa:     {len(results)}")
print(f"  Slugs com outcome simulado:  {len(valid)}")


# ---------------------------------------------------------------------------
# 1. Por janela de secs (quando o flip ocorre)
# ---------------------------------------------------------------------------
print(f"\n{'='*80}")
print(f"  1. POR JANELA DE TEMPO DO FLIP")
print(f"{'='*80}")
print(HDR); print(SEP)
secs_bins = [
    ("flip secs > 180   (muito cedo)",        lambda r: r["secs"] > 180),
    ("flip secs 180-121 (janela de elim)",    lambda r: 121 <= r["secs"] <= 180),
    ("flip secs 120-61  (meio candle)",       lambda r: 61  <= r["secs"] <= 120),
    ("flip secs 60-31   (ultimo minuto)",     lambda r: 31  <= r["secs"] <= 60),
    ("flip secs <= 30   (fim iminente)",      lambda r: r["secs"] <= 30),
]
for label, fn in secs_bins:
    _row(label, [r for r in valid if fn(r)])
print(); print(HDR); print(SEP)
cumulative = [
    ("flip secs <= 180",  lambda r: r["secs"] <= 180),
    ("flip secs <= 120",  lambda r: r["secs"] <= 120),
    ("flip secs <= 60",   lambda r: r["secs"] <= 60),
    ("flip secs <= 30",   lambda r: r["secs"] <= 30),
    ("base (todos)",      lambda r: True),
]
for label, fn in cumulative:
    _row(label, [r for r in valid if fn(r)])


# ---------------------------------------------------------------------------
# 2. Por preco de entrada
# ---------------------------------------------------------------------------
print(f"\n{'='*80}")
print(f"  2. POR PRECO DE ENTRADA (new leader bid)")
print(f"{'='*80}")
print(HDR); print(SEP)
ep_bins = [
    ("ep 0.60-0.63  (mais barato)",  0.60, 0.63),
    ("ep 0.63-0.66",                 0.63, 0.66),
    ("ep 0.66-0.69",                 0.66, 0.69),
    ("ep 0.69-0.72  (mais caro)",    0.69, 0.72),
]
for label, lo, hi in ep_bins:
    _row(label, [r for r in valid if lo <= r["ep"] < hi])
_row("base", valid)


# ---------------------------------------------------------------------------
# 3. Por tamanho do gap
# ---------------------------------------------------------------------------
print(f"\n{'='*80}")
print(f"  3. POR TAMANHO DO GAP DE INVERSAO (opp_bid - orig_bid)")
print(f"{'='*80}")
print(HDR); print(SEP)
gap_bins = [
    ("gap 0.03-0.10  (fraco)",      0.03, 0.10),
    ("gap 0.10-0.20  (moderado)",   0.10, 0.20),
    ("gap 0.20-0.35  (forte)",      0.20, 0.35),
    ("gap > 0.35     (dominante)",  0.35, 99.0),
]
for label, lo, hi in gap_bins:
    _row(label, [r for r in valid if lo <= r["gap"] < hi])
_row("base", valid)


# ---------------------------------------------------------------------------
# 4. Combinacoes
# ---------------------------------------------------------------------------
print(f"\n{'='*80}")
print(f"  4. COMBINACOES")
print(f"{'='*80}")
print(HDR); print(SEP)
combos = [
    ("base",                           lambda r: True),
    ("secs<=60",                       lambda r: r["secs"] <= 60),
    ("secs<=60 + gap>=0.10",           lambda r: r["secs"] <= 60  and r["gap"] >= 0.10),
    ("secs<=60 + gap>=0.20",           lambda r: r["secs"] <= 60  and r["gap"] >= 0.20),
    ("secs<=60 + ep<=0.67",            lambda r: r["secs"] <= 60  and r["ep"] <= 0.67),
    ("secs<=120 + gap>=0.10",          lambda r: r["secs"] <= 120 and r["gap"] >= 0.10),
    ("secs<=120 + gap>=0.20",          lambda r: r["secs"] <= 120 and r["gap"] >= 0.20),
    ("secs<=120 + ep<=0.67",           lambda r: r["secs"] <= 120 and r["ep"] <= 0.67),
    ("gap>=0.20",                      lambda r: r["gap"] >= 0.20),
    ("gap>=0.20 + secs<=120",          lambda r: r["gap"] >= 0.20 and r["secs"] <= 120),
    ("gap>=0.35 (dominant flip)",      lambda r: r["gap"] >= 0.35),
]
for label, fn in combos:
    _row(label, [r for r in valid if fn(r)])


# ---------------------------------------------------------------------------
# 5. Breakdown de outcomes
# ---------------------------------------------------------------------------
print(f"\n{'='*80}")
print(f"  5. BREAKDOWN DE OUTCOMES (base)")
print(f"{'='*80}")
oc_counts: dict[str, list] = defaultdict(list)
for r in valid:
    oc_counts[r["outcome"]].append(r)
print(f"  {'Outcome':<20}  {'n':>4}  {'avg':>8}  {'total':>9}")
print("  " + "-" * 48)
for oc in sorted(oc_counts):
    grp = oc_counts[oc]
    pnl_total = round(sum(r["pnl"] for r in grp), 2)
    print(f"  {oc:<20}  {len(grp):>4}  {pnl_total/len(grp):>+8.3f}  {pnl_total:>+9.2f}")
print(f"  {'TOTAL':<20}  {len(valid):>4}  "
      f"{sum(r['pnl'] for r in valid)/len(valid) if valid else 0:>+8.3f}  "
      f"{sum(r['pnl'] for r in valid):>+9.2f}")


# ---------------------------------------------------------------------------
# 6. EL-Flip vs EE: relacao entre os slugs
# ---------------------------------------------------------------------------
flip_slugs = {r["slug"] for r in results}
print(f"\n{'='*80}")
print(f"  6. CONTEXTO vs EE")
print(f"{'='*80}")
print(f"  Slugs com flip na faixa:     {len(flip_slugs)}")
print(f"  Slugs sem flip (EL estavel): {n_no_flip}")
print(f"  Slugs sem EL:                {n_no_el}")
print(f"  Nota: flip e EE sao mutuamente exclusivos por design —")
print(f"        EE entra no lider original, Flip entra no novo lider.")
print(f"        Em slugs onde o flip ocorre, o EE ou nao trigga (se flip")
print(f"        e antes do range EE 0.82-0.86) ou trigga e perde (se EL")
print(f"        original estava alto e depois inverteu).")
