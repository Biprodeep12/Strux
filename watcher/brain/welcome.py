"""Welcome-back message, shown in the bar the moment you return from being away.

Built from the log without the model, so it is instant and never made up:
  "Welcome back, Biprodeep · 25 min away. You were editing app.py in watcher-ai.
   While you were away: Claude Code finished in watcher-ai, npm finished (12 min)."
Accepting it ("Recap") asks the model for a fuller where-you-left-off summary.
"""
from __future__ import annotations

import ctypes
import json
import logging
from datetime import datetime

from . import summarizer, worklog

log = logging.getLogger(__name__)
_name_cache: str | None = None


def user_name(cfg_name: str = "") -> str:
    """First name: config `user.name`, else the Windows account's display name, else ''."""
    global _name_cache
    if cfg_name:
        return cfg_name.split()[0]
    if _name_cache is None:
        _name_cache = ""
        try:
            n = ctypes.c_ulong(256)
            b = ctypes.create_unicode_buffer(256)
            if ctypes.windll.secur32.GetUserNameExW(3, b, ctypes.byref(n)) and b.value.strip():   # NameDisplay
                _name_cache = b.value.split()[0]
        except Exception:
            pass
    return _name_cache


def away(secs: float) -> str:
    m = int(secs // 60)
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h"


def greeting(now: float, name: str) -> str:
    h = datetime.fromtimestamp(now).hour
    hello = "Welcome back"
    if h < 5:
        hello = "Still up? Welcome back"
    elif h < 11:
        hello = "Morning, welcome back"
    return f"{hello}, {name}" if name else hello


def last_work(store, left: float) -> str | None:
    """What was in front of you just before you left, as 'You were …'."""
    claude = last_claude_prompt(store, left)
    sessions, _ = summarizer.sessions(store, left - 45 * 60, left)
    if claude and (not sessions or (sessions[-1]["app"] or "").lower() in worklog.TERMINALS):
        return claude
    for s in reversed(sessions):
        if s["seconds"] < 20:
            continue
        d = worklog.describe(s["app"], s["title"])
        if not d:
            continue
        text = d[1]
        if text.startswith("Claude Code session"):
            return f"You were in the {text}."
        if text.split(" ", 1)[0].endswith("ing"):
            return f"You were {text}."
        return f"Last up: {text}."
    return None


def last_claude_prompt(store, left: float) -> str | None:
    """'You were working with Claude Code on watcher-ai: “fix the dashboard…”' if you prompted it in the last 30 min."""
    best = None
    for r in store.events_between(left - 30 * 60, left):
        if r["kind"] == "claude":
            d = json.loads(r["data"] or "{}")
            if d.get("event") == "UserPromptSubmit" and d.get("prompt"):
                best = d
    if not best:
        return None
    text = worklog._prompt_text(best["prompt"]) or ""
    text = text if len(text) <= 70 else text[:67].rsplit(" ", 1)[0] + "…"
    proj = best.get("project") or "a project"
    return f"You were working with Claude Code on {proj}" + (f": “{text}”." if text else ".")


def meanwhile(store, left: float, now: float) -> list[str]:
    """Things that happened while you were away: Claude finished / needs approval, longer background jobs."""
    from collections import Counter, defaultdict
    from ..overlay import app_label
    # a session's project = the folder it was mostly in (events carry the cwd, which can be a subfolder)
    projects: dict[str, Counter] = defaultdict(Counter)
    last: dict[str, dict] = {}
    finished: list[str] = []
    jobs: list[str] = []
    for r in store.events_between(left - 6 * 3600, now):
        d = json.loads(r["data"] or "{}")
        if r["kind"] == "claude" and d.get("session"):
            sid = d["session"]
            if d.get("project"):
                projects[sid][d["project"]] += 1
            if r["ts"] >= left:
                last[sid] = d
                if d.get("event") == "Stop" and sid not in finished:
                    finished.append(sid)
        elif r["kind"] == "job_done" and r["ts"] >= left and int(d.get("runtime_s", 0)) >= 120:
            label = f"{d.get('label') or app_label(r['app'])} finished ({int(d['runtime_s']) // 60} min)"
            if label not in jobs:
                jobs.append(label)
    name = lambda sid: projects[sid].most_common(1)[0][0] if projects.get(sid) else "a project"
    needs = [sid for sid, d in last.items() if d.get("event") in ("Notification", "PermissionRequest")
             and any(k in (d.get("summary") or "").lower() for k in ("permission", "approv", "allow"))]
    out = []
    if needs:
        out.append("Claude Code needs your approval in " + ", ".join(dict.fromkeys(name(s) for s in needs[:2])))
    done = [s for s in finished if s not in needs]
    if done:
        out.append("Claude Code finished in " + ", ".join(dict.fromkeys(name(s) for s in done[:2])))
    out += jobs[-2:]
    return out


def compose(store, idle_seconds: float, now: float, name: str = "") -> str:
    left = now - idle_seconds
    parts = [f"{greeting(now, name)} · {away(idle_seconds)} away."]
    lw = last_work(store, left)
    if lw:
        parts.append(lw)
    mw = meanwhile(store, left, now)
    if mw:
        parts.append("While you were away: " + "; ".join(mw) + ".")
    return " ".join(parts)
