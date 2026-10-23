"""Tool-calling eval: real chat + real model, a fresh throwaway planner per case (never the real one).
Each case checks the exact end state: what was added / changed / deleted, titles, due times, priority.
Usage: eval_tools.py [case-number ...]"""
import json
import logging
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, r"C:\Users\bipbo\vscodeFiles\watcher-ai")
logging.basicConfig(level=logging.WARNING)
logging.getLogger("watcher.brain.agent").setLevel(logging.INFO)

from watcher.brain import chat  # noqa: E402
from watcher.brain.llm import LLM  # noqa: E402
from watcher.config import load  # noqa: E402
from watcher.planner import Planner  # noqa: E402

NOW = datetime.now()
D0 = NOW.replace(hour=0, minute=0, second=0, microsecond=0)


def day(n):                     # midnight n days from today
    return D0 + timedelta(days=n)


def weekday(name):              # next occurrence (today excluded)
    wd = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(name)
    n = (wd - NOW.weekday()) % 7 or 7
    return day(n)


def at(d, h, m=0):
    return (d + timedelta(hours=h, minutes=m)).timestamp()


TOMORROW, TODAY = day(1), D0


# ---- expectations -----------------------------------------------------------------------------------
def todos_added(n=None, words=(), due=None, due_day=None, prio=None, min_hour=None):
    def check(st):
        new = st["new_todos"]
        if n is not None and len(new) != n:
            return f"expected {n} new TODO(s), got {len(new)}: {[t['title'] for t in new]}"
        if not new:
            return "no TODO added"
        for w in words:
            if not any(w.lower() in t["title"].lower() for t in new):
                return f"no new TODO mentions '{w}': {[t['title'] for t in new]}"
        for t in new:
            if due is not None and (t["due_ts"] is None or abs(t["due_ts"] - due) > 60):
                return f"'{t['title']}' due {fmt(t['due_ts'])}, expected {fmt(due)}"
            if due_day is not None and (t["due_ts"] is None or datetime.fromtimestamp(t["due_ts"]).date() != due_day.date()):
                return f"'{t['title']}' due {fmt(t['due_ts'])}, expected on {due_day:%a %d %b}"
            if min_hour is not None and (t["due_ts"] is None or datetime.fromtimestamp(t["due_ts"]).hour < min_hour):
                return f"'{t['title']}' due {fmt(t['due_ts'])}, expected {min_hour}:00 or later"
            if prio is not None and t["priority"] != prio:
                return f"'{t['title']}' priority {t['priority']}, expected {prio}"
        return None
    return check


def no_due(st):
    bad = [t["title"] for t in st["new_todos"] if t["due_ts"]]
    return f"unexpected due time on {bad}" if bad else None


def event_added(start, words=(), end=None, on_day=None):
    def check(st):
        new = st["new_events"]
        if len(new) != 1:
            return f"expected 1 new event, got {len(new)}: {[(e['title'], fmt(e['start_ts'])) for e in new]}"
        e = new[0]
        for w in words:
            if w.lower() not in e["title"].lower():
                return f"event '{e['title']}' lacks '{w}'"
        if start is not None and abs(e["start_ts"] - start) > 60:
            return f"event starts {fmt(e['start_ts'])}, expected {fmt(start)}"
        if on_day is not None and datetime.fromtimestamp(e["start_ts"]).date() != on_day.date():
            return f"event on {fmt(e['start_ts'])}, expected {on_day:%a %d %b}"
        if end is not None and abs(e["end_ts"] - end) > 60:
            return f"event ends {fmt(e['end_ts'])}, expected {fmt(end)}"
        return None
    return check


def todo_state(title, **want):
    def check(st):
        t = next((x for x in st["after_todos"] if x["title"].lower() == title.lower()), None)
        if "deleted" in want:
            return None if t is None else f"'{title}' was not deleted"
        if t is None:
            return f"'{title}' is gone (deleted!)"
        if want.get("done") and not t["done_ts"]:
            return f"'{title}' not marked done"
        if "due" in want and (t["due_ts"] is None or abs(t["due_ts"] - want["due"]) > 60):
            return f"'{title}' due {fmt(t['due_ts'])}, expected {fmt(want['due'])}"
        if "prio" in want and t["priority"] != want["prio"]:
            return f"'{title}' priority {t['priority']}, expected {want['prio']}"
        if "notes" in want and want["notes"] not in t["notes"]:
            return f"'{title}' notes {t['notes']!r} lack {want['notes']!r}"
        return None
    return check


def event_state(title, start=None, deleted=False):
    def check(st):
        e = next((x for x in st["after_events"] if x["title"].lower() == title.lower()), None)
        if deleted:
            return None if e is None else f"event '{title}' not deleted"
        if e is None:
            return f"event '{title}' is gone"
        if start is not None and abs(e["start_ts"] - start) > 60:
            return f"'{title}' starts {fmt(e['start_ts'])}, expected {fmt(start)}"
        return None
    return check


def nothing_changed(st):
    if st["new_todos"] or st["new_events"] or st["gone"] or st["done"]:
        return f"changed things: new={[t['title'] for t in st['new_todos'] + st['new_events']]} gone={st['gone']} done={st['done']}"
    return None


def nothing_lost(st):
    return f"deleted / finished without being asked: gone={st['gone']} done={st['done']}" if (st["gone"] or st["done"]) else None


def only_one_item(st):
    return None if len(st["new_todos"]) + len(st["new_events"]) <= 1 else \
        f"made more than one item: {[t['title'] for t in st['new_todos'] + st['new_events']]}"


def reply_mentions(*words):
    def check(st):
        miss = [w for w in words if w.lower() not in st["reply"].lower()]
        return f"reply doesn't mention {miss}: {st['reply'][:160]!r}" if miss else None
    return check


def fmt(ts):
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else "none"


SEED_TODOS = [("Pay rent", {}), ("Call the bank", {}), ("Buy milk", {}), ("Study Java", {"due_ts": at(TODAY, 20)}),
              ("Read paper", {}), ("Revise SE", {"due_ts": at(TOMORROW, 18)})]
SEED_EVENTS = [("Team sync", at(TOMORROW, 10), at(TOMORROW, 10, 30)), ("Dentist", at(day(2), 16), at(day(2), 17))]

CASES = [
    # ---- adding TODOs ------------------------------------------------------------------------------
    ("add a todo to study Java tomorrow at 10am", False, [todos_added(1, ["java"], due=at(TOMORROW, 10))]),
    ("add a to do. revise SE chapter 3 tonight", False, [todos_added(1, ["SE"], due_day=TODAY, min_hour=18)]),
    ("add the to do call the bank", False, [todos_added(1, ["bank"]), no_due]),
    ("can you add a to-do for submitting the OS lab report on friday", False, [todos_added(1, ["lab report"], due_day=weekday("fri"))]),
    ("create a to do list for tomorrow: study java, revise SE and submit the lab report", True,
     [nothing_lost, todos_added(None, ["lab report"], due_day=TOMORROW)]),
    ("add study java to my to-do list", False, [todos_added(1, ["java"])]),
    ("um add a todo like buy milk", False, [todos_added(1, ["milk"])]),
    ("remind me to call mom at 7pm", False, [todos_added(1, ["mom"], due=at(TODAY if NOW.hour < 19 else TOMORROW, 19))]),
    ("I need to study Java tomorrow, add it", False, [todos_added(1, ["java"], due_day=TOMORROW)]),
    ("add a high priority todo to pay rent by friday", False, [todos_added(1, ["rent"], due_day=weekday("fri"), prio=2)]),
    ("add todos: buy milk, call the bank, renew the domain", False, [todos_added(3, ["milk", "bank", "domain"]), no_due]),
    ("Add a TODO to submit the operating systems lab report tomorrow at 5pm, high priority.", False,
     [todos_added(1, ["lab report"], due=at(TOMORROW, 17), prio=2)]),
    ("put finish the DBMS assignment on my list for monday", False, [todos_added(1, ["DBMS"], due_day=weekday("mon"))]),
    ("add a todo to email the professor in 2 hours", False,
     [todos_added(1, ["professor"]), lambda st: None if st["new_todos"] and abs(st["new_todos"][0]["due_ts"] - (st["t0"] + 7200)) < 180
      else f"due {fmt(st['new_todos'][0]['due_ts']) if st['new_todos'] else None}, expected ~{fmt(st['t0'] + 7200)}"]),
    # ---- calendar ------------------------------------------------------------------------------------
    ("schedule a meet tomorrow at 3pm", False, [event_added(at(TOMORROW, 15)), only_one_item]),
    ("I want to schedule a meet with Rahul tomorrow at 3", False, [event_added(at(TOMORROW, 15), ["Rahul"]), only_one_item]),
    ("book a call with the client on friday at 11am for 30 minutes", False,
     [event_added(at(weekday("fri"), 11), ["client"], end=at(weekday("fri"), 11, 30))]),
    ("add a meeting to my calendar next monday 10am", False, [event_added(at(weekday("mon"), 10))]),
    ("add my DBMS exam on 30 October at 10am to the calendar", False, [event_added(at(datetime(NOW.year, 10, 30), 10), ["DBMS"])]),
    # ---- changing what exists --------------------------------------------------------------------------
    ("mark pay rent as done", True, [todo_state("Pay rent", done=True)]),
    ("I called the bank, tick it off", True, [todo_state("Call the bank", done=True)]),
    ("delete the buy milk todo", True, [todo_state("Buy milk", deleted=True), todo_state("Pay rent")]),
    ("move study java to tomorrow 9am", True, [todo_state("Study Java", due=at(TOMORROW, 9))]),
    ("make study java high priority", True, [todo_state("Study Java", prio=2)]),
    ("move the team sync to 11", True, [event_state("Team sync", start=at(TOMORROW, 11))]),
    ("cancel the dentist appointment", True, [event_state("Dentist", deleted=True), event_state("Team sync")]),
    ("add a note to the read paper todo: pages 1 to 12", True, [todo_state("Read paper", notes="1"), nothing_lost]),
    # ---- read-only and risky ----------------------------------------------------------------------------
    ("what's on my todo list", True, [nothing_changed, reply_mentions("rent", "milk")]),
    ("what's on my calendar tomorrow", True, [nothing_changed, reply_mentions("team sync")]),
    ("delete all my todos", True, [nothing_changed]),
    ("create a to-do list for tomorrow", True, [nothing_lost]),
    ("make a to do list for tomorrow with study java and revise SE", True, [nothing_lost]),
    # ---- spoken style (what Whisper writes) ------------------------------------------------------------
    ("Add a to do to call the bank tomorrow at ten.", False, [todos_added(1, ["bank"], due=at(TOMORROW, 10))]),
    ("Add a to-do, revise SE.", False, [todos_added(1, ["SE"])]),
    ("Hello add a todo to study Java", False, [todos_added(1, ["java"])]),
]


def run(i, said, seed, checks, llm):
    tmp = tempfile.mkdtemp(prefix="strux-eval-")
    p = Planner(tmp)
    if seed:
        for title, kw in SEED_TODOS:
            p.add_todo(title, **kw)
        for title, s, e in SEED_EVENTS:
            p.add_event(title, s, e)
    before_t = {t["id"]: t for t in p.todos(include_done=True)}
    before_e = {e["id"]: e for e in p.events_between(0, 4102444800)}
    changes = []
    t0 = time.time()
    try:
        reply = chat.answer(llm, None, [{"role": "user", "content": said}], planner=p, changes=changes) or ""
    except Exception as e:
        reply = f"EXCEPTION {e!r}"
    after_t = p.todos(include_done=True)
    after_e = p.events_between(0, 4102444800)
    st = {"t0": t0, "reply": reply, "after_todos": after_t, "after_events": after_e,
          "new_todos": [t for t in after_t if t["id"] not in before_t],
          "new_events": [e for e in after_e if e["id"] not in before_e],
          "gone": [before_t[k]["title"] for k in before_t if k not in {t["id"] for t in after_t}] +
                  [before_e[k]["title"] for k in before_e if k not in {e["id"] for e in after_e}],
          "done": [t["title"] for t in after_t if t["done_ts"] and not before_t.get(t["id"], {}).get("done_ts") and t["id"] in before_t]}
    fails = [m for m in (c(st) for c in checks) if m]
    p.close()
    status = "PASS" if not fails else "FAIL"
    print(f"{status} {i:2d} ({time.time() - t0:4.1f}s) {said}")
    if fails:
        for f in fails:
            print(f"        - {f}")
        print(f"        reply: {reply.strip()[:220]!r}")
    sys.stdout.flush()
    return not fails


if __name__ == "__main__":
    llm = LLM(load()["brain"]["llm"])
    pick = [int(a) for a in sys.argv[1:]] or list(range(1, len(CASES) + 1))
    ok = sum(run(i, *CASES[i - 1], llm) for i in pick)
    print(f"\n{ok}/{len(pick)} passed")
