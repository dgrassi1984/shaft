"""Access consent against fake Google only; real paired cookies exercise the callback's browser binding."""
import base64
import copy
import hashlib
import io
import urllib.error
import urllib.parse
from contextlib import redirect_stderr
from unittest import mock

from supervisor import core, google, web, web_config, web_google
from supervisor import __main__ as cli
from supervisor.tests.test_google import FakeGoogle, ME
from supervisor.tests.test_web_actions import ActionsTest
from supervisor.tests.test_web_config import ACCESS


class GoogleAccessTest(ActionsTest):
    def setUp(self):
        super().setUp()
        self.fake = FakeGoogle()
        self.env = {"GMAIL_CORP_WEB_CLIENT_ID": "web-id", "GMAIL_CORP_WEB_CLIENT_SECRET": "secret-private"}
        self.logs = []
        for patch in (mock.patch.object(core, "SUP_DIR", self.sup),
                      mock.patch.object(core, "secrets_env", return_value=self.env),
                      mock.patch.object(google, "urlopen", self.fake),
                      mock.patch.object(web, "url", return_value="https://shaft.test"),
                      mock.patch.object(web, "log", side_effect=self.record_log),
                      mock.patch.object(web_google, "_pending", {})):
            patch.start()
            self.addCleanup(patch.stop)
        self.original = {"address": ME, "client": "corp", "scope": google.GMAIL + " " + google.CALENDAR,
                         "refresh_token": "original-token", "created": "2026-10-01"}
        google._save_account("work", self.original)

    def record_log(self, message):
        self.logs.append(message)
        core.log(message)

    def start(self):
        r = self.req("/google/login?alias=work&name=wren", cookie=self.cookie,
                     headers={"sec-fetch-site": "same-origin"})
        self.assertEqual(r.status, 303, r.body)
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["Location"]).query))

    def callback(self, state, cookie=None, **query):
        return self.req("/google/callback?" + urllib.parse.urlencode({"state": state, "code": "private-code", **query}),
                        cookie=cookie or self.cookie)

    def test_start_and_success_refresh_with_issuing_web_client(self):
        q = self.start()
        pending = web_google._pending[q["state"]]
        self.assertEqual(q["login_hint"], ME)
        self.assertEqual(q["scope"].split(), list(google.SCOPES))
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["access_type"], "offline")
        self.assertEqual(q["prompt"], "consent")
        self.assertEqual(q["include_granted_scopes"], "true")
        self.assertEqual(q["client_id"], "web-id")
        self.assertEqual(q["redirect_uri"], "https://shaft.test/google/callback")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["code_challenge"], base64.urlsafe_b64encode(
            hashlib.sha256(pending["verifier"].encode()).digest()).rstrip(b"=").decode())
        self.assertEqual(pending["device"], web.device_from_cookie(self.cookie)[0])
        self.assertAlmostEqual(pending["expires"] - web_google.time.time(), 600, delta=2)
        self.fake.address = ME.upper()
        r = self.callback(q["state"])
        self.assertEqual(r.status, 303)
        self.assertIn("Google+login+saved", r.headers["Location"])
        saved = google.account("work")
        self.assertIn(google.CALENDAR_EVENTS, google.scopes(saved))
        self.assertEqual(saved["client"], "corp:web")
        self.assertEqual(google.login_command("work", saved), f"shaft google login work {ME.upper()} --client corp")
        self.assertEqual(self.logs, [f"Google account work logged in as {ME.upper()} (client corp:web, granted "
                                     "gmail.readonly, calendar.readonly, calendar.events, drive.readonly) by the operator"])
        with mock.patch.object(web_config, "access", return_value=ACCESS):
            page = self.req(r.headers["Location"], cookie=self.cookie)
        self.assertEqual(page.status, 200)
        self.assertIn(b'role="status">Google login saved.', page.body)
        self.assertEqual(self.fake.requests[0][1]["code_verifier"], pending["verifier"])
        self.assertEqual(google.Account("work", self.env).token(), "at")
        refresh = self.fake.requests[-1][1]
        self.assertEqual(refresh["grant_type"], "refresh_token")
        self.assertEqual(refresh["client_id"], "web-id")
        self.assertEqual(refresh["client_secret"], "secret-private")
        self.assertEqual(self.callback(q["state"]).status, 403)
        for private in ("private-code", "secret-private", "rt-new", "original-token"):
            self.assertNotIn(private, (self.sup / "supervisor.log").read_text())

    def test_invalid_expired_and_other_session_save_nothing(self):
        self.assertEqual(self.callback("unknown").status, 403)
        q = self.start()
        web_google._pending[q["state"]]["expires"] = 0
        self.assertEqual(self.callback(q["state"]).status, 403)
        q = self.start()
        self.assertEqual(self.callback(q["state"], cookie=self.paired()).status, 403)
        self.assertEqual(self.callback(q["state"]).status, 403)
        self.assertEqual(google.account("work"), self.original)
        self.assertEqual(self.fake.requests, [])

    def test_different_address_and_remote_errors_save_nothing_and_log_no_payload(self):
        self.fake.address = "someone-else@example.com"
        r = self.callback(self.start()["state"])
        self.assertIn("Nothing+saved", r.headers["Location"])
        self.assertEqual(google.account("work"), self.original)
        self.assertIn("Different+account", r.headers["Location"])     # says why, on the page and in the log
        q = self.start()
        with mock.patch.object(google, "urlopen", side_effect=urllib.error.HTTPError(
                google.TOKEN_URL, 400, "private-code secret-private", {}, io.BytesIO(b'rt-new'))):
            r = self.callback(q["state"])
        self.assertEqual(google.account("work"), self.original)
        logged = "\n".join(self.logs)
        self.assertIn("Google login for work failed", logged)          # never silent (review: undiagnosable)
        self.assertIn("HTTP 400", logged)
        for secret in ("private-code", "secret-private", "rt-new"):
            self.assertNotIn(secret, logged)
            self.assertNotIn(secret, r.headers["Location"])

    def test_google_s_error_code_is_shown_never_its_text(self):
        # redirect_uri_mismatch is the likeliest first failure (the Web client's URI not registered): name it.
        body = b'{"error": "redirect_uri_mismatch", "error_description": "Bad Request private-detail"}'
        with mock.patch.object(google, "urlopen", side_effect=urllib.error.HTTPError(
                google.TOKEN_URL, 400, "Bad Request", {}, io.BytesIO(body))):
            r = self.callback(self.start()["state"])
        self.assertIn("redirect_uri_mismatch", r.headers["Location"])
        self.assertIn("redirect_uri_mismatch", "\n".join(self.logs))
        self.assertNotIn("private-detail", "\n".join(self.logs) + r.headers["Location"])
        r = self.callback(self.start()["state"], error="access_denied")  # consent refused on Google's page
        self.assertIn("access_denied", r.headers["Location"])

    def test_scope_display_setup_and_real_login_command(self):
        a = copy.deepcopy(ACCESS)
        a["google_accounts"] = {"work": self.original}
        self.env.clear()
        body = str(web_config.access_page("wren", a))
        for want in ("Google accounts", "calendar.events", "https://console.cloud.google.com/auth/scopes",
                     "shaft google login work operator@work.example --client corp", "2026-10-01", "Web application",
                     "https://shaft.test/google/callback", "GMAIL_CORP_WEB_CLIENT_ID", "GMAIL_CORP_WEB_CLIENT_SECRET",
                     "Allow writing", 'name="writable"', "/calendar/proposals"):
            self.assertIn(want, body)
        self.assertNotIn("original-token", body)
        self.assertNotIn(">Log in again</button>", body)
        r = self.req("/google/login?alias=work&name=wren", cookie=self.cookie,
                     headers={"sec-fetch-site": "same-origin"})
        self.assertEqual(r.status, 403)
        self.assertEqual(web_google._pending, {})
        self.env.update(GMAIL_CORP_WEB_CLIENT_ID="id", GMAIL_CORP_WEB_CLIENT_SECRET="secret")
        self.assertIn(">Log in again</button>", str(web_config.access_page("wren", a)))
        a["calendar"][0]["write"] = True
        self.assertIn(">Read only</button>", str(web_config.access_page("wren", a)))

    def test_toggle_action_and_writable_form_use_cli_and_calendar_id(self):
        for op in ("grant-write", "revoke-write"):
            status, _ = self.act("calendar", name="wren", op=op, alias="work", what="id%40calendar")
            self.assertEqual(status, 200)
        with mock.patch.object(core, "calendar_grants", return_value=[
                {"account": "work", "calendar": "id@calendar", "name": 'A, "title"'}]):
            self.act("calendar", name="wren", op="grant", alias="work", what="A%2C%20%22title%22", writable="1")
        self.assertEqual([r[1] for r in self.ran], [
            ["calendar", "wren", "grant-write", "work", "id@calendar"],
            ["calendar", "wren", "revoke-write", "work", "id@calendar"],
            ["calendar", "wren", "grant", "work", 'A, "title"'],
            ["calendar", "wren", "grant-write", "work", "id@calendar"]])

    def test_cli_refuses_missing_write_scope_before_resolving_or_saving(self):
        with mock.patch.object(cli, "instance_root", return_value=self.sup / "wren"), \
                mock.patch.object(core, "calendar_grants", return_value=[]), \
                mock.patch.object(core, "set_calendar_grants") as save, \
                mock.patch("supervisor.calendar.resolve") as resolve, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.cmd_calendar("wren", "grant-write", "work", "id@calendar"), 2)
            def run_cli(args, timeout=60, who="web"):
                with redirect_stderr(io.StringIO()) as output:
                    code = cli.cmd_calendar(*args[1:])
                return code == 0, output.getvalue()
            with mock.patch("supervisor.actions.run_now", side_effect=run_cli):
                status, message = self.act("calendar", name="wren", op="grant-write", alias="work", what="id")
            self.assertEqual(status, 409)
            self.assertIn("calendar.events", message)
        save.assert_not_called()
        resolve.assert_not_called()
        self.assertIn(google.missing_scope_message("work", self.original, google.CALENDAR_EVENTS), err.getvalue())

    def test_start_and_callback_keep_front_door_gates(self):
        for path in ("/google/login?alias=work&name=wren", "/google/callback?state=unknown&code=private-code"):
            self.assertEqual(self.req(path, cookie=self.cookie, login=None).status, 403)
            self.assertEqual(self.req(path).status, 403)
        self.assertEqual(self.req("/google/login?alias=work&name=wren", cookie=self.cookie,
                                  headers={"sec-fetch-site": "cross-site"}).status, 403)
