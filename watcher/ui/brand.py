"""The Strux mark, drawn with Pillow: a ring with one orbiting dot on a blue squircle.

Used for the app icon (assets/make_icon.py), the tray and the native bar. ui/src/components/Brand.tsx draws the same in SVG.
"""
import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter

TOP, BOTTOM = (88, 160, 255), (37, 84, 214)          # gradient, top-left -> bottom-right


def mark(size: int = 1024, color: tuple | None = None) -> Image.Image:
    S = size * 4                                       # supersample, then shrink
    tile = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tile).rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.235), fill=255)

    if color is None:
        v = Image.linear_gradient("L").resize((S, S))
        grad = ImageChops.add(v, v.rotate(90), scale=2.0)                     # diagonal: 0 top-left .. 255 bottom-right
        bg = Image.composite(Image.new("RGB", (S, S), BOTTOM), Image.new("RGB", (S, S), TOP), grad)
    else:
        bg = Image.new("RGB", (S, S), color)
    img = bg.convert("RGBA")
    # soft light from the top
    sheen = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sheen).ellipse((-S * 0.3, -S * 0.75, S * 1.3, S * 0.45), fill=46)
    img = Image.composite(Image.new("RGBA", (S, S), (255, 255, 255, 255)), img, sheen.filter(ImageFilter.GaussianBlur(S * 0.06)))

    c, R, w = S / 2, S * 0.255, S * 0.088              # ring centre, radius, stroke
    ring = Image.new("L", (S, S), 0)
    ImageDraw.Draw(ring).ellipse((c - R - w / 2, c - R - w / 2, c + R + w / 2, c + R + w / 2), fill=255)
    ImageDraw.Draw(ring).ellipse((c - R + w / 2, c - R + w / 2, c + R - w / 2, c + R - w / 2), fill=0)
    # the orbiting dot sits on the ring at 1-2 o'clock, with a gap cut around it
    a = math.radians(-50)
    dx, dy = c + R * math.cos(a), c + R * math.sin(a)
    gap, dot = S * 0.115, S * 0.072
    ImageDraw.Draw(ring).ellipse((dx - gap, dy - gap, dx + gap, dy + gap), fill=0)
    ImageDraw.Draw(ring).ellipse((dx - dot, dy - dot, dx + dot, dy + dot), fill=255)

    glow = ring.filter(ImageFilter.GaussianBlur(S * 0.022)).point(lambda v: int(v * 0.35))
    white = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    img = Image.composite(white, img, glow)
    img = Image.composite(white, img, ring)
    img.putalpha(ImageChops.multiply(tile, img.getchannel("A")))
    return img.resize((size, size), Image.Resampling.LANCZOS)

