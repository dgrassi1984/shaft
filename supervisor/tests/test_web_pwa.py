"""The web UI as an app on the Home Screen — its manifest, icons, service worker and offline page,
pairing from inside the app, a cookie that does not lapse, and a page that knows the server was updated."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from datetime import datetime, timedelta
from unittest import mock

from supervisor import web, web_icons, web_pages
from supervisor.tests.test_web import WebTest


class ShellTest(WebTest):
    def test_the_shell_is_served_before_pairing_and_only_on_the_tailnet(self):
        for path, ctype in (("/manifest.webmanifest", "application/manifest+json"),
                            ("/sw.js", "text/javascript; charset=utf-8"), ("/offline", "text/html; charset=utf-8")):
            r = self.req(path)                                         # the tailnet login, no paired cookie
            self.assertEqual((r.status, r.headers["Content-Type"]), (200, ctype), path)
            self.assertEqual(self.req(path, login=None).status, 403, path)
            self.assertEqual(self.req(path, host="evil.example").status, 403, path)

    def test_the_manifest_installs_a_standalone_app_with_its_icons(self):
        m = json.loads(self.req("/manifest.webmanifest").body)
        self.assertEqual((m["id"], m["start_url"], m["scope"], m["display"]), ("/", "/", "/", "standalone"))
        self.assertEqual({i["sizes"] for i in m["icons"]}, {"180x180", "192x192", "512x512"})
        self.assertIn("maskable", {i["purpose"] for i in m["icons"]})
        for icon in m["icons"]:
            r = self.req(icon["src"])
            self.assertEqual((r.status, r.headers["Content-Type"]), (200, "image/png"), icon["src"])
            self.assertIn("immutable", r.headers["Cache-Control"])           # its URL carries the files' hash

    def test_the_icons_are_what_the_generator_draws(self):
        for name in ("icon-180.png", "icon-192.png"):                      # the big ones take seconds: same code
            self.assertEqual((web_icons.STATIC / name).read_bytes(), web_icons.png(**web_icons.ICONS[name]), name)

    def test_every_page_names_its_manifest_and_app(self):
        body = self.req("/overview", cookie=self.paired()).body.decode()
        for tag in ('<link rel="manifest" href="/manifest.webmanifest">', 'viewport-fit=cover',
                    '<meta name="apple-mobile-web-app-title" content="SHAFT">',
                    'name="apple-mobile-web-app-status-bar-style" content="black-translucent"',
                    '<meta name="theme-color" content="#141413" media="(prefers-color-scheme: dark)">',
                    f'<meta name="static-version" content="{web_pages.STATIC_VERSION}">'):
            self.assertIn(tag, body)

    def test_the_service_worker_caches_only_versioned_static_files_and_the_offline_page(self):
        sw = self.req("/sw.js")
        self.assertEqual(sw.headers["Cache-Control"], "no-cache")           # an update is seen at once
        js = sw.body.decode()
        self.assertIn(f'const VERSION = "{web_pages.STATIC_VERSION}";', js)
        self.assertIn('if (req.method !== "GET") return;', js)               # never a POST
        self.assertIn('url.pathname.startsWith("/static/") && url.searchParams.get("v") === VERSION', js)
        self.assertNotIn("/fragment", js)
        self.assertEqual(js.count("cache.put") + js.count("c.put("), 1)       # one place puts: the static branch
        self.assertIn('caches.match("/offline")', js)

    def test_every_answer_says_the_static_version(self):
        self.assertEqual(self.req("/overview", cookie=self.paired()).headers["X-Shaft-Static"],
                         web_pages.STATIC_VERSION)


class PairInTheAppTest(WebTest):
    """A Home Screen app has its own cookies and no address bar: it pairs by pasting the link on the refusal page."""

    def post(self, **form):
        return self.req("/pair", "POST", body=urllib.parse.urlencode(form).encode(),
                        headers={"sec-fetch-site": "same-origin", "content-type": "application/x-www-form-urlencoded"})

    def test_the_refusal_page_takes_the_pasted_link(self):
        r = self.req("/i/wisp/chat")
        self.assertEqual(r.status, 403)
        self.assertIn('<form method="post" action="/pair" class="pairform"><input name="link"', r.body.decode())

    def test_a_pasted_link_or_token_pairs_once(self):
        url = web.new_pairing("phone-app")
        r = self.post(link=f"  {url}  ")
        self.assertEqual((r.status, r.headers["Location"]), (303, "/"))
        self.assertIn("SameSite=Lax", r.headers["Set-Cookie"])
        self.assertEqual(self.post(link=url).status, 403)                    # once
        token = web.new_pairing("other").split("t=")[1]
        self.assertEqual(self.post(link=token).status, 303)

    def test_a_cross_site_paste_is_refused(self):
        url = web.new_pairing("phone-app")
        r = self.req("/pair", "POST", body=urllib.parse.urlencode({"link": url}).encode(),
                     headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(r.status, 403)


class RenewTest(WebTest):
    def test_a_cookie_in_use_is_sent_again_once_a_day(self):
        """It was never renewed: 400 days after pairing a phone in daily use found itself unpaired."""
        cookie = self.paired()
        dev = web.device_from_cookie(cookie)[0]
        fresh = self.req("/overview", cookie=cookie)
        self.assertNotIn("Set-Cookie", fresh.headers)                       # paired today: nothing to renew
        with web._lock:
            ds = web._read("devices.json")
            ds[dev]["created"] = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
            web._write("devices.json", ds)
        old = self.req("/overview", cookie=cookie)
        renewed = old.headers["Set-Cookie"]
        renewed = renewed if isinstance(renewed, str) else next(c for c in renewed if c.startswith("shaft_device="))
        self.assertTrue(renewed.startswith(cookie.split(";")[0]))
        self.assertIn("Max-Age=34560000", renewed)
        self.assertNotIn("Set-Cookie", self.req("/overview", cookie=cookie).headers)   # once a day
