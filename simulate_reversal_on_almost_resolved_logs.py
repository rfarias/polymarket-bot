"""
simulate_reversal_on_almost_resolved_logs.py

Pergunta: se tivéssemos usado o reversal_sniper ao invés do quase-resolvido,
qual seria o resultado?

Lógica:
- Para cada slug nos logs do quase-resolvido onde houve TOTAL_LOSS (token virou pó),
  sabemos que o OUTRO lado ganhou. Isso é exatamente onde o reversal sniper teria lucrado.
- Para cada slug com WIN (quase-resolvido acertou), o reversal sniper teria perdido.

Simulação do reversal sniper:
- Monitora o mesmo mercado com os mesmos dados de mercado dos logs
- Quando winner_bid >= 0.88 e loser_bid <= 0.15 e secs em [15, 120]:
  - Calcula Sinal B (deceleration do winner bid)
  - Se sinal_b_score >= threshold → would_enter no loser
- Resultado: loser que vira winner → lucro (loser_price → 1.00)
            loser que permanece loser → perda total (loser_price → 0)

Entrada: logs/current_almost_resolved_real_*/current_almost_resolved_real.jsonl
Saída: relatório com EV comparativo por setup
"""
from __future__ import annotations

import glob
import json
import os
from collections import defaultdict, deque
from pathlib import Path

LOGS_DIR = Path("logs")

# Parâmetros do sniper (lê do config.json se existir)
CONFIG_PATH = Path("agent/config.json")

def _load_sniper_params() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f).get("reversal_sniper", {})
    except Exception:
        return {}

# Parâmetros default
MIN_WINNER_BID   = 0.88
MAX_LOSER_PRICE  = 0.15
MIN_SECS         = 15
MAX_SECS         = 120
SINAL_B_WEAK_VEL    = -0.020
SINAL_B_STRONG_VEL  = -0.050
BID_DECEL_POLLS  = 3
PAPER_BET_SIZE   = 20.0   # $ simulados

cfg = _load_sniper_params()
SCORE_THRESHOLD = int(cfg.get("entry_score_threshold", 2))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iter_jsonl(path):
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


def _score_sinal_b(history: deque, bid_now: float) -> tuple[int, float | None]:
    """Calcula score Sinal B baseado no histórico de bids do winner."""
    if len(history) < BID_DECEL_POLLS + 1:
        return 0, None
    bid_ago = list(history)[-BID_DECEL_POLLS - 1]
    velocity = round(bid_now - bid_ago, 6)
    if velocity < SINAL_B_STRONG_VEL:
        score = 3
    elif velocity < SINAL_B_WEAK_VEL:
        score = 2
    else:
        score = 0
    return score, velocity


# ---------------------------------------------------------------------------
# Extração de resultados reais do quase-resolvido
# ---------------------------------------------------------------------------

def extract_almost_resolved_outcomes() -> dict[str, dict]:
    """
    Retorna {slug: {"ar_outcome": "win"|"total_loss"|"partial_loss",
                    "ar_entry_price": float, "ar_qty": int,
                    "ar_side": str, "ar_pnl": float}}
    """
    pattern = str(LOGS_DIR / "current_almost_resolved_real_*" / "current_almost_resolved_real.jsonl")
    files = sorted(glob.glob(pattern))

    outcomes: dict[str, dict] = {}
    seen = set()

    for fpath in files:
        for ev in _iter_jsonl(fpath):
            t = ev.get("type", "")
            trade = ev.get("trade", {}) or {}
            slug = trade.get("event_slug")
            if not slug:
                continue

            key = f"{slug}:{trade.get('entry_price')}:{trade.get('side')}"

            if t == "flat":
                exit_ord = ev.get("exit_order") or {}
                ep = float(exit_ord.get("price", 0) or 0)
                eq = float(exit_ord.get("size_matched", 0) or 0)
                entry = float(trade.get("entry_price") or 0)
                side = trade.get("side", "?")
                if eq > 0 and entry > 0 and key not in seen:
                    seen.add(key)
                    pnl = (ep - entry) * eq
                    outcomes[slug] = {
                        "ar_outcome": "win" if pnl > 0 else "partial_loss",
                        "ar_entry_price": entry,
                        "ar_qty": eq,
                        "ar_side": side,
                        "ar_exit_price": ep,
                        "ar_pnl": round(pnl, 4),
                    }

            elif t == "awaiting_redeem":
                entry = float(trade.get("entry_price") or 0)
                qty = float(trade.get("entry_qty_filled") or 0)
                side = trade.get("side", "?")
                side_won = ev.get("side_won")
                reason = trade.get("last_reason", "")
                if not entry or not qty or key in seen:
                    continue
                if side_won is True:
                    seen.add(key)
                    pnl = (1.0 - entry) * qty
                    outcomes[slug] = {
                        "ar_outcome": "win",
                        "ar_entry_price": entry,
                        "ar_qty": qty,
                        "ar_side": side,
                        "ar_exit_price": 1.0,
                        "ar_pnl": round(pnl, 4),
                    }
                elif side_won is False or "platform_redeem_path" in (reason or ""):
                    seen.add(key)
                    pnl = -entry * qty
                    outcomes[slug] = {
                        "ar_outcome": "total_loss",
                        "ar_entry_price": entry,
                        "ar_qty": qty,
                        "ar_side": side,
                        "ar_exit_price": 0.0,
                        "ar_pnl": round(pnl, 4),
                    }

    return outcomes


# ---------------------------------------------------------------------------
# Simulação do reversal sniper nos mesmos logs
# ---------------------------------------------------------------------------

def simulate_reversal_sniper_on_logs(ar_outcomes: dict) -> list[dict]:
    """
    Para cada slug com outcome conhecido, simula o que o reversal_sniper teria feito.
    Lê os snapshots do mesmo log e aplica a lógica de entrada do sniper.
    """
    pattern = str(LOGS_DIR / "current_almost_resolved_real_*" / "current_almost_resolved_real.jsonl")
    files = sorted(glob.glob(pattern))

    # Agrupa eventos por slug
    slug_snapshots: dict[str, list[dict]] = defaultdict(list)

    for fpath in files:
        for ev in _iter_jsonl(fpath):
            t = ev.get("type", "")
            if t != "snapshot":
                continue
            slug = ev.get("current_slug") or (ev.get("trade", {}) or {}).get("event_slug")
            if slug and slug in ar_outcomes:
                slug_snapshots[slug].append(ev)

    results = []

    for slug, snaps in slug_snapshots.items():
        ar = ar_outcomes[slug]
        ar_side = ar["ar_side"]          # lado que o quase-resolvido comprou (o "winner" na época)
        loser_side = "UP" if ar_side == "DOWN" else "DOWN"

        # Ordena snapshots por timestamp
        snaps.sort(key=lambda e: e.get("ts", 0))

        # Histórico de bids do winner para calcular Sinal B
        winner_history: deque = deque(maxlen=BID_DECEL_POLLS + 2)

        sniper_entry = None  # primeira entrada simulada

        for snap in snaps:
            secs = snap.get("current_secs")
            if secs is None:
                continue

            # Filtra janela temporal do sniper
            if not (MIN_SECS <= secs <= MAX_SECS):
                continue

            sig = snap.get("signal", {}) or {}
            up_bid = sig.get("up_buy", 0) or 0
            down_bid = sig.get("down_buy", 0) or 0

            winner_bid = down_bid if ar_side == "DOWN" else up_bid
            loser_bid  = up_bid  if ar_side == "DOWN" else down_bid

            if winner_bid < MIN_WINNER_BID:
                continue
            if loser_bid > MAX_LOSER_PRICE or loser_bid <= 0:
                continue

            winner_history.append(winner_bid)
            sinal_b_score, velocity = _score_sinal_b(winner_history, winner_bid)
            total_score = sinal_b_score  # Sinal A não disponível aqui; apenas B

            if total_score >= SCORE_THRESHOLD and sniper_entry is None:
                sniper_entry = {
                    "entry_secs": secs,
                    "entry_loser_price": round(loser_bid, 4),
                    "entry_winner_price": round(winner_bid, 4),
                    "entry_score": total_score,
                    "sinal_b_score": sinal_b_score,
                    "winner_velocity": velocity,
                }
                break  # uma entrada por slug

        if sniper_entry is None:
            # sniper não teria entrado neste mercado
            results.append({
                "slug": slug,
                "ar_outcome": ar["ar_outcome"],
                "ar_pnl": ar["ar_pnl"],
                "sniper_would_enter": False,
                "sniper_entry": None,
                "sniper_outcome": "no_entry",
                "sniper_pnl": 0.0,
            })
            continue

        entry_price = sniper_entry["entry_loser_price"]
        qty = PAPER_BET_SIZE / entry_price  # shares simuladas com $20

        # O loser vira winner se ar_outcome == "total_loss" (ar comprou o lado que perdeu = loser do sniper ganhou)
        if ar["ar_outcome"] == "total_loss":
            # reversal aconteceu → loser do sniper virou winner
            sniper_exit = 1.0
            sniper_pnl = round((sniper_exit - entry_price) * qty, 4)
            sniper_outcome = "win"
        elif ar["ar_outcome"] == "win":
            # quase-resolvido acertou → loser permaneceu loser → sniper perde tudo
            sniper_exit = 0.0
            sniper_pnl = round(-entry_price * qty, 4)
            sniper_outcome = "total_loss"
        else:
            # partial_loss do ar → ambíguo; conservador: sniper perde
            sniper_exit = 0.0
            sniper_pnl = round(-entry_price * qty, 4)
            sniper_outcome = "loss"

        results.append({
            "slug": slug,
            "ar_outcome": ar["ar_outcome"],
            "ar_pnl": ar["ar_pnl"],
            "sniper_would_enter": True,
            "sniper_entry": sniper_entry,
            "sniper_outcome": sniper_outcome,
            "sniper_exit_price": sniper_exit,
            "sniper_pnl": sniper_pnl,
            "sniper_qty": round(qty, 2),
            "sniper_entry_price": entry_price,
        })

    return results


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

def report(results: list[dict]) -> None:
    entered  = [r for r in results if r["sniper_would_enter"]]
    no_entry = [r for r in results if not r["sniper_would_enter"]]

    wins       = [r for r in entered if r["sniper_outcome"] == "win"]
    total_loss = [r for r in entered if r["sniper_outcome"] == "total_loss"]
    other_loss = [r for r in entered if r["sniper_outcome"] not in ("win", "total_loss", "no_entry")]

    sniper_pnl   = sum(r["sniper_pnl"] for r in entered)
    ar_pnl_total = sum(r["ar_pnl"] for r in results)
    ar_pnl_entered = sum(r["ar_pnl"] for r in entered)

    print(f"\n{'='*70}")
    print(f"  SIMULAÇÃO: Reversal Sniper vs Quase-Resolvido")
    print(f"  Score threshold atual: {SCORE_THRESHOLD}")
    print(f"  Paper bet: ${PAPER_BET_SIZE} por trade")
    print(f"{'='*70}")

    print(f"\n--- Mercados analisados: {len(results)} slugs com outcome conhecido ---\n")

    print(f"  Slugs onde sniper ENTRARIA:   {len(entered)}")
    print(f"  Slugs onde sniper NÃO entraria: {len(no_entry)}")

    print(f"\n--- Resultados do Sniper (nos {len(entered)} mercados onde entraria) ---\n")
    print(f"  {'Slug':30s} {'AR':10s} {'AR PnL':8s}  {'Sniper':12s} {'Sniper PnL':10s} {'Entry':6s} {'Score'}")
    print(f"  {'-'*85}")

    for r in sorted(entered, key=lambda x: x["sniper_pnl"]):
        en = r.get("sniper_entry") or {}
        print(f"  {r['slug'][-30:]:30s} {r['ar_outcome']:10s} {r['ar_pnl']:8.2f}  "
              f"{r['sniper_outcome']:12s} {r['sniper_pnl']:10.2f} "
              f"{r.get('sniper_entry_price', 0):6.3f} {en.get('entry_score', 0)}")

    print(f"\n--- Resumo do Sniper ---\n")
    print(f"  Trades (entradas):      {len(entered)}")
    print(f"  Wins:                   {len(wins)}  (outcome: reversal aconteceu)")
    print(f"  Total losses:           {len(total_loss)}  (outcome: quase-resolvido acertou, sniper perdeu tudo)")
    print(f"  Outros:                 {len(other_loss)}")
    n = len(entered)
    if n:
        print(f"  Win rate:               {len(wins)/n*100:.1f}%")
        print(f"  EV médio/trade:         ${sniper_pnl/n:.2f}")
    print(f"  PnL total sniper:       ${sniper_pnl:+.2f}")

    print(f"\n--- Comparação direta ---\n")
    print(f"  PnL quase-resolvido (todos):      ${ar_pnl_total:+.2f}")
    print(f"  PnL quase-resolvido (mesmos slugs): ${ar_pnl_entered:+.2f}")
    print(f"  PnL reversal sniper (mesmos slugs): ${sniper_pnl:+.2f}")
    delta = sniper_pnl - ar_pnl_entered
    print(f"  Delta (sniper - ar):              ${delta:+.2f}")

    if n:
        print(f"\n--- Análise de por que o sniper não entra em {len(no_entry)} mercados ---\n")
        # Categoriza os que não entram
        ar_wins_no_entry = sum(1 for r in no_entry if r["ar_outcome"] == "win")
        ar_loss_no_entry = sum(1 for r in no_entry if r["ar_outcome"] in ("total_loss", "partial_loss"))
        print(f"  Desses {len(no_entry)} sem entrada do sniper:")
        print(f"    {ar_wins_no_entry} eram wins do AR (bom: sniper evitou perdas)")
        print(f"    {ar_loss_no_entry} eram losses do AR (ruim: sniper perdeu oportunidade de reversão)")

    if wins:
        avg_win_price = sum(r.get("sniper_entry_price", 0) for r in wins) / len(wins)
        avg_pnl_win = sum(r["sniper_pnl"] for r in wins) / len(wins)
        print(f"\n  Wins — preço médio de entrada: {avg_win_price:.3f} | PnL médio: ${avg_pnl_win:.2f}")
    if total_loss:
        avg_loss_price = sum(r.get("sniper_entry_price", 0) for r in total_loss) / len(total_loss)
        avg_pnl_loss = sum(r["sniper_pnl"] for r in total_loss) / len(total_loss)
        print(f"  Losses — preço médio de entrada: {avg_loss_price:.3f} | PnL médio: ${avg_pnl_loss:.2f}")


if __name__ == "__main__":
    print("Carregando outcomes do quase-resolvido...")
    ar_outcomes = extract_almost_resolved_outcomes()
    print(f"Slugs com outcome conhecido: {len(ar_outcomes)}")

    print("Simulando reversal sniper nos mesmos logs...")
    results = simulate_reversal_sniper_on_logs(ar_outcomes)

    report(results)
