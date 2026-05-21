"""
analyze_monitor_pnl.py

Simula trades almost_resolved nos logs do run_market_monitor.py.

Filtros híbridos (derivados da análise dos logs de 13.8h):

  F1 — early_leader: em secs 240-181, algum lado já liderava com bid > 0.55
  F2 — confirma:     early_leader é o mesmo side do sinal AR
  F3 — continuidade: early_leader nunca caiu abaixo de 0.70 durante secs 180-121
                     (indica convicção crescente, não oscilação)
  F4 — spot_ok:      spot_d15 no momento da entrada NÃO contradiz o winner
                     (d15 >= 0 para winner=UP, ou d15 <= 0 para winner=DOWN, ou d15~0)

Uso:
  python analyze_monitor_pnl.py
  python analyze_monitor_pnl.py --log logs/market_monitor_20260520_171127.jsonl
  python analyze_monitor_pnl.py --all-logs          # combina todos os arquivos logs/
  python analyze_monitor_pnl.py --qty 6 --stop-ticks 2 --show-trades
  python analyze_monitor_pnl.py --early-min 0.55 --cont-min 0.70
"""
from __future__ import annotations

import argparse, json, sys, io
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


# ─── data model ─────────────────────────────────────────────────────────────

@dataclass
class SlugInfo:
    slug:       str
    resolution: str          # UP | DOWN | UNKNOWN
    all_cur:    list         # todos os snaps do slot current
    s240:       list         # secs 181-240
    s180:       list         # secs 121-180
    s120:       list         # secs 61-120
    s60:        list         # secs 31-60

    # filtros pré-computados
    early_leader:  Optional[str] = None   # UP | DOWN | None
    early_bid:     float = 0.0
    cont_min_bid:  float = 0.0            # mínimo do early_leader em 180-121s
    cont_ok:       bool  = False

    def compute_filters(self, early_min: float = 0.55, cont_min: float = 0.70):
        # F1: early leader
        if self.s240:
            up_avg = sum(s["up_bid"]   for s in self.s240) / len(self.s240)
            dn_avg = sum(s["down_bid"] for s in self.s240) / len(self.s240)
            if up_avg >= early_min:
                self.early_leader = "UP";   self.early_bid = up_avg
            elif dn_avg >= early_min:
                self.early_leader = "DOWN"; self.early_bid = dn_avg

        # F3: continuidade (early_leader nunca caiu abaixo de cont_min em 180-121s)
        if self.early_leader and self.s180:
            bids_180 = [
                s["up_bid"] if self.early_leader == "UP" else s["down_bid"]
                for s in self.s180
            ]
            self.cont_min_bid = min(bids_180)
            self.cont_ok = self.cont_min_bid >= cont_min
        elif self.early_leader and not self.s180:
            self.cont_ok = True   # sem dados no período → não penalizar


@dataclass
class Trade:
    slug:       str
    entry_ts:   float
    side:       str
    ep:         float
    entry_secs: int
    stop_price: float
    outcome:    str = ""
    exit_bid:   float = 0.0
    exit_secs:  Optional[int] = None
    pnl:        float = 0.0
    worst_bid:  float = 0.0
    # flags de filtro
    f1_has_early: bool = False
    f2_confirms:  bool = False
    f3_cont:      bool = False
    f4_spot:      bool = False
    d15_at_entry: float = 0.0


# ─── loader ─────────────────────────────────────────────────────────────────

def load_slugs(log_path: Path) -> List[SlugInfo]:
    return load_slugs_multi([log_path])


def load_slugs_multi(log_paths: List[Path]) -> List[SlugInfo]:
    # key = (log_path.name, slug) so same slug across sessions is kept separate
    rows: Dict[tuple, list] = defaultdict(list)
    for log_path in log_paths:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("type") == "monitor_snap" and d.get("slot") == "current":
                        rows[(log_path.name, d["slug"])].append(d)
                except Exception:
                    pass

    slugs = []
    for (log_name, slug), cur in rows.items():
        cur.sort(key=lambda x: x["ts"])
        last = cur[-1]
        secs_last = last.get("secs") or 999
        up_f, dn_f = last["up_bid"], last["down_bid"]
        if secs_last > 40:
            resolution = "UNKNOWN"
        elif up_f >= 0.85:
            resolution = "UP"
        elif dn_f >= 0.85:
            resolution = "DOWN"
        else:
            resolution = "UNKNOWN"

        def bucket(lo, hi, c=cur):
            return [s for s in c if s.get("secs") and lo <= s["secs"] <= hi]

        si = SlugInfo(
            slug=f"{log_name[:8]}:{slug}", resolution=resolution, all_cur=cur,
            s240=bucket(181, 240), s180=bucket(121, 180),
            s120=bucket(61, 120),  s60=bucket(31, 60),
        )
        slugs.append(si)
    return slugs


def _load_slugs_single(log_path: Path) -> List[SlugInfo]:
    rows: Dict[str, list] = defaultdict(list)
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
                if d.get("type") == "monitor_snap" and d.get("slot") == "current":
                    rows[d["slug"]].append(d)
            except Exception:
                pass

    slugs = []
    for slug, cur in rows.items():
        cur.sort(key=lambda x: x["ts"])
        last = cur[-1]
        secs_last = last.get("secs") or 999
        up_f, dn_f = last["up_bid"], last["down_bid"]
        if secs_last > 40:
            resolution = "UNKNOWN"
        elif up_f >= 0.85:
            resolution = "UP"
        elif dn_f >= 0.85:
            resolution = "DOWN"
        else:
            resolution = "UNKNOWN"

        def bucket(lo, hi):
            return [s for s in cur if s.get("secs") and lo <= s["secs"] <= hi]

        si = SlugInfo(
            slug=slug, resolution=resolution, all_cur=cur,
            s240=bucket(181, 240), s180=bucket(121, 180),
            s120=bucket(61, 120),  s60=bucket(31, 60),
        )
        slugs.append(si)
    return slugs


# ─── simulation ─────────────────────────────────────────────────────────────

def simulate_slug(
    si: SlugInfo,
    qty: float, stop_ticks: float, entry_min: float,
    max_entry_secs: int, max_end_secs: int,
) -> Optional[Trade]:
    entry = None
    for s in si.all_cur:
        ws = s.get("winner_side")
        if not ws or not s.get("sig_ar"):
            continue
        wb = s["up_bid"] if ws == "UP" else s["down_bid"]
        secs = s.get("secs") or 999
        if wb >= entry_min and secs <= max_entry_secs:
            entry = s; side = ws; ep = wb
            break
    if not entry:
        return None

    tick = 0.01
    sp = round(ep - stop_ticks * tick, 4)
    d15 = entry.get("sc_d15") or 0.0

    t = Trade(
        slug=si.slug,
        entry_ts=entry["ts"],
        side=side, ep=ep,
        entry_secs=entry.get("secs") or 0,
        stop_price=sp,
        worst_bid=ep,
        d15_at_entry=d15,
        # filtros
        f1_has_early=(si.early_leader is not None),
        f2_confirms=(si.early_leader == side),
        f3_cont=(si.cont_ok if si.early_leader == side else False),
        f4_spot=(_spot_agrees(d15, side)),
    )

    post = [s for s in si.all_cur if s["ts"] > entry["ts"]]
    if not post:
        t.outcome = "UNRESOLVED"; return t

    gap = 0
    for s in post:
        wb = s["up_bid"] if side == "UP" else s["down_bid"]
        t.worst_bid = min(t.worst_bid, wb)
        if wb <= 0.0:
            gap += 1
            if gap >= 2:
                t.outcome = "BOOK_GAP"; t.exit_bid = 0.0
                t.exit_secs = s.get("secs"); t.pnl = round(-ep * qty, 4)
                return t
        else:
            gap = 0
        if wb <= sp:
            t.outcome = "STOP"; t.exit_bid = wb
            t.exit_secs = s.get("secs"); t.pnl = round((wb - ep) * qty, 4)
            return t

    last = post[-1]
    lw = last["up_bid"] if side == "UP" else last["down_bid"]
    if last.get("secs") and last["secs"] <= max_end_secs:
        if lw >= 0.85:
            t.outcome = "WIN"; t.exit_bid = 1.0
            t.exit_secs = last.get("secs"); t.pnl = round((1.0 - ep) * qty, 4)
        else:
            t.outcome = "REVERSAL"; t.exit_bid = lw
            t.exit_secs = last.get("secs"); t.pnl = round((lw - ep) * qty, 4)
    else:
        t.outcome = "UNRESOLVED"; t.exit_bid = lw
    return t


def _spot_agrees(d15: float, side: str) -> bool:
    if abs(d15) < 1.0:
        return True   # sinal fraco → neutro, não contradiz
    return (d15 > 0) == (side == "UP")


# ─── report helpers ─────────────────────────────────────────────────────────

SEP = "─" * 60

def _report(trades: List[Trade], label: str, qty: float):
    res = [t for t in trades if t.outcome != "UNRESOLVED"]
    if not res:
        print(f"  {label}: 0 trades resolvidos")
        return
    wins  = [t for t in res if t.outcome == "WIN"]
    stops = [t for t in res if t.outcome == "STOP"]
    gaps  = [t for t in res if t.outcome == "BOOK_GAP"]
    revs  = [t for t in res if t.outcome == "REVERSAL"]
    pnl   = sum(t.pnl for t in res)
    wr    = 100 * len(wins) / len(res)
    ep_avg = sum(t.ep for t in res) / len(res)
    skip  = len(trades) - len(res)
    print(f"  {label}")
    print(f"    Trades: {len(res)}  (+{skip} unresolved)   WR: {wr:.1f}%   "
          f"PnL: ${pnl:+.2f}   avg/trade: ${pnl/len(res):+.4f}")
    print(f"    WIN:{len(wins):3d} ${sum(t.pnl for t in wins):+.2f}   "
          f"STOP:{len(stops):3d} ${sum(t.pnl for t in stops):+.2f}   "
          f"BOOK_GAP:{len(gaps):3d}   REVERSAL:{len(revs):3d}   "
          f"entry_avg:{ep_avg:.3f}")
    if stops:
        eb = [t.exit_bid for t in stops]
        print(f"    Stop exit_bid: avg={sum(eb)/len(eb):.3f}  min={min(eb):.3f}")


def _matrix(all_trades: List[Trade]):
    """Tabela: filtros individuais e combinações."""
    res = [t for t in all_trades if t.outcome != "UNRESOLVED"]
    if not res:
        return

    configs = [
        ("Baseline (sem filtros)",        lambda t: True),
        ("F2 confirma early_leader",       lambda t: t.f2_confirms),
        ("F2 + F3 continuidade",           lambda t: t.f2_confirms and t.f3_cont),
        ("F4 spot_ok",                     lambda t: t.f4_spot),
        ("F2 + F4",                        lambda t: t.f2_confirms and t.f4_spot),
        ("F2 + F3 + F4 (todos filtros)",   lambda t: t.f2_confirms and t.f3_cont and t.f4_spot),
        ("Excluir early_leader oposto",    lambda t: not (t.f1_has_early and not t.f2_confirms)),
        ("Exc.oposto + F4",                lambda t: (not (t.f1_has_early and not t.f2_confirms)) and t.f4_spot),
    ]

    print(f"\n  {'Config':38} {'n':>5} {'WR%':>5} {'PnL':>9} {'avg':>8}")
    print(f"  {'─'*38} {'─'*5} {'─'*5} {'─'*9} {'─'*8}")
    for label, fn in configs:
        subset = [t for t in res if fn(t)]
        if not subset:
            print(f"  {label:38} {'0':>5}")
            continue
        wins = [t for t in subset if t.outcome == "WIN"]
        pnl  = sum(t.pnl for t in subset)
        wr   = 100 * len(wins) / len(subset)
        print(f"  {label:38} {len(subset):>5} {wr:>4.1f}% ${pnl:>+8.2f} ${pnl/len(subset):>+7.4f}")


def _secs_table(trades: List[Trade]):
    res = [t for t in trades if t.outcome != "UNRESOLVED"]
    print(f"\n  {'secs<=':>7} {'n':>5} {'WR%':>5} {'PnL':>9}")
    for ms in [30, 60, 90, 120]:
        ts  = [t for t in res if t.entry_secs <= ms]
        tw  = [t for t in ts if t.outcome == "WIN"]
        pnl = sum(t.pnl for t in ts)
        wr  = 100 * len(tw) / len(ts) if ts else 0
        print(f"  {ms:>7} {len(ts):>5} {wr:>4.1f}% ${pnl:>+8.2f}")


def _ep_table(trades: List[Trade]):
    res = [t for t in trades if t.outcome != "UNRESOLVED"]
    print(f"\n  {'ep>=':>7} {'n':>5} {'WR%':>5} {'PnL':>9}")
    for th in [0.88, 0.90, 0.92, 0.94, 0.96]:
        ts  = [t for t in res if t.ep >= th]
        tw  = [t for t in ts if t.outcome == "WIN"]
        pnl = sum(t.pnl for t in ts)
        wr  = 100 * len(tw) / len(ts) if ts else 0
        print(f"  {th:>7.2f} {len(ts):>5} {wr:>4.1f}% ${pnl:>+8.2f}")


def _early_leader_predictivity(slugs: List[SlugInfo]):
    """Mostra com que frequência o early_leader prediz a resolução final."""
    resolved = [si for si in slugs if si.resolution != "UNKNOWN"]
    if not resolved:
        return

    has_early = [si for si in resolved if si.early_leader]
    no_early  = [si for si in resolved if not si.early_leader]

    correct = [si for si in has_early if si.early_leader == si.resolution]
    wrong   = [si for si in has_early if si.early_leader != si.resolution]

    # por threshold de bid do early_leader
    print(f"\n  {'early_bid>=':>12} {'n':>5} {'acerto%':>8} {'PnL implicito':>14}")
    print(f"  {'─'*12} {'─'*5} {'─'*8} {'─'*14}")
    for thr in [0.52, 0.55, 0.58, 0.60, 0.63, 0.65, 0.70]:
        sub = [si for si in has_early if si.early_bid >= thr]
        if not sub:
            continue
        ok = sum(1 for si in sub if si.early_leader == si.resolution)
        pct = 100 * ok / len(sub)
        # implied PnL: compra líder a early_bid, resolve a 1.0 se correto, 0 se errado
        # simplificado sem stop
        implied = sum((1.0 - si.early_bid) if si.early_leader == si.resolution
                      else -si.early_bid for si in sub) * 6.0
        print(f"  {thr:>12.2f} {len(sub):>5} {pct:>7.1f}% ${implied:>+13.2f}")

    print(f"\n  Total com early_leader: {len(has_early)}/{len(resolved)} "
          f"({100*len(has_early)/len(resolved):.0f}%)")
    print(f"  Acerto direto (sem stop): "
          f"{len(correct)}/{len(has_early)} = {100*len(correct)/len(has_early):.1f}%")
    print(f"  Erro de direção:          "
          f"{len(wrong)}/{len(has_early)} = {100*len(wrong)/len(has_early):.1f}%")
    print(f"  Sem early_leader:         {len(no_early)}/{len(resolved)}")

    # com F3 (continuidade)
    cont_ok = [si for si in has_early if si.cont_ok]
    if cont_ok:
        ok_cont = sum(1 for si in cont_ok if si.early_leader == si.resolution)
        print(f"\n  Com F3 (continuidade >=0.70 em 180-121s):")
        print(f"    {len(cont_ok)} slugs  acerto={ok_cont}/{len(cont_ok)} "
              f"({100*ok_cont/len(cont_ok):.1f}%)")


# ─── main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--log",          type=Path,  default=None)
    ap.add_argument("--all-logs",     action="store_true",
                    help="combina todos os arquivos market_monitor_*.jsonl em logs/")
    ap.add_argument("--qty",          type=float, default=6.0)
    ap.add_argument("--stop-ticks",   type=float, default=2.0)
    ap.add_argument("--entry-min",    type=float, default=0.88)
    ap.add_argument("--entry-secs",   type=int,   default=120)
    ap.add_argument("--end-secs",     type=int,   default=35)
    ap.add_argument("--early-min",    type=float, default=0.55,
                    help="bid mínimo para early_leader em 240-181s (padrão 0.55)")
    ap.add_argument("--cont-min",     type=float, default=0.70,
                    help="bid mínimo de continuidade em 180-121s (padrão 0.70)")
    ap.add_argument("--show-trades",  action="store_true")
    ap.add_argument("--show-matrix",  action="store_true", default=True)
    args = ap.parse_args()

    all_logs = sorted(Path("logs").glob("market_monitor_*.jsonl"), reverse=True)
    if not all_logs:
        print("Nenhum log market_monitor encontrado em logs/")
        sys.exit(1)

    if args.all_logs:
        log_paths = all_logs
        total_kb = sum(p.stat().st_size for p in log_paths) / 1024
        print(f"\nLogs combinados: {len(log_paths)} arquivos  ({total_kb:.0f} KB total)")
        for p in log_paths:
            print(f"  {p.name}  ({p.stat().st_size/1024:.0f} KB)")
    else:
        log_path = args.log or all_logs[0]
        log_paths = [log_path]
        print(f"\nLog: {log_path.name}  ({log_path.stat().st_size/1024:.0f} KB)")

    print(f"Params: qty={args.qty}  stop={args.stop_ticks}T  "
          f"entry_min={args.entry_min}  entry_secs<={args.entry_secs}")
    print(f"Filtros: early_min={args.early_min}  cont_min={args.cont_min}")

    slugs = load_slugs_multi(log_paths)
    for si in slugs:
        si.compute_filters(args.early_min, args.cont_min)

    resolved = [si for si in slugs if si.resolution != "UNKNOWN"]
    total_slugs = len(slugs)
    up_res = sum(1 for si in resolved if si.resolution == "UP")
    print(f"\nMercados: {total_slugs} total  |  {len(resolved)} com resolução clara "
          f"(UP={up_res} DOWN={len(resolved)-up_res})")

    # estatísticas dos filtros
    has_early = sum(1 for si in resolved if si.early_leader)
    confirms  = sum(1 for si in resolved if si.early_leader and si.cont_ok)
    print(f"Slugs c/ early_leader: {has_early}/{len(resolved)}  "
          f"| c/ continuidade: {confirms}/{has_early}")

    print()
    print(SEP)
    print("  EARLY LEADER — PODER PREDITIVO (sem trade, só direção)")
    print(SEP)
    _early_leader_predictivity(slugs)

    # simular todos os trades
    all_trades: List[Trade] = []
    for si in slugs:
        t = simulate_slug(si, args.qty, args.stop_ticks,
                          args.entry_min, args.entry_secs, args.end_secs)
        if t:
            all_trades.append(t)

    print()
    print("=" * 60)
    print("  SIMULAÇÃO AR — BASELINE vs FILTROS HÍBRIDOS")
    print("=" * 60)

    # baseline
    _report(all_trades, "BASELINE — AR sem filtros", args.qty)

    print()
    print(SEP)
    print("  FILTROS INDIVIDUAIS")
    print(SEP)

    # F1: tem early leader
    _report([t for t in all_trades if t.f1_has_early],
            "F1: tem early_leader (algum lado >0.55 em 240-181s)", args.qty)
    print()
    # F2: early leader confirma o side
    _report([t for t in all_trades if t.f2_confirms],
            "F2: early_leader CONFIRMA o side do AR", args.qty)
    print()
    # F2+F3: confirma + continuidade
    _report([t for t in all_trades if t.f2_confirms and t.f3_cont],
            "F2+F3: confirma + continuidade (bid >=0.70 em 180-121s)", args.qty)
    print()
    # Excluir contradição
    _report([t for t in all_trades if not (t.f1_has_early and not t.f2_confirms)],
            "EXC.OPOSTO: evitar AR quando early_leader contradiz", args.qty)
    print()
    # F4: spot concorda
    _report([t for t in all_trades if t.f4_spot],
            "F4: spot_d15 não contradiz winner na entrada", args.qty)

    print()
    print(SEP)
    print("  COMBINAÇÕES")
    print(SEP)
    print()

    # melhor combo
    combo = [t for t in all_trades if t.f2_confirms and t.f3_cont and t.f4_spot]
    _report(combo, "F2+F3+F4 — early confirma + continuidade + spot_ok", args.qty)
    print()
    combo2 = [t for t in all_trades
              if not (t.f1_has_early and not t.f2_confirms) and t.f4_spot]
    _report(combo2, "EXC.OPOSTO + F4 — sem contradição + spot_ok", args.qty)

    print()
    print(SEP)
    print("  MATRIZ COMPLETA DE FILTROS")
    print(SEP)
    _matrix(all_trades)

    print()
    print(SEP)
    print("  SENSIBILIDADE — BASELINE vs MELHOR FILTRO")
    print(SEP)

    best = [t for t in all_trades if t.f2_confirms and t.f3_cont]
    print("\n  Baseline — secs de entrada:")
    _secs_table(all_trades)
    print("\n  F2+F3 — secs de entrada:")
    _secs_table(best)

    print("\n  Baseline — entry_price:")
    _ep_table(all_trades)
    print("\n  F2+F3 — entry_price:")
    _ep_table(best)

    if args.show_trades:
        res = [t for t in all_trades if t.outcome != "UNRESOLVED"]
        print(f"\n  TRADES INDIVIDUAIS ({len(res)}):")
        print(f"  {'Hora':>8}  {'Side':>4}  {'EP':>6}  {'Secs':>4}  "
              f"{'F2':>3}{'F3':>3}{'F4':>3}  {'Out':>10}  {'PnL':>7}")
        for t in sorted(res, key=lambda x: x.entry_ts):
            hora = datetime.fromtimestamp(t.entry_ts).strftime("%H:%M:%S")
            f_str = f"{'Y' if t.f2_confirms else '.':>3}{'Y' if t.f3_cont else '.':>3}{'Y' if t.f4_spot else '.':>3}"
            print(f"  {hora}  {t.side:>4}  {t.ep:.3f}  {t.entry_secs:>4}  "
                  f"{f_str}  {t.outcome:>10}  ${t.pnl:>+.2f}")


if __name__ == "__main__":
    main()
