from __future__ import annotations

import argparse
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

from market.manual_overlay_v1 import ManualOverlayEngineV1, ManualOverlaySnapshotV1


BG = "#101418"
PANEL = "#1b2229"
TEXT = "#ecf1f7"
MUTED = "#98a7b8"
SAFE = "#1f9d55"
CAUTION = "#d69e2e"
UNSAFE = "#d64545"
BLOCKED = "#6b7280"


def _fmt(value, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}{suffix}"


def _status_color(label: str) -> str:
    if label == "SAFE":
        return SAFE
    if label == "CAUTION":
        return CAUTION
    if label == "UNSAFE":
        return UNSAFE
    return BLOCKED


def _mode_label(snap: ManualOverlaySnapshotV1) -> str:
    if snap.one_shot_ready and snap.setup_side and snap.entry_price is not None:
        return "PRONTO"
    if snap.setup_reason == "invalid_book_both_sides_rich" or snap.safety_label == "UNSAFE":
        return "DESCARTAR"
    return "AGUARDE"


class OverlayApp:
    def __init__(self, poll_ms: int, alpha: float) -> None:
        self.engine = ManualOverlayEngineV1()
        self.poll_ms = poll_ms
        self.snapshot_lock = threading.Lock()
        self.latest_snapshot: ManualOverlaySnapshotV1 | None = None
        self.stop_event = threading.Event()
        self.root = tk.Tk()
        self.root.title("Overlay Manual Polymarket V1")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", alpha)
        self.root.geometry("+30+30")
        self.root.resizable(False, False)

        title_font = tkfont.Font(family="Consolas", size=14, weight="bold")
        body_font = tkfont.Font(family="Consolas", size=10)
        small_font = tkfont.Font(family="Consolas", size=9)

        panel = tk.Frame(self.root, bg=PANEL, padx=12, pady=10, bd=0, highlightthickness=0)
        panel.pack(fill="both", expand=True)

        self.header = tk.Label(panel, text="Inicializando...", bg=PANEL, fg=TEXT, font=title_font, anchor="w", justify="left")
        self.header.pack(fill="x")

        self.badge = tk.Label(panel, text="AGUARDE", bg=BLOCKED, fg=TEXT, font=title_font, padx=10, pady=4)
        self.badge.pack(fill="x", pady=(8, 8))

        self.score = tk.Label(panel, text="", bg=PANEL, fg=TEXT, font=title_font, anchor="w", justify="left")
        self.score.pack(fill="x")

        self.fields = tk.Label(panel, text="", bg=PANEL, fg=MUTED, font=body_font, anchor="w", justify="left")
        self.fields.pack(fill="x", pady=(8, 0))

        self.note = tk.Label(panel, text="", bg=PANEL, fg=MUTED, font=small_font, anchor="w", justify="left")
        self.note.pack(fill="x", pady=(8, 0))

        self.footer = tk.Label(panel, text="", bg=PANEL, fg=MUTED, font=small_font, anchor="w", justify="left")
        self.footer.pack(fill="x", pady=(6, 0))

        self._drag_x = 0
        self._drag_y = 0
        for widget in (panel, self.header, self.badge, self.score, self.fields, self.note, self.footer):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)

        self.root.bind("<Escape>", lambda _event: self.root.destroy())
        self.root.bind("r", lambda _event: self.refresh())
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()
        self.root.after(100, self.refresh)

    def _start_drag(self, event) -> None:
        self._drag_x = event.x
        self._drag_y = event.y

    def _drag(self, event) -> None:
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            snap = self.engine.read_snapshot()
            with self.snapshot_lock:
                self.latest_snapshot = snap
            self.stop_event.wait(max(0.5, self.poll_ms / 1000.0))

    def _close(self) -> None:
        self.stop_event.set()
        self.root.destroy()

    def _render(self, snap: ManualOverlaySnapshotV1) -> None:
        updated = time.strftime("%H:%M:%S", time.localtime(snap.last_update_ts))
        mode = _mode_label(snap)
        badge_text = mode
        age = max(0.0, time.time() - snap.last_update_ts)
        live_secs = None if snap.secs_to_end is None else max(0, int(round(float(snap.secs_to_end) - age)))
        live_watch = None if snap.watch_window_eta_secs is None else max(0.0, float(snap.watch_window_eta_secs) - age)
        live_reaction = None if snap.reaction_deadline_secs is None else max(0.0, float(snap.reaction_deadline_secs) - age)
        if mode == "PRONTO" and snap.setup_side and snap.entry_price is not None:
            headline = f"{mode} {snap.setup_side} {_fmt(snap.entry_price, 2)} x 6"
        elif mode == "AGUARDE" and live_watch is not None and live_watch > 0:
            headline = f"{mode} {_fmt(live_watch, 0, 's')}"
        else:
            headline = mode

        self.header.configure(
            text=f"{snap.title}\nslug={snap.slug or '-'} | fim em {live_secs if live_secs is not None else '-'}s"
        )
        self.badge.configure(text=badge_text, bg=_status_color("SAFE" if mode == "PRONTO" else "UNSAFE" if mode == "DESCARTAR" else "BLOCKED"))
        self.score.configure(
            text=(
                f"{headline} | reação={_fmt(live_reaction, 0, 's')}"
            ),
            fg=SAFE if mode == "PRONTO" else (UNSAFE if mode == "DESCARTAR" else TEXT),
        )
        field_lines = [
            f"Tendência: {snap.trend_label or '-'}",
            f"Reversão: {snap.reversal_risk or '-'}",
            f"Price to Beat: {_fmt(snap.price_to_beat_usd, 2, 'usd')} ({_fmt(snap.price_to_beat_bps, 2, 'bps')})",
            f"Buffer: {_fmt(snap.buffer_bps, 2, 'bps')}",
            f"Entrada: {(snap.setup_side or '-')} {_fmt(snap.entry_price, 2)} x 6",
        ]
        self.fields.configure(text="\n".join(field_lines))
        self.note.configure(text=f"Nota: {snap.status_note}")
        self.footer.configure(text=f"atualizado={updated} | Esc fecha | arraste para mover | r atualiza")

    def refresh(self) -> None:
        with self.snapshot_lock:
            snap = self.latest_snapshot
        if snap is None:
            self.root.after(100, self.refresh)
            return
        self._render(snap)
        self.root.after(250, self.refresh)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Desktop overlay for manual Polymarket current/almost-resolved decisions")
    parser.add_argument("--poll-ms", type=int, default=1000, help="Refresh interval in milliseconds")
    parser.add_argument("--alpha", type=float, default=0.92, help="Window opacity")
    args = parser.parse_args()

    app = OverlayApp(poll_ms=max(500, int(args.poll_ms)), alpha=min(1.0, max(0.3, float(args.alpha))))
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
