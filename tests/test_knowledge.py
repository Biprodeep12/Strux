"""Knowledge (Claude transcripts, chat history, feedback), the screen route and the learner."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from watcher.brain import chat, learner, screen
from watcher.knowledge import Knowledge, parse_turns
from watcher.memory import Memory
from tests.test_planner import FakeLLM


def _line(**d) -> str:
    return json.dumps(d, separators=(",", ":")) + "\n"


def transcript(path: Path, cwd: str = r"C:\work\repos") -> None:
    rows = [
        _line(type="user", promptId="p1", timestamp="2026-10-22T08:00:00Z", cwd=cwd, sessionId="s1",
              origin={"kind": "human"}, message={"role": "user", "content": "fix the meta pixel payload in go-backend"}),
        _line(type="assistant", message={"role": "assistant", "content": [
            {"type": "text", "text": "Looking at it."},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": cwd + r"\go-backend\pixel.go"}}]}),
        _line(type="user", message={"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}),
        _line(type="assistant", isSidechain=True, message={"role": "assistant", "content": [{"type": "text", "text": "subagent noise"}]}),
        _line(type="assistant", message={"role": "assistant", "content": [{"type": "text", "text":
            "Fixed: the pixel payload now sends item_price and content_ids, matching the server event. Tests pass."}]}),
        _line(type="user", promptId="p2", timestamp="2026-10-22T09:00:00Z", cwd=cwd, origin={"kind": "human"},
              message={"role": "user", "content": "<task-notification>background done</task-notification>"}),
        _line(type="user", promptId="p3", timestamp="2026-10-22T09:05:00Z", cwd=cwd, origin={"kind": "human"},
              message={"role": "user", "content": "push it"}),
    ]
    path.write_text("".join(rows), encoding="utf-8")


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_turns(self):
        f = self.dir / "a.jsonl"
        transcript(f)
        turns, resume = parse_turns(f)
        self.assertEqual([t["prompt"] for t in turns], ["fix the meta pixel payload in go-backend", "push it"])
        t = turns[0]
        self.assertIn("item_price", t["answer"])
        self.assertNotIn("subagent", t["answer"])
        self.assertEqual(t["project"], "go-backend")                   # the repo the edits are in, not the parent folder
        self.assertTrue(t["files"][0].endswith("pixel.go"))
        self.assertGreater(resume, 0)                                    # resumes at the last (maybe unfinished) turn

    def test_incremental_sync_completes_running_turn(self):
        claude = self.dir / "claude"
        (claude / "proj").mkdir(parents=True)
        f = claude / "proj" / "s.jsonl"
        transcript(f)
        k = Knowledge(self.dir, {}, claude_dir=claude)
        self.assertEqual(k.sync_recent(1), 2)
        with open(f, "a", encoding="utf-8") as fh:                      # the 'push it' turn goes on
            fh.write(_line(type="assistant", message={"role": "assistant", "content": [{"type": "text", "text": "Pushed to origin."}]}))
        time.sleep(0.05)
        k.sync_file(f)
        notes = k.notes("claude")
        self.assertEqual(len(notes), 2)                                  # upserted, not duplicated
        self.assertIn("Pushed", next(n for n in notes if n["title"] == "push it")["body"])
        self.assertIn("meta pixel", k.relevant("what did claude change in the meta pixel payload?"))
        k.close()


class ChatHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.k = Knowledge(self.tmp.name, {})

    def tearDown(self):
        self.k.close()
        self.tmp.cleanup()

    def test_history_and_rating(self):
        mid = self.k.record_chat("c1", "summarise the stripe docs page", "It covers webhooks.", {"model": "m"})
        self.k.record_chat("c1", "and refunds?", "Refunds are in section 4.", {"model": "m"})
        convs = self.k.conversations()
        self.assertEqual(convs[0]["conv"], "c1")
        self.assertEqual(convs[0]["title"], "summarise the stripe docs page")
        self.assertEqual(len(self.k.conversation("c1")), 4)
        self.assertTrue(self.k.rate(mid, -1, "too vague"))
        hint = self.k.feedback_hint("summarise the stripe docs")
        self.assertIn("DISLIKED", hint)
        self.assertIn("too vague", hint)
        self.assertNotIn("It covers webhooks", self.k.relevant("stripe docs webhooks"))   # disliked answers are not recalled
        self.assertEqual(self.k.stats()["disliked"], 1)
        self.k.delete_conversation("c1")
        self.assertEqual(self.k.conversations(), [])
        self.assertEqual(self.k.stats()["chat"], 0)

    def test_search_needs_real_overlap(self):
        self.k.upsert("claude:x", "claude", time.time(), "deploy the frontend", "Deployed staging.", "frontend")
        self.assertEqual(self.k.search("what is the weather"), [])
        self.assertTrue(self.k.search("deploy frontend staging"))


class ScreenRouteTests(unittest.TestCase):
    def test_wants_screen(self):
        for q in ("What is on my screen?", "what's on my screen right now", "what am I looking at", "explain this error",
                  "summarise this page", "can you see my screen", "what's in this sheet?"):
            self.assertTrue(screen.wants_screen(q), q)
        for q in ("what did I do today?", "plan my day", "add a todo to fix the login screen", "screen time today"):
            self.assertFalse(screen.wants_screen(q) and not chat.PLANNER_Q.search(q), q)

    def test_answer_uses_fresh_capture_not_worklog(self):
        from PIL import Image
        llm = FakeLLM(text="You have an Excel sheet of Meta pixel events open.")
        store = mock.Mock()
        ctx = {"target": {"hwnd": 0, "app": "EXCEL.EXE", "title": "events.xlsx - Excel"}, "shot_cfg": {}, "vision_model": "v", "learn": False}
        used = {}
        with mock.patch.object(screen, "capture", return_value=Image.new("RGB", (64, 40), "white")), \
             mock.patch.object(screen, "read", return_value=("Team G - Lead\nmanual_not_detected", "A spreadsheet.", [])):
            text = chat.answer(llm, store, [{"role": "user", "content": "What is on my screen?"}], used=used, screen_ctx=ctx)
        self.assertIn("Excel", text)
        ask = llm.sent[-1][-1]["content"]                                # SCREEN rides in the question's turn (no echo)
        self.assertIn("Team G - Lead", ask)                              # OCR text reached the model
        self.assertIn("events.xlsx", ask)
        self.assertIn("What is on my screen?", ask)
        self.assertTrue(used["screen"]["thumb"])
        store.search.assert_not_called()                                 # not answered from the work log
        # a follow-up reuses the same look (no new capture)
        with mock.patch.object(screen, "capture", side_effect=AssertionError("captured again")):
            chat.answer(llm, store, [{"role": "user", "content": "What is on my screen?"}, {"role": "assistant", "content": text},
                                     {"role": "user", "content": "which columns does it have?"}], used={}, screen_ctx=ctx)
        screen.forget()

    def test_screen_echo_and_loops(self):
        self.assertEqual(screen.unloop("GitHub GitHub GitHub GitHub\nA dashboard.\na dashboard.\nGitHub GitHub GitHub"),
                         "GitHub\nA dashboard.")
        self.assertIn("Windows desktop", screen.context({"app": "explorer.exe", "title": "Program Manager"}, "", ""))
        llm = FakeLLM(text="Window in front: Brave — “GitHub”\n\nYou're on your GitHub dashboard.")
        hist = [{"role": "user", "content": "what's on my screen"}, {"role": "assistant", "content": "Window in front: Explorer\n\n46"},
                {"role": "user", "content": "What's on my screen?"}]
        with mock.patch.object(screen, "capture", return_value=__import__("PIL.Image", fromlist=["Image"]).new("RGB", (64, 40))), \
             mock.patch.object(screen, "read", return_value=("Dashboard", "", [])):
            text = screen.answer(llm, "What's on my screen?", hist, {"app": "brave.exe", "title": "GitHub"}, {}, "v", learn_screens=False, force=True)
        self.assertEqual(text, "You're on your GitHub dashboard.")       # a copied heading is cut
        self.assertNotIn("46", str(llm.sent[-1]))                        # an old echo isn't fed back in
        screen.forget()

    def test_blocklisted_window_is_never_captured(self):
        ctx = {"target": {"app": "1Password.exe", "title": "vault"}, "shot_cfg": {"blocklist_apps": ["1password.exe"]}}
        with mock.patch.object(screen, "capture", side_effect=AssertionError("captured")):
            text = chat.answer(FakeLLM(), mock.Mock(), [{"role": "user", "content": "what's on my screen"}], screen_ctx=ctx)
        self.assertIn("blocklist", text)

    def test_vision_is_off_by_default(self):
        from PIL import Image
        llm = FakeLLM(text="An editor.")
        calls = []
        orig = llm.chat
        llm.chat = lambda messages, **kw: (calls.append(kw.get("model")), orig(messages, **kw))[1]
        ctx = {"target": {"app": "Code.exe", "title": "a.py"}, "shot_cfg": {}, "vision_model": "vis", "learn": False}
        used = {}
        with mock.patch.object(screen, "capture", return_value=Image.new("RGB", (64, 40), "white")),              mock.patch("watcher.ocr.available", return_value=False):
            chat.answer(llm, mock.Mock(), [{"role": "user", "content": "what's on my screen"}], used=used, screen_ctx=ctx)
        self.assertNotIn("vis", calls)                                   # the GPU-crashing vision step never ran
        self.assertEqual(used["model"], "fake-model")
        screen.forget()


class LearnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.k = Knowledge(self.tmp.name, {})
        self.mem = Memory(self.tmp.name)
        for i in range(6):
            self.k.upsert(f"claude:{i}", "claude", time.time(), f"work on the TA slack approval flow step {i}", "done", "go-backend")

    def tearDown(self):
        self.k.close()
        self.mem.close()
        self.tmp.cleanup()

    def test_writes_learned_block_and_drops_secrets(self):
        llm = FakeLLM(text="- Works on go-backend: TA Slack approval flow\n- api key: sk-abcdefghijklmnop\n- probably likes tea\nnot a bullet")
        store = mock.Mock()
        with mock.patch.object(learner.worklog, "build", return_value="10:00 go-backend " * 20):
            res = learner.Learner({}, self.k, self.mem, store, llm).run(forced=True)
        self.assertTrue(res["ok"], res)
        b = next(b for b in self.mem.blocks() if b["title"] == "Learned")
        self.assertEqual(b["content"], "- Works on go-backend: TA Slack approval flow")
        self.assertEqual(b["source"], "ai")

    def test_switched_off_block_is_left_alone(self):
        b = self.mem.add("Learned", "- mine")
        self.mem.update(b["id"], enabled=False)
        res = learner.Learner({}, self.k, self.mem, mock.Mock(), FakeLLM(text="- new")).run(forced=True)
        self.assertFalse(res["ok"])
        self.assertEqual(self.mem.get(b["id"])["content"], "- mine")


if __name__ == "__main__":
    unittest.main()
