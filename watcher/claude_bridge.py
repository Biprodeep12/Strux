"""Turns raw Claude Code hook payloads into compact `claude` events + overlay items.

Hook events (Claude Code): SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
Notification (permission prompts, idle), Stop, SubagentStop, PreCompact, SessionEnd.

We never store full tool inputs or transcripts, only: event, session id, cwd,
tool name, a one-line summary (<= 200 chars), and whether Claude is waiting on you.

When the bar alerts you (only when Claude needs *you*):
  approval   a permission prompt: always, with what it wants to do ("run npm run build")
  finished   a turn you started ended and stayed ended for DONE_GRACE s (a session that goes
             straight on by itself, e.g. woken by a background task, is not "finished"), it took
             >= MIN_TURN s, and you are not already looking at a terminal
  never      Claude Code's own "waiting for your input" idle reminder (the finished alert covered
             it), subagents, and sessions nobody typed into (background / scripted runs)
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .events import Event

log = logging.getLogger(__name__)

DONE_GRACE = 8.0          # s of quiet after Stop before calling it finished
MIN_TURN = 15.0           # s: quick answers you watched come in need no alert
TERMINALS = {"windowsterminal.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "wezterm-gui.exe", "alacritty.exe", "code.exe", "cursor.exe"}


def _ago(secs: float) -> str:
    m = int(secs // 60)
    return f"{int(secs)} s" if secs < 60 else f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"


def _short(s: Any, n: int = 200) -> str:
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_summary(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "file_path", "pattern", "query", "url", "description", "prompt", "skill"):
        if inp.get(k):
            return _short(inp[k], 120)
    return ""


class ClaudeBridge:
    def __init__(self, bus, overlay=None, foreground=None, grace: float = DONE_GRACE) -> None:
        self.bus = bus
        self.overlay = overlay
        self.sessions: dict[str, dict[str, Any]] = {}   # session_id -> state (for the dashboard card)
        self.grace = grace
        self.on_stop = None            # -> fn(transcript_path) when a turn ends (Knowledge reads it)
        # -> the app currently in front ("windowsterminal.exe"); used to skip "finished" when you are watching
        self.foreground = foreground or (lambda: (overlay._current[0] if overlay is not None and hasattr(overlay, "_current") else ""))

    def state(self) -> dict:
        now = time.time()
        # keep active sessions for 6 h, ended ones only 5 min (so the bar/dashboard stay tidy)
        live = {sid: s for sid, s in self.sessions.items()
                if now - s["updated"] < (300 if s["status"] == "ended" else 6 * 3600)}
        self.sessions = live
        return {"sessions": sorted(live.values(), key=lambda s: s["updated"], reverse=True)[:6]}

    def ingest(self, payload: dict) -> dict:
        ev = str(payload.get("hook_event_name") or payload.get("event") or "unknown")
        sid = str(payload.get("session_id") or "?")[:12]
        cwd = str(payload.get("cwd") or "")
        project = Path(cwd).name if cwd else ""
        s = self.sessions.setdefault(sid, {"id": sid, "project": project, "cwd": cwd, "status": "idle", "last": "",
                                           "tools": 0, "prompts": 0, "started": time.time(), "updated": time.time(),
                                           "waiting": False, "last_tool": ""})
        s["updated"] = time.time()
        if project:
            s["project"] = project
        data: dict[str, Any] = {"event": ev, "session": sid, "project": project}
        summary = ""
        alert = None

        if ev == "SessionStart":
            s["status"] = "working"
            summary = f"session started ({payload.get('source', '')})"
        elif ev == "UserPromptSubmit":
            s["prompts"] += 1
            s["status"] = "working"
            s["waiting"] = False
            summary = _short(payload.get("prompt"), 160)
            s["last"] = "you: " + summary
            data["prompt"] = summary
            prompt = str(payload.get("prompt") or "")
            if not prompt.lstrip().startswith("<"):          # <task-notification> etc. are not you typing
                s["human"] = True
                s["turn_start"] = time.time()
                s["asked"] = _short(prompt, 70)
        elif ev == "PreToolUse":
            tool = str(payload.get("tool_name") or "")
            s["tools"] += 1
            s["status"] = "working"
            s["waiting"] = False
            s["last_tool"] = tool
            summary = f"{tool} {_tool_summary(tool, payload.get('tool_input') or {})}".strip()
            data["tool"] = tool
            s["last"] = "claude: " + summary
            s["pending_tool"] = (tool, _tool_summary(tool, payload.get("tool_input") or {}))
        elif ev == "PostToolUse":
            tool = str(payload.get("tool_name") or "")
            data["tool"] = tool
            resp = payload.get("tool_response")
            ok = True
            if isinstance(resp, dict):
                ok = not (resp.get("is_error") or resp.get("error"))
            summary = f"{tool} {'done' if ok else 'FAILED'}"
            data["ok"] = ok
        elif ev == "Notification":
            msg = _short(payload.get("message"), 160)
            ntype = str(payload.get("notification_type") or "").lower()
            summary = msg or ntype
            if "permission" in ntype or "approval" in msg.lower() or "permission" in msg.lower() or "needs your" in msg.lower():
                s["status"] = "waiting for approval"
                s["waiting"] = True
                alert = (f"claude-approval-{sid}", self._approval_text(s, msg), "warn")
            elif "idle" in ntype or "waiting" in msg.lower():
                # Claude Code's 60 s idle reminder after a turn: the "finished" alert already told you
                s["status"] = "waiting for input"
                s["waiting"] = True
            s["last"] = "claude: " + summary
        elif ev in ("Stop", "SubagentStop"):
            s["status"] = "done" if ev == "Stop" else "working"
            s["waiting"] = False
            summary = "finished responding" if ev == "Stop" else "subagent finished"
            if ev == "Stop":
                s["last"] = "claude: finished"
                if self.on_stop and payload.get("transcript_path"):
                    try:
                        self.on_stop(str(payload["transcript_path"]))
                    except Exception:
                        log.exception("on_stop")
                stop_at = s["updated"]
                if not payload.get("stop_hook_active") and self.grace >= 0:
                    t = threading.Timer(self.grace, self._finished, args=(sid, stop_at))
                    t.daemon = True
                    t.start()
        elif ev == "SessionEnd":
            s["status"] = "ended"
            s["waiting"] = False
            summary = "session ended"
        elif ev == "PreCompact":
            summary = "compacting context"
        data["summary"] = summary
        self.bus.publish(Event(kind="claude", ts=time.time(), app="claude", title=f"{project} · {ev}", data=data))
        if alert and self.overlay:
            key, text, level = alert
            self.overlay.alert(key, text, level=level, cooldown=20)
        return {"ok": True, "event": ev}

    # ---- alerts that need you ---------------------------------------------------------------------------
    @staticmethod
    def _approval_text(s: dict, msg: str) -> str:
        where = s.get("project") or "a session"
        tool, what = s.get("pending_tool") or ("", "")
        if tool and what:
            verb = {"Bash": "run", "PowerShell": "run", "Edit": "edit", "Write": "write", "Read": "read",
                    "WebFetch": "open", "WebSearch": "search"}.get(tool, f"use {tool} on")
            return f"Claude needs your OK in {where}: {verb} “{what[:80]}”"
        return f"Claude needs your OK in {where}: {msg[:100] or 'a permission prompt is open'}"

    def _finished(self, sid: str, stop_at: float) -> None:
        """DONE_GRACE s after a Stop: alert only if it really ended and it is worth telling you."""
        s = self.sessions.get(sid)
        if not s or s["updated"] > stop_at + 0.5 or s["status"] != "done":
            return                                   # it went on working by itself: not finished
        if not s.get("human"):
            return                                   # nobody typed into this session (background / scripted run)
        took = stop_at - s.get("turn_start", stop_at)
        if took < MIN_TURN:
            return                                   # a quick answer: you were watching it come in
        try:
            if (self.foreground() or "").lower() in TERMINALS:
                return                               # you are already looking at a terminal / editor
        except Exception:
            pass
        if self.overlay:
            asked = f": “{s['asked']}”" if s.get("asked") else ""
            self.overlay.alert(f"claude-done-{sid}", f"Claude finished in {s.get('project') or 'a session'} after {_ago(took)}{asked}",
                               level="info", cooldown=20)
