"""
analyze_hedge_impact_on_logs.py — Fase 1 da spec de hedge

Para cada evento de PERDA nos logs do current_almost_resolved:
  1. Encontra o primeiro sinal de invalidade APOS a entrada
     (book_gap, bid_decel_gate ativo, winner caindo)
  2. Estima o preco do lado perdedor nesse momento
  3. Calcula custo e perda travada do hedge hipotetico
  4. Compara com a perda real registrada

Emite veredicto final: HEDGE RECOMENDADO ou NAO RECOMENDADO.

Funciona em paper logs e logs reais do runner.

Uso:
  python analyze_hedge_impact_on_logs.py
  python analyze_hedge_impact_on_logs.py --logs-dir logs/
  python analyze_hedge_impact_on_logs.py --logs-dir C:/outro_pc/logs/ --output resultado.csv
  python analyze_hedge_impact_on_logs.py --logs-dir logs/current_almost_resolved_real_20260601_120000
"""
from __future__ import annotations

import argparse
import csv
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

PAPER_LOGS = [
    "dual_almost_paper_guardian_hybrid_v5/current_almost_resolved.jsonl",
    "dual_almost_paper_v2/current_almost_resolved.jsonl",
    "dual_almost_paper_v1/current_almost_resolved.jsonl",
    "current_almost_resolved_paper_resolved_pullback_safe_v2_3h_20260427_101148.jsonl",
    "current_almost_resolved_paper_resolved_pullback_safe_v2_1h.jsonl",
    "current_almost_resolved_paper_variant_1h.jsonl",
]

MAX_LOSER_BID_FOR_HEDGE = 0.40


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sf(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(d)


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


def _find_files(logs_dir: Optional[str]) -> list[tuple[Path, str]]:
    results: list[tuple[Path, str]] = []

    # sempre inclui paper logs locais
    for rel in PAPER_LOGS:
        p = LOGS_DIR / rel
        if p.exists():
            results.append((p, "paper"))

    if logs_dir:
        p = Path(logs_dir)
        if p.is_file() and p.suffix == ".jsonl":
            results.append((p, "real"))
        elif p.is_dir():
            c = p / "current_almost_resolved_real.jsonl"
            if c.exists():
                results.append((c, "real"))
            else:
                for sub in sorted(p.iterdir()):
                    if sub.is_dir() and sub.name.startswith(REAL_PREFIX):
                        rc = sub / "current_almost_resolved_real.jsonl"
                        if rc.exists():
                            results.append((rc, "real"))
                    elif sub.suffix == ".jsonl" and sub.name.startswith(REAL_PREFIX):
                        results.append((sub, "real"))
        else:
            for ep in sorted(glob.glob(str(logs_dir))):
                ep_path = Path(ep)
                if ep_path.is_file() and ep_path.suffix == ".jsonl":
                    results.append((ep_path, "real"))
                elif ep_path.is_dir():
                    rc = ep_path / "current_almost_resolved_real.jsonl"
                    if rc.exists():
                        results.append((rc, "real"))

    # deduplica mantendo ordem
    seen = set()
    deduped = []
    for item in results:
        key = str(item[0])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


# ---------------------------------------------------------------------------
# Estruturas de dados
# ---------------------------------------------------------------------------

@dataclass
class PositionSnap:
    """Estado do mercado num instante durante a posicao aberta."""
    secs: int
    winner_bid: float      # bid do lado que entramos
    loser_bid: float       # bid do lado contrario (estimado ou real)
    active_bid: float      # active_bid do runner (se disponivel)
    decel_gate_active: bool
    loser_tracker_price: float


@dataclass
class LossTrade:
    slug: str
    source: str
    side: str
    entry_price: float
    entry_qty: float
    exit_price: float
    loss_real: float       # perda real (negativo)
    exit_reason: str
    setup_variant: str

    # snapshots durante posicao aberta (cronologicos, do mais recente ao mais antigo)
    snaps: list[PositionSnap] = field(default_factory=list)

    # resultado da analise
    trigger_type: str = "none"          # book_gap | bid_decel_gate | winner_drop | no_signal
    trigger_secs: Optional[int] = None
    loser_bid_at_trigger: float = 0.0
    hedge_viable: bool = False
    custo_hedge: float = 0.0
    max_loss_locked: float = 0.0        # positivo = perda travada, negativo = lucro
    impacto_hedge: float = 0.0          # loss_real - max_loss_locked (quanto o hedge teria poupado)


# ---------------------------------------------------------------------------
# Parsing de logs
# ---------------------------------------------------------------------------

def _parse_file(path: Path, source: str) -> list[LossTrade]:
    events = list(_iter_jsonl(path))
    if not events:
        return []

    all_types = {ev.get("type", "") for ev in events}

    # Real runner: fill events have trade.entry_qty_filled; paper fills don't
    has_real_fill = any(
        ev.get("type") == "fill" and isinstance(ev.get("trade"), dict)
        for ev in events
    )
    if has_real_fill or "entry_filled" in all_types or "trade_summary" in all_types:
        return _parse_real(events, path.stem, source)
    elif "fill" in all_types or "enter" in all_types:
        return _parse_paper(events, path.stem, source)
    else:
        return _parse_real(events, path.stem, source)


# ---- Paper format ----------------------------------------------------------

def _parse_paper(events: list[dict], session: str, source: str) -> list[LossTrade]:
    losses: list[LossTrade] = []
    current_slug: Optional[str] = None
    current_side: Optional[str] = None
    current_entry_price: float = 0.0
    current_entry_qty: float = 0.0
    current_sv: str = "standard"
    current_snaps: list[PositionSnap] = []
    in_position = False

    for ev in events:
        t = ev.get("type", "")
        sig = ev.get("signal") or {}
        tr = ev.get("trade") or {}

        if t == "snapshot":
            slug = ev.get("current_slug")
            if slug:
                current_slug = slug
            mode = str(tr.get("mode") or "idle").lower()
            secs = ev.get("current_secs")
            up_buy = _sf(sig.get("up_buy") or sig.get("up_bid"), 0.0)
            down_buy = _sf(sig.get("down_buy") or sig.get("down_bid"), 0.0)

            if mode in ("open", "open_position", "pending_exit") and in_position:
                if secs is not None and (up_buy > 0 or down_buy > 0):
                    side = current_side or "?"
                    winner = up_buy if side == "UP" else down_buy
                    loser = down_buy if side == "UP" else up_buy

                    # active_bid / decel_gate / loser_tracker (normalmente None em paper)
                    active_bid = _sf(ev.get("active_bid"), 0.0)
                    decel_raw = ev.get("bid_decel_gate") or {}
                    decel_side_val = decel_raw.get(side.lower()) if isinstance(decel_raw, dict) else None
                    decel_active = bool(decel_side_val)
                    loser_tracker = ev.get("loser_bid_tracker") or {}
                    loser_tracker_price = _sf(
                        loser_tracker.get("current_price") or loser_tracker.get("price"), 0.0
                    )

                    current_snaps.append(PositionSnap(
                        secs=int(secs),
                        winner_bid=winner,
                        loser_bid=loser if loser > 0 else max(0.0, 1.0 - winner),
                        active_bid=active_bid,
                        decel_gate_active=decel_active,
                        loser_tracker_price=loser_tracker_price,
                    ))

        elif t == "fill":
            fill_qty = _sf(ev.get("fill_qty"), 0.0)
            ep = _sf(sig.get("entry_price") or ev.get("entry_price"), 0.0)
            side = str(sig.get("side") or ev.get("side") or "?")
            sv = str(sig.get("setup_variant") or "standard")
            if ep > 0 and fill_qty > 0:
                in_position = True
                current_side = side
                current_entry_price = ep
                current_entry_qty = fill_qty
                current_sv = sv
                current_snaps = []

        elif t == "exit":
            pnl_quote = _sf(tr.get("pnl_quote"), 0.0)
            pnl_ticks = _sf(tr.get("pnl_ticks"), 0.0)
            qty = _sf(tr.get("qty"), 0.0)
            exit_price = _sf(tr.get("exit_price"), 0.0)
            exit_reason = str(tr.get("exit_reason") or "")
            ep_from_exit = _sf(tr.get("entry_price"), 0.0)
            side_from_exit = str(tr.get("side") or current_side or "?")
            sv_from_exit = str(tr.get("setup_variant") or current_sv or "standard")

            if pnl_quote == 0.0 and pnl_ticks != 0.0:
                effective_qty = qty if qty > 0 else current_entry_qty
                pnl_quote = pnl_ticks * 0.01 * effective_qty

            is_loss = pnl_quote < -0.001 or (
                pnl_quote == 0.0 and "stop" in exit_reason.lower()
                and "structural" not in exit_reason.lower()
            )

            if is_loss:
                entry_price = ep_from_exit if ep_from_exit > 0 else current_entry_price
                entry_qty = qty if qty > 0 else current_entry_qty
                lt = LossTrade(
                    slug=current_slug or session,
                    source=source,
                    side=side_from_exit,
                    entry_price=entry_price,
                    entry_qty=entry_qty,
                    exit_price=exit_price,
                    loss_real=round(pnl_quote, 4),
                    exit_reason=exit_reason,
                    setup_variant=sv_from_exit,
                    snaps=list(current_snaps),
                )
                losses.append(lt)

            in_position = False
            current_snaps = []
            current_side = None
            current_entry_price = 0.0
            current_entry_qty = 0.0

    return losses


# ---- Real runner format ----------------------------------------------------

def _parse_real(events: list[dict], session: str, source: str) -> list[LossTrade]:
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

    losses: list[LossTrade] = []
    for slug, evs in slug_events.items():
        evs.sort(key=lambda e: _sf(e.get("ts") or e.get("timestamp"), 0))
        result = _reconstruct_real_loss(slug, session, source, evs)
        if result is not None:
            losses.append(result)
    return losses


def _reconstruct_real_loss(slug: str, session: str, source: str, evs: list[dict]) -> Optional[LossTrade]:
    entry_price = 0.0
    entry_qty = 0.0
    side = "?"
    sv = "standard"
    has_entry = False
    snaps: list[PositionSnap] = []
    pnl_quote = 0.0
    exit_price = 0.0
    exit_reason = ""

    for ev in evs:
        t = ev.get("type", "")
        sig = ev.get("signal") or {}
        tr = ev.get("trade") or {}

        if t == "snapshot":
            mode = str(tr.get("mode") or "idle").lower()
            secs = ev.get("current_secs")
            up_buy = _sf(sig.get("up_buy"), 0.0)
            down_buy = _sf(sig.get("down_buy"), 0.0)

            if mode in ("open_position", "pending_exit") and has_entry:
                if secs is not None:
                    winner_bid = up_buy if side == "UP" else down_buy
                    loser_bid_sig = down_buy if side == "UP" else up_buy
                    active_bid = _sf(ev.get("active_bid"), 0.0)

                    # loser bid: usa sinal real se disponivel, senao estima
                    loser_bid = loser_bid_sig if loser_bid_sig > 0 else max(0.0, 1.0 - (active_bid or winner_bid))

                    decel_raw = ev.get("bid_decel_gate") or {}
                    if isinstance(decel_raw, dict):
                        decel_val = decel_raw.get(side.lower()) or decel_raw.get(side)
                        decel_active = bool(decel_val)
                    else:
                        decel_active = bool(decel_raw)

                    loser_tracker = ev.get("loser_bid_tracker") or {}
                    ltp = _sf(loser_tracker.get("current_price") or loser_tracker.get("price"), 0.0)

                    snaps.append(PositionSnap(
                        secs=int(secs),
                        winner_bid=winner_bid if winner_bid > 0 else active_bid,
                        loser_bid=loser_bid,
                        active_bid=active_bid,
                        decel_gate_active=decel_active,
                        loser_tracker_price=ltp,
                    ))

        elif t in ("entry_filled", "entry_confirmed", "position_opened", "fill"):
            ep = _sf(ev.get("entry_price") or ev.get("fill_price") or tr.get("entry_price"), 0.0)
            qty = _sf(ev.get("qty_filled") or ev.get("qty") or tr.get("entry_qty_filled"), 0.0)
            if ep > 0 and qty > 0 and not has_entry:
                has_entry = True
                entry_price = ep
                entry_qty = qty
                side = str(ev.get("side") or tr.get("side") or sig.get("side") or "?")
                sv = str(tr.get("setup_variant") or sig.get("setup_variant") or "standard")

        elif t in ("flat", "redeem_flat"):
            exit_ord = ev.get("exit_order") or {}
            xp = _sf(exit_ord.get("price") or tr.get("target_price"), 0.0)
            xq = _sf(exit_ord.get("size_matched") or tr.get("exit_qty_filled"), 0.0)
            exit_reason = str(tr.get("last_reason") or "")
            if xp > 0 and xq > 0 and entry_price > 0:
                pnl_quote = round((xp - entry_price) * xq, 4)
            exit_price = xp

        elif t == "trade_summary":
            state = ev.get("state") or tr or {}
            if not has_entry:
                ep = _sf(state.get("entry_price") or ev.get("entry_price"), 0.0)
                if ep > 0:
                    has_entry = True
                    entry_price = ep
                    entry_qty = _sf(state.get("entry_qty_filled"), 0.0)
                    side = str(state.get("side") or "?")
                    sv = str(state.get("setup_variant") or "standard")
            p = ev.get("pnl")
            if p is not None:
                pnl_quote = _sf(p)
            else:
                xp = _sf(state.get("exit_price_posted") or state.get("exit_price"), 0.0)
                xq = _sf(state.get("exit_qty_filled"), 0.0)
                if xp > 0 and xq > 0 and entry_price > 0:
                    pnl_quote = round((xp - entry_price) * xq, 4)
            exit_price = _sf(state.get("exit_price_posted") or state.get("exit_price"), 0.0)
            exit_reason = str(state.get("last_reason") or ev.get("reason") or "")

        elif t == "awaiting_redeem":
            state = ev.get("state") or tr or {}
            side_won = ev.get("side_won")
            ep = _sf(state.get("entry_price") or entry_price, 0.0)
            qty = _sf(state.get("entry_qty_filled") or entry_qty, 0.0)
            if ep > 0 and qty > 0:
                if not has_entry:
                    has_entry = True
                    entry_price = ep
                    entry_qty = qty
                    side = str(state.get("side") or "?")
                if side_won is False:
                    pnl_quote = round(-ep * qty, 4)
                    exit_price = 0.0
                    exit_reason = "redeem_loss"
                elif side_won is True:
                    pnl_quote = round((1.0 - ep) * qty, 4)
                    exit_price = 1.0
                    exit_reason = "redeem_win"

    if not has_entry or entry_price <= 0:
        return None

    is_loss = pnl_quote < -0.001 or (
        pnl_quote == 0.0 and "stop" in exit_reason.lower()
        and "structural" not in exit_reason.lower()
    )
    if not is_loss:
        return None

    return LossTrade(
        slug=slug, source=source, side=side,
        entry_price=entry_price, entry_qty=entry_qty,
        exit_price=exit_price,
        loss_real=round(pnl_quote, 4),
        exit_reason=exit_reason,
        setup_variant=sv,
        snaps=snaps,
    )


# ---------------------------------------------------------------------------
# Deteccao de sinal de invalidade e calculo do hedge
# ---------------------------------------------------------------------------

def _find_trigger(lt: LossTrade) -> None:
    """
    Encontra o primeiro sinal de invalidade nos snapshots e calcula o hedge.
    Modifica lt in-place.
    """
    if not lt.snaps:
        lt.trigger_type = "no_signal"
        return

    # consecutivos com active_bid/winner_bid == 0 (book gap)
    gap_count = 0
    prev_winner = None

    for i, snap in enumerate(lt.snaps):
        winner = snap.active_bid if snap.active_bid > 0 else snap.winner_bid

        # --- Gatilho A: book gap (winner_bid = 0 por 2+ polls) ---
        if winner == 0.0:
            gap_count += 1
            if gap_count >= 2:
                lt.trigger_type = "book_gap"
                lt.trigger_secs = snap.secs
                lt.loser_bid_at_trigger = snap.loser_bid if snap.loser_bid > 0 else (1.0 - (prev_winner or lt.entry_price))
                break
        else:
            gap_count = 0
            prev_winner = winner

        # --- Gatilho B: bid_decel_gate ativo ---
        if snap.decel_gate_active and lt.trigger_type == "none":
            lt.trigger_type = "bid_decel_gate"
            lt.trigger_secs = snap.secs
            loser = snap.loser_tracker_price if snap.loser_tracker_price > 0 else snap.loser_bid
            lt.loser_bid_at_trigger = loser if loser > 0 else max(0.001, 1.0 - winner)

        # --- Gatilho C: winner caindo significativamente do entry (>= 3 bps) ---
        if (lt.trigger_type == "none" and lt.entry_price > 0
                and winner > 0 and (lt.entry_price - winner) >= 0.03):
            lt.trigger_type = "winner_drop"
            lt.trigger_secs = snap.secs
            loser = snap.loser_bid if snap.loser_bid > 0 else max(0.001, 1.0 - winner)
            lt.loser_bid_at_trigger = loser

        if lt.trigger_type != "none":
            break

    if lt.trigger_type == "none":
        # sem trigger detectado nos snapshots
        lt.trigger_type = "no_signal"
        return

    # --- Calculo do hedge ---
    loser_bid = lt.loser_bid_at_trigger
    lt.hedge_viable = 0 < loser_bid <= MAX_LOSER_BID_FOR_HEDGE

    if lt.hedge_viable and lt.entry_qty > 0:
        shares = lt.entry_qty
        lt.custo_hedge = round(shares * loser_bid, 4)
        custo_total = round(lt.entry_price * shares + lt.custo_hedge, 4)
        receita_maxima = shares * 1.0
        lt.max_loss_locked = round(custo_total - receita_maxima, 4)
        # impacto: quanto teria poupado (positivo = hedge ajudou)
        lt.impacto_hedge = round(abs(lt.loss_real) - lt.max_loss_locked, 4)


# ---------------------------------------------------------------------------
# Relatorio
# ---------------------------------------------------------------------------

def _print_report(losses: list[LossTrade]) -> None:
    viable = [lt for lt in losses if lt.hedge_viable]
    inviavel = [lt for lt in losses if lt.trigger_type != "no_signal" and not lt.hedge_viable]
    no_signal = [lt for lt in losses if lt.trigger_type == "no_signal"]

    sep = "=" * 54

    print(f"\n{sep}")
    print(f"  ANALISE DE IMPACTO DO HEDGE")
    print(f"{sep}")
    print(f"  Total de eventos de perda analisados: {len(losses)}")
    print()

    print(f"  Hedge viavel   (loser_bid <= 0.40): {len(viable)} ({len(viable)/max(1,len(losses))*100:.0f}%)")
    print(f"  Hedge inviavel (loser_bid >  0.40): {len(inviavel)} ({len(inviavel)/max(1,len(losses))*100:.0f}%)")
    print(f"  Sem sinal detectavel (sem snapshots): {len(no_signal)}")
    print()

    if viable:
        perdas_sem = [abs(lt.loss_real) for lt in viable]
        perdas_com = [lt.max_loss_locked for lt in viable]
        reducoes   = [lt.impacto_hedge for lt in viable]
        pct_poupou = sum(1 for r in reducoes if r > 0) / len(reducoes) * 100

        print(f"  Nos eventos onde hedge era viavel ({len(viable)}):")
        print(f"    Perda media SEM hedge:       -${sum(perdas_sem)/len(perdas_sem):.2f}")
        print(f"    Perda media COM hedge:       -${sum(perdas_com)/len(perdas_com):.2f}")
        print(f"    Reducao media de perda:       ${sum(reducoes)/len(reducoes):.2f}")
        print(f"    Melhor caso (menor perda):   -${min(perdas_com):.2f}")
        print(f"    Pior caso (hedge piorou):    -${max(perdas_com):.2f}")
        print(f"    Hedge ajudou em {pct_poupou:.0f}% dos casos")
        print()

        # breakdown por trigger
        triggers = {}
        for lt in viable:
            triggers.setdefault(lt.trigger_type, []).append(lt.impacto_hedge)
        print(f"  Breakdown por trigger:")
        for trig, impactos in sorted(triggers.items()):
            print(f"    {trig:20s}: {len(impactos)} eventos | impacto medio ${sum(impactos)/len(impactos):.2f}")
        print()

        # detalhe por trade
        print(f"  Detalhe dos trades viáveis:")
        print(f"    {'Slug':35s} {'Side':4} {'Entry':6} {'Loser@trig':10} "
              f"{'LossReal':9} {'MaxLoss':9} {'Poupou':8}  Trigger")
        print(f"    {'-' * 100}")
        for lt in sorted(viable, key=lambda x: x.impacto_hedge, reverse=True):
            print(f"    {lt.slug[-35:]:35s} {lt.side:4} {lt.entry_price:6.3f} "
                  f"{lt.loser_bid_at_trigger:10.3f} "
                  f"{lt.loss_real:+9.2f} {-lt.max_loss_locked:+9.2f} "
                  f"{lt.impacto_hedge:+8.2f}  {lt.trigger_type}@secs={lt.trigger_secs}")

        print()

        # veredicto
        reducao_media = sum(reducoes) / len(reducoes)
        print(f"{sep}")
        if reducao_media >= 10.0 and pct_poupou >= 60.0:
            print(f"  VEREDICTO: HEDGE RECOMENDADO")
            print(f"  Reducao media >= $10 em >= 60% dos casos viaveis.")
            print(f"  Prosseguir com Fase 2 — logging no runner.")
        else:
            print(f"  VEREDICTO: HEDGE NAO RECOMENDADO")
            if reducao_media < 10.0:
                print(f"  Reducao media ({reducao_media:.2f}) abaixo de $10.")
            if pct_poupou < 60.0:
                print(f"  Hedge ajudou em menos de 60% dos casos ({pct_poupou:.0f}%).")
            print(f"  Nao implementar no runner com esses dados.")
        print(f"{sep}")

    else:
        print(f"  Nenhum evento viavel encontrado.")
        print(f"  Hedge nao pode ser avaliado com os logs disponíveis.")
        if no_signal:
            print(f"  {len(no_signal)} trades perdedores nao tinham snapshots de posicao aberta.")
            print(f"  Rodar novamente com logs reais (poll 0.5s) para cobertura completa.")
        print(f"\n{sep}")
        print(f"  VEREDICTO: DADOS INSUFICIENTES — aguardar logs reais")
        print(f"{sep}")


def _write_csv(losses: list[LossTrade], output_path: str) -> None:
    fields = [
        "timestamp_slug", "event_slug", "source", "setup_variant",
        "side", "entry_price", "entry_qty", "exit_price",
        "loss_real", "exit_reason",
        "trigger_type", "trigger_secs",
        "loser_bid_at_signal", "custo_hedge",
        "max_loss_locked", "impacto_hedge",
        "hedge_viable",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for lt in losses:
            w.writerow({
                "timestamp_slug": lt.slug,
                "event_slug": lt.slug,
                "source": lt.source,
                "setup_variant": lt.setup_variant,
                "side": lt.side,
                "entry_price": lt.entry_price,
                "entry_qty": lt.entry_qty,
                "exit_price": lt.exit_price,
                "loss_real": lt.loss_real,
                "exit_reason": lt.exit_reason,
                "trigger_type": lt.trigger_type,
                "trigger_secs": lt.trigger_secs or "",
                "loser_bid_at_signal": round(lt.loser_bid_at_trigger, 4),
                "custo_hedge": round(lt.custo_hedge, 4),
                "max_loss_locked": round(lt.max_loss_locked, 4),
                "impacto_hedge": round(lt.impacto_hedge, 4),
                "hedge_viable": lt.hedge_viable,
            })
    print(f"\n  CSV salvo em: {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--logs-dir", default=None,
                    help="Diretório com logs JSONL (real ou paper). Padrão: logs/")
    ap.add_argument("--output", default=None,
                    help="Arquivo CSV de saída. Padrão: nao gera CSV")
    args = ap.parse_args()

    files = _find_files(args.logs_dir)
    print(f"Arquivos encontrados: {len(files)}")
    for p, src in files:
        print(f"  [{src}] {p}")

    all_losses: list[LossTrade] = []
    for path, source in files:
        batch = _parse_file(path, source)
        all_losses.extend(batch)

    print(f"\nTrades perdedores encontrados: {len(all_losses)}")

    for lt in all_losses:
        _find_trigger(lt)

    _print_report(all_losses)

    if args.output:
        _write_csv(all_losses, args.output)


if __name__ == "__main__":
    main()
