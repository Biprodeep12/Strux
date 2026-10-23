"""Chat that can act: the model manages TODOs and calendar events through tools.

Used when a chat message is about tasks / reminders / the calendar (see PLANNER_Q in chat.py).
Loop: model -> tool calls -> run them against the Planner -> results back to the model -> final
answer; at most MAX_STEPS rounds. Every tool validates its input, so a confused call returns an
error string to the model instead of writing junk.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import timedelta

from ..planner import Planner, fmt_when, parse_when

log = logging.getLogger(__name__)
MAX_STEPS = 6
MAX_CALLS = 40                                 # tool calls per message, whatever the model does
MAX_DAYS_PER_TITLE = 4
MAX_DELETES = 3                                # per message: "delete all my todos" must not wipe the list
MAX_COMPLETES = 10
SAFE_TOOLS = {"add_todo", "add_event", "list_todos", "list_events"}      # (remember is not: a file must not write your Memory)
MEMORY = None                                  # set by the app: watcher.memory.Memory   # all a file / image may lead to

WHEN_HELP = "time as 'YYYY-MM-DD HH:MM', 'HH:MM', 'tomorrow 10:00', 'today 5pm' or 'in 2 hours'"


def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS = [
    _fn("add_todo", "Add a TODO for the user.", {
        "title": {"type": "string", "description": "short task title, imperative"},
        "due": {"type": "string", "description": f"optional due {WHEN_HELP}"},
        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        "project": {"type": "string", "description": "optional project name"},
        "notes": {"type": "string"}}, ["title"]),
    _fn("complete_todo", "Mark a TODO as done.", {
        "todo": {"type": "string", "description": "the TODO's #id or words from its title"}}, ["todo"]),
    _fn("update_todo", "Change a TODO's title, due time or priority, or add a note to it.", {
        "todo": {"type": "string", "description": "#id or words from the title"},
        "title": {"type": "string"}, "due": {"type": "string", "description": WHEN_HELP + ", or 'none'"},
        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        "note": {"type": "string", "description": "text to add to the TODO's notes, in the user's words"}}, ["todo"]),
    _fn("delete_todo", "Delete a TODO (only when the user asks to remove it).", {
        "todo": {"type": "string", "description": "#id or words from the title"}}, ["todo"]),
    _fn("list_todos", "List the open TODOs.", {}, []),
    _fn("add_event", "Add a calendar event.", {
        "title": {"type": "string"},
        "start": {"type": "string", "description": f"start {WHEN_HELP}"},
        "end": {"type": "string", "description": f"optional end, {WHEN_HELP}"},
        "duration_minutes": {"type": "integer", "description": "length in minutes if the user gave one (default 60)"},
        "all_day": {"type": "boolean"},
        "location": {"type": "string"}, "notes": {"type": "string"}}, ["title", "start"]),
    _fn("move_event", "Move an existing event to a new time (or rename it). Use this, never add_event, to reschedule.", {
        "event": {"type": "string", "description": "the event's #id or words from its title"},
        "start": {"type": "string"}, "end": {"type": "string"}, "duration_minutes": {"type": "integer"},
        "title": {"type": "string"}}, ["event"]),
    _fn("delete_event", "Delete a calendar event (only when the user asks).", {
        "event": {"type": "string", "description": "the event's #id or words from its title"}}, ["event"]),
    _fn("remember", "Save a fact or preference about the user to their Memory (only when they ask you to remember it).", {
        "fact": {"type": "string", "description": "the fact in the user's words, one short line"},
        "block": {"type": "string", "description": "optional Memory block: 'About me', 'Preferences', 'Current focus' or 'Notes' (default)"}},
        ["fact"]),
    _fn("forget_memory", "Remove something from the user's Memory (only when they ask you to forget it).", {
        "text": {"type": "string", "description": "words from the line to remove"}}, ["text"]),
    _fn("list_events", "List calendar events for a day.", {
        "day": {"type": "string", "description": "'today', 'tomorrow' or 'YYYY-MM-DD'"}}, []),
]
PRIO = {"low": 0, "normal": 1, "high": 2}


DATE_ONLY = re.compile(r"^\s*\[?\s*((mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(/\d{2,4})?|\d{1,2}(st|nd|rd|th)?\s+[a-z]{3,9}\.?(\s+\d{4})?|"
                       r"[a-z]{3,9}\.?\s+\d{1,2}(st|nd|rd|th)?(,?\s+\d{4})?|today|tomorrow|(next\s+)?(mon|tue|wed|thu|fri|sat|sun)[a-z]*)"
                       r"\]?([ t]00:00(:00)?)?\s*$", re.I)
DUE_HOUR = 21                                  # a TODO due "on 24 Oct" is due that evening, not at 00:00


def parse_due(value, now: float) -> float | None:
    """Due time for a TODO: like parse_when, but a bare day (or the model's 'YYYY-MM-DD 00:00') means that evening."""
    ts = parse_when(value, now)
    if ts is not None:
        from datetime import datetime
        d, cur = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
        if d.year < cur.year:                     # the small model writes its training-era year
            try:
                d = d.replace(year=cur.year)
                if d < cur - timedelta(days=60):
                    d = d.replace(year=cur.year + 1)
                ts = d.timestamp()
            except ValueError:                    # 29 Feb
                pass
    if ts is not None and isinstance(value, str) and DATE_ONLY.match(value):
        from datetime import datetime
        ts = datetime.fromtimestamp(ts).replace(hour=DUE_HOUR, minute=0, second=0, microsecond=0).timestamp()
    return ts


def run_tool(planner: Planner, name: str, args: dict, now: float | None = None, allow_update: bool = True) -> tuple[str, dict | None]:
    """Execute one tool call. Returns (text for the model, record of what changed or None).
    `allow_update`: an add_todo for a TODO that already exists may change its due / priority (only when
    the user's message asks for a change: "retry" / "add it again" must not move existing TODOs)."""
    now = now or time.time()
    a = {k: v for k, v in (args or {}).items() if v not in (None, "")}
    try:
        if name == "add_todo":
            due = parse_due(a.get("due"), now) if a.get("due") else None
            if a.get("due") and due is None:
                return f"error: could not understand due '{a['due']}'; use {WHEN_HELP}", None
            prio = PRIO.get(str(a.get("priority", "normal")).lower(), 1)
            parts = [p.strip(" .") for p in str(a["title"]).split(";") if p.strip(" .")]
            if len(parts) > 1 and all(len(p) >= 3 for p in parts):
                # "SE revision; Java MCQs" from a plan line is two tasks: add each (same day / priority)
                outs = [run_tool(planner, "add_todo", {**a, "title": p}, now, allow_update) for p in parts[:6]]
                made_any = [c for _, c in outs if c]
                return "; ".join(t for t, _ in outs), (made_any[0] if len(made_any) == 1 else
                                                         {"kind": "multi", "changes": made_any} if made_any else None)
            t = planner.add_todo(str(a["title"]), due_ts=due, priority=prio,
                                 project=str(a.get("project", "")), notes=str(a.get("notes", "")), source="ai")
            existed = t.pop("_existing", False)
            if existed:
                # same title on the same day already open. Only an explicit change request may alter it.
                ch = {k: v for k, v in (("due_ts", due), ("priority", prio if "priority" in a else None),
                                        ("project", a.get("project") or None)) if v is not None and t.get(k) != v}
                if ch and allow_update:
                    t = planner.update_todo(t["id"], **ch)
                    return f"updated existing TODO #{t['id']} {t['title']}", {"kind": "todo_updated", "todo": t}
                return f"already on the list: #{t['id']} {t['title']}", None
            if allow_update and due is not None:
                # "move X to friday" answered with add_todo(X, friday): move the open X instead of adding a twin
                twin = next((x for x in planner.todos() if x["title"].lower() == t["title"].lower() and x["id"] != t["id"]), None)
                if twin:
                    planner.delete_todo(t["id"])
                    t = planner.update_todo(twin["id"], due_ts=due, **({"priority": prio} if "priority" in a else {}))
                    return f"updated existing TODO #{t['id']} {t['title']}", {"kind": "todo_updated", "todo": t}
            return f"added TODO #{t['id']} {t['title']}" + (f" due {fmt_when(t['due_ts'], now)}" if t["due_ts"] else ""), {"kind": "todo_added", "todo": t}
        if name in ("complete_todo", "update_todo", "delete_todo"):
            t = planner.find_todo(str(a.get("todo", "")))
            if not t:
                return f"error: no open TODO matches '{a.get('todo')}'", None
            if name == "complete_todo":
                t = planner.update_todo(t["id"], done=True)
                return f"done: #{t['id']} {t['title']}", {"kind": "todo_done", "todo": t}
            if name == "delete_todo":
                planner.delete_todo(t["id"])
                return f"deleted #{t['id']} {t['title']}", {"kind": "todo_deleted", "todo": t}
            ch: dict = {}
            if a.get("title"):
                ch["title"] = str(a["title"])
            if a.get("priority"):
                ch["priority"] = PRIO.get(str(a["priority"]).lower(), 1)
            if "due" in a:
                if str(a["due"]).lower() in ("none", "no", "clear"):
                    ch["due_ts"] = None
                else:
                    due = parse_due(a["due"], now)
                    if due is None:
                        return f"error: could not understand due '{a['due']}'", None
                    ch["due_ts"] = due
            if str(a.get("note") or "").strip():
                note = str(a["note"]).strip()
                ch["notes"] = (t["notes"].rstrip() + "\n" + note).strip() if t["notes"].strip() else note
            t = planner.update_todo(t["id"], **ch)
            return f"updated #{t['id']} {t['title']}", {"kind": "todo_updated", "todo": t}
        if name == "list_todos":
            todos = planner.todos()[:30]
            if not todos:
                return "no open TODOs", None
            return "\n".join(f"#{t['id']} {t['title']}" + (f" (due {fmt_when(t['due_ts'], now)})" if t["due_ts"] else "")
                             for t in todos), None
        if name == "add_event":
            start = parse_when(a.get("start"), now)
            if start is None:
                return f"error: could not understand start '{a.get('start')}'; use {WHEN_HELP}", None
            end = parse_when(a.get("end"), now) if a.get("end") else None
            if end is not None and end <= start:
                end = None
            if end is None and _minutes(a.get("duration_minutes")):
                end = start + _minutes(a["duration_minutes"]) * 60
            e = planner.add_event(str(a["title"]), start, end, all_day=bool(a.get("all_day")),
                                  location=str(a.get("location", "")), notes=str(a.get("notes", "")), source="ai")
            return f"added event #{e['id']} {e['title']} at {fmt_when(e['start_ts'], now)}", {"kind": "event_added", "event": e}
        if name in ("move_event", "delete_event"):
            found = planner.find_event(str(a.get("event", "")), now)
            if not found:
                return f"error: no event matches '{a.get('event')}' (see list_events)", None
            eid = str(found["id"])
            if name == "delete_event":
                return ("deleted event #" + eid, {"kind": "event_deleted", "id": int(eid)}) if planner.delete_event(int(eid)) \
                    else (f"error: no event #{eid}", None)
            ch = {}
            for k in ("start", "end"):
                if a.get(k):
                    v = parse_when(a[k], now)
                    if v is None:
                        return f"error: could not understand {k} '{a[k]}'", None
                    ch[f"{k}_ts"] = v
            old = next((e for e in planner.events_between(0, 1e12) if e["id"] == int(eid)), None)
            if not old:
                return f"error: no event #{eid}", None
            if _minutes(a.get("duration_minutes")):
                ch["end_ts"] = ch.get("start_ts", old["start_ts"]) + _minutes(a["duration_minutes"]) * 60
            elif "start_ts" in ch and "end_ts" not in ch:       # keep the duration
                ch["end_ts"] = ch["start_ts"] + (old["end_ts"] - old["start_ts"])
            if a.get("title"):
                ch["title"] = str(a["title"])
            e = planner.update_event(int(eid), **ch)
            return (f"moved event #{eid} to {fmt_when(e['start_ts'], now)}", {"kind": "event_updated", "event": e}) if e \
                else (f"error: no event #{eid}", None)
        if name in ("remember", "forget_memory"):
            if MEMORY is None:
                return "error: memory is not available", None
            if name == "remember":
                b, added = MEMORY.remember(str(a.get("fact") or ""), str(a.get("block") or "Notes"))
                return (f"remembered in {b['title']}" if added else f"already in {b['title']}"), \
                    ({"kind": "memory_added", "memory": {"id": b["id"], "title": b["title"], "fact": str(a.get('fact'))}} if added else None)
            gone = MEMORY.forget(str(a.get("text") or ""))
            return (f"forgot: {'; '.join(gone)}" if gone else "nothing like that in memory"), \
                ({"kind": "memory_removed", "memory": {"fact": "; ".join(gone)}} if gone else None)
        if name == "list_events":
            from datetime import datetime, timedelta
            day = parse_when(a.get("day") or "today", now) or now
            d0 = datetime.fromtimestamp(day).replace(hour=0, minute=0, second=0, microsecond=0)
            evs = planner.events_between(d0.timestamp(), (d0 + timedelta(days=1)).timestamp())
            return ("\n".join(f"#{e['id']} {e['title']} {fmt_when(e['start_ts'], now)}" for e in evs) or "nothing scheduled"), None
    except (KeyError, ValueError, TypeError) as e:
        return f"error: {e}", None
    return f"error: unknown tool {name}", None


STOPWORDS = {"the", "and", "for", "with", "this", "that", "these", "those", "todo", "todos", "task", "tasks", "event", "events",
             "add", "make", "create", "put", "set", "please", "my", "your", "all", "every", "everything", "list", "item",
             "items", "delete", "remove", "mark", "done", "complete", "finish", "move", "today", "tomorrow", "from", "into",
             "about", "reminder", "remind", "schedule", "calendar", "due", "high", "low", "priority", "retry", "again"}
# "the model says it did something": action verb + a planner object, or a scheduling verb (a day summary saying
# "- Added clipboard paste" describes the user's work and must not match)
CLAIM = re.compile(r"^\W*(?:i(?:'ve| have)?\s+)?(?:(?:added|updated|deleted|moved|created|completed|marked|set)\b.*"
                   r"\b(?:todos?|events?|reminders?|calendar)\b|(?:scheduled|booked|rescheduled)\b)", re.I | re.M)
INVALID_TIME = re.compile(r"\b(2[4-9]|[3-9]\d):[0-5]\d\b|\b(?:[01]?\d|2[0-3]):[6-9]\d\b")


def words(text: str) -> set[str]:
    """Meaningful words (lowercase, >= 3 letters, no filler) for grounding checks; handles 'Priya's' / 'SE:'."""
    return {w for w in re.findall(r"[a-z0-9\u00c0-\uffff]{3,}", (text or "").lower()) if w not in STOPWORDS}


SPAN_Q = re.compile(r"\bfrom\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)\s*(?:to|till|until|-|–)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)", re.I)
RENAME_Q = re.compile(r"\brename\s+(?:the\s+|my\s+)?(?P<old>.+?)(?:\s+(?:todo|to-?do|task|event|meeting))?\s+(?:to|as)\s+(?P<new>.+)$", re.I)
SHIFT_Q = re.compile(r"\b(?:push|move|postpone|delay|shift|bump|bring)\s+(?:back\s+|forward\s+)?(?:the\s+|my\s+)?(?P<item>.+?)\s+"
                     r"(?:back\s+|forward\s+|earlier\s+|later\s+)?by\s+(?P<n>a|an|one|two|three|half an?|\d+)\s*(?P<unit>min(?:ute)?s?|hours?|days?|weeks?)\b", re.I)
HINT = re.compile(r"\s*\[[^\]]*\d{4}[^\]]*\]")
TIME_TAIL = re.compile(r"\s+(?:(?:at|by|on|before)\s+)?(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2})\s*$", re.I)


def _span_clock(a: str, b: str) -> tuple:
    """'10am','6pm' -> (10,0),(18,0); '2','5pm' -> (14,0),(17,0) (the end's am/pm carries over); '9','11' -> (9,0),(11,0)."""
    def one(s):
        m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*", s or "", re.I)
        return (int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")) if m else None
    x, y = one(a), one(b)
    if not x or not y:
        return None, None
    xs, ys = x[2] or ("" if not y[2] or x[0] > y[0] % 12 and y[2] == "pm" else y[2]), y[2] or x[2]
    def to24(h, mi, ap):
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        elif not ap and h < 7:                       # "from 2 to 4": afternoon
            h += 12
        return (h, mi) if h <= 23 and mi <= 59 else None
    return to24(x[0], x[1], xs), to24(y[0], y[1], ys)


def _normalise_call(planner, call: dict, question: str, said: str) -> dict:
    """What the small model gets wrong, put right before the guards look at it:
      - Strux's date hints copied into a title ("…on friday [Fri 23 Oct 2026]"), context text ("Study Java · due …")
      - a time left in the title with no due ("Call mom at 7pm") -> title "Call mom", due 7pm
      - the TODO tool used for an event or the event tool for a TODO ("move study java" -> move_event)
      - "make study java high priority" sent as add_todo("Study Java") -> update_todo
      - remember() for "remind me to …" (that is a TODO)"""
    name, args = call["name"], dict(call.get("arguments") or {})
    if not isinstance(args, dict):
        return call
    if name == "remember" and not REMEMBER_Q.search(question) and re.search(r"\bremind me\b", question, re.I):
        name, args = "add_todo", {"title": str(args.get("fact", "")), **({"due": args["due"]} if args.get("due") else {})}
    from . import dates as _dates
    if isinstance(args.get("title"), str):
        t = HINT.sub("", args["title"]).split(" · ")[0].strip()
        if name == "add_todo" and TIME_Q.search(said):
            if not args.get("due"):                    # the model left the when in the title / dropped it: Strux reads it
                ts = _dates.when_said(said, time.time(), DUE_HOUR)
                if ts:
                    args["due"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            t = _dates.strip_when(t)                   # "submit the form tomorrow morning" -> "submit the form"
        args["title"] = t or args["title"]
    # "from 10am to 6pm": start and end exactly as said, on the day the model / the user picked
    sp = SPAN_Q.search(said)
    if sp and name in ("add_event", "move_event") and args.get("start"):
        a_, b_ = _span_clock(sp.group(1), sp.group(2))
        base = parse_when(args["start"], time.time())
        if a_ and b_ and base:
            day0 = time.localtime(base)
            s_ = time.mktime((day0.tm_year, day0.tm_mon, day0.tm_mday, a_[0], a_[1], 0, 0, 0, -1))
            e_ = time.mktime((day0.tm_year, day0.tm_mon, day0.tm_mday, b_[0], b_[1], 0, 0, 0, -1))
            if e_ > s_:
                args["start"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(s_))
                args["end"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(e_))
                args.pop("duration_minutes", None)
    # "rename the read paper todo to read the ML paper": exactly that, whatever the model passed as the reference
    m = RENAME_Q.search(question)
    if m and name in ("update_todo", "add_todo", "move_event"):
        old, new = m.group("old").strip(), m.group("new").strip(" .")
        item = planner.find_todo(old)
        if item:
            name, args = "update_todo", {"todo": str(item["id"]), "title": new}
        elif (ev := planner.find_event(old)):
            name, args = "move_event", {"event": str(ev["id"]), "title": new}
    # "push revise SE by a day": from the item's own time, not from now
    m = SHIFT_Q.search(question)
    if m and name in ("update_todo", "move_event", "add_todo", "add_event"):
        n = m.group("n").lower()
        n = 0.5 if n.startswith("half") else {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}.get(n) or int(n)
        unit = m.group("unit").lower()
        delta = n * (60 if unit.startswith("min") else 3600 if unit.startswith("hour") else 7 * 86400 if unit.startswith("week") else 86400)
        if re.search(r"\b(earlier|forward|sooner|ahead)\b", question, re.I):
            delta = -delta
        item = planner.find_todo(m.group("item"))
        ev = None if item else planner.find_event(m.group("item"))
        if item and item["due_ts"]:
            name, args = "update_todo", {"todo": str(item["id"]), "due": time.strftime("%Y-%m-%d %H:%M", time.localtime(item["due_ts"] + delta))}
        elif ev:
            name, args = "move_event", {"event": str(ev["id"]), "start": time.strftime("%Y-%m-%d %H:%M", time.localtime(ev["start_ts"] + delta)),
                                        "end": time.strftime("%Y-%m-%d %H:%M", time.localtime(ev["end_ts"] + delta))}
    for k in ("due", "start", "end"):
        if isinstance(args.get(k), str):
            args[k] = HINT.sub(lambda m_: " " + m_.group(0).strip(" []"), args[k]).strip()
    ref =str(args.get("todo") or args.get("event") or "")
    if name in ("move_event", "delete_event") and ref and not planner.find_event(ref) and planner.find_todo(ref):
        if name == "move_event":
            name, args = "update_todo", {"todo": ref, **({"due": args["start"]} if args.get("start") else {}),
                                         **({"title": args["title"]} if args.get("title") else {})}
        else:
            name, args = "delete_todo", {"todo": ref}
    elif name in ("update_todo", "delete_todo", "complete_todo") and ref and not planner.find_todo(ref) and planner.find_event(ref):
        if name == "update_todo" and args.get("due"):
            name, args = "move_event", {"event": ref, "start": args["due"]}
        elif name == "delete_todo":
            name, args = "delete_event", {"event": ref}
    if name == "add_todo" and isinstance(args.get("title"), str):
        same = planner.find_todo(args["title"])
        if same and same["title"].lower() == args["title"].lower() and (CHANGE_Q.search(question) or PRIO_Q.search(question)):
            name, args = "update_todo", {"todo": str(same["id"]), **{k: args[k] for k in ("due", "priority") if args.get(k)}}
    if (name, args) != (call["name"], call.get("arguments")):
        log.info("tool %s(%s) normalised to %s(%s)", call["name"], json.dumps(call.get("arguments"), ensure_ascii=False)[:120],
                 name, json.dumps(args, ensure_ascii=False)[:120])
    return {"name": name, "arguments": args}


def _stem(w: str) -> str:
    return w[:5] if len(w) > 5 else w


def grounded(title: str, source: str) -> bool:
    """A TODO / event title must come from the user's own words (typed or attached), not be invented.
    Word forms count ("SE revision" for "revise SE", "submitting" for "submit")."""
    tw = words(title)
    if not tw:
        return True
    sw = words(source)
    return bool(tw & sw) or bool({_stem(w) for w in tw} & {_stem(w) for w in sw}) or \
        any(a[:4] == b[:4] and min(len(a), len(b)) >= 4 for a in tw for b in sw)


def named(item_title: str, item_id: int, said: str) -> bool:
    """Was this exact item named by the user (by #id or by a word of its title)? Required to delete / finish / move."""
    if re.search(rf"#\s*{item_id}\b", said or ""):
        return True
    return bool(words(item_title) & words(said))


def _minutes(v) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 0
    return n if 0 < n <= 24 * 60 else 0


CHANGE_Q = re.compile(r"\b(change|move|reschedule|postpone|update|set|make it|instead|due|deadline|priority|earlier|later|"
                      r"push|shift|by \w+day|to (today|tomorrow|\d))\b", re.I)
# "add a note to the read paper todo: pages 1 to 12" / "note on study java: chapter 4"
NOTE_Q = re.compile(r"\bnotes?\s+(?:to|on|for|in)\s+(?:the\s+|my\s+)?(?P<todo>.+?)(?:\s+(?:todo|to-?do|task))?\s*[:\-–]\s*(?P<note>.+)$", re.I)
# the user asked to add / schedule something (an answer in plain text instead of a tool call is a miss)
ADD_Q = re.compile(r"^\W*(please\s+|can you\s+|could you\s+|um+\s+)?(add|create|make|put|schedule|book|remind me|note down|jot down|set up)\b|"
                   r"\bremind me (to|about)\b|\b(add|put|schedule|book) (it|this|that|them)\b|\b(add|put)\b.*\b(to|on|in) (my|the) (list|todos?|to-?do list|calendar)\b", re.I)
QUESTION_Q = re.compile(r"^\W*(what|which|when|where|who|how|show|list|tell me|do i|did i|is there|are there|any)\b", re.I)
REMEMBER_Q = re.compile(r"\bremember\b|\bkeep in mind\b|^\W*note that\b|\bmemori[sz]e\b", re.I)
# the user said when: a date, a time, a weekday, "tonight", "in 2 hours", "by friday", "next week", "eod"…
TIME_Q = re.compile(r"\b(\d{1,2}(:\d{2})?\s*(am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2}|at \d{1,2}|at (ten|eleven|twelve|one|two|three|four|five|six|"
                    r"seven|eight|nine)|today|tonight|tomorrow|tmrw|tmr|yesterday|morning|afternoon|evening|noon|midnight|eod|"
                    r"(mon|tues?|wed(nes)?|thu(rs)?|fri|sat(ur)?|sun)(day)?|weekend|next (week|month)|this week|in \d+|in (an?|one|two|"
                    r"three|half an?) (min|minute|hour|day|week)|\d{1,2}(st|nd|rd|th)|\d{1,2} (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|"
                    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}|deadline|due)\b", re.I)
PRIO_Q = re.compile(r"\b(high|low|top|urgent|important|asap|critical|priority|whenever|someday|no rush)\b", re.I)
# what the user's own message must say before a destructive tool may run
DELETE_Q = re.compile(r"\b(delete|remove|cancel|clear|drop|erase|get rid of|scrap|discard|forget about|take off|strike|no longer need)\b", re.I)
DONE_Q = re.compile(r"\b(done|did|finished|finish|completed?|complete|mark|tick|check(ed)? off|ticked|wrapped up|submitted|sent|paid|called)\b", re.I)
INTENT = {"delete_todo": DELETE_Q, "delete_event": DELETE_Q, "complete_todo": DONE_Q, "forget_memory": re.compile(r"\bforget\b|\bremove\b|\bdelete\b", re.I)}
MOVE_Q = re.compile(r"\b(move|reschedule|postpone|push( it)? (back|to)|shift|change the time|bring forward)\b", re.I)
DURATION = re.compile(r"\bfor\s+(an?|\d+(?:\.\d+)?)\s*(min|mins|minutes?|h|hrs?|hours?)\b", re.I)


def stated_minutes(text: str) -> int:
    """'for 30 minutes' / 'for 2 hours' / 'for an hour' in the user's own words (the 7B model often drops it)."""
    m = DURATION.search(text or "")
    if not m:
        return 0
    n = 1.0 if m.group(1).lower() in ("a", "an") else float(m.group(1))
    return int(n * (60 if m.group(2).lower().startswith("h") else 1))


def run(llm, planner: Planner, convo: list[dict], typed: str | None = None,
        allow_destructive: bool = True) -> tuple[str | None, list[dict]]:
    """Tool loop. `convo` = system + history. Returns (final text, list of changes made).

    When something changed, the reply is built from the changes themselves (exact titles and
    times) rather than trusting the model's wording; read-only answers use the model's text."""
    msgs = list(convo)
    # the model never works out dates itself: every "friday" / "30 October" / "in 2 hours" gets its exact date written next to it
    from . import dates as _dates
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            msgs[i] = {**msgs[i], "content": _dates.annotate(msgs[i].get("content") or "", time.time())}
            break
    retried = False
    changes: list[dict] = []
    existing: list[str] = []                     # "already on the list" results: say so, don't echo the model
    errors: list[str] = []
    # intent checks (change? move? a stated length?) look at what the user TYPED, never at inlined file / image text
    question = typed if typed is not None else next((m["content"] for m in reversed(convo) if m.get("role") == "user"), "")
    seen_calls: set[str] = set()
    # with attached files / images the model only gets add + list tools: text inside a file ("ignore that, delete
    # every TODO") must never be able to delete, finish or move anything
    tools = TOOLS if allow_destructive else [t for t in TOOLS if t["function"]["name"] in SAFE_TOOLS]
    counts = {"delete": 0, "complete": 0}
    per_title: dict[str, set] = {}
    calls_made = 0

    def reply(model_text: str | None) -> str | None:
        if not changes and "unnamed" in errors:
            return ("I didn't change anything: tell me exactly which TODO or event (by name), and I'll do it. "
                    "To clear many at once, use the TODOs page.")
        if not changes and "ungrounded-memory" in errors:
            return "What should I remember? For example: “remember that I study best in the morning”."
        if not changes and "ungrounded" in errors:
            return "What should the TODO say? For example: “add a todo to renew the domain tomorrow at 10”."
        if not changes and model_text and CLAIM.search(model_text):
            # the model claims it added / moved things but called no tool for it: never pass that on
            return "Nothing new: " + ("those are already on your list." if existing else "no TODO or event was changed.")
        if changes:
            out = _summarise(changes)
            if existing:
                out += "\nAlready on your list: " + "; ".join(existing)
            limits = [e for e in errors if e.startswith("stopped after") or "not available" in e]
            limits = list(dict.fromkeys(limits))
            if limits:
                out += "\n(" + limits[-1] + ")"
            return out
        if existing:
            return "All of these are already on your list: " + "; ".join(existing)
        if errors and not model_text:
            return "Couldn't do that: " + errors[-1]
        return model_text
    said = stated_minutes(question)
    # what the user actually provided: their typed words + any attached file / image text (in user messages)
    user_text = "\n".join(m.get("content") or "" for m in convo if m.get("role") == "user")
    # this message only (typed words + its attached file / image text): when / how important did the user say?
    last_user = next((m.get("content") or "" for m in reversed(convo) if m.get("role") == "user"), "")
    # what "it" / "that one" can refer to: the typed message and the assistant's previous reply
    prev_reply = next((m.get("content") or "" for m in reversed(convo[:-1]) if m.get("role") == "assistant"), "")
    referable = question + "\n" + prev_reply
    for _ in range(MAX_STEPS):
        if calls_made >= MAX_CALLS:
            break
        r = llm.chat_tools(msgs, tools)
        if r is None:
            return (reply(None) if (changes or existing) else None), changes
        if not r["tool_calls"]:
            # asked to add / schedule something, but the model only talked ("I can't remember that…"): ask it once more
            if not retried and not changes and ADD_Q.search(question) and not QUESTION_Q.search(question):
                retried = True
                log.info("model answered in text to an add request; asking it to call the tool")
                msgs.append({"role": "assistant", "content": r["content"] or ""})
                msgs.append({"role": "user", "content": "Do it now with the tools (add_todo / add_event), using my exact words "
                                                        "for the title. Don't reply in text."})
                continue
            return reply(r["content"]), changes
        msgs.append({"role": "assistant", "content": r["content"] or "", "tool_calls": r["message"].get("tool_calls", [])})
        new_in_round = 0
        for call in r["tool_calls"]:
            if calls_made >= MAX_CALLS:
                errors.append(f"stopped after {MAX_CALLS} actions in one message")
                break
            calls_made += 1
            sig = call["name"] + json.dumps(call["arguments"], sort_keys=True, ensure_ascii=False)
            if sig in seen_calls:                          # the small model re-sends whole batches
                msgs.append({"role": "tool", "content": "error: already done, do not repeat it", "name": call["name"]})
                continue
            seen_calls.add(sig)
            if call["name"] not in {t["function"]["name"] for t in tools}:
                errors.append(f"{call['name']} is not available for this request")
                msgs.append({"role": "tool", "name": call["name"],
                             "content": f"error: {call['name']} is not allowed here (only adding from attached content)"})
                continue
            kind = "delete" if call["name"].startswith("delete") else "complete" if call["name"] == "complete_todo" else None
            if kind:
                counts[kind] += 1
                limit = MAX_DELETES if kind == "delete" else MAX_COMPLETES
                if counts[kind] > limit:
                    msg = f"stopped after {limit} {kind}s: name the exact items if you want more"
                    if msg not in errors:
                        errors.append(msg)
                    msgs.append({"role": "tool", "name": call["name"],
                                 "content": f"error: limit of {limit} {kind}s per message reached; stop and tell the user"})
                    continue
            call = _normalise_call(planner, call, question, last_user)
            if call["name"] == "add_todo" and NOTE_Q.search(question):
                m = NOTE_Q.search(question)
                target = planner.find_todo(m.group("todo")) or planner.find_todo(str(call["arguments"].get("title", "")))
                if target and m.group("note").strip():
                    call = {"name": "update_todo", "arguments": {"todo": str(target["id"]), "note": m.group("note").strip()}}
                    log.info("tool add_todo -> update_todo note on #%s (the user asked for a note)", target["id"])
            # a due time / priority the user never gave is the model's invention: "add a todo to call the bank" has no due
            if call["name"] == "add_todo" and call["arguments"].get("due") and not TIME_Q.search(last_user):
                log.info("tool add_todo: dropped invented due %r", call["arguments"].get("due"))
                call["arguments"].pop("due", None)
            if call["name"] == "add_todo" and str(call["arguments"].get("priority", "")).lower() in ("high", "low") \
                    and not PRIO_Q.search(last_user):
                call["arguments"].pop("priority", None)
            if call["name"] == "add_todo":                 # ... or spreads one item over every following day
                key = str(call["arguments"].get("title", "")).strip().lower()
                days = per_title.setdefault(key, set())
                day = str(call["arguments"].get("due", ""))[:10]
                if day not in days and len(days) >= MAX_DAYS_PER_TITLE:
                    msgs.append({"role": "tool", "name": call["name"],
                                 "content": f"error: '{call['arguments'].get('title')}' is already on {len(days)} days; do not add it to more days"})
                    continue
                days.add(day)
            # Memory: only what the user said, typed by them (never text from a file / image or the model's own idea)
            if call["name"] == "remember" and not grounded(str(call["arguments"].get("fact") or ""), question):
                errors.append("ungrounded-memory")
                log.info("tool %s refused: ungrounded-memory %s", call["name"], json.dumps(call["arguments"], ensure_ascii=False)[:160])
                msgs.append({"role": "tool", "name": call["name"], "content": "error: only save facts the user typed"})
                continue
            if call["name"] == "update_todo" and str(call["arguments"].get("note") or "").strip() \
                    and not grounded(str(call["arguments"]["note"]), user_text):
                errors.append("ungrounded-note")
                log.info("tool %s refused: ungrounded-note %s", call["name"], json.dumps(call["arguments"], ensure_ascii=False)[:160])
                msgs.append({"role": "tool", "name": call["name"], "content": "error: a note must be the user's own words"})
                continue
            # an invented TODO ("add a todo" -> "Read SE chapter 5"): the title must come from the user
            if call["name"] in ("add_todo", "add_event") and not grounded(str(call["arguments"].get("title") or ""),
                                                                      user_text + "\n" + prev_reply):
                errors.append("ungrounded")
                log.info("tool %s refused: ungrounded %s", call["name"], json.dumps(call["arguments"], ensure_ascii=False)[:160])
                msgs.append({"role": "tool", "name": call["name"],
                             "content": "error: that title is not in the user's message; ask the user what it should be"})
                continue
            # ... and to have ASKED for it in this message: "make a to-do list for tomorrow" must never delete or finish
            # anything, even when the small model decides to "tidy up" items with the same names
            need = INTENT.get(call["name"])
            if need and not need.search(question):
                errors.append("no-intent")
                log.info("tool %s refused: no-intent %s", call["name"], json.dumps(call["arguments"], ensure_ascii=False)[:160])
                msgs.append({"role": "tool", "name": call["name"],
                             "content": f"error: the user did not ask to {call['name'].split('_')[0]} anything; only add or list"})
                continue
            # deleting / finishing / changing needs the user to have named that exact item ("delete all my todos" names none)
            if call["name"] in ("delete_todo", "complete_todo", "update_todo", "delete_event", "move_event"):
                ref = str(call["arguments"].get("todo") or call["arguments"].get("event") or "")
                item = planner.find_todo(ref) if call["name"].endswith("todo") else planner.find_event(ref)
                if item and not named(item["title"], item["id"], referable):
                    errors.append("unnamed")
                    log.info("tool %s refused: unnamed %s", call["name"], json.dumps(call["arguments"], ensure_ascii=False)[:160])
                    msgs.append({"role": "tool", "name": call["name"],
                                 "content": f"error: the user did not name '{item['title']}'; only change items they name"})
                    continue
            # the small model sometimes "moves" an event by adding a copy: turn that into a real move
            if call["name"] == "add_event" and MOVE_Q.search(question):
                old = planner.find_event(str(call["arguments"].get("title", "")))
                if old:
                    args = {k: v for k, v in call["arguments"].items() if k in ("start", "end", "duration_minutes")}
                    call = {"name": "move_event", "arguments": {"event": str(old["id"]), **args}}
            if (said and call["name"] in ("add_event", "move_event") and not call["arguments"].get("end")
                    and not _minutes(call["arguments"].get("duration_minutes"))):
                call["arguments"]["duration_minutes"] = said
            # the day must be the one the user said (the model counts weekdays from its training years)
            for key, parse in (("due", parse_due), ("start", parse_when)):
                if key in call["arguments"] and call["name"] in ("add_todo", "update_todo", "add_event", "move_event"):
                    old = call["arguments"][key]
                    new, note = _dates.fix(old, last_user, time.time(), parse)
                    if note:
                        log.info("tool %s: %s %s", call["name"], key, note)
                        call["arguments"][key] = new
                        if key == "start" and call["arguments"].get("end"):          # keep the length the model meant
                            a_, b_, n_ = parse_when(old, time.time()), parse_when(call["arguments"]["end"], time.time()), parse_when(new, time.time())
                            if a_ and b_ and n_ and b_ > a_:
                                call["arguments"]["end"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(n_ + (b_ - a_)))
            text, change = run_tool(planner, call["name"], call["arguments"], allow_update=bool(CHANGE_Q.search(question)))
            log.info("tool %s(%s) -> %s", call["name"], json.dumps(call["arguments"], ensure_ascii=False)[:200], text[:120])
            if change:
                changes.extend(change["changes"] if change.get("kind") == "multi" else [change])
                new_in_round += 1
            elif text.startswith("already on the list: "):
                existing.append(text[len("already on the list: "):].split(" ", 1)[-1])
            elif text.startswith("error"):
                errors.append(text[7:])
            msgs.append({"role": "tool", "content": text, "name": call["name"]})
        if not new_in_round and (changes or existing):     # a round that changed nothing: it's done (or looping)
            break
    return reply(None) or "I couldn't finish that.", changes


def _summarise(changes: list[dict], now: float | None = None) -> str:
    """One exact line per item in its final state: 'Added TODO “Renew domain” · due tomorrow 11:00 · high priority'.
    Several changes to the same TODO collapse into one line (added + updated -> 'Added' with the final values)."""
    from datetime import datetime
    now = now or time.time()
    merged: dict = {}
    for c in changes:
        item = c.get("todo") or c.get("event")
        key = (("t" if c.get("todo") else "e"), item["id"]) if item else ("x", c.get("id") or id(c))
        if key in merged:
            first = merged[key]["kind"]
            merged[key] = {**c, "kind": first if first.endswith("added") and not c["kind"].endswith("deleted") else c["kind"]}
        else:
            merged[key] = c
    changes = list(merged.values())
    if sum(1 for c in changes if c["kind"] == "todo_added") > 6:   # long plans: group by day instead of 20 lines
        by_day: dict = {}
        for c in changes:
            if c["kind"] == "todo_added":
                t = c["todo"]
                by_day.setdefault(fmt_when(t["due_ts"], now).rsplit(" ", 1)[0] if t["due_ts"] else "no date", []).append(t["title"])
        head = f"Added {sum(len(v) for v in by_day.values())} TODOs:"
        rest = [c for c in changes if c["kind"] != "todo_added"]
        return "\n".join([head] + [f"• {d}: " + "; ".join(v) for d, v in by_day.items()]
                         + ([_summarise(rest, now)] if rest else []))
    out = []
    for c in changes:
        k = c["kind"]
        if k.startswith("todo"):
            t = c["todo"]
            verb = {"todo_added": "Added TODO", "todo_done": "Done", "todo_updated": "Updated TODO", "todo_deleted": "Deleted TODO"}[k]
            bits = [f"{verb} “{t['title']}”"]
            if k in ("todo_added", "todo_updated"):
                if t.get("due_ts"):
                    bits.append("due " + fmt_when(t["due_ts"], now))
                if t.get("priority") == 2:
                    bits.append("high priority")
            out.append(" · ".join(bits))
        elif k in ("event_added", "event_updated"):
            e = c["event"]
            if e["all_day"]:
                span = "all day " + fmt_when(e["start_ts"], now).split(" ")[0]
            else:
                span = f"{fmt_when(e['start_ts'], now)}–{datetime.fromtimestamp(e['end_ts']).strftime('%H:%M')}"
            out.append(f"{'Added' if k == 'event_added' else 'Moved'} “{e['title']}” · {span}")
        elif k == "event_deleted":
            out.append(f"Deleted event #{c['id']}")
        elif k == "memory_added":
            out.append(f"Remembered in {c['memory']['title']}: {c['memory']['fact']}")
        elif k == "memory_removed":
            out.append(f"Forgot: {c['memory']['fact']}")
    return "\n".join(out)
