"""Suggestion quality: no repeats, no restating the screen, learning from dismissals, less noise; the screenshot mask."""
import tempfile
import time
import types
import unittest

from PIL import Image, ImageDraw

from watcher.brain import rules as R
from watcher.brain.scheduler import Brain, topic_words
from watcher.config import DEFAULTS, deep_merge
from watcher.events import Event, Suggestion
from watcher.planner import Planner
from watcher.sensors.process import is_service
from watcher.sensors.screenshot import _mask_own_windows, new_error_line
from watcher.store import Store
from tests.test_planner import FakeLLM


class SuggestionQuality(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(self.tmp.name)
        self.store.start()
        self.p = Planner(self.tmp.name)

    def tearDown(self):
        self.p.close()
        self.store.stop()
        self.tmp.cleanup()

    def brain(self, reply):
        cfg = deep_merge(DEFAULTS["brain"], {"quiet_when_idle": False, "rules": {r.name: {"enabled": False} for r in R.ALL_RULES}})
        b = Brain(cfg, self.store, lambda s: None)
        b.llm = FakeLLM(text=reply)
        b.gpu = types.SimpleNamespace(status=lambda: None)
        b.planner = self.p
        self.store.put_event(Event(kind="window", ts=time.time() - 900, app="brave.exe", title="Stripe payment webhook tests - Brave"))
        self.store.put_event(Event(kind="window", ts=time.time() - 60, app="code.exe", title="memory.py - watcher-ai - Visual Studio Code"))
        self.store.flush()
        time.sleep(1.2)
        return b

    def old(self, text, feedback="dismiss", source="llm", ago=600):
        sid = self.store.add_suggestion(Suggestion(text=text, source=source, ts=time.time() - ago))
        if feedback:
            self.store.set_feedback(sid, feedback)

    def test_topic_words_ignore_punctuation_and_filler(self):
        self.assertEqual(topic_words("Add a TODO: “review watcher-ai changes in memory.py”?"), {"watcher-ai", "memory.py"})
        self.assertEqual(topic_words("TODO: review watcher-ai changes in memory.py"), {"watcher-ai", "memory.py"})

    def test_restating_the_open_file_is_dropped(self):
        b = self.brain("TODO: review watcher-ai changes in memory.py")
        self.assertIsNone(b.tick(forced=True))
        self.assertIn("restates the current window", b.last_skip_reason)

    def test_repeat_of_a_dismissed_idea_is_dropped_even_reworded(self):
        self.old("Add a TODO: “Write tests for the payment webhook”?", ago=2 * 86400)
        b = self.brain("TODO: payment webhook tests")
        self.assertIsNone(b.tick(forced=True))
        self.assertIn("repeated", b.last_skip_reason)

    def test_todo_like_an_existing_one_is_dropped(self):
        self.p.add_todo("Send invoice to Acme")
        b = self.brain("TODO: Send the Acme invoice")
        self.assertIsNone(b.tick(forced=True))

    def test_deadline_nudge_about_an_existing_todo_is_not_a_new_todo(self):
        self.p.add_todo("Submit OS lab report", due_ts=time.time() + 2 * 3600)
        b = self.brain("TODO: Submit OS lab report is due in 2 h 15 min; you are on memory.py in watcher-ai.")
        s = b.tick(forced=True)
        self.assertEqual(s.context["action"], {"type": "discuss"})
        self.assertTrue(s.text.startswith("Submit OS lab report is due in 2 h"), s.text)

    def test_open_loop_about_what_you_are_still_doing_is_dropped(self):
        b = self.brain("You left watcher-ai at 23:38 and did not come back; pick it up again?")
        self.assertIsNone(b.tick(forced=True))
        self.assertIn("still active", b.last_skip_reason)

    def test_real_open_loop_gets_through(self):
        b = self.brain("You left the Stripe payment webhook tests page and did not come back; pick it up again?")
        self.store.put_event(Event(kind="window", ts=time.time() - 30 * 60, app="brave.exe", title="Stripe payment webhook tests - Brave"))
        b.store.flush()
        s = b.tick(forced=True)
        self.assertIsNotNone(s, b.last_skip_reason)

    def test_copied_prompt_example_is_dropped(self):
        b = self.brain("DBMS assignment is due at 17:00, about 2 h left, and the team sync is at 15:30.")
        self.assertIsNone(b.tick(forced=True))
        self.assertIn("not in the log", b.last_skip_reason)

    def test_new_idea_still_gets_through(self):
        b = self.brain("TODO: Write tests for the payment webhook")
        s = b.tick(forced=True)
        self.assertEqual(s.context["action"], {"type": "add_todo", "title": "Write tests for the payment webhook"})

    def test_prompt_carries_feedback_and_backs_off(self):
        for i in range(3):
            self.old(f"Add a TODO: “thing {i} in file{i}.py”?", ago=3600 - i)
        self.old("“vite dev server” stopped after running for 2 d. Expected?", feedback="accept", source="rule:job_done")
        self.old("[e2e] test plan", source="rule:test")
        b = self.brain("NONE")
        self.assertEqual(b._learned_cooldown("llm", 5), 40)              # 3 dismissed in a row -> 8x
        prompt = b._prompt(types.SimpleNamespace(now=time.time(), current_app="code.exe", current_title="x", current_focus_seconds=60, error_frames=[]))
        self.assertIn("TURNED DOWN", prompt)
        self.assertIn("ACCEPTED", prompt)
        self.assertIn("do NOT propose a TODO", prompt)
        self.assertNotIn("[e2e]", prompt)

    def test_test_suggestions_never_teach(self):
        for i in range(3):
            self.old(f"[e2e] reminder {i}", source="rule:todo_due", ago=100 - i)
        self.assertEqual(self.store.recent_feedback("rule:todo_due"), [])


class NoiseFilters(unittest.TestCase):
    def test_code_is_not_an_error(self):
        for line in ("except ValueError:", "  42  except ValueError as e:", "raise TypeError('x')", "catch (IOException e) {",
                     "● Error in Explorer: “except ValueError:”. Want an explanation?"):
            self.assertIsNone(new_error_line(line, None), line)
        for line in ("ValueError: invalid literal for int()", "npm ERR! code ELIFECYCLE", "error TS2304: Cannot find name 'x'"):
            self.assertIsNotNone(new_error_line(line, None), line)

    def test_daemons_are_not_jobs(self):
        self.assertTrue(is_service("java gradle-daemon-main-9.3.1.jar org.gradle.launcher.daemon"))
        self.assertTrue(is_service("node tsserver.js"))
        self.assertFalse(is_service("vite build in ui"))
        win = types.SimpleNamespace(now=time.time(), current_app="chrome.exe",
                                    jobs_done=[{"ts": time.time() - 5, "app": "java.exe", "runtime_s": 1600,
                                                "label": "java gradle-daemon-main-9.3.1.jar org.gradle.launcher.daemon"}])
        self.assertIsNone(R.JobDone().evaluate(win, None, {}))

    def test_long_overdue_todo_wording(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        store, p = Store(tmp.name), Planner(tmp.name)
        store.start()
        try:
            p.add_todo("SE revision", due_ts=time.time() - 3 * 86400)
            s = R.TodoDue().evaluate(types.SimpleNamespace(now=time.time(), planner=p), store, {})
            self.assertIn("overdue since", s.text)
            store.add_suggestion(s)
            self.assertIsNone(R.TodoDue().evaluate(types.SimpleNamespace(now=time.time() + 5 * 3600, planner=p), store, {}))  # once a day
        finally:
            p.close()
            store.stop()
            tmp.cleanup()


class ScreenshotMask(unittest.TestCase):
    def test_mask_paints_only_the_drawn_part_with_the_background(self):
        img = Image.new("RGB", (400, 200), (30, 30, 30))                 # dark terminal
        ImageDraw.Draw(img).rectangle((150, 0, 249, 29), fill=(255, 255, 255))   # the bar pill
        _mask_own_windows(img, 0, 0, rects=[(150, 0, 250, 30)])
        self.assertEqual(img.getpixel((200, 10)), (30, 30, 30))          # pill gone, background colour
        self.assertEqual(img.getpixel((200, 100)), (30, 30, 30))         # nothing below it touched
        self.assertEqual(img.getcolors(), [(80000, (30, 30, 30))])


if __name__ == "__main__":
    unittest.main()
