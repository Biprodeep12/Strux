"""HTTP API for TODOs, calendar, prompts and the chat tool path (fake model)."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta

from starlette.testclient import TestClient

from watcher.api.server import create_api
from watcher.app import App
from watcher.brain import prompts
from tests.test_api import _cfg
from tests.test_planner import FakeLLM


class PlannerApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = _cfg(self.tmp.name)
        self.app = App(cfg)
        self.app.store.start()
        self.sent = []
        self.app.broadcast = lambda m: self.sent.append(m)
        self.app.planner.on_change = lambda kind: self.sent.append({"type": "planner", "kind": kind})
        import watcher.config as config_mod
        self._cfg_path = config_mod.config_path
        config_mod.config_path = lambda: __import__("pathlib").Path(self.tmp.name) / "config.yaml"    # never touch the real file
        self.client = TestClient(create_api(self.app))

    def tearDown(self):
        import watcher.config as config_mod
        config_mod.config_path = self._cfg_path
        prompts.configure({})
        self.app.planner.close()
        self.app.store.stop()
        self.tmp.cleanup()

    def test_todo_endpoints(self):
        c = self.client
        r = c.post("/api/todos", json={"text": "Fix login tomorrow 10am !high #strux"}).json()["todo"]
        tomorrow10 = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0).timestamp()
        self.assertEqual((r["title"], r["priority"], r["project"], r["due_ts"]), ("Fix login", 2, "strux", tomorrow10))
        r2 = c.post("/api/todos", json={"title": "Plain", "due": "in 2 hours", "priority": "low"}).json()["todo"]
        self.assertEqual(r2["priority"], 0)
        self.assertEqual(c.post("/api/todos", json={"title": "x", "due": "whenever"}).status_code, 400)
        self.assertEqual(c.post("/api/todos", json={}).status_code, 400)
        self.assertEqual(c.post("/api/todos", json={"text": "   "}).status_code, 400)
        self.assertEqual(len(c.get("/api/todos").json()["todos"]), 2)
        t = c.patch(f"/api/todos/{r['id']}", json={"title": "Fix login flow", "due": None, "priority": "normal"}).json()["todo"]
        self.assertEqual((t["title"], t["due_ts"], t["priority"]), ("Fix login flow", None, 1))
        self.assertEqual(c.patch(f"/api/todos/{r['id']}", json={"due": "nonsense"}).status_code, 400)
        c.patch(f"/api/todos/{r['id']}", json={"done": True})
        self.assertEqual([x["id"] for x in c.get("/api/todos").json()["todos"]], [r2["id"]])
        self.assertEqual(len(c.get("/api/todos?done=1").json()["todos"]), 2)
        self.assertEqual(c.delete(f"/api/todos/{r2['id']}").json(), {"ok": True})
        self.assertEqual(c.delete(f"/api/todos/{r2['id']}").status_code, 404)
        self.assertEqual(c.patch("/api/todos/9999", json={"title": "x"}).status_code, 404)
        self.assertIn({"type": "planner", "kind": "todos"}, self.sent)

    def test_calendar_endpoints(self):
        c = self.client
        s = time.time() + 7200
        e = c.post("/api/calendar", json={"title": "Review", "start": s, "location": "Zoom"}).json()["event"]
        self.assertEqual(e["end_ts"], s + 3600)
        e2 = c.post("/api/calendar", json={"title": "Lunch", "start": "tomorrow 13:00", "end": "tomorrow 14:00"}).json()["event"]
        self.assertEqual(e2["end_ts"] - e2["start_ts"], 3600)
        self.assertEqual(c.post("/api/calendar", json={"title": "x"}).status_code, 400)
        self.assertEqual(c.post("/api/calendar", json={"title": "x", "start": s, "end": s - 60}).status_code, 400)
        self.assertEqual(c.post("/api/calendar", json={"start": s}).status_code, 400)
        evs = c.get(f"/api/calendar?start={s - 60}&end={s + 60}").json()["events"]
        self.assertEqual([x["id"] for x in evs], [e["id"]])
        self.assertEqual(c.get("/api/calendar?start=abc").status_code, 400)
        self.assertGreaterEqual(len(c.get("/api/calendar").json()["events"]), 1)
        m = c.patch(f"/api/calendar/{e['id']}", json={"start": s + 3600, "end": s + 7200, "title": "Review (moved)"}).json()["event"]
        self.assertEqual((m["title"], m["start_ts"]), ("Review (moved)", s + 3600))
        self.assertEqual(c.delete(f"/api/calendar/{e['id']}").json(), {"ok": True})
        self.assertEqual(c.delete(f"/api/calendar/{e['id']}").status_code, 404)

    def test_prompts_endpoints(self):
        c = self.client
        lst = c.get("/api/prompts").json()["prompts"]
        chat = next(p for p in lst if p["key"] == "chat")
        self.assertFalse(chat["custom"])
        r = c.put("/api/prompts", json={"chat": "Answer like a pirate for {who}."}).json()
        self.assertTrue(next(p for p in r["prompts"] if p["key"] == "chat")["custom"])
        self.assertEqual(prompts.get("chat", who="Sam's"), "Answer like a pirate for Sam's.")       # live, no restart
        self.assertIn("pirate", (self.tmp_path() / "config.yaml").read_text(encoding="utf-8"))      # persisted
        # the settings form must not clobber prompt edits
        self.client.put("/api/settings", json={"prompts": {}, "brain": {"global_cooldown_minutes": 7}})
        self.assertEqual(prompts.get("chat", who="X"), "Answer like a pirate for X.")
        c.put("/api/prompts", json={"chat": None})
        self.assertTrue(prompts.get("chat").startswith("You are Strux"))
        self.assertEqual(c.put("/api/prompts", json={"nope": "x"}).status_code, 400)

    def tmp_path(self):
        import pathlib
        return pathlib.Path(self.tmp.name)

    def test_chat_tool_path_and_plan(self):
        fake = FakeLLM(turns=[[{"name": "add_event", "arguments": {"title": "Call Raj", "start": "tomorrow 15:00"}}]],
                       text="Scheduled for tomorrow 15:00.")
        self.app.brain.llm = fake
        r = self.client.post("/api/chat", json={"messages": [{"role": "user", "content": "schedule a call with Raj tomorrow 3pm"}]}).json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["text"], "Added “Call Raj” · tomorrow 15:00–16:00")
        self.assertEqual([x["kind"] for x in r["changes"]], ["event_added"])
        self.assertEqual(self.app.planner.events_between(0, 1e12)[0]["title"], "Call Raj")
        fake.text = "09:00-10:00 Fix login"
        r = self.client.post("/api/plan").json()
        self.assertEqual((r["ok"], r["text"]), (True, "09:00-10:00 Fix login"))
        self.assertIn("Plan", fake.sent[-1][-1]["content"])


if __name__ == "__main__":
    unittest.main()
