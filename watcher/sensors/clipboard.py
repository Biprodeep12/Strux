"""Clipboard sensor. Polls GetClipboardSequenceNumber (a single integer read,
no clipboard open) and only touches the clipboard when the number changes.

Stores a short preview + length + a content hash, not the full payload, so
secrets you copy are not sitting in the DB in full.
"""
from __future__ import annotations

import hashlib
import logging
import time

from ..events import Event
from . import winapi
from .base import Sensor

log = logging.getLogger(__name__)


class ClipboardSensor(Sensor):
    name = "clipboard"

    def run(self) -> None:
        poll = float(self.cfg.get("poll_seconds", 2))
        preview_chars = int(self.cfg.get("preview_chars", 200))
        last_seq = winapi.clipboard_sequence()
        while not self.wait(poll):
            seq = winapi.clipboard_sequence()
            if seq == last_seq:
                continue
            last_seq = seq
            text = None
            for _ in range(3):  # clipboard can be briefly locked by the copying app
                try:
                    text = winapi.clipboard_text()
                    break
                except Exception:
                    time.sleep(0.05)
            _, app, title = winapi.foreground_info()
            if text is None:
                self.emit(Event(kind="clipboard", app=app, title=title, data={"kind": "non-text"}))
                continue
            digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]
            self.emit(Event(
                kind="clipboard", app=app, title=title,
                data={"kind": "text", "len": len(text), "hash": digest,
                      "preview": text[:preview_chars].replace("\r", "")},
            ))
