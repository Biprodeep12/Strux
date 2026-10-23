"""What Strux works out itself instead of trusting the small model: dates, times, titles, renames, shifts,
spans, which tool, follow-ups. Every case here came from a real miss in the live tool-calling eval."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta

from watcher.brain import agent, chat, dates
from watcher.planner import Planner, parse_when
from tests.test_planner import FakeLLM

# a fixed Thursday, 22 Oct 2026 01:30
NOW = datetime(2026, 10, 22, 1, 30).timestamp()


def d(ts):
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else None


class Dates(unittest.TestCase):
    def test_annotate(self):
        self.assertEqual(dates.annotate("book a call on friday at 11", NOW), "book a call on friday [Fri 23 Oct 2026] at 11")
        self.assertIn("[Mon 26 Oct 2026]", dates.annotate("next monday", NOW))
        self.assertIn("[Fri 30 Oct 2026]", dates.annotate("my exam on 30 October", NOW))
        self.assertIn("[Sun 25 Oct 2026]", dates.annotate("the 25th", NOW))
        self.assertIn("[Thu 22 Oct 2026 03:30]", dates.annotate("in 2 hours", NOW))

    def test_model_dates_are_corrected_to_the_day_said(self):
        self.assertEqual(dates.fix("2023-10-06 11:00", "call on friday at 11am", NOW, agent.parse_due)[0], "2026-10-23 11:00")
        self.assertEqual(dates.fix("2026-10-23 11:00", "call on friday at 11am", NOW, agent.parse_due)[1], None)   # already right
        self.assertEqual(dates.fix("2026-11-03", "1 Nov onwards consolidation", NOW, agent.parse_due)[1], None)     # a range
        self.assertEqual(dates.fix("today 17:00", "email him in 2 hours", NOW, agent.parse_due)[0], "2026-10-22 03:30")

    def test_copied_hints_parse(self):
        for s, want in (("Fri 23 Oct 2026 23:59", "Fri 23 Oct 23:59"), ("[Fri 23 Oct 2026] 11:00", "Fri 23 Oct 11:00"),
                        ("Friday, 23 October 2026 at 5pm", "Fri 23 Oct 17:00")):
            self.assertEqual(d(parse_when(s, NOW)), want, s)
        self.assertEqual(d(agent.parse_due("Fri 23 Oct 2026", NOW)), "Fri 23 Oct 21:00")    # date only -> evening

    def test_when_said(self):
        for said, want in (("remind me tomorrow morning to submit the form", "Fri 23 Oct 09:00"),
                           ("remind me about the electricity bill on the 25th", "Sun 25 Oct 21:00"),
                           ("remind me to call mom at 7pm", "Thu 22 Oct 19:00"), ("study at ten tonight", "Thu 22 Oct 22:00"),
                           ("add a todo to buy milk", None), ("from monday to friday at 9am", None)):
            self.assertEqual(d(dates.when_said(said, NOW)), want, said)

    def test_strip_when(self):
        self.assertEqual(dates.strip_when("submit the form tomorrow morning"), "submit the form")
        self.assertEqual(dates.strip_when("Pay electricity bill on Sun 25 Oct 2026"), "Pay electricity bill")
        self.assertEqual(dates.strip_when("call mom at 7pm"), "call mom")
        self.assertEqual(dates.strip_when("study Java"), "study Java")


class Normalise(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.p = Planner(self.tmp.name)
        self.read = self.p.add_todo("Read paper")
        self.se = self.p.add_todo("Revise SE", due_ts=datetime(2026, 10, 23, 18).timestamp())
        self.java = self.p.add_todo("Study Java")
        self.sync = self.p.add_event("Team sync", datetime(2026, 10, 23, 10).timestamp(), datetime(2026, 10, 23, 10, 30).timestamp())

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def n(self, name, args, said):
        return agent._normalise_call(self.p, {"name": name, "arguments": args}, said, said)

    def test_rename_uses_the_old_name(self):
        c = self.n("update_todo", {"todo": "read the ML paper", "title": "read the ML paper"}, "rename the read paper todo to read the ML paper")
        self.assertEqual(c, {"name": "update_todo", "arguments": {"todo": str(self.read["id"]), "title": "read the ML paper"}})

    def test_shift_counts_from_the_items_time(self):
        c = self.n("update_todo", {"todo": "Revise SE", "due": "tomorrow 18:00"}, "push revise SE by a day")
        self.assertEqual(c["arguments"]["due"], "2026-10-24 18:00")
        c = self.n("move_event", {"event": "Team sync", "start": "x"}, "move the team sync earlier by 30 minutes")
        self.assertEqual((c["arguments"]["start"], c["arguments"]["end"]), ("2026-10-23 09:30", "2026-10-23 10:00"))

    def test_span(self):
        c = self.n("add_event", {"title": "hackathon", "start": "2026-10-24 10:00", "end": "2026-10-24 16:00"}, "hackathon on saturday from 10am to 6pm")
        self.assertEqual(c["arguments"]["end"], "2026-10-24 18:00")
        self.assertEqual(agent._span_clock("2", "5pm"), ((14, 0), (17, 0)))
        self.assertEqual(agent._span_clock("11", "1pm"), ((11, 0), (13, 0)))

    def test_wrong_tool_kind(self):
        c = self.n("move_event", {"event": "Study Java", "start": "tomorrow 9am"}, "move study java to tomorrow 9am")
        self.assertEqual(c["name"], "update_todo")
        self.assertEqual(c["arguments"]["due"], "tomorrow 9am")
        c = self.n("delete_todo", {"todo": "Team sync"}, "cancel the team sync")
        self.assertEqual(c["name"], "delete_event")

    def test_add_of_existing_with_change_words_is_an_update(self):
        c = self.n("add_todo", {"title": "Study Java · due today 20:00", "priority": "high"}, "make study java high priority")
        self.assertEqual(c, {"name": "update_todo", "arguments": {"todo": str(self.java["id"]), "priority": "high"}})

    def test_remind_me_is_a_todo_not_a_memory(self):
        c = self.n("remember", {"fact": "call mom at 7pm"}, "remind me to call mom at 7pm")
        self.assertEqual(c["name"], "add_todo")
        self.assertEqual(c["arguments"]["title"], "call mom")
        self.assertTrue(c["arguments"]["due"])
        self.assertEqual(self.n("remember", {"fact": "I like tea"}, "remember that I like tea")["name"], "remember")

    def test_hint_never_in_title(self):
        c = self.n("add_todo", {"title": "submit the OS lab report on friday [Fri 23 Oct 2026]", "due": "Fri 23 Oct 2026"},
                   "add a to-do for submitting the OS lab report on friday")
        self.assertEqual(c["arguments"]["title"], "submit the OS lab report")


class Guards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.p = Planner(self.tmp.name)

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def test_invented_due_and_priority_are_dropped(self):
        chat.answer(FakeLLM(turns=[[{"name": "add_todo", "arguments": {"title": "call the bank", "due": "today 17:00", "priority": "high"}}]]),
                    None, [{"role": "user", "content": "add the to do call the bank"}], planner=self.p)
        t = self.p.todos()[0]
        self.assertEqual((t["title"], t["due_ts"], t["priority"]), ("call the bank", None, 1))

    def test_follow_up_after_a_change_reaches_the_tools(self):
        self.assertTrue(chat.wants_planner("make it 5pm", self.p, "Added TODO “call the bank”"))
        self.assertFalse(chat.wants_planner("make it 5pm", self.p, "Here is how recursion works…"))

    def test_done_statement_about_an_existing_item(self):
        self.p.add_todo("Call the bank")
        self.assertTrue(chat.wants_planner("I called the bank, tick it off", self.p))
        self.assertFalse(chat.wants_planner("I called my friend", self.p))

    def test_text_answer_to_an_add_request_is_retried_once(self):
        llm = FakeLLM(turns=[[], [{"name": "add_todo", "arguments": {"title": "call mom"}}]], text="I can't remember that for you.")
        chat.answer(llm, None, [{"role": "user", "content": "remind me to call mom"}], planner=self.p)
        self.assertEqual([t["title"] for t in self.p.todos()], ["call mom"])

    def test_question_is_never_retried_as_an_add(self):
        llm = FakeLLM(turns=[[]], text="You have nothing open.")
        out = chat.answer(llm, None, [{"role": "user", "content": "what's on my todo list"}], planner=self.p)
        self.assertEqual(self.p.todos(), [])
        self.assertNotIn("What should the TODO say", out)


if __name__ == "__main__":
    unittest.main()
