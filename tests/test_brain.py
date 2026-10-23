import tempfile
import time
import unittest

from watcher.brain import summarizer
from watcher.brain.rules import AppSwitchStorm, ErrorOnScreen, LongFocus, WelcomeBack
from watcher.brain.scheduler import Brain
from watcher.config import DEFAULTS, deep_merge
from watcher.events import Event
from watcher.retention import run_once
from watcher.store import Store


class BrainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.start()
        self.now = time.time()

    def tearDown(self):
        self.store.stop()
        self.tmp.cleanup()

    def _seed(self, events):
        for e in events:
            self.store.put_event(e)
        self.store.flush()
        time.sleep(1.2)

    def test_summary_app_seconds_and_switches(self):
        n = self.now
        self._seed([
            Event(kind="window", ts=n - 600, app="code.exe", title="a"),
            Event(kind="window", ts=n - 300, app="chrome.exe", title="b"),
            Event(kind="window", ts=n - 100, app="code.exe", title="c"),
        ])
        w = summarizer.build(self.store, minutes=15, now=n)
        self.assertEqual(w.switches, 3)
        self.assertAlmostEqual(w.app_seconds["code.exe"], 400, delta=2)
        self.assertAlmostEqual(w.app_seconds["chrome.exe"], 200, delta=2)
        self.assertEqual(w.current_app, "code.exe")
        self.assertIn("code.exe", w.as_text())

    def test_rules(self):
        n = self.now
        self._seed([Event(kind="window", ts=n - 10 - i, app=f"app{i % 2}.exe") for i in range(30)]
                   + [Event(kind="frame", ts=n - 20, app="code.exe", title="Error: boom", data={"error": True, "score": 50})]
                   + [Event(kind="idle_end", ts=n - 30, data={"idle_seconds": 1800})])
        w = summarizer.build(self.store, minutes=15, now=n)
        self.assertIsNotNone(AppSwitchStorm().evaluate(w, self.store, {"min_switches": 25}))
        self.assertIsNotNone(ErrorOnScreen().evaluate(w, self.store, {}))
        self.assertIsNotNone(WelcomeBack().evaluate(w, self.store, {"min_idle_minutes": 20}))
        self.assertIsNone(LongFocus().evaluate(w, self.store, {"minutes": 50}))

    def test_long_focus(self):
        n = self.now
        self._seed([Event(kind="window", ts=n - 70 * 60, app="code.exe")])
        w = summarizer.build(self.store, minutes=15, now=n)
        s = LongFocus().evaluate(w, self.store, {"minutes": 50})
        self.assertIsNotNone(s)
        self.assertIn("VS Code", s.text)
        self.assertEqual(s.context["action"]["type"], "break")

    def test_rule_actions_and_self_filter(self):
        n = self.now
        self._seed([Event(kind="frame", ts=n - 20, app="pythonw.exe", title="Strux", data={"error": True, "id": 7}),
                    Event(kind="ocr_error", ts=n - 15, app="", title="Strux", data={"frame_id": 8})])
        w = summarizer.build(self.store, minutes=15, now=n)
        self.assertIsNone(ErrorOnScreen().evaluate(w, self.store, {}))     # Strux's own windows never count
        self._seed([Event(kind="frame", ts=n - 10, app="code.exe", title="Error: boom", data={"error": True, "id": 9}),
                    Event(kind="idle_end", ts=n - 5, data={"idle_seconds": 1800})])
        w = summarizer.build(self.store, minutes=15, now=n)
        e = ErrorOnScreen().evaluate(w, self.store, {})
        self.assertEqual(e.context["action"], {"type": "explain_error", "frame_id": 9, "label": "VS Code"})
        wb = WelcomeBack().evaluate(w, self.store, {"min_idle_minutes": 20})
        self.assertEqual(wb.context["action"]["type"], "recap")
        self.assertLess(wb.context["action"]["end"], n - 1800)

    def test_learned_cooldown_and_snooze(self):
        from watcher.events import Suggestion
        cfg = deep_merge(DEFAULTS["brain"], {"llm": {"enabled": False}, "quiet_when_idle": False, "snooze_minutes": 0})
        got = []
        brain = Brain(cfg, self.store, got.append)
        ids = [self.store.add_suggestion(Suggestion(text=f"t{i}", source="rule:x", ts=self.now - 100 + i)) for i in range(3)]
        self.assertEqual(brain._learned_cooldown("rule:x", 10), 10)
        for i in ids:
            self.store.set_feedback(i, "dismiss")
        self.assertEqual(brain._learned_cooldown("rule:x", 10), 80)
        self.store.set_feedback(ids[-1], "snooze")
        brain.snooze(ids[-1])
        brain._resurface_snoozed(self.now + 1)
        self.assertEqual([s.id for s in got], [ids[-1]])
        self.assertIsNone(self.store.suggestion(ids[-1])["feedback"])

    def test_welcome_back_message(self):
        from watcher.brain import welcome
        n = self.now
        left = n - 1500
        self._seed([
            Event(kind="window", ts=left - 600, app="code.exe", title="app.py - watcher-ai - Visual Studio Code"),
            Event(kind="idle_start", ts=left),
            Event(kind="claude", ts=left + 60, app="claude", data={"event": "Stop", "session": "s1", "project": "watcher-ai"}),
            Event(kind="job_done", ts=left + 120, app="node.exe", data={"runtime_s": 600}),
            Event(kind="job_done", ts=left + 130, app="git.exe", data={"runtime_s": 3}),
        ])
        text = welcome.compose(self.store, 1500, n, "Sam")
        self.assertTrue(text.startswith(("Welcome back, Sam", "Morning, welcome back, Sam", "Still up? Welcome back, Sam")), text)
        self.assertIn("25 min away", text)
        self.assertIn("You were editing app.py in watcher-ai", text)
        self.assertIn("Claude Code finished in watcher-ai", text)
        self.assertIn("Node finished (10 min)", text)
        self.assertNotIn("Git", text)                       # short jobs are noise

    def test_brain_tick_delivers_rule_and_respects_cooldown(self):
        n = self.now
        self._seed([Event(kind="window", ts=n - 10 - i, app=f"app{i % 2}.exe") for i in range(30)])
        cfg = deep_merge(DEFAULTS["brain"], {"llm": {"enabled": False}, "quiet_when_idle": False})
        got = []
        brain = Brain(cfg, self.store, got.append)
        s1 = brain.tick(forced=True)
        self.assertIsNotNone(s1)
        self.assertEqual(s1.source, "rule:app_switch_storm")
        self.assertEqual(len(got), 1)
        s2 = brain.tick(forced=True)   # global cooldown blocks a second one
        self.assertIsNone(s2)

    def test_retention_tiers(self):
        n = self.now
        rows = [
            {"ts": n - 30 * 3600, "app": "a", "title": "", "phash": "0" * 16, "score": 80, "tier": "hot", "image_path": None, "ocr_text": "x"},
            {"ts": n - 30 * 3600, "app": "a", "title": "", "phash": "0" * 16, "score": 20, "tier": "hot", "image_path": None, "ocr_text": "x"},
            {"ts": n - 30 * 3600, "app": "a", "title": "", "phash": "0" * 16, "score": 5, "tier": "hot", "image_path": None, "ocr_text": "x"},
            {"ts": n - 10 * 86400, "app": "a", "title": "", "phash": "0" * 16, "score": 80, "tier": "warm", "image_path": None, "ocr_text": "x"},
        ]
        for r in rows:
            self.store.put_frame(r)
        self.store.flush()
        time.sleep(1.2)
        st = run_once(self.store, DEFAULTS["retention"], now=n)
        self.assertEqual((st["to_warm"], st["to_cold"], st["dropped"]), (1, 2, 1))
        tiers = self.store.stats()["frames_by_tier"]
        self.assertEqual(tiers, {"cold": 2, "dropped": 1, "warm": 1})


if __name__ == "__main__":
    unittest.main()
