"""Browser URL sensor. No extension needed: reads the address bar of Chromium
browsers (Chrome, Brave, Edge) through UI Automation, only when the foreground
window/title changes and the app is a browser. One UIA query is ~10-50 ms and
happens on its own thread, debounced to at most once per second.

Emits: url  (data = {url, domain})
"""
from __future__ import annotations

import logging
import queue
import time
from urllib.parse import urlparse

from ..events import Event
from .base import Sensor

log = logging.getLogger(__name__)

BROWSERS = {"chrome.exe", "brave.exe", "msedge.exe", "chromium.exe", "vivaldi.exe", "opera.exe"}
ADDRESS_NAMES = ("Address and search bar", "Address bar", "Search or enter web address")


class BrowserSensor(Sensor):
    name = "browser"

    def __init__(self, bus, cfg) -> None:
        super().__init__(bus, cfg)
        self._q: queue.Queue[tuple[int, str, str]] = queue.Queue(maxsize=50)
        self._last_url = ""
        self._last_query = 0.0
        bus.subscribe("window", self._on_win)
        bus.subscribe("title", self._on_win)

    def _on_win(self, ev: Event) -> None:
        if ev.app in BROWSERS:
            try:
                self._q.put_nowait((int(ev.data.get("hwnd", 0)), ev.app, ev.title))
            except queue.Full:
                pass

    def _read_url(self, hwnd: int) -> str:
        import uiautomation as auto
        try:
            win = auto.ControlFromHandle(hwnd)
            if not win:
                return ""
            for name in ADDRESS_NAMES:
                edit = win.EditControl(Name=name, searchDepth=12)
                if edit.Exists(0.2, 0.05):
                    val = edit.GetValuePattern().Value or ""
                    return val.strip()
        except Exception as e:
            log.debug("uia read failed: %s", e)
        return ""

    def run(self) -> None:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            while not self.should_stop():
                try:
                    hwnd, app, title = self._q.get(timeout=1.0)
                except queue.Empty:
                    continue
                if self.paused.is_set():
                    continue
                # debounce: wait for the newest request, at most 1/s
                time.sleep(0.6)
                while not self._q.empty():
                    hwnd, app, title = self._q.get_nowait()
                url = self._read_url(hwnd)
                if not url or url == self._last_url:
                    continue
                if "://" not in url:
                    url = "https://" + url
                self._last_url = url
                domain = urlparse(url).netloc.lower().removeprefix("www.")
                self.emit(Event(kind="url", app=app, title=title, data={"url": url[:500], "domain": domain}))
