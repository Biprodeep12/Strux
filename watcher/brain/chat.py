"""'Ask about my day': answer questions over the local activity log, TODOs and calendar.

The model gets a *work log* (watcher/brain/worklog.py) for the period the question is
about: cleaned window/tab titles with time, websites, what the user asked Claude Code and
which files it changed, finished background jobs; plus open TODOs and the next days of the
calendar. Raw counters and process names are deliberately not given, so answers describe
the work instead of listing .exe files. Bounded size regardless of history.

Routing (`answer`):
  "what's on my screen" / "explain this error" -> brain/screen.py: a fresh capture, OCR + vision, then the 'screen' prompt
  tasks / reminders / calendar  -> agent.run with tools (the model can add / finish / move things)
  "plan my day / what next"     -> the 'plan' prompt
  "what's running / Claude now" -> the 'live' prompt with the live process + Claude state
  anything else                 -> the 'chat' prompt over the work log
All prompt texts come from brain/prompts.py (editable in Settings).

Learning (watcher/knowledge.py): the work-log answers also get related notes from past Claude Code
sessions, chats and screen looks, and liked / disliked answers to similar questions steer the reply.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from . import prompts, summarizer, worklog

STOP = {"what", "did", "do", "does", "done", "the", "and", "for", "you", "my", "me", "was", "were", "have", "has", "had", "this",
        "that", "today", "yesterday", "week", "hour", "hours", "last", "far", "summarise", "summarize", "summary", "work", "worked",
        "working", "on", "how", "much", "time", "spend", "spent", "long", "morning", "evening", "is", "are", "running",
        "background", "claude", "doing", "with", "about", "any", "there", "show", "tell", "give", "list", "all", "so",
        "todo", "todos", "task", "tasks", "calendar", "plan", "should", "next", "can", "please"}

EMPTY_LOG = ("No work activity recorded in this period.", "Nothing notable recorded in this period.")


def empty_answer(label: str, planner=None) -> str:
    """Said without the model when there is nothing in the log: a 7B model asked about an empty
    log invents work ('finalize the project proposal…'), so it never gets the chance."""
    out = f"Nothing was recorded {label}: you were away or idle."
    if planner is not None:
        todos = planner.todos()[:3]
        if todos:
            out += "\nPick up: " + "; ".join(t["title"] for t in todos) + "."
    return out


HEDGES = re.compile(r"\b(likely|possibly|probably|it seems that|it appears that)\s+", re.I)
LIVE_Q = re.compile(r"\b(running|background|process(es)?|cpu|ram|memory|busy|right now|claude (is )?doing|what is claude|what's claude)\b", re.I)
# anything about the TODO list or the calendar goes to the tool-calling model: a miss means "I can't do that",
# a false hit only means the tool model answers instead (it can reply without calling a tool)
PLANNER_NOUN = (r"todo|to-do|to do list|to-do list|(a|the|my|new|this|that|another) to do|task|reminder|event|meeting|meet|meetup|"
                r"calendar|schedule|appointment|deadline|exam|class|lecture|session")
PLANNER_Q = re.compile(
    r"\b(add|create|make|put|schedule|remind|book|set up|mark|finish(ed)?|complete(d)?|done with|tick|delete|remove|cancel|move|"
    rf"reschedule|postpone|push|rename|change|note|list|plan|clear)\b.*\b({PLANNER_NOUN}|call)s?\b|"
    r"\b(todo|to-do|to do list|(a|the|my|new|this|that|another) to do|reminder|calendar|meeting|meet|appointment|deadline)s?\b|"
    r"\b(add|put|save|note|schedule|book) (it|this|that|them|these|those)\b|"
    r"\bremind me\b|\bwhat('s| is) on my (calendar|schedule|list)\b|\b(my|open) (todos|tasks|to-dos)\b|"
    r"\bremember (that|my|i|i'm|this|me)\b|\bdo ?n'?t (let me )?forget (to|about)\b|\bforget (that|my|about)\b|^\s*(note that|keep in mind)\b|"
    r"^\s*(please\s+|can you\s+|could you\s+)?(move|reschedule|postpone|push back|cancel|schedule|book|remind|add|create|"
    r"put|plan|set up|block|note down|jot down)\b|\b(on|to|in) my (calendar|todo|todos|list|schedule)\b|\bmark\b.*\b(done|complete|completed|finished)\b|#\d+", re.I)
def wants_planner(question: str, planner, prev_reply: str = "") -> bool:
    """A TODO / calendar request: the patterns above, a done / delete / move statement about an item you have
    ("I finished revise SE", "push study java to 9"), or a follow-up right after Strux changed something ("make it 5pm")."""
    if PLANNER_Q.search(question):
        return True
    from .agent import CHANGE_Q as _CQ, TIME_Q as _TQ
    if re.match(r"\s*(Added|Updated|Moved|Marked|Deleted|Done|Renamed|Remembered)\b", prev_reply or "") and len(question.split()) <= 12 \
            and (_TQ.search(question) or _CQ.search(question) or re.search(r"\b(it|that|this|them|high|low|priority)\b", question, re.I)):
        return True
    from .agent import CHANGE_Q, DELETE_Q, DONE_Q, MOVE_Q, words
    if not (DONE_Q.search(question) or DELETE_Q.search(question) or MOVE_Q.search(question) or CHANGE_Q.search(question)
            or re.search(r"\b(high|low|top|urgent)\b|\bnotes?\b|\brename\b", question, re.I)):
        return False
    said = {w[:4] for w in words(question)}
    try:
        items = [t["title"] for t in planner.todos()] + [e["title"] for e in planner.events_between(time.time() - 86400, time.time() + 30 * 86400)]
    except Exception:
        return False
    return any((stems := {w[:4] for w in words(t)}) and (stems <= said or len(stems & said) >= 2) for t in items)


PLAN_Q = re.compile(r"\b(plan (my|the) (day|afternoon|evening|morning|week)|what should i (do|work on)( next| now)?|prioriti[sz]e)\b", re.I)


def who(name: str) -> str:
    return f"{name}'s" if name else "the user's"


def system_prompt(name: str = "") -> str:
    return prompts.get("chat", who=who(name))


def tidy(text: str) -> str:
    """The 7B model still hedges sometimes ('Also: Likely working on…'); drop the hedge words."""
    return HEDGES.sub("", text).replace("Also: also", "Also:").strip()


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %H:%M")


def build_context(store, question: str, now: float | None = None, window: tuple[float, float, str] | None = None,
                  live: str = "", planner=None, knowledge=None) -> str:
    now = now or time.time()
    if window:
        start, now, label = window
    else:
        start, label = worklog.window_for(question, now)
        if label == "yesterday":
            now = start + 86400
    parts = [f"Period: {label} ({datetime.fromtimestamp(start).strftime('%a %H:%M')} – {datetime.fromtimestamp(now).strftime('%H:%M')})",
             worklog.build(store, start, now) or "Nothing notable recorded in this period."]
    if planner is not None:
        parts.append(planner.context_text(ids=False))
    if live:
        parts.append(live)
    q = question.lower()
    if any(k in q for k in ("how long", "how much time", "time spent", "spent", "minutes", "hours")):
        totals = summarizer.app_totals(store, start, now)
        from ..overlay import app_label
        if totals:
            parts.append("Time by app (only for time questions): " + ", ".join(f"{app_label(a)} {int(s // 60)}m" for a, s in totals[:8]))

    # search only when the question names something specific (not "summarise today")
    terms = [t for t in re.findall(r"[A-Za-z0-9_.-]{3,}", question) if t.lower() not in STOP]
    if terms:
        hits = [h for h in store.search(" ".join(terms[:6]), limit=12) if h["ts"] >= start - 86400]
        lines, seen = [], set()
        for h in hits:
            d = worklog.describe(h["app"], h["title"])
            if d and d[1] not in seen:
                seen.add(d[1])
                lines.append(f"- {_fmt(h['ts'])} {d[1]}")
        if lines:
            parts.append("Log entries matching the question:\n" + "\n".join(lines[:8]))
    if knowledge is not None:
        if re.search(r"\bclaude\b", question, re.I):
            # what Claude answered in this period (the log has only what you asked and which files changed)
            turns = knowledge.recent_claude(start, now, limit=6)
            if turns:
                parts.append("Claude Code's answers in this period (newest first):\n" + "\n".join(
                    f"- {_fmt(t['ts'])} [{t['project'] or '?'}] asked “{t['title'][:140]}” → {t['body'][:260]}" for t in turns))
        rel = knowledge.relevant(question) if terms else ""
        if rel:
            parts.append(rel)
    return "\n\n".join(parts)


def plan(llm, store, planner, name: str = "", now: float | None = None) -> str | None:
    """'Plan my day': open TODOs + calendar + the last 3 hours of work -> a time-blocked plan."""
    now = now or time.time()
    ctx = planner.context_text(now, ids=False) + "\n\nRecent work (last 3 hours):\n" + (worklog.build(store, now - 3 * 3600, now) or "nothing notable")
    system = prompts.get("plan", who=who(name), now=datetime.fromtimestamp(now).strftime("%A %H:%M"))
    text = llm.chat([{"role": "system", "content": prompts.with_memory(system) + "\n\nDATA:\n" + ctx},
                     {"role": "user", "content": "Plan the rest of my day."}], max_tokens=360, temperature=0.2)
    return tidy(text) if text else text


def answer(llm, store, messages: list[dict], window: tuple[float, float, str] | None = None, name: str = "",
           live: str = "", planner=None, changes: list | None = None, images: list[str] | None = None,
           route_text: str | None = None, has_files: bool = False, used: dict | None = None,
           knowledge=None, screen_ctx: dict | None = None) -> str | None:
    """messages: [{role, content}, ...] from the UI (attachments already expanded, see attachments.py).
    `route_text` = what the user typed (routing must not look at inlined file text).
    `images` go to the vision model. `changes` / `used` (optional) receive the tool changes / the model used
    (and, for a screen answer, used["screen"] = thumbnail + window).
    `knowledge` = watcher.knowledge.Knowledge (recall + feedback); `screen_ctx` = {target, shot_cfg, vision_model, learn}."""
    question = route_text if route_text is not None else next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    history = [{"role": m["role"], "content": m["content"]} for m in messages[-8:]]
    if used is not None:
        used["model"] = llm.model
    if images:
        # images -> text once (vision model, cached per image), inlined into the message they came with;
        # then the text model answers like for any attached file, with the TODO / calendar tools when asked
        from . import vision
        vmodel = getattr(llm, "cfg", {}).get("vision_model", "qwen2.5vl:3b")
        msgs = [dict(m) for m in messages]
        failed = []
        for slot in images:
            read, err = vision.read_image(llm, slot["b64"], vmodel)
            if read is None:
                failed.append(f"{slot['name']}: {err}")
                continue
            msgs[slot["msg"]]["content"] = (msgs[slot["msg"]]["content"] +
                                            f"\n\n[Attached image: {slot['name']} (read by the vision model)]\n```\n{read}\n```").strip()
        if failed and len(failed) == len(images):
            return "Couldn't read the image: " + "; ".join(failed)
        if used is not None:
            used["model"] = f"{vmodel} + {llm.model}"
        history = [{"role": m["role"], "content": m["content"]} for m in msgs[-8:]]
        has_files = True
        if not question.strip():
            question = "What is this?"
    if window is None and planner is not None and has_files and PLANNER_Q.search(question):
        from . import agent                       # "make TODOs / events out of this file or image"
        system = prompts.get("planner", who=who(name)) + "\n\nDATA:\n" + planner.context_text(time.time())
        text, made = agent.run(llm, planner, [{"role": "system", "content": prompts.with_memory(system)}] + history, typed=question,
                               allow_destructive=False)
        if changes is not None:
            changes.extend(made)
        return tidy(text) if text else text
    if has_files and not (planner is not None and PLANNER_Q.search(question)):
        system = prompts.get("files", who=who(name))
        if not question.strip():
            history[-1] = {**history[-1], "content": history[-1]["content"] + "\n\nSummarise this."}
        text = llm.chat([{"role": "system", "content": prompts.with_memory(system)}] + history, max_tokens=600, temperature=0.1)
        return tidy(text) if text else text
    if window is None and planner is not None and PLAN_Q.search(question) and not PLANNER_Q.search(question):
        return plan(llm, store, planner, name)
    prev_reply = next((m.get("content") or "" for m in reversed(messages[:-1]) if m.get("role") == "assistant"), "")
    if window is None and screen_ctx and (screen_ctx.get("force") or not PLANNER_Q.search(question)):
        from . import screen
        if screen_ctx.get("force") or screen.wants_screen(question, prev_reply):
            info: dict = {}
            vmodel = screen_ctx.get("vision_model") or "qwen2.5vl:3b"
            text = screen.answer(llm, question, history, screen_ctx.get("target") or {}, screen_ctx.get("shot_cfg") or {}, vmodel,
                                 name, knowledge, bool(screen_ctx.get("learn", True)), info, force=bool(screen_ctx.get("force")),
                                 use_vision=bool(screen_ctx.get("vision")))
            if used is not None:
                used["model"] = f"{vmodel} + {llm.model}" if info.get("vision") else llm.model
                used["screen"] = info
            return text
    if window is None and planner is not None and wants_planner(question, planner, prev_reply):
        from . import agent as _agent
        bad = _agent.INVALID_TIME.search(question)
        if bad:                                   # "at 25:00": ask, never let the model guess another time
            return f"{bad.group(0)} isn't a valid time. What time did you mean?"
        span = re.search(r"\bfrom\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(?:to|till|until|-)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", question, re.I)
        if span:
            from ..planner import parse_when as _pw
            base = time.time()
            a_, b_ = _pw("today " + span.group(1), base), _pw("today " + span.group(2), base)
            if a_ and b_ and b_ <= a_:
                return (f"That ends ({span.group(2).strip()}) before it starts ({span.group(1).strip()}). "
                        "What end time did you mean?")
        from . import agent
        now = time.time()
        system = prompts.get("planner", who=who(name)) + "\n\nDATA:\n" + planner.context_text(now)
        text, made = agent.run(llm, planner, [{"role": "system", "content": prompts.with_memory(system)}] + history, typed=question)
        if changes is not None:
            changes.extend(made)
        return tidy(text) if text else text
    if live and window is None and LIVE_Q.search(question):
        # "what's running / what is Claude doing": the live snapshot leads, plus the last hour for context
        now = time.time()
        ctx = live + "\n\nLast hour, for context:\n" + (worklog.build(store, now - 3600, now) or "nothing notable")
        system = prompts.get("live", who=who(name))
    else:
        ctx = build_context(store, question, window=window, live=live, planner=planner, knowledge=knowledge)
        system = system_prompt(name)
        if any(m in ctx for m in EMPTY_LOG) and not any(k in ctx for k in ("Log entries matching", "Related notes", "Claude Code's answers")) \
                and not live:
            label = window[2] if window else worklog.window_for(question, time.time())[1]
            return empty_answer(label if label.startswith(("in ", "today", "yesterday", "this")) else f"in {label}", planner)
    if knowledge is not None:
        hint = knowledge.feedback_hint(question)
        if hint:
            system += "\n\n" + hint
    convo = [{"role": "system", "content": prompts.with_memory(system) + "\n\nLOG:\n" + ctx}] + history
    text = llm.chat(convo, max_tokens=320, temperature=0.1)
    if text and planner is not None:
        from .agent import CLAIM
        if CLAIM.search(text):                    # this path has no tools: it cannot have added / scheduled anything
            return ("I haven't changed anything yet. To add it, say e.g. “add a todo to …” or “schedule … tomorrow at 3pm”.")
    return tidy(text) if text else text
