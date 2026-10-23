"""The Strux mark, drawn with Pillow: a geometric white S ending in an orbiting dot, on a blue squircle.

Used for the app icon (assets/make_icon.py), the tray and the native bar. ui/src/components/Brand.tsx draws the same in SVG.
"""
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

    # the glyph, on a 32-unit grid (same path as Brand.tsx): a geometric S of two half-round turns, its top
    # stroke ending in the orbiting dot carried over from the old Halo mark
    k, w = S / 32, S / 32 * 2.8
    ring = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(ring)
    P = lambda x, y: (x * k, y * k)

    def bar(x1, x2, y):
        d.line([P(x1, y), P(x2, y)], fill=255, width=int(w))

    def cap(x, y, r=w / 2):
        cx, cy = P(x, y)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)

    def turn(cx, cy, start, end, r=3.25):
        x, y = P(cx, cy)
        R = r * k + w / 2
        d.arc((x - R, y - R, x + R, y + R), start, end, fill=255, width=int(w))

    bar(11.85, 17.1, 9.5); cap(17.1, 9.5)
    turn(11.85, 12.75, 90, 270)
    bar(11.85, 18.35, 16)
    turn(18.35, 19.25, 270, 90)
    bar(9.1, 18.35, 22.5); cap(9.1, 22.5)
    cap(22.1, 9.5, 2.2 * k)                            # the dot

    glow = ring.filter(ImageFilter.GaussianBlur(S * 0.022)).point(lambda v: int(v * 0.35))
    white = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    img = Image.composite(white, img, glow)
    img = Image.composite(white, img, ring)
    img.putalpha(ImageChops.multiply(tile, img.getchannel("A")))
    return img.resize((size, size), Image.Resampling.LANCZOS)

