"""Sensor base class. A sensor owns one daemon thread and publishes Events."""
from __future__ import annotations

import logging
import threading

from ..events import Bus, Event

log = logging.getLogger(__name__)


class Sensor:
    name = "sensor"

    def __init__(self, bus: Bus, cfg: dict) -> None:
        self.bus = bus
        self.cfg = cfg
        self.paused = threading.Event()   # set => paused
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # subclasses implement run(); they should check self.should_stop() often
    def run(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def emit(self, ev: Event) -> None:
        if not self.paused.is_set():
            self.bus.publish(ev)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._guarded_run, name=f"sensor-{self.name}", daemon=True)
        self._thread.start()

    def _guarded_run(self) -> None:
        """A crashing sensor restarts (10 s, 20 s, ... up to 5 min) instead of silently going dark."""
        delay = 10.0
        while not self._stop.is_set():
            try:
                self.run()
                return
            except Exception:
                log.exception("sensor %s crashed; restarting in %.0f s", self.name, delay)
            if self._stop.wait(delay):
                return
            delay = min(300.0, delay * 2)

    def stop(self) -> None:
        self._stop.set()
        self.on_stop()
        if self._thread:
            self._thread.join(timeout=3)

    def on_stop(self) -> None:
        """Hook for sensors that block in a native loop (e.g. WinEvent message pump)."""

    def wait(self, seconds: float) -> bool:
        """Sleep that wakes early on stop. Returns True if stopping."""
        return self._stop.wait(seconds)
