"""Writes the Strux icon files: assets/strux.png, assets/strux.ico, ui/public/favicon.png.

Run: .venv/Scripts/python assets/make_icon.py
"""
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from watcher.ui.brand import mark  # noqa: E402

if __name__ == "__main__":
    big = mark(1024)
    big.resize((512, 512), Image.Resampling.LANCZOS).save(ROOT / "assets" / "strux.png")
    sizes = [(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
    big.save(ROOT / "assets" / "strux.ico", sizes=sizes)
    big.resize((128, 128), Image.Resampling.LANCZOS).save(ROOT / "ui" / "public" / "favicon.png")
    print("ok")
