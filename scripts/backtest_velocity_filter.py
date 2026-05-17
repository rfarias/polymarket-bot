"""
Backtest do refinamento do velocity filter.

Regra atual:  block se vel_range_60s >= 0.06
Nova regra:   block se vel_range_30s >= 0.04  OU  vel_range_60s >= 0.10

Para cada bloqueio que seria LIBERADO pela nova regra, rastreia o que o mercado
fez nos próximos 120s usando os snapshots do mesmo log — proxy para "teria ganho?"

Uso:
    python scripts/backtest_velocity_filter.py
    python scripts/backtest_velocity_filter.py --logs-dir logs --sessions 6
"""

import json
import os
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NEW_THRESH_60 = 0.10   # vel_range_60s >= este → bloqueia
NEW_THRESH_30 = 0.04   # vel_range_30s >= este → bloqueia


def ts_to_hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%H:%M:%S")


def load_events(log_path: str) -> list[dict]:
    events = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def build_snapshot_index(events: list[dict]) -> dict[str, list[dict]]:
    """slug → lista de snapshots ordenados por ts."""
    idx: dict[str, list[dict]] = {}
    for e in events:
        if e.get("type") != "snapshot":
            continue
        slug = e.get("signal", {}).get("event_slug") or e.get("current_slug")
        if slug:
            idx.setdefault(slug, []).append(e)
    return idx


def outcome_after(snap_idx: dict, slug: str, side: str, ts_after: float, window: float = 120.0) -> dict:
    """
    Retorna o que aconteceu com o mercado (slug) a partir de ts_after.
    Usa os snapshots do log como proxy.
    """
    snaps = [s for s in snap_idx.get(slug, []) if s["ts"] > ts_after and s["ts"] <= ts_after + window]
    if not snaps:
        return {"snaps": 0, "final_price": None, "peak_price": None, "direction_correct": None, "resolved": False}

    buy_key = "up_buy" if side == "UP" else "down_buy"
    prices = [s.get("signal", {}).get(buy_key, 0) for s in snaps if s.get("signal", {}).get(buy_key, 0) > 0]
    if not prices:
        return {"snaps": len(snaps), "final_price": None, "peak_price": None, "direction_correct": None, "resolved": False}

    final_price = prices[-1]
    peak_price = max(prices)
    # "resolvido" se price chegou a 0.99+
    resolved = any(p >= 0.99 for p in prices)
    # direção correta: preço subiu >= 0.01 a partir do preço de entrada
    entry_price = snaps[0].get("signal", {}).get(buy_key, 0)
    direction_correct = (peak_price - entry_price) >= 0.01 if entry_price > 0 else None

    return {
        "snaps": len(snaps),
        "entry_snap_price": round(entry_price, 3),
        "final_price": round(final_price, 3),
        "peak_price": round(peak_price, 3),
        "direction_correct": direction_correct,
        "resolved": resolved,
    }


def analyse_session(log_path: str) -> dict:
    events = load_events(log_path)
    snap_idx = build_snapshot_index(events)

    # Reconstruir rolling history de velocidade (igual ao runner)
    vel_history: dict[str, list[tuple[float, float]]] = {}

    results = {
        "session": Path(log_path).parent.name,
        "total_blocked": 0,
        "old_rule_blocks": 0,
        "new_rule_blocks": 0,
        "freed": [],        # bloqueados pela regra antiga, liberados pela nova
        "still_blocked": 0,
    }

    # Processar eventos em ordem cronológica
    for e in sorted(events, key=lambda x: x.get("ts", 0)):
        etype = e.get("type")
        ts = e.get("ts", 0)
        signal = e.get("signal", {})

        # Atualizar rolling history a partir dos snapshots (igual ao runner)
        if etype == "snapshot":
            slug = signal.get("event_slug") or e.get("current_slug", "")
            if slug:
                for vel_side, vel_key in (("UP", "up_buy"), ("DOWN", "down_buy")):
                    price = signal.get(vel_key, 0) or 0
                    if price > 0:
                        hk = f"{slug}:{vel_side}"
                        hist = vel_history.setdefault(hk, [])
                        hist.append((ts, price))
                        vel_history[hk] = [(t, p) for t, p in hist if ts - t <= 60.0]

        if etype != "entry_blocked" or e.get("reason") != "leader_velocity_too_high":
            continue

        results["total_blocked"] += 1
        vel_range_60 = e.get("velocity_range", 0)
        slug = signal.get("event_slug", "")
        side = signal.get("side", "")

        # Calcular vel_range_30s a partir da history reconstruída
        hk = f"{slug}:{side}"
        hist_30 = [(t, p) for t, p in vel_history.get(hk, []) if ts - t <= 30.0]
        vel_range_30 = (max(p for _, p in hist_30) - min(p for _, p in hist_30)) if len(hist_30) >= 2 else 0.0

        # Regra antiga
        old_blocked = vel_range_60 >= 0.06
        # Nova regra
        new_blocked = vel_range_30 >= NEW_THRESH_30 or vel_range_60 >= NEW_THRESH_60

        if old_blocked:
            results["old_rule_blocks"] += 1
        if new_blocked:
            results["new_rule_blocks"] += 1

        if old_blocked and not new_blocked:
            # Seria liberado — o que o mercado fez depois?
            outcome = outcome_after(snap_idx, slug, side, ts)
            results["freed"].append({
                "ts": ts_to_hhmm(ts),
                "side": side,
                "entry_price": signal.get("entry_price"),
                "vel_range_60": round(vel_range_60, 3),
                "vel_range_30": round(vel_range_30, 3),
                "m15s": signal.get("market_range_15s"),
                "m30s": signal.get("market_range_30s"),
                "secs": int(signal.get("secs_to_end", 0)),
                "variant": signal.get("setup_variant"),
                **outcome,
            })
        elif old_blocked and new_blocked:
            results["still_blocked"] += 1

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs-dir", default="logs")
    parser.add_argument("--sessions", type=int, default=8, help="Quantas sessões analisar (mais recentes)")
    args = parser.parse_args()

    logs_dir = ROOT / args.logs_dir
    log_files = sorted(
        logs_dir.rglob("current_almost_resolved_real.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: args.sessions]

    print(f"Analisando {len(log_files)} sessões\n")
    print(f"Regra atual:  vel_range_60s >= 0.06")
    print(f"Nova regra:   vel_range_30s >= {NEW_THRESH_30}  OR  vel_range_60s >= {NEW_THRESH_60}")
    print("=" * 80)

    grand_total_blocked = 0
    grand_total_freed = 0
    grand_still_blocked = 0
    all_freed = []

    for log_path in reversed(log_files):
        res = analyse_session(str(log_path))
        freed = res["freed"]
        grand_total_blocked += res["old_rule_blocks"]
        grand_total_freed += len(freed)
        grand_still_blocked += res["still_blocked"]
        all_freed.extend(freed)

        print(f"\n[{res['session']}]")
        print(f"  Bloqueados (regra atual): {res['old_rule_blocks']}")
        print(f"  Bloqueados (nova regra) : {res['new_rule_blocks']}")
        print(f"  Liberados               : {len(freed)}")

        for f in freed:
            direction_str = "OK certo" if f.get("direction_correct") else ("XX errado" if f.get("direction_correct") is False else "?")
            resolved_str = " RESOLVEU" if f.get("resolved") else ""
            print(f"    {f['ts']}  {f['side']}@{f['entry_price']}  "
                  f"vel60={f['vel_range_60']}  vel30={f['vel_range_30']}  "
                  f"secs={f['secs']}  "
                  f"peak={f.get('peak_price')}  final={f.get('final_price')}  "
                  f"dir={direction_str}{resolved_str}")

    print("\n" + "=" * 80)
    print(f"TOTAIS")
    print(f"  Bloqueios totais (regra atual): {grand_total_blocked}")
    print(f"  Liberados pela nova regra     : {grand_total_freed}  ({100*grand_total_freed/max(grand_total_blocked,1):.0f}%)")
    print(f"  Ainda bloqueados              : {grand_still_blocked}")

    # Análise dos liberados
    with_outcome = [f for f in all_freed if f.get("direction_correct") is not None]
    correct = [f for f in with_outcome if f["direction_correct"]]
    resolved_wins = [f for f in all_freed if f.get("resolved")]
    print(f"\n  Dos liberados com outcome rastreável: {len(with_outcome)}")
    print(f"    Direção correta (peak subiu >= 0.01): {len(correct)} / {len(with_outcome)}"
          f"  ({100*len(correct)/max(len(with_outcome),1):.0f}%)")
    print(f"    Resolveram a 0.99+ (win quase certo): {len(resolved_wins)} / {len(all_freed)}")
    print(f"    Sem dados de outcome               : {len(all_freed) - len(with_outcome)}")

    print("\n  Distribuição dos vel_range_60 nos LIBERADOS:")
    for lo, hi in [(0.06, 0.07), (0.07, 0.08), (0.08, 0.09), (0.09, 0.10)]:
        n = sum(1 for f in all_freed if lo <= f["vel_range_60"] < hi)
        print(f"    [{lo:.2f}, {hi:.2f}): {n}")


if __name__ == "__main__":
    main()
