"""The learner: every few hours, turns what Strux saw into a short profile in the 'Learned' Memory block.

DATA (bounded, ~7k chars): what you asked Claude Code (by project), your Strux chat questions with your
👍 / 👎, recent screen looks, and the work log of the last 8 hours, plus the current profile, so facts
that stay true are kept and contradicted ones go. The block is an ordinary Memory block: you see it on
the Memory page, can edit or switch it off (then the learner leaves it alone), and every prompt uses it.

Runs only when the machine is not busy (same guard as suggestions), you are not chatting, and Ollama is up.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime

from . import prompts, worklog

log = logging.getLogger(__name__)

BLOCK = "Learned"
SECRET = re.compile(r"(sk-[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{12,}|\b[\w.+-]+@[\w-]+\.[\w.]+\b|\+?\d[\d\s-]{9,}\d|"
                    r"\b(password|passwd|secret|token|api[_ -]?key)\b\s*[:=])", re.I)
HEDGE = re.compile(r"\b(probably|likely|possibly|might be|seems to|appears to)\b", re.I)


def _short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def gather(knowledge, store, now: float | None = None) -> str:
    now = now or time.time()
    parts = []
    claude = knowledge.notes("claude", since=now - 4 * 86400, limit=80)
    if claude:
        seen, lines = set(), []
        for n in claude:
            p = _short(n["title"], 150)
            if p.lower() in seen or len(p) < 8:
                continue
            seen.add(p.lower())
            lines.append(f"- [{n['project'] or '?'}] {p}")
            if len(lines) >= 40:
                break
        parts.append("Asked Claude Code (newest first):\n" + "\n".join(lines))
    chats = knowledge.notes("chat", since=now - 7 * 86400, limit=30)
    if chats:
        parts.append("Asked Strux:\n" + "\n".join(
            f"- {_short(n['title'], 120)}" + (" (liked the answer)" if n["rating"] > 0 else " (DISLIKED the answer: "
                                              + _short(n['body'], 80) + ")" if n["rating"] < 0 else "") for n in chats[:15]))
    screens = knowledge.notes("screen", since=now - 3 * 86400, limit=6)
    if screens:
        parts.append("On their screen when they asked about it:\n" + "\n".join(f"- {n['project']}: {_short(n['body'], 160)}" for n in screens))
    try:
        log_text = worklog.build(store, now - 8 * 3600, now) or ""
    except Exception:
        log_text = ""
    if log_text:
        parts.append("Work log, last 8 hours:\n" + _short(log_text, 1800).replace(" - ", "\n- "))
    data = "\n\n".join(parts)
    return data[:7000]


def clean(text: str) -> list[str]:
    out = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln.startswith(("- ", "* ", "• ")):
            continue
        ln = "- " + ln[2:].strip().rstrip(".")
        if len(ln) < 10 or SECRET.search(ln) or HEDGE.search(ln):
            continue
        out.append(ln[:200])
        if len(out) >= 12:
            break
    return out


class Learner:
    def __init__(self, cfg: dict, knowledge, memory, store, llm, busy=None, paused=None) -> None:
        self.cfg = cfg
        self.knowledge, self.memory, self.store, self.llm = knowledge, memory, store, llm
        self.busy = busy or (lambda: None)            # -> reason string when the machine is busy
        self.paused = paused or (lambda: False)
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._running = threading.Lock()
        self.state = knowledge.get_meta("learner", {}) or {}

    def start(self) -> None:
        threading.Thread(target=self._loop, name="learner", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()

    def learn_now(self) -> None:
        self._kick.set()

    def _loop(self) -> None:
        if self._stop.wait(600):                        # let the morning start settle first
            return
        while not self._stop.is_set():
            forced = self._kick.is_set()
            self._kick.clear()
            every = float(self.cfg.get("every_hours", 3)) * 3600
            due = time.time() - float(self.state.get("last_run", 0)) >= every
            if (due or forced) and self.cfg.get("enabled", True) and self.cfg.get("profile", True):
                try:
                    self.run(forced=forced)
                except Exception:
                    log.exception("learner failed")
            self._kick.wait(600)

    def _save(self, **kw) -> None:
        self.state.update(kw)
        self.knowledge.set_meta("learner", self.state)

    def block(self) -> dict | None:
        return next((b for b in self.memory.blocks() if b["title"] == BLOCK), None)

    def run(self, forced: bool = False) -> dict:
        if not self._running.acquire(blocking=False):
            return {"ok": False, "status": "already learning"}
        try:
            b = self.block()
            if b is not None and not b["enabled"]:
                self._save(status="the Learned block is switched off", last_try=time.time())
                return {"ok": False, "status": self.state["status"]}
            if not forced:
                if self.paused():
                    return {"ok": False, "status": "paused"}
                reason = self.busy()
                if reason or getattr(self.llm, "_busy", 0):
                    self._save(status=f"waiting: {reason or 'chat in progress'}", last_try=time.time())
                    return {"ok": False, "status": self.state["status"]}
            if not self.llm.reachable():
                self._save(status="Ollama isn't running", last_try=time.time())
                return {"ok": False, "status": self.state["status"]}
            data = gather(self.knowledge, self.store)
            if len(data) < 200:
                self._save(status="not enough to learn from yet", last_try=time.time(), last_run=time.time())
                return {"ok": False, "status": self.state["status"]}
            current = (b["content"] if b else "").strip() or "(empty)"
            system = prompts.get("learn", who=f"{self._name()}'s" if self._name() else "the user's")
            t0 = time.time()
            text = self.llm.chat([{"role": "system", "content": system},
                                  {"role": "user", "content": f"CURRENT profile:\n{current}\n\nDATA ({datetime.now():%a %d %b %H:%M}):\n{data}"}],
                                 max_tokens=420, temperature=0.1)
            lines = clean(text or "")
            if not lines:
                self._save(status=f"the model gave nothing usable ({self.llm.last_error or 'empty'})", last_try=time.time())
                return {"ok": False, "status": self.state["status"]}
            content = "\n".join(lines)
            if b is None:
                self.memory.add(BLOCK, content, source="ai")
            elif b["content"].strip() != content:
                self.memory.update(b["id"], content=content, source="ai")
            self._save(status="ok", last_run=time.time(), last_try=time.time(), facts=len(lines), took=round(time.time() - t0, 1))
            log.info("learner: %d facts in %.1fs", len(lines), time.time() - t0)
            return {"ok": True, "status": "ok", "facts": len(lines)}
        finally:
            self._running.release()

    def _name(self) -> str:
        try:
            return self.store_name()
        except Exception:
            return ""

    store_name = staticmethod(lambda: "")
