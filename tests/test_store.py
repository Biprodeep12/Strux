import tempfile
import time
import unittest
from pathlib import Path

from watcher.events import Event, Suggestion
from watcher.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.start()

    def tearDown(self):
        self.store.stop()
        self.tmp.cleanup()

    def test_events_roundtrip(self):
        now = time.time()
        self.store.put_event(Event(kind="window", ts=now - 10, app="code.exe", title="main.py"))
        self.store.put_event(Event(kind="clipboard", ts=now - 5, app="code.exe", data={"hash": "abc", "len": 3}))
        self.store.flush()
        time.sleep(1.2)  # writer batches every ~1s
        rows = self.store.recent_events(1)
        self.assertEqual([r["kind"] for r in rows], ["window", "clipboard"])
        self.assertEqual(self.store.stats()["events"], 2)

    def test_frames_and_forget(self):
        now = time.time()
        img = Path(self.tmp.name) / "x.webp"
        img.write_bytes(b"xx")
        self.store.put_frame({"ts": now, "app": "a", "title": "t", "phash": "0" * 16, "score": 50,
                              "tier": "hot", "image_path": str(img), "ocr_text": None})
        self.store.flush()
        time.sleep(1.2)
        self.assertEqual(len(self.store.recent_frames(1)), 1)
        res = self.store.forget_since(now - 1)
        self.assertEqual(res["frames"], 1)
        self.assertFalse(img.exists())

    def test_suggestion_feedback_pins_frames(self):
        now = time.time()
        self.store.put_frame({"ts": now - 60, "app": "a", "title": "t", "phash": "0" * 16, "score": 10,
                              "tier": "hot", "image_path": None, "ocr_text": None})
        self.store.flush()
        time.sleep(1.2)
        sid = self.store.add_suggestion(Suggestion(text="hi", source="rule:x", ts=now))
        self.assertTrue(self.store.set_feedback(sid, "accept"))
        f = self.store.recent_frames(5)[0]
        self.assertEqual(f["pinned"], 1)
        self.assertEqual(f["score"], 50)
        self.assertAlmostEqual(self.store.last_suggestion_ts("rule:x"), now, places=2)
        self.assertEqual(self.store.last_suggestion_ts("rule:other"), 0.0)

    def test_monthly_partition_path(self):
        p = self.store.path_for(time.time())
        self.assertTrue(p.name.startswith("events-") and p.name.endswith(".db"))


if __name__ == "__main__":
    unittest.main()
