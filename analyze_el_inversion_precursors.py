"""
analyze_el_inversion_precursors.py

Analisa o que precede inversões fortes do Early Leader (inv_bid >= 0.72)
nos logs do monitor e do runner real.

Objetivo: encontrar a "assinatura" de snaps que precedem inversões fortes,
para que o sniper possa antecipar a entrada antes da inversão ser confirmada.

Fontes de dados aceitas:
  - logs/market_monitor_*.jsonl       (run_market_monitor.py — campos EL diretos)
  - logs/current_almost_resolved_real_*/  (runner real — type=snapshot c/ early_leader)

Uso:
    python analyze_el_inversion_precursors.py
    python analyze_el_inversion_precursors.py --log-dir logs/
    python analyze_el_inversion_precursors.py --strong-min 0.60  (relaxar critério)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

sys.stdout.reconfigure(encoding="utf-8")

# Thresholds
EL_MIN       = 0.55   # bid mínimo para detectar EL
STRONG_MIN   = 0.72   # inversão "forte" (novo líder ganha 100% nos dados)
MEDIUM_MIN   = 0.60   # inversão "média"
CONT_MIN     = 0.70   # F3: bid mínimo na janela 181-121s


# ---------------------------------------------------------------------------
# Carregamento de logs
# ---------------------------------------------------------------------------

def _sf(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


def _load_monitor_log(path: Path) -> Dict[str, List[dict]]:
    """Carrega market_monitor_*.jsonl — campos EL já calculados por snap."""
    by_slug: Dict[str, List[dict]] = defaultdict(list)
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
                "el_leader":   d.get("el_leader"),
                "el_bid_240":  _sf(d.get("el_bid_240")),
                "el_bid_180":  _sf(d.get("el_bid_180")),
                "el_vel":      _sf(d.get("el_vel")),
                "el_cont_ok":  bool(d.get("el_cont_ok")),
                "ts":          _sf(d.get("ts")),
                "source":      "monitor",
            })
    return by_slug


def _load_runner_log(path: Path) -> Dict[str, List[dict]]:
    """Carrega logs do runner real — type=snapshot com campo early_leader."""
    by_slug: Dict[str, List[dict]] = defaultdict(list)
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "snapshot":
                continue
            slug = (d.get("current_slug") or
                    (d.get("trade") or {}).get("event_slug") or
                    (d.get("signal") or {}).get("event_slug"))
            secs = d.get("current_secs")
            el = d.get("early_leader") or {}
            if not slug or secs is None:
                continue

            sig = d.get("signal") or {}
            up_bid   = _sf(sig.get("up_buy") or sig.get("up_bid"))
            down_bid = _sf(sig.get("down_buy") or sig.get("down_bid"))
            if up_bid == 0 and down_bid == 0:
                exec_m = (d.get("current_exec") or {})
                up_bid   = _sf(exec_m.get("up_bid"))
                down_bid = _sf(exec_m.get("down_bid"))

            by_slug[slug].append({
                "secs":        int(secs),
                "up_bid":      up_bid,
                "down_bid":    down_bid,
                "el_leader":   el.get("early_side"),
                "el_bid_240":  _sf(el.get("early_bid")),
                "el_bid_180":  0.0,   # não disponível diretamente no runner
                "el_vel":      0.0,
                "el_cont_ok":  bool(el.get("f3_ok")),
                "el_inverted": bool(el.get("inverted")),
                "el_inv_bid":  _sf(el.get("inversion_bid")),
                "el_inv_secs": el.get("inversion_secs"),
                "ts":          _sf(d.get("ts")),
                "source":      "runner",
                # decel gate do runner
                "decel_up":    (d.get("bid_decel_gate") or {}).get("up") or {},
                "decel_down":  (d.get("bid_decel_gate") or {}).get("down") or {},
            })
    return by_slug


def load_all_slugs(log_dir: str = "logs") -> Dict[str, List[dict]]:
    base = Path(log_dir)
    combined: Dict[str, List[dict]] = defaultdict(list)

    # Monitor logs
    for f in sorted(base.glob("market_monitor_*.jsonl")):
        for slug, snaps in _load_monitor_log(f).items():
            combined[slug].extend(snaps)

    # Runner real logs (subdiretórios)
    for subdir in sorted(base.glob("current_almost_resolved_real_*")):
        if subdir.is_dir():
            for f in sorted(subdir.glob("*.jsonl")):
                for slug, snaps in _load_runner_log(f).items():
                    combined[slug].extend(snaps)

    # Runner logs diretos na raiz de logs/
    for f in sorted(base.glob("runner_*.jsonl")):
        for slug, snaps in _load_runner_log(f).items():
            combined[slug].extend(snaps)

    # Ordenar por ts e deduplicar por (secs, source)
    result = {}
    for slug, snaps in combined.items():
        seen = set()
        unique = []
        for s in sorted(snaps, key=lambda x: x["ts"]):
            key = (s["secs"], s["source"])
            if key not in seen:
                seen.add(key)
                unique.append(s)
        if len(unique) >= 5:   # mínimo de cobertura
            result[slug] = unique
    return result


# ---------------------------------------------------------------------------
# Detecção de EL e inversão a partir dos snaps brutos
# ---------------------------------------------------------------------------

def _reconstruct_el(snaps: List[dict]) -> dict:
    """
    Reconstrói estado do EL a partir dos snaps brutos (para logs sem campo el_leader calculado).
    Retorna dict com early_leader, el_bid_240, el_bid_180, el_vel, cont_ok, inversion.
    """
    s240 = [s for s in snaps if 181 <= s["secs"] <= 240]
    s180 = [s for s in snaps if 121 <= s["secs"] <= 180]

    if not s240:
        return {}

    avg_up240 = sum(s["up_bid"] for s in s240) / len(s240)
    avg_dn240 = sum(s["down_bid"] for s in s240) / len(s240)

    if avg_up240 >= EL_MIN:
        el_side, el_bid_240 = "UP", avg_up240
    elif avg_dn240 >= EL_MIN:
        el_side, el_bid_240 = "DOWN", avg_dn240
    else:
        return {}

    el_bid_180 = 0.0
    el_vel = 0.0
    cont_ok = False
    if s180:
        bids180 = [s["up_bid"] if el_side == "UP" else s["down_bid"] for s in s180]
        el_bid_180 = sum(bids180) / len(bids180)
        el_vel = round(el_bid_180 - el_bid_240, 4)
        cont_ok = min(bids180) >= CONT_MIN

    # Detectar inversão: snap pós-secs 180 onde lado oposto domina com >= EL_MIN
    inv_snap = None
    for s in sorted(snaps, key=lambda x: x["secs"], reverse=True):
        if s["secs"] >= 180:
            continue
        opp_bid = s["down_bid"] if el_side == "UP" else s["up_bid"]
        own_bid = s["up_bid"] if el_side == "UP" else s["down_bid"]
        if opp_bid >= EL_MIN and opp_bid > own_bid:
            inv_snap = s
            break

    inv_bid  = _sf(inv_snap.get("down_bid" if el_side == "UP" else "up_bid")) if inv_snap else 0.0
    inv_secs = inv_snap["secs"] if inv_snap else None

    return {
        "early_leader": el_side,
        "el_bid_240":   round(el_bid_240, 4),
        "el_bid_180":   round(el_bid_180, 4),
        "el_vel":       el_vel,
        "cont_ok":      cont_ok,
        "inverted":     inv_snap is not None,
        "inv_bid":      round(inv_bid, 4),
        "inv_secs":     inv_secs,
    }


# ---------------------------------------------------------------------------
# Extração de precursores
# ---------------------------------------------------------------------------

def _extract_precursors(snaps: List[dict], el: dict) -> dict:
    """
    Extrai features dos snaps que precedem a inversão (ou que estão na janela 181-240).
    Foca em: el_vel, el_bid_240, el_bid_180, bid_vel do winner ~secs 130-160, loser momentum.
    """
    inv_secs = el.get("inv_secs") or 0
    el_side  = el.get("early_leader")
    if not el_side:
        return {}

    # Snaps na janela de detecção (240-181) — força do EL original
    s240 = [s for s in snaps if 181 <= s["secs"] <= 240]
    el_bids_240 = [s["up_bid"] if el_side == "UP" else s["down_bid"] for s in s240]
    el_bid_240_min  = min(el_bids_240) if el_bids_240 else 0.0
    el_bid_240_max  = max(el_bids_240) if el_bids_240 else 0.0

    # Snaps na janela de continuidade (180-121)
    s180 = [s for s in snaps if 121 <= s["secs"] <= 180]
    el_bids_180 = [s["up_bid"] if el_side == "UP" else s["down_bid"] for s in s180]
    el_bid_180_min = min(el_bids_180) if el_bids_180 else 0.0

    # Velocidade do EL nos últimos snaps antes da inversão (janela 150-180)
    pre_inv = [s for s in snaps if (inv_secs + 5) <= s["secs"] <= 180] if inv_secs else s180
    if len(pre_inv) >= 2:
        pre_inv_sorted = sorted(pre_inv, key=lambda x: x["secs"])
        first_el = pre_inv_sorted[0]["up_bid"] if el_side == "UP" else pre_inv_sorted[0]["down_bid"]
        last_el  = pre_inv_sorted[-1]["up_bid"] if el_side == "UP" else pre_inv_sorted[-1]["down_bid"]
        vel_pre_inv = round(last_el - first_el, 4)
    else:
        vel_pre_inv = None

    # Loser bid nos snaps imediatamente antes da inversão
    opp_side = "DOWN" if el_side == "UP" else "UP"
    if inv_secs:
        pre_window = [s for s in snaps if inv_secs < s["secs"] <= min(inv_secs + 40, 180)]
    else:
        pre_window = s180

    loser_bids_pre = [s["down_bid"] if opp_side == "DOWN" else s["up_bid"] for s in pre_window]
    loser_bid_pre_max = max(loser_bids_pre) if loser_bids_pre else 0.0
    loser_momentum_pre = None
    if len(loser_bids_pre) >= 2:
        loser_momentum_pre = round(loser_bids_pre[-1] - loser_bids_pre[0], 4)

    # F3: cont_ok e se houve dip abaixo de 0.70
    had_dip = el_bid_180_min < CONT_MIN if s180 else None

    return {
        "el_bid_240_min":     round(el_bid_240_min, 4),
        "el_bid_240_max":     round(el_bid_240_max, 4),
        "el_bid_180_min":     round(el_bid_180_min, 4),
        "had_f3_dip":         had_dip,       # bid caiu < 0.70 na janela 181-121
        "vel_pre_inv":        vel_pre_inv,   # variação do bid EL antes da inversão
        "loser_bid_pre_max":  round(loser_bid_pre_max, 4),
        "loser_momentum_pre": loser_momentum_pre,  # loser subindo antes da inversão
    }


# ---------------------------------------------------------------------------
# Classificação dos slugs
# ---------------------------------------------------------------------------

def classify_slugs(all_slugs: Dict[str, List[dict]]) -> List[dict]:
    records = []
    for slug, snaps in all_slugs.items():
        # Usar campos EL pré-calculados se disponíveis (monitor logs)
        last_with_el = next(
            (s for s in reversed(sorted(snaps, key=lambda x: x["secs"]))
             if s.get("el_leader")), None
        )
        if last_with_el:
            el = {
                "early_leader": last_with_el["el_leader"],
                "el_bid_240":   last_with_el["el_bid_240"],
                "el_bid_180":   last_with_el["el_bid_180"],
                "el_vel":       last_with_el["el_vel"],
                "cont_ok":      last_with_el["el_cont_ok"],
            }
            # Detectar inversão via snaps brutos (campo el_leader pode já refletir inversão)
            raw_el = _reconstruct_el(snaps)
            el["inverted"] = raw_el.get("inverted", False)
            el["inv_bid"]  = raw_el.get("inv_bid", 0.0)
            el["inv_secs"] = raw_el.get("inv_secs")
        else:
            el = _reconstruct_el(snaps)

        if not el.get("early_leader"):
            continue

        precursors = _extract_precursors(snaps, el)

        # Classificar inversão
        inv_bid  = el.get("inv_bid", 0.0)
        inv_secs = el.get("inv_secs")
        if not el.get("inverted"):
            inv_type = "stable"
        elif inv_bid >= STRONG_MIN:
            inv_type = "strong"   # novo líder ganha 100% (19/19)
        elif inv_bid >= MEDIUM_MIN:
            inv_type = "medium"   # novo líder ganha ~87%
        else:
            inv_type = "weak"     # apenas 54%

        records.append({
            "slug":          slug,
            "inv_type":      inv_type,
            "inv_bid":       inv_bid,
            "inv_secs":      inv_secs,
            **{f"el_{k}": v for k, v in el.items() if k not in ("inverted", "inv_bid", "inv_secs")},
            **precursors,
        })
    return records


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

def _avg(lst):
    lst = [x for x in lst if x is not None]
    return round(sum(lst) / len(lst), 4) if lst else None

def _pct(lst, fn):
    lst = [x for x in lst if x is not None]
    return round(100 * sum(1 for x in lst if fn(x)) / len(lst), 1) if lst else None


def report(records: List[dict]) -> None:
    stable = [r for r in records if r["inv_type"] == "stable"]
    strong = [r for r in records if r["inv_type"] == "strong"]
    medium = [r for r in records if r["inv_type"] == "medium"]
    weak   = [r for r in records if r["inv_type"] == "weak"]

    print()
    print("=" * 72)
    print("  ANÁLISE DE PRECURSORES DE INVERSÃO DO EARLY LEADER")
    print("=" * 72)
    print(f"  Total de slugs com EL: {len(records)}")
    print(f"  Estável (sem inversão) : {len(stable)}")
    print(f"  Inversão forte  (>=0.72): {len(strong)}")
    print(f"  Inversão média (0.60-0.72): {len(medium)}")
    print(f"  Inversão fraca  (<0.60): {len(weak)}")
    print()

    groups = [
        ("ESTÁVEL (baseline)", stable),
        ("INVERSÃO FORTE >=0.72 — novo líder vence 100%", strong),
        ("INVERSÃO MÉDIA 0.60-0.72 — novo líder vence ~87%", medium),
        ("INVERSÃO FRACA <0.60 — novo líder vence ~54%", weak),
    ]

    features = [
        ("el_el_bid_240",      "EL bid médio 240-181s (força inicial)"),
        ("el_el_bid_240_min",  "EL bid MÍN 240-181s"),
        ("el_el_vel",          "el_vel (crescimento 240→180)"),
        ("el_el_bid_180_min",  "EL bid MÍN 180-121s"),
        ("had_f3_dip",         "Dip < 0.70 na janela 180-121s (F3 falhou)"),
        ("vel_pre_inv",        "Velocidade EL nos snaps pré-inversão"),
        ("loser_bid_pre_max",  "Loser bid máx antes da inversão"),
        ("loser_momentum_pre", "Loser momentum antes da inversão"),
    ]

    for group_label, group in groups:
        if not group:
            continue
        print(f"  {'─' * 68}")
        print(f"  {group_label}  (n={len(group)})")
        print(f"  {'─' * 68}")
        for fkey, flabel in features:
            vals = [r.get(fkey) for r in group]
            if all(isinstance(v, bool) or v is None for v in vals):
                bvals = [v for v in vals if v is not None]
                pct = round(100 * sum(bvals) / len(bvals), 1) if bvals else None
                print(f"    {flabel:<45}: {pct}% (n={len(bvals)})")
            else:
                nvals = [v for v in vals if isinstance(v, (int, float)) and v is not None]
                if not nvals:
                    continue
                nvals_sorted = sorted(nvals)
                avg = _avg(nvals)
                p25 = nvals_sorted[len(nvals_sorted) // 4]
                p50 = nvals_sorted[len(nvals_sorted) // 2]
                p75 = nvals_sorted[3 * len(nvals_sorted) // 4]
                print(f"    {flabel:<45}: avg={avg:>7}  P25={p25:>7}  P50={p50:>7}  P75={p75:>7}")
        print()

    # Comparativo direto: estável vs forte
    if stable and strong:
        print("=" * 72)
        print("  COMPARATIVO DIRETO: Estável vs Inversão Forte")
        print("=" * 72)
        comparisons = [
            ("el_el_bid_240",      "EL bid 240 (força inicial)",       lambda v: v < 0.65),
            ("el_el_vel",          "el_vel negativo (EL caindo)",      lambda v: v < 0.0),
            ("el_el_vel",          "el_vel < 0.05 (EL fraco)",         lambda v: v < 0.05),
            ("had_f3_dip",         "F3 falhou (dip < 0.70)",           lambda v: v is True),
            ("loser_momentum_pre", "Loser momentum positivo",          lambda v: v is not None and v > 0.01),
        ]
        print(f"  {'Feature':<45} {'Estável':>10} {'Forte':>10}  {'Diferença':>10}")
        print(f"  {'':─<45} {'':─>10} {'':─>10}  {'':─>10}")
        for fkey, flabel, fn in comparisons:
            s_vals = [r.get(fkey) for r in stable if r.get(fkey) is not None]
            f_vals = [r.get(fkey) for r in strong if r.get(fkey) is not None]
            if not s_vals or not f_vals:
                continue
            s_pct = round(100 * sum(1 for v in s_vals if fn(v)) / len(s_vals), 1)
            f_pct = round(100 * sum(1 for v in f_vals if fn(v)) / len(f_vals), 1)
            delta = round(f_pct - s_pct, 1)
            arrow = "↑" if delta > 5 else ("↓" if delta < -5 else "~")
            print(f"  {flabel:<45} {s_pct:>9.1f}%  {f_pct:>9.1f}%  {delta:>+8.1f}% {arrow}")
        print()

    # Gate proposto: combinação de precursores para detectar inversão forte antecipadamente
    print("=" * 72)
    print("  GATE PROPOSTO — detectar inversão forte ANTES de ocorrer")
    print("=" * 72)
    gates = [
        ("el_vel < 0.05",         lambda r: (r.get("el_el_vel") or 0) < 0.05),
        ("F3 falhou",             lambda r: r.get("had_f3_dip") is True),
        ("loser momentum > 0.01", lambda r: (r.get("loser_momentum_pre") or 0) > 0.01),
        ("el_bid_240 < 0.65",     lambda r: (r.get("el_el_bid_240") or 1) < 0.65),
    ]
    combos = [
        ("el_vel<0.05  OR  F3 falhou",               lambda r: (r.get("el_el_vel") or 0) < 0.05 or r.get("had_f3_dip") is True),
        ("el_vel<0.05  AND  F3 falhou",              lambda r: (r.get("el_el_vel") or 0) < 0.05 and r.get("had_f3_dip") is True),
        ("loser_mom>0.01  AND  el_vel<0.05",         lambda r: (r.get("loser_momentum_pre") or 0) > 0.01 and (r.get("el_el_vel") or 0) < 0.05),
        ("loser_mom>0.01  OR  F3 falhou",            lambda r: (r.get("loser_momentum_pre") or 0) > 0.01 or r.get("had_f3_dip") is True),
        ("el_bid_240<0.65  AND  loser_mom>0.01",     lambda r: (r.get("el_el_bid_240") or 1) < 0.65 and (r.get("loser_momentum_pre") or 0) > 0.01),
    ]

    all_combos = [(label, fn) for label, fn in gates] + combos

    if stable and strong:
        print(f"  {'Gate / Combinação':<50} {'Captura forte%':>14} {'FP estável%':>13} {'Razão':>7}")
        print(f"  {'':─<50} {'':─>14} {'':─>13} {'':─>7}")
        for label, fn in all_combos:
            n_strong_hit = sum(1 for r in strong if fn(r))
            n_stable_hit = sum(1 for r in stable if fn(r))
            capture = round(100 * n_strong_hit / len(strong), 1) if strong else 0
            fp_rate = round(100 * n_stable_hit / len(stable), 1) if stable else 0
            ratio   = round(capture / fp_rate, 2) if fp_rate > 0 else float("inf")
            flag    = " ← MELHOR" if capture >= 50 and fp_rate <= 30 else ""
            print(f"  {label:<50} {capture:>13.1f}%  {fp_rate:>12.1f}%  {ratio:>6.2f}x{flag}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Análise de precursores de inversão EL")
    ap.add_argument("--log-dir", default="logs", help="Diretório dos logs (padrão: logs/)")
    ap.add_argument("--strong-min", type=float, default=STRONG_MIN,
                    help=f"Threshold para inversão 'forte' (padrão: {STRONG_MIN})")
    ap.add_argument("--out", default=None, help="Salvar registros em JSONL")
    args = ap.parse_args()

    print(f"Carregando logs de '{args.log_dir}'...", flush=True)
    all_slugs = load_all_slugs(args.log_dir)
    print(f"Slugs carregados com cobertura mínima: {len(all_slugs)}", flush=True)

    records = classify_slugs(all_slugs)
    print(f"Slugs com EL detectado: {len(records)}", flush=True)

    report(records)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, default=str) + "\n")
        print(f"Registros salvos em: {args.out}")


if __name__ == "__main__":
    main()
