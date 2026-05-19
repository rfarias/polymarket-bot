"""
analyze_reversal_candidates_on_logs.py

Fase 0 do reversal_sniper: análise retroativa dos logs do current_almost_resolved
para validar se reversões reais ocorrem nos eventos de perda e bloqueio.

O que é uma "reversão":
    O runner aposta no lado "quase vencedor" (active_bid >= 0.88).
    Reversão = esse lado perdeu → o lado "perdedor barato" venceu.
    Proxy nos logs: trade do runner terminou com PnL < 0 (stop atingido,
    book collapse, ou resolução adversa).

Saídas:
    - Resumo estatístico de reversões confirmadas
    - Distribuição de preços do lado perdedor no momento do sinal
    - PnL hipotético do reversal_sniper em cada reversão
    - Análise de sinais presentes nos momentos de reversão (bid_decel, velocity, etc.)
    - JSONL com todos os candidatos: logs/reversal_candidates_YYYYMMDD.jsonl

Uso:
    python analyze_reversal_candidates_on_logs.py
    python analyze_reversal_candidates_on_logs.py --real-only
    python analyze_reversal_candidates_on_logs.py --min-bid 0.85
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8")

BET_SIZE = 20.0          # $ simulados por trade
MIN_ACTIVE_BID = 0.88    # lado vencedor precisa estar neste patamar ou acima
MAX_LOSER_PRICE = 0.15   # loser já subiu demais, edge foi embora
MIN_SECS = 15
MAX_SECS = 120

EXIT_REAL = {"flat", "redeem_flat", "redeem_dust_archived", "external_close_detected"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sf(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


def _pnl_ticks(entry: float, exit_posted: str_or_none, last_reason: str) -> Optional[float]:
    xp = _sf(exit_posted)
    if xp > 0 and any(t in last_reason for t in ("near_win_exit", "oracle_margin_exit")):
        m = re.search(r":bid=([0-9.]+)", last_reason)
        if m:
            xp = float(m.group(1))
    ep = _sf(entry)
    if ep > 0 and xp > 0:
        return round((xp - ep) / 0.01, 1)
    return None


# ---------------------------------------------------------------------------
# Per-slug candidate record
# ---------------------------------------------------------------------------

class SlugRecord:
    """Acumula dados de um slug em uma sessão."""

    def __init__(self, slug: str, session: str):
        self.slug = slug
        self.session = session
        # Snapshot/signal data em momentos quase-resolvidos (active_bid >= MIN)
        self.snapshots: List[dict] = []      # {ts, active_bid, winner_side, loser_price, secs, decel, vel}
        # Entradas do runner (fill events)
        self.runner_side: Optional[str] = None
        self.runner_entry_price: Optional[float] = None
        self.runner_entry_ts: Optional[float] = None
        self.runner_active_bid_at_entry: Optional[float] = None
        self.runner_bid_decel_at_entry: Optional[dict] = None
        # Saída do runner
        self.runner_pnl_ticks: Optional[float] = None
        self.runner_exit_reason: Optional[str] = None
        self.runner_exit_ts: Optional[float] = None
        # Entradas bloqueadas
        self.blocks: List[dict] = []        # {ts, reason, secs, active_bid, loser_price, decel}
        # Estado open para detectar fill
        self._pending_entry = False
        self._pending_fill = False


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_session(path: Path) -> List[SlugRecord]:
    session = path.parent.name
    records: Dict[str, SlugRecord] = {}

    def _rec(slug: str) -> SlugRecord:
        if slug not in records:
            records[slug] = SlugRecord(slug, session)
        return records[slug]

    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue

            etype = ev.get("type")
            ts = _sf(ev.get("ts"))
            sig = ev.get("signal") or {}
            trade = ev.get("trade") or {}

            # ---- snapshot ------------------------------------------------
            if etype == "snapshot":
                slug = str(ev.get("current_slug") or trade.get("event_slug") or "")
                if not slug:
                    continue

                active_bid = _sf(ev.get("active_bid"))
                secs = _sf(ev.get("current_secs") or sig.get("secs_to_end"))

                if active_bid < MIN_ACTIVE_BID or not (MIN_SECS <= secs <= MAX_SECS):
                    continue

                # Determinar lado vencedor
                up_buy = _sf(sig.get("up_buy"))
                down_buy = _sf(sig.get("down_buy"))
                winner_side = "UP" if up_buy >= down_buy else "DOWN"
                if up_buy == 0 and down_buy == 0:
                    continue
                winner_bid = max(up_buy, down_buy)
                loser_price = round(1.0 - winner_bid, 4)

                if loser_price > MAX_LOSER_PRICE or loser_price <= 0:
                    continue

                decel_raw = ev.get("bid_decel_gate") or {}
                decel_side = decel_raw.get("up") if winner_side == "UP" else decel_raw.get("down")
                decel_would_block = bool(decel_side.get("would_block")) if decel_side else False
                bid_velocity = _sf(decel_side.get("bid_velocity"), float("nan")) if decel_side else float("nan")

                _rec(slug).snapshots.append({
                    "ts": ts,
                    "active_bid": winner_bid,
                    "winner_side": winner_side,
                    "loser_price": loser_price,
                    "secs": secs,
                    "decel_would_block": decel_would_block,
                    "bid_velocity": bid_velocity,
                    "range15s": _sf(sig.get("market_range_15s")),
                    "dist_ptb_bps": _sf(sig.get("distance_to_price_to_beat_bps")),
                    "oracle_price": _sf(ev.get("oracle_price")),
                    "oracle_open": _sf(ev.get("oracle_open_price")),
                })

            # ---- enter ---------------------------------------------------
            elif etype == "enter":
                slug = str(sig.get("event_slug") or trade.get("event_slug") or "")
                if not slug:
                    continue
                r = _rec(slug)
                r.runner_side = sig.get("side") or trade.get("side")
                r.runner_entry_price = _sf(sig.get("entry_price") or trade.get("entry_price"))
                r.runner_entry_ts = ts
                r.runner_active_bid_at_entry = _sf(ev.get("active_bid") or sig.get(
                    "up_buy" if r.runner_side == "UP" else "down_buy"))
                r.runner_bid_decel_at_entry = ev.get("bid_decel_gate")
                r._pending_fill = True

            # ---- fill ----------------------------------------------------
            elif etype == "fill":
                slug = str(trade.get("event_slug") or "")
                if not slug:
                    continue
                # fill confirma entrada — sem ação adicional necessária

            # ---- exit (flat / redeem / etc.) -----------------------------
            elif etype in EXIT_REAL:
                slug = str(trade.get("event_slug") or "")
                if not slug or slug not in records:
                    continue
                r = records[slug]
                ep = _sf(trade.get("entry_price") or r.runner_entry_price)
                xp = trade.get("exit_price_posted")
                lr = str(trade.get("last_reason") or "")
                qty = _sf(trade.get("entry_qty_filled"))
                if qty < 0.01:
                    continue  # nunca preencheu — não conta
                r.runner_pnl_ticks = _pnl_ticks(ep, xp, lr)
                r.runner_exit_reason = lr
                r.runner_exit_ts = ts

            # ---- entry_blocked -------------------------------------------
            elif etype == "entry_blocked":
                slug = str(sig.get("event_slug") or trade.get("event_slug") or "")
                if not slug:
                    continue

                up_buy = _sf(sig.get("up_buy"))
                down_buy = _sf(sig.get("down_buy"))
                winner_bid = max(up_buy, down_buy)
                loser_price = round(1.0 - winner_bid, 4)
                secs = _sf(sig.get("secs_to_end"))
                reason = str(ev.get("reason") or "")

                decel_raw = ev.get("bid_decel_gate") or {}
                winner_side = "UP" if up_buy >= down_buy else "DOWN"
                decel_side = decel_raw.get("up") if winner_side == "UP" else decel_raw.get("down")

                _rec(slug).blocks.append({
                    "ts": ts,
                    "reason": reason,
                    "secs": secs,
                    "active_bid": winner_bid,
                    "loser_price": loser_price,
                    "winner_side": winner_side,
                    "decel_would_block": bool(decel_side.get("would_block")) if decel_side else False,
                    "bid_velocity": _sf(decel_side.get("bid_velocity"), float("nan")) if decel_side else float("nan"),
                })

    return list(records.values())


# ---------------------------------------------------------------------------
# Classify records
# ---------------------------------------------------------------------------

def _classify(rec: SlugRecord) -> Optional[dict]:
    """
    Retorna um dict com o resultado do candidato, ou None se irrelevante.

    reversal_occurred = True  → runner perdeu (lado perdedor teria vencido)
    reversal_occurred = False → runner ganhou (sem reversão)
    reversal_occurred = None  → runner não entrou no mercado (apenas bloqueios/snapshots)
    """
    if not rec.snapshots and not rec.blocks:
        return None

    has_trade = rec.runner_pnl_ticks is not None

    if has_trade:
        reversal = rec.runner_pnl_ticks < 0
    else:
        reversal = None  # desconhecido sem trade

    # Primeiro snapshot válido como referência de sinal
    snap0 = rec.snapshots[0] if rec.snapshots else None
    # Pior bid_velocity nos snapshots (mais negativo = maior decelação)
    velocities = [s["bid_velocity"] for s in rec.snapshots if not (s["bid_velocity"] != s["bid_velocity"])]
    worst_vel = min(velocities) if velocities else float("nan")
    any_decel_block = any(s["decel_would_block"] for s in rec.snapshots)
    decel_block_count = sum(1 for s in rec.snapshots if s["decel_would_block"])

    # Loser price na entrada do runner (ou no primeiro snapshot)
    if rec.runner_active_bid_at_entry and rec.runner_active_bid_at_entry >= MIN_ACTIVE_BID:
        loser_price_at_signal = round(1.0 - rec.runner_active_bid_at_entry, 4)
        winner_bid_at_signal = rec.runner_active_bid_at_entry
    elif snap0:
        loser_price_at_signal = snap0["loser_price"]
        winner_bid_at_signal = snap0["active_bid"]
    else:
        return None

    if loser_price_at_signal <= 0 or loser_price_at_signal > MAX_LOSER_PRICE:
        return None

    # PnL simulado do reversal_sniper
    shares = BET_SIZE / loser_price_at_signal
    if reversal is True:
        sniper_pnl = round(shares * 1.0 - BET_SIZE, 2)
    elif reversal is False:
        sniper_pnl = -BET_SIZE
    else:
        sniper_pnl = None

    # Secs ao entrar (runner ou primeiro snap)
    secs_at_signal = snap0["secs"] if snap0 else None

    # Divergência BTC (Sinal A) — usa oracle dos snapshots
    btc_divergence = None
    if snap0:
        op = snap0["oracle_open"]
        cp = snap0["oracle_price"]
        if op > 0 and cp > 0:
            btc_divergence = round((cp - op) / op * 10000, 2)  # bps

    # Sinal A ativo?
    winner_side = snap0["winner_side"] if snap0 else rec.runner_side
    sinal_a_active = False
    if btc_divergence is not None and winner_side:
        # Se vencedor é UP, BTC deve estar acima do open → divergência > 0
        # Reversão → BTC está abaixo do open enquanto mercado ainda precifica UP
        sinal_a_active = (
            (winner_side == "UP" and btc_divergence < -100) or
            (winner_side == "DOWN" and btc_divergence > 100)
        )

    return {
        "slug": rec.slug,
        "session": rec.session,
        "reversal_occurred": reversal,
        "had_trade": has_trade,
        "runner_side": rec.runner_side,
        "winner_side": winner_side,
        "winner_bid_at_signal": round(winner_bid_at_signal, 4),
        "loser_price_at_signal": round(loser_price_at_signal, 4),
        "secs_at_signal": secs_at_signal,
        "runner_pnl_ticks": rec.runner_pnl_ticks,
        "runner_exit_reason": rec.runner_exit_reason,
        "sniper_pnl_simulated": sniper_pnl,
        "snapshot_count": len(rec.snapshots),
        "block_count": len(rec.blocks),
        "block_reasons": list({b["reason"].split(":")[0] for b in rec.blocks}),
        "decel_would_block_any": any_decel_block,
        "decel_would_block_count": decel_block_count,
        "worst_bid_velocity": round(worst_vel, 5) if worst_vel == worst_vel else None,
        "btc_divergence_bps": btc_divergence,
        "sinal_a_btc_divergence": sinal_a_active,
        "range15s_at_signal": snap0["range15s"] if snap0 else None,
        "dist_ptb_bps": snap0["dist_ptb_bps"] if snap0 else None,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _print_report(candidates: List[dict]) -> None:
    confirmed = [c for c in candidates if c["reversal_occurred"] is True]
    no_reversal = [c for c in candidates if c["reversal_occurred"] is False]
    unknown = [c for c in candidates if c["reversal_occurred"] is None]

    total_with_outcome = len(confirmed) + len(no_reversal)
    wr = len(confirmed) / total_with_outcome if total_with_outcome else 0

    print()
    print("=" * 70)
    print("ANÁLISE RETROATIVA — REVERSAL SNIPER")
    print("=" * 70)
    print(f"Slugs analisados       : {len(candidates)}")
    print(f"Com trade (outcome ok) : {total_with_outcome}  →  reversões: {len(confirmed)}  sem reversão: {len(no_reversal)}")
    print(f"Sem trade (só bloqueio): {len(unknown)}")
    print(f"Win rate real (trades) : {wr:.1%}  (threshold mínimo para +EV: 8%)")
    print()

    if confirmed:
        pnl_list = [c["sniper_pnl_simulated"] for c in confirmed if c["sniper_pnl_simulated"] is not None]
        pnl_no_rev = [-BET_SIZE] * len(no_reversal)
        total_pnl = sum(pnl_list) + sum(pnl_no_rev)
        avg_win = sum(pnl_list) / len(pnl_list) if pnl_list else 0
        ev = wr * avg_win - (1 - wr) * BET_SIZE if wr else 0

        print(f"PnL simulado total ($20/trade):")
        print(f"  Ganhos em reversões  : +${sum(pnl_list):.2f}  (avg: +${avg_win:.2f})")
        print(f"  Perdas sem reversão  : -${BET_SIZE * len(no_reversal):.2f}")
        print(f"  TOTAL                : ${total_pnl:+.2f}")
        print(f"  EV por trade         : ${ev:+.2f}  (meta: >= $0.50)")
        print()

    # Distribuição de preços do lado perdedor
    print("Distribuição de loser_price nas reversões confirmadas:")
    buckets = [(0.01, 0.04), (0.04, 0.07), (0.07, 0.10), (0.10, 0.15)]
    for lo, hi in buckets:
        ct = sum(1 for c in confirmed if lo <= c["loser_price_at_signal"] < hi)
        pnl_b = [c["sniper_pnl_simulated"] for c in confirmed if lo <= c["loser_price_at_signal"] < hi and c["sniper_pnl_simulated"]]
        avg_p = sum(pnl_b) / len(pnl_b) if pnl_b else 0
        print(f"  {lo:.2f}–{hi:.2f} : {ct:>3} reversões  avg PnL={avg_p:+.2f}")
    print()

    # Sinais presentes nas reversões
    print("Sinais presentes nas reversões confirmadas:")
    for field, label in [
        ("sinal_a_btc_divergence", "Sinal A — BTC divergência"),
        ("decel_would_block_any",  "Sinal B — bid decel (would_block)"),
    ]:
        ct = sum(1 for c in confirmed if c.get(field))
        print(f"  {label}: {ct}/{len(confirmed)} ({ct/len(confirmed):.0%})" if confirmed else f"  {label}: 0")
    print()

    # Detalhes das reversões
    print("Detalhe das reversões confirmadas:")
    print(f"  {'slug':<35} {'loser':>6}  {'secs':>4}  {'sniper_pnl':>10}  {'decel':>5}  {'sinalA':>6}  exit_reason")
    for c in sorted(confirmed, key=lambda x: x["loser_price_at_signal"]):
        decel = "Y" if c["decel_would_block_any"] else "n"
        sA = "Y" if c["sinal_a_btc_divergence"] else "n"
        reason = (c["runner_exit_reason"] or "")[:30]
        pnl = f"+${c['sniper_pnl_simulated']:.2f}" if c["sniper_pnl_simulated"] else "?"
        print(f"  {c['slug'][:34]:<35} {c['loser_price_at_signal']:>6.3f}  {c['secs_at_signal'] or 0:>4.0f}  {pnl:>10}  {decel:>5}  {sA:>6}  {reason}")
    print()

    # Bloqueios relacionados a reversões
    rev_slugs = {c["slug"] for c in confirmed}
    blocks_on_reversals = [c for c in unknown if any(r in (c.get("block_reasons") or []) for r in
                            ["bid_decel_gate", "leader_velocity_too_high", "entry_too_late"])]
    print(f"Slugs com bloqueio e sem trade (outcome desconhecido): {len(unknown)}")
    print(f"  desses com bloqueio por decel/velocity/late: {len(blocks_on_reversals)}")
    print()

    # Conclusão
    print("=" * 70)
    print("CONCLUSÃO:")
    if not total_with_outcome:
        print("  Dados insuficientes. Rodar após mais sessões com o runner real.")
    elif wr >= 0.08:
        print(f"  Win rate {wr:.1%} >= 8% — reversões existem nos dados históricos.")
        print("  Prosseguir para implementação do runner paper (Fase 1).")
    else:
        print(f"  Win rate {wr:.1%} < 8% — hipótese NÃO validada com os dados disponíveis.")
        print("  Revisar critérios de candidatura antes de implementar runner.")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real-only", action="store_true", help="Só logs do runner real")
    ap.add_argument("--min-bid", type=float, default=MIN_ACTIVE_BID)
    ap.add_argument("--out", default=None, help="Salvar JSONL dos candidatos")
    args = ap.parse_args()

    min_bid = args.min_bid
    base = Path("logs")
    files: List[Path] = []

    for sd in sorted(base.glob("current_almost_resolved_real_*")):
        if sd.is_dir():
            files.extend(sorted(sd.glob("*.jsonl")))

    if not files:
        print("Nenhum log encontrado em logs/current_almost_resolved_real_*/")
        return

    print(f"Lendo {len(files)} arquivo(s)...")
    all_records: List[SlugRecord] = []
    for f in files:
        all_records.extend(_parse_session(f))

    candidates = [c for c in (_classify(r) for r in all_records) if c is not None and c["winner_bid_at_signal"] >= min_bid]
    print(f"Candidatos com active_bid >= {min_bid}: {len(candidates)}")

    _print_report(candidates)

    out_path = args.out or f"logs/reversal_candidates_{datetime.now().strftime('%Y%m%d')}.jsonl"
    with open(out_path, "w", encoding="utf-8") as fh:
        for c in candidates:
            fh.write(json.dumps(c, default=str) + "\n")
    print(f"\nCandidatos salvos em: {out_path}")


if __name__ == "__main__":
    main()
