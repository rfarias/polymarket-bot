"""
analyze_real_variants.py

Testa variantes de filtros sobre os logs do runner real (current_almost_resolved_real).
Objetivo: identificar qual combinação de restrições teria gerado PnL positivo.

Como funciona:
  1. Lê todos os eventos de logs reais (snapshot, entry_filled/confirmed, trade_summary,
     awaiting_redeem, flat, etc.)
  2. Para cada slug: reconstrói as condições de mercado no momento da entrada real
     (secs, entry_price, setup_variant, bid_decel_gate, counter_price, loser_bid, etc.)
  3. Aplica uma matriz de filtros e calcula: quantos trades cada filtro teria bloqueado
     e qual seria o PnL resultante
  4. Imprime ranking das variantes por PnL total

Uso:
    python analyze_real_variants.py
    python analyze_real_variants.py --log-dir C:/outro_pc/logs
    python analyze_real_variants.py --log-dir logs/current_almost_resolved_real_20260601_120000
    python analyze_real_variants.py --min-pnl 0 --top 20
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.stdout.reconfigure(encoding="utf-8")

LOGS_DIR = Path("logs")
REAL_PREFIX = "current_almost_resolved_real_"


# ---------------------------------------------------------------------------
# I/O helpers
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


def _find_real_jsonl(log_dir: Optional[str]) -> list[Path]:
    """Encontra todos os arquivos JSONL do runner real."""
    results: list[Path] = []

    if log_dir:
        p = Path(log_dir)
        if p.is_file() and p.suffix == ".jsonl":
            return [p]
        if p.is_dir():
            # tenta nome canônico direto
            candidate = p / "current_almost_resolved_real.jsonl"
            if candidate.exists():
                return [candidate]
            # tenta subdirs com prefixo real (caso logs/ seja passado)
            for entry in sorted(p.iterdir()):
                if entry.is_dir() and entry.name.startswith(REAL_PREFIX):
                    c = entry / "current_almost_resolved_real.jsonl"
                    if c.exists():
                        results.append(c)
            if results:
                return results
            # fallback: qualquer jsonl na dir
            results = sorted(p.glob("*.jsonl"))
            return results
        # pode ser glob string
        expanded = sorted(glob.glob(str(log_dir)))
        for ep in expanded:
            ep_path = Path(ep)
            if ep_path.is_file() and ep_path.suffix == ".jsonl":
                results.append(ep_path)
            elif ep_path.is_dir():
                c = ep_path / "current_almost_resolved_real.jsonl"
                if c.exists():
                    results.append(c)
                else:
                    results.extend(sorted(ep_path.glob("*.jsonl")))
        return results

    # default: todos os diretórios com prefixo real
    for entry in sorted(LOGS_DIR.iterdir()):
        if not entry.name.startswith(REAL_PREFIX):
            continue
        if not entry.is_dir():
            continue
        c = entry / "current_almost_resolved_real.jsonl"
        if c.exists():
            results.append(c)
        else:
            cands = sorted(entry.glob("*.jsonl"))
            if cands:
                results.append(cands[0])
    return results


# ---------------------------------------------------------------------------
# Estrutura de um trade real reconstruído
# ---------------------------------------------------------------------------

@dataclass
class RealTrade:
    slug: str
    session: str

    # condições no momento da entrada
    setup_variant: str = "unknown"
    entry_price: float = 0.0
    entry_secs: Optional[int] = None       # secs_to_end no momento da entrada
    side: str = "?"
    bid_decel_gate_active: bool = False    # gate estava ativo para o lado entrado
    loser_bid_at_entry: float = 0.0       # preço do lado perdedor no momento da entrada
    counter_price_at_entry: float = 0.0   # preço do lado contrário ao entrado
    entry_score: float = 0.0              # score do sinal (se disponível)

    # resultado real
    exit_price: float = 0.0
    pnl_usd: float = 0.0                  # lucro/perda em USDC
    exit_reason: str = ""
    outcome: str = "unknown"              # win | loss | partial_loss | total_loss

    # contexto extra
    entry_ts: float = 0.0
    entry_qty: float = 0.0


# ---------------------------------------------------------------------------
# Reconstrução dos trades a partir dos logs
# ---------------------------------------------------------------------------

def _parse_sessions(files: list[Path]) -> list[RealTrade]:
    """Lê todos os eventos e reconstrói trades com condições de entrada."""
    trades: list[RealTrade] = []

    for fpath in files:
        session = fpath.parent.name if fpath.parent.name != "logs" else fpath.stem
        events = list(_iter_jsonl(fpath))

        # Agrupa por slug
        slug_events: dict[str, list[dict]] = defaultdict(list)
        for ev in events:
            t = ev.get("type", "")
            # extrai slug de múltiplos lugares
            slug = (
                ev.get("current_slug")
                or ev.get("slug")
                or (ev.get("signal") or {}).get("event_slug")
                or (ev.get("trade") or {}).get("event_slug")
                or (ev.get("state") or {}).get("event_slug")
            )
            if slug:
                slug_events[slug].append(ev)

        for slug, evs in slug_events.items():
            evs.sort(key=lambda e: _sf(e.get("ts") or e.get("timestamp"), 0))
            trade = _reconstruct_trade(slug, session, evs)
            if trade is not None:
                trades.append(trade)

    return trades


def _reconstruct_trade(slug: str, session: str, evs: list[dict]) -> Optional[RealTrade]:
    """
    Reconstrói um único trade a partir dos seus eventos ordenados.
    Retorna None se não há trade completo (sem entrada confirmada).
    """
    t = RealTrade(slug=slug, session=session)

    has_entry = False
    last_entry_snap: Optional[dict] = None  # snapshot imediatamente antes da entrada

    for ev in evs:
        ev_type = ev.get("type", "")
        signal = ev.get("signal") or {}
        trade_state = ev.get("trade") or {}

        # ----------------------------------------------------------------
        # Captura snapshot pré-entrada (quando signal.allow=True e mode=idle)
        # ----------------------------------------------------------------
        if ev_type == "snapshot":
            mode = (trade_state.get("mode") or "").lower()
            if mode == "idle" and signal.get("allow"):
                last_entry_snap = ev

        # ----------------------------------------------------------------
        # Entrada confirmada
        # ----------------------------------------------------------------
        if ev_type in ("entry_filled", "entry_confirmed", "position_opened", "fill"):
            ep = _sf(
                ev.get("entry_price") or ev.get("fill_price") or ev.get("price")
                or trade_state.get("entry_price"), 0.0
            )
            qty = _sf(
                ev.get("qty_filled") or ev.get("qty") or ev.get("entry_qty_filled")
                or trade_state.get("entry_qty_filled"), 0.0
            )
            if ep > 0 and qty > 0 and not has_entry:
                has_entry = True
                t.entry_price = ep
                t.entry_qty = qty
                t.side = str(ev.get("side") or trade_state.get("side") or signal.get("side") or "?")
                t.entry_ts = _sf(ev.get("ts") or ev.get("timestamp"), 0.0)
                t.setup_variant = str(
                    ev.get("setup_variant") or trade_state.get("setup_variant")
                    or signal.get("setup_variant") or "standard"
                )

            # usa o snapshot pré-entrada para pegar contexto de mercado
            if last_entry_snap:
                _fill_entry_context(t, last_entry_snap)

        # ----------------------------------------------------------------
        # Snapshot durante posição aberta (fallback para condições de entrada)
        # ----------------------------------------------------------------
        if ev_type == "snapshot" and not has_entry:
            mode = (trade_state.get("mode") or "").lower()
            if mode in ("open_position", "pending_entry"):
                # primeira vez que vemos a posição aberta → snapshot serve de contexto
                if t.entry_secs is None:
                    _fill_entry_context(t, ev)

        # ----------------------------------------------------------------
        # Trade summary: tem tudo numa linha
        # ----------------------------------------------------------------
        if ev_type == "trade_summary":
            state = ev.get("state") or trade_state or {}
            if not has_entry:
                ep = _sf(state.get("entry_price") or ev.get("entry_price"), 0.0)
                if ep > 0:
                    has_entry = True
                    t.entry_price = ep
                    t.entry_qty = _sf(state.get("entry_qty_filled") or ev.get("entry_qty_filled"), 0.0)
                    t.side = str(state.get("side") or ev.get("side") or "?")
                    t.entry_ts = _sf(state.get("entry_ts") or state.get("created_at"), 0.0)
                    t.setup_variant = str(state.get("setup_variant") or "standard")
            # exit
            xp = _sf(state.get("exit_price_posted") or state.get("exit_price") or ev.get("exit_price"), 0.0)
            xq = _sf(state.get("exit_qty_filled") or ev.get("exit_qty_filled"), 0.0)
            pnl = ev.get("pnl")
            if pnl is not None:
                t.pnl_usd = _sf(pnl)
            elif xp > 0 and xq > 0 and t.entry_price > 0:
                t.pnl_usd = round((xp - t.entry_price) * xq, 4)
            t.exit_price = xp
            t.exit_reason = str(state.get("last_reason") or ev.get("reason") or "")

        # ----------------------------------------------------------------
        # awaiting_redeem: resolução pelo mercado
        # ----------------------------------------------------------------
        if ev_type == "awaiting_redeem":
            state = ev.get("state") or trade_state or {}
            # campo pode ser side_won ou won dependendo da versão do runner
            side_won = ev.get("side_won")
            if side_won is None:
                side_won = ev.get("won")
            if side_won is None:
                side_won = trade_state.get("side_won") or trade_state.get("won")
            ep = _sf(state.get("entry_price") or t.entry_price, 0.0)
            qty = _sf(state.get("entry_qty_filled") or t.entry_qty, 0.0)
            if ep > 0 and qty > 0:
                if not has_entry:
                    has_entry = True
                    t.entry_price = ep
                    t.entry_qty = qty
                    t.side = str(state.get("side") or "?")
                    t.setup_variant = str(state.get("setup_variant") or "standard")
                # só sobrescreve PnL se não houver saída anterior (flat)
                # o runner já saiu pelo flat — redeem é apenas informativo
                if not t.exit_reason:
                    if side_won is True:
                        t.pnl_usd = round((1.0 - ep) * qty, 4)
                        t.exit_price = 1.0
                        t.exit_reason = "redeem_win"
                    elif side_won is False:
                        t.pnl_usd = round(-ep * qty, 4)
                        t.exit_price = 0.0
                        t.exit_reason = "redeem_loss"

        # ----------------------------------------------------------------
        # flat (saída de mercado secundário)
        # ----------------------------------------------------------------
        if ev_type in ("flat", "redeem_flat"):
            trade_data = ev.get("trade") or {}
            xord = ev.get("exit_order") or {}
            # formato real: trade.best_bid + trade.exit_qty_filled
            xp = _sf(
                trade_data.get("best_bid")
                or xord.get("price") or ev.get("exit_price") or ev.get("price"), 0.0
            )
            xq = _sf(
                trade_data.get("exit_qty_filled")
                or xord.get("size_matched") or ev.get("qty_filled") or t.entry_qty, 0.0
            )
            if xp > 0 and t.entry_price > 0:
                t.pnl_usd = round((xp - t.entry_price) * xq, 4)
                t.exit_price = xp
                # preserva last_reason para classificação correta
                t.exit_reason = trade_data.get("last_reason") or ev_type

    if not has_entry or t.entry_price <= 0:
        return None

    # classifica outcome
    if t.pnl_usd > 0.01:
        t.outcome = "win"
    elif t.pnl_usd < -0.01:
        t.outcome = "total_loss" if t.exit_price == 0.0 else "partial_loss"
    else:
        t.outcome = "flat"

    # garante setup_variant mínimo
    if not t.setup_variant or t.setup_variant == "unknown":
        t.setup_variant = "standard"

    return t


def _fill_entry_context(t: RealTrade, snap: dict) -> None:
    """Preenche condições de mercado a partir de um snapshot."""
    signal = snap.get("signal") or {}
    t.entry_secs = snap.get("current_secs")

    # setup_variant e score
    if t.setup_variant in ("unknown", "standard", ""):
        sv = signal.get("setup_variant")
        if sv:
            t.setup_variant = str(sv)

    t.entry_score = _sf(signal.get("score") or signal.get("signal_score"), 0.0)

    # bid decel gate para o lado entrado
    decel = snap.get("bid_decel_gate") or {}
    side_key = (t.side or "").lower()
    gate_val = decel.get(side_key) or decel.get(t.side)
    t.bid_decel_gate_active = bool(gate_val)

    # loser bid e counter price
    loser_tracker = snap.get("loser_bid_tracker") or {}
    t.loser_bid_at_entry = _sf(
        loser_tracker.get("loser_bid")
        or loser_tracker.get("current_price")
        or loser_tracker.get("price"), 0.0
    )

    # preço do lado contrário (counter)
    side = (t.side or "").upper()
    if side == "UP":
        counter = _sf(signal.get("down_buy") or signal.get("counter_buy"), 0.0)
    elif side == "DOWN":
        counter = _sf(signal.get("up_buy") or signal.get("counter_buy"), 0.0)
    else:
        counter = _sf(signal.get("counter_buy"), 0.0)
    t.counter_price_at_entry = counter


# ---------------------------------------------------------------------------
# Definição de variantes (filtros a testar)
# ---------------------------------------------------------------------------

@dataclass
class Variant:
    name: str
    description: str
    # Filtros — None significa "sem restrição"
    allowed_setup_variants: Optional[set[str]] = None     # ex: {"standard", "resolved_pullback_limit"}
    blocked_setup_variants: Optional[set[str]] = None     # ex: {"controlled_late_entry"}
    min_secs: Optional[int] = None                        # ex: 30 — só entra se secs >= 30
    max_secs: Optional[int] = None                        # ex: 80
    max_entry_price: Optional[float] = None               # ex: 0.97 — bloqueia entradas muito caras
    min_entry_price: Optional[float] = None               # ex: 0.90
    block_if_decel_gate: bool = False                     # bloqueia se bid_decel_gate ativo
    max_counter_price: Optional[float] = None             # ex: 0.08 — bloqueia se lado contrário alto
    max_loser_bid: Optional[float] = None                 # ex: 0.10

    def allows(self, trade: RealTrade) -> bool:
        if self.allowed_setup_variants and trade.setup_variant not in self.allowed_setup_variants:
            return False
        if self.blocked_setup_variants and trade.setup_variant in self.blocked_setup_variants:
            return False
        if self.min_secs is not None and (trade.entry_secs is None or trade.entry_secs < self.min_secs):
            return False
        if self.max_secs is not None and (trade.entry_secs is None or trade.entry_secs > self.max_secs):
            return False
        if self.max_entry_price is not None and trade.entry_price > self.max_entry_price:
            return False
        if self.min_entry_price is not None and trade.entry_price < self.min_entry_price:
            return False
        if self.block_if_decel_gate and trade.bid_decel_gate_active:
            return False
        if self.max_counter_price is not None and trade.counter_price_at_entry > self.max_counter_price:
            return False
        if self.max_loser_bid is not None and trade.loser_bid_at_entry > self.max_loser_bid:
            return False
        return True


def _build_variants() -> list[Variant]:
    """Constrói a matriz de variantes a testar."""
    variants: list[Variant] = [
        # --- baseline ---
        Variant("baseline", "Todos os trades reais (sem filtro)"),

        # --- por setup_variant ---
        Variant("only_standard", "Só setup_variant=standard",
                allowed_setup_variants={"standard"}),
        Variant("only_resolved_pullback", "Só resolved_pullback_limit",
                allowed_setup_variants={"resolved_pullback_limit"}),
        Variant("only_controlled_late", "Só controlled_late_entry",
                allowed_setup_variants={"controlled_late_entry"}),
        Variant("only_passive_extreme", "Só passive_extreme_liquidity_capture",
                allowed_setup_variants={"passive_extreme_liquidity_capture"}),
        Variant("no_controlled_late", "Bloqueia controlled_late_entry",
                blocked_setup_variants={"controlled_late_entry"}),
        Variant("no_resolved_pullback", "Bloqueia resolved_pullback_limit",
                blocked_setup_variants={"resolved_pullback_limit"}),
        Variant("standard_or_passive", "standard + passive_extreme",
                allowed_setup_variants={"standard", "passive_extreme_liquidity_capture"}),

        # --- por janela de tempo ---
        Variant("secs_30_to_95", "Apenas secs entre 30 e 95",
                min_secs=30, max_secs=95),
        Variant("secs_45_to_95", "Apenas secs entre 45 e 95",
                min_secs=45, max_secs=95),
        Variant("secs_60_to_95", "Apenas secs entre 60 e 95",
                min_secs=60, max_secs=95),
        Variant("secs_10_to_60", "Apenas secs entre 10 e 60",
                min_secs=10, max_secs=60),
        Variant("secs_10_to_45", "Apenas secs entre 10 e 45",
                min_secs=10, max_secs=45),
        Variant("secs_10_to_30", "Últimos 30s",
                min_secs=10, max_secs=30),

        # --- por preço de entrada ---
        Variant("price_max_097", "Preço de entrada <= 0.97",
                max_entry_price=0.97),
        Variant("price_max_095", "Preço de entrada <= 0.95",
                max_entry_price=0.95),
        Variant("price_max_093", "Preço de entrada <= 0.93",
                max_entry_price=0.93),
        Variant("price_090_to_097", "Entrada entre 0.90 e 0.97",
                min_entry_price=0.90, max_entry_price=0.97),
        Variant("price_090_to_095", "Entrada entre 0.90 e 0.95",
                min_entry_price=0.90, max_entry_price=0.95),

        # --- por bid_decel_gate ---
        Variant("no_decel_gate", "Bloqueia se bid_decel_gate ativo",
                block_if_decel_gate=True),

        # --- por preço do counter ---
        Variant("counter_max_008", "Counter price <= 0.08",
                max_counter_price=0.08),
        Variant("counter_max_006", "Counter price <= 0.06",
                max_counter_price=0.06),
        Variant("counter_max_010", "Counter price <= 0.10",
                max_counter_price=0.10),

        # --- por loser_bid ---
        Variant("loser_max_008", "Loser bid <= 0.08",
                max_loser_bid=0.08),
        Variant("loser_max_010", "Loser bid <= 0.10",
                max_loser_bid=0.10),
        Variant("loser_max_012", "Loser bid <= 0.12",
                max_loser_bid=0.12),

        # --- combinações ---
        Variant("std_30s_097", "standard + secs>=30 + price<=0.97",
                allowed_setup_variants={"standard"},
                min_secs=30, max_entry_price=0.97),
        Variant("std_45s_097", "standard + secs>=45 + price<=0.97",
                allowed_setup_variants={"standard"},
                min_secs=45, max_entry_price=0.97),
        Variant("std_30s_nodecel", "standard + secs>=30 + sem decel gate",
                allowed_setup_variants={"standard"},
                min_secs=30, block_if_decel_gate=True),
        Variant("std_45s_nodecel", "standard + secs>=45 + sem decel gate",
                allowed_setup_variants={"standard"},
                min_secs=45, block_if_decel_gate=True),
        Variant("price_097_nodecel", "price<=0.97 + sem decel gate",
                max_entry_price=0.97, block_if_decel_gate=True),
        Variant("price_097_counter006", "price<=0.97 + counter<=0.06",
                max_entry_price=0.97, max_counter_price=0.06),
        Variant("std_30s_097_nodecel", "standard + secs>=30 + price<=0.97 + sem decel",
                allowed_setup_variants={"standard"},
                min_secs=30, max_entry_price=0.97, block_if_decel_gate=True),
        Variant("30s_097_counter006", "secs>=30 + price<=0.97 + counter<=0.06",
                min_secs=30, max_entry_price=0.97, max_counter_price=0.06),
        Variant("30s_097_loser010", "secs>=30 + price<=0.97 + loser<=0.10",
                min_secs=30, max_entry_price=0.97, max_loser_bid=0.10),
        Variant("30s_nodecel_counter006", "secs>=30 + sem decel + counter<=0.06",
                min_secs=30, block_if_decel_gate=True, max_counter_price=0.06),
        Variant("std_60s_095_nodecel", "standard + secs>=60 + price<=0.95 + sem decel",
                allowed_setup_variants={"standard"},
                min_secs=60, max_entry_price=0.95, block_if_decel_gate=True),
        Variant("most_strict", "standard + secs>=45 + price<=0.95 + sem decel + counter<=0.06",
                allowed_setup_variants={"standard"},
                min_secs=45, max_entry_price=0.95, block_if_decel_gate=True, max_counter_price=0.06),
    ]
    return variants


# ---------------------------------------------------------------------------
# Aplicação das variantes e relatório
# ---------------------------------------------------------------------------

@dataclass
class VariantResult:
    variant: Variant
    total: int = 0
    allowed: int = 0
    blocked: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    pnl_usd: float = 0.0
    pnl_wins: float = 0.0
    pnl_losses: float = 0.0


def _apply_variants(trades: list[RealTrade], variants: list[Variant]) -> list[VariantResult]:
    results = []
    for v in variants:
        vr = VariantResult(variant=v, total=len(trades))
        for t in trades:
            if v.allows(t):
                vr.allowed += 1
                vr.pnl_usd += t.pnl_usd
                if t.outcome == "win":
                    vr.wins += 1
                    vr.pnl_wins += t.pnl_usd
                elif t.outcome in ("loss", "partial_loss", "total_loss"):
                    vr.losses += 1
                    vr.pnl_losses += t.pnl_usd
                else:
                    vr.flats += 1
            else:
                vr.blocked += 1
        results.append(vr)
    return results


def _print_report(
    trades: list[RealTrade],
    results: list[VariantResult],
    min_pnl: float,
    top_n: int,
) -> None:
    # Ordena por PnL DESC
    results.sort(key=lambda r: r.pnl_usd, reverse=True)

    print("\n" + "=" * 80)
    print("  ANÁLISE DE VARIANTES — current_almost_resolved REAL")
    print("=" * 80)
    print(f"  Total de trades reais reconstruídos: {len(trades)}")

    if not trades:
        print("\n  Nenhum trade encontrado. Verifique o caminho dos logs.")
        print("  Use: python analyze_real_variants.py --log-dir <caminho>")
        print("  Exemplo: python analyze_real_variants.py --log-dir /outro_pc/logs/current_almost_resolved_real_20260601_120000")
        return

    # Sumário geral dos trades reais
    wins_all   = [t for t in trades if t.outcome == "win"]
    losses_all = [t for t in trades if t.outcome in ("loss", "partial_loss", "total_loss")]
    pnl_all    = sum(t.pnl_usd for t in trades)

    print(f"\n  Wins:   {len(wins_all)} | soma={sum(t.pnl_usd for t in wins_all):+.4f}")
    print(f"  Losses: {len(losses_all)} | soma={sum(t.pnl_usd for t in losses_all):+.4f}")
    print(f"  PnL total (baseline): {pnl_all:+.4f} USDC")

    # Setup variants presentes
    sv_counts: dict[str, int] = defaultdict(int)
    for t in trades:
        sv_counts[t.setup_variant] += 1
    print(f"\n  Setup variants encontrados:")
    for sv, cnt in sorted(sv_counts.items(), key=lambda x: -x[1]):
        print(f"    {sv:40s} {cnt:4d} trades")

    # Distribuição de secs
    secs_known = [t.entry_secs for t in trades if t.entry_secs is not None]
    if secs_known:
        print(f"\n  Distribuição de secs_to_end na entrada:")
        print(f"    min={min(secs_known)}  max={max(secs_known)}  "
              f"mediana={sorted(secs_known)[len(secs_known)//2]}")

    print(f"\n{'=' * 80}")
    print(f"  RANKING DE VARIANTES (filtro: PnL >= {min_pnl:.2f}; top {top_n})")
    print(f"{'=' * 80}")

    header = (f"{'Variante':30s} {'Allow':>5} {'Blk':>5} {'W':>4} {'L':>4} "
              f"{'WR':>6} {'PnL':>9} {'ΔvsBase':>9}  Descrição")
    print(f"\n  {header}")
    print(f"  {'-' * 100}")

    baseline_pnl = results[0].pnl_usd if results[0].variant.name == "baseline" else pnl_all
    # mantém baseline no topo independente do filtro min_pnl
    shown = 0
    for vr in results:
        if vr.variant.name == "baseline":
            _print_row(vr, baseline_pnl)
            continue
        if vr.pnl_usd < min_pnl:
            continue
        if shown >= top_n:
            continue
        _print_row(vr, baseline_pnl)
        shown += 1

    # Trades individuais (para debug)
    print(f"\n{'=' * 80}")
    print(f"  TRADES RECONSTRUÍDOS (todos)")
    print(f"{'=' * 80}")
    print(f"\n  {'#':>3} {'Outcome':8} {'Variant':35s} {'Secs':>5} {'Price':>6} "
          f"{'PnL':>8} {'Decel':>5}  Slug")
    print(f"  {'-' * 100}")
    for i, t in enumerate(sorted(trades, key=lambda x: x.pnl_usd), 1):
        decel = "Y" if t.bid_decel_gate_active else "N"
        secs = str(t.entry_secs) if t.entry_secs is not None else "?"
        print(f"  {i:>3} {t.outcome:8s} {t.setup_variant:35s} {secs:>5} "
              f"{t.entry_price:6.3f} {t.pnl_usd:+8.4f} {decel:>5}  {t.slug[-40:]}")


def _print_row(vr: VariantResult, baseline_pnl: float) -> None:
    n = vr.allowed
    wr = f"{vr.wins / n * 100:.1f}%" if n > 0 else "  -  "
    delta = vr.pnl_usd - baseline_pnl
    delta_str = f"{delta:+.4f}" if vr.variant.name != "baseline" else "  base"
    print(f"  {vr.variant.name:30s} {n:5d} {vr.blocked:5d} {vr.wins:4d} {vr.losses:4d} "
          f"{wr:>6} {vr.pnl_usd:+9.4f} {delta_str:>9}  {vr.variant.description}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-dir", default=None,
                    help="Caminho para o diretório (ou glob) com logs reais. "
                         "Padrão: logs/current_almost_resolved_real_*/")
    ap.add_argument("--min-pnl", type=float, default=-999.0,
                    help="Exibe apenas variantes com PnL >= este valor (padrão: sem filtro)")
    ap.add_argument("--top", type=int, default=50,
                    help="Número máximo de variantes exibidas (padrão: 50)")
    args = ap.parse_args()

    print(f"Buscando logs reais...")
    files = _find_real_jsonl(args.log_dir)
    print(f"Arquivos encontrados: {len(files)}")
    for f in files:
        print(f"  {f}")

    print(f"\nReconstruindo trades...")
    trades = _parse_sessions(files)
    print(f"Trades reconstruídos: {len(trades)}")

    variants = _build_variants()
    print(f"Variantes a testar: {len(variants)}")

    results = _apply_variants(trades, variants)
    _print_report(trades, results, args.min_pnl, args.top)


if __name__ == "__main__":
    main()
