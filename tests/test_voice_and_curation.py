"""Speech to text (without a microphone) and the curated finished-job / heads-up alerts."""
import json
import tempfile
import time
import types
import unittest

import numpy as np

from watcher.brain import rules as R
from watcher.brain.scheduler import Brain
from watcher.config import DEFAULTS, deep_merge
from watcher.events import Suggestion
from watcher.store import Store
from watcher.stt import RATE, SpeechToText, resample


class Stt(unittest.TestCase):
    def test_resample(self):
        x = np.sin(np.linspace(0, 100, 44100)).astype(np.float32)
        y = resample(x, 44100)
        self.assertEqual(len(y), RATE)
        self.assertEqual(y.dtype, np.float32)
        self.assertIs(resample(np.zeros(0, np.float32), 48000).size, 0)

    def test_silence_is_not_sent_to_the_model(self):
        s = SpeechToText({}, tempfile.gettempdir())
        s._chunks, s._rate = [np.zeros(RATE, np.float32)], RATE
        s._load = lambda: self.fail("model must not load for silence")
        self.assertEqual(s.stop()["text"], "")

    def test_transcribe_uses_hints_and_language(self):
        got = {}

        class Model:
            def transcribe(self, audio, **kw):
                got.update(kw)
                return iter([types.SimpleNamespace(text=" add a todo "), types.SimpleNamespace(text="for Strux. ")]), types.SimpleNamespace(language="en")
        s = SpeechToText({"language": "en"}, tempfile.gettempdir(), hints=lambda: "Biprodeep, Strux.")
        s._model, s._model_state = Model(), "ready"
        s.cfg["unload_minutes"] = 0
        out = s.transcribe(np.ones(RATE, np.float32) * 0.1)
        self.assertEqual(out["text"], "add a todo for Strux.")
        self.assertEqual((got["language"], got["initial_prompt"], got["vad_filter"]), ("en", "Biprodeep, Strux.", True))

    def test_gpu_model_and_cpu_fallback(self):
        import watcher.stt as stt_mod
        old, stt_mod._cuda_ok = stt_mod._cuda_ok, True
        self.addCleanup(setattr, stt_mod, "_cuda_ok", old)
        s = SpeechToText({"model": "small", "gpu_model": "large-v3-turbo", "unload_minutes": 0}, tempfile.gettempdir())
        self.assertEqual((s.device, s.model_name), ("cuda", "large-v3-turbo"))
        self.assertEqual(SpeechToText({"device": "cpu", "gpu_model": "large-v3-turbo"}, tempfile.gettempdir()).model_name, "small")

        class Broken:
            def transcribe(self, audio, **kw):
                raise RuntimeError("CUDA failed with error out of memory")

        class Cpu:
            def transcribe(self, audio, **kw):
                return iter([types.SimpleNamespace(text="hello")]), types.SimpleNamespace(language="en")
        s._load = lambda: Broken() if s.device == "cuda" else Cpu()
        out = s.transcribe(np.ones(RATE, np.float32) * 0.1)
        self.assertEqual((out["text"], out["model"], s.device), ("hello", "whisper-small", "cpu"))

    def test_api(self):
        from starlette.testclient import TestClient
        from watcher.api.server import create_api
        from watcher.app import App
        from tests.test_api import _cfg
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        app = App(_cfg(tmp.name))
        app.store.start()
        try:
            c = TestClient(create_api(app), raise_server_exceptions=False)
            app.planner.add_todo("Ship it", project="watcher-ai")
            hints = app.stt.hints()                                              # the real hints, as stop() uses them
            self.assertIn("Strux", hints)
            self.assertIn("watcher-ai", hints)
            self.assertFalse(c.get("/api/stt/status").json()["recording"])
            self.assertEqual(c.post("/api/stt/stop").status_code, 409)           # not recording
            app.stt.start = lambda: (_ for _ in ()).throw(OSError("no microphone"))
            r = c.post("/api/stt/start")
            self.assertEqual(r.status_code, 409)
            self.assertIn("no microphone", r.json()["detail"])
            app.cfg["stt"]["enabled"] = False
            self.assertEqual(c.post("/api/stt/start").status_code, 403)
            self.assertEqual(c.post("/api/stt/cancel").json(), {"ok": True})
        finally:
            app.planner.close()
            app.memory.close()
            app.store.stop()
            tmp.cleanup()


class Wake(unittest.TestCase):
    def test_phrase(self):
        from watcher.wake import command_after, find_wake
        for said in ("Hey Strux, what's on my calendar?", "Hi Strux. Plan my day", "Okay, Strux, add a todo",
                     # how whisper tiny.en actually wrote it (SAPI voices, 2026-10-23)
                     "Hey, Strucks! What's the time?", "high strokes, plan my day", "OK, structs. Add a todo to call mom.", "Hey, Strucks.",
                     "Heistrux. Plan my day."):                            # large-v3-turbo, no prompt
            self.assertTrue(find_wake(said), said)
        for said in ("I said hello to them", "the strux effect in UX", "they gave a strux award", "So yesterday I was telling him hey strux",
                     "hey hello what's on my calendar", "Hey Halo, what's next", "okay, tracks three and four", "Hey, trucks are parked",
                     "OK, structure first", "hey stroll over"):
            self.assertFalse(find_wake(said), said)                     # not opening the sentence / no greeting / not the name
        self.assertEqual(command_after("Hey Strux, what's on my calendar today?"), "what's on my calendar today")
        self.assertEqual(command_after("Hey Strux."), "")

    def test_name_spellings(self):
        from watcher.wake import is_name
        for w in ("Strux", "Strucks", "strokes", "structs", "Starks", "Stucks", "Strugs", "Strux,"):
            self.assertTrue(is_name(w), w)
        for w in ("stocks", "strong", "struggling", "stop", "stuff", "Sarah", "Halo", "trucks", "tracks", "stokes"):
            self.assertFalse(is_name(w), w)

    def test_repeated_wake_is_not_the_message(self):
        from watcher.wake import message_after
        self.assertEqual(message_after("Hey, Strux. Hey, Strux, what's up?"), "what's up")      # said again while waiting
        self.assertEqual(message_after("Hey Strux. Strux, Strux."), "")
        self.assertEqual(message_after("Hey Strux, add a todo to call mom."), "add a todo to call mom")
        self.assertEqual(message_after("Hello, what's next?"), "what's next")
        self.assertEqual(message_after("Heistrux. Plan my day."), "Plan my day")
        self.assertEqual(message_after("Restructs, what's next?"), "what's next")
        self.assertEqual(message_after("structure my notes"), "structure my notes")         # a lowercase real word stays                 # big model's spelling

    def test_audio_from_while_transcribing_is_dropped(self):
        """'Hey Strux?' said again while the model loaded used to replay afterwards as a burst of stale commands."""
        import threading
        from watcher.wake import WakeListener
        blocks, lock, got = [], threading.Lock(), []

        class Stt:
            def transcribe(self, audio, hints=True):
                blocks.append(np.ones(RATE, np.float32) * 0.1)          # the mic keeps running meanwhile
                return {"text": "Hey Strux, what's the time?"}
        w = WakeListener({"wake": {"sound": False}}, Stt(), on_command=got.append, on_state=lambda s: None,
                         suppressed=lambda: False)
        w._tiny = types.SimpleNamespace(transcribe=lambda *a, **k: ([types.SimpleNamespace(text="Hey Strux, what's")], None))
        w._record_until_silence = lambda *a, **k: np.zeros(RATE // 2, np.float32)
        self.assertTrue(w._burst(np.zeros(RATE, np.float32), blocks, lock, RATE, vad=None))
        self.assertEqual(got, ["what's the time"])
        self.assertEqual(blocks, [])
        self.assertEqual(w.state, "waiting")

    def test_other_speech_does_not_wake(self):
        """2026-10-23: a video's "What?! No, no, no…" came out of the primed tiny.en as "Hey Strux" and was sent as a
        message. The unprompted chat model has to hear the wake phrase too."""
        import threading
        from watcher.wake import WakeListener
        got, calls = [], []

        class Stt:
            def transcribe(self, audio, hints=True):
                calls.append(hints)
                return {"text": "What?! No, no, no, what?!"}
        w = WakeListener({"wake": {"sound": False}}, Stt(), on_command=got.append, on_state=lambda s: None,
                         suppressed=lambda: False)
        w._tiny = types.SimpleNamespace(transcribe=lambda *a, **k: ([types.SimpleNamespace(text="Hey Strux.")], None))
        w._record_until_silence = lambda *a, **k: self.fail("must not start listening")
        self.assertFalse(w._burst(np.zeros(RATE, np.float32), [], threading.Lock(), RATE, vad=None))
        self.assertEqual((got, calls, w.woken, w.state), ([], [False], 0, "off"))

    def test_double_check_on_real_transcripts(self):
        """What the unprompted chat model wrote on 2026-10-24 after the primed tiny.en said "Hey Strux."."""
        from watcher.wake import confirms_wake
        for you in ("Hey, Strux.", "Hey Struck! Summarize my last hour.", "Hey, Strunks.", "Restructs", "Aestrux", "A strokes.",
                    "Very struck us.", "Rrrrstruck!", "Heistrux. Plan my day."):
            self.assertTrue(confirms_wake(you), you)
        for other in ("Very cool.", "Exactly.", "Thank you.", "How do you get this?", "", "What?! No, no, no, what?!",
                      "I've been so tired to have even the least favorite", "See you in the next video.", "So yesterday the strong man"):
            self.assertFalse(confirms_wake(other), other)

    def test_prompt_echo_is_dropped(self):
        from watcher.stt import prompt_echo
        hints = "Biprodeep, Strux, Claude Code, TODO."
        self.assertTrue(prompt_echo("Biprodeep, Strux, Claude Code, Strux, Claude Code, Strux, Claude Code, Strux", hints))
        self.assertTrue(prompt_echo("Strux, Claude Code.", hints))
        for real in ("Strux, add a TODO for Claude Code", "what's on my calendar", "Strux", "Claude Code"):
            self.assertFalse(prompt_echo(real, hints), real)

    def test_api_and_settings_form_cannot_flip_it(self):
        from starlette.testclient import TestClient
        from watcher.api.server import create_api
        from watcher.app import App
        from tests.test_api import _cfg
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        app = App(_cfg(tmp.name))
        app.store.start()
        started = []
        app.wake.start = lambda: started.append(True)
        app.wake.stop = lambda: started.append(False)
        try:
            c = TestClient(create_api(app), raise_server_exceptions=False)
            v = c.get("/api/voice").json()
            self.assertEqual((v["read_aloud"], v["wake"]["enabled"]), ("off", False))      # both off by default
            self.assertEqual(c.put("/api/voice", json={"read_aloud": "loud"}).status_code, 400)
            self.assertEqual(c.put("/api/voice", json={"wake": "yes"}).status_code, 400)
            v = c.put("/api/voice", json={"read_aloud": "voice", "wake": True}).json()
            self.assertEqual((v["read_aloud"], v["wake"]["enabled"], started), ("voice", True, [True]))
            # which Windows voice reads replies, and how fast ('' = automatic; speed clamped)
            self.assertEqual((v["tts_voice"], v["tts_rate"]), ("", 1.05))
            v = c.put("/api/voice", json={"tts_voice": " Microsoft Zira ", "tts_rate": 9}).json()
            self.assertEqual((v["tts_voice"], v["tts_rate"]), ("Microsoft Zira", 1.6))
            self.assertEqual(c.put("/api/voice", json={"tts_voice": 3}).status_code, 400)
            self.assertEqual(c.put("/api/voice", json={"tts_rate": "fast"}).status_code, 400)
            # a Settings form saved with an old copy of the config must not switch the mic back
            c.put("/api/settings", json={"stt": {"wake": {"enabled": False}, "read_aloud": "off"}})
            self.assertTrue(app.cfg["stt"]["wake"]["enabled"])
            self.assertEqual(app.cfg["stt"]["read_aloud"], "voice")
            self.assertTrue(c.get("/api/status").json()["wake"]["enabled"])
            # while a reply is read out (or Strux is paused) the listener stands down
            c.post("/api/voice/speaking", json={"seconds": 5})
            self.assertTrue(app._voice_suppressed())
            c.post("/api/voice/speaking", json={"seconds": 0})
            app.speaking_until = 0
            app.paused = True
            self.assertTrue(app._voice_suppressed())
            app.paused = False
            self.assertEqual(c.post("/api/voice/speaking", json={"seconds": "x"}).json(), {"ok": True})
        finally:
            app.planner.close()
            app.memory.close()
            app.store.stop()
            tmp.cleanup()


class Curation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(self.tmp.name)
        self.store.start()

    def tearDown(self):
        self.store.stop()
        self.tmp.cleanup()

    def job(self, **kw):
        j = {"ts": time.time() - 5, "app": "node.exe", "runtime_s": 600, "label": "npm run build in shop", **kw}
        return R.JobDone().evaluate(types.SimpleNamespace(now=time.time(), current_app="chrome.exe", jobs_done=[j]), self.store, {"min_minutes": 3})

    def test_only_your_longer_jobs(self):
        self.assertIsNotNone(self.job())
        self.assertIsNone(self.job(agent=True))                                 # Claude Code's own tool call
        self.assertIsNone(self.job(runtime_s=60))                               # 1-min build: not news
        self.assertIsNone(self.job(label="eslint in agent-a51e4f76c5a12be4f"))  # background-agent worktree

    def test_heads_ups_expire_without_teaching(self):
        cfg = deep_merge(DEFAULTS["brain"], {"llm": {"enabled": False}, "quiet_when_idle": False})
        b = Brain(cfg, self.store, lambda s: None)
        gone = []
        b.on_expire = gone.append
        old = time.time() - 20 * 60
        info = self.store.add_suggestion(Suggestion(text="“npm run build” finished", source="rule:job_done", ts=old))
        act = self.store.add_suggestion(Suggestion(text="Error in VS Code", source="rule:error_on_screen", ts=old,
                                                   context={"action": {"type": "explain_error"}}))
        fresh = self.store.add_suggestion(Suggestion(text="“pytest” finished", source="rule:job_done", ts=time.time() - 60))
        b._expire_info(time.time())
        self.assertEqual(gone, [info])
        self.assertEqual(self.store.suggestion(info)["feedback"], "expired")
        self.assertIsNone(self.store.suggestion(act)["feedback"])               # has something to do: stays
        self.assertIsNone(self.store.suggestion(fresh)["feedback"])
        self.assertEqual(b._learned_cooldown("rule:job_done", 2), 2)             # expiring is not dismissing


class Ollama(unittest.TestCase):
    def _llm(self, base="http://localhost:11434", up=False):
        from watcher.brain.llm import LLM
        llm = LLM({"base_url": base})
        llm.reachable = lambda: up
        return llm

    def test_already_up(self):
        from watcher.brain.llm import start_ollama
        self.assertEqual(start_ollama(self._llm(up=True))["started"], False)

    def test_remote_server_is_not_started_here(self):
        from watcher.brain.llm import start_ollama
        r = start_ollama(self._llm("http://10.0.0.5:11434"))
        self.assertFalse(r["ok"])
        self.assertIn("isn't a local Ollama", r["error"])

    def test_not_installed(self):
        from unittest import mock
        from watcher.brain import llm as L
        with mock.patch.object(L, "_ollama_exe", return_value=None):
            r = L.start_ollama(self._llm())
        self.assertFalse(r["ok"])
        self.assertIn("isn't installed", r["error"])

    def test_warm_chat_model_counts_as_ours(self):
        from unittest import mock
        from watcher.brain import llm as L
        llm = L.LLM({"model": "qwen2.5:7b", "vision_model": "qwen2.5vl:3b"})
        ps = {"models": [{"name": "qwen2.5:7b", "size_vram": 4200 << 20}, {"name": "someone-elses:13b", "size_vram": 900 << 20}]}
        with mock.patch.object(L, "_http", return_value=ps):
            self.assertEqual(llm.resident_vram_mb(), 4200)
        with mock.patch.object(L, "_http", side_effect=OSError("down")):
            self.assertEqual(llm.resident_vram_mb(), 4200)                # cached for 5 s
            llm._res_at = 0
            self.assertEqual(llm.resident_vram_mb(), 0)

    def test_own_whisper_vram_is_not_a_busy_gpu(self):
        from watcher.brain.llm import GpuGuard, system_busy
        g = GpuGuard.__new__(GpuGuard)
        g._handle = None
        g._nvml = types.SimpleNamespace(nvmlDeviceGetUtilizationRates=lambda h: types.SimpleNamespace(gpu=2),
                                        nvmlDeviceGetMemoryInfo=lambda h: types.SimpleNamespace(free=4409 << 20, total=6144 << 20))
        g.own_mb = lambda: 0
        self.assertIn("vram free", system_busy({"max_cpu_percent": 101, "llm": {"min_free_vram_mb": 4800}}, g) or "")
        g.own_mb = lambda: 1300                                          # resident large-v3-turbo for "Hey Strux"
        self.assertIsNone(system_busy({"max_cpu_percent": 101, "llm": {"min_free_vram_mb": 4800}}, g))


if __name__ == "__main__":
    unittest.main()
