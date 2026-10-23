"""The chat only deletes / finishes what you asked it to, every planner request reaches the tools, and
"Hey Strux" leaves your words intact."""
import tempfile
import unittest

from watcher.brain import chat
from watcher.brain.chat import PLANNER_Q
from watcher.planner import Planner
from watcher.wake import command_after, strip_greeting
from tests.test_planner import FakeLLM


class Intent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.p = Planner(self.tmp.name)
        for t in ("Study Java", "Revise SE", "Submit the lab report"):
            self.p.add_todo(t)

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def ask(self, said, calls):
        changes = []
        out = chat.answer(FakeLLM(turns=[calls]), None, [{"role": "user", "content": said}], planner=self.p, changes=changes)
        return out, changes

    def test_making_a_list_never_deletes_or_finishes(self):
        # what the small model actually did: "tidied up" same-named items while making the list
        _, ch = self.ask("create a to do list for tomorrow: study java, revise SE and submit the lab report", [
            {"name": "delete_todo", "arguments": {"todo": "Study Java"}},
            {"name": "complete_todo", "arguments": {"todo": "Revise SE"}},
            {"name": "add_todo", "arguments": {"title": "study java", "due": "tomorrow"}}])
        self.assertNotIn("todo_deleted", [c["kind"] for c in ch])
        self.assertNotIn("todo_done", [c["kind"] for c in ch])
        self.assertEqual(len(self.p.todos()), 4)                 # 3 kept + 1 for tomorrow

    def test_asked_for_it_still_works(self):
        _, ch = self.ask("delete the study java todo", [{"name": "delete_todo", "arguments": {"todo": "Study Java"}}])
        self.assertEqual([c["kind"] for c in ch], ["todo_deleted"])
        _, ch = self.ask("I finished revise SE", [{"name": "complete_todo", "arguments": {"todo": "Revise SE"}}])
        self.assertEqual([c["kind"] for c in ch], ["todo_done"])


class Routing(unittest.TestCase):
    def test_planner_requests_reach_the_tools(self):
        for said in ("a to-do to study Java", "Add a to do. Revise SE chapter 3 tonight.", "add the to do call the bank",
                     "I want to schedule a meet tomorrow at 3", "Make a to do list for tomorrow", "I need to study Java tomorrow, add it",
                     "um add a todo like buy milk", "put finish the DBMS assignment on my list for monday", "what's on my calendar tomorrow",
                     "book a call with the client on friday", "remind me to call mom at 7pm", "cancel the dentist appointment"):
            self.assertTrue(PLANNER_Q.search(said), said)

    def test_ordinary_questions_do_not(self):
        for said in ("explain Python class inheritance", "what is a good task manager app", "What did I do today?",
                     "how do I fix this error"):
            self.assertFalse(PLANNER_Q.search(said), said)


class WakeWords(unittest.TestCase):
    def test_only_the_greeting_is_removed(self):
        self.assertEqual(strip_greeting("Hello, add a to-do to study Java."), "add a to-do to study Java.")
        self.assertEqual(strip_greeting("Strux add a todo"), "add a todo")
        self.assertEqual(strip_greeting("add a todo to call mom"), "add a todo to call mom")   # nothing to strip
        self.assertEqual(command_after("Hey Strux, schedule a meet tomorrow at 3pm."), "schedule a meet tomorrow at 3pm")


if __name__ == "__main__":
    unittest.main()
