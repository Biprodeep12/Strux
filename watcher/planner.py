"""TODOs and calendar events: data/planner.db, one small SQLite file that never rotates.

Everything can write here: you (dashboard), the chat model (tools in brain/agent.py), and
suggestions ("TODO: …" from the model, "add as TODO" actions). Every change calls `on_change`,
which the app turns into a WebSocket "planner" message so open pages refresh.

Times are epoch seconds (local wall clock is applied in the UI / in text for the model).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,   -- never reused: suggestions refer to items by id
    title      TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    due_ts     REAL,
    priority   INTEGER NOT NULL DEFAULT 1,      -- 0 low, 1 normal, 2 high
    project    TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'you',     -- you | ai | suggestion
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    done_ts    REAL
);
CREATE INDEX IF NOT EXISTS todos_open ON todos(done_ts, due_ts);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    start_ts   REAL NOT NULL,
    end_ts     REAL NOT NULL,
    all_day    INTEGER NOT NULL DEFAULT 0,
    location   TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'you',
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_start ON events(start_ts);
CREATE TABLE IF NOT EXISTS todo_files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,                   -- the original file name, for display / download
    mime       TEXT NOT NULL DEFAULT '',
    size       INTEGER NOT NULL,
    created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS todo_files_todo ON todo_files(todo_id);
"""

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_TODO = 30

TODO_FIELDS = {"title", "notes", "due_ts", "priority", "project", "done_ts"}
EVENT_FIELDS = {"title", "start_ts", "end_ts", "all_day", "location", "notes"}
PRIO = {0: "low", 1: "normal", 2: "high"}


def _check_fields(sets: dict, required_text: tuple, times: tuple, texts: tuple) -> None:
    """Raise ValueError for values the database must never get (None / list titles, absurd times)."""
    import math
    for k in required_text:
        if k in sets:
            if not isinstance(sets[k], str) or not sets[k].strip():
                raise ValueError(f"{k} must be non-empty text")
            sets[k] = sets[k].strip()[:300]
    for k in times:
        if k in sets and sets[k] is not None:
            if not isinstance(sets[k], (int, float)) or isinstance(sets[k], bool) or not math.isfinite(sets[k]) \
                    or not 0 < sets[k] <= MAX_EPOCH:
                raise ValueError(f"{k} is not a valid time")
    for k in texts:
        if k in sets:
            if sets[k] is None:
                sets[k] = ""
            if not isinstance(sets[k], str):
                raise ValueError(f"{k} must be text")
            sets[k] = sets[k][:4000]


def _audit(fmt: str, *args) -> None:
    """Every delete / completion goes to watcher.log with the caller, so a vanished item can always be traced."""
    import inspect
    import logging
    who = "?"
    for f in inspect.stack()[2:8]:
        mod = f.frame.f_globals.get("__name__", "")
        if mod.startswith("watcher.") and not mod.endswith("planner"):
            who = f"{mod.rsplit('.', 1)[-1]}.{f.function}"
            break
    logging.getLogger("watcher.planner").info(fmt + " (via %s)", *args, who)


class Planner:
    def __init__(self, data_dir: str | Path, on_change: Callable[[str], None] | None = None) -> None:
        self.path = Path(data_dir) / "planner.db"
        self.files_dir = Path(data_dir) / "todo_files"           # data/todo_files/<todo id>/<file id>-<name>
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._migrate_ids()
        self._db.executescript(SCHEMA)
        self.on_change = on_change or (lambda kind: None)

    def _migrate_ids(self) -> None:
        """v1 tables used plain INTEGER PRIMARY KEY, which reuses the id of a deleted last row; a new
        TODO could then inherit an old one's id (and its 'already reminded' state). Rebuild with AUTOINCREMENT."""
        for table in ("todos", "events"):
            row = self._db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if row and "AUTOINCREMENT" not in row[0].upper():
                cols = [r[1] for r in self._db.execute(f"PRAGMA table_info({table})")]
                self._db.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")
                self._db.executescript(SCHEMA)
                self._db.execute(f"INSERT INTO {table}({','.join(cols)}) SELECT {','.join(cols)} FROM {table}_v1")
                self._db.execute(f"DROP TABLE {table}_v1")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _changed(self, kind: str) -> None:
        try:
            self.on_change(kind)
        except Exception:
            pass

    # ---- todos ---------------------------------------------------------------------------
    def add_todo(self, title: str, *, notes: str = "", due_ts: float | None = None, priority: int = 1,
                 project: str = "", source: str = "you") -> dict:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title required")
        title = title.strip()
        if due_ts is not None:
            _check_fields({"due_ts": due_ts}, (), ("due_ts",), ())
        notes, project = (notes if isinstance(notes, str) else ""), (project if isinstance(project, str) else "")
        now = time.time()
        with self._lock:
            # the model sometimes proposes the same task twice: reuse an identical open one (same title, same day)
            dup = None
            for r in self._db.execute("SELECT * FROM todos WHERE done_ts IS NULL AND lower(title)=lower(?)", (title,)).fetchall():
                if due_ts is None or _same_day(r["due_ts"], due_ts):     # undated re-add of an open title = duplicate
                    dup = r
                    break
            if dup:
                return {**dict(dup), "_existing": True}
            cur = self._db.execute(
                "INSERT INTO todos(title,notes,due_ts,priority,project,source,created_ts,updated_ts) VALUES(?,?,?,?,?,?,?,?)",
                (title[:300], notes[:4000], due_ts, max(0, min(2, int(priority))), project[:80], source, now, now))
            row = self._db.execute("SELECT * FROM todos WHERE id=?", (cur.lastrowid,)).fetchone()
        self._changed("todos")
        return dict(row)

    def update_todo(self, tid: int, **fields: Any) -> dict | None:
        sets = {k: v for k, v in fields.items() if k in TODO_FIELDS}
        _check_fields(sets, ("title",), ("due_ts", "done_ts"), ("notes", "project"))
        if "done" in fields:
            sets["done_ts"] = time.time() if fields["done"] else None
        if "priority" in sets:
            sets["priority"] = max(0, min(2, int(sets["priority"])))
        with self._lock:
            if sets:
                cols = ", ".join(f"{k}=?" for k in sets)
                self._db.execute(f"UPDATE todos SET {cols}, updated_ts=? WHERE id=?", (*sets.values(), time.time(), tid))
            row = self._db.execute("SELECT * FROM todos WHERE id=?", (tid,)).fetchone()
        if row and sets:
            self._changed("todos")
            if "done_ts" in sets:
                _audit("TODO #%s %r marked %s", tid, row["title"], "done" if sets["done_ts"] else "not done")
        return dict(row) if row else None

    def delete_todo(self, tid: int) -> bool:
        with self._lock:
            row = self._db.execute("SELECT title FROM todos WHERE id=?", (tid,)).fetchone()
            n = self._db.execute("DELETE FROM todos WHERE id=?", (tid,)).rowcount
            if n:
                self._db.execute("DELETE FROM todo_files WHERE todo_id=?", (tid,))
        if n:
            import shutil
            shutil.rmtree(self.files_dir / str(int(tid)), ignore_errors=True)
            _audit("deleted TODO #%s %r", tid, row["title"] if row else "?")
        if n:
            self._changed("todos")
        return n > 0

    def todos(self, include_done: bool = False, done_since: float | None = None, limit: int = 300) -> list[dict]:
        """Open TODOs (overdue / soonest due first, then high priority, then newest); optionally recent done ones."""
        with self._lock:
            files = "(SELECT COUNT(*) FROM todo_files f WHERE f.todo_id=todos.id) AS files"
            rows = self._db.execute(
                f"SELECT *, {files} FROM todos WHERE done_ts IS NULL ORDER BY due_ts IS NULL, due_ts, priority DESC, created_ts DESC LIMIT ?",
                (limit,)).fetchall()
            if include_done:
                rows += self._db.execute(f"SELECT *, {files} FROM todos WHERE done_ts IS NOT NULL AND done_ts>=? ORDER BY done_ts DESC LIMIT ?",
                                         (done_since or 0, limit)).fetchall()
        return [dict(r) for r in rows]

    # ---- files under a TODO ----------------------------------------------------------------
    def add_file(self, tid: int, name: str, data: bytes, mime: str = "") -> dict:
        """Keep a copy of a file with a TODO (notes, a PDF to read, a screenshot of the bug)."""
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("empty file")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        name = _safe_name(name)
        with self._lock:
            if not self._db.execute("SELECT 1 FROM todos WHERE id=?", (tid,)).fetchone():
                raise KeyError("TODO not found")
            if self._db.execute("SELECT COUNT(*) FROM todo_files WHERE todo_id=?", (tid,)).fetchone()[0] >= MAX_FILES_PER_TODO:
                raise ValueError(f"a TODO can hold at most {MAX_FILES_PER_TODO} files")
            cur = self._db.execute("INSERT INTO todo_files(todo_id,name,mime,size,created_ts) VALUES(?,?,?,?,?)",
                                   (tid, name, str(mime or "")[:100], len(data), time.time()))
            fid = cur.lastrowid
            path = self.file_path({"id": fid, "todo_id": tid, "name": name})
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bytes(data))
            except OSError:
                self._db.execute("DELETE FROM todo_files WHERE id=?", (fid,))
                raise
            self._db.execute("UPDATE todos SET updated_ts=? WHERE id=?", (time.time(), tid))
            row = self._db.execute("SELECT * FROM todo_files WHERE id=?", (fid,)).fetchone()
        self._changed("todos")
        return dict(row)

    def files(self, tid: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM todo_files WHERE todo_id=? ORDER BY created_ts", (tid,))]

    def file(self, fid: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM todo_files WHERE id=?", (fid,)).fetchone()
        return dict(r) if r else None

    def file_path(self, f: dict) -> Path:
        return self.files_dir / str(int(f["todo_id"])) / f"{int(f['id'])}-{_safe_name(f['name'])}"

    def delete_file(self, fid: int) -> bool:
        f = self.file(fid)
        if not f:
            return False
        with self._lock:
            self._db.execute("DELETE FROM todo_files WHERE id=?", (fid,))
        try:
            self.file_path(f).unlink()
        except OSError:
            pass
        _audit("deleted file #%s %r from TODO #%s", fid, f["name"], f["todo_id"])
        self._changed("todos")
        return True

    def find_todo(self, query: str) -> dict | None:
        """An open TODO by id ("12" / "#12") or by words in its title."""
        q = (str(query) if query is not None else "").strip()
        import re as _re
        lead = _re.match(r"#\s*(\d+)\b", q)                 # "#108 renew the domain" / "#108: …" -> id 108
        if lead:
            q = lead.group(1)
        q = q.lstrip("#").strip()
        if not q or (not q.isdigit() and len(q) < 3):
            return None                                   # never "whatever matches nothing in particular"
        with self._lock:
            if q.isdigit():
                r = self._db.execute("SELECT * FROM todos WHERE id=?", (int(q),)).fetchone()
                return dict(r) if r else None
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            r = self._db.execute("SELECT * FROM todos WHERE done_ts IS NULL AND title LIKE ? ESCAPE '\\' ORDER BY created_ts DESC",
                                 (f"%{esc}%",)).fetchone()
        return dict(r) if r else None

    # ---- events --------------------------------------------------------------------------
    def add_event(self, title: str, start_ts: float, end_ts: float | None = None, *, all_day: bool = False,
                  location: str = "", notes: str = "", source: str = "you") -> dict:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title required")
        title = title.strip()
        _check_fields({"start_ts": start_ts, **({"end_ts": end_ts} if end_ts else {})}, (), ("start_ts", "end_ts"), ())
        location, notes = (location if isinstance(location, str) else ""), (notes if isinstance(notes, str) else "")
        start_ts = float(start_ts)
        end_ts = float(end_ts) if end_ts else start_ts + (86400 if all_day else 3600)
        if end_ts < start_ts:
            raise ValueError("end before start")
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events(title,start_ts,end_ts,all_day,location,notes,source,created_ts,updated_ts) VALUES(?,?,?,?,?,?,?,?,?)",
                (title[:300], start_ts, end_ts, int(bool(all_day)), location[:200], notes[:4000], source, now, now))
            row = self._db.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone()
        self._changed("events")
        return dict(row)

    def update_event(self, eid: int, **fields: Any) -> dict | None:
        sets = {k: v for k, v in fields.items() if k in EVENT_FIELDS}
        _check_fields(sets, ("title",), ("start_ts", "end_ts"), ("notes", "location"))
        if "start_ts" in sets or "end_ts" in sets:
            with self._lock:
                cur = self._db.execute("SELECT start_ts, end_ts FROM events WHERE id=?", (eid,)).fetchone()
            if cur and sets.get("end_ts", cur["end_ts"]) < sets.get("start_ts", cur["start_ts"]):
                raise ValueError("end is before start")
        with self._lock:
            if sets:
                cols = ", ".join(f"{k}=?" for k in sets)
                self._db.execute(f"UPDATE events SET {cols}, updated_ts=? WHERE id=?", (*sets.values(), time.time(), eid))
            row = self._db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if row and sets:
            self._changed("events")
        return dict(row) if row else None

    def delete_event(self, eid: int) -> bool:
        with self._lock:
            row = self._db.execute("SELECT title FROM events WHERE id=?", (eid,)).fetchone()
            n = self._db.execute("DELETE FROM events WHERE id=?", (eid,)).rowcount
        if n:
            _audit("deleted event #%s %r", eid, row["title"] if row else "?")
        if n:
            self._changed("events")
        return n > 0

    def find_event(self, query: str, now: float | None = None) -> dict | None:
        """An event by id ("12" / "#12") or by words in its title (the soonest upcoming match wins)."""
        q = (str(query) if query is not None else "").strip()
        import re as _re
        lead = _re.match(r"#\s*(\d+)\b", q)
        if lead:
            q = lead.group(1)
        q = q.lstrip("#").strip()
        now = now or time.time()
        if not q:
            return None
        with self._lock:
            if q.isdigit():
                r = self._db.execute("SELECT * FROM events WHERE id=?", (int(q),)).fetchone()
                return dict(r) if r else None
            words = [w for w in q.lower().replace("[", " ").replace("]", " ").split() if len(w) > 2 and w not in ("the", "with", "event", "meeting")]
            rows = self._db.execute("SELECT * FROM events WHERE end_ts>=? ORDER BY start_ts", (now - 86400,)).fetchall()
        best, score = None, 0
        for r in rows:
            t = r["title"].lower()
            n = sum(1 for w in words if w in t)
            if n > score:
                best, score = dict(r), n
        return best if score and score >= max(1, len(words) // 2) else None

    def events_between(self, start: float, end: float) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM events WHERE start_ts<? AND end_ts>? ORDER BY start_ts", (end, start)).fetchall()
        return [dict(r) for r in rows]

    # ---- for the models --------------------------------------------------------------------
    def context_text(self, now: float | None = None, days: int = 3, ids: bool = True) -> str:
        """Compact, bounded view of open TODOs and the next few days, for prompts.
        `ids=False` for prompts without tools, so answers don't quote '#12'."""
        now = now or time.time()
        tag = (lambda i: f"#{i} ") if ids else (lambda i: "")
        lines = [f"Now: {fmt_when(now, now)}"]
        todos = self.todos()[:25]
        if todos:
            lines.append("Open TODOs (" + ("#id " if ids else "") + "title · due · priority · project):")
            for t in todos:
                bits = [f"{tag(t['id'])}{t['title']}"]
                if t["due_ts"]:
                    bits.append(("OVERDUE " if t["due_ts"] < now else "due ") + fmt_when(t["due_ts"], now) + _left(t["due_ts"], now))
                if t["priority"] != 1:
                    bits.append(PRIO[t["priority"]])
                if t["project"]:
                    bits.append(t["project"])
                if t["notes"].strip():
                    note = " ".join(t["notes"].split())
                    bits.append("notes: " + (note if len(note) <= 80 else note[:77] + "…"))
                if t.get("files"):
                    bits.append(f"{t['files']} file{'s' if t['files'] != 1 else ''} attached")
                lines.append("- " + " · ".join(bits))
        else:
            lines.append("Open TODOs: none")
        day0 = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        evs = self.events_between(day0.timestamp(), (day0 + timedelta(days=days + 1)).timestamp())
        if evs:
            lines.append(f"Calendar (today and next {days} days):")
            for e in evs:
                when = "all day " + fmt_day(e["start_ts"], now) if e["all_day"] else \
                    f"{fmt_when(e['start_ts'], now)}–{datetime.fromtimestamp(e['end_ts']).strftime('%H:%M')}"
                lines.append(f"- {tag(e['id'])}{e['title']} · {when}" + ("" if e["all_day"] else _left(e["start_ts"], now))
                             + (f" · {e['location']}" if e["location"] else ""))
        else:
            lines.append("Calendar: nothing scheduled in the next few days")
        return "\n".join(lines)


def _left(ts: float, now: float) -> str:
    """' (in 2 h 15 min)' for things in the next 12 hours, so the models never do time arithmetic."""
    secs = ts - now
    if not 0 < secs <= 12 * 3600:
        return ""
    m = int(round(secs / 60))
    return f" (in {m} min)" if m < 60 else f" (in {m // 60} h {m % 60} min)" if m % 60 else f" (in {m // 60} h)"


def _safe_name(name: str) -> str:
    """A file name that is safe on disk: no folders, no reserved characters, never empty."""
    import re as _re
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _re.sub(r'[<>:"|?*\x00-\x1f]', "_", base).strip(" .")[:120]
    return base or "file"


def _same_day(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return datetime.fromtimestamp(a).date() == datetime.fromtimestamp(b).date()


def fmt_day(ts: float, now: float) -> str:
    d = datetime.fromtimestamp(ts).date()
    today = datetime.fromtimestamp(now).date()
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    if d == today - timedelta(days=1):
        return "yesterday"
    return datetime.fromtimestamp(ts).strftime("%a %d %b")


def fmt_when(ts: float, now: float) -> str:
    return f"{fmt_day(ts, now)} {datetime.fromtimestamp(ts).strftime('%H:%M')}"


MAX_EPOCH = 4102444800.0                       # 2100-01-01: anything past that is a mistake, not a plan


def _epoch(v: float) -> float | None:
    """A number as a timestamp (seconds, or milliseconds from JavaScript); None if absurd."""
    import math
    if not math.isfinite(v):
        return None
    if v > 1e11:
        v /= 1000
    return v if 0 < v <= MAX_EPOCH else None


def parse_when(value: Any, now: float | None = None) -> float | None:
    """Model / API input -> epoch. Accepts epoch numbers, ISO 'YYYY-MM-DD[THH:MM]', 'HH:MM' (today, or
    tomorrow if passed), 'today'/'tomorrow' [HH:MM], 'in N minutes/hours/days'. None if not understood."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _epoch(float(value))
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if len(s) > 80:
        return None
    import re as _re
    # the chat model copies Strux's date hints: "[Fri 23 Oct 2026] 11:00", "Friday, 23 October 2026 at 5pm"
    s = _re.sub(r"[\[\]=,]", " ", s)
    s = _re.sub(r"^\s*(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\s+(?=\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", "", s)
    s = _re.sub(r"\s+", " ", s).strip()
    now = now or time.time()
    base = datetime.fromtimestamp(now)
    try:
        return _epoch(float(s))
    except ValueError:
        pass
    import re
    m = re.fullmatch(r"in\s+(\d+)\s*(min|mins|minutes?|h|hrs?|hours?|d|days?)", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        return now + n * {"m": 60, "h": 3600, "d": 86400}[unit]
    m = re.fullmatch(r"(today|tomorrow|tonight)?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if m and (m.group(1) or m.group(4) or m.group(3)):
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if m.group(4) == "pm" and h < 12:
            h += 12
        if m.group(4) == "am" and h == 12:
            h = 0
        if m.group(1) == "tonight" and h < 12:
            h += 12
        if h > 23 or mi > 59:
            return None
        d = base.replace(hour=h, minute=mi, second=0, microsecond=0)
        if m.group(1) == "tomorrow" or (not m.group(1) and d.timestamp() < now):
            d += timedelta(days=1)
        return d.timestamp()
    # "21 Oct", "today 21 Oct", "Oct 21 5pm", "21 October 2026 17:00", "21/10", "on 21/10 at 9" (the model writes these)
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    tm = r"(?:\s*,?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?"
    s2 = re.sub(r"^(?:today|tomorrow|on|by|due)\s+(?=\d|[a-z]{3})", "", s)
    m = (re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})[a-z]*\.?(?:\s+(\d{4}))?" + tm, s2)
         or re.fullmatch(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?" + tm, s2)
         or re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?" + tm, s2))
    if m:
        a, b, year = m.group(1), m.group(2), m.group(3)
        if a.isdigit() and b.isdigit():
            day, mon = int(a), int(b)                          # dd/mm (India / UK order)
        elif a.isdigit():
            day, mon = int(a), (months.index(b) + 1 if b in months else 0)
        else:
            day, mon = int(b), (months.index(a) + 1 if a in months else 0)
        if not (1 <= mon <= 12 and 1 <= day <= 31):
            return None
        yr = int(year) + (2000 if year and len(year) == 2 else 0) if year else base.year
        h, mi = (int(m.group(4)), int(m.group(5) or 0)) if m.group(4) else (None, 0)
        if h is not None and m.group(6) == "pm" and h < 12:
            h += 12
        try:
            d = datetime(yr, mon, day, h if h is not None else 9, mi)
        except ValueError:
            return None
        if h is None and d.date() == base.date() and d.timestamp() < now:
            d = d.replace(hour=18 if base.hour < 18 else 23, minute=0 if base.hour < 18 else 30)   # today, no time: end of day
        if not year and d.date() < base.date() - timedelta(days=60):
            d = d.replace(year=d.year + 1)                     # "5 Jan" in October means next January
        return d.timestamp()
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    m = re.fullmatch(r"(?:next\s+)?(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s*(?:at\s*)?(?:(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?", s)
    if m:
        target = [d[:3] for d in days].index(m.group(1))
        ahead = (target - base.weekday()) % 7 or 7
        h, mi = int(m.group(2) or 9), int(m.group(3) or 0)
        if m.group(4) == "pm" and h < 12:
            h += 12
        if h > 23 or mi > 59:
            return None
        return (base + timedelta(days=ahead)).replace(hour=h, minute=mi, second=0, microsecond=0).timestamp()
    if s in ("today", "tonight"):
        return base.replace(hour=18 if s == "today" else 21, minute=0, second=0, microsecond=0).timestamp()
    if s == "tomorrow":
        return (base + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s.upper().replace("Z", "") if "T" in s.upper() else s, fmt)
            if fmt == "%Y-%m-%d":
                d = d.replace(hour=9)
            return d.timestamp()
        except ValueError:
            continue
    return None


_DUE_TAIL = None


def quick_parse(text: str, now: float | None = None) -> dict:
    """'Fix login bug tomorrow 10am !high #watcher-ai' -> {title, due_ts, priority, project}.

    !high / !! / !low set priority, #word sets the project, and a time phrase at the end
    (today / tomorrow / tonight / weekday [at] H[:MM][am|pm], 'at 14:30', 'in 2 hours',
    'YYYY-MM-DD [HH:MM]', optionally after 'by' / 'due' / 'on') sets the due time."""
    import re
    global _DUE_TAIL
    if _DUE_TAIL is None:
        when = (r"(?:today|tomorrow|tonight|(?:next\s+)?(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*)(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?"
                r"|(?:at\s+)?\d{1,2}(?::\d{2})\s*(?:am|pm)?|(?:at\s+)?\d{1,2}\s*(?:am|pm)"
                r"|in\s+\d+\s*(?:min|mins|minutes?|h|hrs?|hours?|d|days?)|\d{4}-\d{2}-\d{2}(?:[ t]\d{1,2}:\d{2})?")
        _DUE_TAIL = re.compile(rf"\s+(?:(?:by|due|on)\s+)?({when})\s*$", re.I)
    t = " " + (text or "").strip() + " "
    prio = 1
    if re.search(r"\s(!high|!!|!h)\s", t, re.I):
        prio = 2
    elif re.search(r"\s(!low|!l)\s", t, re.I):
        prio = 0
    t = re.sub(r"\s(!high|!!|!h|!low|!l|!normal)(?=\s)", " ", t, flags=re.I)
    project = ""
    m = re.search(r"\s#([A-Za-z][\w.-]*)(?=\s)", t)          # '#12' (an issue number) is not a project
    if m:
        project = m.group(1)
        t = t[:m.start()] + t[m.end():]
    t = re.sub(r"\s+", " ", t).strip()
    due = None
    m = _DUE_TAIL.search(" " + t)
    if m:
        phrase = re.sub(r"^at\s+", "", m.group(1).strip(), flags=re.I)
        due = parse_when(phrase, now)
        rest = (" " + t)[:m.start()].strip()
        if due is not None and rest:                  # "tomorrow" alone stays a title, not an empty TODO
            t = rest
        elif not rest:
            due = None
    return {"title": t, "due_ts": due, "priority": prio, "project": project}
