"""SQLite context store, partitioned one file per month.

Design notes
- One DB file per month (events-YYYY-MM.db). Queries hit the current month
  unless a window crosses a boundary, in which case the previous month is
  queried too. Old months can be archived or deleted as a unit.
- A single writer thread with a bounded queue. Sensors never block on disk.
  If the queue fills, frames and title-changes are dropped first; window
  switches and idle events block briefly instead of being lost.
- WAL journal, NORMAL sync: fast and safe enough for an activity log.
- PRAGMA user_version holds the schema version for migrations.
"""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .events import Event, Suggestion

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id    INTEGER PRIMARY KEY,
    ts    REAL NOT NULL,
    kind  TEXT NOT NULL,
    app   TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    data  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts);

CREATE TABLE IF NOT EXISTS frames (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,
    app        TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    phash      TEXT NOT NULL,
    score      INTEGER NOT NULL DEFAULT 0,
    tier       TEXT NOT NULL DEFAULT 'hot',
    image_path TEXT,
    ocr_text   TEXT,
    pinned     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames(ts);
CREATE INDEX IF NOT EXISTS idx_frames_tier ON frames(tier, ts);

CREATE TABLE IF NOT EXISTS suggestions (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    source   TEXT NOT NULL,
    text     TEXT NOT NULL,
    context  TEXT NOT NULL DEFAULT '{}',
    feedback TEXT
);

CREATE TABLE IF NOT EXISTS summaries (
    id       INTEGER PRIMARY KEY,
    period   TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts   REAL NOT NULL,
    text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries ON summaries(period, start_ts);
"""

SCHEMA_V2 = """
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    kind, ref_id UNINDEXED, ts UNINDEXED, app, title, body,
    tokenize='porter unicode61'
);
"""


def month_key(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m")


class Store:
    def __init__(self, data_dir: str | Path, queue_size: int = 2000) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._q: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=queue_size)
        self._lock = threading.RLock()
        self._conns: dict[str, sqlite3.Connection] = {}
        self._stop = threading.Event()
        self._writer = threading.Thread(target=self._writer_loop, name="store-writer", daemon=True)
        self.dropped = 0

    # ---- connections -------------------------------------------------
    def path_for(self, ts: float) -> Path:
        return self.data_dir / f"events-{month_key(ts)}.db"

    def conn_for(self, ts: float | None = None) -> sqlite3.Connection:
        ts = ts or time.time()
        key = month_key(ts)
        with self._lock:
            conn = self._conns.get(key)
            if conn is None:
                conn = sqlite3.connect(self.path_for(ts), check_same_thread=False, isolation_level=None)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA temp_store=MEMORY")
                self._migrate(conn)
                self._conns[key] = conn
            return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        if v < 1:
            conn.executescript(SCHEMA)
        if v < 2:
            conn.executescript(SCHEMA_V2)
            # backfill the search index from existing rows
            conn.execute(
                "INSERT INTO search(kind,ref_id,ts,app,title,body) "
                "SELECT 'frame', id, ts, app, title, COALESCE(ocr_text,'') FROM frames"
            )
            conn.execute(
                "INSERT INTO search(kind,ref_id,ts,app,title,body) "
                "SELECT kind, id, ts, app, title, '' FROM events WHERE kind IN ('window','title','url')"
            )
        if v < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _months_for(self, start_ts: float, end_ts: float) -> list[sqlite3.Connection]:
        """Connections for every month touched by [start_ts, end_ts] that exists on disk (or is current)."""
        out: list[sqlite3.Connection] = []
        seen: set[str] = set()
        for t in (start_ts, end_ts):
            k = month_key(t)
            if k in seen:
                continue
            seen.add(k)
            if k == month_key(time.time()) or self.path_for(t).exists():
                out.append(self.conn_for(t))
        return out

    # ---- writer thread -----------------------------------------------
    def start(self) -> None:
        self.conn_for()
        self._writer.start()

    def stop(self) -> None:
        self._stop.set()
        self._writer.join(timeout=5)
        with self._lock:
            for c in self._conns.values():
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()

    def put_event(self, ev: Event) -> None:
        self._enqueue("event", ev, droppable=ev.kind in ("title",))

    def put_frame(self, row: dict) -> None:
        self._enqueue("frame", row, droppable=True)

    def _enqueue(self, kind: str, obj: Any, droppable: bool) -> None:
        try:
            self._q.put_nowait((kind, obj))
        except queue.Full:
            if droppable:
                self.dropped += 1
                return
            try:
                self._q.put((kind, obj), timeout=2)
            except queue.Full:
                self.dropped += 1

    def flush(self, timeout: float = 5.0) -> None:
        """Block until the queue has drained (tests / shutdown)."""
        deadline = time.time() + timeout
        while not self._q.empty() and time.time() < deadline:
            time.sleep(0.02)

    def _writer_loop(self) -> None:
        batch: list[tuple[str, Any]] = []
        while not self._stop.is_set() or not self._q.empty():
            try:
                batch.append(self._q.get(timeout=1.0))
                while len(batch) < 100:
                    batch.append(self._q.get_nowait())
            except queue.Empty:
                pass
            if batch:
                try:
                    self._flush(batch)
                except Exception:
                    log.exception("store flush failed (%d rows lost)", len(batch))
                batch = []

    def _flush(self, batch: Iterable[tuple[str, Any]]) -> None:
        with self._lock:
            conn = self.conn_for()
            conn.execute("BEGIN")
            try:
                for kind, obj in batch:
                    if kind == "event":
                        cur = conn.execute(
                            "INSERT INTO events(ts,kind,app,title,data) VALUES(?,?,?,?,?)",
                            (obj.ts, obj.kind, obj.app, obj.title, json.dumps(obj.data, ensure_ascii=False)),
                        )
                        if obj.kind in ("window", "url", "clipboard"):
                            body = obj.data.get("preview", "") if obj.kind == "clipboard" else obj.data.get("url", "")
                            conn.execute(
                                "INSERT INTO search(kind,ref_id,ts,app,title,body) VALUES(?,?,?,?,?,?)",
                                (obj.kind, cur.lastrowid, obj.ts, obj.app, obj.title, body or ""),
                            )
                    elif kind == "frame":
                        cur = conn.execute(
                            "INSERT INTO frames(ts,app,title,phash,score,tier,image_path,ocr_text) "
                            "VALUES(:ts,:app,:title,:phash,:score,:tier,:image_path,:ocr_text)",
                            obj,
                        )
                        conn.execute(
                            "INSERT INTO search(kind,ref_id,ts,app,title,body) VALUES(?,?,?,?,?,?)",
                            ("frame", cur.lastrowid, obj["ts"], obj["app"], obj["title"], obj.get("ocr_text") or ""),
                        )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ---- synchronous writes (brain thread, low volume) ----------------
    def add_frame(self, row: dict) -> int:
        """Insert a frame row now (frames are low volume) and return its id, so OCR text can be attached later."""
        with self._lock:
            conn = self.conn_for(row["ts"])
            cur = conn.execute(
                "INSERT INTO frames(ts,app,title,phash,score,tier,image_path,ocr_text) "
                "VALUES(:ts,:app,:title,:phash,:score,:tier,:image_path,:ocr_text)", row,
            )
            fid = int(cur.lastrowid)
            conn.execute("INSERT INTO search(kind,ref_id,ts,app,title,body) VALUES(?,?,?,?,?,?)",
                         ("frame", fid, row["ts"], row["app"], row["title"], row.get("ocr_text") or ""))
            return fid

    def set_frame_text(self, frame_id: int, ts: float, text: str, score_delta: int = 0) -> None:
        with self._lock:
            conn = self.conn_for(ts)
            conn.execute("UPDATE frames SET ocr_text=?, score=MIN(100, MAX(0, score+?)) WHERE id=?", (text, score_delta, frame_id))
            conn.execute("UPDATE search SET body=? WHERE kind='frame' AND ref_id=?", (text, frame_id))

    def add_suggestion(self, s: Suggestion) -> int:
        with self._lock:
            cur = self.conn_for(s.ts).execute(
                "INSERT INTO suggestions(ts,source,text,context) VALUES(?,?,?,?)",
                (s.ts, s.source, s.text, json.dumps(s.context, ensure_ascii=False)),
            )
            s.id = int(cur.lastrowid)
            return s.id

    def set_feedback(self, suggestion_id: int, feedback: str) -> bool:
        with self._lock:
            cur = self.conn_for().execute("UPDATE suggestions SET feedback=? WHERE id=?", (feedback, suggestion_id))
            if cur.rowcount > 0 and feedback == "accept":
                # pin frames that were in context when the suggestion was made (+40 retroactive score)
                row = self.conn_for().execute("SELECT ts FROM suggestions WHERE id=?", (suggestion_id,)).fetchone()
                if row:
                    self.conn_for().execute(
                        "UPDATE frames SET pinned=1, score=MIN(100, score+40) WHERE ts BETWEEN ? AND ?",
                        (row["ts"] - 15 * 60, row["ts"]),
                    )
            return cur.rowcount > 0

    def add_summary(self, period: str, start_ts: float, end_ts: float, text: str) -> None:
        with self._lock:
            self.conn_for(end_ts).execute(
                "INSERT INTO summaries(period,start_ts,end_ts,text) VALUES(?,?,?,?)", (period, start_ts, end_ts, text)
            )

    # ---- reads --------------------------------------------------------
    def _query(self, sql: str, params: tuple, start_ts: float, end_ts: float) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        with self._lock:
            for conn in self._months_for(start_ts, end_ts):
                rows.extend(conn.execute(sql, params).fetchall())
        return rows

    def recent_events(self, minutes: float, kinds: tuple[str, ...] | None = None, now: float | None = None) -> list[sqlite3.Row]:
        now = now or time.time()
        start = now - minutes * 60
        if kinds:
            marks = ",".join("?" * len(kinds))
            q = f"SELECT * FROM events WHERE ts>=? AND ts<=? AND kind IN ({marks}) ORDER BY ts"
            return self._query(q, (start, now, *kinds), start, now)
        return self._query("SELECT * FROM events WHERE ts>=? AND ts<=? ORDER BY ts", (start, now), start, now)

    def recent_frames(self, minutes: float, now: float | None = None) -> list[sqlite3.Row]:
        now = now or time.time()
        start = now - minutes * 60
        return self._query("SELECT * FROM frames WHERE ts>=? AND ts<=? ORDER BY ts", (start, now), start, now)

    def recent_suggestions(self, minutes: float, now: float | None = None) -> list[sqlite3.Row]:
        now = now or time.time()
        start = now - minutes * 60
        return self._query("SELECT * FROM suggestions WHERE ts>=? AND ts<=? ORDER BY ts DESC", (start, now), start, now)

    def suggestion(self, sid: int) -> sqlite3.Row | None:
        now = time.time()
        rows = self._query("SELECT * FROM suggestions WHERE id=?", (sid,), now - 35 * 86400, now)
        return rows[-1] if rows else None   # newest month wins (ids are per month)

    def reopen_suggestion(self, sid: int) -> None:
        with self._lock:
            self.conn_for().execute("UPDATE suggestions SET feedback=NULL WHERE id=?", (sid,))

    def recent_feedback(self, source: str, n: int = 3, days: float = 7) -> list[str | None]:
        """Feedback of the last n suggestions from one source, newest first."""
        now = time.time()
        rows = self._query("SELECT ts, feedback FROM suggestions WHERE source=? AND ts>=? AND text NOT LIKE '%[e2e]%' "
                           "ORDER BY ts DESC LIMIT ?",
                           (source, now - days * 86400, n), now - days * 86400, now)
        return [r["feedback"] for r in sorted(rows, key=lambda r: r["ts"], reverse=True)[:n]]

    def last_suggestion_ts(self, source: str | None = None, now: float | None = None) -> float:
        now = now or time.time()
        start = now - 24 * 3600
        if source:
            rows = self._query("SELECT MAX(ts) AS m FROM suggestions WHERE source=? AND ts>=?", (source, start), start, now)
        else:
            rows = self._query("SELECT MAX(ts) AS m FROM suggestions WHERE ts>=?", (start,), start, now)
        return max((r["m"] or 0.0) for r in rows) if rows else 0.0

    def recent_summaries(self, period: str, limit: int = 12) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn_for().execute(
                "SELECT * FROM summaries WHERE period=? ORDER BY start_ts DESC LIMIT ?", (period, limit)
            ).fetchall()

    def forget_since(self, ts: float) -> dict[str, int]:
        """Hard-delete everything after ts (the 'forget last N minutes' action)."""
        with self._lock:
            conn = self.conn_for()
            paths = [r[0] for r in conn.execute("SELECT image_path FROM frames WHERE ts>=? AND image_path IS NOT NULL", (ts,))]
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass
            e = conn.execute("DELETE FROM events WHERE ts>=?", (ts,)).rowcount
            f = conn.execute("DELETE FROM frames WHERE ts>=?", (ts,)).rowcount
            conn.execute("DELETE FROM search WHERE ts>=?", (ts,))
            return {"events": e, "frames": f, "files": len(paths)}

    def clear_timeline(self) -> dict[str, int]:
        """Delete the whole activity history: events, screenshots (rows + files), search index and the
        summaries built from them, in every monthly database; then VACUUM so nothing stays in free pages.
        Suggestions, TODOs, calendar, memory and settings are kept."""
        import shutil
        self.flush()
        out = {"events": 0, "frames": 0, "files": 0, "summaries": 0, "databases": 0}
        with self._lock:
            for path in sorted(self.data_dir.glob("events-*.db")):
                open_conn = self._conns.get(path.stem.replace("events-", ""))
                conn = open_conn or self._open_path(path)            # autocommit connections
                try:
                    out["events"] += conn.execute("DELETE FROM events").rowcount
                    out["frames"] += conn.execute("DELETE FROM frames").rowcount
                    out["summaries"] += conn.execute("DELETE FROM summaries").rowcount
                    # FTS5 keeps deleted text in its index segments: drop and recreate the index instead
                    conn.execute("DROP TABLE IF EXISTS search")
                    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5("
                                 "kind, ref_id UNINDEXED, ts UNINDEXED, app, title, body, tokenize='porter unicode61')")
                    conn.execute("VACUUM")                            # deleted rows must not linger in free pages
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # ... and in WAL mode VACUUM lands in the -wal first
                    out["databases"] += 1
                finally:
                    if open_conn is None:
                        conn.close()
            frames_dir = self.data_dir / "frames"
            if frames_dir.exists():
                out["files"] = sum(1 for p in frames_dir.rglob("*") if p.is_file())
                shutil.rmtree(frames_dir, ignore_errors=True)
                frames_dir.mkdir(parents=True, exist_ok=True)
        self._stats_cache = None
        return out

    def _open_path(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def frame(self, frame_id: int, ts: float | None = None) -> sqlite3.Row | None:
        with self._lock:
            for conn in self._months_for((ts or time.time()) - 35 * 86400, ts or time.time()):
                r = conn.execute("SELECT * FROM frames WHERE id=?", (frame_id,)).fetchone()
                if r:
                    return r
        return None

    def frames_between(self, start_ts: float, end_ts: float, limit: int = 500) -> list[sqlite3.Row]:
        return self._query(
            "SELECT id,ts,app,title,score,tier,image_path,pinned,(ocr_text IS NOT NULL) AS has_text "
            "FROM frames WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
            (start_ts, end_ts, limit), start_ts, end_ts,
        )

    def events_between(self, start_ts: float, end_ts: float) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM events WHERE ts>=? AND ts<=? ORDER BY ts", (start_ts, end_ts), start_ts, end_ts)

    def suggestions_page(self, limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
        now = time.time()
        return self._query(
            "SELECT * FROM suggestions ORDER BY ts DESC LIMIT ? OFFSET ?", (limit, offset), now - 35 * 86400, now
        )

    def summaries(self, period: str, limit: int = 30) -> list[sqlite3.Row]:
        now = time.time()
        return self._query(
            "SELECT * FROM summaries WHERE period=? ORDER BY start_ts DESC LIMIT ?", (period, limit), now - 35 * 86400, now
        )

    def search(self, q: str, limit: int = 30) -> list[dict[str, Any]]:
        """Full-text search over titles, URLs, clipboard previews and OCR text (current + previous month)."""
        q = q.strip()
        if not q:
            return []
        # quote each term so punctuation in user input cannot break the FTS query
        terms = " ".join('"' + t.replace('"', '') + '"' for t in q.split())
        now = time.time()
        rows = self._query(
            "SELECT kind, ref_id, ts, app, title, snippet(search, 5, '[', ']', ' ... ', 12) AS snip, bm25(search) AS rank "
            "FROM search WHERE search MATCH ? ORDER BY rank LIMIT ?",
            (terms, limit), now - 35 * 86400, now,
        )
        out = [dict(r) for r in rows]
        out.sort(key=lambda r: r["rank"])
        return out[:limit]

    def stats(self) -> dict[str, Any]:
        """Row counts are cached for 30 s: COUNT(*) scans the table and runs under the writer lock."""
        now = time.time()
        cached = getattr(self, "_stats_cache", None)
        if cached and now - cached[0] < 30:
            out = dict(cached[1])
            out["queue"], out["dropped"] = self._q.qsize(), self.dropped
            return out
        out = self._stats_uncached()
        self._stats_cache = (now, out)
        return dict(out)

    def _stats_uncached(self) -> dict[str, Any]:
        with self._lock:
            conn = self.conn_for()
            out: dict[str, Any] = {"month": month_key(time.time()), "queue": self._q.qsize(), "dropped": self.dropped}
            for t in ("events", "frames", "suggestions", "summaries"):
                out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            out["frames_by_tier"] = {r[0]: r[1] for r in conn.execute("SELECT tier, COUNT(*) FROM frames GROUP BY tier")}
            out["db_files"] = sorted(p.name for p in self.data_dir.glob("events-*.db"))
            return out
