"""Turn raw events into a compact activity summary the rules and the LLM read.

The summary is bounded in size no matter how long the log is: it only looks at
the last N minutes of raw events plus the stored rolling summaries.
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActivityWindow:
    minutes: float
    now: float
    app_seconds: dict[str, float] = field(default_factory=dict)
    switches: int = 0
    current_app: str = ""
    current_title: str = ""
    current_focus_seconds: float = 0.0
    idle_seconds: float = 0.0
    last_idle_end: dict | None = None
    clipboard_count: int = 0
    clipboard_hashes: Counter = field(default_factory=Counter)
    frames: int = 0
    error_frames: list[dict] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    jobs_done: list[dict] = field(default_factory=list)
    hot_procs: list[str] = field(default_factory=list)
    claude: list[str] = field(default_factory=list)
    # attached by the brain before rules run (not part of the log itself)
    planner: Any = None          # watcher.planner.Planner
    procs: Any = None            # the process sensor (long_busy())
    user: str = ""               # the user's first name

    def top_apps(self, n: int = 5) -> list[tuple[str, float]]:
        return sorted(self.app_seconds.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def as_text(self) -> str:
        lines = [f"Window: last {int(self.minutes)} min"]
        if self.current_app:
            lines.append(f"Now: {self.current_app} - {self.current_title[:80]} (focused {int(self.current_focus_seconds // 60)} min)")
        if self.app_seconds:
            lines.append("Apps: " + ", ".join(f"{a} {int(s // 60)}m" for a, s in self.top_apps()))
        lines.append(f"Window switches: {self.switches}; idle: {int(self.idle_seconds // 60)} min; clipboard copies: {self.clipboard_count}; frames: {self.frames}")
        if self.error_frames:
            lines.append("Errors on screen: " + "; ".join(f"{e['app']}: {e['title'][:60]}" for e in self.error_frames[-3:]))
        if self.titles:
            lines.append("Recent titles: " + " | ".join(t[:60] for t in self.titles[-6:]))
        if self.jobs_done:
            lines.append("Background jobs finished: " + ", ".join(f"{j.get('label') or j['app']} ({j['runtime_s'] // 60}m)" for j in self.jobs_done[-4:]))
        if self.hot_procs:
            lines.append("Busy background processes: " + ", ".join(self.hot_procs[-4:]))
        if self.claude:
            lines.append("Claude Code: " + " | ".join(self.claude[-5:]))
        return "\n".join(lines)


def build(store, minutes: float = 15, now: float | None = None) -> ActivityWindow:
    now = now or time.time()
    w = ActivityWindow(minutes=minutes, now=now)
    start = now - minutes * 60
    events = store.recent_events(minutes, now=now)

    app_seconds: dict[str, float] = defaultdict(float)
    cur_app, cur_title, cur_since = "", "", start
    idle_since: float | None = None
    seen_titles: list[str] = []

    for ev in events:
        kind, ts = ev["kind"], ev["ts"]
        if kind == "window":
            if cur_app and idle_since is None:
                app_seconds[cur_app] += ts - cur_since
            if ev["app"] != cur_app:
                w.switches += 1
                cur_since = ts
            cur_app, cur_title = ev["app"], ev["title"]
            if cur_title and (not seen_titles or seen_titles[-1] != cur_title):
                seen_titles.append(cur_title)
        elif kind == "title":
            cur_title = ev["title"]
            if cur_title and (not seen_titles or seen_titles[-1] != cur_title):
                seen_titles.append(cur_title)
        elif kind == "idle_start":
            idle_since = ts
            if cur_app:
                app_seconds[cur_app] += ts - cur_since
        elif kind == "idle_end":
            data = json.loads(ev["data"] or "{}")
            w.idle_seconds += data.get("idle_seconds", (ts - idle_since) if idle_since else 0)
            w.last_idle_end = {"ts": ts, "idle_seconds": data.get("idle_seconds", 0)}
            idle_since = None
            cur_since = ts
        elif kind == "clipboard":
            w.clipboard_count += 1
            data = json.loads(ev["data"] or "{}")
            if data.get("hash"):
                w.clipboard_hashes[data["hash"]] += 1
        elif kind == "frame":
            w.frames += 1
            data = json.loads(ev["data"] or "{}")
            if data.get("error"):
                w.error_frames.append({"ts": ts, "app": ev["app"], "title": ev["title"], "frame_id": data.get("id")})
        elif kind == "ocr_error":
            data = json.loads(ev["data"] or "{}")
            w.error_frames.append({"ts": ts, "app": ev["app"], "title": ev["title"], "frame_id": data.get("frame_id"),
                                   "line": data.get("line")})
        elif kind == "job_done":
            data = json.loads(ev["data"] or "{}")
            w.jobs_done.append({"ts": ts, "app": ev["app"], "runtime_s": int(data.get("runtime_s", 0)), "label": data.get("label"),
                                "agent": bool(data.get("agent"))})
        elif kind == "proc_hot":
            if ev["app"] not in w.hot_procs:
                w.hot_procs.append(ev["app"])
        elif kind == "claude":
            data = json.loads(ev["data"] or "{}")
            if data.get("summary") and data.get("event") in ("UserPromptSubmit", "Notification", "Stop", "SessionStart"):
                w.claude.append(f"{data.get('project', '')}: {data['summary'][:80]}")

    if cur_app and idle_since is None:
        app_seconds[cur_app] += now - cur_since
    w.app_seconds = dict(app_seconds)
    w.current_app, w.current_title = cur_app, cur_title
    w.current_focus_seconds = (now - cur_since) if cur_app and idle_since is None else 0.0
    w.titles = seen_titles
    return w


def focus_streak_seconds(store, now: float | None = None, lookback_minutes: float = 180) -> tuple[str, float]:
    """How long the current app has been continuously in front (idle breaks it)."""
    now = now or time.time()
    events = store.recent_events(lookback_minutes, kinds=("window", "idle_start", "idle_end"), now=now)
    if not events:
        return "", 0.0
    cur_app = ""
    since = now
    for ev in events:
        if ev["kind"] == "window":
            if ev["app"] != cur_app:
                cur_app, since = ev["app"], ev["ts"]
        elif ev["kind"] == "idle_end":
            since = ev["ts"]
        elif ev["kind"] == "idle_start":
            cur_app, since = cur_app, ev["ts"]  # streak restarts after idle_end
    return cur_app, max(0.0, now - since)


# windows that are not work: the shell, lock screen, system popups, Strux itself
NOT_WORK_APPS = {"lockapp.exe", "logonui.exe", "pythonw.exe", "strux.exe", "halo.exe", "shellexperiencehost.exe", "searchhost.exe",
                 "startmenuexperiencehost.exe", "snippingtool.exe", "screenclippinghost.exe", "gameinputsvc.exe",
                 "textinputhost.exe", "applicationframehost.exe", "systemsettingsbroker.exe", "consent.exe"}
SHELL_TITLES = {"", "program manager", "task switching", "task view", "start", "search", "system tray overflow window.",
                "notification center", "notification centre", "action center", "quick settings"}


def counts_as_work(app: str, title: str) -> bool:
    a = (app or "").lower()
    if not a or a in NOT_WORK_APPS:
        return False
    return not (a == "explorer.exe" and (title or "").strip().lower() in SHELL_TITLES)


def app_totals(store, start_ts: float, end_ts: float) -> list[tuple[str, float]]:
    """Seconds per app between two timestamps, idle time excluded. Sorted desc."""
    events = store.events_between(start_ts, end_ts)
    totals: dict[str, float] = defaultdict(float)
    cur_app, since, idle = "", start_ts, False
    for ev in events:
        k, ts = ev["kind"], ev["ts"]
        if k == "window":
            if cur_app and not idle:
                totals[cur_app] += ts - since
            cur_app, since = (ev["app"] if counts_as_work(ev["app"], ev["title"]) else ""), ts
        elif k == "idle_start":
            if cur_app and not idle:
                totals[cur_app] += ts - since
            idle = True
        elif k == "idle_end":
            idle, since = False, ts
    if cur_app and not idle:
        totals[cur_app] += min(end_ts, time.time()) - since
    return sorted(((a, s) for a, s in totals.items() if a), key=lambda kv: kv[1], reverse=True)


def sessions(store, start_ts: float, end_ts: float, events=None) -> list[dict]:
    """Contiguous foreground sessions [{start,end,app,title,seconds}] plus markers for the timeline UI.
    Pass `events` (events_between(start_ts, end_ts)) when the caller already has them."""
    events = store.events_between(start_ts, end_ts) if events is None else events
    out: list[dict] = []
    markers: list[dict] = []
    cur: dict | None = None
    shell = False
    for ev in events:
        k, ts = ev["kind"], ev["ts"]
        if k == "window":
            if not counts_as_work(ev["app"], ev["title"]):      # desktop, lock screen, Strux...: not a session
                if cur:
                    cur["end"] = ts
                    out.append(cur)
                cur, shell = None, True
                continue
            shell = False
            if cur and ev["app"] == cur["app"]:
                cur["title"] = ev["title"] or cur["title"]
                continue
            if cur:
                cur["end"] = ts
                out.append(cur)
            cur = {"start": ts, "end": ts, "app": ev["app"], "title": ev["title"]}
        elif k == "title" and cur:
            cur["title"] = ev["title"] or cur["title"]
        elif k in ("idle_start", "idle_end", "clipboard", "blocked", "url"):
            d = json.loads(ev["data"] or "{}")
            markers.append({"ts": ts, "kind": k, "app": ev["app"], "text": d.get("url") or d.get("preview", "")[:80]
                            or (f"{int(d.get('idle_seconds', 0) // 60)} min away" if k == "idle_end" else d.get("reason", ""))})
            if k == "idle_start" and cur:
                cur["end"] = ts
                out.append(cur)
                cur = None
            if k == "idle_end" and cur is None and out and not shell:
                cur = {"start": ts, "end": ts, "app": out[-1]["app"], "title": out[-1]["title"]}
    if cur:
        cur["end"] = min(end_ts, time.time())
        out.append(cur)
    for s in out:
        s["seconds"] = max(0, int(s["end"] - s["start"]))
    return [s for s in out if s["seconds"] >= 1] , markers
