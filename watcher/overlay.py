"""Bar controller: feeds the native Strux bar and manages the drop-down pane.

  NativeBar (watcher/ui/nativebar.py)  - the always-on-top strip: status, headline, count,
                                          hover actions, suggestions / alerts popping out
  pane (WebView2, route #/pane)        - chat + activity, drops down under the bar when
                                          you click the keyboard / list icons or the headline

State comes from the app: open suggestions (store), alerts, Claude sessions waiting for
you, the foreground app, brain processing. Everything here is thread-safe; the bar
redraws on its own thread.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .events import Event

log = logging.getLogger(__name__)

NAMES = {
    "excel.exe": "Excel", "winword.exe": "Word", "powerpnt.exe": "PowerPoint", "outlook.exe": "Outlook", "olk.exe": "Outlook",
    "acrobat.exe": "Acrobat", "acrord32.exe": "Acrobat", "notepad.exe": "Notepad", "firefox.exe": "Firefox",
    "code.exe": "VS Code", "brave.exe": "Brave", "chrome.exe": "Chrome", "msedge.exe": "Edge", "windowsterminal.exe": "Terminal",
    "explorer.exe": "Explorer", "slack.exe": "Slack", "discord.exe": "Discord", "notion.exe": "Notion", "spotify.exe": "Spotify",
    "figma.exe": "Figma", "postman.exe": "Postman", "powershell.exe": "PowerShell", "cmd.exe": "CMD", "whatsapp.exe": "WhatsApp",
    "claude.exe": "Claude", "obsidian.exe": "Obsidian", "telegram.exe": "Telegram",
}


ACTION_LABELS = {"explain_error": "Explain", "recap": "Recap", "focus": "Start focus", "break": "Take break", "discuss": "Ask",
                 "add_todo": "Add TODO", "complete_todo": "Mark done", "plan": "Plan day", "prep_event": "Prep me", "digest": "Wrap up"}


def action_label(context) -> str:
    """Accept-button label for a suggestion's action ("" -> plain check icon)."""
    import json
    if isinstance(context, str):
        try:
            context = json.loads(context or "{}")
        except ValueError:
            context = {}
    return ACTION_LABELS.get(((context or {}).get("action") or {}).get("type", ""), "")


def app_label(app: str) -> str:
    if not app:
        return "Strux"
    return NAMES.get(app.lower(), app.rsplit(".", 1)[0].capitalize())


class Overlay:
    def __init__(self, app, windows, cfg: dict, broadcast: Callable[[dict], None]) -> None:
        from .ui.nativebar import Item, NativeBar
        self._Item = Item
        self.app = app
        self.windows = windows
        self.cfg = cfg
        self.broadcast = broadcast
        self.enabled = cfg.get("enabled", True)
        self.always = True
        self.timeout = float(cfg.get("timeout_seconds", 14))
        self.processing = "idle"
        self.alerts: list[dict[str, Any]] = []
        self._alert_seen: dict[str, float] = {}
        self.suggestions: list[dict[str, Any]] = []
        self.chat_log: list[dict[str, Any]] = []      # results of accepted suggestions, for the pane
        self.attach_log: list[dict[str, Any]] = []    # files dropped on the bar / Ctrl+Alt+V, waiting in the pane's tray
        self._chat_seq = 0
        self.pane: str | None = None
        self._pane_closed_at = 0.0
        self._pane_closed_tab: str | None = None
        self._pane_opened_at = 0.0
        self._bar_pressed_at = 0.0
        self.pane_h = 380              # logical px, reported by the page
        self._current = ("", "")
        self._current_hwnd = 0         # the window behind it, for "what's on my screen"
        self._lock = threading.RLock()
        self.bar = NativeBar(app.data_dir, self._on_bar, peek_seconds=self.timeout, on_press=self._on_bar_press)

    # ---- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if not self.enabled:
            return
        try:
            for r in self.app.store.suggestions_page(20):
                if not r["feedback"] and time.time() - r["ts"] < 6 * 3600:
                    self.suggestions.append({"id": r["id"], "ts": r["ts"], "source": r["source"], "text": r["text"],
                                             "action": action_label(r["context"])})
        except Exception:
            log.exception("loading open suggestions")
        self.app.bus.subscribe("window", self._on_window)
        self.app.bus.subscribe("title", self._on_window)
        self.app.bus.subscribe("claude", lambda ev: self._refresh())
        self.bar.start()
        self._refresh()
        threading.Thread(target=self._sync_loop, name="pane-sync", daemon=True).start()

    def _sync_loop(self) -> None:
        """If the pane window got hidden behind our back, stop showing it as open in the bar."""
        while True:
            time.sleep(1.0)
            try:
                if self.pane and time.time() - self._pane_opened_at > 1.5 and not self.windows.pane_visible():
                    log.info("pane hidden externally; resetting bar state")
                    self.pane = None
                    self.bar.update(pane=None)
            except Exception:
                log.exception("pane sync")

    @property
    def mode(self) -> str:
        return self.pane or "idle"

    @property
    def expanded(self) -> bool:
        return self.pane is not None

    # ---- inputs from the app -------------------------------------------------------
    def suggestion(self, s) -> None:
        with self._lock:
            self.suggestions = [x for x in self.suggestions if x["id"] != s.id]
            act = action_label(s.context)
            self.suggestions.insert(0, {"id": s.id, "ts": s.ts, "source": s.source, "text": s.text, "action": act})
            self.suggestions = self.suggestions[:5]
        self.broadcast({"type": "overlay", "item": "suggestion", "id": s.id, "ts": s.ts, "source": s.source, "text": s.text, "action": act})
        src = "model" if s.source == "llm" else s.source.replace("rule:", "").replace("_", " ")
        self._refresh(peek=self._Item("sugg", f"s{s.id}", s.text, meta=f"{src} · just now", id=s.id, action=act))

    def alert(self, key: str, text: str, level: str = "warn", cooldown: float = 3600) -> None:
        now = time.time()
        if now - self._alert_seen.get(key, 0) < cooldown:
            return
        self._alert_seen[key] = now
        item = {"type": "overlay", "item": "alert", "key": key, "ts": now, "level": level, "text": text}
        with self._lock:
            self.alerts = [a for a in self.alerts if a["key"] != key][-2:] + [item]
        self.broadcast(item)
        self._refresh(peek=self._Item("alert", key, text, level=level))

    def set_processing(self, stage: str, detail: str = "") -> None:
        self.processing = stage
        self.broadcast({"type": "overlay", "item": "processing", "stage": stage, "detail": detail, "ts": time.time()})
        label = {"thinking": "Thinking", "vision": "Looking", "listening": "Listening…", "hearing": "Writing it down…"}.get(stage)       # background OCR stays silent
        self.bar.update(stage=label)

    def feedback_done(self, sid: int) -> None:
        """A suggestion was answered (bar, pane or dashboard)."""
        with self._lock:
            self.suggestions = [x for x in self.suggestions if x["id"] != sid]
            peek = self.bar.state.peek
        if peek is not None and peek.id == sid:
            self.bar.update(peek=None)
        self._refresh()

    def post_chat(self, msg: dict) -> None:
        """An action result for the pane chat. Broadcast now, and kept (10 min) so a pane whose page
        was not running yet (it is created hidden) still gets it when it loads."""
        now = time.time()
        with self._lock:
            self._chat_seq += 1
            item = {**msg, "id": self._chat_seq, "ts": now}
            self.chat_log = [m for m in self.chat_log if now - m["ts"] < 600][-19:] + [item]
        self.broadcast({"type": "overlay", "item": "chat", **item})

    def voice_message(self, text: str) -> None:
        """'Hey Strux, …': open the bar chat and send what was said, as if typed. Kept 60 s for a pane page that was not running."""
        now = time.time()
        with self._lock:
            self._chat_seq += 1
            item = {"id": self._chat_seq, "ts": now, "text": text}
            self.voice_log = [v for v in getattr(self, "voice_log", []) if now - v["ts"] < 60][-3:] + [item]
        self.show_tab("chat")
        self.broadcast({"type": "overlay", "item": "voice", **item})

    def attach(self, attachments: list[dict], problems: list[str] | None = None) -> None:
        """Put attachments into the bar chat's tray and open the chat (dropped on the bar, Ctrl+Alt+V)."""
        now = time.time()
        if attachments:
            with self._lock:
                self._chat_seq += 1
                item = {"id": self._chat_seq, "ts": now, "attachments": attachments}
                self.attach_log = [a for a in self.attach_log if now - a["ts"] < 120][-3:] + [item]
            self.broadcast({"type": "overlay", "item": "attach", **item})
            self.show_tab("chat")
        if problems:
            self.alert(f"attach-{now:.0f}", "; ".join(problems)[:200], level="warn", cooldown=0)

    def attach_paths(self, paths: list[str]) -> None:
        from .brain import attachments
        self.attach(*attachments.from_paths(paths))

    def attach_clipboard(self) -> None:
        from .brain import attachments
        self.attach(*attachments.from_clipboard())

    def dismiss_alert(self, key: str) -> None:
        with self._lock:
            self.alerts = [a for a in self.alerts if a["key"] != key]
        self._refresh()

    def _on_window(self, ev: Event) -> None:
        if ev.app and ev.app.lower() not in ("pythonw.exe", "python.exe", "strux.exe", "halo.exe"):
            self._current = (ev.app, ev.title)
            self._current_hwnd = int((ev.data or {}).get("hwnd") or 0)
            self._refresh()

    # ---- pane -------------------------------------------------------------------
    def summon(self) -> None:
        self.open_pane("chat")
        self.broadcast({"type": "overlay", "item": "focus-chat"})

    def expand(self, mode, width=None, height=None) -> None:
        if mode in ("chat", "panel"):
            self.open_pane(mode)
        elif mode in ("idle", "bar", False, None):
            self.close_pane()

    def close(self) -> None:
        self.close_pane()

    def keep(self) -> None:
        pass

    def fit(self, height: float, width: float | None = None) -> None:
        h = max(120, min(720, int(round(height))))
        if abs(h - self.pane_h) < 4:
            return
        self.pane_h = h
        if self.pane:
            self.windows.resize_pane(int(round(h * (self.bar.scale or 1.0))))

    def toggle_pane(self, tab: str) -> None:
        if self.pane == tab:
            self.close_pane()
            return
        if self.pane is not None and self.windows.pane_visible():   # switching tabs: keep the window, change the tab
            self.pane = tab
            self._pane_opened_at = time.time()
            self.bar.update(pane=tab)
            self.broadcast({"type": "overlay", "item": "pane", "tab": tab})
            return
        self.open_pane(tab)

    def show_tab(self, tab: str) -> None:
        """Make sure the pane is open on `tab` (switch in place if it is already open)."""
        if self.pane == tab and self.windows.pane_visible():
            return
        if self.pane is not None and self.windows.pane_visible():
            self.toggle_pane(tab)
        else:
            self.open_pane(tab)

    def _on_bar_press(self) -> None:
        self._bar_pressed_at = time.time()

    def open_pane(self, tab: str) -> None:
        self._pane_opened_at = time.time()
        g = self.bar.geometry()
        s = g["scale"] or 1.0
        self.pane = tab
        self.bar.update(pane=tab)
        self.broadcast({"type": "overlay", "item": "pane", "tab": tab})
        w_ = int(round(520 * s))
        x = int(g["center_x"] - w_ / 2)
        y = int(g["top"] + g["bar_h"] + 6 * s)
        self.windows.show_pane(x, y, w_, int(round(self.pane_h * s)))

    def close_pane(self, by_blur: bool = False) -> None:
        if self.pane is None:
            return
        if by_blur and time.time() - max(self._pane_opened_at, self._bar_pressed_at) < 0.6:
            return          # focus moved to the bar (part of the same UI) or the pane is just opening
        self._pane_closed_tab, self._pane_closed_at = self.pane, time.time()
        self.pane = None
        self.bar.update(pane=None)
        self.windows.hide_pane()

    # ---- bar actions --------------------------------------------------------------
    def _on_bar(self, key: str, arg: Any) -> None:
        if key in ("chat", "panel"):
            self.toggle_pane(key)
        elif key == "headline":
            self.toggle_pane("panel")
        elif key == "close":
            self.close_pane()
        elif key == "dashboard":
            self.close_pane()
            self.app.show_window()
        elif key == "pause":
            self.app.set_paused(not self.app.paused)
        elif key == "think":
            self.app.brain.think_now()
        elif key in ("accept", "snooze", "dismiss") and arg is not None:
            self.app.suggestion_feedback(int(arg), key)
        elif key == "alert-x" and arg:
            self.dismiss_alert(str(arg))
        elif key == "drop_files" and arg:
            self.attach_paths(list(arg))

    # ---- bar state ------------------------------------------------------------------
    def _refresh(self, peek=None) -> None:
        with self._lock:
            waiting = None
            try:
                for c in self.app.claude.state()["sessions"]:
                    if c.get("waiting"):
                        waiting = c.get("project") or c.get("id")
                        break
            except Exception:
                pass
            pending = len(self.suggestions) + len(self.alerts) + (1 if waiting else 0)
            if self.suggestions:
                headline, emph = self.suggestions[0]["text"], True
            elif self.alerts:
                headline, emph = self.alerts[-1]["text"], True
            else:
                app_, title = self._current
                from .brain.worklog import LEAD_GLYPHS
                title = LEAD_GLYPHS.sub("", title or "").strip()      # Claude Code's spinner glyphs have no font glyph
                headline = app_label(app_) + (f" · {title}" if title and app_ else "")
                emph = False
        changes: dict[str, Any] = {"headline": headline, "emphasis": emph, "pending": pending,
                                   "waiting": waiting, "paused": bool(self.app.paused)}
        if peek is not None:
            changes["peek"] = peek
        self.bar.update(**changes)
