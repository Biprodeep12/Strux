"""The brain loop. One thread, wakes every `tick_seconds`:

  1. skip if paused, or user is idle (nobody to see a toast)
  2. re-surface snoozed suggestions that are due
  3. build the activity window (bounded query)
  4. run rules (free)        -> suggestion?   (only urgent ones during a focus timer)
  5. every `interval_minutes`, if the machine is not busy, ask the LLM
  6. enforce per-source + global cooldowns, store, toast

Cooldowns learn from feedback: a rule whose last suggestions were dismissed waits 4x / 8x longer.

Nothing here ever touches the GPU unless step 5 passes the guards.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Callable

from ..events import Suggestion
from ..sensors import winapi
from . import rules as rules_mod
from . import summarizer, worklog
from .llm import LLM, GpuGuard, system_busy

log = logging.getLogger(__name__)

# words that carry no topic: two suggestions that differ only in these are the same suggestion
FILLER = set("""a an the and or of in on at to for from with by into about your you my me i it this that these those is are be
was were will can could should would do does did add todo todos task tasks maybe now next then just quick
review check test tests testing look looking verify update finish finishing continue work working changes change
file files code project make sure run start open left come came back pick again page tab did not didnt still today
time minutes min hours hour since about due""".split())


LEFT_RE = re.compile(r"\b(you )?left\b|did(n't| not) come back|pick (it|that) up again|went back to", re.I)


def topic_words(text: str) -> set[str]:
    """Lower-case topic words of a suggestion, punctuation and filler stripped ('“memory.py”?' -> 'memory.py')."""
    return {w.strip("._-") for w in re.findall(r"[a-z0-9][a-z0-9._+#-]*", (text or "").lower())
            if len(w) > 2 and w.strip("._-") not in FILLER and not w.replace(":", "").isdigit()}


def _is_test(row) -> bool:
    return (row["source"] or "") == "rule:test" or "[e2e]" in (row["text"] or "")


class Brain:
    def __init__(self, cfg: dict, store, notify: Callable[[Suggestion], None],
                 on_state: Callable[[str, str], None] | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.notify = notify
        self.on_state = on_state or (lambda stage, detail="": None)
        self.paused = threading.Event()
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="brain", daemon=True)
        self.llm = LLM(cfg.get("llm", {}))
        self.gpu = GpuGuard()
        self._last_llm = 0.0
        self._last_hour_summary = time.time()
        self.last_skip_reason: str | None = None
        self.ticks = 0
        self.snoozed: dict[int, float] = {}       # suggestion id -> when it was snoozed
        self.focusing: Callable[[], bool] = lambda: False
        self.planner = None                        # set by the app: TODOs / calendar for rules + prompt
        self.procs = None                          # set by the app: the process sensor
        self.user_name: Callable[[], str] = lambda: ""
        self.on_expire: Callable[[int], None] = lambda sid: None   # the app takes expired items out of the bar / pane

    def snooze(self, sid: int) -> None:
        self.snoozed[sid] = time.time()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        self._thread.join(timeout=3)

    def think_now(self) -> None:
        """Force a tick (tray menu)."""
        self._kick.set()

    # ---- loop ------------------------------------------------------------
    def _loop(self) -> None:
        tick = float(self.cfg.get("tick_seconds", 30))
        time.sleep(5)  # let sensors warm up
        while not self._stop.is_set():
            forced = self._kick.is_set()
            self._kick.clear()
            try:
                self.tick(forced=forced)
            except Exception:
                log.exception("brain tick failed")
            self._kick.wait(timeout=tick)

    def tick(self, forced: bool = False) -> Suggestion | None:
        self.ticks += 1
        now = time.time()
        if self.paused.is_set():
            return None
        if self.cfg.get("quiet_when_idle", True) and winapi.idle_seconds() > 120 and not forced:
            self.last_skip_reason = "idle"
            return None

        self._resurface_snoozed(now)
        self._expire_info(now)
        win = summarizer.build(self.store, minutes=15, now=now)
        self._maybe_hour_summary(win, now)
        self._maybe_day_summary(now)
        focusing = self.focusing()
        win.planner, win.procs, win.user = self.planner, self.procs, self.user_name()
        if win.current_app and win.current_app in {a.lower() for a in self.cfg.get("never_suggest_apps", [])}:
            self.last_skip_reason = f"never-suggest app {win.current_app}"
            return None

        # 1) rules
        rule_cfgs = self.cfg.get("rules", {})
        for rule in rules_mod.ALL_RULES:
            rc = rule_cfgs.get(rule.name, {})
            if not rc.get("enabled", True) or (focusing and not rule.urgent):
                continue
            cd = float(rc.get("cooldown_minutes", 30))
            if not self._cooldown_ok(f"rule:{rule.name}", self._learned_cooldown(f"rule:{rule.name}", cd) if rule.learns else cd, now):
                continue
            try:
                s = rule.evaluate(win, self.store, rc)
            except Exception:
                log.exception("rule %s failed", rule.name)
                continue
            if s and (rule.urgent or self._global_ok(now)):
                return self._deliver(s)

        # 2) LLM
        llm_cfg = self.cfg.get("llm", {})
        if not llm_cfg.get("enabled", True) or (focusing and not forced):
            return None
        # the model learns from dismissals like the rules do: 2 in a row -> 4x the interval, 3 -> 8x
        interval = self._learned_cooldown("llm", float(llm_cfg.get("interval_minutes", 5)))
        due = forced or (now - self._last_llm) >= interval * 60
        if not due or not self._global_ok(now):
            return None
        if win.switches == 0 and win.frames == 0 and not forced:
            self.last_skip_reason = "nothing happened"
            return None
        busy = system_busy(self.cfg, self.gpu)
        if busy and not forced:
            self.last_skip_reason = f"busy: {busy}"
            log.info("llm skipped, machine busy (%s)", busy)
            return None
        if not self.llm.reachable():
            self.last_skip_reason = "llm unreachable"
            self._last_llm = now  # do not hammer a dead endpoint
            self.on_state("alert", f"llm-unreachable: Model server not reachable at {self.llm.base}. Suggestions run on rules only.")
            return None
        self._last_llm = now
        prompt = self._prompt(win)
        self.on_state("thinking", self.llm.model)
        try:
            text = self.llm.suggest(prompt)
        finally:
            self.on_state("idle")
        log.info("llm answered in %.1fs: %r", self.llm.last_latency, text)
        if not text:
            return None
        text = text.strip().strip('"').strip()
        if self._repeats(text):
            self.last_skip_reason = "model repeated an earlier suggestion"
            return None
        words = topic_words(text)
        if words and len(words & topic_words(prompt)) / len(words) < 0.5:
            self.last_skip_reason = f"model talked about things not in the log: {text}"     # e.g. copied a prompt example
            log.info("llm suggestion dropped, not grounded in the log: %r", text)
            return None
        if text.upper().startswith("TODO:"):
            title = text[5:].strip(" .\"'“”")
            head = re.split(r"\s*(?:[(;·,]|\bis due\b|\bdue\b|\bin \d)", title)[0]
            if head != title and self.planner is not None and self.planner.find_todo(head[:30]):
                text = title                         # a deadline nudge about a TODO that exists, not a new TODO
        if text.upper().startswith("TODO:"):           # the model proposes a task worth tracking
            why = self._weak_todo(title, win)
            if why:
                self.last_skip_reason = f"dropped TODO idea ({why}): {title}"
                log.info("llm TODO dropped (%s): %r", why, title)
                return None
            return self._deliver(Suggestion(text=f"Add a TODO: “{title}”?", source="llm",
                                            context={"action": {"type": "add_todo", "title": title}}))
        if not topic_words(text):
            return None                                  # too vague to act on
        if LEFT_RE.search(text):                         # "you left X and did not come back": only if X really went quiet
            recent = topic_words(worklog.build(self.store, now - 15 * 60, now) + f" {win.current_title or ''}")
            if words & recent - {"strux", "halo"}:
                self.last_skip_reason = f"not an open loop, still active: {text}"
                log.info("llm open-loop nudge dropped, still active: %r", text)
                return None
        return self._deliver(Suggestion(text=text, source="llm", context={"action": {"type": "discuss"}}))

    # ---- helpers -----------------------------------------------------------
    def _prompt(self, win) -> str:
        """Work-level context (what is being worked on), not process names; bounded size."""
        now = win.now
        parts = []
        cur = worklog.describe(win.current_app, win.current_title)
        if cur:
            parts.append(f"Right now: {cur[1]} (for {int(win.current_focus_seconds // 60)} min)")
        log_ = worklog.build(self.store, now - 45 * 60, now)
        parts.append("Last 45 minutes:\n" + (log_ or "nothing notable"))
        if self.planner is not None:
            parts.append(self.planner.context_text(now, ids=False))
        if win.error_frames:
            parts.append("Errors seen on screen: " + "; ".join((e["title"] or "")[:60] for e in win.error_frames[-2:]))
        recent = [r for r in self.store.recent_suggestions(3 * 24 * 60)
                  if not _is_test(r) and r["source"] != "rule:welcome_back"]     # greetings are not ideas
        turned_down = [r["text"] for r in recent if r["feedback"] == "dismiss"][:8]
        liked = [r["text"] for r in recent if r["feedback"] == "accept"][:4]
        shown = [r["text"] for r in recent if r["ts"] >= now - 4 * 3600 and r["text"] not in turned_down][:5]
        if liked:
            parts.append("Suggestions they ACCEPTED lately (this kind helps):\n" + "\n".join(f"- {t}" for t in liked))
        if turned_down:
            parts.append("Suggestions they TURNED DOWN lately (nothing like these again):\n" + "\n".join(f"- {t}" for t in turned_down))
        if shown:
            parts.append("Already shown today (never repeat or rephrase):\n" + "\n".join(f"- {t}" for t in shown))
        todo_fb = [r["feedback"] for r in recent if r["source"] == "llm" and r["text"].startswith("Add a TODO")][:3]
        if len(todo_fb) == 3 and all(f == "dismiss" for f in todo_fb):
            parts.append("They turned down your last 3 TODO ideas: do NOT propose a TODO this time.")
        return "\n\n".join(parts)

    def _repeats(self, text: str) -> bool:
        """True if the model basically repeated something suggested lately (4 h), or anything turned down in 3 days."""
        words = topic_words(text)
        if not words:
            return False
        for r in self.store.recent_suggestions(3 * 24 * 60):
            if r["feedback"] != "dismiss" and r["ts"] < time.time() - 4 * 3600:
                continue
            other = topic_words(r["text"])
            if not other:
                continue
            same = len(words & other)
            if same / len(words | other) > 0.5 or same / min(len(words), len(other)) >= 0.75:
                return True
        return False

    def _weak_todo(self, title: str, win) -> str | None:
        """Why a TODO idea from the model is not worth showing, or None if it is."""
        if not title or len(title) > 90:
            return "empty or rambling"
        if self.planner is not None and self.planner.find_todo(title[:30]):
            return "already a TODO"
        words = topic_words(title)
        if not words:
            return "no topic"
        # just restating what is on screen ('review changes in memory.py' while memory.py is open) is not news
        here = topic_words(f"{win.current_title or ''} {(worklog.describe(win.current_app, win.current_title) or ('', ''))[1]}")
        if words <= here:
            return "restates the current window"
        if self.planner is not None:
            for t in self.planner.todos():
                other = topic_words(t["title"])
                if other and len(words & other) / min(len(words), len(other)) >= 0.75:
                    return f"like TODO #{t['id']}"
        return None

    def _learned_cooldown(self, source: str, minutes: float) -> float:
        """Dismissals stretch the wait. Test suggestions ([e2e] / rule:test) never count."""
        fb = self.store.recent_feedback(source, 3)
        if len(fb) >= 3 and all(f == "dismiss" for f in fb):
            return minutes * 8
        if len(fb) >= 2 and fb[0] == fb[1] == "dismiss":
            return minutes * 4
        return minutes

    def _expire_info(self, now: float) -> None:
        """A heads-up with nothing to do ("build finished", "welcome back") leaves Activity by itself after
        `info_expire_minutes`; it is marked 'expired', which never counts as a dismissal."""
        after = float(self.cfg.get("info_expire_minutes", 15)) * 60
        for r in self.store.recent_suggestions(12 * 60, now=now):
            if r["feedback"] or now - r["ts"] < after:
                continue
            try:
                action = (json.loads(r["context"] or "{}").get("action") or {}).get("type")
            except ValueError:
                action = None
            if action in (None, "", "recap") and r["source"] != "llm" and self.store.set_feedback(r["id"], "expired"):
                self.on_expire(r["id"])

    def _resurface_snoozed(self, now: float) -> None:
        after = float(self.cfg.get("snooze_minutes", 30)) * 60
        for sid, ts in list(self.snoozed.items()):
            if now - ts < after:
                continue
            self.snoozed.pop(sid, None)
            row = self.store.suggestion(sid)
            if not row or row["feedback"] != "snooze":
                continue
            self.store.reopen_suggestion(sid)
            s = Suggestion(text=row["text"], source=row["source"], ts=row["ts"], id=sid, context=json.loads(row["context"] or "{}"))
            log.info("re-surfacing snoozed suggestion #%s", sid)
            try:
                self.notify(s)
            except Exception:
                log.exception("notify failed")

    def _maybe_hour_summary(self, win, now: float) -> None:
        if now - self._last_hour_summary < 3600:
            return
        w = summarizer.build(self.store, minutes=60, now=now)
        apps = ", ".join(f"{a} {int(s // 60)}m" for a, s in w.top_apps(4)) or "idle"
        self.store.add_summary("hour", now - 3600, now, f"{apps}; {w.switches} switches; {int(w.idle_seconds // 60)}m idle")
        self._last_hour_summary = now

    def _maybe_day_summary(self, now: float) -> None:
        """Once per day, write a one-paragraph summary of yesterday (rules only, no LLM needed)."""
        import datetime as _dt
        today = _dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        y_start = (today - _dt.timedelta(days=1)).timestamp()
        y_end = today.timestamp()
        last = self.store.summaries("day", limit=1)
        if last and last[0]["end_ts"] >= y_end - 1:
            return
        totals = summarizer.app_totals(self.store, y_start, y_end)
        if not totals:
            self.store.add_summary("day", y_start, y_end, "no activity recorded")
            return
        top = ", ".join(f"{a} {int(s // 60)}m" for a, s in totals[:6])
        active = int(sum(s for _, s in totals) // 60)
        self.store.add_summary("day", y_start, y_end, f"{active} active min; top apps: {top}")

    def _cooldown_ok(self, source: str, minutes: float, now: float) -> bool:
        return now - self.store.last_suggestion_ts(source, now=now) >= minutes * 60

    def _global_ok(self, now: float) -> bool:
        return self._cooldown_ok(None, float(self.cfg.get("global_cooldown_minutes", 10)), now)

    def _deliver(self, s: Suggestion) -> Suggestion:
        self.store.add_suggestion(s)
        log.info("suggestion #%s [%s]: %s", s.id, s.source, s.text)
        try:
            self.notify(s)
        except Exception:
            log.exception("notify failed")
        return s
