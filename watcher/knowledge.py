"""Knowledge: what Strux has learned, in data/knowledge.db (kept like Memory; not rotated with the timeline).

Three sources, all local:
  claude   Claude Code turns read from its own transcripts (~/.claude/projects/*/*.jsonl): what you asked,
           Claude's final answer, which files it edited. Synced incrementally (per-file offset), right after a
           Stop hook and every few minutes for recently changed files, so sessions without hooks count too.
  chat     every Strux chat question + answer (also the chat history the pane restores), with your 👍 / 👎
  screen   each "what's on my screen" look: window, what the vision model saw, the OCR text

`relevant(question)` finds past notes for a question (FTS5, OR of the words, best first); the chat puts the
best few into its context, and liked / disliked answers to similar questions steer the next answer.
The learner (brain/learner.py) distils all of it into the "Learned" Memory block.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    key     TEXT UNIQUE NOT NULL,
    source  TEXT NOT NULL,                -- claude | chat | screen
    ts      REAL NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    title   TEXT NOT NULL DEFAULT '',     -- the question / prompt / window
    body    TEXT NOT NULL DEFAULT '',     -- the answer / what was seen
    extra   TEXT NOT NULL DEFAULT '{}',   -- json: files, model, ...
    rating  INTEGER NOT NULL DEFAULT 0    -- chat: 1 liked, -1 disliked
);
CREATE INDEX IF NOT EXISTS notes_src_ts ON notes(source, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(title, body, project, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    conv    TEXT NOT NULL,
    ts      REAL NOT NULL,
    role    TEXT NOT NULL,
    content TEXT NOT NULL,
    meta    TEXT NOT NULL DEFAULT '{}',
    rating  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conv, id);
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, offset INTEGER NOT NULL, mtime REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

CLAUDE_DIR = Path.home() / ".claude" / "projects"
MAX_PROMPT, MAX_ANSWER = 1500, 2000
_LINE_KIND = re.compile(r'"type"\s*:\s*"(user|assistant)"')
_STOP = {"what", "did", "does", "the", "and", "for", "you", "my", "me", "was", "were", "have", "has", "this", "that", "with",
         "about", "is", "are", "can", "please", "how", "why", "when", "where", "which", "who", "today", "yesterday", "tell",
         "show", "give", "any", "there", "its", "it's", "from", "into", "then", "than", "but", "not", "all", "just", "also",
         "screen", "strux", "halo", "claude", "your", "our", "out", "get", "got", "make", "need", "want", "should", "would", "could"}


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", text or "") if w.lower() not in _STOP]


def _short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _iso_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


class Knowledge:
    def __init__(self, data_dir: str | Path, cfg: dict | None = None, claude_dir: Path | None = None) -> None:
        self.cfg = cfg if cfg is not None else {}
        self.claude_dir = Path(claude_dir) if claude_dir else CLAUDE_DIR
        self.path = Path(data_dir) / "knowledge.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._pending: set[str] = set()
        self._thread: threading.Thread | None = None
        self.synced_files = 0

    # ---- lifecycle ---------------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="knowledge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()

    def close(self) -> None:
        self.stop()
        with self._lock:
            self._db.close()

    def _loop(self) -> None:
        from .ocr import set_thread_idle_priority
        set_thread_idle_priority()                     # parsing transcripts never competes with your work
        if self._stop.wait(20):
            return
        days = float(self.cfg.get("backfill_days", 14))
        while not self._stop.is_set():
            try:
                if self.cfg.get("enabled", True) and self.cfg.get("claude", True):
                    with self._lock:
                        pending, self._pending = list(self._pending), set()
                    for p in pending:
                        self.sync_file(Path(p))
                    self.sync_recent(days)
                    days = 1.0                         # after the first pass only recently touched files
            except Exception:
                log.exception("knowledge sync failed")
            self._kick.wait(float(self.cfg.get("sync_minutes", 5)) * 60)
            self._kick.clear()

    def claude_stopped(self, transcript_path: str) -> None:
        """A Claude Code turn just ended (Stop hook): read it now."""
        if transcript_path and self.cfg.get("enabled", True) and self.cfg.get("claude", True):
            with self._lock:
                self._pending.add(transcript_path)
            self._kick.set()

    # ---- meta ----------------------------------------------------------------------------------------------
    def get_meta(self, k: str, default: Any = None) -> Any:
        with self._lock:
            r = self._db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        if not r:
            return default
        try:
            return json.loads(r[0])
        except Exception:
            return default

    def set_meta(self, k: str, v: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(v)))

    # ---- notes ----------------------------------------------------------------------------------------------
    def upsert(self, key: str, source: str, ts: float, title: str, body: str, project: str = "",
               extra: dict | None = None, rating: int | None = None) -> int:
        ex = json.dumps(extra or {}, ensure_ascii=False)
        with self._lock:
            old = self._db.execute("SELECT id, rating FROM notes WHERE key=?", (key,)).fetchone()
            if old:
                self._db.execute("UPDATE notes SET ts=?, project=?, title=?, body=?, extra=?, rating=? WHERE id=?",
                                 (ts, project, title, body, ex, old["rating"] if rating is None else rating, old["id"]))
                self._db.execute("DELETE FROM notes_fts WHERE rowid=?", (old["id"],))
                nid = old["id"]
            else:
                nid = self._db.execute("INSERT INTO notes(key,source,ts,project,title,body,extra,rating) VALUES(?,?,?,?,?,?,?,?)",
                                       (key, source, ts, project, title, body, ex, rating or 0)).lastrowid
            self._db.execute("INSERT INTO notes_fts(rowid,title,body,project) VALUES(?,?,?,?)", (nid, title, body, project))
        return nid

    def notes(self, source: str | None = None, since: float = 0, limit: int = 50) -> list[dict]:
        q, args = "SELECT * FROM notes WHERE ts>=?", [since]
        if source:
            q += " AND source=?"
            args.append(source)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY ts DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._note(r) for r in rows]

    @staticmethod
    def _note(r) -> dict:
        d = dict(r)
        try:
            d["extra"] = json.loads(d.get("extra") or "{}")
        except Exception:
            d["extra"] = {}
        return d

    def search(self, text: str, source: str | None = None, limit: int = 6, since: float = 0) -> list[dict]:
        terms = list(dict.fromkeys(w.lower() for w in words(text)))[:10]
        if not terms:
            return []
        match = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
        q = ("SELECT n.*, bm25(notes_fts) AS rank FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid "
             "WHERE notes_fts MATCH ? AND n.ts >= ?")
        args: list = [match, since]
        if source:
            q += " AND n.source = ?"
            args.append(source)
        try:
            with self._lock:
                rows = self._db.execute(q + " ORDER BY rank LIMIT ?", (*args, limit * 3)).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for r in rows:
            d = self._note(r)
            # a single shared common word is noise: need 2 matching words, or 1 rarer (long) one
            hay = f"{d['title']} {d['body']} {d['project']}".lower()
            hits = [t for t in terms if t in hay]
            if len(hits) >= 2 or any(len(t) >= 6 for t in hits) or len(terms) == 1:
                out.append(d)
            if len(out) >= limit:
                break
        return out

    def relevant(self, question: str, exclude_key: str = "", limit: int = 4) -> str:
        """Past Claude Code turns, chats and screen looks about the same thing, as context lines."""
        if not self.cfg.get("enabled", True) or not self.cfg.get("recall", True):
            return ""
        lines = []
        for n in self.search(question, limit=limit + 2):
            if n["key"] == exclude_key or (n["source"] == "chat" and n["rating"] < 0):
                continue
            when = datetime.fromtimestamp(n["ts"]).strftime("%a %d %b %H:%M")
            if n["source"] == "claude":
                files = n["extra"].get("files") or []
                lines.append(f"- {when} [Claude Code · {n['project'] or '?'}] you asked: “{_short(n['title'], 220)}” → Claude: "
                             f"{_short(n['body'], 380)}" + (f" (edited: {', '.join(Path(f).name for f in files[:5])})" if files else ""))
            elif n["source"] == "chat":
                lines.append(f"- {when} [Strux chat] you asked: “{_short(n['title'], 160)}” → {_short(n['body'], 260)}")
            else:
                lines.append(f"- {when} [your screen · {n['project'] or n['title'][:40]}] {_short(n['body'], 300)}")
            if len(lines) >= limit:
                break
        return ("Related notes from your past Claude Code sessions, chats and screens (older; use only if relevant):\n"
                + "\n".join(lines)) if lines else ""

    def feedback_hint(self, question: str) -> str:
        """Liked / disliked Strux answers to similar questions: learn from them."""
        liked, disliked = [], []
        for n in self.search(question, source="chat", limit=8):
            if n["rating"] > 0 and len(liked) < 1:
                liked.append(n)
            elif n["rating"] < 0 and len(disliked) < 2:
                disliked.append(n)
        parts = []
        if liked:
            n = liked[0]
            parts.append(f"An earlier answer they liked for a similar question (match its style):\nQ: {_short(n['title'], 160)}\nA: {_short(n['body'], 400)}")
        for n in disliked:
            why = n["extra"].get("why")
            parts.append(f"An earlier answer they DISLIKED for a similar question (do not repeat its mistakes"
                         + (f"; they said: {_short(why, 160)}" if why else "") + f"):\nQ: {_short(n['title'], 160)}\nA: {_short(n['body'], 300)}")
        return "\n\n".join(parts)

    def recent_claude(self, start: float, end: float, limit: int = 8) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM notes WHERE source='claude' AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT ?",
                                    (start, end, limit)).fetchall()
        return [self._note(r) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            counts = {r[0]: r[1] for r in self._db.execute("SELECT source, COUNT(*) FROM notes GROUP BY source")}
            projects = [r[0] for r in self._db.execute(
                "SELECT project FROM notes WHERE source='claude' AND project<>'' GROUP BY project ORDER BY MAX(ts) DESC LIMIT 8")]
            rated = {r[0]: r[1] for r in self._db.execute("SELECT rating, COUNT(*) FROM notes WHERE source='chat' AND rating<>0 GROUP BY rating")}
            convs = self._db.execute("SELECT COUNT(DISTINCT conv) FROM messages").fetchone()[0]
        return {"claude": counts.get("claude", 0), "chat": counts.get("chat", 0), "screen": counts.get("screen", 0),
                "projects": projects, "liked": rated.get(1, 0), "disliked": rated.get(-1, 0), "conversations": convs,
                "files": self.synced_files}

    def clear(self, source: str) -> int:
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM notes WHERE source=?", (source,))]
            for i in ids:
                self._db.execute("DELETE FROM notes_fts WHERE rowid=?", (i,))
            self._db.execute("DELETE FROM notes WHERE source=?", (source,))
            if source == "chat":
                self._db.execute("DELETE FROM messages")
            if source == "claude":
                self._db.execute("DELETE FROM files")      # a later sync reads the transcripts again
        return len(ids)

    def forget_since(self, ts: float, source: str = "screen") -> int:
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM notes WHERE source=? AND ts>=?", (source, ts))]
            for i in ids:
                self._db.execute("DELETE FROM notes_fts WHERE rowid=?", (i,))
            self._db.execute(f"DELETE FROM notes WHERE id IN ({','.join('?' * len(ids))})", ids) if ids else None
        return len(ids)

    # ---- chat history -------------------------------------------------------------------------------------
    def add_message(self, conv: str, role: str, content: str, meta: dict | None = None) -> int:
        with self._lock:
            return self._db.execute("INSERT INTO messages(conv,ts,role,content,meta) VALUES(?,?,?,?,?)",
                                    (conv[:64], time.time(), role, content[:20000], json.dumps(meta or {}, ensure_ascii=False))).lastrowid

    def record_chat(self, conv: str, question: str, answer: str, meta: dict | None = None, attachments: list | None = None) -> int:
        """One finished Q&A: kept as history (both messages) and as a note to learn from. Returns the answer's id."""
        conv = (conv or f"c{int(time.time() * 1000)}")[:64]
        self.add_message(conv, "user", question, {"attachments": attachments} if attachments else None)
        mid = self.add_message(conv, "assistant", answer, meta)
        if question.strip():
            self.upsert(f"chat:{mid}", "chat", time.time(), _short(question, 600), _short(answer, 1500),
                        extra={"model": (meta or {}).get("model"), "conv": conv})
        return mid

    def rate(self, mid: int, rating: int, why: str = "") -> bool:
        rating = max(-1, min(1, int(rating)))
        with self._lock:
            n = self._db.execute("UPDATE messages SET rating=? WHERE id=? AND role='assistant'", (rating, mid)).rowcount
            note = self._db.execute("SELECT * FROM notes WHERE key=?", (f"chat:{mid}",)).fetchone()
        if note:
            extra = self._note(note)["extra"]
            if why:
                extra["why"] = why[:300]
            self.upsert(note["key"], "chat", note["ts"], note["title"], note["body"], note["project"], extra, rating)
        return n > 0

    def conversations(self, limit: int = 15) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT conv, MIN(ts) AS started, MAX(ts) AS updated, COUNT(*) AS n, "
                "(SELECT content FROM messages m2 WHERE m2.conv = m.conv AND m2.role='user' ORDER BY id LIMIT 1) AS title "
                "FROM messages m GROUP BY conv ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "title": _short(r["title"] or "(attachment)", 80)} for r in rows]

    def conversation(self, conv: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM messages WHERE conv=? ORDER BY id", (conv,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["meta"] = json.loads(d["meta"] or "{}")
            except Exception:
                d["meta"] = {}
            out.append(d)
        return out

    def delete_conversation(self, conv: str) -> int:
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM messages WHERE conv=? AND role='assistant'", (conv,))]
            for i in ids:
                nid = self._db.execute("SELECT id FROM notes WHERE key=?", (f"chat:{i}",)).fetchone()
                if nid:
                    self._db.execute("DELETE FROM notes_fts WHERE rowid=?", (nid[0],))
                    self._db.execute("DELETE FROM notes WHERE id=?", (nid[0],))
            return self._db.execute("DELETE FROM messages WHERE conv=?", (conv,)).rowcount

    # ---- Claude Code transcripts --------------------------------------------------------------------------------
    def sync_recent(self, days: float) -> int:
        if not self.claude_dir.exists():
            return 0
        cutoff = time.time() - days * 86400
        n = 0
        for f in sorted(self.claude_dir.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            if self._stop.is_set():
                break
            try:
                if f.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            n += self.sync_file(f)
            time.sleep(0.02)
        return n

    def sync_file(self, path: Path) -> int:
        """Read new Claude Code turns from one transcript. Re-reads from the start of the last turn it saw,
        so a turn that was still running last time is completed (upsert by the prompt's id)."""
        path = Path(path)
        try:
            st = path.stat()
        except OSError:
            return 0
        with self._lock:
            row = self._db.execute("SELECT offset, mtime FROM files WHERE path=?", (str(path),)).fetchone()
        offset = row["offset"] if row else 0
        if row and row["mtime"] == st.st_mtime:
            return 0
        if offset > st.st_size:
            offset = 0
        turns, resume = parse_turns(path, offset)
        for t in turns:
            self.upsert(f"claude:{t['key']}", "claude", t["ts"], t["prompt"], t["answer"], t["project"],
                        {"files": t["files"], "session": t["session"]})
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO files(path, offset, mtime) VALUES(?,?,?)", (str(path), resume, st.st_mtime))
        self.synced_files += 1
        return len(turns)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_human_prompt(d: dict) -> bool:
    if d.get("isSidechain") or d.get("isMeta"):
        return False
    m = d.get("message") or {}
    c = m.get("content")
    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
        return False
    origin = (d.get("origin") or {}).get("kind")
    if origin and origin != "human":
        return False
    text = _text_of(c).strip()
    return bool(text) and not text.startswith("<") and not text.startswith("Caveat:")


def _project(cwd: str, files: list[str]) -> str:
    """A session started in a folder of repos (vscodeFiles) is about the repo its edits are in."""
    if not cwd:
        return ""
    base = Path(cwd)
    seen: dict[str, int] = {}
    for f in files:
        try:
            rel = Path(f).relative_to(base)
        except ValueError:
            continue
        if len(rel.parts) > 1:
            seen[rel.parts[0]] = seen.get(rel.parts[0], 0) + 1
    if seen and not (base / ".git").exists():
        return max(seen, key=seen.get)
    return base.name


def parse_turns(path: Path, offset: int = 0) -> tuple[list[dict], int]:
    """(turns, offset to resume from). A turn = a prompt you typed + Claude's text answers + files it edited."""
    turns: list[dict] = []
    cur: dict | None = None
    cur_start = pos = offset
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            for raw in fh:
                line_start, pos = pos, pos + len(raw)
                if b'"type":"user"' not in raw and b'"type":"assistant"' not in raw and not _LINE_KIND.search(raw.decode("utf-8", "ignore")):
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                kind = d.get("type")
                if kind == "user" and _is_human_prompt(d):
                    if cur:
                        turns.append(cur)
                    cur_start = line_start
                    cwd = d.get("cwd") or ""
                    cur = {"key": d.get("promptId") or d.get("uuid") or f"{path.stem}:{line_start}", "ts": _iso_ts(d.get("timestamp", "")),
                           "prompt": _short(_text_of(d["message"].get("content")), MAX_PROMPT), "answer": "", "texts": [],
                           "files": [], "project": Path(cwd).name if cwd else "", "cwd": cwd, "session": (d.get("sessionId") or "")[:12]}
                elif kind == "assistant" and cur is not None and not d.get("isSidechain"):
                    for b in (d.get("message") or {}).get("content") or []:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text" and b.get("text", "").strip():
                            cur["texts"].append(b["text"].strip())
                        elif b.get("type") == "tool_use" and b.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                            fp = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path")
                            if fp and fp not in cur["files"]:
                                cur["files"].append(fp)
    except OSError:
        return [], offset
    if cur:
        turns.append(cur)
    for t in turns:
        texts = t.pop("texts")
        # the last text is Claude's summary for you; earlier ones are progress notes
        ans = texts[-1] if texts else ""
        if len(ans) < 200 and len(texts) > 1:
            ans = texts[-2] + "\n" + ans
        t["answer"] = _short(ans, MAX_ANSWER)
        t["files"] = t["files"][:20]
        t["project"] = _project(t.pop("cwd"), t["files"]) or t["project"]
    turns = [t for t in turns if t["prompt"]]
    return turns, cur_start if cur else pos
