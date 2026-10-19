"""The operator's Google accounts (PLAN-calendar.md M1): one `shaft google login` asks for gmail.readonly and
calendar.readonly together and records what Google granted; account files live in .supervisor/google/, moved there
from PLAN-mail's .supervisor/mail/ by `shaft google migrate`; `shaft mail login` is gone."""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
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

    def __init__(self, address: str = ME, granted: str = f"{google.GMAIL} {google.CALENDAR}"):
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
        self.secrets = {"GMAIL_example-co_CLIENT_ID": "cid", "GMAIL_example-co_CLIENT_SECRET": "csecret",
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

    def test_login_asks_both_scopes_and_records_what_was_granted(self):
        saved = self.login(ME)
        self.assertEqual(self.asked[0]["scope"].split(), [google.GMAIL, google.CALENDAR])
        self.assertEqual(self.asked[0]["include_granted_scopes"], "true")
        self.assertEqual(google.scopes(saved), {google.GMAIL, google.CALENDAR})
        on_disk = json.loads((self.sup / "google" / "work.json").read_text())
        self.assertEqual(google.scopes(on_disk), {google.GMAIL, google.CALENDAR})
        self.assertEqual(google.scope_text(on_disk), "gmail.readonly, calendar.readonly")

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
        self.fake.address = "someone@else.com"
        with self.assertRaisesRegex(google.AccountError, "signed in as someone@else.com, not operator@work.example"):
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
        self.assertEqual(self.login(ME, client="example-co")["client"], "example-co")
        codes = [f["client_id"] for p, f in self.fake.requests if p == "token"]
        self.assertEqual(codes, ["wid", "cid"])
        self.assertEqual(json.loads((self.sup / "google" / "work.json").read_text())["client"], "example-co")
        with self.assertRaisesRegex(google.AccountError, "Google client 'nope' needs GMAIL_NOPE_CLIENT_ID and "
                                                         "GMAIL_NOPE_CLIENT_SECRET"):
            self.login(ME, client="nope")

    def test_login_keeps_the_token_only_for_the_right_account(self):
        saved = self.login("Daniele.Grassi@example-co.ai")
        self.assertEqual(saved["address"], ME)
        path = self.sup / "google" / "work.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["refresh_token"], "rt-new")
        path.unlink()
        with self.assertRaisesRegex(google.AccountError, "signed in as operator@work.example, not d@gmail.com: "
                                                         "nothing saved"):
            self.login("d@gmail.com")
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
        example-co = {"address": ME, "client": "example-co", "refresh_token": "rt", "scope": google.GMAIL}
        home = {"address": "d@gmail.com", "client": "personal", "refresh_token": "rt2", "scope": google.GMAIL}
        self.old("example-co", example-co)
        self.old("home", home)
        # Not read from the old folder (one location): the error names the migration.
        with self.assertRaisesRegex(google.AccountError, r"'example-co' is not read: its file is still in .*/mail "
                                                         r"\(PLAN-mail's folder\); the operator runs `shaft google "
                                                         r"migrate`$"):
            google.Account("example-co", self.secrets).require(google.GMAIL)
        self.assertEqual(google.unmigrated(), ["example-co", "home"])
        lines = google.migrate()
        self.assertEqual(google.accounts(), {"example-co": example-co, "home": home})
        for alias in ("example-co", "home"):
            self.assertEqual((self.sup / "google" / f"{alias}.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.sup / "google").stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.sup / "mail").exists())
        self.assertIn("removed the empty", lines[-1])
        self.assertEqual(google.unmigrated(), [])
        self.assertEqual(google.migrate(), [f"nothing to migrate: {self.sup / 'mail'} does not exist"])   # idempotent
        self.assertEqual(google.Account("example-co", self.secrets).require(google.GMAIL).alias, "example-co")

    def test_migrate_refuses_a_clash(self):
        self.old("example-co", {"address": ME, "client": "example-co", "refresh_token": "old", "scope": google.GMAIL})
        google._save_account("example-co", {"address": ME, "client": "example-co", "refresh_token": "new",
                                       "scope": f"{google.GMAIL} {google.CALENDAR}"})
        with self.assertRaisesRegex(google.AccountError, "example-co exist in both"):
            google.migrate()
        self.assertEqual(json.loads((self.sup / "mail" / "example-co.json").read_text())["refresh_token"], "old")
        self.assertEqual(google.account("example-co")["refresh_token"], "new")


class CliTest(GoogleCase):
    def cli(self, *argv) -> tuple[int, str]:
        from supervisor import __main__ as cli
        out = io.StringIO()
        with mock.patch.object(core, "secrets_env", lambda: self.secrets), mock.patch.object(cli, "log", lambda m: None), \
                redirect_stdout(out), redirect_stderr(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_mail_login_is_gone(self):
        code, out = self.cli("mail", "login", "example-co", ME)
        self.assertEqual(code, 2)
        self.assertIn("shaft mail login is gone: shaft google login <alias> <address> [--client <name>]", out)
        self.assertFalse((self.sup / "google").exists())

    def test_google_lists_accounts_with_scopes_and_unmigrated_files(self):
        google._save_account("example-co", {"address": ME, "client": "example-co", "refresh_token": "rt", "scope": google.GMAIL,
                                       "created": "2026-10-15T10:00:00"})
        (self.sup / "mail").mkdir()
        (self.sup / "mail" / "home.json").write_text("{}")
        code, out = self.cli("google")
        self.assertEqual(code, 0)
        self.assertIn("example-co      operator@work.example · client example-co · granted gmail.readonly · since 2026-10-15", out)
        self.assertIn("NOT READ: home still in", out)
        self.assertIn("(shaft google migrate)", out)
        code, out = self.cli("google", "migrate")
        self.assertEqual(code, 0, out)
        self.assertIn("home: moved to", out)


if __name__ == "__main__":
    unittest.main()
