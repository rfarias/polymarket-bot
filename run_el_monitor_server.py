"""
run_el_monitor_server.py — Servidor EL/EE Monitor para extensão Chrome.

Monitora a API pública da Polymarket em tempo real, computa o estado do
Early Leader e serve os dados via HTTP para a extensão do navegador.

Uso:
    python run_el_monitor_server.py [--port 8766] [--poll 0.5]

A extensão Chrome em browser_extension/polymarket_el_monitor_v1/ consome
http://127.0.0.1:8766/state e exibe os dados na página da Polymarket.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

import requests as _requests

# ---------------------------------------------------------------------------
# Fast bid fetch — 2 chamadas paralelas ao CLOB /price (UP + DOWN bid)
# Substitui _fetch_slot_state() que fazia 18+ chamadas HTTP sequenciais.
# Session persistente reutiliza conexão TCP/TLS: ~100-300ms por ciclo.
# Executor persistente evita criar/destruir threads a cada ciclo (300ms).
# ---------------------------------------------------------------------------
_CLOB = "https://clob.polymarket.com"
_http = _requests.Session()
_http.headers.update({"Accept": "application/json"})
_bid_executor = ThreadPoolExecutor(max_workers=2)


def _fast_bid(token_id: str) -> float:
    try:
        r = _http.get(
            f"{_CLOB}/price",
            params={"token_id": token_id, "side": "BUY"},
            timeout=3.0,
        )
        r.raise_for_status()
        val = r.json().get("price")
        return float(val) if val is not None else 0.0
    except Exception:
        return 0.0


def _fast_fetch_bids(up_token_id: str, down_token_id: str) -> Tuple[float, float]:
    """2 GETs em paralelo — único gargalo de rede por ciclo."""
    up_f = _bid_executor.submit(_fast_bid, up_token_id)
    dn_f = _bid_executor.submit(_fast_bid, down_token_id)
    return up_f.result(timeout=5.0), dn_f.result(timeout=5.0)


def _token_ids_from_bundle(slot_bundle: dict) -> Tuple[str, str]:
    """Extrai token IDs de UP e DOWN do slot atual (dados já cacheados no bundle)."""
    current_slot = (slot_bundle.get("slots") or {}).get("current") or {}
    meta = current_slot.get("meta") or {}
    token_mapping = meta.get("token_mapping") or []
    up_id = next(
        (t["token_id"] for t in token_mapping if str(t.get("outcome", "")).lower() == "up"), ""
    )
    dn_id = next(
        (t["token_id"] for t in token_mapping if str(t.get("outcome", "")).lower() == "down"), ""
    )
    return up_id, dn_id


RUNNER_LIVE_STATE_PATH = pathlib.Path("logs/current_almost_resolved_real_live.json")
RUNNER_STALE_SECS = 10.0


def _read_runner_state() -> Optional[dict]:
    try:
        if not RUNNER_LIVE_STATE_PATH.exists():
            return None
        data = RUNNER_LIVE_STATE_PATH.read_text(encoding="utf-8")
        s = json.loads(data)
        if time.time() - float(s.get("ts", 0)) > RUNNER_STALE_SECS:
            return None
        return s
    except Exception:
        return None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from market.rest_5m_shadow_public_v5 import _build_slot_bundle

# ---------------------------------------------------------------------------
# Parâmetros EE v2 (idênticos ao AR runner)
# ---------------------------------------------------------------------------
EE_EL_MIN = 0.55
EE_CONT_MIN = 0.70
EE_VEL_MIN = 0.13
EE_ENTRY_LO = 0.82
EE_ENTRY_HI = 0.86
EE_MAX_ENTRY_SECS = 155   # gate runner real 2026-05-27
EE_MIN_ENTRY_SECS = 30
EE_N180_MIN = 3           # gate runner real 2026-05-27
EE_STOP_LEVEL = 0.65
EE_PROFIT_PROTECT_BID = 0.88
EE_PROFIT_PROTECT_SECS = 70
EE_HEDGE_THR = 0.50
AR_BID_MIN = 0.87         # AR: bid mínimo do líder para sinal AR
AR_SECS_MAX = 70          # AR: janela de secs para quase-resolução


def _sf(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(d)


# ---------------------------------------------------------------------------
# Early Leader Tracker EE v2
# ---------------------------------------------------------------------------
class _EarlyLeaderTrackerEE:
    """Janela 181-240s = detecção EL, 121-180s = F3/velocidade."""

    def __init__(self) -> None:
        self._slug: Optional[str] = None
        self._s240: list = []
        self._s180: list = []
        self.early_leader: Optional[str] = None
        self.el_bid_240: float = 0.0
        self.el_bid_180: float = 0.0
        self.el_vel: float = 0.0
        self.cont_ok: bool = False

    def _reset(self, slug: str) -> None:
        self._slug = slug
        self._s240 = []
        self._s180 = []
        self.early_leader = None
        self.el_bid_240 = 0.0
        self.el_bid_180 = 0.0
        self.el_vel = 0.0
        self.cont_ok = False

    def update(self, slug: str, secs: Optional[int], up_bid: float, down_bid: float) -> None:
        if secs is None or up_bid <= 0 or down_bid <= 0:
            return
        if slug != self._slug:
            self._reset(slug)
        snap = {"secs": secs, "up_bid": up_bid, "down_bid": down_bid}
        if 181 <= secs <= 240:
            self._s240.append(snap)
        elif 121 <= secs <= 180:
            self._s180.append(snap)
        self._compute_el()
        if self.early_leader:
            self._compute_f3()

    def _compute_el(self) -> None:
        if not self._s240:
            return
        avg_up = sum(s["up_bid"] for s in self._s240) / len(self._s240)
        avg_dn = sum(s["down_bid"] for s in self._s240) / len(self._s240)
        if max(avg_up, avg_dn) < EE_EL_MIN:
            return
        self.early_leader = "UP" if avg_up >= avg_dn else "DOWN"
        self.el_bid_240 = avg_up if self.early_leader == "UP" else avg_dn

    def _compute_f3(self) -> None:
        if not self._s180 or not self.early_leader:
            return
        bids = [
            s["up_bid"] if self.early_leader == "UP" else s["down_bid"]
            for s in self._s180
        ]
        avg = sum(bids) / len(bids)
        self.el_bid_180 = avg
        self.el_vel = avg - self.el_bid_240
        self.cont_ok = min(bids) >= EE_CONT_MIN

    @property
    def signal_ok(self) -> bool:
        return bool(self.early_leader and self.cont_ok and self.el_vel >= EE_VEL_MIN)

    def state_dict(self) -> dict:
        return {
            "early_side": self.early_leader,
            "el_bid_240": round(self.el_bid_240, 4),
            "el_bid_180": round(self.el_bid_180, 4),
            "el_vel": round(self.el_vel, 4),
            "f3_ok": self.cont_ok if self.early_leader else None,
            "signal_ok": self.signal_ok,
            "n_s240": len(self._s240),
            "n_s180": len(self._s180),
        }


# ---------------------------------------------------------------------------
# Early Leader Tracker AR (inversão)
# ---------------------------------------------------------------------------
class _EarlyLeaderTrackerAR:
    """Detecta early leader e inversão para filtro AR (idêntico ao AR runner)."""

    def __init__(self) -> None:
        self._slug: Optional[str] = None
        self._window: list = []
        self.early_side: Optional[str] = None
        self.f3_ok: Optional[bool] = None
        self.inverted: bool = False
        self.inversion_side: Optional[str] = None
        self.inversion_bid: float = 0.0
        self.inversion_strong: bool = False

    def _reset(self, slug: str) -> None:
        self._slug = slug
        self._window = []
        self.early_side = None
        self.f3_ok = None
        self.inverted = False
        self.inversion_side = None
        self.inversion_bid = 0.0
        self.inversion_strong = False

    def update(self, slug: str, secs: Optional[int], up_bid: float, down_bid: float) -> None:
        if secs is None or up_bid <= 0 or down_bid <= 0:
            return
        if slug != self._slug:
            self._reset(slug)
        self._window.append({"secs": secs, "up_bid": up_bid, "down_bid": down_bid})
        if len(self._window) > 300:
            self._window.pop(0)
        self._compute()

    def _compute(self) -> None:
        early = [s for s in self._window if s["secs"] >= 181]
        if not early:
            return
        avg_up = sum(s["up_bid"] for s in early) / len(early)
        avg_dn = sum(s["down_bid"] for s in early) / len(early)
        if max(avg_up, avg_dn) < EE_EL_MIN:
            return
        self.early_side = "UP" if avg_up >= avg_dn else "DOWN"

        f3 = [s for s in self._window if s["secs"] < 181]
        if f3:
            bids = [s["up_bid"] if self.early_side == "UP" else s["down_bid"] for s in f3]
            self.f3_ok = min(bids) >= 0.70

        recent = [s for s in self._window if s["secs"] < 100][-10:]
        if recent and self.early_side:
            opp = "DOWN" if self.early_side == "UP" else "UP"
            opp_bids = [s["down_bid"] if opp == "DOWN" else s["up_bid"] for s in recent]
            avg_opp = sum(opp_bids) / len(opp_bids)
            if avg_opp > 0.55:
                self.inverted = True
                self.inversion_side = opp
                self.inversion_bid = round(avg_opp, 4)
                self.inversion_strong = avg_opp >= 0.72

    def state_dict(self) -> dict:
        return {
            "early_side": self.early_side,
            "f3_ok": self.f3_ok,
            "inverted": self.inverted,
            "inversion_side": self.inversion_side,
            "inversion_bid": round(self.inversion_bid, 4),
            "inversion_strong": self.inversion_strong,
        }


# ---------------------------------------------------------------------------
# Construção do estado JSON
# ---------------------------------------------------------------------------
def _build_state(
    slug: Optional[str],
    secs: Optional[int],
    up_bid: float,
    down_bid: float,
    ee: _EarlyLeaderTrackerEE,
    ar: _EarlyLeaderTrackerAR,
) -> dict:
    ee_d = ee.state_dict()
    ar_d = ar.state_dict()

    el_side = ee_d["early_side"]
    el_bid  = (up_bid if el_side == "UP" else down_bid) if el_side else 0.0
    opp_bid = (down_bid if el_side == "UP" else up_bid) if el_side else 0.0
    n_s180  = ee_d["n_s180"]

    in_secs  = secs is not None and EE_MIN_ENTRY_SECS <= secs <= EE_MAX_ENTRY_SECS
    in_bid   = EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI if el_side else False
    n180_ok  = n_s180 >= EE_N180_MIN
    signal_ok = ee_d["signal_ok"] and in_secs and in_bid and n180_ok

    # AR: EL estabelecido, bid acima da janela EE, mercado quase resolvido
    ar_signal = (
        el_side is not None
        and secs is not None
        and EE_MIN_ENTRY_SECS <= secs <= AR_SECS_MAX
        and el_bid >= AR_BID_MIN
        and opp_bid < 0.30
        and not ar_d.get("inversion_strong")
    )

    criteria = [
        {
            "label": "Early Leader",
            "value": f"{el_side} (bid={ee_d['el_bid_240']:.3f})" if el_side else "nao detectado",
            "accepted": f">= {EE_EL_MIN}",
            "ok": bool(el_side),
            "detail": f"{ee_d['n_s240']} amostras (janela 181-240s)",
        },
        {
            "label": "F3 continuidade",
            "value": "sim" if ee_d.get("f3_ok") else ("nao" if ee_d.get("f3_ok") is False else "aguardando"),
            "accepted": f"min bid >= {EE_CONT_MIN}",
            "ok": bool(ee_d.get("f3_ok")),
            "detail": f"{n_s180} amostras (janela 121-180s)",
        },
        {
            "label": "Velocidade EL",
            "value": f"+{ee_d['el_vel']:.3f}" if ee_d["el_vel"] >= 0 else f"{ee_d['el_vel']:.3f}",
            "accepted": f">= {EE_VEL_MIN}",
            "ok": bool(el_side) and ee_d["el_vel"] >= EE_VEL_MIN,
            "detail": f"bid_180={ee_d['el_bid_180']:.3f} - bid_240={ee_d['el_bid_240']:.3f}",
        },
        {
            "label": "Amostras 180s",
            "value": str(n_s180),
            "accepted": f">= {EE_N180_MIN}",
            "ok": n180_ok,
            "detail": "gate runner real",
        },
        {
            "label": "Bid na janela",
            "value": f"{el_bid:.3f}" if el_bid > 0 else "-",
            "accepted": f"[{EE_ENTRY_LO}, {EE_ENTRY_HI}]",
            "ok": in_bid,
            "detail": None,
        },
        {
            "label": "Secs na janela",
            "value": f"{secs}s" if secs is not None else "-",
            "accepted": f"[{EE_MIN_ENTRY_SECS}, {EE_MAX_ENTRY_SECS}]s",
            "ok": in_secs,
            "detail": None,
        },
    ]

    # ── Instrução principal ───────────────────────────────────────────────
    vel_pct = int(round(ee_d["el_vel"] * 100)) if ee_d["el_vel"] else 0
    if signal_ok:
        price_c = int(round(el_bid * 100))
        action  = f"EE vel {vel_pct}. comprar {el_side.lower()} a {price_c},00"
        color   = "green"
        detail  = f"n_s180={n_s180} | secs={secs}s | gates: vel>={EE_VEL_MIN}, n>={EE_N180_MIN}"
    elif ar_signal:
        price_c = int(round(el_bid * 100))
        action  = f"AR standard: comprar {el_side.lower()} a {price_c},00"
        color   = "green"
        detail  = f"secs={secs}s | bid={el_bid:.3f} | opp={opp_bid:.3f}"
    elif ar_d.get("inverted") and ar_d.get("inversion_strong"):
        action = "INVERSAO FORTE"
        color  = "orange"
        detail = f"Novo lider: {ar_d.get('inversion_side')} bid={ar_d.get('inversion_bid', 0):.3f} (100% hist.)"
    elif ar_d.get("inverted"):
        action = "INVERTIDO"
        color  = "yellow"
        detail = f"{ar_d.get('inversion_side')} subindo bid={ar_d.get('inversion_bid', 0):.3f}"
    elif not el_side:
        action = "AGUARDAR"
        color  = "gray"
        detail = "EL nao detectado — janela 181-240s"
    elif not ee_d.get("f3_ok"):
        action = "AGUARDAR"
        color  = "yellow"
        detail = f"EL {el_side} formando — aguardando F3 (janela 121-180s)"
    elif ee_d["el_vel"] < EE_VEL_MIN:
        action = "AGUARDAR"
        color  = "yellow"
        detail = f"EL+F3 ok — vel {vel_pct} (precisa {int(EE_VEL_MIN*100)}) aguardando aceleracao"
    elif not n180_ok:
        action = "AGUARDAR"
        color  = "yellow"
        detail = f"EL+F3+vel ok — n_s180={n_s180} < {EE_N180_MIN} aguardando amostras"
    elif not in_bid and el_side:
        action = "AGUARDAR"
        color  = "yellow"
        if el_bid < EE_ENTRY_LO:
            detail = f"EE pronto — aguardando bid {el_bid:.3f} -> [{EE_ENTRY_LO},{EE_ENTRY_HI}]"
        else:
            detail = f"bid {el_bid:.3f} acima de {EE_ENTRY_HI} — aguardar AR?"
    elif not in_secs:
        action = "AGUARDAR"
        color  = "yellow"
        if secs is not None and secs > EE_MAX_ENTRY_SECS:
            detail = f"EE pronto — aguardando secs <= {EE_MAX_ENTRY_SECS} (agora: {secs}s)"
        else:
            detail = f"secs={secs}s fora [{EE_MIN_ENTRY_SECS}s, {EE_MAX_ENTRY_SECS}s]"
    else:
        action = "AGUARDAR"
        color  = "gray"
        detail = "aguardando sinal"

    return {
        "status": "ok",
        "ts": time.time(),
        "slug": slug,
        "secs_to_end": secs,
        "bids": {"up": round(up_bid, 4), "down": round(down_bid, 4)},
        "ee": ee_d,
        "el": ar_d,
        "signal_ok": signal_ok,
        "ar_signal": ar_signal,
        "action": action,
        "color": color,
        "detail": detail,
        "criteria": criteria,
        # Compatibilidade com formato da extensão manual_assist
        "title": f"EL Monitor | {(slug or '')[-22:]}",
        "setup_side": el_side,
        "entry_price": round(el_bid, 3) if el_side and in_bid else None,
        "entry_criteria": criteria,
        "suggested_detail": detail,
        "status_note": f"secs={secs} | UP={up_bid:.3f} DN={down_bid:.3f}",
        "active_action": action,
        "active_side": el_side,
        "active_price": round(el_bid, 3) if in_bid else None,
        "opp_bid": round(opp_bid, 4),
        "el_bid": round(el_bid, 4),
        "runner": _read_runner_state(),
    }


# ---------------------------------------------------------------------------
# Três threads independentes — cada uma com responsabilidade única:
#   _bundle_refresh_loop : atualiza slug/token_ids a cada 15s
#   _bid_fetch_loop      : busca bids continuamente, sem cadência fixa
#   _poll_loop           : lê tudo da memória, atualiza trackers EE/AR, serve estado
#
# O handler HTTP só lê _state — nenhum I/O no caminho da resposta.
# ---------------------------------------------------------------------------

_bundle_lock = threading.Lock()
_bnd: dict = {}
_bnd_up_id: str = ""
_bnd_dn_id: str = ""
_bnd_ts: float = 0.0


def _bundle_refresh_loop(interval_secs: float) -> None:
    global _bnd, _bnd_up_id, _bnd_dn_id, _bnd_ts
    while True:
        t0 = time.time()
        try:
            new_bnd = _build_slot_bundle()
            up_id, dn_id = _token_ids_from_bundle(new_bnd)
            with _bundle_lock:
                _bnd = new_bnd
                _bnd_up_id = up_id
                _bnd_dn_id = dn_id
                _bnd_ts = t0
        except Exception:
            pass
        elapsed = time.time() - t0
        remaining = interval_secs - elapsed
        if remaining > 0:
            time.sleep(remaining)


_bids_lock = threading.Lock()
_live_up_bid: float = 0.0
_live_dn_bid: float = 0.0
_live_bids_ts: float = 0.0


def _bid_fetch_loop() -> None:
    """Busca bids em loop contínuo; atualiza _live_*bid sem bloquear nada."""
    global _live_up_bid, _live_dn_bid, _live_bids_ts
    while True:
        try:
            with _bundle_lock:
                up_id = _bnd_up_id
                dn_id = _bnd_dn_id
            if up_id and dn_id:
                up, dn = _fast_fetch_bids(up_id, dn_id)
                with _bids_lock:
                    _live_up_bid = up
                    _live_dn_bid = dn
                    _live_bids_ts = time.time()
            else:
                time.sleep(0.5)
        except Exception:
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# Estado compartilhado
# ---------------------------------------------------------------------------
_state: Dict[str, Any] = {"status": "starting", "ts": time.time()}
_state_lock = threading.Lock()


def _poll_loop(poll_secs: float) -> None:
    """Lê bids e bundle da memória — zero I/O; atualiza trackers e estado."""
    ee_tracker = _EarlyLeaderTrackerEE()
    ar_tracker = _EarlyLeaderTrackerAR()

    while True:
        t0 = time.time()
        try:
            with _bundle_lock:
                bundle = _bnd
                bundle_ts = _bnd_ts

            with _bids_lock:
                up_bid = _live_up_bid
                down_bid = _live_dn_bid

            if not bundle:
                time.sleep(poll_secs)
                continue

            current_item = bundle.get("queue", {}).get("current")
            if not current_item:
                with _state_lock:
                    _state.update({"status": "no_market", "ts": t0, "slug": None, "secs_to_end": None})
                time.sleep(poll_secs)
                continue

            slug = str(current_item.get("slug") or "")
            secs_raw = current_item.get("seconds_to_end")
            if secs_raw is not None:
                secs = max(0, int(secs_raw) - int(t0 - bundle_ts))
            else:
                secs = None

            if slug and secs is not None and up_bid > 0 and down_bid > 0:
                ee_tracker.update(slug, secs, up_bid, down_bid)
                ar_tracker.update(slug, secs, up_bid, down_bid)

            new_state = _build_state(slug, secs, up_bid, down_bid, ee_tracker, ar_tracker)
            with _state_lock:
                _state.clear()
                _state.update(new_state)

        except Exception as exc:
            with _state_lock:
                _state.update({"status": "error", "ts": t0, "error": str(exc), "slug": _state.get("slug")})

        elapsed = time.time() - t0
        remaining = poll_secs - elapsed
        if remaining > 0:
            time.sleep(remaining)


# ---------------------------------------------------------------------------
# Servidor HTTP
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/state", ""):
            with _state_lock:
                body = json.dumps(_state, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()


def run_el_monitor_server(port: int = 8766, poll_secs: float = 0.5) -> None:
    # 1) Bundle refresh: slug/token_ids a cada 15s
    bt = threading.Thread(target=_bundle_refresh_loop, args=(15.0,), daemon=True)
    bt.start()
    # Aguarda primeiro bundle (max 15s) antes de iniciar as demais threads
    print("[EL Monitor] Aguardando bundle inicial...")
    deadline = time.time() + 15.0
    while time.time() < deadline:
        with _bundle_lock:
            if _bnd:
                break
        time.sleep(0.2)

    # 2) Bid fetch: loop contínuo, 2 GETs paralelos, sem cadência fixa
    bft = threading.Thread(target=_bid_fetch_loop, daemon=True)
    bft.start()
    time.sleep(0.5)  # Aguarda primeiro bid antes de iniciar poll loop

    # 3) Poll loop: só lê memória, zero I/O
    t = threading.Thread(target=_poll_loop, args=(poll_secs,), daemon=True)
    t.start()

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"[EL Monitor] Servidor em http://127.0.0.1:{port}/state | poll={poll_secs}s")
    print("[EL Monitor] Ctrl+C para parar")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[EL Monitor] Parado.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Servidor EL/EE Monitor")
    parser.add_argument("--port", type=int, default=8766, help="Porta HTTP (default: 8766)")
    parser.add_argument("--poll", type=float, default=0.5, help="Intervalo de poll em segundos (default: 0.5)")
    args = parser.parse_args()
    run_el_monitor_server(port=args.port, poll_secs=args.poll)
