"""Idle sensor: one cheap GetLastInputInfo call every few seconds.

Emits idle_start when input has been absent for idle_after_seconds and
idle_end when input resumes (data.idle_seconds = how long you were away).

Accuracy:
- locking the PC (Win+L) counts as away at once, not after idle_after_seconds;
  idle_start is back-dated to your last input either way
- a fullscreen window (video, slideshow, game) without input is watching, not idle,
  for up to watch_max_minutes
"""
from __future__ import annotations

import logging
import time

from ..events import Event
from . import winapi
from .base import Sensor

log = logging.getLogger(__name__)


class IdleSensor(Sensor):
    name = "idle"

    def __init__(self, bus, cfg) -> None:
        super().__init__(bus, cfg)
        self.is_idle = False
        self._idle_since = 0.0

    def run(self) -> None:
        poll = float(self.cfg.get("poll_seconds", 5))
        threshold = float(self.cfg.get("idle_after_seconds", 120))
        watch_max = float(self.cfg.get("watch_max_minutes", 180)) * 60
        while not self.wait(poll):
            idle = winapi.idle_seconds()
            now = time.time()
            locked = winapi.session_locked()
            away = locked or (idle >= threshold and not (idle < watch_max and winapi.foreground_fullscreen()))
            if not self.is_idle and away:
                self.is_idle = True
                self._idle_since = now - idle
                self.emit(Event(kind="idle_start", ts=self._idle_since))
            elif self.is_idle and not locked and idle < threshold:
                self.is_idle = False
                self.emit(Event(kind="idle_end", ts=now, data={"idle_seconds": round(now - self._idle_since)}))
