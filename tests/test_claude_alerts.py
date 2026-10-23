"""Claude Code alerts: only when Claude needs you, with useful text."""
import time
import types
import unittest

from watcher.claude_bridge import ClaudeBridge
from watcher.events import Bus


class Alerts(unittest.TestCase):
    def setUp(self):
        self.alerts = []
        self.fg = "brave.exe"
        ov = types.SimpleNamespace(alert=lambda key, text, **kw: self.alerts.append((key, text, kw.get("level"))))
        self.b = ClaudeBridge(Bus(), ov, foreground=lambda: self.fg, grace=0.05)

    def ev(self, name, **kw):
        self.b.ingest({"hook_event_name": name, "session_id": "s1", "cwd": r"C:\code\watcher-ai", **kw})

    def turn(self, took=30.0, prompt="fix the dashboard spacing"):
        self.ev("UserPromptSubmit", prompt=prompt)
        self.b.sessions["s1"]["turn_start"] -= took
        self.ev("PreToolUse", tool_name="Edit", tool_input={"file_path": "Settings.tsx"})
        self.ev("Stop")

    def test_finished_alert_text(self):
        self.turn()
        time.sleep(0.2)
        self.assertEqual(len(self.alerts), 1)
        key, text, level = self.alerts[0]
        self.assertEqual(text, "Claude finished in watcher-ai after 30 s: “fix the dashboard spacing”")

    def test_no_alert_when_it_carries_on(self):
        self.turn()
        self.ev("PreToolUse", tool_name="Bash", tool_input={"command": "npm test"})     # woke up by itself
        time.sleep(0.2)
        self.assertEqual(self.alerts, [])

    def test_no_alert_for_background_or_quick_or_watched(self):
        self.ev("UserPromptSubmit", prompt="<task-notification>build done</task-notification>")
        self.ev("Stop")                                                                  # nobody typed: background
        time.sleep(0.2)
        self.turn(took=5)                                                                # quick answer
        time.sleep(0.2)
        self.fg = "windowsterminal.exe"
        self.turn(took=120)                                                              # you are looking at it
        time.sleep(0.2)
        self.assertEqual(self.alerts, [])

    def test_idle_reminder_never_alerts(self):
        self.turn()
        time.sleep(0.2)
        self.ev("Notification", message="Claude is waiting for your input", notification_type="idle_prompt")
        self.assertEqual(len(self.alerts), 1)                                            # only the finished one

    def test_subagent_stop_never_alerts(self):
        self.ev("UserPromptSubmit", prompt="research this")
        self.b.sessions["s1"]["turn_start"] -= 100
        self.ev("SubagentStop")
        time.sleep(0.2)
        self.assertEqual(self.alerts, [])

    def test_approval_says_what(self):
        self.ev("UserPromptSubmit", prompt="build it")
        self.ev("PreToolUse", tool_name="Bash", tool_input={"command": "npm run build"})
        self.ev("Notification", message="Claude needs your permission to use Bash", notification_type="permission_prompt")
        self.assertEqual(self.alerts[-1][1:], ("Claude needs your OK in watcher-ai: run “npm run build”", "warn"))


if __name__ == "__main__":
    unittest.main()
