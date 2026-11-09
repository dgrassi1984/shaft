"""The web UI's front door (docs/plans/PLAN-webui.md M0): who may use it (a tailnet login in SHAFT_API_LOGINS and a paired
browser), forged requests refused, instance-written text never running as markup, and a port no run can reach."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import core, web, web_pages

HOST = "mac.tail1234.ts.net:8444"
ME = "operator@example.com"


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
                        mock.patch("supervisor.version.SERVICES", self.sup / "services"),
                        mock.patch("supervisor.version.supervisor_commit", return_value="abc1234"),
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
        self.assertEqual(self.req(login="someone@example.com", cookie=self.paired()).status, 403)

    def test_an_allowed_login_without_a_paired_browser_is_told_how_to_pair(self):
        r = self.req()
        self.assertEqual(r.status, 403)
        self.assertIn(b"shaft web pair", r.body)
        self.assertNotIn(b"wren", r.body)          # nothing of the instances before pairing

    def test_a_paired_browser_sees_the_overview(self):
        r = self.req("/overview", cookie=self.paired())
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

    def test_the_cookie_is_httponly_secure_and_lax(self):
        """Lax, not Strict (docs/plans/PLAN-pwa P1): a link to the UI opened from Telegram or Messages is a cross-site navigation
        and a Strict cookie was not sent. Every POST still needs the page's token and same-origin (ForgedRequestsTest)."""
        url = web.new_pairing("laptop")
        r = self.req("/pair", "POST", body=f"t={url.split('t=')[1]}".encode(), headers={"sec-fetch-site": "same-origin"})
        flags = r.headers["Set-Cookie"].lower()
        for f in ("httponly", "secure", "samesite=lax", "path=/"):
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
        self.assertEqual(self.req("/overview", cookie=cookie).status, 200)   # still paired: nothing was done

    def test_a_cross_site_post_refused_even_with_the_token(self):
        cookie = self.paired()
        r = self.req("/unpair", "POST", cookie=cookie,
                     headers={"sec-fetch-site": "cross-site", "x-csrf-token": self.csrf(cookie)})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.req("/overview", cookie=cookie).status, 200)

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
        body = self.req("/overview", cookie=self.paired()).body.decode()
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
            for path in ("/overview", "/fragment/overview", "/fragment/rail"):
                body = self.req(path, cookie=self.paired()).body.decode()
                self.assertNotIn("<script>alert", body)
                self.assertNotIn("<img", body)
                if path != "/fragment/rail":                   # the rail shows no instance text but its chats'
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
        body = self.req("/overview", cookie=self.paired()).body.decode()
        self.assertIn('data-poll="/fragment/overview"', body)

    def test_issue_34_alerts_past_five_open_and_each_dismisses(self):
        """#34. With more than five open alerts the rest was a "… N more" line, and the only dismissals were the
        first five or all of them. Reverting that leaves alert 5 off the page (or with no Dismiss of its own) and
        drops the data-key, so a refresh shuts the list the operator had opened."""
        import html as _html
        import json as _json
        log_id = "log:2026-10-19 09:48:34:2373c79a19f2"          # a real log alert's id: its time has a space
        many = [{"id": f"a{i}", "text": f"alert {i}"} for i in range(7)] + [{"id": log_id, "text": "a log line"}]
        s = snap()
        s["alerts"] = many
        out = str(web_pages.overview(s))
        self.assertIn("<summary>Show all 8 alerts</summary>", out)
        self.assertIn('<details data-key="all-alerts">', out)
        self.assertNotIn("… 3 more", out)
        self.assertEqual(out.count("alert 0"), 2)                # on view, and again in the full list
        self.assertEqual(out.count("alert 5"), 1)                # past the five: only once the list is open
        self.assertEqual(out.count("a log line"), 1)
        posted = [_json.loads(_html.unescape(v.split('"')[0])) for v in out.split('name="ids" value="')[1:]]
        singles = [p[0] for p in posted if len(p) == 1]
        self.assertEqual(set(singles), {a["id"] for a in many})
        self.assertEqual(singles.count("a0"), 2)
        self.assertEqual(singles.count(log_id), 1)               # one Dismiss, the whole id, space and all
        self.assertIn([f"a{i}" for i in range(5)], posted)       # Dismiss these alerts, still the five on view
        self.assertIn([a["id"] for a in many], posted)           # Dismiss all 8, still every open alert
        self.assertIn("Dismiss all 8", out)
        few = snap()
        few["alerts"] = [{"id": "a0", "text": "only one"}]
        short = str(web_pages.overview(few))
        self.assertIn("only one", short)
        self.assertIn(">Dismiss<", short)
        self.assertIn("Dismiss these alerts", short)
        self.assertNotIn("Show all", short)
        self.assertNotIn("<details", short)
        self.assertNotIn("Dismiss all", short)

    def test_issue_34_no_instances_says_how_to_create_one(self):
        """#34. A fresh install opens on /overview with an empty Chats group and no cards. Reverting the empty
        state removes "No instances yet", the `shaft new` command, or the link to the README quickstart."""
        s = snap()
        s["instances"] = []
        out = str(web_pages.overview(s))
        self.assertIn("No instances yet", out)
        self.assertIn("Instances are created from the terminal", out)
        self.assertIn("<code>shaft new &lt;name&gt;</code>", out)
        self.assertIn('href="https://github.com/dgrassi1984/shaft#quickstart-cloud-only"', out)
        self.assertIn('rel="noopener noreferrer" target="_blank"', out)
        self.assertNotIn("No instances yet", str(web_pages.overview(snap())))
        with mock.patch.object(web, "overview", return_value=s):
            page = self.req("/overview", cookie=self.paired()).body.decode()
            frag = self.req("/fragment/overview", cookie=self.paired()).body.decode()
        self.assertIn("No instances yet", page)
        self.assertIn("shaft new &lt;name&gt;", frag)
        self.assertIn("#quickstart-cloud-only", frag)


class HostsTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-web-hosts-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        for p in (mock.patch.object(web, "SUP_DIR", self.sup), mock.patch.object(web, "WEB_HOSTS", set())):
            p.start()
            self.addCleanup(p.stop)
        web._hosts.update(at=0.0, hosts=set())
        self.addCleanup(web._hosts.update, at=0.0, hosts=set())

    def test_the_service_reads_the_name_shaft_web_on_recorded_and_never_asks_tailscale(self):
        """Live, 2026-10-19: under launchd the Tailscale CLI cannot run, every request was refused as the wrong host."""
        with mock.patch.object(web, "tailnet_hosts", return_value={HOST}):
            self.assertEqual(web.record_hosts(), {HOST})                 # shaft web on, in a terminal
        web._hosts.update(at=0.0)
        with mock.patch.object(web, "tailnet_hosts", side_effect=AssertionError("asked tailscale")):
            self.assertEqual(web.allowed_hosts(), {HOST})                # the service

    def test_on_refuses_when_the_tailnet_name_cannot_be_read(self):
        with mock.patch.object(web, "allowed_logins", return_value={ME}), \
                mock.patch.object(web, "tailnet_hosts", return_value=set()), \
                mock.patch("supervisor.autonomy.service") as service:
            self.assertEqual(web.command("on"), 1)
        service.assert_not_called()


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


class FaviconTest(WebTest):
    def test_every_page_names_its_icon_and_the_browsers_own_request_gets_it(self):
        body = self.req("/overview", cookie=self.paired()).body.decode()
        self.assertRegex(body, r'<link rel="icon" type="image/svg\+xml" href="/static/favicon\.svg\?v=[0-9a-f]+">')
        self.assertIn('<link rel="apple-touch-icon" href="/static/icon-180.png', body)
        for path, ctype in (("/static/favicon.svg", "image/svg+xml"), ("/favicon.ico", "image/png")):
            r = self.req(path)                                    # the tailnet login, no paired cookie needed
            self.assertEqual((r.status, r.headers["Content-Type"]), (200, ctype), path)
        self.assertTrue(self.req("/favicon.ico").body.startswith(b"\x89PNG"))
        self.assertEqual(self.req("/favicon.ico", login=None).status, 403)    # still the operator's tailnet only


class BakeoffJudgePagesTest(WebTest):
    """/bakeoffs: the operator judges bake-off pairs blind in the web UI; their verdicts are saved apart (review
    2026-11-09). The pages go through the same front door as every other: paired browser, CSRF token on the POST."""

    def setUp(self):
        super().setUp()
        from supervisor import bakeoff, trial
        self.bakeoff = bakeoff
        d = self.sup / "bakeoffs"
        d.mkdir()
        rec = lambda model, text: {   # noqa: E731
            "exit": 0, "ended": "ok", "seconds": 60, "model": model, "completed": True, "usd": 0, "usage": {},
            "output_tail": "", "episode": {"intent": {"goal": "<b>goal</b>"}, "finish": {"what_happened": text}},
            "trace": [{"tool": "send_message", "args": json.dumps({"to": "operator", "text": text}), "ok": True,
                       "result": "delivered", "tags": []}], "diff": ""}
        b = {"id": "x1", "name": "wren", "created": "2026-11-09T09:00:00", "judge": "j", "code_rev": "abc",
             "arms": [{"label": "codex/gpt-6.1-sol", "model": "codex/gpt-6.1-sol", "env": {}},
                      {"label": "openrouter/deepseek/deepseek-v4.1-flash", "model": "m", "env": {}}],
             "states": [{"ts": "2026-11-08T10:00:00", "t0": "2026-11-08T09:55:00", "stratum": "check", "outcomes": {},
                         "arms": {"codex/gpt-6.1-sol": rec("a", "Sol <script>alert(1)</script>"),
                                  "openrouter/deepseek/deepseek-v4.1-flash": rec("b", "deepseek did it")}}]}
        (d / "wren-x1.json").write_text(json.dumps(b))
        for p in (mock.patch.object(bakeoff, "DIR", d), mock.patch.object(trial, "state_context", return_value="ctx")):
            p.start()
            self.addCleanup(p.stop)
        bakeoff._LOADED.clear()

    def test_list_bakeoff_and_judge_pages_render_blind_and_escaped(self):
        cookie = self.paired()
        self.assertIn(b"/bakeoffs/wren/x1", self.req("/bakeoffs", cookie=cookie).body)
        page = self.req("/bakeoffs/wren/x1", cookie=cookie)
        self.assertEqual(page.status, 200)
        self.assertIn(b"Judge pairs", page.body)
        judge = self.req("/bakeoffs/wren/x1/judge", cookie=cookie)   # nothing ticked: every model
        self.assertEqual(judge.status, 200)
        self.assertIn(b"Run A", judge.body)
        self.assertNotIn(b"<script>alert", judge.body)          # instance-written text never runs as markup
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", judge.body)   # ...and is still shown, escaped
        self.assertEqual(self.req("/bakeoffs/wren/x1/judge?vs=%C2%B2&vs=9999", cookie=cookie).status, 200)
        self.assertNotIn(b"deepseek", judge.body.lower())      # blind: the run's own words name no model
        self.assertIn(b"1 left", judge.body)
        self.assertEqual(self.req("/bakeoffs/wren/nope", cookie=cookie).status, 404)
        self.assertEqual(self.req("/bakeoffs/wren/x1", login=None).status, 403)   # not on the tailnet login list

    def test_a_verdict_posts_with_the_token_and_the_pair_leaves_the_queue(self):
        cookie = self.paired()
        post = lambda body, token=True: self.req(   # noqa: E731
            "/bakeoffs/wren/x1/judge", "POST", cookie=cookie, body=body.encode(),
            headers={"sec-fetch-site": "same-origin", "content-type": "application/x-www-form-urlencoded",
                     **({"x-csrf-token": self.csrf(cookie)} if token else {})})
        pair = ("codex/gpt-6.1-sol", "openrouter/deepseek/deepseek-v4.1-flash")
        runs = self.bakeoff.operator_case(self.bakeoff.load("wren", "x1"), 0, pair)["runs"]
        self.assertEqual(post(f"state=0&x=0&y=1&choice=A&runs={runs}", token=False).status, 403)
        self.assertEqual(post("state=0&x=0&y=1&choice=A").status, 409)            # no fingerprint of what was shown
        self.assertEqual(post("state=0&x=0&y=1&choice=A&runs=stale").status, 409)
        self.assertEqual(post(f"state=0&x=0&y=1&choice=maybe&runs={runs}").status, 409)
        self.assertEqual(post(f"state=0&x=1&y=1&choice=A&runs={runs}").status, 409)   # a model against itself
        self.assertEqual(post(f"state=0&x=1&y=0&choice=A&note=crisper&runs={runs}").status, 200)
        b = self.bakeoff.load("wren", "x1")
        v = self.bakeoff.operator_verdicts(b)[(0, pair)]
        self.assertEqual((v["choice"], v["note"], v["by"]), ("A", "crisper", ME))
        self.assertIn(b"Nothing left to judge", self.req("/bakeoffs/wren/x1/judge", cookie=cookie).body)
        ds_won = self.bakeoff._flipped(b, 0, pair)   # A was the second of the pair in name order: deepseek
        self.assertEqual(v["winner"], pair[1] if ds_won else pair[0])
        page = self.req("/bakeoffs/wren/x1", cookie=cookie).body.decode()
        self.assertIn('deepseek-v4.1-flash</td><td data-label="Your pairs">1</td><td data-label="Its W–L–T">'
                      + ("1–0–0" if ds_won else "0–1–0"), page)
        self.assertIn("<h2>Ranking of wren</h2>", page)             # the instance's ranking, updated by this verdict
        ds = re.search(r"deepseek-v4.1-flash</td><td data-label=\"Your Elo \[90%\]\">(\d+) \[", page)
        self.assertTrue(ds and (int(ds[1]) > 1000) == ds_won, page[-3000:])   # moved from 1000 by the verdict

    def test_judging_across_all_bake_offs_of_the_instance(self):
        cookie = self.paired()
        self.assertIn(b"Nothing left to judge", self.req("/bakeoffs/wren/judge-all", cookie=cookie).body)   # none started
        b = self.bakeoff.load("wren", "x1")
        pair = ("codex/gpt-6.1-sol", "openrouter/deepseek/deepseek-v4.1-flash")
        self.bakeoff.operator_path(b).parent.mkdir(parents=True, exist_ok=True)
        self.bakeoff.operator_path(b).write_text("")   # judging started on it
        page = self.req("/bakeoffs/wren/judge-all", cookie=cookie)
        self.assertEqual(page.status, 200)
        self.assertIn(b"Run A", page.body)
        self.assertIn(b'data-post="/bakeoffs/wren/x1/judge"', page.body)   # the verdict goes to the pair's bake-off
        self.assertIn(b"/bakeoffs/wren/judge-all", self.req("/bakeoffs", cookie=cookie).body)
        self.assertEqual(self.req("/bakeoffs/nobody/judge-all", cookie=cookie).status, 404)

