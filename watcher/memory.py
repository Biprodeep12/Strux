"""Memory: what Strux knows about you, as editable blocks (data/memory.db, never rotated or cleared with the timeline).

A block is {title, content, enabled}. Enabled blocks go into every model prompt (chat, plan, suggestions,
recap, files / images) as "What you know about the user", so the AI can use your name, role, projects,
preferences and standing instructions. You edit them on the Memory page; the chat can add to them when
you say "remember that …" (tools in brain/agent.py), always into a block you can see and change.

Bounded: each block <= 4000 chars, and at most MAX_CONTEXT chars of memory go into a prompt.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    position   INTEGER NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT 'you',      -- you | ai (last edited by)
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
"""
MAX_BLOCK = 4000
MAX_CONTEXT = 4000
NOTES = "Notes"
DEFAULTS = [
    ("About me", "Who you are: name, role, what you study / work on. e.g. “I'm a CS student; I build apps for SaffronStays.”"),
    ("Preferences", "How you like answers and plans. e.g. “Keep answers short. Plan study blocks of 50 min.”"),
    ("Current focus", "What matters this week. e.g. “SE + Java tests on 23 and 25 Oct.”"),
    (NOTES, ""),
]


class Memory:
    def __init__(self, data_dir: str | Path, on_change: Callable[[], None] | None = None, name: str = "") -> None:
        self.path = Path(data_dir) / "memory.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self.on_change = on_change or (lambda: None)
        if not self._db.execute("SELECT 1 FROM blocks LIMIT 1").fetchone():
            now = time.time()
            for i, (title, hint) in enumerate(DEFAULTS):
                content = f"Name: {name}" if title == "About me" and name else ""
                self._db.execute("INSERT INTO blocks(title,content,enabled,position,created_ts,updated_ts) VALUES(?,?,1,?,?,?)",
                                 (title, content, i, now, now))
        self.hints = dict(DEFAULTS)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _changed(self) -> None:
        try:
            self.on_change()
        except Exception:
            pass

    # ---- blocks ----------------------------------------------------------------------------------------
    def blocks(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM blocks ORDER BY position, id").fetchall()
        return [{**dict(r), "enabled": bool(r["enabled"]), "hint": self.hints.get(r["title"], "")} for r in rows]

    def get(self, bid: int) -> dict | None:
        return next((b for b in self.blocks() if b["id"] == bid), None)

    def add(self, title: str, content: str = "", source: str = "you") -> dict:
        title = _text(title, "title", 80, required=True)
        content = _text(content, "content", MAX_BLOCK)
        now = time.time()
        with self._lock:
            pos = (self._db.execute("SELECT MAX(position) FROM blocks").fetchone()[0] or 0) + 1
            cur = self._db.execute("INSERT INTO blocks(title,content,position,source,created_ts,updated_ts) VALUES(?,?,?,?,?,?)",
                                   (title, content, pos, source, now, now))
        self._changed()
        return self.get(cur.lastrowid)

    def update(self, bid: int, *, title=None, content=None, enabled=None, source: str = "you") -> dict | None:
        sets = {}
        if title is not None:
            sets["title"] = _text(title, "title", 80, required=True)
        if content is not None:
            sets["content"] = _text(content, "content", MAX_BLOCK)
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be true or false")
            sets["enabled"] = int(enabled)
        if not sets:
            return self.get(bid)
        with self._lock:
            cols = ", ".join(f"{k}=?" for k in sets)
            n = self._db.execute(f"UPDATE blocks SET {cols}, source=?, updated_ts=? WHERE id=?",
                                 (*sets.values(), source, time.time(), bid)).rowcount
        if n:
            self._changed()
        return self.get(bid)

    def delete(self, bid: int) -> bool:
        with self._lock:
            n = self._db.execute("DELETE FROM blocks WHERE id=?", (bid,)).rowcount
        if n:
            self._changed()
        return n > 0

    # ---- used by the chat tools ----------------------------------------------------------------------------
    def remember(self, fact: str, block: str = NOTES) -> tuple[dict, bool]:
        """Append one line to a block (created if missing). Returns (block, added?) — False if already there."""
        fact = _text(fact, "fact", 400, required=True).rstrip(".")
        b = next((x for x in self.blocks() if x["title"].lower() == (block or NOTES).strip().lower()), None)
        if b is None:
            b = self.add(block or NOTES, "", source="ai")
        lines = [ln for ln in b["content"].splitlines() if ln.strip()]
        if any(fact.lower() in ln.lower() for ln in lines):
            return b, False
        content = "\n".join(lines + [f"- {fact}"])
        if len(content) > MAX_BLOCK:
            raise ValueError(f"the {b['title']} block is full; tidy it on the Memory page")
        return self.update(b["id"], content=content, source="ai"), True

    def forget(self, text: str) -> list[str]:
        """Remove lines containing `text` (>= 4 chars) from any block. Returns the removed lines."""
        q = (text or "").strip().lower()
        if len(q) < 4:
            return []
        removed = []
        for b in self.blocks():
            keep = []
            for ln in b["content"].splitlines():
                (removed if q in ln.lower() else keep).append(ln)
            if len(keep) != len(b["content"].splitlines()):
                self.update(b["id"], content="\n".join(keep), source="ai")
        return [r.strip(" -") for r in removed]

    # ---- for prompts ----------------------------------------------------------------------------------------------
    def context_text(self) -> str:
        parts, used = [], 0
        for b in self.blocks():
            c = b["content"].strip()
            if not b["enabled"] or not c or c == self.hints.get(b["title"]):
                continue
            piece = f"[{b['title']}]\n{c}"
            if used + len(piece) > MAX_CONTEXT:
                piece = piece[: max(0, MAX_CONTEXT - used)] + "…"
            parts.append(piece)
            used += len(piece)
            if used >= MAX_CONTEXT:
                break
        if not parts:
            return ""
        return ("What you know about the user (their own Memory; use it, follow their stated preferences, "
                "never contradict it):\n" + "\n\n".join(parts))


def _text(v, field: str, limit: int, required: bool = False) -> str:
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise ValueError(f"{field} must be text")
    v = v.strip() if field == "title" else v
    if required and not v.strip():
        raise ValueError(f"{field} required")
    if len(v) > limit:
        raise ValueError(f"{field} is too long (max {limit} characters)")
    return v
