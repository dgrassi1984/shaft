"""C17: iOS's cookie-free install fetch, exact startup dimensions/media, and bounded rendering cost."""
from __future__ import annotations

import struct
import time
import zlib
from html.parser import HTMLParser
from unittest import mock

from supervisor import web, web_icons, web_pages
from supervisor.tests.test_web import WebTest


class Links(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.links = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == "link":
            self.links.append(dict(attrs))


class LaunchTest(WebTest):
    def png_rows(self, body, width, height):
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")
        pos, chunks = 8, []
        while pos < len(body):
            size = struct.unpack(">I", body[pos:pos + 4])[0]
            kind, data = body[pos + 4:pos + 8], body[pos + 8:pos + 8 + size]
            crc = struct.unpack(">I", body[pos + 8 + size:pos + 12 + size])[0]
            self.assertEqual(crc, zlib.crc32(kind + data) & 0xffffffff)
            chunks.append((kind, data))
            pos += size + 12
        self.assertEqual([kind for kind, _ in chunks], [b"IHDR", b"IDAT", b"IEND"])
        self.assertEqual(struct.unpack(">IIBBBBB", chunks[0][1]), (width, height, 8, 6, 0, 0, 0))
        rows = zlib.decompress(chunks[1][1])
        self.assertEqual(len(rows), height * (1 + width * 4))
        self.assertEqual(set(rows[::1 + width * 4]), {0})
        return rows

    def test_launch_is_an_exact_png_in_both_schemes_and_orientations_without_pairing(self):
        for width, height in ((750, 1334), (1334, 750)):
            for scheme, bg in (("light", b"\xf6\xf6\xf4\xff"), ("dark", b"\x14\x14\x13\xff")):
                with self.subTest(width=width, height=height, scheme=scheme):
                    path = f"/launch/{width}x{height}-{scheme}.png?v={web_pages.STATIC_VERSION}"
                    with mock.patch.object(web, "_paired") as paired:
                        r = self.req(path)                         # no device cookie, as WebKit fetches at install
                        paired.assert_not_called()
                    self.assertEqual((r.status, r.headers["Content-Type"]), (200, "image/png"))
                    self.assertEqual(r.headers["Cache-Control"], "public, max-age=31536000, immutable")
                    rows = self.png_rows(r.body, width, height)
                    self.assertEqual(rows[1:5], bg)
                    self.assertEqual(rows[-4:], bg)
                    centre = (height // 2) * (1 + width * 4) + 1 + (width // 2) * 4
                    self.assertEqual(rows[centre:centre + 4], b"\x7a\xa2\xff\xff")
                    self.assertEqual(self.req(path, login=None).status, 403)
                    self.assertEqual(self.req(path, host="evil.example").status, 403)

    def test_launch_refuses_unlisted_sizes_and_schemes_before_rendering(self):
        with mock.patch.object(web_icons, "launch_png") as draw:
            for name in ("751x1334-light.png", "9999x9999-dark.png", "750x1334-blue.png", "0750x1334-light.png"):
                self.assertEqual(self.req("/launch/" + name).status, 404, name)
            draw.assert_not_called()

    def test_each_size_and_scheme_is_rendered_once_and_uses_static_cache_policy(self):
        with mock.patch.object(web, "_launch_images", {}), \
                mock.patch.object(web_icons, "launch_png", wraps=web_icons.launch_png) as draw:
            r = self.req("/launch/750x1334-light.png")
            again = self.req(f"/launch/750x1334-light.png?v={web_pages.STATIC_VERSION}")
            draw.assert_called_once_with(750, 1334, web_pages.THEME["light"])
            self.assertEqual(r.body, again.body)
            self.assertEqual(r.headers["Cache-Control"], "max-age=3600")
            self.assertEqual(r.headers["Cache-Control"], self.req("/static/icon-152.png").headers["Cache-Control"])
            self.assertEqual(again.headers["Cache-Control"],
                             self.req(f"/static/icon-152.png?v={web_pages.STATIC_VERSION}").headers["Cache-Control"])

    def test_page_links_every_device_orientation_and_scheme_and_all_touch_icons(self):
        links = Links(web_pages.page("Launch", web_pages.Html(""))).links
        startup = [link for link in links if link.get("rel") == "apple-touch-startup-image"]
        devices = ((375, 667, 2), (375, 812, 3), (390, 844, 3), (393, 852, 3), (402, 874, 3),
                   (414, 896, 2), (414, 896, 3), (428, 926, 3), (430, 932, 3), (440, 956, 3),
                   (744, 1133, 2), (768, 1024, 2), (810, 1080, 2), (820, 1180, 2), (834, 1194, 2),
                   (1024, 1366, 2), (1032, 1376, 2))
        self.assertEqual(len(startup), 68)
        for w, h, r in devices:
            for orientation, pw, ph in (("portrait", w * r, h * r), ("landscape", h * r, w * r)):
                for scheme in ("light", "dark"):
                    self.assertIn({"rel": "apple-touch-startup-image",
                                   "href": f"/launch/{pw}x{ph}-{scheme}.png?v={web_pages.STATIC_VERSION}",
                                   "media": f"(device-width: {w}px) and (device-height: {h}px) and "
                                            f"(-webkit-device-pixel-ratio: {r}) and (orientation: {orientation}) "
                                            f"and (prefers-color-scheme: {scheme})"}, startup)
        for size in (152, 167, 180):
            self.assertIn({"rel": "apple-touch-icon", "sizes": f"{size}x{size}",
                           "href": f"/static/icon-{size}.png?v={web_pages.STATIC_VERSION}"}, links)
            name = f"icon-{size}.png"
            self.assertEqual((web_icons.STATIC / name).read_bytes(), web_icons.png(**web_icons.ICONS[name]))

    def test_service_worker_leaves_launch_images_to_the_network(self):
        js = self.req("/sw.js").body.decode()
        guard = 'if (url.pathname.startsWith("/launch/")) return;'
        self.assertIn(guard, js)
        self.assertLess(js.index(guard), js.index('if (req.mode === "navigate")'))
        self.assertNotIn("/launch/", js.split('const SHELL = [', 1)[1].split('];', 1)[0])

    def test_largest_ipad_render_is_under_two_seconds(self):
        start = time.perf_counter()
        body = web_icons.launch_png(2064, 2752, web_pages.THEME["light"])
        elapsed = time.perf_counter() - start
        print(f"\nC17 largest render (2064x2752): {elapsed:.6f} s", flush=True)
        self.assertLess(elapsed, 2.0)
        self.png_rows(body, 2064, 2752)
