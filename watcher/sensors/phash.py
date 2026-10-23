"""Perceptual hash (pHash): 64-bit DCT hash, pure Python (no numpy, saves ~12 MB RSS).

A 32x32 grayscale DCT reduced to the 8x8 low-frequency block is ~16k
multiply-adds, a few milliseconds in CPython, which is nothing next to the
screen grab itself. Two frames whose hashes differ by < ~6 bits are visually
the same screen.
"""
from __future__ import annotations

import math

from PIL import Image

_HASH = 8
_HIGH = 4
_N = _HASH * _HIGH  # 32


def _dct_matrix(n: int) -> list[list[float]]:
    m = [[math.cos(math.pi * (2 * i + 1) * k / (2 * n)) * math.sqrt(2 / n) for i in range(n)] for k in range(n)]
    m[0] = [v / math.sqrt(2) for v in m[0]]
    return m


_D = _dct_matrix(_N)          # _D[k][i]
_D8 = _D[:_HASH]              # only the first 8 frequency rows are needed


def phash(img: Image.Image) -> int:
    g = img.convert("L").resize((_N, _N), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    a = [px[r * _N:(r + 1) * _N] for r in range(_N)]
    # tmp = D8 @ a  (8 x 32)
    tmp = [[sum(dk[i] * a[i][j] for i in range(_N)) for j in range(_N)] for dk in _D8]
    # low = tmp @ D8^T  (8 x 8)
    flat = [sum(tk[j] * dl[j] for j in range(_N)) for tk in tmp for dl in _D8]
    srt = sorted(flat)
    med = (srt[31] + srt[32]) / 2
    out = 0
    for v in flat:
        out = (out << 1) | (1 if v > med else 0)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def to_hex(h: int) -> str:
    return f"{h:016x}"


def from_hex(s: str) -> int:
    return int(s, 16)
