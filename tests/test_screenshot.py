import unittest

from PIL import Image, ImageDraw

from watcher.events import Bus
from watcher.sensors import phash
from watcher.sensors.screenshot import ScreenshotSensor, score_frame, text_diff_ratio


def _img(seed: int, text: str = "") -> Image.Image:
    img = Image.new("RGB", (320, 200), (seed * 37 % 255, seed * 91 % 255, seed * 13 % 255))
    d = ImageDraw.Draw(img)
    d.rectangle((seed % 100, 20, seed % 100 + 120, 140), fill="white")
    if text:
        d.text((10, 160), text, fill="black")
    return img


class PhashTests(unittest.TestCase):
    def test_identical_images_zero_distance(self):
        a = phash.phash(_img(1))
        self.assertEqual(phash.hamming(a, phash.phash(_img(1))), 0)

    def test_small_change_small_distance(self):
        a, b = phash.phash(_img(1)), phash.phash(_img(1, "hello"))
        self.assertLess(phash.hamming(a, b), 10)

    def test_different_images_large_distance(self):
        grid = Image.new("RGB", (320, 200), "white")
        d = ImageDraw.Draw(grid)
        for x in range(0, 320, 40):
            for y in range(0, 200, 40):
                if (x // 40 + y // 40) % 2 == 0:
                    d.rectangle((x, y, x + 40, y + 40), fill="black")
        a, b = phash.phash(_img(1)), phash.phash(grid)
        self.assertGreaterEqual(phash.hamming(a, b), 10)

    def test_hex_roundtrip(self):
        h = phash.phash(_img(3))
        self.assertEqual(phash.from_hex(phash.to_hex(h)), h)


class ScoreTests(unittest.TestCase):
    def test_score_components(self):
        self.assertEqual(score_frame(app_changed=True, visual_distance=20, text_diff=None, error_hit=True,
                                     clipboard_recent=True, idle_seconds=0), 95)
        self.assertEqual(score_frame(app_changed=False, visual_distance=2, text_diff=0.1, error_hit=False,
                                     clipboard_recent=False, idle_seconds=300), 0)
        self.assertEqual(score_frame(app_changed=False, visual_distance=2, text_diff=0.5, error_hit=False,
                                     clipboard_recent=False, idle_seconds=10), 35)

    def test_text_diff(self):
        self.assertIsNone(text_diff_ratio(None, "a"))
        self.assertEqual(text_diff_ratio("a b c", "a b c"), 0.0)
        self.assertEqual(text_diff_ratio("a b", "c d"), 1.0)


class BlocklistTests(unittest.TestCase):
    def test_blocklist(self):
        cfg = {"blocklist_apps": ["KeePassXC.exe"], "blocklist_title_keywords": ["Incognito"], "on_window_change": False}
        s = ScreenshotSensor(Bus(), cfg, store=None, data_dir=".")
        self.assertTrue(s.is_blocked("keepassxc.exe", "db"))
        self.assertTrue(s.is_blocked("chrome.exe", "Gmail - Incognito"))
        self.assertFalse(s.is_blocked("chrome.exe", "Gmail"))


if __name__ == "__main__":
    unittest.main()
