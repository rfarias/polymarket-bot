"""
analyze_entry_threshold_and_gaps.py

Dois blocos de análise sobre todos os logs disponíveis:

BLOCO 1 — COMPRAR MAIS BARATO COM EL ESTÁVEL
  Hipótese: com EL estável (F3, 94% acerto de direção), podemos abaixar o
  entry_min de 0.88 para 0.82-0.86 e compensar perdas com maior lucro por win.
  O filtro EL estável garante a direção; o preço de entrada define o P&L.

BLOCO 2 — BOOK GAPS E QUEDAS BRUSCAS
  Quando o bid do winner cai >=N cents em 1 snap, o que sinaliza antes?
  spread, depth, range_15s, secs, bid naquele momento?
  Objetivo: identificar gate para bloquear entrada quando gap é provável.

Uso:
  python analyze_entry_threshold_and_gaps.py
  python analyze_entry_threshold_and_gaps.py --drop-min 0.10
"""
from __future__ import annotations

import argparse, json, sys, io
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Tuple

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SEP = "─" * 68
QTY = 6.0
TICK = 0.01


# ─── estrutura base (reaproveitada do analyze_early_leader_all_logs) ──────────

@dataclass
class Snap:
    ts:        float
    secs:      int
    up_bid:    float
    down_bid:  float
    d15:       float = 0.0
    depth:     float = 0.0
    spread_up: float = 0.0
    spread_dn: float = 0.0
    range_15s: float = 0.0
    range_30s: float = 0.0


@dataclass
class SlugData:
    key:        str
    resolution: str
    snaps:      List[Snap]
    s240:       List[Snap] = field(default_factory=list)
    s180:       List[Snap] = field(default_factory=list)
    s120:       List[Snap] = field(default_factory=list)
    early_leader:  Optional[str] = None
    early_bid:     float = 0.0
    cont_min_bid:  float = 0.0
    cont_ok:       bool  = False

    def build(self, early_min=0.55, cont_min=0.70):
        self.s240 = [s for s in self.snaps if 181 <= s.secs <= 240]
        self.s180 = [s for s in self.snaps if 121 <= s.secs <= 180]
        self.s120 = [s for s in self.snaps if  61 <= s.secs <= 120]
        if self.s240:
            avg_up = sum(s.up_bid   for s in self.s240) / len(self.s240)
            avg_dn = sum(s.down_bid for s in self.s240) / len(self.s240)
            if avg_up >= early_min:
                self.early_leader = "UP";   self.early_bid = avg_up
            elif avg_dn >= early_min:
                self.early_leader = "DOWN"; self.early_bid = avg_dn
        if self.early_leader and self.s180:
            bids = [s.up_bid if self.early_leader == "UP" else s.down_bid
                    for s in self.s180]
            self.cont_min_bid = min(bids)
            self.cont_ok = self.cont_min_bid >= cont_min
        elif self.early_leader and not self.s180:
            self.cont_ok = True


def _to_snap(d: dict, is_monitor: bool) -> Tuple[Optional[str], Optional[Snap]]:
    if is_monitor:
        if d.get("type") != "monitor_snap" or d.get("slot") != "current":
            return None, None
        secs = d.get("secs")
        if secs is None:
            return None, None
        return d.get("slug"), Snap(
            ts=d["ts"], secs=secs,
            up_bid=d.get("up_bid", 0.0), down_bid=d.get("down_bid", 0.0),
            d15=d.get("sc_d15") or 0.0,
        )
    else:
        if d.get("type") != "snapshot":
            return None, None
        sc = d.get("current_scalp_context") or d.get("scalp_context") or {}
        secs = sc.get("secs_to_end") or d.get("secs_to_end") or d.get("current_secs")
        ub = sc.get("up_bid"); db = sc.get("down_bid")
        if ub is None:
            lb = d.get("leader_buy"); cb = d.get("counter_buy")
            side = (d.get("guardian") or {}).get("side") or \
                   (d.get("almost_resolved_signal") or {}).get("side")
            if lb is not None and cb is not None and side:
                ub = lb if side == "UP" else cb
                db = cb if side == "UP" else lb
        slug = d.get("slug") or d.get("current_slug")
        if secs is None or ub is None or db is None or not slug:
            return None, None
        return slug, Snap(
            ts=d["ts"], secs=int(secs),
            up_bid=float(ub), down_bid=float(db),
            d15=sc.get("spot_delta_15s_bps") or 0.0,
            depth=sc.get("combined_bid_depth_top3") or 0.0,
            spread_up=sc.get("spread_up") or 0.0,
            spread_dn=sc.get("spread_down") or 0.0,
            range_15s=sc.get("market_range_15s") or 0.0,
            range_30s=sc.get("market_range_30s") or 0.0,
        )


def load_all(log_dir: Path, early_min=0.55, cont_min=0.70) -> List[SlugData]:
    result: List[SlugData] = []
    for log_path in sorted(log_dir.glob("*.jsonl")):
        is_monitor = "market_monitor" in log_path.name
        raw: Dict[str, List[Snap]] = defaultdict(list)
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                slug, snap = _to_snap(d, is_monitor)
                if snap:
                    raw[slug].append(snap)
        for slug, snaps in raw.items():
            snaps.sort(key=lambda s: s.ts)
            last = snaps[-1]
            if last.secs <= 40:
                res = ("UP"   if last.up_bid >= 0.85 else
                       "DOWN" if last.down_bid >= 0.85 else "UNKNOWN")
            else:
                res = "UNKNOWN"
            sd = SlugData(key=f"{log_path.stem[:18]}:{slug}", resolution=res, snaps=snaps)
            sd.build(early_min, cont_min)
            result.append(sd)
    return result


# ─── BLOCO 1: entry threshold + EL estável ───────────────────────────────────

def simulate_ar(
    sd: SlugData,
    entry_min: float, entry_secs: int,
    stop_ticks: float,
    require_el_stable: bool = False,
) -> Optional[dict]:
    """Simula trade AR no slug. Retorna dict com resultado ou None."""
    if require_el_stable and not (sd.early_leader and sd.cont_ok):
        return None
    if require_el_stable and sd.early_leader != sd.resolution and sd.resolution != "UNKNOWN":
        pass  # deixar passar; o filtro EL estável não bloqueia — analisa o acerto

    entry = None
    for s in sd.snaps:
        if s.secs > entry_secs:
            continue
        wb = s.up_bid if sd.resolution == "UP" else s.down_bid
        # sem winner_side no log passivo → usar resolution como proxy
        # (simulação ideal — em produção usa winner_side em tempo real)
        winner_bid = s.up_bid if s.up_bid > s.down_bid else s.down_bid
        if winner_bid >= entry_min:
            # inferir side pelo maior bid
            side = "UP" if s.up_bid >= s.down_bid else "DOWN"
            ep = winner_bid
            entry = s; entry_side = side
            break
    if not entry:
        return None

    sp = round(ep - stop_ticks * TICK, 4)
    post = [s for s in sd.snaps if s.ts > entry.ts]
    if not post:
        return None

    gap = 0; outcome = None; exit_b = ep
    for s in post:
        wb = s.up_bid if entry_side == "UP" else s.down_bid
        if wb <= 0.0:
            gap += 1
            if gap >= 2:
                outcome = "BOOK_GAP"; exit_b = 0.0; break
        else:
            gap = 0
        if wb <= sp:
            outcome = "STOP"; exit_b = wb; break

    if outcome is None:
        last = post[-1]
        lw = last.up_bid if entry_side == "UP" else last.down_bid
        if last.secs <= 35:
            outcome = "WIN" if lw >= 0.85 else "REVERSAL"
            exit_b = 1.0 if outcome == "WIN" else lw
        else:
            return None  # não resolvido

    pnl = round((exit_b - ep) * QTY, 4)
    dir_ok = (entry_side == sd.resolution) if sd.resolution != "UNKNOWN" else None
    return {
        "outcome": outcome, "pnl": pnl, "ep": ep, "exit_b": exit_b,
        "side": entry_side, "resolution": sd.resolution,
        "dir_ok": dir_ok,
        "el_stable": sd.early_leader is not None and sd.cont_ok,
        "el_side": sd.early_leader,
        "entry_secs": entry.secs,
    }


def report_threshold_sensitivity(slugs: List[SlugData]):
    print(f"\n  {'Filtro':22} {'ep>=':>5} {'n':>5} {'WR%':>5} "
          f"{'PnL':>9} {'avg':>8} {'min_exit':>8}")
    print(f"  {'─'*22} {'─'*5} {'─'*5} {'─'*5} {'─'*9} {'─'*8} {'─'*8}")

    for label, use_el in [("Baseline", False), ("EL estável (F3)", True)]:
        for ep_min in [0.82, 0.84, 0.86, 0.88, 0.90, 0.92]:
            trades = []
            for sd in slugs:
                if sd.resolution == "UNKNOWN":
                    continue
                t = simulate_ar(sd, entry_min=ep_min, entry_secs=120,
                                stop_ticks=2, require_el_stable=use_el)
                if t:
                    trades.append(t)
            if not trades:
                continue
            wins  = [t for t in trades if t["outcome"] == "WIN"]
            stops = [t for t in trades if t["outcome"] == "STOP"]
            pnl   = sum(t["pnl"] for t in trades)
            wr    = 100 * len(wins) / len(trades)
            exits = [t["exit_b"] for t in stops]
            min_e = min(exits) if exits else 0.0
            first = f"{label:22}" if ep_min == 0.82 else f"{'':22}"
            print(f"  {first} {ep_min:>5.2f} {len(trades):>5} {wr:>4.1f}% "
                  f"${pnl:>+8.2f} ${pnl/len(trades):>+7.4f} {min_e:>8.3f}")
        print()


def report_dir_accuracy_vs_ep(slugs: List[SlugData]):
    """Acerto de direção (independente do stop) por faixa de entry_price."""
    print(f"\n  {'Subset':22} {'ep faixa':>12} {'n':>5} {'dir_ok%':>8} {'pnl_sem_stop':>12}")
    print(f"  {'─'*22} {'─'*12} {'─'*5} {'─'*8} {'─'*12}")
    for label, el_filter in [("Todos AR", False), ("EL estável (F3)", True)]:
        for lo, hi in [(0.82, 0.86), (0.86, 0.90), (0.90, 0.94), (0.94, 1.00)]:
            sub = []
            for sd in slugs:
                if sd.resolution == "UNKNOWN": continue
                if el_filter and not (sd.early_leader and sd.cont_ok): continue
                for s in sd.snaps:
                    if s.secs > 120: continue
                    wb = s.up_bid if s.up_bid >= s.down_bid else s.down_bid
                    if lo <= wb < hi:
                        side = "UP" if s.up_bid >= s.down_bid else "DOWN"
                        dir_ok = side == sd.resolution
                        # PnL sem stop: win=(1-ep)*6, loss=(-ep)*6
                        pnl_ns = (1.0 - wb) * QTY if dir_ok else -wb * QTY
                        sub.append({"dir_ok": dir_ok, "ep": wb, "pnl": pnl_ns})
                        break
            if not sub: continue
            ok  = sum(1 for t in sub if t["dir_ok"])
            pnl = sum(t["pnl"] for t in sub)
            first = f"{label:22}" if lo == 0.82 else f"{'':22}"
            print(f"  {first} {lo:.2f}–{hi:.2f}     {len(sub):>5} "
                  f"{100*ok/len(sub):>7.1f}% ${pnl:>+11.2f}")
        print()


# ─── BLOCO 2: book gaps e quedas bruscas ──────────────────────────────────────

@dataclass
class DropEvent:
    slug:       str
    secs:       int
    wb_before:  float
    wb_after:   float
    drop:       float
    resolution: str
    was_winner: bool      # True = o bid que caiu era do winner
    depth_before: float
    spread_before: float
    range15_before: float
    # contexto 30s antes da queda
    wb_30s_ago: float = 0.0
    vel_30s:    float = 0.0   # variação do bid nos 30s antes


def find_drops(slugs: List[SlugData], drop_min: float = 0.10) -> List[DropEvent]:
    events: List[DropEvent] = []
    for sd in slugs:
        if sd.resolution == "UNKNOWN":
            continue
        snaps = sd.snaps
        for i in range(1, len(snaps)):
            prev = snaps[i-1]; cur = snaps[i]
            for side, wb_prev, wb_cur in [
                ("UP",   prev.up_bid,   cur.up_bid),
                ("DOWN", prev.down_bid, cur.down_bid),
            ]:
                if wb_prev < 0.60: continue   # ignorar quando bid já estava baixo
                drop = wb_prev - wb_cur
                if drop < drop_min: continue
                # contexto 30s antes
                wb_30s = wb_prev
                vel_30 = 0.0
                for j in range(i-1, -1, -1):
                    if prev.ts - snaps[j].ts >= 30:
                        wb_30s = snaps[j].up_bid if side == "UP" else snaps[j].down_bid
                        vel_30 = (wb_prev - wb_30s) / 30.0
                        break
                events.append(DropEvent(
                    slug=sd.key, secs=cur.secs,
                    wb_before=wb_prev, wb_after=wb_cur, drop=drop,
                    resolution=sd.resolution,
                    was_winner=(side == sd.resolution),
                    depth_before=prev.depth,
                    spread_before=prev.spread_up if side == "UP" else prev.spread_dn,
                    range15_before=prev.range_15s,
                    wb_30s_ago=wb_30s, vel_30s=vel_30,
                ))
    return events


def report_drops(events: List[DropEvent]):
    if not events:
        print("  Nenhum evento de queda encontrado.")
        return

    # split: queda do winner vs queda do loser
    w_drops = [e for e in events if e.was_winner]
    l_drops = [e for e in events if not e.was_winner]

    print(f"\n  Total de quedas bruscas detectadas: {len(events)}")
    print(f"  Queda do WINNER bid: {len(w_drops)}   |   Queda do LOSER bid: {len(l_drops)}")
    print()

    for label, evts in [("WINNER bid caiu (stop/gap para quem entrou)", w_drops),
                        ("LOSER bid caiu (normal: market resolve)", l_drops)]:
        if not evts:
            continue
        print(f"  {label}  (n={len(evts)})")
        drops = [e.drop for e in evts]
        secs_v = [e.secs for e in evts]
        ranges = [e.range15_before for e in evts if e.range15_before > 0]
        depths = [e.depth_before for e in evts if e.depth_before > 0]
        spreads= [e.spread_before for e in evts if e.spread_before > 0]
        vels   = [e.vel_30s for e in evts if e.vel_30s != 0]
        print(f"    drop avg={sum(drops)/len(drops):.3f}  "
              f"max={max(drops):.3f}  p75={sorted(drops)[int(len(drops)*0.75)]:.3f}")
        print(f"    secs avg={sum(secs_v)/len(secs_v):.0f}  "
              f"min={min(secs_v)}  p25={sorted(secs_v)[int(len(secs_v)*0.25)]}")
        if ranges:
            print(f"    range_15s_antes avg={sum(ranges)/len(ranges):.4f}  "
                  f"max={max(ranges):.4f}")
        if depths:
            print(f"    depth_antes avg={sum(depths)/len(depths):.0f}  "
                  f"min={min(depths):.0f}")
        if spreads:
            print(f"    spread_antes avg={sum(spreads)/len(spreads):.4f}")
        if vels:
            pos_v = [v for v in vels if v > 0]
            neg_v = [v for v in vels if v < 0]
            print(f"    vel_30s_antes avg={sum(vels)/len(vels):+.4f}  "
                  f"positivo(subindo):{len(pos_v)}  negativo(caindo):{len(neg_v)}")
        print()

    # tabela por faixa de secs
    print(f"  Quedas do WINNER por faixa de secs (potencial gate):")
    print(f"  {'secs':>8}  {'n':>4}  {'drop_avg':>9}  {'range15_avg':>12}  {'vel30_avg':>10}")
    for lo, hi in [(1,30),(31,60),(61,90),(91,120),(121,180),(181,240)]:
        sub = [e for e in w_drops if lo <= e.secs <= hi]
        if not sub: continue
        drops = [e.drop for e in sub]
        rng   = [e.range15_before for e in sub if e.range15_before > 0]
        vels  = [e.vel_30s for e in sub]
        r_avg = sum(rng)/len(rng) if rng else 0
        v_avg = sum(vels)/len(vels) if vels else 0
        print(f"  {lo:>4}-{hi:<3}  {len(sub):>4}  {sum(drops)/len(drops):>9.3f}  "
              f"{r_avg:>12.4f}  {v_avg:>+10.4f}")

    # gate potencial: range_15s alto antes da queda
    print()
    print(f"  Teste de gate: range_15s >= 0.03 antes da queda do WINNER:")
    r_high = [e for e in w_drops if e.range15_before >= 0.03]
    r_low  = [e for e in w_drops if e.range15_before > 0 and e.range15_before < 0.03]
    r_zero = [e for e in w_drops if e.range15_before == 0]
    print(f"    range_15s >= 0.03 : {len(r_high)} quedas  "
          f"({100*len(r_high)/len(w_drops):.0f}% do total)")
    print(f"    range_15s < 0.03  : {len(r_low)} quedas")
    print(f"    range_15s = 0     : {len(r_zero)} quedas (campo ausente no log)")


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--early-min", type=float, default=0.55)
    ap.add_argument("--cont-min",  type=float, default=0.70)
    ap.add_argument("--drop-min",  type=float, default=0.10,
                    help="queda mínima em 1 snap para contar como drop (padrão 0.10)")
    ap.add_argument("--log-dir",   type=Path,  default=Path("logs"))
    args = ap.parse_args()

    print(f"\nCarregando logs de {args.log_dir}/ ...")
    slugs = load_all(args.log_dir, args.early_min, args.cont_min)
    resolved = [s for s in slugs if s.resolution != "UNKNOWN"]
    el_stable = [s for s in resolved if s.early_leader and s.cont_ok]
    print(f"Total slugs: {len(slugs)}  |  Resolvidos: {len(resolved)}  "
          f"|  EL estável (F3): {len(el_stable)}")

    # ── BLOCO 1 ────────────────────────────────────────────────────────────────
    print()
    print("=" * 68)
    print("  BLOCO 1 — ENTRY THRESHOLD vs EL ESTÁVEL")
    print("=" * 68)
    print()
    print("  Hipótese: EL estável garante direção (94%). Podemos entrar mais")
    print("  barato (ep 0.82-0.86) e ganhar mais por win sem perder acerto?")
    print()
    print(SEP)
    print("  SENSIBILIDADE: threshold de entrada x filtro EL estável (stop=2T)")
    print(SEP)
    report_threshold_sensitivity(slugs)

    print(SEP)
    print("  ACERTO DE DIREÇÃO por faixa de entry_price (sem stop — só direção)")
    print(SEP)
    report_dir_accuracy_vs_ep(slugs)

    # ── BLOCO 2 ────────────────────────────────────────────────────────────────
    print()
    print("=" * 68)
    print("  BLOCO 2 — BOOK GAPS E QUEDAS BRUSCAS")
    print("=" * 68)
    print()
    print(f"  Detectando quedas de bid >= {args.drop_min:.2f} em 1 snap ...")
    print()
    events = find_drops(slugs, args.drop_min)
    report_drops(events)

    # ── RESUMO ────────────────────────────────────────────────────────────────
    print()
    print("=" * 68)
    print("  RESUMO DAS CONCLUSÕES")
    print("=" * 68)
    print()
    print("  [BLOCO 1] Resultado:")
    print("   Verificar se EL estável + ep_min=0.84-0.86 supera baseline 0.88")
    print("   Comparar: wins maiores compensam stops maiores em trades errados?")
    print()
    print("  [BLOCO 2] Gate de book gap:")
    print("   Verificar se range_15s alto antes da queda é gate confiável")
    print("   Se range_15s >= 0.03 coincide com maioria das quedas do winner,")
    print("   bloquear entradas quando range_15s >= 0.03 nos últimos 15s.")


if __name__ == "__main__":
    main()
