"""Files dropped on the bar, Ctrl+Alt+V clipboard attachments."""
import ctypes
import io
import os
import tempfile
import types
import unittest
from unittest import mock

from PIL import Image

from watcher.brain import attachments as A


def make_hdrop(paths):
    """A real HDROP (DROPFILES + double-NUL-terminated wide paths) in this process, like Explorer sends."""
    k32 = ctypes.windll.kernel32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    files = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    header = (20).to_bytes(4, "little") + bytes(12) + (1).to_bytes(4, "little")      # pFiles=20, pt, fNC, fWide=1
    h = k32.GlobalAlloc(0x0042, len(header) + len(files))                            # GHND
    ptr = k32.GlobalLock(h)
    ctypes.memmove(ptr, header + files, len(header) + len(files))
    k32.GlobalUnlock(h)
    return h


class DropTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.txt = os.path.join(self.tmp.name, "notes.txt")
        with open(self.txt, "w", encoding="utf-8") as f:
            f.write("buy milk")
        self.png = os.path.join(self.tmp.name, "shot.png")
        Image.new("RGB", (2560, 1440), "white").save(self.png)

    def tearDown(self):
        self.tmp.cleanup()

    def test_hdrop_paths(self):
        from watcher.ui.nativebar import _dropped_paths
        self.assertEqual(_dropped_paths(make_hdrop([self.txt, self.png])), [self.txt, self.png])

    def test_from_paths(self):
        atts, problems = A.from_paths([self.txt, self.png, self.tmp.name])
        self.assertEqual([a["name"] for a in atts], ["notes.txt", "shot.png"])
        self.assertEqual(atts[1]["type"], "image/jpeg")
        self.assertTrue(atts[1]["preview"].startswith("data:image/jpeg;base64,"))
        self.assertIn("folders can't be attached", problems[0])
        self.assertEqual(A.file_text(atts[0]), "buy milk")                    # same shape the chat endpoint reads

    def test_from_clipboard(self):
        with mock.patch("PIL.ImageGrab.grabclipboard", return_value=Image.new("RGB", (100, 50), "red")):
            atts, _ = A.from_clipboard()
        self.assertEqual((atts[0]["name"], atts[0]["type"]), ("clipboard.png", "image/jpeg"))
        with mock.patch("PIL.ImageGrab.grabclipboard", return_value=[self.txt]):
            atts, _ = A.from_clipboard()
        self.assertEqual(atts[0]["name"], "notes.txt")                        # files copied in Explorer
        with mock.patch("PIL.ImageGrab.grabclipboard", return_value=None):
            atts, problems = A.from_clipboard()
        self.assertEqual(atts, [])
        self.assertIn("nothing to attach", problems[0])

    def test_overlay_hands_attachments_to_the_pane(self):
        from watcher.overlay import Overlay
        sent, tabs, alerts = [], [], []
        ov = Overlay.__new__(Overlay)                 # no native bar / windows needed for this path
        import threading
        ov._lock, ov._chat_seq, ov.attach_log, ov.chat_log = threading.RLock(), 0, [], []
        ov.broadcast = sent.append
        ov.show_tab = tabs.append
        ov.alert = lambda key, text, **kw: alerts.append(text)
        ov.attach_paths([self.txt, os.path.join(self.tmp.name, "missing.pdf")])
        self.assertEqual(sent[0]["item"], "attach")
        self.assertEqual([a["name"] for a in sent[0]["attachments"]], ["notes.txt"])
        self.assertEqual(ov.attach_log[0]["id"], sent[0]["id"])            # kept for a pane that wasn't loaded yet
        self.assertEqual(tabs, ["chat"])
        self.assertTrue(alerts and "missing.pdf" in alerts[0])


if __name__ == "__main__":
    unittest.main()
