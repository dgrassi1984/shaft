"""The web UI's front door (PLAN-webui.md M0): who may use it (a tailnet login in SHAFT_API_LOGINS and a paired
browser), forged requests refused, instance-written text never running as markup, and a port no run can reach."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import core, web, web_pages

HOST = "mac.tail1234.ts.net:8444"
ME = "daniele@example.com"


def snap(**inst) -> dict:
    i = {"name": "wren", "state": "idle", "since": "", "doing": "", "goal": "", "model": "cloud mimo-v2.6-pro",
         "every": "5 min", "next_wake": "12:05", "conversations": 0, "calls_today": 3, "tokens_today": 12_000,
         "usd_today": 0.12, "judged": 4, "success": 0.5, "achieved": 2.0, "value": 3.0, "tools_off": [],
         "trial": "", "recap": {"running": False}, **inst}
    return {"at": "12:00:00", "services": [{"name": "api", "running": True, "detail": "abc since 09:00"},
                                          {"name": "web", "running": False, "detail": ""}],
            "instances": [i], "events": ["12:00:00 wren: cycle done"], "messages": [], "alerts": [], "spend": 0.42}


class WebTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-web-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        self.patches = [mock.patch.object(web, "SUP_DIR", self.sup),
                        mock.patch.object(web, "allowed_logins", return_value={ME}),
                        mock.patch.object(web, "allowed_hosts", return_value={HOST}),
                        mock.patch.object(web, "overview", return_value=snap()),
                        mock.patch("supervisor.web_actions._update_question",
                                   side_effect=lambda n: ("", f"{n} is already on the current template"))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def req(self, path="/", method="GET", login=ME, host=HOST, cookie=None, headers=None, body=b""):
        h = {"host": host, **({"tailscale-user-login": login} if login else {}), **(headers or {})}
        if cookie:
            h["cookie"] = cookie
        return web.respond(web.Request(method, path, h, body))

    def paired(self) -> str:
        """A paired browser's cookie, through the real pairing flow."""
        url = web.new_pairing("laptop")
        token = url.split("t=")[1]
        r = self.req("/pair", "POST", body=f"t={token}".encode(),
                     headers={"sec-fetch-site": "same-origin", "content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 303, r.body)
        return r.headers["Set-Cookie"].split(";")[0]

    def csrf(self, cookie: str) -> str:
        return web.csrf_token(web.device_from_cookie(cookie)[1])


class WhoMayUseItTest(WebTest):
    def test_without_a_tailnet_login_refused(self):
        r = self.req(login=None)
        self.assertEqual(r.status, 403)

    def test_a_login_not_in_the_list_refused(self):
        self.assertEqual(self.req(login="someone@else.com", cookie=self.paired()).status, 403)

    def test_an_allowed_login_without_a_paired_browser_is_told_how_to_pair(self):
        r = self.req()
        self.assertEqual(r.status, 403)
        self.assertIn(b"shaft web pair", r.body)
        self.assertNotIn(b"wren", r.body)          # nothing of the instances before pairing

    def test_a_paired_browser_sees_the_overview(self):
        r = self.req(cookie=self.paired())
        self.assertEqual(r.status, 200)
        self.assertIn(b"wren", r.body)

    def test_a_forged_device_cookie_refused(self):
        dev, _ = self.paired().split("=", 1)[1].split(".")
        self.assertEqual(self.req(cookie=f"shaft_device={dev}.{'0' * 43}").status, 403)

    def test_an_unpaired_device_refused(self):
        cookie = self.paired()
        dev = web.device_from_cookie(cookie)[0]
        self.assertEqual(web.unpair(dev), 1)
        self.assertEqual(self.req(cookie=cookie).status, 403)

    def test_the_device_secret_is_stored_only_as_a_hash(self):
        cookie = self.paired()
        secret = web.device_from_cookie(cookie)[1]
        self.assertNotIn(secret, (self.sup / "web" / "devices.json").read_text())


class PairingTest(WebTest):
    def test_a_pairing_link_works_once(self):
        token = web.new_pairing("phone").split("t=")[1]
        ok = {"sec-fetch-site": "same-origin"}
        self.assertEqual(self.req("/pair", "POST", body=f"t={token}".encode(), headers=ok).status, 303)
        self.assertEqual(self.req("/pair", "POST", body=f"t={token}".encode(), headers=ok).status, 403)

    def test_opening_the_link_does_not_use_it(self):
        token = web.new_pairing("phone").split("t=")[1]
        for _ in range(2):                         # a link preview or a prefetch must not spend it
            r = self.req(f"/pair?t={token}")
            self.assertEqual(r.status, 200)
            self.assertIn(b'method="post"', r.body)
        r = self.req("/pair", "POST", body=f"t={token}".encode(), headers={"sec-fetch-site": "same-origin"})
        self.assertEqual(r.status, 303)

    def test_an_expired_link_refused(self):
        token = web.new_pairing("phone").split("t=")[1]
        with mock.patch.object(web.time, "time", return_value=web.time.time() + web.PAIR_MINUTES * 60 + 1):
            r = self.req("/pair", "POST", body=f"t={token}".encode(), headers={"sec-fetch-site": "same-origin"})
        self.assertEqual(r.status, 403)

    def test_pairing_needs_the_tailnet_login_too(self):
        token = web.new_pairing("phone").split("t=")[1]
        r = self.req("/pair", "POST", login=None, body=f"t={token}".encode(), headers={"sec-fetch-site": "same-origin"})
        self.assertEqual(r.status, 403)

    def test_a_cross_site_pairing_post_refused(self):
        token = web.new_pairing("phone").split("t=")[1]
        r = self.req("/pair", "POST", body=f"t={token}".encode(), headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(r.status, 403)

    def test_the_cookie_is_httponly_secure_and_strict(self):
        url = web.new_pairing("laptop")
        r = self.req("/pair", "POST", body=f"t={url.split('t=')[1]}".encode(), headers={"sec-fetch-site": "same-origin"})
        flags = r.headers["Set-Cookie"].lower()
        for f in ("httponly", "secure", "samesite=strict", "path=/"):
            self.assertIn(f, flags)


class ForgedRequestsTest(WebTest):
    def test_a_wrong_host_refused_even_when_paired(self):
        cookie = self.paired()                      # DNS rebinding: the browser sends the attacker's name as Host
        self.assertEqual(self.req(cookie=cookie, host="evil.example:8785").status, 403)
        self.assertEqual(self.req(cookie=cookie, host="127.0.0.1:8785").status, 403)

    def test_a_post_without_the_csrf_token_refused(self):
        cookie = self.paired()
        r = self.req("/unpair", "POST", cookie=cookie, headers={"sec-fetch-site": "same-origin"})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.req(cookie=cookie).status, 200)          # still paired: nothing was done

    def test_a_cross_site_post_refused_even_with_the_token(self):
        cookie = self.paired()
        r = self.req("/unpair", "POST", cookie=cookie,
                     headers={"sec-fetch-site": "cross-site", "x-csrf-token": self.csrf(cookie)})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.req(cookie=cookie).status, 200)

    def test_a_post_with_no_origin_information_refused(self):
        cookie = self.paired()
        r = self.req("/unpair", "POST", cookie=cookie, headers={"x-csrf-token": self.csrf(cookie)})
        self.assertEqual(r.status, 403)

    def test_a_same_origin_post_with_the_token_works(self):
        cookie = self.paired()
        r = self.req("/unpair", "POST", cookie=cookie,
                     headers={"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(cookie)})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(self.req(cookie=cookie).status, 403)          # this browser is unpaired now

    def test_an_origin_header_must_name_this_host(self):
        cookie = self.paired()
        bad = self.req("/unpair", "POST", cookie=cookie,
                       headers={"origin": "https://evil.example", "x-csrf-token": self.csrf(cookie)})
        self.assertEqual(bad.status, 403)
        good = self.req("/unpair", "POST", cookie=cookie,
                        headers={"origin": f"https://{HOST}", "x-csrf-token": self.csrf(cookie)})
        self.assertEqual(good.status, 200)

    def test_every_response_carries_the_content_security_policy(self):
        for r in (self.req(login=None), self.req(), self.req(cookie=self.paired()),
                  self.req("/static/web.js", cookie=self.paired()), self.req("/nope", cookie=self.paired())):
            csp = r.headers.get("Content-Security-Policy", "")
            self.assertIn("default-src 'self'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertNotIn("unsafe-inline", csp)
            self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")

    def test_no_page_has_inline_script(self):
        body = self.req(cookie=self.paired()).body.decode()
        self.assertNotIn("<script>", body)
        self.assertNotRegex(body, r"<[^>]+\son[a-z]+\s*=")           # no inline event handlers either
        self.assertRegex(body, r'<script src="/static/web\.js[^"]*" defer></script>')

    def test_static_paths_cannot_leave_the_static_folder(self):
        cookie = self.paired()
        for p in ("/static/../web.py", "/static/%2e%2e/web.py", "/static/..%2fweb.py", "/static/"):
            self.assertEqual(self.req(p, cookie=cookie).status, 404, p)


class EscapingTest(WebTest):
    EVIL = '<script>alert(1)</script><img src=x onerror="alert(2)">'

    def test_instance_written_text_never_runs_as_markup(self):
        s = snap(goal=self.EVIL, doing=self.EVIL, name="wren", trial=self.EVIL,
                 recap={"ts": "2026-10-19T11:59:00", "text": self.EVIL, "running": False})
        s["events"] = [self.EVIL]
        s["messages"] = [{"ts": "2026-10-19T11:00:00", "from": "wren", "to": "operator", "read": True,
                          "kind": "note", "text": self.EVIL, "file": "m.md"}]
        s["alerts"] = [{"id": "x", "text": self.EVIL}]
        with mock.patch.object(web, "overview", return_value=s):
            for path in ("/", "/fragment/overview"):
                body = self.req(path, cookie=self.paired()).body.decode()
                self.assertNotIn("<script>alert", body)
                self.assertNotIn("<img", body)
                self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)

    def test_control_characters_are_dropped(self):
        out = web_pages.overview(snap(goal="ok\x1b[31mred\x07"))
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)

    def test_html_is_escaped_unless_already_html(self):
        self.assertEqual(web_pages.esc("<b>"), "&lt;b&gt;")
        self.assertEqual(web_pages.esc(web_pages.Html("<b>")), "<b>")
        self.assertEqual(web_pages.h("<p>{x}</p>", x="<i>"), "<p>&lt;i&gt;</p>")


class OverviewTest(WebTest):
    def test_the_overview_shows_what_the_tui_shows(self):
        s = snap(state="in a cycle", since="11:58", doing="read_file notes.md · 3s", goal="tidy the notes",
                 trial="t3 running: 2/6 pairs", tools_off=["delegate_coding"])
        s["alerts"] = [{"id": "svc:model", "text": "service model is not running"}]
        out = web_pages.overview(s)
        for text in ("in a cycle", "since 11:58", "read_file notes.md · 3s", "tidy the notes", "t3 running: 2/6 pairs",
                     "off: delegate_coding", "service model is not running", "$0.42", "3 model calls", "12k tokens",
                     "$0.12", "value 3.0/5", "achieved 2.0/5", "success 50%", "wakes every 5 min", "next 12:05",
                     "12:00:00 wren: cycle done", "web"):
            self.assertIn(text, out, text)

    def test_the_overview_says_so_when_its_data_failed(self):
        with mock.patch.object(web, "overview", side_effect=RuntimeError("broken log")):
            r = self.req("/fragment/overview", cookie=self.paired())
        self.assertEqual(r.status, 500)
        self.assertIn(b"broken log", r.body)        # never an empty page that looks like "nothing to show"

    def test_the_overview_polls_its_own_fragment(self):
        body = self.req(cookie=self.paired()).body.decode()
        self.assertIn('data-poll="/fragment/overview"', body)


class PortTest(unittest.TestCase):
    def test_the_web_port_is_one_no_run_can_reach(self):
        """A run's sandbox allows the LLM ports and the embed port (runner.py, `ports = …`); the web UI changes
        configs and grants, so an instance must never reach it (the default was once 8782, the embed port)."""
        self.assertNotIn(web.PORT, set(core.LLM_PORTS) | {core.EMBED_PORT})

    def test_serve_refuses_a_port_a_run_can_reach(self):
        with mock.patch.object(web, "PORT", core.EMBED_PORT), mock.patch.object(web, "allowed_logins",
                                                                                 return_value={ME}):
            self.assertEqual(web.serve(), 2)

    def test_the_web_service_is_in_every_service_list(self):
        from supervisor import autonomy, dashboard, panel
        self.assertIn("web", autonomy.SERVICES)
        self.assertIn("web", dashboard.SERVICES)
        self.assertIn("web", panel.SERVICE_LOGS)
        self.assertEqual(autonomy.SERVICES["web"]["args"][0], core.PYTHON)   # no venv for a launchd job


if __name__ == "__main__":
    unittest.main()
