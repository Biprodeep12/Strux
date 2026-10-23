"""System tray icon (pystray), run detached so the WebView2 window can own the
main thread. Menu: status, open dashboard, pause/resume, think now, start Ollama,
forget last 10 min, open data folder, quit.
"""
from __future__ import annotations

import logging
import os
import threading

from PIL import Image, ImageColor

from .. import APP_NAME

log = logging.getLogger(__name__)


def _icon_image(color: str | None) -> Image.Image:
    """The Strux mark (watcher/ui/brand.py); grey while paused."""
    from .brand import mark
    return mark(64, color=None if color is None else ImageColor.getrgb(color))


class Tray:
    ACTIVE, PAUSED = None, "#6b6b6b"

    def __init__(self, app) -> None:
        self.app = app
        self.icon = None
        self.quit_requested = threading.Event()

    def _status(self, item=None) -> str:
        st = self.app.status()
        return f"{'Paused' if st['paused'] else 'Watching'} | frames {st['frames']} | suggestions {st['suggestions']}"

    def refresh(self) -> None:
        if self.icon:
            self.icon.icon = _icon_image(self.PAUSED if self.app.paused else self.ACTIVE)
            self.icon.title = f"{APP_NAME} (paused)" if self.app.paused else APP_NAME
            self.icon.update_menu()

    def _toggle_pause(self, icon, item) -> None:
        self.app.set_paused(not self.app.paused)

    def _forget(self, icon, item) -> None:
        n = self.app.forget_minutes(10)
        from . import toast
        toast.info("Forgot last 10 min", f"{n['events']} events, {n['frames']} frames, {n['files']} files removed")

    def _think(self, icon, item) -> None:
        self.app.brain.think_now()

    def _start_ollama(self, icon, item) -> None:
        def run():
            from ..brain.llm import start_ollama
            from . import toast
            r = start_ollama(self.app.brain.llm)
            if r["ok"]:
                toast.info("Ollama", r.get("note") or f"Up in {r['seconds']} s" + (" (restarted a stuck one)" if r.get("restarted") else ""))
            else:
                toast.info("Ollama didn't start", r["error"])
        threading.Thread(target=run, name="ollama-start", daemon=True).start()

    def _open_dashboard(self, icon, item) -> None:
        self.app.show_window()

    def _open_data(self, icon, item) -> None:
        os.startfile(str(self.app.store.data_dir))

    def _quit(self, icon, item) -> None:
        self.quit_requested.set()
        if self.app.windows:
            self.app.windows.quit()
        icon.stop()

    def _build(self):
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem(self._status, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open dashboard", self._open_dashboard, default=True),
            pystray.MenuItem(lambda i: "Resume" if self.app.paused else "Pause", self._toggle_pause),
            pystray.MenuItem("Think now", self._think),
            pystray.MenuItem("Start Ollama", self._start_ollama),
            pystray.MenuItem("Forget last 10 min", self._forget),
            pystray.MenuItem("Open data folder", self._open_data),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self.icon = pystray.Icon("strux", _icon_image(self.ACTIVE), APP_NAME, menu)
        return self.icon

    def run(self) -> None:
        self._build().run()

    def run_detached(self) -> None:
        self._build().run_detached()

    def stop(self) -> None:
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
