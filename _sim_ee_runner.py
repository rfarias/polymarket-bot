"""
_sim_ee_runner.py

Simula a estratégia Early Entry (EE) sobre os logs do runner real de hoje.

Setup EE:
  - Janela de detecção EL: secs 181–240
  - Filtro F3 (continuidade): bid EL >= 0.70 na janela 121–180s
  - Filtro el_vel: crescimento bid (bid_180 – bid_240) >= 0.08
  - Janela de entrada: secs 30–180, bid EL em 0.82–0.86
  - Saída: hold to resolution
  - Hedge: compra lado oposto ao cruzar bid EL abaixo de 0.50

Usa snapshots do runner real (campos early_leader + current_exec).
"""
import json, sys, io, datetime
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LOG_DIR = Path(r"C:\Users\Leticia\Documents\polymarket-bot\logs")
if not LOG_DIR.exists():
    LOG_DIR = Path(r"C:\Users\Letícia\Documents\polymarket-bot\logs")

# Parâmetros EE
EL_MIN       = 0.55
CONT_MIN     = 0.70
VEL_MIN      = 0.08
ENTRY_LO     = 0.82
ENTRY_HI     = 0.86
HEDGE_THR    = 0.50
QTY          = 3.0


def sf(v, d=0.0):
    try: return float(v)
    except: return d


# ---------------------------------------------------------------------------
# Carregar snapshots do runner real
# ---------------------------------------------------------------------------

def load_runner_snapshots(log_dir: Path):
    """Retorna dict slug -> list of snap dicts ordenados por secs desc."""
    by_slug = defaultdict(list)
    seen = set()

    for subdir in sorted(log_dir.glob("current_almost_resolved_real_*")):
        if not subdir.is_dir():
            continue
        for f in sorted(subdir.glob("*.jsonl")):
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") != "snapshot":
                        continue
                    slug = (d.get("current_slug")
                            or (d.get("signal") or {}).get("event_slug")
                            or (d.get("trade") or {}).get("event_slug"))
                    secs = d.get("current_secs")
                    if not slug or secs is None:
                        continue
                    secs = int(secs)
                    key = (slug, secs)
                    if key in seen:
                        continue
                    seen.add(key)

                    el = d.get("early_leader") or {}
                    sig = d.get("signal") or {}
                    exc = d.get("current_exec") or {}

                    up_bid   = sf(exc.get("up_bid") or sig.get("up_buy"))
                    down_bid = sf(exc.get("down_bid") or sig.get("down_buy"))

                    by_slug[slug].append({
                        "secs":        secs,
                        "up_bid":      up_bid,
                        "down_bid":    down_bid,
                        "el_side":     el.get("early_side"),
                        "el_bid":      sf(el.get("early_bid")),
                        "el_vel":      sf(el.get("el_vel") if "el_vel" in el else 0.0),
                        "f3_ok":       bool(el.get("f3_ok")),
                        "inverted":    bool(el.get("inverted")),
                        "inv_side":    el.get("inversion_side"),
                        "inv_bid":     sf(el.get("inversion_bid")),
                        "ts":          sf(d.get("ts")),
                    })

    # Ordena por secs desc e preenche el_vel se não vier do runner
    result = {}
    for slug, snaps in by_slug.items():
        snaps.sort(key=lambda x: x["secs"], reverse=True)
        result[slug] = snaps
    return result


# ---------------------------------------------------------------------------
# Reconstrução do EL state a partir dos snaps brutos
# (runner não exporta el_vel — calculamos aqui)
# ---------------------------------------------------------------------------

def reconstruct_el(snaps):
    """Retorna dict com early_side, bid_240, bid_180, el_vel, f3_ok."""
    s240 = [s for s in snaps if 181 <= s["secs"] <= 240]
    s180 = [s for s in snaps if 121 <= s["secs"] <= 180]

    if not s240:
        return None

    avg_up240  = sum(s["up_bid"]   for s in s240) / len(s240)
    avg_dn240  = sum(s["down_bid"] for s in s240) / len(s240)

    if avg_up240 >= EL_MIN:
        el_side, bid_240 = "UP", avg_up240
    elif avg_dn240 >= EL_MIN:
        el_side, bid_240 = "DOWN", avg_dn240
    else:
        return None

    bid_180, el_vel, f3_ok = 0.0, 0.0, False
    if s180:
        bids = [s["up_bid"] if el_side == "UP" else s["down_bid"] for s in s180]
        bid_180 = sum(bids) / len(bids)
        el_vel  = round(bid_180 - bid_240, 4)
        f3_ok   = min(bids) >= CONT_MIN

    return {
        "side":    el_side,
        "bid_240": round(bid_240, 4),
        "bid_180": round(bid_180, 4),
        "el_vel":  el_vel,
        "f3_ok":   f3_ok,
        "n_240":   len(s240),
        "n_180":   len(s180),
    }


# ---------------------------------------------------------------------------
# Simulação EE para um slug
# ---------------------------------------------------------------------------

def simulate_ee(snaps, el):
    """Simula uma entrada EE. Retorna dict de resultado ou None."""
    if not el:
        return None
    if not el["f3_ok"] or el["el_vel"] < VEL_MIN:
        return None

    el_side   = el["side"]
    opp_side  = "DOWN" if el_side == "UP" else "UP"

    # Snaps na janela de entrada (secs 30–180), ordenados do mais velho para novo
    entry_snaps = sorted(
        [s for s in snaps if 30 <= s["secs"] <= 180],
        key=lambda x: x["secs"], reverse=True,   # secs desc = cronológico
    )

    ep = None           # entry price
    entry_secs = None
    hedge_ep = None     # hedge entry price
    hedge_secs = None
    peak_bid = 0.0

    for s in entry_snaps:
        bid_el  = s["up_bid"]  if el_side == "UP" else s["down_bid"]
        bid_opp = s["down_bid"] if el_side == "UP" else s["up_bid"]

        if ep is None:
            # Condição de entrada
            if ENTRY_LO <= bid_el <= ENTRY_HI:
                ep = bid_el
                entry_secs = s["secs"]
            continue

        # Já em posição
        peak_bid = max(peak_bid, bid_el)

        if hedge_ep is None and bid_el < HEDGE_THR:
            hedge_ep   = bid_opp
            hedge_secs = s["secs"]

    if ep is None:
        return None

    # Resolução — snaps nos últimos 35s
    res_snaps = sorted(
        [s for s in snaps if s["secs"] <= 35],
        key=lambda x: x["secs"],
    )
    if not res_snaps:
        return None

    last = res_snaps[0]  # secs mais baixo disponível
    bid_el_final  = last["up_bid"] if el_side == "UP" else last["down_bid"]
    bid_opp_final = last["down_bid"] if el_side == "UP" else last["up_bid"]

    el_won  = bid_el_final  >= 0.85
    opp_won = bid_opp_final >= 0.85

    if el_won:
        pnl   = round((1.0 - ep) * QTY, 4)
        if hedge_ep:
            pnl += round((0.0 - hedge_ep) * QTY, 4)   # hedge perdeu
            outcome = "WIN_BOUNCE"
        else:
            outcome = "WIN"
    elif opp_won:
        pnl = round((0.0 - ep) * QTY, 4)   # EL perdeu
        if hedge_ep:
            pnl += round((1.0 - hedge_ep) * QTY, 4)   # hedge ganhou
            outcome = "WIN_HEDGE"
        else:
            outcome = "REVERSAL"
    else:
        return None   # mercado ainda não resolvido nos dados disponíveis

    return {
        "el_side":    el_side,
        "ep":         round(ep, 4),
        "entry_secs": entry_secs,
        "hedge_ep":   round(hedge_ep, 4) if hedge_ep else None,
        "hedge_secs": hedge_secs,
        "peak_bid":   round(peak_bid, 4),
        "outcome":    outcome,
        "pnl":        round(pnl, 4),
        "el_vel":     el["el_vel"],
        "bid_240":    el["bid_240"],
        "bid_180":    el["bid_180"],
        "n_240":      el["n_240"],
        "n_180":      el["n_180"],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Carregando snapshots do runner real...")
    all_slugs = load_runner_snapshots(LOG_DIR)
    print(f"  {len(all_slugs)} slugs encontrados\n")

    results = []
    skipped_no_el    = 0
    skipped_no_f3    = 0
    skipped_vel      = 0
    skipped_no_entry = 0
    skipped_no_res   = 0

    for slug, snaps in sorted(all_slugs.items()):
        el = reconstruct_el(snaps)
        if not el:
            skipped_no_el += 1
            continue
        if not el["f3_ok"]:
            skipped_no_f3 += 1
            continue
        if el["el_vel"] < VEL_MIN:
            skipped_vel += 1
            continue

        res = simulate_ee(snaps, el)
        if res is None:
            # Determinar razão
            entry_snaps = [s for s in snaps if 30 <= s["secs"] <= 180]
            if not any(ENTRY_LO <= (s["up_bid"] if el["side"] == "UP" else s["down_bid"]) <= ENTRY_HI
                       for s in entry_snaps):
                skipped_no_entry += 1
            else:
                skipped_no_res += 1
            continue

        results.append((slug, res))

    # Relatório
    print("=" * 85)
    print("SIMULAÇÃO EARLY ENTRY — LOGS RUNNER REAL (hoje)")
    print(f"Parâmetros: F3 ok + el_vel>={VEL_MIN} + bid {ENTRY_LO}-{ENTRY_HI} + hedge<{HEDGE_THR}")
    print("=" * 85)

    if not results:
        print("Nenhum trade simulado.")
    else:
        print(f"{'#':>3} | {'Slug[-18:]':18} | {'EL':4} | {'ep':5} | {'sec':4} | {'vel':5} | {'hedge':5} | {'peak':5} | {'outcome':10} | {'PnL':>8}")
        print("-" * 85)
        total = 0.0
        nw, nl = 0, 0
        for i, (slug, r) in enumerate(results, 1):
            ts = slug[-18:]
            hedge_str = f"{r['hedge_ep']:.2f}" if r["hedge_ep"] else "  —  "
            pnl_str   = f"{r['pnl']:>+8.4f}"
            print(f"{i:>3} | {ts:18} | {r['el_side']:4} | {r['ep']:.2f} | {r['entry_secs']:>4} | {r['el_vel']:.3f} | {hedge_str:5} | {r['peak_bid']:.2f} | {r['outcome']:<10} | {pnl_str}")
            total += r["pnl"]
            if "WIN" in r["outcome"]: nw += 1
            else: nl += 1
        print("-" * 85)
        nc = nw + nl
        wr = f"{nw/nc*100:.0f}%" if nc else "n/a"
        print(f"Trades: {nc}  ({nw}W / {nl}L  WR={wr})  PnL total: {total:>+.4f} USD  avg/trade: {total/nc:>+.4f}")

    print(f"\nFiltrados:")
    print(f"  Sem EL detectado (secs 181-240 vazios/fracos): {skipped_no_el}")
    print(f"  EL sem F3 (bid caiu abaixo de 0.70 em 121-180s): {skipped_no_f3}")
    print(f"  el_vel < {VEL_MIN} (EL não ganhou velocidade): {skipped_vel}")
    print(f"  EL ok mas bid fora da faixa 0.82-0.86 na janela 30-180s: {skipped_no_entry}")
    print(f"  Sem resolução nos dados (mercado ainda aberto ou dados parciais): {skipped_no_res}")
    print(f"  Total slugs: {len(all_slugs)}")

if __name__ == "__main__":
    main()
