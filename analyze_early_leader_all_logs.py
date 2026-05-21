"""
analyze_early_leader_all_logs.py

Analisa o early leader em TODOS os logs disponíveis (monitor + runner).
Adapta automaticamente entre formatos diferentes de log.

Análises:
  1. Poder preditivo do early leader por threshold e janela de tempo
  2. Early leader com F3 (continuidade) — acerto e PnL implícito
  3. REVERSÃO DO EL: quando o líder troca de lado em determinada janela,
     qual a probabilidade de reversão na resolução?
  4. Intensidade da inversão: líder fraco que inverte vs líder forte que inverte

Uso:
  python analyze_early_leader_all_logs.py
  python analyze_early_leader_all_logs.py --early-min 0.55 --cont-min 0.70
"""
from __future__ import annotations

import argparse, json, sys, io
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SEP = "─" * 66


# ─── estrutura de slug ────────────────────────────────────────────────────────

@dataclass
class Snap:
    ts:       float
    secs:     int
    up_bid:   float
    down_bid: float
    d15:      float = 0.0   # spot_delta_15s_bps (se disponível)


@dataclass
class SlugData:
    key:        str          # "arquivo:slug"
    resolution: str          # UP | DOWN | UNKNOWN
    snaps:      List[Snap]   # ordenados por ts

    # janelas pré-computadas
    s240: List[Snap] = field(default_factory=list)  # 181-240s
    s180: List[Snap] = field(default_factory=list)  # 121-180s
    s120: List[Snap] = field(default_factory=list)  # 61-120s
    s60:  List[Snap] = field(default_factory=list)  # 31-60s

    # early leader
    early_leader:  Optional[str] = None
    early_bid:     float = 0.0
    early_side_history: List[tuple] = field(default_factory=list)  # (secs_bucket, side, bid)

    # F3 continuidade
    cont_min_bid: float = 0.0
    cont_ok:      bool  = False

    def build_windows(self):
        self.s240 = [s for s in self.snaps if 181 <= s.secs <= 240]
        self.s180 = [s for s in self.snaps if 121 <= s.secs <= 180]
        self.s120 = [s for s in self.snaps if  61 <= s.secs <= 120]
        self.s60  = [s for s in self.snaps if  31 <= s.secs <=  60]

    def compute_filters(self, early_min: float = 0.55, cont_min: float = 0.70):
        self.build_windows()

        # F1: early leader (média de up_bid vs down_bid em 240-181s)
        if self.s240:
            avg_up = sum(s.up_bid   for s in self.s240) / len(self.s240)
            avg_dn = sum(s.down_bid for s in self.s240) / len(self.s240)
            if avg_up >= early_min:
                self.early_leader = "UP";   self.early_bid = avg_up
            elif avg_dn >= early_min:
                self.early_leader = "DOWN"; self.early_bid = avg_dn

        # F3: continuidade (early_leader mantém bid >= cont_min em 180-121s)
        if self.early_leader and self.s180:
            bids = [s.up_bid if self.early_leader == "UP" else s.down_bid
                    for s in self.s180]
            self.cont_min_bid = min(bids)
            self.cont_ok = self.cont_min_bid >= cont_min
        elif self.early_leader and not self.s180:
            self.cont_ok = True   # sem dados → não penalizar

        # histórico do líder por bucket de 30s (para análise de inversão)
        buckets = [(181, 240), (121, 180), (61, 120), (31, 60), (1, 30)]
        for lo, hi in buckets:
            w = [s for s in self.snaps if lo <= s.secs <= hi]
            if not w:
                continue
            avg_up = sum(s.up_bid   for s in w) / len(w)
            avg_dn = sum(s.down_bid for s in w) / len(w)
            if avg_up >= 0.52:
                self.early_side_history.append((hi, "UP", avg_up))
            elif avg_dn >= 0.52:
                self.early_side_history.append((hi, "DOWN", avg_dn))
            else:
                self.early_side_history.append((hi, None, max(avg_up, avg_dn)))


# ─── loaders ──────────────────────────────────────────────────────────────────

def _snap_from_monitor(d: dict) -> Optional[Snap]:
    """Formato: market_monitor_*.jsonl  →  type=monitor_snap"""
    if d.get("type") != "monitor_snap" or d.get("slot") != "current":
        return None
    secs = d.get("secs")
    if secs is None:
        return None
    return Snap(
        ts=d["ts"], secs=secs,
        up_bid=d.get("up_bid", 0.0),
        down_bid=d.get("down_bid", 0.0),
        d15=d.get("sc_d15") or 0.0,
    )


def _snap_from_runner(d: dict) -> Optional[Snap]:
    """Formato: runner/guardian logs  →  type=snapshot"""
    if d.get("type") != "snapshot":
        return None
    sc = d.get("current_scalp_context") or d.get("scalp_context") or {}
    secs = sc.get("secs_to_end") or d.get("secs_to_end") or d.get("current_secs")
    up_bid  = sc.get("up_bid")
    down_bid = sc.get("down_bid")
    # fallback para guardian que usa leader_buy/counter_buy
    if up_bid is None:
        lb = d.get("leader_buy"); cb = d.get("counter_buy")
        side = (d.get("guardian") or {}).get("side") or \
               (d.get("almost_resolved_signal") or {}).get("side")
        if lb is not None and cb is not None and side:
            up_bid   = lb if side == "UP"   else cb
            down_bid = cb if side == "UP"   else lb
    if secs is None or up_bid is None or down_bid is None:
        return None
    return Snap(
        ts=d["ts"], secs=int(secs),
        up_bid=float(up_bid),
        down_bid=float(down_bid),
        d15=sc.get("spot_delta_15s_bps") or 0.0,
    )


def load_all_logs(log_dir: Path, early_min: float, cont_min: float) -> List[SlugData]:
    log_files = sorted(log_dir.glob("*.jsonl"))
    all_slugs: List[SlugData] = []

    for log_path in log_files:
        raw: Dict[str, List[Snap]] = defaultdict(list)
        is_monitor = "market_monitor" in log_path.name

        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                snap = _snap_from_monitor(d) if is_monitor else _snap_from_runner(d)
                if snap is None:
                    continue
                slug = d.get("slug") or d.get("current_slug") or ""
                if slug:
                    raw[slug].append(snap)

        for slug, snaps in raw.items():
            snaps.sort(key=lambda s: s.ts)
            last = snaps[-1]
            if last.secs <= 40:
                if last.up_bid >= 0.85:
                    resolution = "UP"
                elif last.down_bid >= 0.85:
                    resolution = "DOWN"
                else:
                    resolution = "UNKNOWN"
            else:
                resolution = "UNKNOWN"

            sd = SlugData(
                key=f"{log_path.stem[:20]}:{slug}",
                resolution=resolution,
                snaps=snaps,
            )
            sd.compute_filters(early_min, cont_min)
            all_slugs.append(sd)

    return all_slugs


# ─── análises ─────────────────────────────────────────────────────────────────

def report_predictivity(slugs: List[SlugData]):
    resolved = [s for s in slugs if s.resolution != "UNKNOWN"]
    has_early = [s for s in resolved if s.early_leader]
    no_early  = [s for s in resolved if not s.early_leader]

    print(f"\n  Total resolvidos: {len(resolved)}")
    print(f"  Com early_leader: {len(has_early)} ({100*len(has_early)/len(resolved):.0f}%)")
    print(f"  Sem early_leader: {len(no_early)}")

    correct = [s for s in has_early if s.early_leader == s.resolution]
    wrong   = [s for s in has_early if s.early_leader != s.resolution]
    print(f"\n  Acerto direto: {len(correct)}/{len(has_early)} = "
          f"{100*len(correct)/len(has_early):.1f}%")
    print(f"  Erro direção:  {len(wrong)}/{len(has_early)} = "
          f"{100*len(wrong)/len(has_early):.1f}%")

    print(f"\n  {'early_bid>=':>12} {'n':>5} {'acerto%':>8} {'PnL implicito':>15} {'avg/slug':>9}")
    print(f"  {'─'*12} {'─'*5} {'─'*8} {'─'*15} {'─'*9}")
    for thr in [0.52, 0.55, 0.58, 0.60, 0.63, 0.65, 0.70]:
        sub = [s for s in has_early if s.early_bid >= thr]
        if not sub:
            continue
        ok = sum(1 for s in sub if s.early_leader == s.resolution)
        pct = 100 * ok / len(sub)
        # PnL implícito: compra líder a early_bid, hold to resolution (qty=6, sem stop)
        pnl = sum((1.0 - s.early_bid) * 6 if s.early_leader == s.resolution
                  else -s.early_bid * 6 for s in sub)
        print(f"  {thr:>12.2f} {len(sub):>5} {pct:>7.1f}% ${pnl:>+14.2f} ${pnl/len(sub):>+8.3f}")

    # F3 continuidade
    cont = [s for s in has_early if s.cont_ok]
    if cont:
        ok_c = sum(1 for s in cont if s.early_leader == s.resolution)
        pnl_c = sum((1.0 - s.early_bid) * 6 if s.early_leader == s.resolution
                    else -s.early_bid * 6 for s in cont)
        print(f"\n  Com F3 (continuidade >= cont_min em 180-121s):")
        print(f"    n={len(cont)}  acerto={ok_c}/{len(cont)} "
              f"({100*ok_c/len(cont):.1f}%)  PnL=${pnl_c:+.2f}  avg=${pnl_c/len(cont):+.3f}")


def report_el_reversal(slugs: List[SlugData]):
    """Analisa quando o EL muda de lado em determinada janela de tempo."""
    resolved = [s for s in slugs if s.resolution != "UNKNOWN" and s.early_leader]

    # Para cada slug: detectar se o líder da janela 240-181 é diferente
    # da liderança em janelas posteriores (180-121, 120-61)
    cases_flipped_at_180 = []  # inverte na janela 180-121s
    cases_flipped_at_120 = []  # inverte na janela 120-61s
    cases_stable = []           # mantém o mesmo lado até 60s
    cases_partial = []          # sem dados suficientes

    for sd in resolved:
        hist = sd.early_side_history  # [(hi_secs, side, bid), ...]
        # separar por janela
        w = {hi: (side, bid) for hi, side, bid in hist}
        el = sd.early_leader  # lado em 240-181s

        w240 = w.get(240)
        w180 = w.get(180)
        w120 = w.get(120)

        if not w240:
            cases_partial.append(sd)
            continue

        # verificar inversão em 180-121s
        if w180 and w180[0] is not None and w180[0] != el:
            cases_flipped_at_180.append((sd, "at_180", w180[1]))
        # verificar inversão em 120-61s (mas não em 180)
        elif w120 and w120[0] is not None and w120[0] != el:
            cases_flipped_at_120.append((sd, "at_120", w120[1]))
        else:
            cases_stable.append(sd)

    def summarize(cases, label, extract_bid=None):
        if not cases:
            print(f"  {label}: sem casos")
            return
        slugs_here = [c[0] if isinstance(c, tuple) else c for c in cases]
        correct = [s for s in slugs_here if s.early_leader == s.resolution]
        wrong   = [s for s in slugs_here if s.early_leader != s.resolution]
        wr = 100 * len(correct) / len(slugs_here)
        # PnL implícito comprando o early_leader
        pnl = sum((1.0 - s.early_bid) * 6 if s.early_leader == s.resolution
                  else -s.early_bid * 6 for s in slugs_here)
        print(f"  {label}")
        print(f"    n={len(slugs_here)}  acerto_EL={len(correct)}/{len(slugs_here)} ({wr:.1f}%)"
              f"  PnL_implicito=${pnl:+.2f}")
        if wrong:
            print(f"    → Quando EL inverte: {len(wrong)} vezes o LADO OPOSTO ao EL resolveu")
            # sugerir: comprar o lado oposto ao EL quando ele inverte
            pnl_anti = sum((1.0 - (1.0 - s.early_bid)) * 6 if s.early_leader != s.resolution
                           else -(1.0 - s.early_bid) * 6 for s in slugs_here)
            # simplificado: comprar contra o EL quando detectamos inversão
            # entrada ≈ 0.5 (mercado neutro nesse ponto)
            print(f"    Sugestão — comprar lado OPOSTO ao EL quando inversão detectada:")
            n_rev = len(wrong)
            n_hold = len(correct)
            print(f"      Se inverte em 180s: {n_rev} trocam vencedor, {n_hold} EL resistiu")

    print(f"\n  Total com early_leader e resolução: {len(resolved)}")
    summarize(cases_stable,          "EL ESTÁVEL (mantém lado até 60s ou menos)")
    print()
    summarize(cases_flipped_at_180,  "EL INVERTE em 180-121s (novo líder aparece)")
    print()
    summarize(cases_flipped_at_120,  "EL INVERTE em 120-61s (novo líder aparece)")
    print()
    if cases_partial:
        print(f"  Sem dados suficientes nas janelas: {len(cases_partial)}")

    # análise detalhada da inversão: bid do EL no momento da inversão
    print()
    print(f"  DETALHE — bid do EL no momento da inversão:")
    print(f"  (bid alto = EL estava forte e inverteu → mais raro, mais confiável?)")
    all_flipped = cases_flipped_at_180 + cases_flipped_at_120
    if not all_flipped:
        print("  sem inversões detectadas")
        return

    low  = [(sd, bid) for sd, _, bid in all_flipped if bid < 0.60]
    mid  = [(sd, bid) for sd, _, bid in all_flipped if 0.60 <= bid < 0.72]
    high = [(sd, bid) for sd, _, bid in all_flipped if bid >= 0.72]

    for label2, group in [("<0.60 (EL fraco inverteu)", low),
                          ("0.60-0.72 (EL médio inverteu)", mid),
                          (">=0.72 (EL forte inverteu!)", high)]:
        if not group:
            continue
        sds = [g[0] for g in group]
        ok  = sum(1 for s in sds if s.early_leader == s.resolution)
        print(f"    {label2}: n={len(sds)}  EL_correto={ok}/{len(sds)} "
              f"({100*ok/len(sds):.0f}%)")
        if len(sds) - ok > 0:
            print(f"      → {len(sds)-ok} casos em que o LADO OPOSTO venceu "
                  f"(possível sinal de reversão)")


def report_by_source(slugs: List[SlugData]):
    """Resumo por arquivo de log."""
    from collections import Counter
    sources: Dict[str, List[SlugData]] = defaultdict(list)
    for s in slugs:
        src = s.key.split(":")[0]
        sources[src].append(s)

    print(f"\n  {'Fonte':40} {'total':>6} {'resolvidos':>10} {'c/early':>8} {'acerto%':>8}")
    print(f"  {'─'*40} {'─'*6} {'─'*10} {'─'*8} {'─'*8}")
    total_r = 0; total_e = 0; total_ok = 0; total_n = 0
    for src, slist in sorted(sources.items()):
        res = [s for s in slist if s.resolution != "UNKNOWN"]
        has = [s for s in res if s.early_leader]
        ok  = sum(1 for s in has if s.early_leader == s.resolution)
        pct = f"{100*ok/len(has):.0f}%" if has else "—"
        total_r += len(res); total_e += len(has); total_ok += ok; total_n += len(slist)
        print(f"  {src:40} {len(slist):>6} {len(res):>10} {len(has):>8} {pct:>8}")
    pct_tot = f"{100*total_ok/total_e:.0f}%" if total_e else "—"
    print(f"  {'TOTAL':40} {total_n:>6} {total_r:>10} {total_e:>8} {pct_tot:>8}")


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--early-min", type=float, default=0.55)
    ap.add_argument("--cont-min",  type=float, default=0.70)
    ap.add_argument("--log-dir",   type=Path,  default=Path("logs"))
    args = ap.parse_args()

    print(f"\nCarregando logs de {args.log_dir}/ ...")
    slugs = load_all_logs(args.log_dir, args.early_min, args.cont_min)
    print(f"Total de slugs carregados: {len(slugs)}")

    print()
    print("=" * 66)
    print("  RESUMO POR ARQUIVO DE LOG")
    print("=" * 66)
    report_by_source(slugs)

    print()
    print("=" * 66)
    print("  EARLY LEADER — PODER PREDITIVO (todos os logs)")
    print("=" * 66)
    report_predictivity(slugs)

    print()
    print("=" * 66)
    print("  INVERSÃO DO EARLY LEADER — SINAL DE REVERSÃO?")
    print("=" * 66)
    report_el_reversal(slugs)

    print()
    print(SEP)
    print("  CONCLUSÕES")
    print(SEP)
    resolved = [s for s in slugs if s.resolution != "UNKNOWN"]
    has_early = [s for s in resolved if s.early_leader]
    correct   = sum(1 for s in has_early if s.early_leader == s.resolution)
    cont      = [s for s in has_early if s.cont_ok]
    ok_cont   = sum(1 for s in cont if s.early_leader == s.resolution)
    print(f"\n  EL baseline:       {correct}/{len(has_early)} = "
          f"{100*correct/len(has_early):.1f}% correto")
    if cont:
        print(f"  EL + continuidade: {ok_cont}/{len(cont)} = "
              f"{100*ok_cont/len(cont):.1f}% correto  (F3)")
    print()


if __name__ == "__main__":
    main()
