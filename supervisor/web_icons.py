"""The web UI's app icons, drawn here (stdlib only: no image tools on the Mac) and written to web_static/.

One drawing — a shaft of light on a dark tile, as favicon.svg — at the sizes a Home Screen app needs (docs/plans/PLAN-pwa P1):
  icon-152.png, -167, -180 Apple's touch icon: opaque and full bleed (iOS rounds the corners itself; a transparent
                         corner showed as black)
  icon-192.png, -512     the manifest's icons: the rounded tile, transparent outside it
  icon-maskable-512.png  the manifest's maskable icon: full bleed, the glyph inside the central 80% safe zone

`python3 -m supervisor.web_icons` writes them; test_web_pwa checks the files are what this draws.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "web_static"
BG, BAR, DOT = (0x14, 0x14, 0x13), (0x7a, 0xa2, 0xff), (0xec, 0xec, 0xe8)
SS = 4                                     # supersampling per axis


def _rrect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    cx, cy = min(max(x, x0 + r), x1 - r), min(max(y, y0 + r), y1 - r)
    return x0 <= x <= x1 and y0 <= y <= y1 and (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _colour(x: float, y: float, bleed: bool, glyph: float) -> tuple[int, int, int, int]:
    """The 64-unit drawing at (x, y). bleed: the tile fills the square; glyph: the glyph's scale about the centre."""
    gx, gy = 32 + (x - 32) / glyph, 32 + (y - 32) / glyph
    if (gx - 32) ** 2 + (gy - 18) ** 2 <= 16:
        return DOT + (255,)
    if _rrect(gx, gy, 26, 10, 38, 54, 6):
        return BAR + (255,)
    if bleed or _rrect(x, y, 2, 2, 62, 62, 14):
        return BG + (255,)
    return (0, 0, 0, 0)


def png(size: int, bleed: bool = False, glyph: float = 1.0, *, inset: float = 0.0) -> bytes:
    """Draw the icon; inset shrinks the whole drawing by that fraction on each side."""
    scale = 1 - 2 * inset
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    x, y = (px + (sx + .5) / SS) * 64 / size, (py + (sy + .5) / SS) * 64 / size
                    if inset:
                        x, y = 32 + (x - 32) / scale, 32 + (y - 32) / scale
                    c = _colour(x, y, bleed, glyph)
                    for k in range(3):
                        acc[k] += c[k] * c[3]
                    acc[3] += c[3]
            row += bytes([round(acc[k] / acc[3]) if acc[3] else 0 for k in range(3)] + [round(acc[3] / (SS * SS))])
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


# CSS portrait size and DPR; both orientations share this bounded allowlist (C17).
LAUNCH_DEVICES = ((375, 667, 2), (375, 812, 3), (390, 844, 3), (393, 852, 3), (402, 874, 3),
                  (414, 896, 2), (414, 896, 3), (428, 926, 3), (430, 932, 3), (440, 956, 3),
                  (744, 1133, 2), (768, 1024, 2), (810, 1080, 2), (820, 1180, 2), (834, 1194, 2),
                  (1024, 1366, 2), (1032, 1376, 2))
LAUNCH_SIZES = frozenset(size for w, h, r in LAUNCH_DEVICES for size in ((w * r, h * r), (h * r, w * r)))


def launch_png(width: int, height: int, background: str) -> bytes:
    """An opaque startup image. Only the glyph's bounds need supersampling, not millions of background pixels."""
    bg = bytes.fromhex(background.lstrip("#")) + b"\xff"
    row = b"\0" + bg * width
    rows = [row] * height
    size = round(min(width, height) * .22)
    left, top = (width - size) // 2, (height - size) // 2
    x0, x1 = int(size * 26 / 64), int(size * 38 / 64) + 1
    for py in range(int(size * 10 / 64), int(size * 54 / 64) + 1):
        glyph = bytearray()
        for px in range(x0, x1):
            acc = [0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    c = _colour((px + (sx + .5) / SS) * 64 / size,
                                (py + (sy + .5) / SS) * 64 / size, True, 1.0)
                    c = bg if c[:3] == BG else c
                    for k in range(3):
                        acc[k] += c[k]
            glyph += bytes(round(v / (SS * SS)) for v in acc) + b"\xff"
        start = 1 + (left + x0) * 4
        rows[top + py] = row[:start] + glyph + row[start + len(glyph):]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


ICONS = {"icon-152.png": dict(size=152, bleed=True, glyph=.9),
         "icon-167.png": dict(size=167, bleed=True, glyph=.9), "icon-180.png": dict(size=180, bleed=True, glyph=.9),
         "icon-192.png": dict(size=192),
         "icon-512.png": dict(size=512), "icon-maskable-512.png": dict(size=512, bleed=True, glyph=.72)}


def main() -> None:
    for name, how in ICONS.items():
        (STATIC / name).write_bytes(png(**how))
        print(f"wrote {STATIC / name}")


if __name__ == "__main__":
    main()
