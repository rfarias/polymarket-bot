"""
_sim_preinversion_buy.py

Simula o setup "compra antecipada de inversão":
  - Trigger: F3 começa a falhar (bid EL cai abaixo de cont_min na janela 181-121s)
    enquanto el_vel < 0.05 (EL fraco desde o início)
  - Entry: compra o lado OPOSTO ao EL (que está se tornando o novo líder)
    no snap em que o dip é detectado — preço ~0.30-0.45
  - Exit A: hold to resolution
  - Exit B: saída antecipada quando bid do lado comprado atinge target_exit (ex: 0.80)
  - Exit C: dynamic stop — sai quando bid recua > pullback_frac do pico
             (captura quando a inversão volta antes de completar)

Comparativo:
  Baseline: entrada EL estável (el_vel >= 0.08, F3 ok) — Early Entry clássico
  Novo setup: pré-inversão, entrada mais barata, risco maior

Uso:
    python _sim_preinversion_buy.py
    python _sim_preinversion_buy.py --qty 3 --target-exit 0.80 --pullback 0.65
"""
from __future__ import annotations

import argparse, json, sys, io
from pathlib import Path
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Parâmetros do precursor
EL_MIN       = 0.55
CONT_MIN     = 0.70   # F3 threshold
VEL_MAX      = 0.05   # EL fraco: el_vel abaixo disto

# Saída antecipada padrão
TARGET_EXIT  = 0.80   # bid atingiu 80% → sai com lucro
PULLBACK     = 0.65   # bid recuou abaixo de 65% do pico → sai (dynamic stop)

QTY          = 3.0    # qty padrão para setup mais arriscado


def _sf(v, d=0.0):
    try: return float(v)
    except: return d


# ---------------------------------------------------------------------------
# Carregar logs
# ---------------------------------------------------------------------------

def load_slugs(log_dir="logs"):
    by_slug = defaultdict(list)
    for path in sorted(Path(log_dir).glob("market_monitor_*.jsonl")):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try: d = json.loads(line)
                except: continue
                if d.get("type") != "monitor_snap" or d.get("slot") != "current":
                    continue
                slug = d.get("slug"); secs = d.get("secs")
                if not slug or secs is None: continue
                by_slug[slug].append({
                    "secs":       int(secs),
                    "up_bid":     _sf(d.get("up_bid")),
                    "down_bid":   _sf(d.get("down_bid")),
                    "el_leader":  d.get("el_leader"),
                    "el_vel":     _sf(d.get("el_vel")),
                    "el_cont_ok": bool(d.get("el_cont_ok")),
                    "ts":         _sf(d.get("ts")),
                })
    result = {}
    for slug, snaps in by_slug.items():
        seen = set(); unique = []
        for s in sorted(snaps, key=lambda x: x["ts"]):
            if s["secs"] not in seen:
                seen.add(s["secs"]); unique.append(s)
        if len(unique) >= 8:
            result[slug] = sorted(unique, key=lambda x: x["secs"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# Extrai estado EL
# ---------------------------------------------------------------------------

def get_el_state(snaps):
    """Retorna el_leader, el_vel, had_f3_dip, dip_secs (secs onde dip ocorreu)."""
    s240 = [s for s in snaps if 181 <= s["secs"] <= 240]
    s180 = [s for s in snaps if 121 <= s["secs"] <= 180]

    if not s240:
        return None

    avg_up = sum(s["up_bid"] for s in s240) / len(s240)
    avg_dn = sum(s["down_bid"] for s in s240) / len(s240)
    if avg_up >= EL_MIN:
        el_side, el_bid_240 = "UP", avg_up
    elif avg_dn >= EL_MIN:
        el_side, el_bid_240 = "DOWN", avg_dn
    else:
        return None

    # el_vel: usa campo pré-calculado do monitor quando disponível
    vel_vals = [s["el_vel"] for s in s180 if s.get("el_vel") is not None]
    if vel_vals:
        el_vel = vel_vals[-1]
    elif s240:
        # aproximação: bid médio 180 - bid 240
        bids180 = [s["up_bid"] if el_side == "UP" else s["down_bid"] for s in s180]
        if bids180:
            el_vel = round(sum(bids180) / len(bids180) - el_bid_240, 4)
        else:
            el_vel = None
    else:
        el_vel = None

    # Detectar dip na janela 181-121s (F3 falha)
    dip_secs = None
    for s in sorted(s180, key=lambda x: x["secs"], reverse=True):  # mais cedo primeiro (secs alto)
        el_bid = s["up_bid"] if el_side == "UP" else s["down_bid"]
        if el_bid < CONT_MIN:
            dip_secs = s["secs"]
            break

    return {
        "el_leader":  el_side,
        "el_vel":     el_vel,
        "had_f3_dip": dip_secs is not None,
        "dip_secs":   dip_secs,
    }


# ---------------------------------------------------------------------------
# Simula "compra antecipada de inversão"
# ---------------------------------------------------------------------------

def sim_preinversion(snaps, el_state, qty, target_exit, pullback_frac):
    """
    Entry: no snap em que o bid EL cai abaixo de CONT_MIN pela primeira vez
           (snap de dip). Compra o lado OPOSTO ao EL.
    """
    if not el_state or not el_state.get("had_f3_dip"):
        return None
    if el_state.get("el_vel") is None or el_state["el_vel"] >= VEL_MAX:
        return None

    el_side = el_state["el_leader"]
    opp_side = "DOWN" if el_side == "UP" else "UP"
    dip_secs = el_state["dip_secs"]

    # Encontrar snap de entry: primeiro snap com dip (secs = dip_secs)
    entry_snap = next((s for s in snaps if s["secs"] == dip_secs), None)
    if entry_snap is None:
        # Pegar snap mais próximo
        entry_snap = min(
            (s for s in snaps if 121 <= s["secs"] <= 181),
            key=lambda x: abs(x["secs"] - dip_secs), default=None
        )
    if entry_snap is None:
        return None

    entry_price = entry_snap["up_bid"] if opp_side == "UP" else entry_snap["down_bid"]
    if entry_price <= 0.02:  # sem mercado ainda
        return None

    # Snaps após a entrada (secs menores)
    post = sorted(
        [s for s in snaps if s["secs"] < entry_snap["secs"]],
        key=lambda x: x["secs"], reverse=True  # ordem cronológica
    )
    if not post:
        return None

    # --- Exit A: hold to resolution ---
    last = post[-1]  # snap final (menor secs)
    final_bid = last["up_bid"] if opp_side == "UP" else last["down_bid"]
    winner_at_end = "UP" if last["up_bid"] >= last["down_bid"] else "DOWN"
    resolved_ok = (winner_at_end == opp_side and last["secs"] <= 15)
    pnl_hold = round(((1.0 - entry_price) if resolved_ok else -entry_price) * qty, 4)

    # --- Rastrear trajetória do bid comprado ---
    bid_seq = [entry_price]
    for s in post:
        b = s["up_bid"] if opp_side == "UP" else s["down_bid"]
        if b > 0:
            bid_seq.append(b)
    max_bid = max(bid_seq)
    min_bid_post = min(bid_seq[1:]) if len(bid_seq) > 1 else entry_price

    # --- Exit B: saída ao atingir target_exit ---
    exit_b_price = None; exit_b_secs = None
    for s in post:
        b = s["up_bid"] if opp_side == "UP" else s["down_bid"]
        if b >= target_exit:
            exit_b_price = b; exit_b_secs = s["secs"]; break
    pnl_target = round((exit_b_price - entry_price) * qty, 4) if exit_b_price else pnl_hold

    # --- Exit C: dynamic stop (quando bid volta do pico) ---
    peak = entry_price; exit_c_price = None; exit_c_secs = None
    for s in post:
        b = s["up_bid"] if opp_side == "UP" else s["down_bid"]
        if b <= 0: continue
        if b > peak:
            peak = b
        # Stop: peak atingiu >= target_exit × 0.80 E bid recuou abaixo de pullback × peak
        elif peak >= target_exit * 0.85 and b < peak * pullback_frac:
            exit_c_price = b; exit_c_secs = s["secs"]; break
    if exit_c_price is None and exit_b_price:
        exit_c_price = exit_b_price; exit_c_secs = exit_b_secs  # usa target se não deu pullback
    pnl_dynstop = round((exit_c_price - entry_price) * qty, 4) if exit_c_price else pnl_hold

    return {
        "entry_price":   round(entry_price, 4),
        "entry_secs":    entry_snap["secs"],
        "opp_side":      opp_side,
        "el_vel":        el_state["el_vel"],
        "max_bid":       round(max_bid, 4),
        "min_bid_post":  round(min_bid_post, 4),
        "reached_target": max_bid >= target_exit,
        "final_bid":     round(final_bid, 4),
        "resolved_ok":   resolved_ok,
        # PnL por estratégia de saída
        "pnl_hold":      pnl_hold,
        "pnl_target":    pnl_target,
        "pnl_dynstop":   pnl_dynstop,
        "exit_b_secs":   exit_b_secs,
        "exit_c_secs":   exit_c_secs,
        "exit_c_price":  round(exit_c_price, 4) if exit_c_price else None,
    }


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

def print_report(label, trades, key, qty, target_exit):
    if not trades: print(f"  {label}: sem trades"); return
    pnls   = [t[key] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    pnl    = sum(pnls)
    wr     = 100 * len(wins) / len(trades)
    avg    = pnl / len(trades)

    eps    = [t["entry_price"] for t in trades]
    maxbs  = [t["max_bid"] for t in trades]
    reached = sum(1 for t in trades if t["reached_target"])

    print(f"  {label}")
    print(f"    n={len(trades)}  WR={wr:.1f}%  PnL=${pnl:+.2f}  avg=${avg:+.4f}/trade")
    print(f"    entry_price: avg={sum(eps)/len(eps):.3f}  min={min(eps):.3f}  max={max(eps):.3f}")
    print(f"    max_bid após entry: avg={sum(maxbs)/len(maxbs):.3f}  max={max(maxbs):.3f}")
    print(f"    Chegou a target {target_exit}: {reached}/{len(trades)} ({100*reached/len(trades):.0f}%)")
    print(f"    WIN: {len(wins)}  LOSS: {len(losses)}")
    if wins:   print(f"      avg win  = ${sum(wins)/len(wins):+.4f}")
    if losses: print(f"      avg loss = ${sum(losses)/len(losses):+.4f}")

    # Distribuição de max_bid (o quanto a inversão chegou a ir)
    buckets = [(0.0, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 1.01)]
    labels  = ["<0.40 (sem inversão)", "0.40-0.60 (parcial)", "0.60-0.80 (média)", ">=0.80 (completa)"]
    print(f"    Profundidade da inversão (max_bid do lado comprado):")
    for (lo, hi), lbl in zip(buckets, labels):
        ct = sum(1 for t in trades if lo <= t["max_bid"] < hi)
        print(f"      {lbl}: {ct}/{len(trades)} ({100*ct/len(trades):.0f}%)")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qty",          type=float, default=QTY)
    ap.add_argument("--target-exit",  type=float, default=TARGET_EXIT)
    ap.add_argument("--pullback",     type=float, default=PULLBACK)
    ap.add_argument("--log-dir",      default="logs")
    args = ap.parse_args()

    print(f"Carregando logs de '{args.log_dir}'...", flush=True)
    all_slugs = load_slugs(args.log_dir)
    print(f"Slugs carregados: {len(all_slugs)}", flush=True)
    print()

    trades = []
    no_signal = 0; no_entry = 0
    for slug, snaps in all_slugs.items():
        el = get_el_state(snaps)
        if el is None or not el.get("had_f3_dip") or (el.get("el_vel") or 1) >= VEL_MAX:
            no_signal += 1
            continue
        t = sim_preinversion(snaps, el, args.qty, args.target_exit, args.pullback)
        if t is None:
            no_entry += 1
            continue
        trades.append(t)

    print("=" * 70)
    print("  SETUP: COMPRA ANTECIPADA DE INVERSÃO DO EL")
    print(f"  Precursor: el_vel < {VEL_MAX} AND F3 dip (bid EL < {CONT_MIN})")
    print(f"  Entry: compra lado oposto ao EL no snap do dip | qty={args.qty}")
    print(f"  Target exit: bid >= {args.target_exit} | Pullback stop: {args.pullback}x pico")
    print("=" * 70)
    print(f"  Sem sinal precursor: {no_signal} slugs | Sem entry válido: {no_entry}")
    print()

    print_report("EXIT A — Hold to resolution", trades, "pnl_hold",    args.qty, args.target_exit)
    print_report("EXIT B — Saída no target",    trades, "pnl_target",  args.qty, args.target_exit)
    print_report("EXIT C — Dynamic stop",       trades, "pnl_dynstop", args.qty, args.target_exit)

    # Comparativo resumido
    if trades:
        print("=" * 70)
        print("  RESUMO COMPARATIVO DAS SAÍDAS")
        print("=" * 70)
        for key, label in [
            ("pnl_hold",    "Hold to resolution"),
            ("pnl_target",  f"Sair no target ({args.target_exit})"),
            ("pnl_dynstop", "Dynamic stop (pullback)"),
        ]:
            pnls = [t[key] for t in trades]
            wr   = 100 * sum(1 for p in pnls if p > 0) / len(pnls)
            avg  = sum(pnls) / len(pnls)
            total = sum(pnls)
            print(f"  {label:<30} WR={wr:5.1f}%  avg=${avg:+.4f}  total=${total:+.2f}")
        print()

        # Analisa casos de pullback: quando entrou, subiu, voltou antes de resolver
        pullbacks = [t for t in trades
                     if t["exit_c_secs"] is not None and t["exit_c_secs"] > 15
                     and t["max_bid"] >= args.target_exit * 0.85
                     and t["exit_c_price"] is not None and t["exit_c_price"] < t["max_bid"] * 0.85]
        print(f"  Casos com pullback real (subiu e voltou antes da resolução): {len(pullbacks)}")
        if pullbacks:
            pb_pnls = [t["pnl_dynstop"] for t in pullbacks]
            hold_pnls = [t["pnl_hold"] for t in pullbacks]
            print(f"    PnL dynamic stop nesses casos: ${sum(pb_pnls):+.2f}  avg=${sum(pb_pnls)/len(pb_pnls):+.4f}")
            print(f"    PnL hold nesses casos:         ${sum(hold_pnls):+.2f}  avg=${sum(hold_pnls)/len(hold_pnls):+.4f}")
            print(f"    Economia do dynamic stop: ${sum(pb_pnls) - sum(hold_pnls):+.2f}")
        print()

        # Break-even
        print("=" * 70)
        print("  BREAK-EVEN POR FAIXA DE ENTRY_PRICE (qty={:.0f})".format(args.qty))
        print("=" * 70)
        for ep in [0.25, 0.30, 0.35, 0.40, 0.45]:
            win_val  = round((1.0 - ep) * args.qty, 2)
            loss_val = round(ep * args.qty, 2)
            be_wr    = round(100 * loss_val / (win_val + loss_val), 1)
            print(f"  entry={ep:.2f} → win=${win_val:.2f}/trade | loss=${loss_val:.2f}/trade | BE={be_wr:.1f}%")
        print()


if __name__ == "__main__":
    main()
