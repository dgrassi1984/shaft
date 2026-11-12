"""The operator's Google accounts: one `shaft google login` asks for gmail.readonly and
calendar.readonly + calendar.events together and records what Google granted; account files live in .supervisor/google/, moved there
from the old .supervisor/mail/ by `shaft google migrate`; `shaft mail login` is gone."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from supervisor import core, google

ME = "operator@work.example"


class FakeGoogle:
    """Google's token endpoint, Gmail's profile and the primary calendar, as login uses them. `granted` is what the
    operator left ticked on the consent page."""

    def __init__(self, address: str = ME, granted: str = " ".join(google.SCOPES)):
        self.address, self.granted = address, granted
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, req: urllib.request.Request, timeout: float = 0):
        if req.full_url == google.TOKEN_URL:
            form = dict(urllib.parse.parse_qsl(req.data.decode()))
            self.requests.append(("token", form))
            return io.BytesIO(json.dumps({"access_token": "at", "refresh_token": "rt-new",
                                          "scope": self.granted}).encode())
        self.requests.append((req.full_url, {}))
        if req.full_url == google.GMAIL_PROFILE:
            assert google.GMAIL in self.granted.split(), "Gmail's profile needs gmail.readonly"
            return io.BytesIO(json.dumps({"emailAddress": self.address}).encode())
        if req.full_url == google.CALENDAR_PRIMARY:
            assert google.CALENDAR in self.granted.split(), "the primary calendar needs calendar.readonly"
            return io.BytesIO(json.dumps({"id": self.address, "summary": self.address}).encode())
        raise AssertionError(f"unexpected call {req.full_url}")


class GoogleCase(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-google-")).resolve() / ".supervisor"
        self.addCleanup(shutil.rmtree, self.sup.parent, True)
        self.fake = FakeGoogle()
        for p in (mock.patch.object(core, "SUP_DIR", self.sup), mock.patch.object(google, "urlopen", self.fake)):
            p.start()
            self.addCleanup(p.stop)
        self.secrets = {"GMAIL_CORP_CLIENT_ID": "cid", "GMAIL_CORP_CLIENT_SECRET": "csecret",
                        "GMAIL_WORK_CLIENT_ID": "wid", "GMAIL_WORK_CLIENT_SECRET": "wsecret"}
        self.asked: list[dict] = []


class LoginTest(GoogleCase):
    def browser(self, code: str = "the-code", state: str | None = None):
        def open_url(url):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            self.asked.append(q)
            self.assertEqual(q["code_challenge_method"], "S256")
            back = q["redirect_uri"] + "/?" + urllib.parse.urlencode({"code": code, "state": state or q["state"]})
            urllib.request.urlopen(back, timeout=5).read()   # the real loopback, not the fake Google
        return open_url

    def login(self, address: str, client: str = "", **kw):
        with redirect_stdout(io.StringIO()):
            return google.login("work", address, self.secrets, client=client, open_url=self.browser(**kw), timeout_s=5)

    def test_login_asks_all_scopes_and_records_what_was_granted(self):
        saved = self.login(ME)
        self.assertEqual(self.asked[0]["scope"].split(), [google.GMAIL, google.GMAIL_COMPOSE, google.GMAIL_MODIFY, google.CALENDAR, google.CALENDAR_EVENTS, google.DRIVE_READONLY,
                                                       google.CHAT_SPACES, google.CHAT_MESSAGES, google.CHAT_MEMBERS])
        self.assertEqual(self.asked[0]["include_granted_scopes"], "true")
        self.assertEqual(google.scopes(saved), {google.GMAIL, google.GMAIL_COMPOSE, google.GMAIL_MODIFY, google.CALENDAR, google.CALENDAR_EVENTS, google.DRIVE_READONLY,
                                                google.CHAT_SPACES, google.CHAT_MESSAGES, google.CHAT_MEMBERS})
        on_disk = json.loads((self.sup / "google" / "work.json").read_text())
        self.assertEqual(google.scopes(on_disk), {google.GMAIL, google.GMAIL_COMPOSE, google.GMAIL_MODIFY, google.CALENDAR, google.CALENDAR_EVENTS, google.DRIVE_READONLY,
                                                google.CHAT_SPACES, google.CHAT_MESSAGES, google.CHAT_MEMBERS})
        self.assertEqual(google.scope_text(on_disk), "gmail.readonly, gmail.compose, gmail.modify, calendar.readonly, calendar.events, drive.readonly, "
                                                    "chat.spaces.readonly, chat.messages.readonly, chat.memberships.readonly")

    def test_partial_grant_recorded_as_such(self):
        # The operator unticked Calendar on the consent page: mail keeps working, a calendar call names the login.
        self.fake.granted = google.GMAIL
        saved = self.login(ME)
        self.assertEqual(google.scopes(saved), {google.GMAIL})
        acct = google.Account("work", self.secrets)
        self.assertIs(acct.require(google.GMAIL), acct)
        with self.assertRaisesRegex(google.AccountError, r"was not granted calendar.readonly \(its login granted: "
                                                         r"gmail.readonly\); the operator runs `shaft google login work "
                                                         r"operator@work.example` again and ticks it"):
            acct.require(google.CALENDAR)

    def test_calendar_only_grant_still_checks_the_account(self):
        self.fake.granted = google.CALENDAR
        saved = self.login(ME)
        self.assertEqual(google.scopes(saved), {google.CALENDAR})
        self.assertIn(google.CALENDAR_PRIMARY, [u for u, _ in self.fake.requests])
        self.fake.address = "someone@example.com"
        with self.assertRaisesRegex(google.AccountError, "signed in as someone@example.com, not operator@work.example"):
            self.login(ME)

    def test_no_scope_granted_saves_nothing(self):
        (self.sup / "google").mkdir(parents=True)
        self.fake.granted = "openid"
        with self.assertRaisesRegex(google.AccountError, "granted no Gmail, Calendar or Drive scope: "
                                                         "nothing saved"):
            self.login(ME)
        self.assertFalse((self.sup / "google" / "work.json").exists())

    def test_login_records_its_client(self):
        self.assertEqual(self.login(ME)["client"], "work")           # default: the alias
        self.assertEqual(self.login(ME, client="corp")["client"], "corp")
        codes = [f["client_id"] for p, f in self.fake.requests if p == "token"]
        self.assertEqual(codes, ["wid", "cid"])
        self.assertEqual(json.loads((self.sup / "google" / "work.json").read_text())["client"], "corp")
        with self.assertRaisesRegex(google.AccountError, "Google client 'nope' needs GMAIL_NOPE_CLIENT_ID and "
                                                         "GMAIL_NOPE_CLIENT_SECRET"):
            self.login(ME, client="nope")

    def test_login_keeps_the_token_only_for_the_right_account(self):
        saved = self.login("Operator@Work.Example")
        self.assertEqual(saved["address"], ME)
        path = self.sup / "google" / "work.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["refresh_token"], "rt-new")
        path.unlink()
        with self.assertRaisesRegex(google.AccountError, "signed in as operator@work.example, not operator@example.com: "
                                                         "nothing saved"):
            self.login("operator@example.com")
        self.assertFalse(path.exists())

    def test_a_forged_answer_is_refused(self):
        with self.assertRaisesRegex(google.AccountError, "state"):
            self.login(ME, state="forged")
        self.assertFalse((self.sup / "google" / "work.json").exists())


class MigrateTest(GoogleCase):
    def old(self, alias: str, data: dict) -> Path:
        d = self.sup / "mail"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{alias}.json"
        path.write_text(json.dumps(data))
        os.chmod(path, 0o600)
        return path

    def test_migrate_moves_account_files_and_keeps_their_mode(self):
        work = {"address": ME, "client": "work", "refresh_token": "rt", "scope": google.GMAIL}
        home = {"address": "operator@example.com", "client": "personal", "refresh_token": "rt2", "scope": google.GMAIL}
        self.old("work", work)
        self.old("home", home)
        # Not read from the old folder (one location): the error names the migration.
        with self.assertRaisesRegex(google.AccountError, r"'work' is not read: its file is still in .*/mail "
                                                         r"\(the old mail folder\); the operator runs `shaft google "
                                                         r"migrate`$"):
            google.Account("work", self.secrets).require(google.GMAIL)
        self.assertEqual(google.unmigrated(), ["home", "work"])
        lines = google.migrate()
        self.assertEqual(google.accounts(), {"work": work, "home": home})
        for alias in ("work", "home"):
            self.assertEqual((self.sup / "google" / f"{alias}.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.sup / "google").stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.sup / "mail").exists())
        self.assertIn("removed the empty", lines[-1])
        self.assertEqual(google.unmigrated(), [])
        self.assertEqual(google.migrate(), [f"nothing to migrate: {self.sup / 'mail'} does not exist"])   # idempotent
        self.assertEqual(google.Account("work", self.secrets).require(google.GMAIL).alias, "work")

    def test_migrate_refuses_a_clash(self):
        self.old("work", {"address": ME, "client": "work", "refresh_token": "old", "scope": google.GMAIL})
        google._save_account("work", {"address": ME, "client": "work", "refresh_token": "new",
                                       "scope": f"{google.GMAIL} {google.CALENDAR}"})
        with self.assertRaisesRegex(google.AccountError, "work exist in both"):
            google.migrate()
        self.assertEqual(json.loads((self.sup / "mail" / "work.json").read_text())["refresh_token"], "old")
        self.assertEqual(google.account("work")["refresh_token"], "new")


class CliTest(GoogleCase):
    def cli(self, *argv) -> tuple[int, str]:
        from supervisor import __main__ as cli
        out = io.StringIO()
        with mock.patch.object(core, "secrets_env", lambda: self.secrets), mock.patch.object(cli, "log", lambda m: None), \
                redirect_stdout(out), redirect_stderr(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_mail_login_is_gone(self):
        code, out = self.cli("mail", "login", "work", ME)
        self.assertEqual(code, 2)
        self.assertIn("shaft mail login is gone: shaft google login <alias> <address> [--client <name>]", out)
        self.assertFalse((self.sup / "google").exists())

    def test_google_lists_accounts_with_scopes_and_unmigrated_files(self):
        google._save_account("work", {"address": ME, "client": "work", "refresh_token": "rt", "scope": google.GMAIL,
                                       "created": "2026-10-15T10:00:00"})
        (self.sup / "mail").mkdir()
        (self.sup / "mail" / "home.json").write_text("{}")
        code, out = self.cli("google")
        self.assertEqual(code, 0)
        self.assertIn("work       operator@work.example · client work · granted gmail.readonly · since 2026-10-15", out)
        self.assertIn("NOT READ: home still in", out)
        self.assertIn("(shaft google migrate)", out)
        code, out = self.cli("google", "migrate")
        self.assertEqual(code, 0, out)
        self.assertIn("home: moved to", out)


DESKTOP_JSON = json.dumps({"installed": {"client_id": "123-abc.apps.googleusercontent.com", "project_id": "shaft-1",
                                          "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                          "token_uri": "https://oauth2.googleapis.com/token",
                                          "client_secret": "GOCSPX-desktop-secret",
                                          "redirect_uris": ["http://localhost"]}})
WEB_JSON = json.dumps({"web": {"client_id": "456-web.apps.googleusercontent.com", "client_secret": "GOCSPX-web",
                               "redirect_uris": ["https://example.test/google/callback"]}})


class ClientJsonTest(GoogleCase):
    """`shaft google client NAME --json`: the Desktop app client from the JSON Google Cloud downloads, on stdin."""

    def setUp(self):
        super().setUp()
        self.project = self.sup.parent
        p = mock.patch.object(core, "PROJECT", self.project)
        p.start()
        self.addCleanup(p.stop)
        self.env = self.project / ".env"

    def cli(self, *argv, stdin: str = "") -> tuple[int, list[dict], str]:
        from supervisor import __main__ as cli
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch.object(cli, "log", lambda m: None), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, [json.loads(line) for line in out.getvalue().splitlines()], err.getvalue()

    def test_client_from_desktop_json_writes_two_keys(self):
        self.env.write_text("OPENROUTER_API_KEY=k\n")
        code, rows, err = self.cli("google", "client", "google", "--json", stdin=DESKTOP_JSON)
        self.assertEqual(code, 0, err)
        self.assertEqual(rows, [{"ok": True, "client": "google", "login_again": []}])
        self.assertEqual(self.env.read_text(), "OPENROUTER_API_KEY=k\n"
                                               "GMAIL_GOOGLE_CLIENT_ID=123-abc.apps.googleusercontent.com\n"
                                               "GMAIL_GOOGLE_CLIENT_SECRET=GOCSPX-desktop-secret\n")
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        # the secret is never printed, and the login finds the client by its name
        self.assertNotIn("GOCSPX", json.dumps(rows) + err)
        self.assertEqual(google._client("google", core.secrets_env()),
                         ("123-abc.apps.googleusercontent.com", "GOCSPX-desktop-secret"))

    def test_a_web_client_json_is_refused_with_the_fix(self):
        code, rows, _ = self.cli("google", "client", "google", "--json", stdin=WEB_JSON)
        self.assertEqual(code, 1)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["ok"])
        self.assertIn("This is a Web application client: create a Desktop app client instead", rows[0]["error"])
        self.assertFalse(self.env.exists())                       # nothing written

    def test_what_is_not_a_desktop_client_is_refused_and_writes_nothing(self):
        bad = {"not json": "{oops",
               "a list": "[1, 2]",
               "no installed": json.dumps({"other": {}}),
               "no secret": json.dumps({"installed": {"client_id": "x.apps.googleusercontent.com"}}),
               "empty id": json.dumps({"installed": {"client_id": " ", "client_secret": "s"}}),
               "two lines": json.dumps({"installed": {"client_id": "a\nB=1", "client_secret": "s"}}),
               "too large": json.dumps({"installed": {"client_id": "x", "client_secret": "y"}, "pad": "x" * 70000})}
        for what, text in bad.items():
            with self.subTest(what):
                code, rows, _ = self.cli("google", "client", "google", "--json", stdin=text)
                self.assertEqual(code, 1)
                self.assertEqual([r["ok"] for r in rows], [False])
                self.assertTrue(rows[0]["error"].startswith("not saved: "), rows[0]["error"])
        self.assertFalse(self.env.exists())
        code, rows, _ = self.cli("google", "client", "Bad_Name", "--json", stdin=DESKTOP_JSON)
        self.assertEqual((code, rows[0]["ok"]), (1, False))
        self.assertIn("not an OAuth client name", rows[0]["error"])
        code, rows, _ = self.cli("google", "client", "corp-web", "--json", stdin=DESKTOP_JSON)
        self.assertIn("Web application client", rows[0]["error"])       # GMAIL_CORP_WEB_* belongs to corp:web
        code, rows, _ = self.cli("google", "client", "--json")
        self.assertEqual(code, 2)
        self.assertIn("usage", rows[0]["error"])
        self.assertFalse(self.env.exists())

    def test_a_new_id_for_a_client_in_use_names_the_logins_to_redo(self):
        self.env.write_text("GMAIL_GOOGLE_CLIENT_ID=old.apps.googleusercontent.com\nGMAIL_GOOGLE_CLIENT_SECRET=old\n")
        google._save_account("google", {"address": ME, "client": "google", "refresh_token": "rt", "scope": google.GMAIL})
        google._save_account("work", {"address": ME, "client": "work", "refresh_token": "rt", "scope": google.GMAIL})
        code, rows, _ = self.cli("google", "client", "google", "--json", stdin=DESKTOP_JSON)
        self.assertEqual((code, rows), (0, [{"ok": True, "client": "google", "login_again": ["google"]}]))
        code, rows, _ = self.cli("google", "client", "google", "--json", stdin=DESKTOP_JSON)   # same ID again
        self.assertEqual(rows[0]["login_again"], [])

    def test_only_client_and_login_take_json(self):
        code, rows, err = self.cli("google", "migrate", "--json")
        self.assertEqual(code, 2)
        self.assertIn("has no --json", err)


class LoginJsonTest(GoogleCase):
    """`shaft google login ALIAS ADDRESS --client NAME --json`: the consent URL as a JSON line before the wait, opened by
    the app (never a browser from here), then the outcome."""

    def cli(self, *argv, stdout=None) -> tuple[int, list[dict], str]:
        from supervisor import __main__ as cli
        out, err = stdout or io.StringIO(), io.StringIO()
        with mock.patch.object(core, "secrets_env", lambda: self.secrets), mock.patch.object(cli, "log", lambda m: None), \
                mock.patch("webbrowser.open", side_effect=AssertionError("--json never opens a browser")), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, [json.loads(line) for line in out.getvalue().splitlines()], err.getvalue()

    def test_login_json_emits_the_url_first(self):
        # A fake consent, no network: what stdout holds when the wait would start is the URL line, already flushed.
        seen = {}

        def fake_consent(address, client, wanted, secrets_env, open_url=None, timeout_s=300, announce=True, **kw):
            self.assertFalse(announce)
            open_url("https://accounts.google.com/o/oauth2/v2/auth?client_id=wid&x=1")
            seen["before_wait"] = sys.stdout.getvalue()
            return {"address": address, "client": client, "refresh_token": "rt", "scope": f"{google.GMAIL} {google.CALENDAR}",
                    "created": "2026-11-12T10:00:00"}
        with mock.patch.object(google, "consent", side_effect=fake_consent):
            code, rows, err = self.cli("google", "login", "google", ME, "--client", "work", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(seen["before_wait"]), {"url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=wid&x=1"})
        self.assertEqual(rows[0], {"url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=wid&x=1"})
        self.assertEqual(rows[1], {"ok": True, "alias": "google", "address": ME, "client": "work",
                                   "granted": ["gmail.readonly", "calendar.readonly"],
                                   "missing": ["gmail.compose", "gmail.modify", "calendar.events", "drive.readonly",
                                               "chat.spaces.readonly", "chat.messages.readonly",
                                               "chat.memberships.readonly"]})
        self.assertEqual(len(rows), 2)
        self.assertEqual(google.account("google")["client"], "work")

    def test_login_json_through_the_real_loopback(self):
        # The real consent and its loopback listener, Google faked (urlopen): the app "opens" the URL line it reads.
        test = self

        class App(io.StringIO):
            def write(self, text):
                for line in filter(None, text.splitlines()):    # print writes the line, then its newline
                    row = json.loads(line)
                    if "url" in row:
                        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(row["url"]).query))
                        test.assertEqual(q["client_id"], "wid")
                        back = q["redirect_uri"] + "/?" + urllib.parse.urlencode({"code": "c", "state": q["state"]})
                        threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=5).read(),
                                         daemon=True).start()
                return super().write(text)
        code, rows, err = self.cli("google", "login", "work", ME, "--json", stdout=App())
        self.assertEqual(code, 0, err)
        self.assertEqual([list(r) for r in rows], [["url"], ["ok", "alias", "address", "client", "granted", "missing"]])
        self.assertEqual(rows[1]["missing"], [])
        self.assertEqual(google.account("work")["refresh_token"], "rt-new")

    def test_login_json_failures_say_the_real_reason(self):
        cases = {
            "timeout": google.AccountError("no answer from the browser in 300 s: nothing saved"),
            "network": OSError("[Errno 8] nodename nor servname provided"),
        }
        for what, error in cases.items():
            with self.subTest(what), mock.patch.object(google, "consent", side_effect=error):
                code, rows, _ = self.cli("google", "login", "google", ME, "--client", "work", "--json")
                self.assertEqual(code, 1)
                self.assertEqual(len(rows), 1)
                self.assertFalse(rows[0]["ok"])
                self.assertIn(str(error), rows[0]["error"])
        self.assertIsNone(google.account("google"))
        # a client with no keys in .env: the consent never starts, and the error names the two keys
        code, rows, _ = self.cli("google", "login", "google", ME, "--client", "nope", "--json")
        self.assertEqual(code, 1)
        self.assertIn("GMAIL_NOPE_CLIENT_ID and GMAIL_NOPE_CLIENT_SECRET", rows[0]["error"])
        code, rows, _ = self.cli("google", "login", "google", "--json")
        self.assertEqual(code, 2)
        self.assertIn("usage", rows[0]["error"])

    def test_an_api_not_enabled_is_the_error_not_a_traceback(self):
        # Google's own words when the project has not enabled the API the address check reads.
        def fake(req, timeout=0):
            if req.full_url == google.TOKEN_URL:
                return io.BytesIO(json.dumps({"access_token": "at", "refresh_token": "rt",
                                              "scope": google.GMAIL}).encode())
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(json.dumps(
                {"error": {"message": "Gmail API has not been used in project 123 before or it is disabled."}}).encode()))
        browser = LoginTest.browser(self)
        with mock.patch.object(google, "urlopen", fake), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(google.AccountError, "HTTP 403: Gmail API has not been used in project 123"):
                google.login("work", ME, self.secrets, open_url=browser, timeout_s=5)
        self.assertIsNone(google.account("work"))


    def test_an_api_not_enabled_gives_way_to_the_next_granted_source(self):
        # Gmail granted but its API off in the project (a setup for Calendar alone): the primary calendar says who it is.
        def fake(req, timeout=0):
            if req.full_url == google.TOKEN_URL:
                return io.BytesIO(json.dumps({"access_token": "at", "refresh_token": "rt",
                                              "scope": f"{google.GMAIL} {google.CALENDAR}"}).encode())
            if req.full_url == google.GMAIL_PROFILE:
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"error": {"message": "off"}}'))
            return io.BytesIO(json.dumps({"id": ME}).encode())
        browser = LoginTest.browser(self)
        with mock.patch.object(google, "urlopen", fake), redirect_stdout(io.StringIO()):
            saved = google.login("work", ME, self.secrets, open_url=browser, timeout_s=5)
        self.assertEqual(saved["address"], ME)


class AppScopesTest(unittest.TestCase):
    def test_the_app_lists_every_scope_the_login_asks_for(self):
        # The Mac app's Google setup tells the operator which scopes to add on Data Access (GoogleSetup.swift); a scope
        # added to SCOPES without it there would be missing from every new project, and its grant refused.
        app = (Path(__file__).resolve().parents[2] / "apps/shaft/macOS/Setup/GoogleSetup.swift").read_text()
        for scope in google.SCOPES:
            self.assertIn(f'"{scope}"', app, scope)


if __name__ == "__main__":
    unittest.main()
