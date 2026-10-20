"""An instance's own Google account as a key (docs/plans/PLAN-own-mail.md: Thelos's Gmail, gmail.modify). The operator signs it
in once (`shaft secret <name> google`); the broker makes an access token from its refresh token per run and adds it
to http_request like any key, only to its hosts. It is never one of the operator's accounts, never minted in a
battery exam, never sent from a trial replay; and a battery exam never writes with any of an instance's keys."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supervisor import broker as broker_mod
from supervisor import core, google
from supervisor.broker import Broker, ReplayBroker

THELOS = "assistant@example.com"
OPERATOR = "operator@example.com"
SECRETS = {"GMAIL_PERSONAL_CLIENT_ID": "pid", "GMAIL_PERSONAL_CLIENT_SECRET": "psecret"}


class FakeTokens:
    """Google's token endpoint and Gmail's profile, behind google.urlopen. Each refresh gives a new access token."""

    def __init__(self, address: str = THELOS, granted: str = google.GMAIL_MODIFY):
        self.address, self.granted, self.refused = address, granted, False
        self.minted: list[str] = []

    def __call__(self, req: urllib.request.Request, timeout: float = 0):
        if req.full_url == google.TOKEN_URL:
            form = dict(urllib.parse.parse_qsl(req.data.decode()))
            if self.refused:
                raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                             io.BytesIO(b'{"error": "invalid_grant", "error_description": "revoked"}'))
            token = f"ya29.access-{len(self.minted) + 1}"
            self.minted.append(token)
            body = {"access_token": token, "expires_in": 3599}
            if form.get("grant_type") == "authorization_code":
                body.update(refresh_token="1//refresh-thelos", scope=self.granted)
            return io.BytesIO(json.dumps(body).encode())
        if req.full_url == google.GMAIL_PROFILE:
            return io.BytesIO(json.dumps({"emailAddress": self.address}).encode())
        raise AssertionError(f"unexpected call to Google {req.full_url}")


class FakeGmail:
    """The Gmail API on localhost (check_public is patched off): records what reached it, answers 401 on demand."""

    def __init__(self):
        self.seen: list[dict] = []
        self.unauthorized = False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                fake.seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                                  "body": self.rfile.read(n).decode() if n else ""})
                code = 401 if fake.unauthorized else 200
                body = json.dumps({"you_sent": self.headers.get("Authorization"), "messages": [{"id": "m1"}]}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/gmail/v1/users/me"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def key(address: str = THELOS, hosts=("127.0.0.1",)) -> dict:
    return {"kind": "google", "client": "personal", "address": address, "refresh_token": "1//refresh-thelos",
            "scope": google.GMAIL_MODIFY, "hosts": list(hosts)}


class OwnMailCase(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-own-mail-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        self.root = self.project / "instances" / "thelos"
        self.root.mkdir(parents=True)
        self.tokens, self.gmail = FakeTokens(), FakeGmail()
        self.addCleanup(self.gmail.close)
        for p in (mock.patch.object(core, "SUP_DIR", self.project / ".supervisor"),
                  mock.patch.object(google, "urlopen", self.tokens),
                  mock.patch.object(broker_mod, "check_public", lambda url: None)):   # the fake Gmail is local
            p.start()
            self.addCleanup(p.stop)
        # The operator's own Google account, as `shaft google login home operator@example.com` keeps it.
        google._save_account("home", {"address": OPERATOR, "client": "personal", "refresh_token": "1//operator",
                                      "scope": f"{google.GMAIL} {google.CALENDAR}"})

    def broker(self, **kw) -> Broker:
        return Broker("thelos", self.project, SECRETS, self.project / "broker.log", **kw)

    def ok(self, b: Broker, tool: str, **args) -> str:
        out = b.call(tool, args)
        self.assertTrue(out["ok"], out)
        return out["result"]

    def refused(self, b: Broker, tool: str, **args) -> str:
        out = b.call(tool, args)
        self.assertFalse(out["ok"], out)
        return out["error"]


class TokenTest(OwnMailCase):
    def test_google_key_refreshes_and_is_added_as_bearer(self):
        core.store_google_key(self.root, "gmail", key())
        b = self.broker()
        self.ok(b, "http_request", url=self.gmail.base + "/messages", auth="gmail")
        self.ok(b, "http_request", url=self.gmail.base + "/messages", auth="gmail")
        self.assertEqual(self.tokens.minted, ["ya29.access-1"])                 # one token, kept for the run
        self.assertEqual([s["auth"] for s in self.gmail.seen], ["Bearer ya29.access-1"] * 2)
        token, _ = b.google_tokens["gmail"]
        b.google_tokens["gmail"] = (token, 0.0)                                  # expired
        self.ok(b, "http_request", url=self.gmail.base + "/messages", auth="gmail")
        self.assertEqual(self.gmail.seen[-1]["auth"], "Bearer ya29.access-2")

    def test_google_key_401_makes_a_new_token_next_call(self):
        core.store_google_key(self.root, "gmail", key())
        b = self.broker()
        self.gmail.unauthorized = True
        self.assertTrue(self.ok(b, "http_request", url=self.gmail.base + "/messages", auth="gmail").startswith("HTTP 401"))
        self.gmail.unauthorized = False
        self.ok(b, "http_request", url=self.gmail.base + "/messages", auth="gmail")
        self.assertEqual(self.gmail.seen[-1]["auth"], "Bearer ya29.access-2")

    def test_google_key_token_masked_in_answer_and_log(self):
        core.store_google_key(self.root, "gmail", key())
        b = self.broker()
        out = self.ok(b, "http_request", url=self.gmail.base + "/messages", auth="gmail")
        self.assertIn("Bearer [your key]", out)                                 # the fake echoed it back
        self.assertNotIn("ya29.access-1", out)
        log = (self.project / "broker.log").read_text()
        self.assertNotIn("ya29", log)
        self.assertNotIn("1//refresh-thelos", log)
        self.assertNotIn("1//refresh-thelos", self.gmail.seen[0]["auth"] + self.gmail.seen[0]["body"])

    def test_google_key_only_to_its_hosts(self):
        core.store_google_key(self.root, "gmail", key(hosts=["gmail.googleapis.com"]))
        err = self.refused(self.broker(), "http_request", url=self.gmail.base + "/messages", auth="gmail")
        self.assertIn("may only be sent to gmail.googleapis.com", err)
        self.assertEqual(self.tokens.minted, [])                                 # refused before any token is made
        self.assertEqual(self.gmail.seen, [])

    def test_google_key_refused_refresh_named(self):
        core.store_google_key(self.root, "gmail", key())
        self.tokens.refused = True
        err = self.refused(self.broker(), "http_request", url=self.gmail.base + "/messages", auth="gmail")
        self.assertIn("your Google key 'gmail' (assistant@example.com): Google refused its login", err)
        self.assertIn("shaft secret thelos google gmail assistant@example.com --client personal", err)
        self.assertEqual(self.gmail.seen, [])                                    # no request without a token

    def test_google_key_cannot_be_operator_account_at_use(self):
        # Hand-written into the key file: an instance holding the operator's mailbox would read it whole, with no
        # label check (docs/plans/PLAN-mail). Refused on every use, before a token is made.
        core.store_google_key(self.root, "gmail", key(address=OPERATOR.upper()))
        err = self.refused(self.broker(), "http_request", url=self.gmail.base + "/messages", auth="gmail")
        self.assertIn("the operator's own Google account", err)
        self.assertEqual(self.tokens.minted, [])
        self.assertEqual(self.gmail.seen, [])

    def test_secret_list_shows_the_google_key_never_a_token(self):
        core.store_google_key(self.root, "gmail", key())
        b = self.broker()
        self.ok(b, "http_request", url=self.gmail.base + "/messages", auth="gmail")
        listing = self.ok(b, "secret_list")
        self.assertIn("gmail: Google account assistant@example.com (gmail.modify; refreshed by the supervisor)", listing)
        self.assertNotIn("ya29", listing)
        self.assertNotIn("1//refresh", listing)

    def test_secret_set_cannot_replace_google_key(self):
        core.store_google_key(self.root, "gmail", key())
        err = self.refused(self.broker(), "secret_set", name="gmail", value="x", hosts=["127.0.0.1"])
        self.assertIn("your Google login", err)
        self.assertEqual(core.read_secrets(self.root)["gmail"]["kind"], "google")
        self.assertTrue(self.ok(self.broker(), "secret_delete", name="gmail").startswith("deleted"))
        self.assertNotIn("gmail", core.read_secrets(self.root))

    def test_store_google_key_needs_its_login(self):
        with self.assertRaisesRegex(ValueError, "refresh_token"):
            core.store_google_key(self.root, "gmail", {**key(), "refresh_token": ""})
        with self.assertRaisesRegex(ValueError, "hosts"):
            core.store_google_key(self.root, "gmail", {**key(), "hosts": []})


class ExamAndReplayTest(OwnMailCase):
    def test_battery_never_mints_google_token(self):
        core.store_google_key(self.root, "gmail", key())
        exam = self.broker(root=self.root, network=False)                      # as battery.py builds it
        err = self.refused(exam, "http_request", url=self.gmail.base + "/messages/send", method="POST",
                           body={"raw": "x"}, auth="gmail")
        self.assertIn("not used in a run without network", err)
        self.assertEqual(self.tokens.minted, [])
        self.assertEqual(self.gmail.seen, [])

    def test_battery_keyed_write_not_sent(self):
        # Found while planning docs/plans/PLAN-own-mail (D5): an exam's broker reads the instance's real keys (root = its
        # folder) and network=False only closed the proxy, so a keyed POST reached the real API.
        core.store_secret(self.root, "github", "ghp_TEST", ["127.0.0.1"])
        exam = self.broker(root=self.root, network=False)
        out = self.ok(exam, "http_request", url=self.gmail.base + "/issues", method="POST", body={"t": 1}, auth="github")
        self.assertTrue(out.startswith("HTTP 200"), out)
        self.assertEqual(self.gmail.seen, [])                                    # nothing reached the API
        self.assertIn("not sent: no network in this run", (self.project / "broker.log").read_text())
        self.ok(exam, "http_request", url=self.gmail.base + "/issues", auth="github")   # a read still goes out
        self.assertEqual(len(self.gmail.seen), 1)

    def replay(self, cache, lock) -> ReplayBroker:
        state = Path(tempfile.mkdtemp(dir=self.project, prefix="copy-"))
        return ReplayBroker("thelos", self.project, SECRETS, self.project / "broker.log", state_root=state,
                            start_commit="0", as_of=datetime(2026, 10, 18, 8, 0), cache=cache, cache_lock=lock,
                            billed="thelos@trial:t", tools_log=self.project / "t.log")

    def test_replay_does_not_send_mail(self):
        core.store_google_key(self.root, "gmail", key())
        out = self.ok(self.replay({}, threading.Lock()), "http_request", url=self.gmail.base + "/messages/send",
                      method="POST", body={"raw": "aGk"}, auth="gmail")
        self.assertTrue(out.startswith("HTTP 200"), out)
        self.assertEqual(self.gmail.seen, [])

    def test_replay_pair_shares_keyed_reads(self):
        # Each run of the pair makes its own access token; with the header value in the cache key the second run's
        # read missed the first's answer and went to Gmail again (and the token sat in the cache).
        core.store_google_key(self.root, "gmail", key())
        cache, lock = {}, threading.Lock()
        first, second = self.replay(cache, lock), self.replay(cache, lock)
        a = self.ok(first, "http_request", url=self.gmail.base + "/messages", auth="gmail")
        b = self.ok(second, "http_request", url=self.gmail.base + "/messages", auth="gmail")
        self.assertEqual(self.tokens.minted, ["ya29.access-1", "ya29.access-2"])
        self.assertEqual(len(self.gmail.seen), 1)                                # the second answer was shared
        self.assertEqual(a, b)
        self.assertNotIn("ya29", json.dumps(list(cache)))


class LoginTest(OwnMailCase):
    def browser(self, url: str):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.asked.append(q)
        back = q["redirect_uri"] + "/?" + urllib.parse.urlencode({"code": "the-code", "state": q["state"]})
        urllib.request.urlopen(back, timeout=5).read()   # the real loopback, not the fake Google

    def cli(self, *argv) -> tuple[int, str, str]:
        from supervisor import __main__ as cli
        self.asked: list[dict] = []
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("supervisor.__main__.instance_root", return_value=self.root), \
             mock.patch.object(core, "secrets_env", return_value=SECRETS), \
             mock.patch("webbrowser.open", self.browser), mock.patch("supervisor.__main__.log"), \
             redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["secret", "thelos", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_shaft_secret_google_stores_the_key_with_its_scope_and_hosts(self):
        code, out, err = self.cli("google", "gmail", THELOS, "--client", "personal")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.asked[0]["scope"], google.GMAIL_MODIFY)            # only what the key needs
        self.assertEqual(self.asked[0]["include_granted_scopes"], "false")
        self.assertEqual(self.asked[0]["login_hint"], THELOS)
        entry = core.read_secrets(self.root)["gmail"]
        self.assertEqual({k: entry[k] for k in ("kind", "client", "address", "hosts")},
                         {"kind": "google", "client": "personal", "address": THELOS, "hosts": ["gmail.googleapis.com"]})
        self.assertNotIn("1//refresh", out)
        self.assertIn("http_request(auth='gmail') to gmail.googleapis.com", out)
        code, listing, _ = self.cli("list")
        self.assertIn("gmail: Google account assistant@example.com (gmail.modify", listing)

    def test_google_key_cannot_be_operator_account_at_login(self):
        code, _, err = self.cli("google", "gmail", OPERATOR, "--client", "personal")
        self.assertEqual(code, 2)
        self.assertIn("one of your own Google accounts", err)
        self.assertEqual(self.asked, [])                                         # the browser never opened
        self.assertEqual(core.read_secrets(self.root), {})

    def test_signed_in_as_another_account_saves_nothing(self):
        self.tokens.address = "someone.else@example.com"
        code, _, err = self.cli("google", "gmail", THELOS, "--client", "personal")
        self.assertEqual(code, 2)
        self.assertIn("not assistant@example.com: nothing saved", err)
        self.assertEqual(core.read_secrets(self.root), {})

    def test_scope_unticked_saves_nothing(self):
        self.tokens.granted = google.GMAIL
        code, _, err = self.cli("google", "gmail", THELOS, "--client", "personal")
        self.assertEqual(code, 2)
        self.assertIn("Google did not grant gmail.modify", err)
        self.assertEqual(core.read_secrets(self.root), {})

    def test_usage_names_the_clients_in_env(self):
        code, _, err = self.cli("google", "gmail", THELOS)
        self.assertEqual(code, 2)
        self.assertIn("the OAuth clients in .env: personal", err)


if __name__ == "__main__":
    unittest.main()
