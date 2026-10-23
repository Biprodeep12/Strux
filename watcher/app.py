"""Wires everything together: store <- bus <- sensors, brain -> toast/overlay/ws,
API server, tray, hotkeys, window manager (dashboard + overlay)."""
from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
from pathlib import Path
from typing import Callable

from . import config as config_mod
from . import priority
from .brain import Brain
from .brain.actions import Actions
from .events import Bus, Event, Suggestion
from .hotkey import Hotkeys
from .ui.boost import UiBoost
from .overlay import Overlay
from .retention import RetentionThread
from .claude_bridge import ClaudeBridge
from .sensors import BrowserSensor, ClipboardSensor, IdleSensor, ProcessSensor, ScreenshotSensor, WindowSensor
from .store import Store
from .ui import Tray, toast

log = logging.getLogger(__name__)


def setup_logging(data_dir: Path, level: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fh = logging.handlers.RotatingFileHandler(data_dir / "watcher.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    handlers: list[logging.Handler] = [fh]
    import sys
    if sys.stderr is not None:  # pythonw / windowed exe has no console
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        handlers.append(sh)
    root.handlers = handlers
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("comtypes").setLevel(logging.WARNING)
    for noisy in ("faster_whisper", "httpx", "httpx2", "huggingface_hub"):       # per-clip / per-download chatter
        logging.getLogger(noisy).setLevel(logging.WARNING)


class App:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or config_mod.load()
        self.data_dir = Path(self.cfg["data_dir"])
        setup_logging(self.data_dir, self.cfg.get("log_level", "INFO"))
        priority.apply(self.cfg.get("priority", {}))

        self.bus = Bus()
        self.store = Store(self.data_dir)
        from .brain import prompts
        from .planner import Planner
        prompts.configure(self.cfg)
        self.broadcasters: list[Callable[[dict], None]] = []   # WebSocket hub(s) register here
        self.planner = Planner(self.data_dir, on_change=lambda kind: self.broadcast({"type": "planner", "kind": kind}))
        from .brain import agent as _agent, welcome as _welcome
        from .memory import Memory
        self.memory = Memory(self.data_dir, on_change=lambda: self.broadcast({"type": "planner", "kind": "memory"}),
                             name=_welcome.user_name(self.cfg.get("user", {}).get("name", "")))
        prompts.set_memory(self.memory.context_text)
        from .knowledge import Knowledge
        self.knowledge = Knowledge(self.data_dir, self.cfg.setdefault("learn", {}))
        _agent.MEMORY = self.memory
        from .stt import SpeechToText
        self.stt = SpeechToText(self.cfg.get("stt", {}), self.data_dir, hints=self._speech_hints)
        from .wake import WakeListener
        self.wake = WakeListener(self.cfg.setdefault("stt", {}), self.stt, on_command=self._voice_command,
                                 on_state=self._voice_state, suppressed=self._voice_suppressed)
        self.stt.keep_loaded = lambda: self.wake.running
        self.speaking_until = 0.0              # the pane is reading a reply out: the wake listener stands down
        self.bus.subscribe("*", self._to_store)
        self.on_suggestion: list[Callable[[Suggestion], None]] = []

        s = self.cfg["sensors"]
        self.sensors = []
        if s["window"]["enabled"]:
            self.sensors.append(WindowSensor(self.bus, s["window"]))
        if s["idle"]["enabled"]:
            self.sensors.append(IdleSensor(self.bus, s["idle"]))
        if s["clipboard"]["enabled"]:
            self.sensors.append(ClipboardSensor(self.bus, s["clipboard"]))
        if s["screenshot"]["enabled"]:
            self.sensors.append(ScreenshotSensor(self.bus, s["screenshot"], self.store, self.data_dir))
        if s.get("browser", {}).get("enabled", True):
            self.sensors.append(BrowserSensor(self.bus, s.get("browser", {})))
        if s.get("process", {}).get("enabled", True):
            self.sensors.append(ProcessSensor(self.bus, s.get("process", {})))

        self.brain = Brain(self.cfg["brain"], self.store, self._notify, on_state=self._brain_state)
        # our own resident models (whisper while "Hey Strux" is on, the warm chat model) must not read as a busy GPU
        self.brain.gpu.own_mb = lambda: self.stt.vram_mb() + self.brain.llm.resident_vram_mb()
        self.actions = Actions(self)
        self.brain.focusing = lambda: self.actions.focusing
        self.brain.planner = self.planner
        self.brain.procs = next((x for x in self.sensors if x.name == "process"), None)
        self.brain.on_expire = lambda sid: self.overlay.feedback_done(sid) if getattr(self, "overlay", None) else None
        self.brain.user_name = lambda: self.user_name
        self.retention = RetentionThread(self.store, self.cfg["retention"])
        self.paused = False
        from .brain.learner import Learner
        from .brain.llm import system_busy
        self.learner = Learner(self.cfg["learn"], self.knowledge, self.memory, self.store, self.brain.llm,
                               busy=lambda: system_busy(self.cfg["brain"], self.brain.gpu), paused=lambda: self.paused)
        self.learner.store_name = lambda: self.user_name
        self._last_url: tuple[str, str] = ("", "")        # (app, url) of the last browser tab seen
        self.bus.subscribe("url", lambda ev: setattr(self, "_last_url", (ev.app, (ev.data or {}).get("url", ""))))

        srv = self.cfg.get("server", {})
        self.api = None
        if srv.get("enabled", True):
            from .api import ApiServer
            self.api = ApiServer(self, srv.get("host", "127.0.0.1"), int(srv.get("port", 8765)))

        self.windows = None
        self.overlay: Overlay | None = None
        if self.api and self.cfg["ui"].get("window", True):
            from .ui.window import WindowManager
            self.windows = WindowManager(self.url)
            self.overlay = Overlay(self, self.windows, self.cfg["ui"].get("overlay", {}), self.broadcast)
        self.claude = ClaudeBridge(self.bus, self.overlay)
        self.claude.on_stop = self.knowledge.claude_stopped          # read the finished turn from its transcript
        self.bus.subscribe("idle_end", self._on_return)
        self.tray = None
        self.hotkeys = Hotkeys()
        self._started = time.time()

    # ---- plumbing ------------------------------------------------------
    def _to_store(self, ev: Event) -> None:
        if ev.kind == "_processing":
            self._brain_state(ev.data.get("stage", "idle"), ev.data.get("detail", ""))
            return
        if ev.kind == "proc_top":
            return  # live-only snapshot, not worth a row every 30 s
        self.store.put_event(ev)

    def broadcast(self, msg: dict) -> None:
        for fn in self.broadcasters:
            try:
                fn(msg)
            except Exception:
                log.exception("broadcast failed")

    def _notify(self, s: Suggestion) -> None:
        if self.cfg["ui"].get("toasts", True) and not (self.overlay and self.overlay.enabled):
            toast.show(s, launch=self.url)   # toasts only when the overlay is off (no double notification)
        if self.overlay:
            self.overlay.suggestion(s)
        for fn in self.on_suggestion:
            try:
                fn(s)
            except Exception:
                log.exception("on_suggestion hook failed")

    def _brain_state(self, stage: str, detail: str = "") -> None:
        """Called by the brain: thinking / ocr / vision / idle, plus alerts."""
        if self.overlay:
            if stage == "alert":
                self.overlay.alert(detail.split(":", 1)[0], detail.split(":", 1)[-1].strip())
            else:
                self.overlay.set_processing(stage, detail)
        else:
            self.broadcast({"type": "overlay", "item": "processing", "stage": stage, "detail": detail})

    @property
    def url(self) -> str | None:
        return self.api.url if self.api else None

    # ---- controls ------------------------------------------------------
    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        for s in self.sensors:
            (s.paused.set if paused else s.paused.clear)()
        (self.brain.paused.set if paused else self.brain.paused.clear)()
        if self.tray:
            self.tray.refresh()
        if self.overlay:
            self.overlay.alert("paused" if paused else "resumed", "Watching paused. Ctrl+Alt+P to resume." if paused else "Watching resumed.",
                               level="info", cooldown=0)
        log.info("paused=%s", paused)

    def _on_return(self, ev: Event) -> None:
        """Back from being away: greet right away (not on the next brain tick)."""
        idle = float(ev.data.get("idle_seconds", 0))
        rc = self.cfg["brain"].get("rules", {}).get("welcome_back", {})
        if self.paused or not rc.get("enabled", True) or idle < float(rc.get("min_idle_minutes", 10)) * 60:
            return
        threading.Thread(target=self.welcome_back, args=(idle,), name="welcome", daemon=True).start()

    @property
    def user_name(self) -> str:
        from .brain import welcome
        return welcome.user_name(self.cfg.get("user", {}).get("name", ""))

    def live_context(self) -> str:
        """Right-now state for the chat: what is running and what Claude Code is doing."""
        from .overlay import app_label
        lines = []
        ps = next((x for x in self.sensors if x.name == "process"), None)
        if ps and ps.top:
            busy = set(ps.hot.values())
            lines.append("Running right now (top CPU): " + ", ".join(
                f"{p.get('label') or app_label(p['name'])} {p['cpu']:.0f}% CPU {p['mem_mb']} MB" + (" (busy)" if p["name"] in busy else "")
                for p in ps.top[:6]))
        for c in self.claude.state()["sessions"][:3]:
            lines.append(f"Claude Code session in {c.get('project') or c['id']}: {c['status']}"
                         + (" (waiting for you)" if c.get("waiting") else "") + (f"; last: {c['last']}" if c.get("last") else ""))
        app_, title = self.foreground()
        if app_:
            url = self._last_url[1] if self._last_url[0] == app_ else ""
            lines.insert(0, f"In front right now: {app_label(app_)} — “{title[:140]}”" + (f" ({url[:120]})" if url else ""))
        return "Live state:\n" + "\n".join(lines) if lines else ""

    def foreground(self) -> tuple[str, str]:
        """(app, title) of the window you were using: the last one in front that is not Strux itself."""
        if self.overlay is not None and self.overlay._current[0]:
            return self.overlay._current
        from .sensors import winapi                   # nothing switched since start: ask Windows
        hwnd, app_, title = winapi.foreground_info()
        if (app_ or "").lower() in ("pythonw.exe", "python.exe", "strux.exe", "halo.exe"):
            return "", ""
        if self.overlay is not None and app_:
            self.overlay._current, self.overlay._current_hwnd = (app_, title), int(hwnd or 0)
        return app_, title

    def screen_target(self) -> dict:
        app_, title = self.foreground()
        hwnd = getattr(self.overlay, "_current_hwnd", 0) if self.overlay is not None else 0
        url = self._last_url[1] if self._last_url[0] == app_ else ""
        return {"hwnd": hwnd, "app": app_, "title": title, "url": url}

    def screen_ctx(self) -> dict:
        return {"target": self.screen_target(), "shot_cfg": self.cfg["sensors"].get("screenshot", {}),
                "vision_model": self.cfg["brain"]["llm"].get("vision_model", "qwen2.5vl:3b"),
                "learn": self.cfg["learn"].get("enabled", True) and self.cfg["learn"].get("screens", True),
                "vision": bool(self.cfg.get("screen", {}).get("vision", False))}

    def chips(self) -> list[str]:
        """Quick questions for the empty pane chat, from what is going on right now."""
        from datetime import datetime
        now = time.time()
        out: list[str] = []
        app_, _title = self.foreground()
        a = (app_ or "").lower()
        try:
            if self.store.recent_events(10, kinds=("ocr_error",), now=now):
                out.append("Explain the error on my screen")
        except Exception:
            pass
        if a in ("msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "arc.exe"):
            out.append("Summarise this page")
        elif a in ("excel.exe", "winword.exe", "powerpnt.exe", "acrord32.exe", "acrobat.exe"):
            out.append(f"What's in this {'sheet' if a == 'excel.exe' else 'document'}?")
        out.append("What's on my screen?")
        for c in self.claude.state()["sessions"][:2]:
            if c.get("waiting"):
                out.append(f"What does Claude need in {c.get('project') or 'the session'}?")
                break
            if c.get("status") == "working":
                out.append(f"What is Claude doing in {c.get('project') or 'the session'}?")
                break
        try:
            soon = [e for e in self.planner.events_between(now, now + 3 * 3600) if e["start_ts"] > now]
            if soon:
                out.append(f"Prep me for {soon[0]['title'][:40]}")
            if any(t.get("due_ts") and t["due_ts"] < now + 86400 for t in self.planner.todos()):
                out.append("What's due today?")
        except Exception:
            pass
        h = datetime.now().hour
        out.append("Plan my day" if h < 13 else "What did I get done today?" if h >= 18 else "What did I do in the last hour?")
        if self.knowledge.stats().get("claude"):
            out.append("What did I ask Claude today?")
        return list(dict.fromkeys(out))[:5]

    def welcome_back(self, idle: float) -> Suggestion | None:
        from .brain import welcome
        now = time.time()
        rc = self.cfg["brain"].get("rules", {}).get("welcome_back", {})
        if now - self.store.last_suggestion_ts("rule:welcome_back", now=now) < float(rc.get("cooldown_minutes", 5)) * 60:
            return None
        self.store.flush()
        text = welcome.compose(self.store, idle, now, self.user_name)
        left = now - idle
        return self.brain._deliver(Suggestion(text=text, source="rule:welcome_back",
                                              context={"idle": int(idle), "action": {"type": "recap", "start": left - 90 * 60, "end": left}}))

    def suggestion_feedback(self, sid: int, value: str) -> bool:
        """accept runs the suggestion's action, snooze brings it back later, dismiss teaches the cooldowns."""
        if not self.store.set_feedback(sid, value):
            return False
        if self.overlay:
            self.overlay.feedback_done(sid)
        if value == "accept":
            self.actions.run(sid)
        elif value == "snooze":
            self.brain.snooze(sid)
        return True

    def clear_timeline(self) -> dict[str, int]:
        """Settings -> 'Clear timeline': the activity history goes, plus anything kept in memory from it."""
        res = self.store.clear_timeline()
        try:
            from .brain import vision
            vision._read_cache.clear()
        except Exception:
            pass
        if self.overlay:
            self.overlay.chat_log.clear()
        res["screen_notes"] = self.knowledge.clear("screen")       # what Strux kept of your screens goes with the timeline
        from .brain import screen
        screen.forget()
        log.info("timeline cleared: %s", res)
        return res

    def forget_minutes(self, minutes: float) -> dict[str, int]:
        self.store.flush()
        res = self.store.forget_since(time.time() - minutes * 60)
        res["screen_notes"] = self.knowledge.forget_since(time.time() - minutes * 60)
        from .brain import screen
        screen.forget()
        log.info("forgot last %s min: %s", minutes, res)
        return res

    def show_window(self) -> None:
        if self.windows:
            self.windows.show("dashboard")
        elif self.url:
            os.startfile(self.url)  # --no-window mode: open the dashboard in the default browser

    @property
    def window(self):
        """Backwards-compatible handle used by the tray/hotkeys."""
        return self.windows

    def status(self) -> dict:
        st = self.store.stats()
        st["paused"] = self.paused
        st["brain_ticks"] = self.brain.ticks
        st["brain_skip"] = self.brain.last_skip_reason
        st["llm_error"] = self.brain.llm.last_error
        st["url"] = self.url
        st["uptime"] = int(time.time() - self._started)
        st["processing"] = self.overlay.processing if self.overlay else "idle"
        st["overlay_open"] = bool(self.overlay and self.overlay.enabled)
        st["pane"] = self.overlay.pane if self.overlay else None
        ps = next((x for x in self.sensors if x.name == "process"), None)
        st["top_processes"] = ps.top if ps else []
        st["hot_processes"] = sorted(set(ps.hot.values())) if ps else []
        st["claude"] = self.claude.state()["sessions"][:3]
        st["focus_until"] = self.actions.focus_until or None
        st["wake"] = self.wake.status()
        return st

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self._started = time.time()
        self.store.start()
        for s in self.sensors:
            s.start()
        self.brain.start()
        self.retention.start()
        self.knowledge.start()
        self.learner.start()
        if self.api:
            self.api.start()
        if self.overlay:
            self.overlay.start()
        if self.cfg.get("stt", {}).get("wake", {}).get("enabled"):
            self.wake.start()                # "Hey Strux" (only if you switched it on)
        if self.windows:
            UiBoost().start()   # WebView2 renderers get NORMAL priority, no EcoQoS, so the bar never drops frames
        self.hotkeys.bind("F", lambda: (self.forget_minutes(10), toast.info("Forgot last 10 min", "Events, frames and files removed")))
        self.hotkeys.bind("P", lambda: self.set_paused(not self.paused))
        self.hotkeys.bind("W", lambda: self.windows and self.windows.toggle("dashboard"))
        self.hotkeys.bind(" ", lambda: self.overlay and self.overlay.summon())
        self.hotkeys.bind("V", lambda: self.overlay and self.overlay.attach_clipboard())   # ask about what you just copied
        self.hotkeys.start()
        log.info("watcher-ai started with sensors: %s", [s.name for s in self.sensors])

    # ---- "Hey Strux" ----------------------------------------------------------------------------
    def set_wake(self, on: bool) -> None:
        self.cfg.setdefault("stt", {}).setdefault("wake", {})["enabled"] = bool(on)
        (self.wake.start if on else self.wake.stop)()

    def _voice_suppressed(self) -> bool:
        from .sensors import winapi
        try:
            locked = winapi.session_locked()
        except Exception:
            locked = False
        return self.paused or locked or self.stt.recording or time.time() < self.speaking_until

    def _voice_command(self, text: str) -> None:
        if self.overlay:
            self.overlay.voice_message(text)

    def _voice_state(self, state: str) -> None:
        if self.overlay:
            self.overlay.set_processing({"listening": "listening", "transcribing": "hearing"}.get(state, "idle"))
            if state == "nothing":
                self.overlay.alert("voice-nothing", "I heard “Hey Strux” but nothing after it.", level="info", cooldown=5)

    def _speech_hints(self) -> str:
        """Words Whisper should spell the way you do: your name, Strux, open TODOs' projects."""
        bits = [self.user_name, "Strux", "Claude Code", "TODO"]
        try:
            bits += sorted({t["project"] for t in self.planner.todos() if t["project"]})[:8]
        except Exception:
            pass
        return ", ".join(b for b in bits if b) + "."

    def stop(self) -> None:
        log.info("stopping")
        try:
            self.stt.cancel()
            self.wake.stop()
        except Exception:
            pass
        self.hotkeys.stop()
        if self.api:
            self.api.stop()
        self.retention.stop()
        self.brain.stop()
        for s in self.sensors:
            s.stop()
        self.store.stop()
        self.learner.stop()
        self.knowledge.close()
        self.planner.close()
        self.memory.close()

    def run_forever(self, window: bool | None = None) -> None:
        """Main-thread entry. Tray runs detached; the window manager owns the main thread."""
        self.start()
        use_window = self.cfg["ui"].get("window", True) if window is None else window
        try:
            if self.cfg["ui"].get("tray", True):
                self.tray = Tray(self)
                self.tray.run_detached()
            if use_window and self.windows:
                start_with: list[str] = [] if self.cfg["ui"].get("start_hidden", False) else ["dashboard"]
                if self.overlay and self.overlay.enabled:
                    start_with.append("pane")      # hidden drop-down, created once so it opens instantly
                self.windows.run(start_with=tuple(start_with))
            else:
                while not (self.tray and self.tray.quit_requested.is_set()):
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            if self.tray:
                self.tray.stop()
            self.stop()
