"""Event / Suggestion types and the in-process event bus.

The bus is deliberately tiny: sensors publish, subscribers get a synchronous
callback. Subscribers MUST be fast (enqueue and return). Heavy work (SQLite,
OCR, LLM) happens on the subscriber's own thread.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Event:
    kind: str                       # window | title | idle_start | idle_end | clipboard | frame | blocked
    ts: float = field(default_factory=time.time)
    app: str = ""                   # process name, e.g. "code.exe"
    title: str = ""                 # window title (may be empty)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Suggestion:
    text: str
    source: str                     # "rule:<name>" or "llm"
    ts: float = field(default_factory=time.time)
    context: dict[str, Any] = field(default_factory=dict)
    id: int | None = None


class Bus:
    """Synchronous pub/sub with per-kind and wildcard subscribers."""

    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[Event], None]]] = {}
        self._lock = threading.Lock()
        self.dropped = 0

    def subscribe(self, kind: str, fn: Callable[[Event], None]) -> None:
        with self._lock:
            self._subs.setdefault(kind, []).append(fn)

    def publish(self, ev: Event) -> None:
        with self._lock:
            targets = list(self._subs.get(ev.kind, [])) + list(self._subs.get("*", []))
        for fn in targets:
            try:
                fn(ev)
            except Exception:  # never let one subscriber kill a sensor thread
                log.exception("subscriber %s failed on %s", fn, ev.kind)
