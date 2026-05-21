"""
market/live_reversal_sniper_v1.py

Reversal Sniper — Fase 1: paper only (zero ordens reais).

Monitora mercados 5-min BTC onde o lado "quase vencedor" está a >= 0.88
e secs_to_end entre 15 e 120. Quando Sinal A (divergência BTC oracle) e/ou
Sinal B (desaceleração bid) disparam com score >= 4, registra would_enter=True
no log JSONL mas NÃO posta nenhuma ordem.

REGRAS INEGOCIÁVEIS:
1. Fase 1 = somente logging. Nenhuma ordem real até validação dos logs paper.
2. Nunca misturar bankroll com current_almost_resolved.
3. analyze_reversal_candidates_on_logs.py DEVE ser rodado antes de ativar modo real.
4. Position sizing real será FIXO — nunca all-in.
5. Logs separados por data — nunca sobrescrever logs anteriores.
6. Se win_rate real cair abaixo de 5% em 100+ trades, pausar e revisar sinais.

Uso:
    python market/live_reversal_sniper_v1.py
    python market/live_reversal_sniper_v1.py --seconds 3600
    python -m market.live_reversal_sniper_v1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from datetime import date
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple

CONFIG_PATH = Path(__file__).parent.parent / "agent" / "config.json"

def _load_sniper_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f).get("reversal_sniper", {})
    except Exception:
        return {}

# ---- market modules ----
from market.binance_ws_v1 import BinanceTickFeed
from market.chainlink_oracle import ChainlinkBTCOracle
from market.rest_5m_shadow_public_v4 import (
    _build_slot_bundle,
    _compute_executable_metrics,
    _fetch_slot_state,
    _slot_snapshot,
)

# ---------------------------------------------------------------------------
# Parâmetros — Fase 1
# ---------------------------------------------------------------------------

MIN_WINNER_BID = 0.88       # active_bid mínimo para entrar na zona de monitoramento
MAX_LOSER_PRICE = 0.15      # loser muito caro → edge sumiu
MIN_SECS = 15               # muito perto da resolução → skip
MAX_SECS = 120              # muito cedo → skip

SCORE_THRESHOLD_PAPER = 4   # score mínimo para registrar would_enter
COOLDOWN_SECS = 60.0        # cooldown por mercado após qualquer would_enter

BID_HISTORY_SIZE = 6        # polls mantidos para cálculo de velocidade
BID_DECEL_POLLS = 3         # velocity = bid_now - bid[N-3]

PAPER_BET_SIZE = 20.0       # $ simulados por trade (apenas para log de contexto)

# Sinal B thresholds
SINAL_B_WEAK_VEL = -0.020   # peso 2
SINAL_B_STRONG_VEL = -0.050 # peso 3

# Sinal A threshold
SINAL_A_BPS = 100.0         # divergência mínima oracle vs open (contra o vencedor)

# Sinal E thresholds (loser momentum — para reversal_scalp)
SINAL_E_WEAK_MOM = 0.005    # peso 1
SINAL_E_STRONG_MOM = 0.015  # peso 2
LOSER_HISTORY_POLLS = 2     # momentum = loser_bid_now - loser_bid_2_polls_ago

# Saída antecipada inteligente — paper tracking (não altera entrada)
EARLY_EXIT_PROFIT_MULT   = 1.80  # cond. a: bid >= entry × 1.80 (lucro ≥ 80%)
EARLY_EXIT_PULLBACK_GATE = 1.30  # cond. c: max_bid >= entry × 1.30 para ativar dynamic_stop
EARLY_EXIT_PULLBACK_FRAC = 0.60  # cond. c: bid atual < max_bid × 0.60 → saída


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sf(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _build_log_path() -> Path:
    today = date.today().strftime("%Y%m%d")
    return Path("logs") / f"reversal_sniper_paper_{today}.jsonl"


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Deceleration gate — igual ao live runner
# ---------------------------------------------------------------------------

class _EarlyLeaderTracker:
    """Rastreia o early leader (EL) por slug ao longo de todos os polls.

    Janelas:
      secs 181-240: detecta o lado dominante com bid >= early_min (EL)
      secs 121-180: verifica continuidade — EL nunca caiu abaixo de cont_min (F3)
      secs < 180:   detecta inversão quando o líder troca de lado

    Gate para o sniper:
      EL confirma winner → penaliza score (winner provavelmente mantém)
      EL inverte forte (>= strong_min) → bloqueia sniper (novo líder ganha 100%)
      EL inverte médio (0.60–0.72) → penaliza score moderadamente
      Sem EL / inversão fraca → neutro (sinais A/B/E valem face value)
    """

    def __init__(self, early_min: float = 0.55, cont_min: float = 0.70, strong_min: float = 0.72):
        self._early_min = early_min
        self._cont_min = cont_min
        self._strong_min = strong_min
        self._by_slug: dict = {}

    def update(self, slug: str, secs: int, up_bid: float, down_bid: float) -> None:
        if up_bid <= 0 or down_bid <= 0:
            return
        d = self._by_slug.setdefault(slug, {
            "early_side": None, "early_bid": 0.0, "early_secs": None,
            "f3_ok": None, "f3_started": False,
            "inverted": False, "inversion_side": None,
            "inversion_bid": 0.0, "inversion_secs": None,
        })
        leader_bid = max(up_bid, down_bid)
        leader_side = "UP" if up_bid >= down_bid else "DOWN"

        if 181 <= secs <= 240 and d["early_side"] is None:
            if leader_bid >= self._early_min:
                d["early_side"] = leader_side
                d["early_bid"] = round(leader_bid, 4)
                d["early_secs"] = secs

        if 121 <= secs <= 180 and d["early_side"] is not None:
            el_bid_now = up_bid if d["early_side"] == "UP" else down_bid
            if not d["f3_started"]:
                d["f3_started"] = True
                d["f3_ok"] = True
            if el_bid_now < self._cont_min:
                d["f3_ok"] = False

        if secs < 180 and d["early_side"] is not None and not d["inverted"]:
            if leader_side != d["early_side"] and leader_bid >= self._early_min:
                d["inverted"] = True
                d["inversion_side"] = leader_side
                d["inversion_bid"] = round(leader_bid, 4)
                d["inversion_secs"] = secs

    def state(self, slug: str) -> dict:
        d = self._by_slug.get(slug) or {}
        inv_bid = d.get("inversion_bid", 0.0)
        return {
            "early_side": d.get("early_side"),
            "early_bid": d.get("early_bid", 0.0),
            "early_secs": d.get("early_secs"),
            "f3_ok": d.get("f3_ok"),
            "inverted": bool(d.get("inverted")),
            "inversion_side": d.get("inversion_side"),
            "inversion_bid": inv_bid,
            "inversion_secs": d.get("inversion_secs"),
            "inversion_strong": bool(d.get("inverted") and inv_bid >= self._strong_min),
        }

    def gate_score(self, slug: str, winner_side: str) -> int:
        """Retorna penalidade de score (-3 a 0) baseada no EL.

        Negativo = EL prediz que o winner continua → sniper não deve entrar.
        Zero = sem informação EL suficiente → sinais A/B/E valem face value.
        """
        d = self._by_slug.get(slug) or {}
        el_side = d.get("early_side")
        if not el_side:
            return 0  # sem EL detectado — neutro

        inv_bid = d.get("inversion_bid", 0.0)
        inverted = bool(d.get("inverted"))

        if not inverted:
            # EL estável e confirma winner → 89.6% winner mantém
            if el_side == winner_side:
                return -3
            # EL estável mas aponta pro loser → mercado divergiu do EL → incerto
            return 0

        # EL inverteu — o winner agora é o novo líder
        if inv_bid >= self._strong_min:
            return -3   # inversão forte: novo líder ganha 100% (19/19)
        if inv_bid >= 0.60:
            return -1   # inversão média: novo líder ganha 87% (26/30)
        return 0        # inversão fraca: só 54% (17/37) — neutro para o sniper

    def evict_old(self, current_slug: str) -> None:
        for slug in [k for k in self._by_slug if k != current_slug]:
            del self._by_slug[slug]


class _LoserMomentumTracker:
    """Rastreia bid do lado perdedor para detectar Sinal E (momentum do loser)."""

    def __init__(self):
        self._history: Dict[str, Deque[Tuple[float, float]]] = {}

    def update(self, slug: str, loser_side: str, loser_bid: float, now: float) -> dict:
        key = f"{slug}:{loser_side}"
        hist = self._history.setdefault(key, deque(maxlen=6))
        hist.append((now, loser_bid))

        if len(hist) < LOSER_HISTORY_POLLS + 1:
            return {"loser_bid": loser_bid, "loser_momentum": None, "score": 0}

        loser_2ago = list(hist)[-LOSER_HISTORY_POLLS - 1][1]
        momentum = round(loser_bid - loser_2ago, 6)

        if momentum > SINAL_E_STRONG_MOM:
            score = 2
        elif momentum > SINAL_E_WEAK_MOM:
            score = 1
        else:
            score = 0

        return {"loser_bid": loser_bid, "loser_momentum": momentum, "score": score}

    def evict_old_slugs(self, current_slug: str) -> None:
        stale = [k for k in self._history if not k.startswith(current_slug + ":")]
        for k in stale:
            del self._history[k]


class _DecelTracker:
    """Rastreia histórico de bid de um mercado (slug+side) para detectar desaceleração."""

    def __init__(self):
        self._history: Dict[str, Deque[Tuple[float, float]]] = {}

    def update(self, slug: str, side: str, bid: float, now: float) -> dict:
        key = f"{slug}:{side}"
        hist = self._history.setdefault(key, deque(maxlen=BID_HISTORY_SIZE))
        hist.append((now, bid))

        if len(hist) < BID_DECEL_POLLS + 1:
            return {"bid_now": bid, "bid_velocity": None, "would_block": False, "score": 0}

        bid_3ago = list(hist)[-BID_DECEL_POLLS - 1][1]
        velocity = round(bid - bid_3ago, 6)

        if velocity < SINAL_B_STRONG_VEL:
            score = 3
        elif velocity < SINAL_B_WEAK_VEL:
            score = 2
        else:
            score = 0

        return {
            "bid_now": bid,
            "bid_velocity": velocity,
            "would_block": score > 0,
            "score": score,
        }

    def evict_old_slugs(self, current_slug: str) -> None:
        stale = [k for k in self._history if not k.startswith(current_slug + ":")]
        for k in stale:
            del self._history[k]


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_reversal_sniper_paper(
    run_for: float = float("inf"),
    poll_secs: float = 1.0,
) -> None:
    log_path = _build_log_path()
    print(f"[REVERSAL_SNIPER] Iniciando — modo PAPER ONLY", flush=True)
    print(f"[REVERSAL_SNIPER] Log: {log_path}", flush=True)
    print(f"[REVERSAL_SNIPER] Parâmetros: min_bid={MIN_WINNER_BID} max_loser={MAX_LOSER_PRICE} "
          f"secs=[{MIN_SECS},{MAX_SECS}] score>={SCORE_THRESHOLD_PAPER}", flush=True)

    _append_jsonl(log_path, {
        "type": "session_start",
        "ts": time.time(),
        "params": {
            "min_winner_bid": MIN_WINNER_BID,
            "max_loser_price": MAX_LOSER_PRICE,
            "min_secs": MIN_SECS,
            "max_secs": MAX_SECS,
            "score_threshold": SCORE_THRESHOLD_PAPER,
            "cooldown_secs": COOLDOWN_SECS,
            "paper_bet_size": PAPER_BET_SIZE,
            "sinal_a_bps": SINAL_A_BPS,
            "sinal_b_weak_vel": SINAL_B_WEAK_VEL,
            "sinal_b_strong_vel": SINAL_B_STRONG_VEL,
            "early_exit_profit_mult": EARLY_EXIT_PROFIT_MULT,
            "early_exit_pullback_gate": EARLY_EXIT_PULLBACK_GATE,
            "early_exit_pullback_frac": EARLY_EXIT_PULLBACK_FRAC,
        },
    })

    oracle = ChainlinkBTCOracle(cache_secs=5.0)
    btc_feed = BinanceTickFeed()
    btc_feed.start()

    decel = _DecelTracker()
    loser_mom = _LoserMomentumTracker()
    el_tracker = _EarlyLeaderTracker(early_min=0.55, cont_min=0.70, strong_min=0.72)
    oracle_open_prices: Dict[str, float] = {}   # slug → oracle_price ao início do slug
    cooldowns: Dict[str, float] = {}            # slug → ts fim do cooldown
    paper_positions: Dict[str, dict] = {}       # slug → paper position ativa

    started_at = time.time()

    while time.time() - started_at < run_for:
        now = time.time()
        try:
            slot_bundle = _build_slot_bundle()
            current_item = slot_bundle["queue"].get("current")

            if not current_item:
                time.sleep(poll_secs)
                continue

            slug = str(current_item.get("slug") or "")
            if not slug:
                time.sleep(poll_secs)
                continue

            raw_secs = current_item.get("seconds_to_end")
            current_secs = int(raw_secs) if raw_secs is not None else None

            # ---- fechar posições de slugs anteriores (mercado mudou) ----
            for _stale in list(paper_positions.keys()):
                if _stale != slug:
                    _sp = paper_positions.pop(_stale)
                    _append_jsonl(log_path, {
                        "type": "position_expired",
                        "ts": now,
                        "slug": _stale,
                        "reason": "market_changed",
                        "entry_score": _sp["entry_score"],
                        "max_score_seen": _sp.get("max_score_seen", 0),
                        "max_loser_bid_seen": round(_sp["max_loser_bid_seen"], 4),
                        "loser_bid_at_t20s": _sp["loser_bid_at_t20s"],
                        "signal_btc_divergence_faded_at": _sp["signal_btc_divergence_faded_at"],
                        "min_score_during_hold": _sp["min_score_during_hold"],
                        "would_enter_fired": _sp["would_enter_fired"],
                        "would_enter_score": _sp.get("would_enter_score"),
                        "early_exit_triggered": _sp["early_exit_triggered"],
                        "early_exit_reason": _sp["early_exit_reason"],
                        "early_exit_bid": _sp.get("early_exit_bid"),
                        "hold_time_secs": round(now - _sp["entered_at"], 1),
                    })

            # ---- bid data (buscar sempre — EL precisa de dados fora da janela do sniper) ----
            slot_state = _fetch_slot_state(slot_bundle)
            snap = _slot_snapshot(slot_state, "current")
            exec_metrics, exec_reason = _compute_executable_metrics(snap)

            if exec_metrics is None:
                time.sleep(poll_secs)
                continue

            up_bid = _sf(exec_metrics.get("up_bid"))
            down_bid = _sf(exec_metrics.get("down_bid"))

            # Atualiza EL tracker para todos os polls (inclusive secs 181-240, fora da janela)
            if current_secs is not None and up_bid > 0 and down_bid > 0:
                el_tracker.update(slug, current_secs, up_bid, down_bid)
            el_tracker.evict_old(slug)
            el_state = el_tracker.state(slug)

            # ---- fora da janela → skip (posição ativa: continuar tracking) ----
            if current_secs is None or (not (MIN_SECS <= current_secs <= MAX_SECS) and slug not in paper_positions):
                time.sleep(poll_secs)
                continue

            winner_side = "UP" if up_bid >= down_bid else "DOWN"
            winner_bid = up_bid if winner_side == "UP" else down_bid
            loser_price = round(1.0 - winner_bid, 4)

            # ---- fora da zona de monitoramento → skip (posição ativa: tracking continua) ----
            if winner_bid < MIN_WINNER_BID or loser_price <= 0 or loser_price > MAX_LOSER_PRICE:
                if slug not in paper_positions:
                    time.sleep(poll_secs)
                    continue

            # ---- oracle ----
            oracle_price, _oracle_updated_at, _oracle_staleness = oracle.get_price()
            if oracle_price is not None and slug not in oracle_open_prices:
                oracle_open_prices[slug] = oracle_price
            oracle_open = oracle_open_prices.get(slug)

            # ---- Sinal A: divergência BTC oracle vs open (contra o vencedor) ----
            sinal_a_score = 0
            btc_divergence_bps: Optional[float] = None
            if oracle_price is not None and oracle_open and oracle_open > 0:
                btc_divergence_bps = round((oracle_price - oracle_open) / oracle_open * 10_000, 2)
                if winner_side == "UP" and btc_divergence_bps < -SINAL_A_BPS:
                    sinal_a_score = 2   # BTC caiu mas mercado precifica UP como vencedor
                elif winner_side == "DOWN" and btc_divergence_bps > SINAL_A_BPS:
                    sinal_a_score = 2   # BTC subiu mas mercado precifica DOWN como vencedor

            # ---- Sinal B: desaceleração bid (decel gate) ----
            decel.evict_old_slugs(slug)
            decel_info = decel.update(slug, winner_side, winner_bid, now)
            sinal_b_score = decel_info["score"]

            # ---- Sinal E: momentum do loser bid (para coleta de dados reversal_scalp) ----
            loser_side = "DOWN" if winner_side == "UP" else "UP"
            loser_bid = down_bid if loser_side == "DOWN" else up_bid
            loser_mom.evict_old_slugs(slug)
            sinal_e_info = loser_mom.update(slug, loser_side, loser_bid, now)
            sinal_e_score = sinal_e_info["score"]

            el_gate = el_tracker.gate_score(slug, winner_side)
            total_score = sinal_a_score + sinal_b_score + el_gate
            cfg = _load_sniper_config()
            score_threshold = int(cfg.get("entry_score_threshold", SCORE_THRESHOLD_PAPER))
            would_enter = total_score >= score_threshold

            # ---- log snapshot ----
            snap_row = {
                "type": "sniper_snapshot",
                "ts": now,
                "slug": slug,
                "current_secs": current_secs,
                "winner_side": winner_side,
                "winner_bid": round(winner_bid, 4),
                "loser_side": loser_side,
                "loser_bid": round(loser_bid, 4),
                "loser_price": loser_price,
                "up_bid": round(up_bid, 4),
                "down_bid": round(down_bid, 4),
                "oracle_price": oracle_price,
                "oracle_open": oracle_open,
                "btc_divergence_bps": btc_divergence_bps,
                "sinal_a_score": sinal_a_score,
                "sinal_b_score": sinal_b_score,
                "sinal_e_score": sinal_e_score,
                "sinal_e_loser_momentum": sinal_e_info["loser_momentum"],
                "bid_velocity": decel_info["bid_velocity"],
                "total_score": total_score,
                "el_gate_score": el_gate,
                "score_threshold": score_threshold,
                "would_enter": would_enter,
                "in_cooldown": cooldowns.get(slug, 0) > now,
                "btc_price": btc_feed.current_price() or None,
                "btc_tick_direction": btc_feed.tick_direction_score(8.0),
                "early_leader": el_state,
            }
            _append_jsonl(log_path, snap_row)

            # ---- Atualizar posição paper ativa ----
            if slug in paper_positions:
                _pos = paper_positions[slug]
                _ps = _pos["entry_loser_side"]
                _pb = round(up_bid if _ps == "UP" else down_bid, 4)
                _entry = _pos["entry_loser_price"]

                _pos["max_loser_bid_seen"] = max(_pos["max_loser_bid_seen"], _pb)
                _pos["min_score_during_hold"] = min(_pos["min_score_during_hold"], total_score)
                _pos["max_score_seen"] = max(_pos.get("max_score_seen", 0), total_score)
                if current_secs is not None and current_secs <= 20 and _pos["loser_bid_at_t20s"] is None:
                    _pos["loser_bid_at_t20s"] = _pb
                if (_pos["signal_btc_divergence_faded_at"] is None
                        and _pos.get("entry_sinal_a_score", 0) > 0
                        and sinal_a_score == 0):
                    _pos["signal_btc_divergence_faded_at"] = now

                # ---- should_exit_early() — spec §2 ----
                if not _pos["early_exit_triggered"]:
                    _max = _pos["max_loser_bid_seen"]
                    _why: Optional[str] = None

                    # a) lucro >= 80% E Sinal A não mais ativo
                    if _pb >= _entry * EARLY_EXIT_PROFIT_MULT and sinal_a_score == 0:
                        _why = "partial_profit_signal_faded"
                    # b) score < 2 E bid acima da entrada
                    elif total_score < 2 and _pb > _entry:
                        _why = "score_collapsed_take_profit"
                    # c) pico >= 1.30× entrada E bid recuou abaixo de 60% do pico
                    elif _max >= _entry * EARLY_EXIT_PULLBACK_GATE and _pb < _max * EARLY_EXIT_PULLBACK_FRAC:
                        _why = "dynamic_stop_pullback"

                    if _why:
                        _pos["early_exit_triggered"] = True
                        _pos["early_exit_reason"] = _why
                        _pos["early_exit_bid"] = _pb
                        _pos["early_exit_ts"] = now
                        _ret = round((_pb / _entry - 1) * 100, 1) if _entry > 0 else None
                        _append_jsonl(log_path, {
                            "type": "would_exit_early",
                            "ts": now,
                            "slug": slug,
                            "current_secs": current_secs,
                            "exit_reason": _why,
                            "loser_bid": _pb,
                            "entry_loser_price": _entry,
                            "max_loser_bid_seen": round(_max, 4),
                            "loser_bid_at_t20s": _pos["loser_bid_at_t20s"],
                            "signal_btc_divergence_faded_at": _pos["signal_btc_divergence_faded_at"],
                            "min_score_during_hold": _pos["min_score_during_hold"],
                            "pnl_per_share": round(_pb - _entry, 4),
                            "return_pct": _ret,
                            "sinal_a_score": sinal_a_score,
                            "total_score": total_score,
                        })
                        print(
                            f"[SNIPER] would_exit_early slug={slug} reason={_why} "
                            f"bid={_pb:.4f} entry={_entry:.4f} ret={_ret}% secs={current_secs}",
                            flush=True,
                        )

                # ---- Resolução da posição ----
                if current_secs is not None and current_secs <= 2:
                    _pb_final = round(up_bid if _ps == "UP" else down_bid, 4)
                    _reversal = _pb_final > 0.5
                    _pnl_hold = round((1.0 - _entry) if _reversal else -_entry, 4)
                    _pnl_early = (
                        round(_pos["early_exit_bid"] - _entry, 4)
                        if _pos["early_exit_triggered"] else None
                    )
                    _append_jsonl(log_path, {
                        "type": "position_resolved",
                        "ts": now,
                        "slug": slug,
                        "current_secs": current_secs,
                        "reversal_happened": _reversal,
                        "entry_winner_side": _pos["entry_winner_side"],
                        "entry_loser_side": _ps,
                        "entry_loser_price": _entry,
                        "entry_secs": _pos["entry_secs"],
                        "entry_score": _pos["entry_score"],
                        "loser_bid_at_resolution": _pb_final,
                        "max_loser_bid_seen": round(_pos["max_loser_bid_seen"], 4),
                        "max_score_seen": _pos.get("max_score_seen", 0),
                        "loser_bid_at_t20s": _pos["loser_bid_at_t20s"],
                        "signal_btc_divergence_faded_at": _pos["signal_btc_divergence_faded_at"],
                        "min_score_during_hold": _pos["min_score_during_hold"],
                        "would_enter_fired": _pos["would_enter_fired"],
                        "would_enter_score": _pos.get("would_enter_score"),
                        "early_exit_triggered": _pos["early_exit_triggered"],
                        "early_exit_reason": _pos["early_exit_reason"],
                        "early_exit_bid": _pos.get("early_exit_bid"),
                        "hold_time_secs": round(now - _pos["entered_at"], 1),
                        "pnl_hold": _pnl_hold,
                        "pnl_early_exit": _pnl_early,
                        "ev_delta": round(_pnl_early - _pnl_hold, 4) if _pnl_early is not None else None,
                    })
                    if _reversal and not _pos["would_enter_fired"]:
                        print(
                            f"[SNIPER] REVERSAO sem sinal detectado! slug={slug} "
                            f"max_score={_pos.get('max_score_seen', 0)} "
                            f"loser_entry={_entry:.3f} loser_final={_pb_final:.3f}",
                            flush=True,
                        )
                    paper_positions.pop(slug, None)

            # ---- Iniciar observação para todo mercado que entra na zona ----
            # Independente de score — para capturar reversões mesmo sem sinal conhecido.
            in_zone = (winner_bid >= MIN_WINNER_BID
                       and 0 < loser_price <= MAX_LOSER_PRICE
                       and current_secs is not None
                       and MIN_SECS <= current_secs <= MAX_SECS)

            if in_zone and slug not in paper_positions:
                paper_positions[slug] = {
                    "slug": slug,
                    "entered_at": now,
                    "entry_loser_price": loser_price,
                    "entry_secs": current_secs,
                    "entry_score": total_score,
                    "entry_sinal_a_score": sinal_a_score,
                    "entry_winner_side": winner_side,
                    "entry_loser_side": "DOWN" if winner_side == "UP" else "UP",
                    "max_loser_bid_seen": loser_price,
                    "max_score_seen": total_score,
                    "loser_bid_at_t20s": None,
                    "signal_btc_divergence_faded_at": None,
                    "min_score_during_hold": total_score,
                    "would_enter_fired": False,
                    "would_enter_ts": None,
                    "would_enter_score": None,
                    "early_exit_triggered": False,
                    "early_exit_reason": None,
                    "early_exit_bid": None,
                    "early_exit_ts": None,
                }

            # Atualizar max_score da observação
            if slug in paper_positions:
                paper_positions[slug]["max_score_seen"] = max(
                    paper_positions[slug].get("max_score_seen", 0), total_score
                )

            # ---- would_enter: sinal atingiu threshold — registrar evento ----
            if would_enter and cooldowns.get(slug, 0) <= now:
                if slug in paper_positions and not paper_positions[slug]["would_enter_fired"]:
                    paper_positions[slug]["would_enter_fired"] = True
                    paper_positions[slug]["would_enter_ts"] = now
                    paper_positions[slug]["would_enter_score"] = total_score

                shares = PAPER_BET_SIZE / loser_price
                _append_jsonl(log_path, {
                    "type": "would_enter",
                    "ts": now,
                    "slug": slug,
                    "current_secs": current_secs,
                    "winner_side": winner_side,
                    "winner_bid": round(winner_bid, 4),
                    "sniper_side": "DOWN" if winner_side == "UP" else "UP",
                    "loser_price": loser_price,
                    "paper_bet_size": PAPER_BET_SIZE,
                    "paper_shares": round(shares, 2),
                    "paper_target_pnl": round(shares * 1.0 - PAPER_BET_SIZE, 2),
                    "total_score": total_score,
                    "sinal_a_score": sinal_a_score,
                    "sinal_b_score": sinal_b_score,
                    "sinal_e_score": sinal_e_score,
                    "el_gate_score": el_gate,
                    "sinal_e_loser_momentum": sinal_e_info["loser_momentum"],
                    "bid_velocity": decel_info["bid_velocity"],
                    "btc_divergence_bps": btc_divergence_bps,
                    "oracle_price": oracle_price,
                    "oracle_open": oracle_open,
                    "btc_price": btc_feed.current_price() or None,
                    "btc_tick_direction": btc_feed.tick_direction_score(8.0),
                    "early_leader": el_state,
                })
                cooldowns[slug] = now + COOLDOWN_SECS
                print(
                    f"[SNIPER] would_enter slug={slug} side={'DOWN' if winner_side == 'UP' else 'UP'} "
                    f"loser={loser_price:.3f} score={total_score} "
                    f"(A={sinal_a_score} B={sinal_b_score}) secs={current_secs}",
                    flush=True,
                )

        except KeyboardInterrupt:
            break
        except Exception as exc:
            _append_jsonl(log_path, {
                "type": "error",
                "ts": time.time(),
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"[SNIPER] Erro no loop: {exc!r}", flush=True)
            time.sleep(poll_secs)
            continue

        time.sleep(poll_secs)

    btc_feed.stop()
    _append_jsonl(log_path, {"type": "session_end", "ts": time.time()})
    print(f"[REVERSAL_SNIPER] Sessão encerrada. Log: {log_path}", flush=True)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Reversal Sniper — Fase 1 (paper only)")
    parser.add_argument("--seconds", type=float, default=float("inf"),
                        help="Duração máxima em segundos (padrão: infinito)")
    parser.add_argument("--poll", type=float, default=1.0,
                        help="Intervalo de polling em segundos (padrão: 1.0)")
    args = parser.parse_args()

    run_reversal_sniper_paper(
        run_for=args.seconds,
        poll_secs=args.poll,
    )


if __name__ == "__main__":
    main()
