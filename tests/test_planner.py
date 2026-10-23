"""TODOs / calendar / prompts / agent / planner-aware rules and actions."""
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta

from watcher.brain import agent, chat, prompts
from watcher.brain import rules as R
from watcher.brain.scheduler import Brain
from watcher.brain.summarizer import ActivityWindow
from watcher.config import DEFAULTS, deep_merge
from watcher.events import Event, Suggestion
from watcher.planner import Planner, parse_when, quick_parse
from watcher.store import Store


def at(h, m=0, days=0):
    d = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days)
    return d.timestamp()


class FakeLLM:
    """Scripted model: returns queued tool-call turns / texts and records what it was sent."""

    def __init__(self, turns=None, text="ok"):
        self.turns = list(turns or [])
        self.text = text
        self.sent = []
        self.base = "fake"
        self.model = "fake-model"
        self.last_error = None
        self.last_latency = 0.0

    def chat_tools(self, messages, tools, **kw):
        self.sent.append(messages)
        if self.turns:
            calls = self.turns.pop(0)
            return {"content": "", "tool_calls": calls, "message": {"role": "assistant", "content": "", "tool_calls": []}}
        return {"content": self.text, "tool_calls": [], "message": {"role": "assistant", "content": self.text}}

    def chat(self, messages, **kw):
        self.sent.append(messages)
        return self.text

    def reachable(self):
        return True

    def suggest(self, prompt):
        self.sent.append(prompt)
        return self.text


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.changes = []
        self.p = Planner(self.tmp.name, on_change=self.changes.append)

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def test_todo_crud_order_and_dedupe(self):
        now = time.time()
        a = self.p.add_todo("Later thing", due_ts=now + 86400)
        b = self.p.add_todo("Overdue thing", due_ts=now - 3600, priority=2)
        c = self.p.add_todo("Undated")
        self.assertEqual(self.p.add_todo("undated")["id"], c["id"])          # same open title -> reused
        self.assertEqual([t["id"] for t in self.p.todos()], [b["id"], a["id"], c["id"]])
        self.p.update_todo(b["id"], done=True)
        self.assertEqual([t["id"] for t in self.p.todos()], [a["id"], c["id"]])
        self.assertEqual(len(self.p.todos(include_done=True)), 3)
        self.assertEqual(self.p.find_todo("#%d" % a["id"])["title"], "Later thing")
        self.assertEqual(self.p.find_todo("undat")["id"], c["id"])
        self.assertTrue(self.p.delete_todo(c["id"]))
        self.assertFalse(self.p.delete_todo(c["id"]))
        self.assertIn("todos", self.changes)
        with self.assertRaises(ValueError):
            self.p.add_todo("   ")

    def test_ids_never_reused(self):
        a = self.p.add_todo("first")
        self.p.delete_todo(a["id"])
        self.assertNotEqual(self.p.add_todo("second")["id"], a["id"])
        e = self.p.add_event("x", time.time())
        self.p.delete_event(e["id"])
        self.assertNotEqual(self.p.add_event("y", time.time())["id"], e["id"])

    def test_events_and_context(self):
        s = at(15, days=1)
        e = self.p.add_event("Standup", s)
        self.assertEqual(e["end_ts"], s + 3600)
        self.p.add_event("Holiday", at(0, days=2), all_day=True)
        with self.assertRaises(ValueError):
            self.p.add_event("bad", s, s - 10)
        self.assertEqual(len(self.p.events_between(s - 60, s + 60)), 1)
        self.p.update_event(e["id"], title="Daily standup")
        self.p.add_todo("Ship it", due_ts=time.time() - 60, priority=2, project="strux")
        ctx = self.p.context_text()
        self.assertIn("Daily standup", ctx)
        self.assertIn("OVERDUE", ctx)
        self.assertIn("high", ctx)
        self.assertIn("strux", ctx)
        self.assertIn("all day", ctx)

    def test_parse_when(self):
        now = at(12)
        self.assertEqual(parse_when("14:30", now), at(14, 30))
        self.assertEqual(parse_when("9:00", now), at(9, days=1))               # already passed -> tomorrow
        self.assertEqual(parse_when("tomorrow 10am", now), at(10, days=1))
        self.assertEqual(parse_when("today 5pm", now), at(17))
        self.assertEqual(parse_when("tonight 9", now), at(21))
        self.assertEqual(parse_when("in 2 hours", now), now + 7200)
        self.assertEqual(parse_when("in 30 min", now), now + 1800)
        self.assertEqual(parse_when("2026-12-01 08:15", now), datetime(2026, 12, 1, 8, 15).timestamp())
        self.assertEqual(parse_when("2026-12-01T08:15", now), datetime(2026, 12, 1, 8, 15).timestamp())
        self.assertEqual(parse_when(1700000000000, now), 1700000000.0)          # ms epoch
        nxt = datetime.fromtimestamp(parse_when("friday", now))
        self.assertEqual((nxt.weekday(), nxt.hour), (4, 9))
        self.assertEqual(parse_when("Oct 21 5pm", now), datetime(datetime.now().year, 10, 21, 17).timestamp())
        self.assertEqual(parse_when("on 21/10 at 9", now), datetime(datetime.now().year, 10, 21, 9).timestamp())
        self.assertIsNone(parse_when("31/02", now))
        for bad in ("whenever", "25:00", "tomorrow 99", ""):
            self.assertIsNone(parse_when(bad, now), bad)

    def test_quick_parse(self):
        now = at(12)
        q = quick_parse("Fix login bug tomorrow 10am !high #watcher-ai", now)
        self.assertEqual((q["title"], q["due_ts"], q["priority"], q["project"]), ("Fix login bug", at(10, days=1), 2, "watcher-ai"))
        q = quick_parse("Review PR #12", now)
        self.assertEqual((q["title"], q["project"], q["due_ts"]), ("Review PR #12", "", None))
        q = quick_parse("Call mom at 18:30", now)
        self.assertEqual((q["title"], q["due_ts"]), ("Call mom", at(18, 30)))
        q = quick_parse("Gym tonight 7pm !low", now)
        self.assertEqual((q["title"], q["due_ts"], q["priority"]), ("Gym", at(19), 0))
        q = quick_parse("Read chapter 3", now)                                    # a number that is not a time
        self.assertEqual((q["title"], q["due_ts"]), ("Read chapter 3", None))


class PromptTests(unittest.TestCase):
    def tearDown(self):
        prompts.configure({})

    def test_defaults_overrides_and_safe_format(self):
        cfg = {}
        prompts.configure(cfg)
        self.assertIn("Biprodeep's", prompts.get("chat", who="Biprodeep's"))
        cfg["prompts"] = {"chat": "Be terse, {who}. Unknown {thing} stays. {"}
        self.assertEqual(prompts.get("chat", who="Sam's"), "Be terse, {who}. Unknown {thing} stays. {")   # stray brace -> raw
        cfg["prompts"] = {"chat": "Be terse for {who}; keep {thing}."}
        self.assertEqual(prompts.get("chat", who="Sam's"), "Be terse for Sam's; keep {thing}.")
        cfg["prompts"] = {"chat": "   "}
        self.assertTrue(prompts.get("chat").startswith("You are Strux"))          # blank override = default
        keys = {p["key"] for p in prompts.listing()}
        self.assertTrue({"chat", "live", "plan", "suggest", "explain", "vision", "recap", "discuss"} <= keys)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.p = Planner(self.tmp.name)

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def test_tools(self):
        now = at(12)
        txt, ch = agent.run_tool(self.p, "add_todo", {"title": "Call bank", "due": "tomorrow 10:00", "priority": "high"}, now)
        self.assertIn("added TODO", txt)
        t = ch["todo"]
        self.assertEqual((t["due_ts"], t["priority"], t["source"]), (at(10, days=1), 2, "ai"))
        self.assertTrue(agent.run_tool(self.p, "add_todo", {"title": "x", "due": "someday"}, now)[0].startswith("error"))
        self.assertIn("Call bank", agent.run_tool(self.p, "list_todos", {}, now)[0])
        self.assertIn("updated", agent.run_tool(self.p, "update_todo", {"todo": "bank", "due": "none"}, now)[0])
        self.assertIsNone(self.p.find_todo("bank")["due_ts"])
        self.assertIn("done", agent.run_tool(self.p, "complete_todo", {"todo": f"#{t['id']}"}, now)[0])
        self.assertTrue(agent.run_tool(self.p, "complete_todo", {"todo": "nothing like this"}, now)[0].startswith("error"))
        txt, ch = agent.run_tool(self.p, "add_event", {"title": "Dentist", "start": "tomorrow 15:00", "end": "tomorrow 14:00"}, now)
        e = ch["event"]
        self.assertEqual(e["end_ts"] - e["start_ts"], 3600)                       # end before start -> default hour
        self.assertIn("Dentist", agent.run_tool(self.p, "list_events", {"day": "tomorrow"}, now)[0])
        self.assertIn("moved", agent.run_tool(self.p, "move_event", {"event": str(e["id"]), "start": "tomorrow 16:00"}, now)[0])
        moved = self.p.events_between(0, 1e12)[0]
        self.assertEqual((moved["start_ts"], moved["end_ts"] - moved["start_ts"]), (at(16, days=1), 3600))  # duration kept
        self.assertTrue(agent.run_tool(self.p, "move_event", {"event": "board meeting"}, now)[0].startswith("error"))
        self.assertIn("deleted", agent.run_tool(self.p, "delete_event", {"event": f"#{e['id']}"}, now)[0])
        self.assertTrue(agent.run_tool(self.p, "nope", {}, now)[0].startswith("error"))
        # "re-adding" an open TODO with a new time updates it instead of keeping the old one
        agent.run_tool(self.p, "add_todo", {"title": "Study Java", "due": "today 17:23"}, now)
        txt, ch = agent.run_tool(self.p, "add_todo", {"title": "study java", "due": "today 21:00", "priority": "high"}, now)
        t = self.p.find_todo("study java")
        self.assertEqual((ch["kind"], t["due_ts"], t["priority"]), ("todo_updated", at(21), 2))
        self.assertEqual(len([x for x in self.p.todos() if x["title"].lower() == "study java"]), 1)
        self.assertIn("already on the list", agent.run_tool(self.p, "add_todo", {"title": "Study Java"}, now)[0])
        self.assertEqual(agent.run_tool(self.p, "add_todo", {"title": "Exam prep", "due": "today 21 Oct"}, at(12))[0][:9], "added TOD")

    def test_loop_and_chat_routing(self):
        llm = FakeLLM(turns=[[{"name": "add_todo", "arguments": {"title": "Water plants", "due": "today 18:00"}}]], text="Added it for 18:00.")
        changes = []
        out = chat.answer(llm, store=None, messages=[{"role": "user", "content": "add a todo to water plants today at 6pm"}],
                          planner=self.p, changes=changes)
        self.assertEqual(out, "Added TODO “Water plants” · due today 18:00")          # exact, from the change itself
        self.assertEqual([c["kind"] for c in changes], ["todo_added"])
        self.assertEqual(self.p.todos()[0]["title"], "Water plants")
        sys_msg = llm.sent[0][0]["content"]
        self.assertIn("Open TODOs", sys_msg)                                       # planner context reaches the model
        self.assertIn("managing their TODOs and calendar", sys_msg)               # the planner prompt, not the summary one
        # a stated length the model forgot is applied
        llm = FakeLLM(turns=[[{"name": "add_event", "arguments": {"title": "Sync", "start": "tomorrow 15:00"}}]])
        agent.run(llm, self.p, [{"role": "user", "content": "schedule a sync tomorrow at 3pm for 30 minutes"}])
        ev = next(e for e in self.p.events_between(0, 1e12) if e["title"] == "Sync")
        self.assertEqual(ev["end_ts"] - ev["start_ts"], 1800)
        # "move X" answered with add_event(X) becomes a real move, found by title words
        llm = FakeLLM(turns=[[{"name": "add_event", "arguments": {"title": "Sync", "start": "tomorrow 17:00"}}]])
        out, made = agent.run(llm, self.p, [{"role": "user", "content": "move the sync to 5pm tomorrow"}])
        syncs = [e for e in self.p.events_between(0, 1e12) if e["title"] == "Sync"]
        self.assertEqual(len(syncs), 1)
        self.assertEqual((syncs[0]["start_ts"], syncs[0]["end_ts"] - syncs[0]["start_ts"]), (at(17, days=1), 1800))
        self.assertTrue(out.startswith("Moved “Sync”"), out)
        self.assertEqual(self.p.find_event("the sync meeting")["title"], "Sync")
        self.assertIsNone(self.p.find_event("dentist"))
        # re-adding things that exist is reported truthfully, not as "Added"
        llm = FakeLLM(turns=[[{"name": "add_todo", "arguments": {"title": "Water plants"}}]], text="Added TODO Water plants!")
        retry = [{"role": "user", "content": "add a todo to water plants"}, {"role": "assistant", "content": "Added TODO “Water plants”"},
                 {"role": "user", "content": "retry and add it to the todo list"}]
        out, made = agent.run(llm, self.p, retry)
        self.assertEqual((out, made), ("All of these are already on your list: Water plants", []))
        due0 = self.p.find_todo("water plants")["due_ts"]
        llm = FakeLLM(turns=[[{"name": "add_todo", "arguments": {"title": "Water plants", "due": "tomorrow 07:00"}}]])
        agent.run(llm, self.p, retry)
        dues = [t["due_ts"] for t in self.p.todos() if t["title"].lower() == "water plants"]
        self.assertIn(due0, dues)                                                        # a retry never moves an existing TODO
        # a model that loops forever is cut off and summarised
        llm = FakeLLM(turns=[[{"name": "list_todos", "arguments": {}}]] * 10)
        out, made = agent.run(llm, self.p, [{"role": "user", "content": "x"}])
        self.assertEqual(out, "I couldn't finish that.")


class EmptyLogTests(unittest.TestCase):
    def test_empty_period_is_never_sent_to_the_model(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        store = Store(tmp.name)
        store.start()
        p = Planner(tmp.name)
        p.add_todo("Renew domain")
        llm = FakeLLM(text="You finalised the client proposal.")       # what a 7B model invents for an empty log
        now = time.time()
        out = chat.answer(llm, store, [{"role": "user", "content": "Where did I leave off?"}],
                          window=(now - 7200, now - 3600, "the 90 minutes before the break"), planner=p)
        self.assertEqual(out, "Nothing was recorded in the 90 minutes before the break: you were away or idle.\nPick up: Renew domain.")
        self.assertEqual(llm.sent, [])
        p.close()
        store.stop()
        tmp.cleanup()


class Rules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(self.tmp.name)
        self.store.start()
        self.p = Planner(self.tmp.name)

    def tearDown(self):
        self.p.close()
        self.store.stop()
        self.tmp.cleanup()

    def win(self, now=None, **kw):
        w = ActivityWindow(minutes=15, now=now or time.time())
        w.planner = self.p
        for k, v in kw.items():
            setattr(w, k, v)
        return w

    def test_todo_due_and_event_soon(self):
        now = time.time()
        t = self.p.add_todo("Send invoice", due_ts=now + 600)
        s = R.TodoDue().evaluate(self.win(now), self.store, {"lead_minutes": 30})
        self.assertIn("Send invoice", s.text)
        self.assertEqual(s.context["action"], {"type": "complete_todo", "id": t["id"]})
        self.store.add_suggestion(s)
        self.assertIsNone(R.TodoDue().evaluate(self.win(now), self.store, {}))         # not twice
        self.p.add_todo("Far away", due_ts=now + 5 * 3600)
        e = self.p.add_event("Design review", now + 8 * 60, location="Room 2")
        s = R.EventSoon().evaluate(self.win(now), self.store, {"lead_minutes": 15})
        self.assertIn("Design review", s.text)
        self.assertIn("Room 2", s.text)
        self.assertEqual(s.context["action"], {"type": "prep_event", "id": e["id"]})

    def test_reminders_ignore_dismiss_learning(self):
        cfg = deep_merge(DEFAULTS["brain"], {"quiet_when_idle": False, "llm": {"enabled": False}})
        b = Brain(cfg, self.store, lambda s: None)
        b.planner = self.p
        for i in range(3):        # three dismissed reminders for other TODOs
            sid = self.store.add_suggestion(Suggestion(text=f"old {i}", source="rule:todo_due", ts=time.time() - 3600 + i, context={"todo_key": "999:1"}))
            self.store.set_feedback(sid, "dismiss")
        self.p.add_todo("Pay rent", due_ts=time.time() + 300)
        s = b.tick(forced=True)
        self.assertIsNotNone(s)
        self.assertIn("Pay rent", s.text)

    def test_morning_and_wrap_up(self):
        morning = at(8, 30)
        self.assertIsNone(R.MorningPlan().evaluate(self.win(morning), self.store, {}))   # nothing planned -> quiet
        self.p.add_todo("A", due_ts=morning - 3600)
        self.p.add_todo("B")
        self.p.add_event("Gym", at(18))
        s = R.MorningPlan().evaluate(self.win(morning, user="Sam"), self.store, {})
        self.assertIn("Morning, Sam! 2 open TODOs (1 overdue) and 1 event today", s.text)
        self.assertEqual(s.context["action"]["type"], "plan")
        self.assertIsNone(R.MorningPlan().evaluate(self.win(at(15)), self.store, {}))    # afternoon
        self.assertIsNone(R.WrapUp().evaluate(self.win(at(15)), self.store, {"hour": 21}))
        s = R.WrapUp().evaluate(self.win(at(22)), self.store, {"hour": 21})
        self.assertIn("2 TODOs are still open", s.text)

    def test_busy_process(self):
        procs = types.SimpleNamespace(long_busy=lambda m, c: [{"pid": 1, "name": "node.exe", "label": "vite dev server in Untrue", "cpu": 20.4, "seconds": 7300}])
        s = R.BusyProcess().evaluate(self.win(procs=procs), self.store, {})
        self.assertEqual(s.text, "vite dev server in Untrue has used ~20% CPU for 2 h 2 min. Still need it running?")
        self.store.add_suggestion(s)
        self.assertIsNone(R.BusyProcess().evaluate(self.win(procs=procs), self.store, {}))

    def _seed(self, events):
        for e in events:
            self.store.put_event(e)
        self.store.flush()
        time.sleep(1.2)

    def test_revisit_and_distraction(self):
        now = max(time.time(), R.midnight(time.time()) + 2 * 3600)    # all visits must be "today", even just after midnight
        ev = []
        for i in range(5):              # 5 visits to the same page, other work in between
            ev.append(Event(kind="window", ts=now - 4800 + i * 600, app="brave.exe", title="Stripe webhooks docs - Brave"))
            ev.append(Event(kind="window", ts=now - 4800 + i * 600 + 60, app="code.exe", title="api.py - shop - Visual Studio Code"))
        ev.append(Event(kind="window", ts=now - 1600, app="brave.exe", title="Funny cats - YouTube - Brave"))     # 26 min, uninterrupted
        ev.append(Event(kind="window", ts=now - 10, app="code.exe", title="api.py - shop - Visual Studio Code"))
        self._seed(ev)
        s = R.RevisitPage().evaluate(self.win(now), self.store, {"min_visits": 4})
        self.assertIn("Stripe webhooks docs", s.text)
        self.assertEqual(s.context["action"]["type"], "add_todo")
        self.p.add_todo("Fix checkout", priority=2)
        s = R.Distraction().evaluate(self.win(now), self.store, {"window_minutes": 45, "minutes": 25})
        self.assertIn("YouTube", s.text)
        self.assertIn("Fix checkout", s.text)
        self.assertEqual(s.context["action"]["type"], "focus")

    def test_model_todo_suggestion_becomes_add_todo(self):
        cfg = deep_merge(DEFAULTS["brain"], {"quiet_when_idle": False, "rules": {r.name: {"enabled": False} for r in R.ALL_RULES}})
        got = []
        b = Brain(cfg, self.store, got.append)
        b.llm = FakeLLM(text="TODO: Write tests for the payment webhook")
        b.gpu = types.SimpleNamespace(status=lambda: None)
        b.planner = self.p
        self._seed([Event(kind="window", ts=time.time() - 900, app="brave.exe", title="Stripe payment webhook tests - Brave"),
                    Event(kind="window", ts=time.time() - 60, app="code.exe", title="x.py - shop - Visual Studio Code")])
        s = b.tick(forced=True)
        self.assertEqual(s.text, "Add a TODO: “Write tests for the payment webhook”?")
        self.assertEqual(s.context["action"], {"type": "add_todo", "title": "Write tests for the payment webhook"})
        self.assertIn("Open TODOs", b.llm.sent[0])                     # planner context in the suggestion prompt


class ActionTests(unittest.TestCase):
    def test_planner_actions(self):
        from watcher.brain.actions import Actions
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        p = Planner(tmp.name)
        alerts = []
        store = types.SimpleNamespace(suggestion=lambda sid: {"id": sid, "text": "Add a TODO?", "context": "{}"})
        app = types.SimpleNamespace(planner=p, store=store, overlay=types.SimpleNamespace(alert=lambda k, t, **kw: alerts.append(t)),
                                    brain=types.SimpleNamespace(llm=FakeLLM()), broadcast=lambda m: None, cfg={})
        a = Actions(app)
        a._run({"text": "x"}, {"type": "add_todo", "title": "Renew domain"})
        t = p.todos()[0]
        self.assertEqual((t["title"], t["source"]), ("Renew domain", "suggestion"))
        a._run({"text": "x"}, {"type": "complete_todo", "id": t["id"]})
        self.assertEqual(p.todos(), [])
        self.assertEqual(alerts, ["Added to your TODOs: Renew domain", "Done: Renew domain"])
        p.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
