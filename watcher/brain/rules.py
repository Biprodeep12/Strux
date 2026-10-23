"""Cheap, deterministic rules. They run every brain tick (default 30 s) and
cost nothing. A rule returns a Suggestion or None. Cooldowns are enforced by
the scheduler using the suggestions table, so rules stay stateless.

Each suggestion carries an `action` (see brain/actions.py) that runs when you accept it,
so a suggestion is an offer Strux can actually carry out, not just a remark.

`urgent` rules (errors, finished jobs, due TODOs, events about to start) skip the global
cooldown and still fire during a focus timer.

Rules read the planner (TODOs / calendar) and the process sensor through `win.planner` and
`win.procs`, which the brain attaches to the activity window before evaluating.

Adding a rule: subclass Rule, set `name`, implement `evaluate`, add it to
ALL_RULES. Enable/disable + cooldown live in config.yaml under brain.rules.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from ..events import Suggestion
from .summarizer import ActivityWindow, focus_streak_seconds

SELF_APPS = {"pythonw.exe", "python.exe", "strux.exe", "halo.exe"}
BROWSERS = {"brave.exe", "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "vivaldi.exe", "arc.exe"}
FUN_APPS = {"discord.exe", "steam.exe", "epicgameslauncher.exe", "netflix.exe", "vlc.exe", "spotify.exe"}
FUN_TITLES = re.compile(r"(- YouTube|YouTube$|Netflix|Prime Video|Hotstar|Twitch|Reddit|Instagram|Facebook|TikTok|"
                        r"/ X$| on X:|9GAG|Crunchyroll|Disney\+)", re.I)


def label(app: str) -> str:
    from ..overlay import app_label
    return app_label(app)


def is_self(app: str, title: str) -> bool:
    return (app or "").lower() in SELF_APPS or (title or "").lower().startswith(("strux", "halo"))   # halo: the old name, still in past events


def recently_said(store, needle: str, minutes: float = 240) -> bool:
    """Was something containing `needle` suggested in the last `minutes`? (per-item dedupe)"""
    n = needle.lower()
    return any(n in (r["text"] or "").lower() for r in store.recent_suggestions(minutes))


def already_suggested(store, key: str, value, minutes: float) -> bool:
    """Was a suggestion about this exact item (context[key] == value, e.g. event_id) made recently?"""
    import json
    for r in store.recent_suggestions(minutes):
        try:
            if json.loads(r["context"] or "{}").get(key) == value:
                return True
        except ValueError:
            continue
    return False


def hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def mins(secs: float) -> str:
    m = max(1, int(round(secs / 60)))
    if m >= 24 * 60:
        d, h = divmod(m // 60, 24)
        return f"{d} d {h} h" if h else f"{d} d"
    return f"{m} min" if m < 60 else (f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h")


def midnight(now: float) -> float:
    return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Rule:
    name = "rule"
    urgent = False          # urgent rules skip the global cooldown and fire during a focus timer
    learns = True           # dismissals stretch the cooldown; off for per-item reminders (a dismissed
                            # reminder for one TODO must not silence the reminder for the next one)

    def evaluate(self, win: ActivityWindow, store, cfg: dict) -> Suggestion | None:  # pragma: no cover
        raise NotImplementedError

    def make(self, text: str, action: dict | None = None, **ctx) -> Suggestion:
        if action:
            ctx["action"] = action
        return Suggestion(text=text, source=f"rule:{self.name}", context=ctx)


# ---- activity rules ---------------------------------------------------------------------------
class AppSwitchStorm(Rule):
    name = "app_switch_storm"

    def evaluate(self, win, store, cfg):
        if win.switches >= int(cfg.get("min_switches", 25)):
            on = label(win.current_app) if win.current_app and not is_self(win.current_app, win.current_title) else "one thing"
            return self.make(f"{win.switches} switches in {int(win.minutes)} min. Start a 25-min focus block on {on}?",
                             action={"type": "focus", "minutes": 25}, switches=win.switches)
        return None


class LongFocus(Rule):
    name = "long_focus"

    def evaluate(self, win, store, cfg):
        app, secs = focus_streak_seconds(store, now=win.now)
        limit = int(cfg.get("minutes", 50)) * 60
        if app and secs >= limit and not is_self(app, ""):
            return self.make(f"{int(secs // 60)} min straight in {label(app)}. Take a 5-min break?",
                             action={"type": "break", "minutes": 5}, app=app, seconds=int(secs))
        return None


class ErrorOnScreen(Rule):
    name = "error_on_screen"
    urgent = True

    def evaluate(self, win, store, cfg):
        errs = [e for e in win.error_frames if not is_self(e["app"], e["title"]) and win.now - e["ts"] <= 120]
        if not errs:
            return None
        e = errs[-1]
        where = label(e["app"]) if e["app"] else "on screen"
        if e.get("line"):
            text = f"Error in {where}: “{e['line'][:70]}”. Want an explanation?"
        else:
            text = f"Error in {where}" + (f" ({e['title'][:50]})" if e["title"] else "") + ". Want an explanation?"
        return self.make(text,
                         action={"type": "explain_error", "frame_id": e.get("frame_id"), "label": where}, **e)


class ClipboardRepeat(Rule):
    name = "clipboard_repeat"

    def evaluate(self, win, store, cfg):
        top = win.clipboard_hashes.most_common(1)
        if top and top[0][1] >= int(cfg.get("min_same", 4)):
            return self.make(f"You've copied the same thing {top[0][1]} times. Worth saving it as a snippet?",
                             action={"type": "discuss"}, copies=win.clipboard_count)
        if win.clipboard_count >= int(cfg.get("min_copies", 8)):
            return self.make(f"{win.clipboard_count} copies in {int(win.minutes)} min. Moving data by hand? A script could do it.",
                             action={"type": "discuss"}, copies=win.clipboard_count)
        return None


class WelcomeBack(Rule):
    """Kept for reference/tests: the live welcome is event-driven (app.welcome_back, brain/welcome.py)."""
    name = "welcome_back"

    def evaluate(self, win, store, cfg):
        li = win.last_idle_end
        if not li or win.now - li["ts"] > 90:
            return None
        idle = li["idle_seconds"]
        if idle < int(cfg.get("min_idle_minutes", 20)) * 60:
            return None
        left = li["ts"] - idle
        return self.make(f"Welcome back ({_ago(idle)} away). Want a recap of where you left off?",
                         action={"type": "recap", "start": left - 90 * 60, "end": left}, idle=idle)


class JobDone(Rule):
    name = "job_done"
    urgent = True

    def evaluate(self, win, store, cfg):
        from ..sensors.process import is_service
        # only jobs you would want to hear about: yours (not Claude Code's tool calls), not daemons, and long
        # enough that you probably switched away (a 1-min build you are watching is not news)
        min_s = float(cfg.get("min_minutes", 3)) * 60
        jobs = [j for j in win.jobs_done if not is_service(j.get("label")) and not j.get("agent") and j["runtime_s"] >= min_s]
        if not jobs:
            return None
        j = jobs[-1]
        if win.now - j["ts"] > 90:
            return None
        if win.current_app and win.current_app.lower() in ("windowsterminal.exe", "code.exe") and j["runtime_s"] < 120:
            return None  # you are probably looking at it already
        what = f"“{j['label']}”" if j.get("label") else label(j["app"])
        lbl = (j.get("label") or "").lower()
        if j["runtime_s"] >= 3 * 3600 or any(k in lbl for k in ("dev server", " serve", "watch", "start")):
            return self.make(f"{what} stopped after running for {mins(j['runtime_s'])}. Expected?", action={"type": "discuss"}, **j)
        return self.make(f"{what} finished in the background after {mins(j['runtime_s'])}.", **j)


class BusyProcess(Rule):
    """Something has been eating CPU for a long time (a forgotten dev server, a stuck build)."""
    name = "busy_process"

    def evaluate(self, win, store, cfg):
        procs = getattr(win, "procs", None)
        if procs is None:
            return None
        for p in procs.long_busy(float(cfg.get("minutes", 30)), float(cfg.get("min_cpu", 10))):
            what = p["label"] or label(p["name"])
            if recently_said(store, what, 6 * 60):
                continue
            return self.make(f"{what} has used ~{p['cpu']:.0f}% CPU for {mins(p['seconds'])}. Still need it running?",
                             action={"type": "discuss"}, pid=p["pid"], name=p["name"])
        return None


class RevisitPage(Rule):
    """You keep coming back to the same page today: probably an open loop worth tracking."""
    name = "revisit_page"

    def evaluate(self, win, store, cfg):
        from . import summarizer, worklog
        sessions, _ = summarizer.sessions(store, midnight(win.now), win.now)
        visits: Counter = Counter()
        for s in sessions:
            if (s["app"] or "").lower() in BROWSERS and s["seconds"] >= 20:
                t = worklog._clean(s["title"])
                if t and len(t) > 6 and not FUN_TITLES.search(s["title"] or "") and t.lower() not in ("new tab", "start page"):
                    visits[t] += 1
        for title, n in visits.most_common(3):
            if n < int(cfg.get("min_visits", 4)) or recently_said(store, title[:40], 24 * 60):
                continue
            planner = getattr(win, "planner", None)
            if planner and planner.find_todo(title[:30]):
                continue
            short = title if len(title) <= 60 else title[:57] + "…"
            return self.make(f"You've come back to “{short}” {n} times today. Add it as a TODO?",
                             action={"type": "add_todo", "title": f"Follow up: {short}"}, visits=n)
        return None


class Distraction(Rule):
    """A long stretch on videos / social / chat while there is open work."""
    name = "distraction"

    def evaluate(self, win, store, cfg):
        from . import summarizer
        window = float(cfg.get("window_minutes", 45)) * 60
        sessions, _ = summarizer.sessions(store, win.now - window, win.now)
        fun: Counter = Counter()
        for s in sessions:
            a = (s["app"] or "").lower()
            if a in FUN_APPS and a != "spotify.exe":
                fun[label(a)] += s["seconds"]
            elif a in BROWSERS and FUN_TITLES.search(s["title"] or ""):
                m = FUN_TITLES.search(s["title"])
                fun[m.group(0).strip(" -/:").replace(" on X", "X") or "videos"] += s["seconds"]
        total = sum(fun.values())
        if total < float(cfg.get("minutes", 25)) * 60:
            return None
        planner = getattr(win, "planner", None)
        top = planner.todos()[:1] if planner else []
        where = " and ".join(k for k, _ in fun.most_common(2))
        back = f"Back to “{top[0]['title']}”?" if top else "Back to work?"
        return self.make(f"{mins(total)} on {where} in the last {int(window // 60)} min. {back} I'll start a 25-min focus block.",
                         action={"type": "focus", "minutes": 25}, seconds=int(total))


# ---- planner rules ----------------------------------------------------------------------------
class TodoDue(Rule):
    name = "todo_due"
    urgent = True
    learns = False

    def evaluate(self, win, store, cfg):
        planner = getattr(win, "planner", None)
        if planner is None:
            return None
        lead = float(cfg.get("lead_minutes", 30)) * 60
        for t in planner.todos():
            due = t["due_ts"]
            if due is None or due - win.now > lead:
                continue        # sorted by due: the rest are later or undated
            key = f"{t['id']}:{int(t['created_ts'])}"          # id + creation time: unique for good
            late = win.now - due
            # before it is due: once per 3 h; just missed: once more; long overdue: once a day
            if already_suggested(store, "todo_key", key, 180 if late < 3600 else 12 * 60 if late < 86400 else 24 * 60):
                continue
            if due >= win.now:
                text = f"“{t['title']}” is due in {mins(due - win.now)} ({hm(due)})."
            elif late < 86400:
                text = f"“{t['title']}” was due at {hm(due)}. Done yet?"
            else:
                text = f"“{t['title']}” has been overdue since {datetime.fromtimestamp(due):%a %d %b}. Done, or move it?"
            return self.make(text, action={"type": "complete_todo", "id": t["id"]}, todo_id=t["id"], todo_key=key)
        return None


class EventSoon(Rule):
    name = "event_soon"
    urgent = True
    learns = False

    def evaluate(self, win, store, cfg):
        planner = getattr(win, "planner", None)
        if planner is None:
            return None
        lead = float(cfg.get("lead_minutes", 15)) * 60
        for e in planner.events_between(win.now - 120, win.now + lead):
            key = f"{e['id']}:{int(e['created_ts'])}"
            if e["all_day"] or e["start_ts"] < win.now - 120 or already_suggested(store, "event_key", key, 120):
                continue
            when = f"starts in {mins(e['start_ts'] - win.now)}" if e["start_ts"] > win.now else "is starting now"
            where = f" · {e['location']}" if e["location"] else ""
            return self.make(f"“{e['title']}” {when} ({hm(e['start_ts'])}{where}). Want a quick prep?",
                             action={"type": "prep_event", "id": e["id"]}, event_id=e["id"], event_key=key)
        return None


class MorningPlan(Rule):
    name = "morning_plan"

    def evaluate(self, win, store, cfg):
        planner = getattr(win, "planner", None)
        h = datetime.fromtimestamp(win.now).hour
        if planner is None or not (int(cfg.get("from_hour", 5)) <= h < int(cfg.get("until_hour", 13))):
            return None
        if store.last_suggestion_ts(f"rule:{self.name}", now=win.now) >= midnight(win.now):
            return None                                     # once a day
        todos = planner.todos()
        d0 = midnight(win.now)
        events = [e for e in planner.events_between(win.now, d0 + 86400) if not e["all_day"]]
        if not todos and not events:
            return None
        overdue = sum(1 for t in todos if t["due_ts"] and t["due_ts"] < win.now)
        name = getattr(win, "user", "")
        bits = [f"{len(todos)} open TODO{'s' if len(todos) != 1 else ''}" + (f" ({overdue} overdue)" if overdue else "")] if todos else []
        if events:
            bits.append(f"{len(events)} event{'s' if len(events) != 1 else ''} today, first at {hm(events[0]['start_ts'])}")
        return self.make(f"Morning{', ' + name if name else ''}! {' and '.join(bits)}. Plan the day?", action={"type": "plan"})


class WrapUp(Rule):
    name = "wrap_up"

    def evaluate(self, win, store, cfg):
        h = datetime.fromtimestamp(win.now).hour
        if h < int(cfg.get("hour", 21)) and h >= 4:
            return None
        if store.last_suggestion_ts(f"rule:{self.name}", now=win.now) >= win.now - 12 * 3600:
            return None
        planner = getattr(win, "planner", None)
        left = len(planner.todos()) if planner else 0
        tail = f" {left} TODO{'s are' if left != 1 else ' is'} still open." if left else ""
        return self.make(f"It's {hm(win.now)}. Wrap up for today? I'll recap what you did and what's next.{tail}",
                         action={"type": "digest"})


def _ago(secs: float) -> str:
    m = int(secs // 60)
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h"


# WelcomeBack is not ticked: the app greets you on the idle_end event itself (app.welcome_back, brain/welcome.py)
ALL_RULES: list[Rule] = [ErrorOnScreen(), EventSoon(), TodoDue(), JobDone(), MorningPlan(), Distraction(), BusyProcess(),
                         AppSwitchStorm(), LongFocus(), RevisitPage(), ClipboardRepeat(), WrapUp()]
