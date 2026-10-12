"""A one-line live status for the terminal: what I'm doing and for how long.

    ⠋ waiting for the model · 12s
    ⠙ reading my memory · 63% · 41s
    ⠹ thinking · ~1.2k tokens · 58s

It animates only on a real terminal; when output is piped it stays silent.
"""
from __future__ import annotations

import sys
import threading
import time

DIM, RESET = "\033[2m", "\033[0m"
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Status:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.tty = self.stream.isatty()
        self.lock = threading.Lock()
        self.label, self.detail, self.started = "", "", 0.0
        self.active = False
        self._thread: threading.Thread | None = None

    def write(self, text: str) -> None:
        """Print normal output, clearing the status line first."""
        with self.lock:
            if self.active and self.tty:
                self.stream.write("\r\033[K")
            self.stream.write(text)
            self.stream.flush()

    def show(self, label: str, detail: str = "") -> None:
        with self.lock:
            if not self.active:
                self.started = time.monotonic()
            self.label, self.detail, self.active = label, detail, True
        if self.tty and not (self._thread and self._thread.is_alive()):
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()

    def update(self, detail: str) -> None:
        self.detail = detail

    def clear(self) -> None:
        with self.lock:
            if self.active and self.tty:
                self.stream.write("\r\033[K")
                self.stream.flush()
            self.active = False

    def _spin(self) -> None:
        i = 0
        while True:
            with self.lock:
                if not self.active:
                    return
                secs = int(time.monotonic() - self.started)
                parts = [self.label, self.detail, f"{secs}s"]
                line = " · ".join(p for p in parts if p)
                self.stream.write(f"\r\033[K{DIM}{FRAMES[i % len(FRAMES)]} {line}{RESET}")
                self.stream.flush()
            i += 1
            time.sleep(0.12)
