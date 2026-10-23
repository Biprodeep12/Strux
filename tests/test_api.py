import tempfile
import time
import unittest

from starlette.testclient import TestClient

from watcher.app import App
from watcher.api.server import create_api
from watcher.brain import summarizer
from watcher.config import DEFAULTS, deep_merge
from watcher.events import Event


def _cfg(tmp: str) -> dict:
    return deep_merge(DEFAULTS, {
        "data_dir": tmp,
        "priority": {"below_normal": False, "eco_qos": False},
        "sensors": {k: {"enabled": False} for k in ("window", "idle", "clipboard", "screenshot", "browser")},
        "server": {"enabled": False},
        "ui": {"tray": False, "toasts": False, "window": False},
        "brain": {"llm": {"enabled": False, "base_url": "http://127.0.0.1:1"}},
    })


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.app = App(_cfg(self.tmp.name))
        self.app.store.start()
        self.app._started = time.time()
        n = time.time()
        for e in [
            Event(kind="window", ts=n - 900, app="code.exe", title="main.py - repo"),
            Event(kind="url", ts=n - 800, app="brave.exe", title="GitHub PR", data={"url": "https://github.com/x/y/pull/1", "domain": "github.com"}),
            Event(kind="window", ts=n - 700, app="brave.exe", title="GitHub PR #1 fix login bug"),
            Event(kind="clipboard", ts=n - 650, app="brave.exe", data={"kind": "text", "len": 20, "hash": "abc", "preview": "npm install recharts"}),
            Event(kind="idle_start", ts=n - 600),
            Event(kind="idle_end", ts=n - 300, data={"idle_seconds": 300}),
            Event(kind="window", ts=n - 200, app="code.exe", title="store.py - repo"),
        ]:
            self.app.store.put_event(e)
        self.app.store.put_frame({"ts": n - 100, "app": "code.exe", "title": "Traceback error", "phash": "0" * 16, "score": 70,
                                  "tier": "hot", "image_path": None, "ocr_text": "ValueError: invalid literal for int()"})
        self.app.store.flush()
        time.sleep(1.2)
        self.client = TestClient(create_api(self.app))

    def tearDown(self):
        import logging
        self.app.store.stop()
        for h in list(logging.getLogger().handlers):   # release watcher.log so the temp dir can be removed
            h.close()
            logging.getLogger().removeHandler(h)
        self.tmp.cleanup()

    def test_status_and_timeline(self):
        st = self.client.get("/api/status").json()
        self.assertEqual(st["events"], 7)
        self.assertEqual(st["frames"], 1)
        tl = self.client.get("/api/timeline?minutes=30").json()
        apps = [s["app"] for s in tl["sessions"]]
        # idle splits the brave session in two: before idle (100 s) and after returning (100 s)
        self.assertEqual(apps, ["code.exe", "brave.exe", "brave.exe", "code.exe"])
        kinds = {m["kind"] for m in tl["markers"]}
        self.assertTrue({"url", "clipboard", "idle_start", "idle_end"} <= kinds)
        self.assertAlmostEqual(tl["sessions"][1]["seconds"], 100, delta=2)
        self.assertAlmostEqual(tl["sessions"][2]["seconds"], 100, delta=2)

    def test_app_totals_exclude_idle(self):
        now = time.time()
        totals = dict(summarizer.app_totals(self.app.store, now - 1000, now))
        self.assertAlmostEqual(totals["brave.exe"], 100 + 100, delta=2)  # 700->600 (idle) + 300->200 (back, still brave)
        self.assertAlmostEqual(totals["code.exe"], 200 + 200, delta=3)   # 900->700 and 200->now

    def test_search(self):
        hits = self.client.get("/api/search?q=login").json()["hits"]
        self.assertTrue(any(h["kind"] == "window" for h in hits))
        hits = self.client.get("/api/search?q=ValueError").json()["hits"]
        self.assertEqual(hits[0]["kind"], "frame")
        self.assertIn("[ValueError", hits[0]["snip"])
        hits = self.client.get("/api/search?q=recharts").json()["hits"]
        self.assertEqual(hits[0]["kind"], "clipboard")
        self.assertEqual(self.client.get("/api/search?q=").json()["hits"], [])
        self.assertEqual(self.client.get('/api/search?q="quoted(weird)').status_code, 200)

    def test_frames_and_image_404(self):
        fr = self.client.get("/api/frames?minutes=30").json()["frames"]
        self.assertEqual(len(fr), 1)
        self.assertEqual(self.client.get(f"/api/frames/{fr[0]['id']}/image").status_code, 404)
        r = self.client.post(f"/api/frames/{fr[0]['id']}/explain", json={}).json()
        self.assertFalse(r["ok"])

    def test_suggestion_feedback_and_controls(self):
        from watcher.events import Suggestion
        sid = self.app.store.add_suggestion(Suggestion(text="t", source="rule:x"))
        self.assertEqual(self.client.post(f"/api/suggestions/{sid}/feedback", json={"value": "accept"}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/suggestions/{sid}/feedback", json={"value": "bad"}).status_code, 400)
        self.assertEqual(self.client.get("/api/suggestions").json()["suggestions"][0]["feedback"], "accept")
        self.assertTrue(self.client.post("/api/control/pause", json={"paused": True}).json()["paused"])
        self.assertTrue(self.app.paused)
        self.client.post("/api/control/pause", json={"paused": False})
        self.assertEqual(self.client.post("/api/control/think", json={}).json(), {"ok": True})

    def test_chat_without_llm(self):
        r = self.client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}).json()
        self.assertFalse(r["ok"])
        self.assertIn("not reachable", r["error"])

    def test_settings_roundtrip_in_memory(self):
        before = self.app.cfg["brain"]["global_cooldown_minutes"]
        # patch config path to the temp dir so the test does not touch the real config.yaml
        import watcher.api.server as srv
        import watcher.config as cm
        from pathlib import Path
        old_root = cm.USER_DIR
        try:
            cm.USER_DIR = Path(self.tmp.name)
            r = self.client.put("/api/settings", json={"brain": {"global_cooldown_minutes": before + 5}})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self.app.cfg["brain"]["global_cooldown_minutes"], before + 5)
            self.assertTrue((Path(self.tmp.name) / "config.yaml").exists())
        finally:
            cm.USER_DIR = old_root


if __name__ == "__main__":
    unittest.main()
