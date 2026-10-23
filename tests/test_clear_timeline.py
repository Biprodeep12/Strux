"""Clear timeline: every month, every screenshot file, nothing left in free pages; TODOs and suggestions stay."""
import tempfile
import time
import unittest
from pathlib import Path

from watcher.events import Event, Suggestion
from watcher.planner import Planner
from watcher.store import Store


class ClearTimeline(unittest.TestCase):
    def test_everything_goes_except_planner_and_suggestions(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        store = Store(tmp.name)
        store.start()
        planner = Planner(tmp.name)
        now = time.time()
        img = Path(tmp.name) / "frames" / "2026-09-01" / "1.webp"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"secret pixels")
        for ts in (now - 40 * 86400, now - 60):                      # last month + this month
            store.put_event(Event(kind="window", ts=ts, app="code.exe", title="SECRET-TITLE-123"))
            store.put_frame({"ts": ts, "app": "code.exe", "title": "SECRET-TITLE-123", "phash": "0" * 16, "score": 50,
                             "tier": "hot", "image_path": str(img), "ocr_text": "SECRET-OCR-456"})
        store.add_summary("hour", now - 3600, now, "SECRET-SUMMARY")
        store.add_suggestion(Suggestion(text="keep me", source="rule:test"))
        planner.add_todo("keep this todo")
        store.flush()
        time.sleep(1.2)
        res = store.clear_timeline()
        self.assertEqual((res["events"], res["frames"], res["files"]), (2, 2, 1))
        self.assertGreaterEqual(res["databases"], 1)
        self.assertEqual(store.events_between(0, now + 10), [])
        self.assertEqual(store.frames_between(0, now + 10), [])
        self.assertEqual(store.search("SECRET"), [])
        self.assertFalse(img.exists())
        for db in Path(tmp.name).glob("events-*.db"):                 # VACUUM: not even in free pages
            raw = db.read_bytes() + (db.with_suffix(".db-wal").read_bytes() if db.with_suffix(".db-wal").exists() else b"")
            self.assertNotIn(b"SECRET-TITLE-123", raw, db.name)
            self.assertNotIn(b"SECRET-OCR-456", raw, db.name)
        self.assertEqual([r["text"] for r in store.suggestions_page(5)], ["keep me"])
        self.assertEqual([t["title"] for t in planner.todos()], ["keep this todo"])
        planner.close()
        store.stop()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
