"""
_ee_log_reader.py
Leitor unificado de logs EE — compatível com paper e runner real.

Uso:
    from _ee_log_reader import load_ee_trades, load_ee_snapshots

    # paper (padrão)
    trades = load_ee_trades("logs/ee_paper_*/ee_paper.jsonl")

    # runner real standalone
    trades = load_ee_trades("logs/ee_real_*/ee_real.jsonl")

    # runner AR com EE integrado
    trades = load_ee_trades("logs/current_almost_resolved_real_*/*.jsonl")

Formato normalizado de cada trade:
    slug, ts_entry, ts_exit, ep, secs, side, el_vel,
    outcome, pnl, source ("paper" | "real" | "ar_real")
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Iterator

_OUTCOME_MAP = {
    "near_win_exit":      "WIN",
    "target":             "WIN",
    "ee_profit_protect":  "PROFIT_PROTECT",
    "profit_protect":     "PROFIT_PROTECT",
    "ee_stop":            "STOP_LOSS",
    "stop":               "STOP_LOSS",
    "ee_reversal":        "STOP_LOSS",
    "structural_stop":    "STOP_LOSS",
    "timeout":            "STOP_LOSS",
}


def _iter_rows(glob_pattern: str) -> Iterator[dict]:
    for lp in sorted(Path(".").glob(glob_pattern)):
        for line in lp.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except Exception:
                    pass


def _is_paper(row: dict) -> bool:
    return row.get("type", "").startswith("ee_paper")


def _load_ar_real(glob_pattern: str, skip_outcomes) -> list[dict]:
    """Carrega trades EE do AR runner integrado.
    Cruza ee_real_entry (por slug) com trade_closed (por entry_slug) em todos os arquivos.
    """
    all_entries: dict[str, dict] = {}  # slug -> ee_real_entry row
    all_closeds: dict[str, dict] = {}  # slug -> trade_closed row

    for lp in sorted(Path(".").glob(glob_pattern)):
        for line in lp.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get("type", "")
            if t == "ee_real_entry" and "slug" in r:
                slug = r["slug"]
                if slug not in all_entries:
                    all_entries[slug] = r
            elif t == "trade_closed" and "entry_slug" in r:
                slug = r["entry_slug"]
                if slug not in all_closeds:
                    all_closeds[slug] = r

    trades = []
    for slug, entry in all_entries.items():
        cl = all_closeds.get(slug)
        if not cl:
            continue

        pnl = float(cl.get("pnl_usd", 0) or 0)
        lr  = cl.get("last_reason", "")
        parts  = lr.split(":")
        reason = parts[1] if len(parts) > 1 else lr
        outcome = _OUTCOME_MAP.get(reason, "WIN" if pnl > 0 else "LOSS")

        if outcome in skip_outcomes:
            continue

        el     = entry.get("el", {})
        el_vel = el.get("el_vel", 0)

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
            "source":    "ar_real",
            "btc":       entry.get("btc", {}),
        })

    return trades


def load_ee_trades(glob_pattern: str, skip_outcomes=("aberta", "MISSED", "MISSED_SLUG_CHANGE")) -> list[dict]:
    """Carrega trades finalizados normalizados de qualquer runner EE."""
    # Detectar formato pelo primeiro arquivo com eventos relevantes
    for lp in sorted(Path(".").glob(glob_pattern)):
        for line in lp.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get("type", "")
            if t == "ee_paper_entry" or t == "ee_paper_closed":
                # formato paper — processar por sessão
                break
            if t == "ee_real_entry":
                # Verificar se há ee_real_closed no mesmo arquivo
                rows = [json.loads(l) for l in lp.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
                has_real_closed = any(r2.get("type") == "ee_real_closed" for r2 in rows)
                if not has_real_closed:
                    # AR runner integrado — entrada e fechamento em arquivos distintos
                    return _load_ar_real(glob_pattern, skip_outcomes)
                break
        else:
            continue
        break

    # Formato paper ou real standalone (ee_real_closed presente)
    sessions: dict[str, list[dict]] = {}
    for lp in sorted(Path(".").glob(glob_pattern)):
        key = str(lp)
        rows = []
        for line in lp.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        sessions[key] = rows

    trades = []
    for rows in sessions.values():
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

            if source == "paper":
                ee      = cl.get("ee", {})
                outcome = ee.get("outcome", "?")
                pnl     = ee.get("pnl", 0)
            else:
                outcome = cl.get("outcome", "?")
                pnl     = cl.get("pnl", 0)

            if outcome in skip_outcomes:
                continue

            el     = entry.get("el", {})
            el_vel = el.get("el_vel", 0)

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
