from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from market.manual_overlay_v1 import ManualOverlayEngineV1


class _StateHandler(BaseHTTPRequestHandler):
    engine = ManualOverlayEngineV1()
    default_qty = 6
    refresh_secs = 0.5
    cache_lock = threading.Lock()
    cache_payload: dict = {
        "ok": False,
        "status_note": "warming_up",
        "default_qty": 6,
    }

    @classmethod
    def _live_payload(cls) -> dict:
        with cls.cache_lock:
            payload = dict(cls.cache_payload)
        now = time.time()
        last_ts = float(payload.get("last_update_ts") or now)
        elapsed = max(0.0, now - last_ts)
        payload["snapshot_age_ms"] = round(elapsed * 1000.0, 1)
        payload["server_now_ts"] = now
        payload["refresh_secs"] = cls.refresh_secs

        secs_to_end = payload.get("secs_to_end")
        if secs_to_end is not None:
            payload["secs_to_end"] = max(0, int(round(float(secs_to_end) - elapsed)))

        for key in ("watch_window_eta_secs", "reaction_deadline_secs"):
            value = payload.get(key)
            if value is not None:
                payload[key] = round(max(0.0, float(value) - elapsed), 3)

        return payload

    def _write_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.rstrip("/") not in ("", "/state"):
            self._write_json(404, {"ok": False, "error": "not_found"})
            return
        payload = self._live_payload()
        self._write_json(200, payload)

    def log_message(self, format: str, *args) -> None:
        return


def _refresh_loop(interval_secs: float) -> None:
    interval_secs = max(0.25, float(interval_secs))
    while True:
        cycle_started = time.time()
        try:
            snap = _StateHandler.engine.read_snapshot()
            payload = snap.as_dict()
            payload["default_qty"] = _StateHandler.default_qty
        except Exception as exc:
            payload = {
                "ok": False,
                "status_note": str(exc),
                "default_qty": _StateHandler.default_qty,
            }
        with _StateHandler.cache_lock:
            _StateHandler.cache_payload = payload
        time.sleep(max(0.0, interval_secs - max(0.0, time.time() - cycle_started)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve manual overlay state over localhost for a browser userscript")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument("--qty", type=int, default=6, help="Default quantity for autofill")
    parser.add_argument("--refresh-secs", type=float, default=0.5, help="Background refresh interval")
    args = parser.parse_args()

    _StateHandler.default_qty = max(1, int(args.qty))
    _StateHandler.refresh_secs = max(0.25, float(args.refresh_secs))
    _StateHandler.cache_payload["default_qty"] = _StateHandler.default_qty
    refresher = threading.Thread(target=_refresh_loop, args=(_StateHandler.refresh_secs,), daemon=True)
    refresher.start()
    server = ThreadingHTTPServer((args.host, int(args.port)), _StateHandler)
    print(
        f"[MANUAL_SIGNAL_SERVER] http://{args.host}:{args.port}/state "
        f"qty={_StateHandler.default_qty} refresh={_StateHandler.refresh_secs:.2f}s"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
