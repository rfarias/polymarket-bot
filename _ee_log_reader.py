"""
_ee_log_reader.py
Leitor unificado de logs EE — compatível com paper e runner real.

Uso:
    from _ee_log_reader import load_ee_trades, load_ee_snapshots

    # paper (padrão)
    trades = load_ee_trades("logs/ee_paper_*/ee_paper.jsonl")

    # runner real
    trades = load_ee_trades("logs/ee_real_*/ee_real.jsonl")

Formato normalizado de cada trade:
    slug, ts_entry, ts_exit, ep, secs, side, el_vel,
    outcome, pnl, source ("paper" | "real")
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Iterator


def _iter_rows(glob_pattern: str) -> Iterator[dict]:
    for lp in sorted(Path(".").glob(glob_pattern)):
        for line in lp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except Exception:
                    pass


def _is_paper(row: dict) -> bool:
    return row.get("type", "").startswith("ee_paper")


def load_ee_trades(glob_pattern: str, skip_outcomes=("aberta", "MISSED", "MISSED_SLUG_CHANGE")) -> list[dict]:
    """Carrega trades finalizados normalizados de qualquer runner EE."""
    sessions: dict[str, list[dict]] = {}  # lp_str -> rows

    for lp in sorted(Path(".").glob(glob_pattern)):
        key = str(lp)
        rows = []
        for line in lp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        sessions[key] = rows

    trades = []
    for rows in sessions.values():
        # Detectar fonte pelo primeiro tipo relevante
        source = "paper"
        for r in rows:
            t = r.get("type", "")
            if "ee_real" in t:
                source = "real"
                break
            if "ee_paper" in t:
                source = "paper"
                break

        entry_type  = "ee_paper_entry"  if source == "paper" else "ee_real_entry"
        closed_type = "ee_paper_closed" if source == "paper" else "ee_real_closed"

        entries = {r["slug"]: r for r in rows if r.get("type") == entry_type and "slug" in r}
        closeds = {r["slug"]: r for r in rows if r.get("type") == closed_type and "slug" in r}

        for slug, entry in entries.items():
            cl = closeds.get(slug)
            if not cl:
                continue

            # Outcome e PnL — formato diverge
            if source == "paper":
                ee      = cl.get("ee", {})
                outcome = ee.get("outcome", "?")
                pnl     = ee.get("pnl", 0)
            else:
                outcome = cl.get("outcome", "?")
                pnl     = cl.get("pnl", 0)

            if outcome in skip_outcomes:
                continue

            el      = entry.get("el", {})
            el_vel  = el.get("el_vel", 0)

            trades.append({
                "slug":      slug,
                "ts_entry":  entry.get("ts", 0),
                "ts_exit":   cl.get("ts", 0),
                "ep":        entry.get("ep", 0),
                "secs":      entry.get("secs", 0),
                "side":      entry.get("side", ""),
                "el_vel":    el_vel,
                "el":        el,
                "outcome":   outcome,
                "pnl":       pnl,
                "source":    source,
                # campos extras do paper (btc logging)
                "btc":       entry.get("btc", {}),
            })

    return trades


def load_ee_snapshots(glob_pattern: str) -> dict[str, list[dict]]:
    """Carrega snapshots por slug de qualquer runner EE.
    Retorna dict: slug -> [snaps ordenados por ts]
    """
    from collections import defaultdict
    slug_snaps: dict[str, list] = defaultdict(list)

    for lp in sorted(Path(".").glob(glob_pattern)):
        for line in lp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") != "snapshot":
                continue
            sl = r.get("current_slug", "")
            if sl:
                slug_snaps[sl].append({
                    "ts":     r.get("ts", 0),
                    "secs":   r.get("current_secs"),
                    "up_bid": r.get("current_exec", {}).get("up_bid", 0),
                    "dn_bid": r.get("current_exec", {}).get("down_bid", 0),
                    "el":     r.get("early_leader", {}),
                })

    # Ordenar por ts
    for sl in slug_snaps:
        slug_snaps[sl].sort(key=lambda s: s["ts"])

    return dict(slug_snaps)
