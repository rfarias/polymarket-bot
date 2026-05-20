"""
Analisa todos os logs reais do almost_resolved para extrair P&L por trade.
Lê eventos 'trade_closed', 'exit_filled', 'position_resolved', 'stop_triggered'
e reconstrói o histórico completo de ganhos e perdas.
"""
import json
import os
import sys
from pathlib import Path
from collections import defaultdict

LOGS_DIR = Path("logs")
PREFIX = "current_almost_resolved_real_"

def iter_jsonl(path):
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

def find_all_jsonl():
    results = []
    for entry in sorted(LOGS_DIR.iterdir()):
        if not entry.name.startswith(PREFIX):
            continue
        if not entry.is_dir():
            continue
        jsonl = entry / (PREFIX.rstrip("_") + ".jsonl")
        # tenta nome alternativo
        if not jsonl.exists():
            candidates = list(entry.glob("*.jsonl"))
            if candidates:
                jsonl = candidates[0]
        if jsonl.exists():
            results.append(jsonl)
    return results

def analyze():
    files = find_all_jsonl()
    print(f"Sessões encontradas: {len(files)}")

    trades = []  # cada item = dict com info do trade
    seen_slugs = set()

    # tipos de evento que indicam fechamento
    CLOSE_TYPES = {
        "trade_closed", "exit_filled", "position_resolved",
        "stop_triggered", "stop_filled", "stop_exit",
        "fill_and_kill_exit", "exit_repost", "redeem_success",
        "trade_summary", "shutdown_non_idle",
    }

    # coleta todos os eventos relevantes agrupados por slug
    slug_events = defaultdict(list)

    for jsonl_path in files:
        for ev in iter_jsonl(jsonl_path):
            t = ev.get("type", "")
            slug = ev.get("slug") or ev.get("event_slug") or ev.get("market_slug")
            if not slug:
                # tenta dentro de state
                state = ev.get("state", {}) or {}
                slug = state.get("event_slug")
            if slug:
                slug_events[slug].append(ev)

    print(f"Slugs únicos com eventos: {len(slug_events)}")

    # para cada slug, tenta reconstruir o trade
    for slug, evs in slug_events.items():
        evs_sorted = sorted(evs, key=lambda e: e.get("ts", e.get("timestamp", 0)))

        entry_price = None
        entry_qty = None
        side = None
        exit_price = None
        exit_qty = None
        pnl = None
        close_reason = None
        entry_ts = None
        exit_ts = None
        is_real = False

        for ev in evs_sorted:
            t = ev.get("type", "")

            # detecta se é trade real (tem order_id ou real_posts_enabled)
            cfg = ev.get("live_guarded_config", {}) or {}
            if cfg.get("real_posts_enabled") or cfg.get("shadow_only") is False:
                is_real = True
            if ev.get("order_id") or ev.get("entry_order_id"):
                is_real = True

            # entrada
            if t in ("entry_filled", "entry_confirmed", "position_opened"):
                entry_price = ev.get("entry_price") or ev.get("fill_price") or ev.get("price")
                entry_qty = ev.get("qty_filled") or ev.get("qty") or ev.get("entry_qty_filled")
                side = ev.get("side")
                entry_ts = ev.get("ts") or ev.get("timestamp")

            # saída
            if t in ("exit_filled", "stop_filled", "trade_closed", "redeem_success",
                     "position_resolved", "trade_summary"):
                ep = ev.get("exit_price") or ev.get("fill_price") or ev.get("price")
                eq = ev.get("qty_filled") or ev.get("exit_qty") or ev.get("exit_qty_filled")
                reason = ev.get("reason") or ev.get("close_reason") or t
                ts = ev.get("ts") or ev.get("timestamp")

                if ep and eq:
                    exit_price = ep
                    exit_qty = eq
                    close_reason = reason
                    exit_ts = ts

                # pnl direto
                if ev.get("pnl") is not None:
                    pnl = ev["pnl"]
                if ev.get("realized_pnl") is not None:
                    pnl = ev["realized_pnl"]

            # trade_summary costuma ter tudo
            if t == "trade_summary":
                s = ev.get("state", {}) or {}
                if not entry_price:
                    entry_price = s.get("entry_price")
                if not entry_qty:
                    entry_qty = s.get("entry_qty_filled")
                if not side:
                    side = s.get("side")
                if not exit_price:
                    exit_price = s.get("exit_price_posted") or s.get("exit_price")
                if not exit_qty:
                    exit_qty = s.get("exit_qty_filled")
                if s.get("entry_ts"):
                    entry_ts = s["entry_ts"]
                if ev.get("pnl") is not None:
                    pnl = ev["pnl"]

        # Se tem entry mas não tem exit explícito, procura awaiting_redeem / plataforma
        has_entry = entry_price and entry_qty and entry_qty > 0
        has_exit = exit_price or pnl is not None

        if not has_entry:
            continue

        # calcula pnl se não veio direto
        if pnl is None and exit_price and exit_qty:
            # Nos tokens de previsão: compra por entry_price, vende/redeem por exit_price
            pnl = (exit_price - entry_price) * exit_qty

        # Detecta perdas por redeem de token perdedor (resolvido contra)
        # nesses casos exit_qty=0 e exit_price=0 (token virou lixo)
        for ev in evs_sorted:
            t = ev.get("type", "")
            s = ev.get("state", {}) or {}
            mode = s.get("mode") or ev.get("mode")

            if mode == "awaiting_redeem" or t in ("platform_redeem_path", "pending_exit_platform_redeem_path"):
                eq = s.get("exit_qty_filled", 0)
                ep = s.get("entry_price") or entry_price
                eqty = s.get("entry_qty_filled") or entry_qty
                if eq == 0 and ep and eqty:
                    # token perdeu, vale 0
                    if pnl is None:
                        pnl = -ep * eqty
                        close_reason = "platform_redeem_loss"

            if t == "trade_summary":
                s2 = ev.get("state", {}) or {}
                if s2.get("exit_qty_filled", 0) == 0 and s2.get("entry_qty_filled", 0) > 0:
                    ep2 = s2.get("entry_price") or entry_price
                    eqty2 = s2.get("entry_qty_filled") or entry_qty
                    if ep2 and eqty2 and pnl is None:
                        pnl = -ep2 * eqty2
                        close_reason = close_reason or "loss_no_exit"

        if pnl is None:
            continue

        trades.append({
            "slug": slug,
            "side": side,
            "entry_price": entry_price,
            "entry_qty": entry_qty,
            "exit_price": exit_price,
            "pnl": round(pnl, 4),
            "reason": close_reason,
            "entry_ts": entry_ts,
            "is_real": is_real,
        })

    # filtra apenas trades reais com pnl significativo
    real_trades = [t for t in trades if abs(t["pnl"]) > 0.01]
    real_trades.sort(key=lambda t: t.get("entry_ts") or 0)

    if not real_trades:
        print("Nenhum trade real com PnL encontrado nos logs.")
        print("Tentando estratégia alternativa de busca...")
        return trades

    print(f"\n{'='*70}")
    print(f"TRADES REAIS ENCONTRADOS: {len(real_trades)}")
    print(f"{'='*70}")

    total_pnl = 0
    wins = []
    losses = []

    for i, t in enumerate(real_trades, 1):
        pnl = t["pnl"]
        total_pnl += pnl
        sign = "+" if pnl >= 0 else ""
        marker = "WIN" if pnl > 0 else "LOSS"
        print(f"  #{i:3d} {marker:4s} | {t['slug'][-30:]:30s} | side={t['side'] or '?':4s} | "
              f"entry={t['entry_price'] or 0:.3f} | qty={t['entry_qty'] or 0:.0f} | "
              f"pnl={sign}{pnl:.4f} | {(t['reason'] or '')[:25]}")
        if pnl > 0:
            wins.append(pnl)
        else:
            losses.append(pnl)

    print(f"\n{'='*70}")
    print(f"RESUMO")
    print(f"{'='*70}")
    print(f"  Total trades:    {len(real_trades)}")
    print(f"  Wins:            {len(wins)} | soma={sum(wins):.4f} | médio={sum(wins)/len(wins):.4f}" if wins else "  Wins:            0")
    print(f"  Losses:          {len(losses)} | soma={sum(losses):.4f} | médio={sum(losses)/len(losses):.4f}" if losses else "  Losses:          0")
    print(f"  PnL total:       {'+' if total_pnl >= 0 else ''}{total_pnl:.4f}")
    print(f"  Win rate:        {len(wins)/len(real_trades)*100:.1f}%" if real_trades else "")
    if wins and losses:
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        print(f"  Payoff ratio:    {avg_win / abs(avg_loss):.2f}x (win médio / |loss médio|)")
    if losses:
        big_losses = sorted(losses)
        print(f"  Maior perda:     {big_losses[0]:.4f}")
        print(f"  Top 3 perdas:    {[round(x,4) for x in big_losses[:3]]}")

    return real_trades

if __name__ == "__main__":
    analyze()
