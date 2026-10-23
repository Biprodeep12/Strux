"""Turn the raw activity log into a *work log*: what was worked on, not which .exe was open.

Signals, strongest first:
  - Claude Code hooks: your prompts, the project, files Claude read / edited, commands it ran
  - window / tab titles, cleaned: document + folder (editors), page title + site (browsers),
    Claude Code session topic (terminal tab title), chat channel (Discord / Slack ...)
  - URLs from the browser sensor
  - background jobs that finished (builds, installs, tests)
Noise dropped: Explorer chrome, the tray, Program Manager, new tabs, Strux's own windows,
throw-away test windows, anything shorter than ~45 s in total.
"""
from __future__ import annotations

import json
import re
from bisect import bisect_left, bisect_right
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime
from pathlib import PureWindowsPath
from urllib.parse import urlparse

from . import summarizer

BROWSERS = {"brave.exe", "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "vivaldi.exe", "arc.exe"}
EDITORS = {"code.exe": "VS Code", "cursor.exe": "Cursor", "devenv.exe": "Visual Studio", "idea64.exe": "IntelliJ",
           "pycharm64.exe": "PyCharm", "notepad++.exe": "Notepad++", "sublime_text.exe": "Sublime", "notepad.exe": "Notepad"}
TERMINALS = {"windowsterminal.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "wezterm-gui.exe", "alacritty.exe"}
CHAT = {"discord.exe": "Discord", "slack.exe": "Slack", "whatsapp.exe": "WhatsApp", "telegram.exe": "Telegram",
        "teams.exe": "Teams", "ms-teams.exe": "Teams"}
DOCS = {"winword.exe": "Word", "excel.exe": "Excel", "powerpnt.exe": "PowerPoint", "acrobat.exe": "Acrobat",
        "notion.exe": "Notion", "obsidian.exe": "Obsidian", "figma.exe": "Figma", "postman.exe": "Postman"}
OWN_TITLES = re.compile(r"^(watcher ai|strux|strux bar|strux pane|halo|halo bar|halo pane|tk|redtest|rgntest|keytest|glasstest|pvtest|nr2|hosttest)(\b| overlay)", re.I)
NOISE_TITLES = {"", "program manager", "system tray overflow window.", "task switching", "search", "start",
                "new tab", "snipping tool overlay", "task view", "notification centre", "notification center"}
NOISE_APPS = {"pythonw.exe", "strux.exe", "halo.exe", "snippingtool.exe", "shellexperiencehost.exe", "searchhost.exe",
              "startmenuexperiencehost.exe", "lockapp.exe", "applicationframehost.exe"}
SUFFIX = re.compile(r"\s+[-–—|]\s+(Brave|Google Chrome|Microsoft​?\s?Edge|Mozilla Firefox|Opera|Vivaldi|Visual Studio Code|"
                    r"Cursor|Discord|Slack|Notepad\+\+|Notepad|Microsoft Word|Excel|PowerPoint)\s*$", re.I)
LEAD_GLYPHS = re.compile(r"^[\s◐◑◒◓✳●•*⠁-⣿·]+")
EXE_PATH = re.compile(r'[a-z]:\\[^"]*\.exe\b', re.I)
COUNT_PREFIX = re.compile(r"^\(\d+\)\s*")


def _clean(title: str) -> str:
    t = COUNT_PREFIX.sub("", LEAD_GLYPHS.sub("", title or "")).strip()
    t = SUFFIX.sub("", t).strip()
    return t


TAG = re.compile(r"<[^>]+>")
SITE_NAMES = {          # phrased as actions so a small model cannot misread a page title as a finished task
    "chatgpt.com": 'chatting with ChatGPT about "{t}"', "chat.openai.com": 'chatting with ChatGPT about "{t}"',
    "claude.ai": 'chatting with Claude.ai about "{t}"', "gemini.google.com": 'chatting with Gemini about "{t}"',
    "github.com": 'on GitHub: "{t}"', "expo.dev": 'checking an Expo build: "{t}"',
    "stackoverflow.com": 'reading Stack Overflow: "{t}"', "youtube.com": 'watching YouTube: "{t}"',
    "docs.google.com": 'in a Google Doc: "{t}"', "mail.google.com": 'reading email: "{t}"', "figma.com": 'in Figma: "{t}"',
    "notion.so": 'in Notion: "{t}"', "linear.app": 'in Linear: "{t}"',
}
GENERIC_DIRS = {"src", "ui", "app", "lib", "web", "frontend", "backend", "server", "client", "packages"}


def _prompt_text(p: str) -> str | None:
    """Your prompt as a short line; harness notices are dropped, pasted logs summarised."""
    t = p.strip()
    if t.startswith("<task-notification") or t.startswith("<system-reminder"):
        return None
    if t.startswith("<pasted_content"):
        body = TAG.sub(" ", t)
        kind = "an error/build log" if re.search(r"error|failed|exception|npm|gradle|build", body, re.I) else "some text"
        return f"pasted {kind} for Claude to look at"
    t = TAG.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:150] or None


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _dur(s: float) -> str:
    m = int(round(s / 60))
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{max(1, m)}m"


def describe(app: str, title: str) -> tuple[str, str] | None:
    """(activity key, human description) for one foreground window, or None for noise."""
    a = (app or "").lower()
    raw = title or ""
    t = _clean(raw)
    if a in NOISE_APPS or OWN_TITLES.match(t) or t.lower() in NOISE_TITLES:
        return None
    if EXE_PATH.search(raw) and a not in TERMINALS:
        return None     # updaters / installers / helper popups titled with their own program path
    if a in TERMINALS:
        if LEAD_GLYPHS.match(raw) and t and not t.lower().startswith(("windows powershell", "c:\\", "npm", "cmd")):
            return f"claude:{t}", f'Claude Code session "{t}"'
        if t.lower() in ("windows powershell", "command prompt") or t.lower().startswith("c:\\windows\\system32"):
            return "terminal", "working in a terminal"
        return f"term:{t}", f'terminal: {t}'
    if a in EDITORS:
        parts = [p.strip() for p in t.split(" - ") if p.strip()]
        if len(parts) >= 2:
            return f"edit:{parts[-1]}:{parts[0]}", f"editing {parts[0]} in {parts[-1]} ({EDITORS[a]})"
        return f"edit:{t}", f"editing {t or 'files'} ({EDITORS[a]})"
    if a in BROWSERS:
        if not t:
            return None
        return f"web:{t}", f'reading "{t}" in the browser'
    if a in CHAT:
        return f"chat:{CHAT[a]}:{t}", (f'chatting on {CHAT[a]} in "{t}"' if t else f"chatting on {CHAT[a]}")
    if a in DOCS:
        return f"doc:{t}", f"{DOCS[a]}: {t}" if t else f"working in {DOCS[a]}"
    if a == "explorer.exe":
        return (f"files:{t}", f"browsing files in {t}") if t else None
    if not t:
        return None
    name = a.rsplit(".", 1)[0].capitalize()
    return f"app:{a}:{t}", f"{name}: {t}"


def window_for(question: str, now: float) -> tuple[float, str]:
    q = question.lower()
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if "yesterday" in q:
        return midnight - 86400, "yesterday"
    if "hour" in q:
        return now - 75 * 60, "the last hour"
    if "week" in q:
        return now - 7 * 86400, "the last 7 days"
    if "morning" in q:
        return midnight, "this morning"
    start = min(midnight, now - 3 * 3600)
    return start, "today"


def build(store, start: float, end: float) -> str:
    rows = store.events_between(start, end)          # read once, reused below
    sessions, markers = summarizer.sessions(store, start, end, events=rows)
    # ---- activities: aggregate cleaned titles -------------------------------------------
    acts: "OrderedDict[str, dict]" = OrderedDict()
    # title -> site, from URL events (each records the tab title it was read with)
    title_host: dict[str, Counter] = defaultdict(Counter)
    sess_proj: dict[str, Counter] = defaultdict(Counter)
    claude_ts: list[tuple[float, str]] = []          # (ts, session) of Claude Code hook events
    for r in rows:
        if r["kind"] == "url":
            u = json.loads(r["data"] or "{}").get("url", "")
            host = urlparse(u).netloc.lower().removeprefix("www.")
            if host and "." in host:
                title_host[_clean(r["title"])][host] += 1
        elif r["kind"] == "claude":
            d = json.loads(r["data"] or "{}")
            if d.get("session"):
                claude_ts.append((r["ts"], d["session"]))
                if d.get("project"):
                    sess_proj[d["session"]][d["project"]] += 1
    claude_times = [t for t, _ in claude_ts]

    def claude_project(a: float, b: float) -> str | None:
        """Project Claude Code was working on between a and b (a terminal window in front = that work)."""
        i, j = bisect_left(claude_times, a), bisect_right(claude_times, b)
        if i >= j:
            return None
        c = Counter()
        for _, sid in claude_ts[i:j]:
            if sess_proj.get(sid):
                c[sess_proj[sid].most_common(1)[0][0]] += 1
        return c.most_common(1)[0][0] if c else None

    for s in sessions:
        d = describe(s["app"], s["title"])
        if not d or s["seconds"] < 5:
            continue
        key, text = d
        if (s["app"] or "").lower() in TERMINALS:
            proj = claude_project(s["start"], s["end"])
            if proj:
                key, text = f"cc:{proj}", f"working with Claude Code on {proj}"
        if key.startswith("web:"):
            ct = _clean(s["title"])
            host = title_host[ct].most_common(1)[0][0] if title_host.get(ct) else ""
            if host:
                label = SITE_NAMES.get(host)
                text = label.format(t=ct) if label else f'reading "{ct}" on {host}'
        a = acts.setdefault(key, {"text": text, "secs": 0.0, "first": s["start"], "last": s["end"]})
        a["secs"] += s["seconds"]
        a["last"] = max(a["last"], s["end"])
    # attach sites to browser activities
    sites: dict[str, set] = defaultdict(set)
    for m in markers:
        if m["kind"] == "url" and m["text"]:
            host = urlparse(m["text"]).netloc.lower().removeprefix("www.")
            if host and "." in host:
                sites[host].add(m["ts"])
    ranked = sorted((a for a in acts.values() if a["secs"] >= 45), key=lambda a: a["secs"], reverse=True)
    main = [a for a in ranked if a["secs"] >= 180][:8]
    minor = [a for a in ranked if a["secs"] < 180][:8]
    lines = []
    if main:
        lines.append("Main things in front of the user (window/tab titles, total time, first-last seen):")
        for a in main:
            lines.append(f"- {a['text']} — {_dur(a['secs'])} ({_hm(a['first'])}-{_hm(a['last'])})")
    if minor:
        lines.append("Briefly (under 3 min each): " + "; ".join(a["text"] for a in minor))
    if sites:
        lines.append("Websites visited: " + ", ".join(sorted(sites, key=lambda h: -len(sites[h]))[:8]))

    # ---- Claude Code -------------------------------------------------------------------
    claude: "OrderedDict[str, dict]" = OrderedDict()      # keyed by Claude session id
    jobs = []
    for r in rows:
        if r["kind"] == "job_done":
            d = json.loads(r["data"] or "{}")
            if int(d.get("runtime_s", 0)) >= 60:      # sub-minute helpers are noise
                jobs.append(f"{d.get('label') or r['app'].rsplit('.', 1)[0]} finished at {_hm(r['ts'])} after {_dur(d.get('runtime_s', 0))}")
            continue
        if r["kind"] != "claude":
            continue
        d = json.loads(r["data"] or "{}")
        sid = d.get("session") or "?"
        c = claude.setdefault(sid, {"prompts": [], "files": OrderedDict(), "edits": OrderedDict(), "cmds": 0,
                                    "first": r["ts"], "last": r["ts"], "projects": defaultdict(int)})
        c["last"] = r["ts"]
        if d.get("project"):
            c["projects"][d["project"]] += 1
        ev, summ = d.get("event"), d.get("summary") or ""
        if ev == "UserPromptSubmit" and summ:
            pt = _prompt_text(d.get("prompt") or summ)
            if pt:
                c["prompts"].append((r["ts"], pt))
        elif ev == "PreToolUse":
            tool = d.get("tool") or summ.split(" ", 1)[0]
            arg = summ.split(" ", 1)[1] if " " in summ else ""
            name = PureWindowsPath(arg).name if arg else ""
            if name.endswith("…") or len(name) < 3:
                name = ""
            if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit") and name:
                c["edits"][name] = 1
            elif tool == "Read" and name:
                c["files"][name] = 1
            elif tool in ("Bash", "PowerShell"):
                c["cmds"] += 1
    useful = [c for c in claude.values() if c["prompts"] or c["edits"]]
    if useful:
        lines.append("Claude Code sessions (what the user asked it and what it changed):")
        for c in useful:
            named = sorted(c["projects"].items(), key=lambda kv: -kv[1])
            proj = next((p for p, _ in named if p.lower() not in GENERIC_DIRS), named[0][0] if named else "a project")
            lines.append(f"- project {proj} ({_hm(c['first'])}–{_hm(c['last'])}):")
            for ts, p in c["prompts"][-6:]:
                lines.append(f"    · {_hm(ts)} asked: {p[:150]}")
            if c["edits"]:
                lines.append(f"    · files changed: {', '.join(list(c['edits'])[-10:])}")
            if c["files"]:
                lines.append(f"    · files read: {', '.join(list(c['files'])[-8:])}")
    if jobs:
        lines.append("Background jobs: " + "; ".join(jobs[-5:]))
    return "\n".join(lines) if lines else "No work activity recorded in this period."
