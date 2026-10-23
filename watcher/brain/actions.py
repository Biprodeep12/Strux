"""What happens when you accept a suggestion.

Every suggestion may carry `context["action"]`: {"type": ..., ...}. Accepting runs it on a
worker thread and the result lands in the Strux pane as a chat message, so the follow-up
conversation can continue from there.

  explain_error  {frame_id}         OCR text -> text model; no OCR -> vision model on the frame
  recap          {start, end}       "where you left off" from the work log
  focus          {minutes}          focus timer: rules (except errors / jobs) stay quiet, alert at the end
  break          {minutes}          break timer, alert when it's over
  discuss        {}                 opens the chat with the suggestion, for follow-up questions
Snooze re-surfaces the suggestion after `snooze_minutes`; see Brain.resurface_snoozed.
"""
from __future__ import annotations

import logging
import threading
import time

from . import chat as chat_mod
from . import prompts
from . import vision

log = logging.getLogger(__name__)



class Actions:
    def __init__(self, app) -> None:
        self.app = app
        self._timer: threading.Timer | None = None
        self.focus_until = 0.0
        self.timer_kind = ""

    # ---- entry -------------------------------------------------------------------------
    def run(self, sid: int) -> None:
        row = self.app.store.suggestion(sid)
        if not row:
            return
        import json
        ctx = json.loads(row["context"] or "{}")
        act = ctx.get("action") or {"type": "discuss"}
        log.info("suggestion #%s accepted: running %s", sid, act.get("type"))
        threading.Thread(target=self._run, args=(row, act), name=f"action-{sid}-{act.get('type')}", daemon=True).start()

    def _run(self, row, act: dict) -> None:
        kind = act.get("type")
        try:
            if kind == "explain_error":
                self._explain(row, act)
            elif kind == "recap":
                self._recap(row, act)
            elif kind in ("focus", "break"):
                self.start_timer(kind, float(act.get("minutes", 25 if kind == "focus" else 5)))
            elif kind == "discuss":
                self._discuss(row)
            elif kind == "add_todo":
                t = self.app.planner.add_todo(act.get("title") or row["text"], source="suggestion",
                                              project=act.get("project", ""))
                t.pop("_existing", None)
                self._notify(f"Added to your TODOs: {t['title']}")
            elif kind == "complete_todo":
                t = self.app.planner.update_todo(int(act["id"]), done=True)
                self._notify(f"Done: {t['title']}" if t else "That TODO no longer exists.")
            elif kind == "plan":
                self._plan()
            elif kind == "prep_event":
                self._prep(act)
            elif kind == "digest":
                self._digest()
        except Exception:
            log.exception("action %s failed", kind)
            self._say("That didn't work, see watcher.log.", error=True)

    # ---- pane output ---------------------------------------------------------------------
    def _say(self, text: str, *, pending: bool = False, error: bool = False, open_chat: bool = True,
             model: str | None = None) -> None:
        log.info("action -> pane chat%s: %r", " (pending)" if pending else "", text[:120])
        ov = self.app.overlay
        msg = {"thread": threading.current_thread().name, "content": text, "pending": pending, "error": error, "model": model}
        if ov:
            ov.post_chat(msg)          # kept server-side too: a pane that wasn't loaded yet picks it up on open
            if open_chat:
                ov.show_tab("chat")
        else:
            self.app.broadcast({"type": "overlay", "item": "chat", "ts": time.time(), **msg})

    def _notify(self, text: str) -> None:
        """Small confirmations go to the bar, not the chat."""
        if self.app.overlay:
            self.app.overlay.alert(f"done-{time.time():.0f}", text, level="info", cooldown=0)

    # ---- actions -------------------------------------------------------------------------
    def _explain(self, row, act: dict) -> None:
        llm, store = self.app.brain.llm, self.app.store
        fr = store.frame(int(act["frame_id"])) if act.get("frame_id") else None
        if not fr:
            self._say("The screen that showed the error is no longer stored.", error=True)
            return
        if not llm.reachable():
            self._say(f"Model server not reachable at {llm.base}.", error=True)
            return
        where = f"{act.get('label') or fr['app']} · {(fr['title'] or '')[:60]}"
        self._say(f"Looking at the error in {where}…", pending=True)
        self.app.overlay and self.app.overlay.set_processing("thinking")
        try:
            text = (fr["ocr_text"] or "").strip()
            if len(text) > 40:
                ans = llm.chat([{"role": "user", "content": prompts.get("explain", where=where, text=_around_error(text))}],
                               max_tokens=260, temperature=0.1)
                err, used = llm.last_error, llm.model
            else:
                model = self.app.cfg["brain"]["llm"].get("vision_model", "qwen2.5vl:3b")
                r = vision.explain_frame(llm, fr, model)
                ans, err, used = r.get("text"), r.get("error"), model
        finally:
            self.app.overlay and self.app.overlay.set_processing("idle")
        self._say(ans or f"Couldn't explain it: {err or 'model call failed'}", error=not ans, model=used if ans else None)

    def _recap(self, row, act: dict) -> None:
        llm = self.app.brain.llm
        start, end = float(act.get("start", time.time() - 3600)), float(act.get("end", time.time()))
        self._say("Pulling up where you left off…", pending=True)
        if not llm.reachable():
            from . import worklog
            self._say(worklog.build(self.app.store, start, end) or "Nothing recorded before the break.")
            return
        q = [{"role": "user", "content": prompts.get("recap")}]
        ans = chat_mod.answer(llm, self.app.store, q, window=(start, end, "the 90 minutes before the break"),
                              name=getattr(self.app, "user_name", ""))
        self._say(ans or "Couldn't build a recap.", error=not ans, model=llm.model if ans else None)

    def _discuss(self, row) -> None:
        """The suggestion, then the model's why / how, grounded in the last hour of the log."""
        llm = self.app.brain.llm
        self._say(row["text"])
        if not llm.reachable():
            return
        self._say("Thinking about it…", pending=True)
        now = time.time()
        msgs = [{"role": "assistant", "content": row["text"]},
                {"role": "user", "content": prompts.get("discuss")}]
        ans = chat_mod.answer(llm, self.app.store, msgs, window=(now - 3600, now, "the last hour"), name=getattr(self.app, "user_name", ""))
        self._say(ans or "Couldn't get more on that.", error=not ans, model=llm.model if ans else None)

    def _plan(self) -> None:
        llm = self.app.brain.llm
        self._say("Planning your day…", pending=True)
        if not llm.reachable():
            self._say(self.app.planner.context_text(ids=False))
            return
        ans = chat_mod.plan(llm, self.app.store, self.app.planner, getattr(self.app, "user_name", ""))
        self._say(ans or "Couldn't make a plan.", error=not ans, model=llm.model if ans else None)

    def _prep(self, act: dict) -> None:
        """Before an event: what it is, related TODOs, what you were just doing that relates to it."""
        llm = self.app.brain.llm
        ev = next((e for e in self.app.planner.events_between(time.time() - 3600, time.time() + 86400) if e["id"] == act.get("id")), None)
        if not ev:
            self._say("That event is gone from the calendar.", error=True)
            return
        self._say(f"Prepping for “{ev['title']}”…", pending=True)
        q = [{"role": "user", "content": f"I have “{ev['title']}” at {time.strftime('%H:%M', time.localtime(ev['start_ts']))}"
              + (f" ({ev['location']})" if ev["location"] else "") + (f". Notes: {ev['notes']}" if ev["notes"] else "")
              + ". In 3 short bullets: what to have ready, related TODOs, and anything from today's work that's relevant."}]
        now = time.time()
        ans = chat_mod.answer(llm, self.app.store, q, window=(now - 6 * 3600, now, "today"), name=getattr(self.app, "user_name", ""),
                              planner=self.app.planner) if llm.reachable() else None
        self._say(ans or f"“{ev['title']}” at {time.strftime('%H:%M', time.localtime(ev['start_ts']))}.", error=False, model=llm.model if ans else None)

    def _digest(self) -> None:
        llm = self.app.brain.llm
        self._say("Wrapping up your day…", pending=True)
        now = time.time()
        start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        q = [{"role": "user", "content": "Wrap up my day: what did I get done, what's still open (TODOs due today or overdue), "
                                         "and the first thing to start with tomorrow."}]
        ans = chat_mod.answer(llm, self.app.store, q, window=(start, now, "today"), name=getattr(self.app, "user_name", ""),
                              planner=self.app.planner) if llm.reachable() else None
        self._say(ans or "Couldn't build the wrap-up.", error=not ans, model=llm.model if ans else None)

    def start_timer(self, kind: str, minutes: float) -> None:
        if self._timer:
            self._timer.cancel()
        self.timer_kind = kind
        self.focus_until = time.time() + minutes * 60 if kind == "focus" else 0.0
        ov = self.app.overlay
        if ov:
            msg = f"Focus for {int(minutes)} min. Suggestions stay quiet until then." if kind == "focus" else f"Break: {int(minutes)} min. I'll tell you when it's over."
            ov.alert(f"timer-{kind}", msg, level="info", cooldown=0)
        self._timer = threading.Timer(minutes * 60, self._timer_done, args=(kind, minutes))
        self._timer.daemon = True
        self._timer.start()

    def _timer_done(self, kind: str, minutes: float) -> None:
        self.focus_until, self.timer_kind, self._timer = 0.0, "", None
        ov = self.app.overlay
        if ov:
            ov.dismiss_alert(f"timer-{kind}")
            ov.alert(f"timer-{kind}-done", f"{int(minutes)}-min focus done. Nice." if kind == "focus" else "Break's over.", level="info", cooldown=0)

    @property
    def focusing(self) -> bool:
        return time.time() < self.focus_until


def _around_error(text: str, width: int = 1800) -> str:
    """The OCR text around the first error-looking line, bounded."""
    from ..sensors.screenshot import OCR_ERROR_RE
    m = OCR_ERROR_RE.search(text)
    if not m:
        return text[:width]
    a = max(0, m.start() - width // 3)
    return text[a:a + width]
