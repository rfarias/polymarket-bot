"""
analyze_hedge_hypothesis.py

Testa a hipótese de hedge parcial em trades perdedores do almost_resolved.

Ideia: quando a posição começa a reverter, comprar o lado contrário (loser)
para limitar a perda. Mesmo que odds_winner + odds_loser > 1.00 (ineficiente),
o hedge limita o drawdown máximo.

Dois triggers principais:
  A) loser_rise: loser_buy cruza acima de um threshold (0.04, 0.06, 0.08, 0.12, 0.15)
  B) winner_drop: winner_buy cai X bps abaixo do entry_price (1, 2, 3, 5 bps)
  C) early_insurance: sempre compra loser quando loser_buy <= 0.03 (logo após entrada)
     — seguro de cauda para reversões explosivas de novo candle

Estratégia de saída do hedge:
  hold_to_resolution: mantém o hedge aberto até a resolução final.
  Resultado:
    - Se stop/loss no original → assume loser venceu → hedge recebe 1.00
    - Se win/target/profit no original → winner venceu → hedge perde (0.00)

Fórmula:
  pnl_original = conforme registrado (pnl_ticks * tick_size * qty ou pnl_quote)
  pnl_hedge    = (1.0 - hedge_entry_price) * hedge_qty   [se loser ganhou]
               = -hedge_entry_price * hedge_qty           [se winner ganhou]
  pnl_net      = pnl_original + pnl_hedge

Funciona em:
  - Logs paper locais (type=enter/exit/snapshot)
  - Logs reais do runner (type=entry_filled/trade_summary/awaiting_redeem/snapshot)

Uso:
  python analyze_hedge_hypothesis.py
  python analyze_hedge_hypothesis.py --log-dir logs/current_almost_resolved_real_20260601
  python analyze_hedge_hypothesis.py --log-dir logs/ --min-improvement 0
  python analyze_hedge_hypothesis.py --paper-only
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.stdout.reconfigure(encoding="utf-8")

LOGS_DIR = Path("logs")
REAL_PREFIX = "current_almost_resolved_real_"

# Paper logs com amostra suficiente
PAPER_LOGS = [
    "dual_almost_paper_guardian_hybrid_v5/current_almost_resolved.jsonl",
    "dual_almost_paper_v2/current_almost_resolved.jsonl",
    "dual_almost_paper_v1/current_almost_resolved.jsonl",
    "current_almost_resolved_paper_resolved_pullback_safe_v2_3h_20260427_101148.jsonl",
    "current_almost_resolved_paper_resolved_pullback_safe_v2_1h.jsonl",
    "current_almost_resolved_paper_variant_1h.jsonl",
    "current_almost_resolved_gray_zone_paper_v1/gray_zone_live.jsonl",
]


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _iter_jsonl(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    pass
    except Exception:
        pass


def _sf(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(d)


def _find_files(log_dir: Optional[str], paper_only: bool) -> list[tuple[Path, str]]:
    """Retorna lista de (path, source_label)."""
    results: list[tuple[Path, str]] = []

    if paper_only or log_dir is None:
        # inclui sempre os paper logs locais
        for rel in PAPER_LOGS:
            p = LOGS_DIR / rel
            if p.exists():
                results.append((p, "paper"))

    if log_dir and not paper_only:
        p = Path(log_dir)
        if p.is_file() and p.suffix == ".jsonl":
            results.append((p, "real"))
        elif p.is_dir():
            c = p / "current_almost_resolved_real.jsonl"
            if c.exists():
                results.append((c, "real"))
            else:
                for f in sorted(p.glob("*.jsonl")):
                    results.append((f, "real"))
                # subdirs com prefixo real
                for d in sorted(p.iterdir()):
                    if d.is_dir() and d.name.startswith(REAL_PREFIX):
                        rc = d / "current_almost_resolved_real.jsonl"
                        if rc.exists():
                            results.append((rc, "real"))
        else:
            for ep in sorted(glob.glob(str(log_dir))):
                ep_path = Path(ep)
                if ep_path.is_file() and ep_path.suffix == ".jsonl":
                    results.append((ep_path, "real"))
                elif ep_path.is_dir():
                    rc = ep_path / "current_almost_resolved_real.jsonl"
                    if rc.exists():
                        results.append((rc, "real"))
                    else:
                        for f in sorted(ep_path.glob("*.jsonl")):
                            results.append((f, "real"))

    return results


# ---------------------------------------------------------------------------
# Estrutura de trade reconstruído
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    slug: str
    source: str          # "paper" | "real"
    side: str = "?"
    entry_price: float = 0.0
    entry_qty: float = 0.0
    exit_price: float = 0.0
    pnl_quote: float = 0.0   # P&L em USDC
    exit_reason: str = ""
    outcome: str = "unknown"  # win | loss | unknown

    # snapshots durante a posição aberta: list of (secs, winner_bid, loser_bid)
    position_snaps: list[tuple[int, float, float]] = field(default_factory=list)

    # secs no momento da entrada (primeiro snapshot aberto)
    entry_secs: Optional[int] = None
    setup_variant: str = "standard"


# ---------------------------------------------------------------------------
# Parsing de logs (suporta formato paper e real)
# ---------------------------------------------------------------------------

def _parse_file(path: Path, source: str) -> list[TradeRecord]:
    events = list(_iter_jsonl(path))
    if not events:
        return []

    # detecta formato pelo primeiro tipo relevante
    all_types = {ev.get("type", "") for ev in events}

    # Real runner: fill events have trade.entry_qty_filled; paper fills don't
    has_real_fill = any(
        ev.get("type") == "fill" and isinstance(ev.get("trade"), dict)
        for ev in events
    )
    if has_real_fill or "entry_filled" in all_types or "trade_summary" in all_types:
        return _parse_real_format(events, path.stem, source)
    elif "enter" in all_types or "fill" in all_types:
        return _parse_paper_format(events, path.stem, source)
    else:
        return _parse_real_format(events, path.stem, source)


def _parse_paper_format(events: list[dict], session: str, source: str) -> list[TradeRecord]:
    """
    Formato dos paper runners (dual_almost_paper_*, etc.).
    Eventos: type=fill (entrada), type=exit (saída), type=snapshot (estado).
    """
    records: list[TradeRecord] = []
    current_slug: Optional[str] = None
    current_trade: Optional[TradeRecord] = None

    for ev in events:
        t = ev.get("type", "")
        sig = ev.get("signal") or {}
        tr = ev.get("trade") or {}

        # ----------------------------------------------------------------
        # Snapshot: atualiza slug e captura preços durante posição aberta
        # ----------------------------------------------------------------
        if t == "snapshot":
            slug = ev.get("current_slug")
            if slug:
                current_slug = slug

            mode = str(tr.get("mode") or "idle").lower()
            secs = ev.get("current_secs")
            up_buy  = _sf(sig.get("up_buy")  or sig.get("up_bid"),   0.0)
            down_buy = _sf(sig.get("down_buy") or sig.get("down_bid"), 0.0)

            if mode in ("open", "open_position", "pending_exit") and current_trade is not None:
                if secs is not None and up_buy > 0 and down_buy > 0:
                    side = current_trade.side
                    winner = up_buy if side == "UP" else down_buy
                    loser  = down_buy if side == "UP" else up_buy
                    current_trade.position_snaps.append((int(secs), winner, loser))
                    if current_trade.entry_secs is None:
                        current_trade.entry_secs = int(secs)

        # ----------------------------------------------------------------
        # Fill: entrada em posição (paper runners usam type=fill)
        # ----------------------------------------------------------------
        elif t == "fill":
            fill_qty   = _sf(ev.get("fill_qty"), 0.0)
            entry_price = _sf(sig.get("entry_price") or ev.get("entry_price"), 0.0)
            side = str(sig.get("side") or ev.get("side") or "?")
            sv   = str(sig.get("setup_variant") or "standard")

            if entry_price > 0 and fill_qty > 0:
                current_trade = TradeRecord(
                    slug=current_slug or session,
                    source=source,
                    side=side,
                    entry_price=entry_price,
                    entry_qty=fill_qty,
                    setup_variant=sv,
                )

        # ----------------------------------------------------------------
        # Exit: saída da posição — todos os dados estão em trade sub-dict
        # ----------------------------------------------------------------
        elif t == "exit":
            pnl_quote   = _sf(tr.get("pnl_quote"),   0.0)
            pnl_ticks   = _sf(tr.get("pnl_ticks"),   0.0)
            exit_price  = _sf(tr.get("exit_price"),   0.0)
            exit_reason = str(tr.get("exit_reason")  or "")
            entry_price = _sf(tr.get("entry_price"),  0.0)
            qty         = _sf(tr.get("qty"),          0.0)
            side        = str(tr.get("side")         or "?")
            sv          = str(tr.get("setup_variant") or "standard")

            if current_trade is None and entry_price > 0:
                # exit sem fill visto — reconstrói do evento de exit
                current_trade = TradeRecord(
                    slug=current_slug or session,
                    source=source,
                    side=side,
                    entry_price=entry_price,
                    entry_qty=qty,
                    setup_variant=sv,
                )

            if current_trade is not None:
                if current_trade.entry_qty == 0:
                    current_trade.entry_qty = qty
                # pnl_quote tem prioridade; fallback de ticks
                if pnl_quote == 0.0 and pnl_ticks != 0.0:
                    pnl_quote = pnl_ticks * 0.01 * current_trade.entry_qty
                current_trade.exit_price  = exit_price
                current_trade.pnl_quote   = round(pnl_quote, 6)
                current_trade.exit_reason = exit_reason
                current_trade.outcome     = _infer_outcome(exit_reason, pnl_quote)

                if current_trade.entry_price > 0:
                    records.append(current_trade)
                current_trade = None

    return records


def _parse_real_format(events: list[dict], session: str, source: str) -> list[TradeRecord]:
    """Formato do runner real: type=entry_filled/trade_summary/awaiting_redeem/snapshot."""
    # agrupa por slug
    slug_events: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        slug = (
            ev.get("current_slug")
            or ev.get("slug")
            or (ev.get("signal") or {}).get("event_slug")
            or (ev.get("trade") or {}).get("event_slug")
            or (ev.get("state") or {}).get("event_slug")
        )
        if slug:
            slug_events[slug].append(ev)

    records: list[TradeRecord] = []
    for slug, evs in slug_events.items():
        evs.sort(key=lambda e: _sf(e.get("ts") or e.get("timestamp"), 0))
        rec = _reconstruct_real_trade(slug, session, source, evs)
        if rec is not None:
            records.append(rec)
    return records


def _reconstruct_real_trade(slug: str, session: str, source: str, evs: list[dict]) -> Optional[TradeRecord]:
    rec = TradeRecord(slug=slug, source=source)
    has_entry = False

    for ev in evs:
        t = ev.get("type", "")
        sig = ev.get("signal") or {}
        tr_state = ev.get("trade") or {}

        if t == "snapshot":
            mode = str(tr_state.get("mode") or "idle").lower()
            secs = ev.get("current_secs")
            up_buy = _sf(sig.get("up_buy"), 0.0)
            down_buy = _sf(sig.get("down_buy"), 0.0)

            if mode in ("open_position", "pending_exit") and has_entry:
                if secs is not None and up_buy > 0 and down_buy > 0:
                    side = rec.side
                    winner = up_buy if side == "UP" else down_buy
                    loser = down_buy if side == "UP" else up_buy
                    rec.position_snaps.append((int(secs), winner, loser))
                    if rec.entry_secs is None:
                        rec.entry_secs = int(secs)

        elif t in ("entry_filled", "entry_confirmed", "position_opened", "fill"):
            ep = _sf(ev.get("entry_price") or ev.get("fill_price") or tr_state.get("entry_price"), 0.0)
            qty = _sf(ev.get("qty_filled") or ev.get("qty") or tr_state.get("entry_qty_filled"), 0.0)
            if ep > 0 and qty > 0 and not has_entry:
                has_entry = True
                rec.entry_price = ep
                rec.entry_qty = qty
                rec.side = str(ev.get("side") or tr_state.get("side") or sig.get("side") or "?")
                rec.setup_variant = str(tr_state.get("setup_variant") or sig.get("setup_variant") or "standard")

        elif t in ("flat", "redeem_flat"):
            exit_ord = ev.get("exit_order") or {}
            xp = _sf(exit_ord.get("price") or tr_state.get("target_price"), 0.0)
            xq = _sf(exit_ord.get("size_matched") or tr_state.get("exit_qty_filled"), 0.0)
            if xp > 0 and xq > 0 and rec.entry_price > 0:
                rec.pnl_quote = round((xp - rec.entry_price) * xq, 4)
            rec.exit_price = xp
            rec.exit_reason = str(tr_state.get("last_reason") or t)

        elif t == "trade_summary":
            state = ev.get("state") or tr_state or {}
            if not has_entry:
                ep = _sf(state.get("entry_price") or ev.get("entry_price"), 0.0)
                if ep > 0:
                    has_entry = True
                    rec.entry_price = ep
                    rec.entry_qty = _sf(state.get("entry_qty_filled"), 0.0)
                    rec.side = str(state.get("side") or "?")
                    rec.setup_variant = str(state.get("setup_variant") or "standard")
            xp = _sf(state.get("exit_price_posted") or state.get("exit_price") or ev.get("exit_price"), 0.0)
            xq = _sf(state.get("exit_qty_filled") or ev.get("exit_qty_filled"), 0.0)
            pnl = ev.get("pnl")
            if pnl is not None:
                rec.pnl_quote = _sf(pnl)
            elif xp > 0 and xq > 0 and rec.entry_price > 0:
                rec.pnl_quote = round((xp - rec.entry_price) * xq, 4)
            rec.exit_price = xp
            rec.exit_reason = str(state.get("last_reason") or ev.get("reason") or "")

        elif t == "awaiting_redeem":
            state = ev.get("state") or tr_state or {}
            side_won = ev.get("side_won")
            ep = _sf(state.get("entry_price") or rec.entry_price, 0.0)
            qty = _sf(state.get("entry_qty_filled") or rec.entry_qty, 0.0)
            if ep > 0 and qty > 0:
                if not has_entry:
                    has_entry = True
                    rec.entry_price = ep
                    rec.entry_qty = qty
                    rec.side = str(state.get("side") or "?")
                if side_won is True:
                    rec.pnl_quote = round((1.0 - ep) * qty, 4)
                    rec.exit_price = 1.0
                    rec.exit_reason = "redeem_win"
                elif side_won is False:
                    rec.pnl_quote = round(-ep * qty, 4)
                    rec.exit_price = 0.0
                    rec.exit_reason = "redeem_loss"

    if not has_entry or rec.entry_price <= 0:
        return None

    rec.outcome = _infer_outcome(rec.exit_reason, rec.pnl_quote)
    return rec


def _infer_outcome(exit_reason: str, pnl_quote: float) -> str:
    if pnl_quote > 0.001:
        return "win"
    if pnl_quote < -0.001:
        return "loss"
    reason = exit_reason.lower()
    if any(k in reason for k in ("target", "profit_protect", "near_win", "redeem_win")):
        return "win"
    if any(k in reason for k in ("stop", "redeem_loss", "platform_redeem_loss", "timeout")):
        return "loss"
    return "unknown"


# ---------------------------------------------------------------------------
# Definição dos triggers de hedge
# ---------------------------------------------------------------------------

@dataclass
class HedgeTrigger:
    name: str
    description: str
    # "loser_rise": dispara quando loser_bid >= threshold
    loser_rise_threshold: Optional[float] = None
    # "winner_drop": dispara quando winner_bid cai X bps abaixo do entry_price
    winner_drop_bps: Optional[float] = None
    # "early_insurance": dispara no primeiro snapshot com loser_bid <= max_loser
    early_insurance_max: Optional[float] = None
    # preço máximo para aceitar hedge (evita hedge caro)
    max_hedge_price: float = 0.30

    def find_trigger(
        self, entry_price: float, snaps: list[tuple[int, float, float]]
    ) -> Optional[tuple[int, float]]:
        """
        Retorna (secs, loser_price) no momento do trigger, ou None.
        snaps = [(secs, winner_bid, loser_bid), ...]
        """
        if not snaps:
            return None

        if self.early_insurance_max is not None:
            # primeiro snapshot com loser <= max
            for secs, winner, loser in snaps:
                if 0 < loser <= self.early_insurance_max:
                    return (secs, loser)
            return None

        for secs, winner, loser in snaps:
            if self.loser_rise_threshold is not None:
                if loser >= self.loser_rise_threshold and loser <= self.max_hedge_price:
                    return (secs, loser)

            if self.winner_drop_bps is not None:
                drop = entry_price - winner
                if drop >= self.winner_drop_bps / 100.0 and loser <= self.max_hedge_price:
                    return (secs, loser)

        return None


def _build_triggers() -> list[HedgeTrigger]:
    return [
        # Seguro de cauda barato (para reversões explosivas de novo candle)
        HedgeTrigger("insurance_003", "Seguro inicial: loser <= 0.03", early_insurance_max=0.03),
        HedgeTrigger("insurance_005", "Seguro inicial: loser <= 0.05", early_insurance_max=0.05),
        HedgeTrigger("insurance_008", "Seguro inicial: loser <= 0.08", early_insurance_max=0.08),

        # Reversal by loser side rising
        HedgeTrigger("loser_004", "Loser sobe para >= 0.04", loser_rise_threshold=0.04),
        HedgeTrigger("loser_006", "Loser sobe para >= 0.06", loser_rise_threshold=0.06),
        HedgeTrigger("loser_008", "Loser sobe para >= 0.08", loser_rise_threshold=0.08),
        HedgeTrigger("loser_010", "Loser sobe para >= 0.10", loser_rise_threshold=0.10),
        HedgeTrigger("loser_012", "Loser sobe para >= 0.12", loser_rise_threshold=0.12),
        HedgeTrigger("loser_015", "Loser sobe para >= 0.15", loser_rise_threshold=0.15),
        HedgeTrigger("loser_020", "Loser sobe para >= 0.20", loser_rise_threshold=0.20),

        # Reversal by winner dropping
        HedgeTrigger("drop_1bps", "Winner cai >= 1 bps do entry", winner_drop_bps=1.0),
        HedgeTrigger("drop_2bps", "Winner cai >= 2 bps do entry", winner_drop_bps=2.0),
        HedgeTrigger("drop_3bps", "Winner cai >= 3 bps do entry", winner_drop_bps=3.0),
        HedgeTrigger("drop_5bps", "Winner cai >= 5 bps do entry", winner_drop_bps=5.0),
        HedgeTrigger("drop_10bps", "Winner cai >= 10 bps do entry", winner_drop_bps=10.0),

        # Combinados (drop + loser já subiu)
        HedgeTrigger("drop2_loser006", "Winner -2bps E loser >= 0.06",
                     winner_drop_bps=2.0, loser_rise_threshold=0.06),
        HedgeTrigger("drop3_loser008", "Winner -3bps E loser >= 0.08",
                     winner_drop_bps=3.0, loser_rise_threshold=0.08),
    ]


# ---------------------------------------------------------------------------
# Simulação de hedge
# ---------------------------------------------------------------------------

@dataclass
class HedgeVariant:
    trigger: HedgeTrigger
    qty_ratio: float   # múltiplo da posição original (ex: 0.5, 1.0, 2.0)

    @property
    def name(self) -> str:
        return f"{self.trigger.name}_q{self.qty_ratio:.1f}x"

    @property
    def description(self) -> str:
        return f"{self.trigger.description} | {self.qty_ratio:.1f}x qty"


@dataclass
class SimResult:
    variant: HedgeVariant
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    unknowns: int = 0
    hedged_count: int = 0
    # trades onde o hedge foi ativado e o original foi loss → hedge salvou
    hedge_saved: int = 0
    # trades onde o hedge foi ativado e o original foi win → hedge custou
    hedge_cost_on_win: int = 0
    # P&L totais
    pnl_baseline: float = 0.0
    pnl_with_hedge: float = 0.0
    hedge_pnl_on_losses: float = 0.0   # ganho do hedge nos trades perdedores
    hedge_pnl_on_wins: float = 0.0     # custo do hedge nos trades vencedores


def _simulate_variants(
    trades: list[TradeRecord],
    triggers: list[HedgeTrigger],
    qty_ratios: list[float],
) -> tuple[float, list[SimResult]]:
    """Retorna (baseline_pnl, lista de SimResult)."""
    baseline_pnl = sum(t.pnl_quote for t in trades if t.outcome != "unknown")
    results: list[SimResult] = []

    for trigger in triggers:
        for qr in qty_ratios:
            variant = HedgeVariant(trigger=trigger, qty_ratio=qr)
            sr = SimResult(variant=variant)

            for trade in trades:
                if trade.outcome == "unknown":
                    sr.unknowns += 1
                    continue

                sr.total_trades += 1
                sr.pnl_baseline += trade.pnl_quote

                if trade.outcome == "win":
                    sr.wins += 1
                elif trade.outcome == "loss":
                    sr.losses += 1

                # verifica se trigger dispara
                trig_result = trigger.find_trigger(trade.entry_price, trade.position_snaps)

                if trig_result is None:
                    # sem hedge — P&L não muda
                    sr.pnl_with_hedge += trade.pnl_quote
                    continue

                # hedge disparou
                trig_secs, hedge_price = trig_result
                hedge_qty = trade.entry_qty * qr
                sr.hedged_count += 1

                if trade.outcome == "loss":
                    # loser ganhou → hedge recebe 1.00
                    hedge_pnl = (1.0 - hedge_price) * hedge_qty
                    sr.hedge_pnl_on_losses += hedge_pnl
                    sr.hedge_saved += 1
                else:
                    # winner ganhou → hedge perde tudo
                    hedge_pnl = -hedge_price * hedge_qty
                    sr.hedge_pnl_on_wins += hedge_pnl
                    sr.hedge_cost_on_win += 1

                sr.pnl_with_hedge += trade.pnl_quote + hedge_pnl

            results.append(sr)

    return baseline_pnl, results


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

def _print_report(
    trades: list[TradeRecord],
    baseline_pnl: float,
    results: list[SimResult],
    min_improvement: float,
    top_n: int,
) -> None:
    results.sort(key=lambda r: r.pnl_with_hedge, reverse=True)

    total = len([t for t in trades if t.outcome != "unknown"])
    wins  = len([t for t in trades if t.outcome == "win"])
    losses= len([t for t in trades if t.outcome == "loss"])

    print("\n" + "=" * 90)
    print("  ANÁLISE DE HEDGE — almost_resolved")
    print("=" * 90)
    print(f"  Trades (outcome conhecido): {total}  |  Wins: {wins}  |  Losses: {losses}")
    print(f"  PnL baseline total: {baseline_pnl:+.4f} USDC")

    if not trades:
        print("\n  Nenhum trade encontrado. Verifique o caminho dos logs.")
        return

    # distribuição das perdas
    loss_trades = [t for t in trades if t.outcome == "loss"]
    if loss_trades:
        print(f"\n  Trades perdedores:")
        for t in sorted(loss_trades, key=lambda x: x.pnl_quote):
            snap_info = ""
            if t.position_snaps:
                snaps = t.position_snaps
                max_loser = max(s[2] for s in snaps)
                min_winner = min(s[1] for s in snaps)
                snap_info = (f" | snaps={len(snaps)} max_loser={max_loser:.3f} "
                             f"min_winner={min_winner:.3f}")
            print(f"    {t.outcome:6s} {t.setup_variant:30s} entry={t.entry_price:.3f} "
                  f"side={t.side} qty={t.entry_qty:.0f} pnl={t.pnl_quote:+.4f} "
                  f"reason={t.exit_reason}{snap_info}  [{t.source}]")

    print(f"\n{'=' * 90}")
    print(f"  RANKING DE VARIANTES DE HEDGE (melhoria mín: {min_improvement:+.4f} USDC; top {top_n})")
    print(f"  Formato: melhoria = pnl_com_hedge − pnl_baseline")
    print(f"{'=' * 90}")

    header = (f"  {'Variante':28s} {'PnL+Hedge':>10} {'Melhoria':>9} "
              f"{'Hedged':>7} {'Salvou':>7} {'Custou':>7} "
              f"{'GanhoPerda':>11} {'CustoWin':>10}  Descrição")
    print(f"\n{header}")
    print(f"  {'-' * 110}")

    shown = 0
    for sr in results:
        improvement = sr.pnl_with_hedge - baseline_pnl
        if improvement < min_improvement:
            continue
        if shown >= top_n:
            break
        print(
            f"  {sr.variant.name:28s} {sr.pnl_with_hedge:+10.4f} {improvement:+9.4f} "
            f"{sr.hedged_count:7d} {sr.hedge_saved:7d} {sr.hedge_cost_on_win:7d} "
            f"{sr.hedge_pnl_on_losses:+11.4f} {sr.hedge_pnl_on_wins:+10.4f}  "
            f"{sr.variant.description}"
        )
        shown += 1

    # Análise de cobertura dos snapshots
    no_snaps = [t for t in trades if not t.position_snaps and t.outcome != "unknown"]
    if no_snaps:
        print(f"\n  AVISO: {len(no_snaps)} trades sem snapshots de posição aberta")
        print(f"  → triggers de reversão não conseguem ser avaliados nesses casos")
        print(f"  → em logs reais com poll de 0.5s haverá mais cobertura")
        for t in no_snaps:
            print(f"    {t.slug[-35:]:35s} {t.outcome:5s} pnl={t.pnl_quote:+.4f} [{t.source}]")

    # Tabela de trades com hedge mais interessante
    if results:
        best = results[0]
        if best.hedged_count > 0:
            print(f"\n{'=' * 90}")
            print(f"  DETALHE: melhor variante = {best.variant.name}")
            print(f"  ({best.variant.description})")
            print(f"{'=' * 90}")
            print(f"\n  {'Slug':38s} {'Out':6s} {'EntryP':>7} {'HedgeP':>8} "
                  f"{'HedgeQ':>7} {'PnL_orig':>9} {'PnL_hdg':>9} {'PnL_net':>9}  Trigger")
            print(f"  {'-' * 100}")

            for trade in sorted(trades, key=lambda x: x.pnl_quote):
                if trade.outcome == "unknown":
                    continue
                trig_result = best.variant.trigger.find_trigger(trade.entry_price, trade.position_snaps)
                hedge_pnl = 0.0
                hedge_p = 0.0
                hedge_q = 0.0
                trig_info = "no_trigger"
                if trig_result:
                    trig_secs, hedge_p = trig_result
                    hedge_q = trade.entry_qty * best.variant.qty_ratio
                    if trade.outcome == "loss":
                        hedge_pnl = (1.0 - hedge_p) * hedge_q
                    else:
                        hedge_pnl = -hedge_p * hedge_q
                    trig_info = f"secs={trig_secs} loser={hedge_p:.3f}"
                net = trade.pnl_quote + hedge_pnl
                print(
                    f"  {trade.slug[-38:]:38s} {trade.outcome:6s} {trade.entry_price:7.3f} "
                    f"{hedge_p:8.3f} {hedge_q:7.1f} {trade.pnl_quote:+9.4f} "
                    f"{hedge_pnl:+9.4f} {net:+9.4f}  {trig_info}"
                )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--log-dir", default=None,
                    help="Diretório com logs reais (ex: logs/ ou /outro_pc/logs/)")
    ap.add_argument("--paper-only", action="store_true",
                    help="Usar apenas logs paper locais (ignora --log-dir)")
    ap.add_argument("--min-improvement", type=float, default=-999.0,
                    help="Exibe só variantes com melhoria >= X sobre baseline (default: sem filtro)")
    ap.add_argument("--top", type=int, default=30,
                    help="Número máximo de variantes exibidas (default: 30)")
    ap.add_argument("--qty-ratios", type=str, default="0.5,1.0,2.0",
                    help="Múltiplos de qty para o hedge (default: 0.5,1.0,2.0)")
    args = ap.parse_args()

    qty_ratios = [float(x) for x in args.qty_ratios.split(",") if x.strip()]

    files = _find_files(args.log_dir, args.paper_only)
    print(f"Arquivos encontrados: {len(files)}")
    for p, src in files:
        print(f"  [{src}] {p}")

    trades: list[TradeRecord] = []
    for path, source in files:
        batch = _parse_file(path, source)
        trades.extend(batch)

    print(f"\nTrades reconstruídos: {len(trades)}")
    known = [t for t in trades if t.outcome != "unknown"]
    print(f"  Com outcome conhecido: {len(known)}")
    print(f"  Wins: {sum(1 for t in known if t.outcome == 'win')}")
    print(f"  Losses: {sum(1 for t in known if t.outcome == 'loss')}")

    triggers = _build_triggers()
    print(f"\nTriggers a testar: {len(triggers)}")
    print(f"Qty ratios: {qty_ratios}")
    print(f"Total variantes: {len(triggers) * len(qty_ratios)}")

    baseline_pnl, results = _simulate_variants(known, triggers, qty_ratios)
    _print_report(known, baseline_pnl, results, args.min_improvement, args.top)


if __name__ == "__main__":
    main()
