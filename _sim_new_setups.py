#!/usr/bin/env python3
"""
_sim_new_setups.py

Análise exploratória: busca novos padrões de alta probabilidade
nos logs EE paper, além dos setups já conhecidos.

Seção A: Condições pré-entrada vs WR nos trades existentes
         → quais features separam wins de losses?

Seção B: Simulação de novos setups em toda a história de slugs
         → há janelas com WR alto onde o mercado ainda está barato?

Uso:
  python _sim_new_setups.py
  python _sim_new_setups.py --logs "logs/ee_paper_*/ee_paper.jsonl"
"""

import argparse
import glob
import json
import sys
from collections import defaultdict

# Garante saída UTF-8 no Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_PATTERN = "logs/ee_paper_*/ee_paper.jsonl"
QTY = 6.0


# --- I/O ------------------------------------------------------------------

def load_events(pattern):
    events = []
    for path in sorted(glob.glob(pattern, recursive=True)):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
    return events


# --- Helpers --------------------------------------------------------------

def get_el_opp(snap):
    """Return (el_bid, opp_bid, side) from snapshot, or (None, None, None)."""
    el = snap.get("early_leader", {})
    side = el.get("early_side")
    exec_ = snap.get("current_exec", {})
    up = exec_.get("up_bid", 0.0)
    down = exec_.get("down_bid", 0.0)
    if side == "UP":
        return up, down, "UP"
    elif side == "DOWN":
        return down, up, "DOWN"
    return None, None, None


def bucket(val, edges, labels):
    for i, e in enumerate(edges):
        if val < e:
            return labels[i]
    return labels[-1]


def ev_per_share(wr, ep):
    """EV = WR*(1-ep) - (1-WR)*ep = WR - ep."""
    return wr - ep


def print_sep(title=""):
    print(f"\n{'='*64}")
    if title:
        print(f"  {title}")
        print('='*64)


def print_table(segments, min_n=5, show_ep=False):
    """Print WR table. segments: {label: [wins, total, ep_sum]}"""
    rows = []
    for label in sorted(segments.keys()):
        wins, total, ep_sum = segments[label]
        if total < min_n:
            continue
        wr = wins / total
        ep = ep_sum / total if total > 0 else 0
        ev = ev_per_share(wr, ep) if show_ep else None
        rows.append((label, wr, wins, total, ep, ev))
    rows.sort(key=lambda r: r[1], reverse=True)
    for label, wr, wins, total, ep, ev in rows:
        bar = "#" * int(wr * 20)
        base = f"  {label:<42} {wr*100:5.1f}%  ({wins}/{total})  {bar}"
        if show_ep and ep > 0:
            ev_str = f"  EV={ev:+.3f}/share"
            base += ev_str
        print(base)


# --- SECTION A ------------------------------------------------------------

def section_a(events):
    print_sep("A) Condições pré-entrada vs WR nos trades existentes")

    snaps_by_key = defaultdict(list)
    entries = {}
    closes = {}

    for ev in events:
        t = ev.get("type", "")
        sid = ev.get("session_id", "")
        if t == "snapshot":
            slug = ev.get("current_slug", "")
            snaps_by_key[(sid, slug)].append(ev)
        elif t == "ee_paper_entry":
            slug = ev.get("slug", "")
            entries[(sid, slug)] = ev
        elif t == "ee_paper_closed":
            slug = ev.get("slug", "")
            closes[(sid, slug)] = ev

    trades = []
    for key, entry in entries.items():
        close = closes.get(key)
        if not close:
            continue
        pnl = close.get("ee", {}).get("pnl", 0)
        win = pnl > 0

        entry_ts = entry["ts"]
        el = entry.get("el", {})
        ep = entry.get("ep", 0.0)
        secs = entry.get("secs", 0)
        vel = el.get("el_vel", 0.0)
        n180 = el.get("n_s180", 0)
        el180 = el.get("el_bid_180", 0.0)

        pre = sorted(
            [s for s in snaps_by_key[key] if s["ts"] < entry_ts],
            key=lambda s: s["ts"]
        )
        if not pre:
            continue

        opp_series = []
        el_series = []
        for s in pre:
            el_b, opp_b, _ = get_el_opp(s)
            if el_b is not None:
                opp_series.append((s["ts"], opp_b))
                el_series.append((s["ts"], el_b))

        opp_at_entry = opp_series[-1][1] if opp_series else None

        opp_30s = [(ts, b) for ts, b in opp_series if entry_ts - ts <= 30]
        opp_decay_30s = None
        opp_monotonic = None
        if len(opp_30s) >= 3:
            vals = [b for _, b in opp_30s]
            opp_decay_30s = vals[0] - vals[-1]
            opp_monotonic = all(vals[i] >= vals[i+1] for i in range(len(vals)-1))

        el_10s = [b for ts, b in el_series if entry_ts - ts <= 10]
        el_variance = None
        if len(el_10s) >= 3:
            m = sum(el_10s) / len(el_10s)
            el_variance = sum((v - m) ** 2 for v in el_10s) / len(el_10s)

        trades.append(dict(
            win=win, ep=ep, secs=secs, vel=vel, n180=n180, el180=el180,
            opp_at_entry=opp_at_entry, opp_decay_30s=opp_decay_30s,
            opp_monotonic=opp_monotonic, el_variance=el_variance,
        ))

    if not trades:
        print("  Nenhum trade encontrado.")
        return

    total_w = sum(1 for t in trades if t["win"])
    print(f"\n  Total: {len(trades)} trades  |  Wins: {total_w}  |"
          f"  WR geral: {total_w/len(trades)*100:.1f}%")

    def seg_wr(key_fn, trades_list):
        d = defaultdict(lambda: [0, 0, 0.0])
        for t in trades_list:
            lbl = key_fn(t)
            if lbl is None:
                continue
            d[lbl][1] += 1
            d[lbl][2] += t["ep"]
            if t["win"]:
                d[lbl][0] += 1
        return d

    # A1: el_vel
    print("\n  A1. WR por el_vel na entrada:")
    print_table(seg_wr(
        lambda t: bucket(t["vel"],
                         [0.13, 0.17, 0.22, 0.30, 999],
                         ["<0.13", "0.13-0.17", "0.17-0.22", "0.22-0.30", "0.30+"]),
        trades), min_n=3, show_ep=True)

    # A2: opp_bid na entrada
    print("\n  A2. WR por nível do opp_bid na entrada:")
    print_table(seg_wr(
        lambda t: bucket(t["opp_at_entry"] or 999,
                         [0.05, 0.10, 0.15, 0.20, 999],
                         ["<0.05", "0.05-0.10", "0.10-0.15", "0.15-0.20", "0.20+"])
                 if t["opp_at_entry"] is not None else None,
        trades), min_n=3, show_ep=True)

    # A3: decaimento opp_bid (30s)
    print("\n  A3. WR por decaimento do opp_bid nos 30s pré-entrada:")
    print_table(seg_wr(
        lambda t: bucket(t["opp_decay_30s"] or -999,
                         [0.0, 0.05, 0.15, 0.30, 999],
                         ["subindo", "estável(0-0.05)", "decai(0.05-0.15)",
                          "colapso(0.15-0.30)", "colapso+(0.30+)"])
                 if t["opp_decay_30s"] is not None else None,
        trades), min_n=3, show_ep=True)

    # A4: opp_bid monotônico
    print("\n  A4. WR — decaimento monotônico opp_bid (30s):")
    print_table(seg_wr(
        lambda t: "monotônico-dec" if t["opp_monotonic"] else "nao-monotônico"
                  if t["opp_monotonic"] is not None else None,
        trades), min_n=3, show_ep=True)

    # A5: estabilidade el_bid (variância 10s)
    print("\n  A5. WR por estabilidade do el_bid (variância 10s antes):")
    print_table(seg_wr(
        lambda t: bucket(t["el_variance"] or 999,
                         [5e-5, 5e-4, 2e-3, 999],
                         ["plateau(<5e-5)", "estável(5e-5–5e-4)",
                          "movendo(5e-4–2e-3)", "volátil(2e-3+)"])
                 if t["el_variance"] is not None else None,
        trades), min_n=3, show_ep=True)

    # A6: ep
    print("\n  A6. WR por preço de entrada (ep):")
    print_table(seg_wr(
        lambda t: f"ep={t['ep']:.2f}",
        trades), min_n=3, show_ep=True)

    # A7: secs
    print("\n  A7. WR por secs na entrada:")
    print_table(seg_wr(
        lambda t: bucket(t["secs"],
                         [60, 90, 120, 155, 999],
                         ["35-60s", "60-90s", "90-120s", "120-155s", ">155s"]),
        trades), min_n=3, show_ep=True)

    # A8: n_s180
    print("\n  A8. WR por n_s180:")
    print_table(seg_wr(
        lambda t: f"n_s180={t['n180']}" if t["n180"] <= 8 else "n_s180=9+",
        trades), min_n=3, show_ep=True)

    # A9: el_bid_180
    print("\n  A9. WR por el_bid_180 (força do sinal):")
    print_table(seg_wr(
        lambda t: bucket(t["el180"],
                         [0.70, 0.80, 0.85, 0.90, 999],
                         ["<0.70", "0.70-0.80", "0.80-0.85", "0.85-0.90", "0.90+"]),
        trades), min_n=3, show_ep=True)

    # A10: combinação vel × opp_bid (cross-feature)
    print("\n  A10. WR — combinações vel × opp_at_entry (cross-feature):")
    def cross(t):
        if t["opp_at_entry"] is None:
            return None
        v = "vel-alto" if t["vel"] >= 0.22 else ("vel-med" if t["vel"] >= 0.17 else "vel-baixo")
        o = "opp<0.10" if t["opp_at_entry"] < 0.10 else "opp>=0.10"
        return f"{v} + {o}"
    print_table(seg_wr(cross, trades), min_n=3, show_ep=True)


# --- SECTION B ------------------------------------------------------------

def build_slug_timelines(events):
    """De-duplica snapshots por (slug, ts) e retorna {slug: [snaps sorted by ts]}."""
    slug_ts = defaultdict(dict)
    for ev in events:
        if ev.get("type") != "snapshot":
            continue
        slug = ev.get("current_slug", "")
        if not slug:
            continue
        ts = ev.get("ts", 0)
        if ts not in slug_ts[slug]:
            slug_ts[slug][ts] = ev
    return {
        slug: sorted(ts_map.values(), key=lambda s: s["ts"])
        for slug, ts_map in slug_ts.items()
    }


def resolve_slug(snaps):
    """Retorna 'UP', 'DOWN' ou None a partir dos últimos snapshots."""
    for s in reversed(snaps):
        secs = s.get("current_secs", 999)
        if secs > 10:
            continue
        exec_ = s.get("current_exec", {})
        up = exec_.get("up_bid", 0)
        down = exec_.get("down_bid", 0)
        if up >= 0.90:
            return "UP"
        if down >= 0.90:
            return "DOWN"
    return None


def section_b(events):
    print_sep("B) Simulação de novos setups em toda a história de slugs")

    timelines = build_slug_timelines(events)
    outcomes = {slug: resolve_slug(snaps) for slug, snaps in timelines.items()}
    resolved = {slug: out for slug, out in outcomes.items() if out is not None}
    print(f"\n  Slugs únicos: {len(timelines)}  |  Com resolução capturada: {len(resolved)}")

    entry_slugs = {ev.get("slug", "") for ev in events if ev.get("type") == "ee_paper_entry"}

    def sim_setup(label, condition_fn, min_n=10):
        """
        Varre todos os slugs resolvidos.
        condition_fn(snap) -> ep_candidato ou None.
        Retorna primeiro sinal por slug, computa WR e EV.
        """
        wins, total, ep_sum = 0, 0, 0.0
        for slug, snaps in timelines.items():
            outcome = resolved.get(slug)
            if not outcome:
                continue
            for s in snaps:
                ep = condition_fn(s)
                if ep is None:
                    continue
                _, _, side = get_el_opp(s)
                if side is None:
                    continue
                total += 1
                ep_sum += ep
                if outcome == side:
                    wins += 1
                break  # primeiro sinal por slug

        if total < min_n:
            print(f"\n  {label}")
            print(f"    Slugs qualificados: {total}  (abaixo do mínimo {min_n})")
            return

        wr = wins / total
        ep_avg = ep_sum / total
        ev = ev_per_share(wr, ep_avg)
        ev_10d = ev * QTY * total / max(1, len(resolved)) * 288  # ~288 slugs/dia * 10d
        bar = "#" * int(wr * 20)
        print(f"\n  {label}")
        print(f"    Slugs: {total}  |  WR: {wr*100:.1f}%  |  ep médio: {ep_avg:.3f}"
              f"  |  EV/share: {ev:+.3f}  {bar}")
        print(f"    EV estimado (10 dias, qty={QTY:.0f}): ${ev * QTY * 288 * 10 / len(resolved) * len(resolved):+.1f}"
              f"  ← baseado em {len(resolved)} slugs observados")

    # B1: Janela antecipada — entra antes da janela atual (secs > 155)
    def cond_b1(s):
        secs = s.get("current_secs", 0)
        if secs <= 155:
            return None
        el = s.get("early_leader", {})
        vel = el.get("el_vel", 0)
        el_b, _, _ = get_el_opp(s)
        if el_b is None:
            return None
        if 0.70 <= el_b <= 0.82 and vel >= 0.08:
            return el_b
        return None

    sim_setup("B1. 'Janela antecipada' (secs>155, el_bid 0.70-0.82, vel>=0.08)", cond_b1)

    # B2: Alta dominância precoce (secs>200, el_bid>0.78)
    def cond_b2(s):
        secs = s.get("current_secs", 0)
        if secs <= 200:
            return None
        el_b, _, _ = get_el_opp(s)
        if el_b is None or el_b <= 0.78:
            return None
        return el_b

    sim_setup("B2. 'Dominância precoce alta' (secs>200, el_bid>0.78)", cond_b2)

    # B3: Colapso opp_bid com el_bid ainda barato
    def cond_b3(s):
        secs = s.get("current_secs", 0)
        if not (35 <= secs <= 155):
            return None
        el_b, opp_b, _ = get_el_opp(s)
        if el_b is None:
            return None
        if opp_b < 0.05 and 0.70 <= el_b <= 0.82:
            return el_b
        return None

    sim_setup("B3. 'Colapso opp_bid' (opp<0.05, el_bid 0.70-0.82, secs 35-155)", cond_b3)

    # B4: Plateau pós-spike (el_bid>=0.82, vel<0.05, secs 35-155)
    def cond_b4(s):
        secs = s.get("current_secs", 0)
        if not (35 <= secs <= 155):
            return None
        el = s.get("early_leader", {})
        vel = el.get("el_vel", 0)
        el_b, _, _ = get_el_opp(s)
        if el_b is None:
            return None
        if el_b >= 0.82 and vel < 0.05:
            return el_b
        return None

    sim_setup("B4. 'Plateau pós-spike' (el_bid>=0.82, vel<0.05, secs 35-155)", cond_b4)

    # B5: Creep lento com vel>=0.05 mas <0.13, secs 35-155
    def cond_b5(s):
        secs = s.get("current_secs", 0)
        if not (35 <= secs <= 155):
            return None
        el = s.get("early_leader", {})
        vel = el.get("el_vel", 0)
        n180 = el.get("n_s180", 0)
        el_b, _, _ = get_el_opp(s)
        if el_b is None:
            return None
        if 0.82 <= el_b <= 0.86 and 0.05 <= vel < 0.13 and n180 >= 3:
            return el_b
        return None

    sim_setup("B5. 'Vel lento qualificado' (0.05<=vel<0.13, el_bid 0.82-0.86, n_s180>=3, secs 35-155)", cond_b5)

    # B6: Alta velocidade pura (vel>=0.30) independente de outras condições
    def cond_b6(s):
        secs = s.get("current_secs", 0)
        if not (35 <= secs <= 200):
            return None
        el = s.get("early_leader", {})
        vel = el.get("el_vel", 0)
        el_b, _, _ = get_el_opp(s)
        if el_b is None:
            return None
        if vel >= 0.30 and el_b >= 0.70:
            return el_b
        return None

    sim_setup("B6. 'Velocidade extrema' (vel>=0.30, el_bid>=0.70, secs 35-200)", cond_b6)

    # B7: Setup já existente como baseline (para comparação)
    def cond_b7(s):
        secs = s.get("current_secs", 0)
        if not (35 <= secs <= 155):
            return None
        el = s.get("early_leader", {})
        vel = el.get("el_vel", 0)
        n180 = el.get("n_s180", 0)
        el_b, _, _ = get_el_opp(s)
        if el_b is None:
            return None
        if 0.82 <= el_b <= 0.86 and vel >= 0.13 and n180 >= 3:
            return el_b
        return None

    sim_setup("B7. [BASELINE] Setup atual (vel>=0.13, el_bid 0.82-0.86, n_s180>=3, secs 35-155)", cond_b7)

    # B8: Setup atual mas sem o gate secs<=155 (janela extendida)
    def cond_b8(s):
        secs = s.get("current_secs", 0)
        if not (35 <= secs <= 250):
            return None
        el = s.get("early_leader", {})
        vel = el.get("el_vel", 0)
        n180 = el.get("n_s180", 0)
        el_b, _, _ = get_el_opp(s)
        if el_b is None:
            return None
        if 0.82 <= el_b <= 0.86 and vel >= 0.13 and n180 >= 3:
            return el_b
        return None

    sim_setup("B8. Setup atual SEM gate secs<=155 (secs 35-250)", cond_b8)

    # --- B9: Oportunidades perdidas por razão -----------------------------
    print_sep("B9) Oportunidades perdidas — slugs sem entrada mas com sinal forte")

    missed = defaultdict(lambda: [0, 0, 0.0])
    for slug, snaps in timelines.items():
        if slug in entry_slugs:
            continue
        outcome = resolved.get(slug)
        if not outcome:
            continue
        for s in snaps:
            secs = s.get("current_secs", 0)
            if not (35 <= secs <= 200):
                continue
            el = s.get("early_leader", {})
            vel = el.get("el_vel", 0)
            n180 = el.get("n_s180", 0)
            el_b, _, side = get_el_opp(s)
            if el_b is None or side is None:
                continue
            if el_b < 0.80:
                continue
            win = (outcome == side)

            if secs > 155 and el_b >= 0.82 and vel >= 0.13:
                reason = "bloq: secs>155"
            elif vel < 0.13 and el_b >= 0.82:
                reason = "bloq: vel<0.13"
            elif n180 < 3 and el_b >= 0.82:
                reason = "bloq: n_s180<3"
            elif el_b > 0.86:
                reason = "bloq: ep>0.86 (fora da janela)"
            else:
                reason = "bloq: outro"

            missed[reason][1] += 1
            missed[reason][2] += el_b
            if win:
                missed[reason][0] += 1
            break

    print("  (min_n=5)")
    print_table(missed, min_n=5, show_ep=True)


# --- Main -----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logs", default=DEFAULT_PATTERN,
                        help="Glob dos arquivos ee_paper.jsonl")
    args = parser.parse_args()

    print("Carregando logs...")
    events = load_events(args.logs)
    print(f"Eventos: {len(events):,}")

    section_a(events)
    section_b(events)

    print("\n" + "-"*64)
    print("  Análise concluída.")
    print("-"*64 + "\n")


if __name__ == "__main__":
    main()
