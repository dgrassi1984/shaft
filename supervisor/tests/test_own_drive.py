"""An instance's own Drive, Sheets, Docs and Slides (docs/plans/PLAN-own-drive.md): a `drive` bundle for its own Google key,
every scope in it granted or nothing saved, the address checked through Drive's `about` (no Gmail scope to read a
profile with); and http_request's binary bodies (`body_file`, `save_to`), which a Drive upload or download needs."""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from supervisor import broker as broker_mod
from supervisor import core, google
from supervisor.tests import test_own_mail as own_mail
from supervisor.tests.test_own_mail import OPERATOR, THELOS

DRIVE_SCOPES = " ".join((google.DRIVE, google.SHEETS, google.DOCS, google.SLIDES))
DRIVE_HOSTS = ["www.googleapis.com", "sheets.googleapis.com", "docs.googleapis.com", "slides.googleapis.com"]
# Every byte value, twice: text decoding or a text body would change some of them.
BINARY = bytes(range(256)) * 2 + b"%PDF-1.7\r\n\x00\xff"


class FakeDriveTokens(own_mail.FakeTokens):
    """FakeTokens for a Drive-only login: the address is in Drive's `about`; Gmail's profile is not reachable with
    this token (as on Google), so a login that still asked Gmail fails the test."""

    def __init__(self, address: str = THELOS, granted: str = DRIVE_SCOPES):
        super().__init__(address, granted)

    def __call__(self, req: urllib.request.Request, timeout: float = 0):
        if req.full_url == google.DRIVE_ABOUT:
            return io.BytesIO(json.dumps({"user": {"emailAddress": self.address}}).encode())
        if req.full_url == google.GMAIL_PROFILE:
            raise AssertionError("a Drive login read Gmail's profile")
        return super().__call__(req, timeout)


class FakeDrive:
    """Drive on localhost: records each request's raw bytes, and answers GET /file with BINARY (or a set status)."""

    def __init__(self):
        self.seen: list[dict] = []
        self.status, self.headers = 200, {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                fake.seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                                  "type": self.headers.get("Content-Type"), "body": self.rfile.read(n)})
                body = BINARY if self.path.startswith("/file") else json.dumps({"id": "f1"}).encode()
                if fake.status != 200:
                    body = json.dumps({"error": {"code": fake.status, "message": "File not found"}}).encode()
                self.send_response(fake.status)
                for k, v in fake.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/octet-stream" if fake.status == 200 else
                                 "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def drive_key(address: str = THELOS, hosts=("127.0.0.1",)) -> dict:
    return {"kind": "google", "client": "personal", "address": address, "refresh_token": "1//refresh-thelos-drive",
            "scope": DRIVE_SCOPES, "hosts": list(hosts)}


class DriveCase(own_mail.OwnMailCase):
    def setUp(self):
        super().setUp()
        self.drive = FakeDrive()
        self.addCleanup(self.drive.close)


class DriveLoginTest(own_mail.LoginTest):
    """`shaft secret thelos google drive … --scope drive`, through the real CLI and the real loopback."""
    def setUp(self):
        super().setUp()
        self.tokens = FakeDriveTokens()
        p = mock.patch.object(google, "urlopen", self.tokens)
        p.start()
        self.addCleanup(p.stop)

    # LoginTest's own tests are about the gmail key; this class runs only its own.
    test_shaft_secret_google_stores_the_key_with_its_scope_and_hosts = None
    test_google_key_cannot_be_operator_account_at_login = None
    test_signed_in_as_another_account_saves_nothing = None
    test_scope_unticked_saves_nothing = None
    test_usage_names_the_clients_in_env = None

    def test_drive_bundle_asks_all_four_scopes_and_binds_four_hosts(self):
        code, out, err = self.cli("google", "drive", THELOS, "--client", "personal", "--scope", "drive")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.asked[0]["scope"].split(), DRIVE_SCOPES.split())   # the four, nothing of the operator's
        self.assertEqual(self.asked[0]["include_granted_scopes"], "false")
        entry = core.read_secrets(self.root)["drive"]
        self.assertEqual((entry["kind"], entry["address"], entry["hosts"]), ("google", THELOS, sorted(DRIVE_HOSTS)))
        self.assertIn("http_request(auth='drive') to docs.googleapis.com, sheets.googleapis.com, "
                      "slides.googleapis.com, www.googleapis.com", out)
        self.assertNotIn("1//refresh", out)
        code, listing, _ = self.cli("list")
        self.assertIn("drive: Google account assistant@example.com (documents, drive, presentations, spreadsheets",
                      listing)

    def test_drive_signed_in_as_reads_drive_about(self):
        # Before docs/plans/PLAN-own-drive, _signed_in_as knew only Gmail and Calendar: a Drive-only login was refused with
        # "Google granted no Gmail or Calendar scope". Here the address comes from Drive's `about` and is checked.
        self.tokens.address = "someone.else@example.com"
        code, _, err = self.cli("google", "drive", THELOS, "--client", "personal", "--scope", "drive")
        self.assertEqual(code, 2)
        self.assertIn("signed in as someone.else@example.com, not assistant@example.com: nothing saved", err)
        self.assertEqual(core.read_secrets(self.root), {})

    def test_drive_bundle_partial_grant_saves_nothing(self):
        self.tokens.granted = google.DRIVE                                        # three boxes unticked
        code, _, err = self.cli("google", "drive", THELOS, "--client", "personal", "--scope", "drive")
        self.assertEqual(code, 2)
        self.assertIn("Google did not grant spreadsheets, documents, presentations", err)
        self.assertIn("drive needs all of drive, spreadsheets, documents, presentations", err)
        self.assertEqual(core.read_secrets(self.root), {})

    def test_drive_key_cannot_be_operator_account_at_login(self):
        code, _, err = self.cli("google", "drive", OPERATOR, "--client", "personal", "--scope", "drive")
        self.assertEqual(code, 2)
        self.assertIn("one of your own Google accounts", err)
        self.assertEqual(self.asked, [])                                          # the browser never opened

    def test_unknown_bundle_refused_naming_the_bundles(self):
        code, _, err = self.cli("google", "drive", THELOS, "--client", "personal", "--scope", "drive.file")
        self.assertEqual(code, 2)
        self.assertIn("may be granted gmail.modify, drive, not 'drive.file'", err)
        self.assertEqual(self.asked, [])


class DriveKeyTest(DriveCase):
    def test_drive_key_only_to_its_hosts(self):
        core.store_google_key(self.root, "drive", drive_key(hosts=DRIVE_HOSTS))
        b = self.broker()
        err = self.refused(b, "http_request", url="https://gmail.googleapis.com/gmail/v1/users/me/messages",
                           auth="drive")
        self.assertIn("key 'drive' may only be sent to docs.googleapis.com, sheets.googleapis.com, "
                      "slides.googleapis.com, www.googleapis.com, not gmail.googleapis.com", err)
        self.refused(b, "http_request", url="https://example.com/x", auth="drive")
        self.assertEqual(self.tokens.minted, [])                                  # refused before any token

    def test_drive_key_cannot_be_operator_account_at_use(self):
        core.store_google_key(self.root, "drive", drive_key(address=OPERATOR))   # put in the file by hand
        err = self.refused(self.broker(), "http_request", url=self.drive.base + "/file", auth="drive")
        self.assertIn("operator's own Google account", err)
        self.assertEqual(self.drive.seen, [])


class BinaryBodyTest(DriveCase):
    def test_http_request_body_file_sent_as_bytes(self):
        core.store_google_key(self.root, "drive", drive_key())
        (self.root / "report.pdf").write_bytes(BINARY)
        out = self.ok(self.broker(), "http_request", url=self.drive.base + "/upload?uploadType=media",
                      method="POST", body_file="report.pdf", auth="drive")
        self.assertTrue(out.startswith("HTTP 200"), out)
        self.assertEqual(self.drive.seen[0]["body"], BINARY)                     # byte for byte
        self.assertEqual(self.drive.seen[0]["type"], "application/octet-stream")
        self.assertEqual(self.drive.seen[0]["auth"], "Bearer ya29.access-1")

    def test_body_file_keeps_its_content_type(self):
        (self.root / "a.png").write_bytes(BINARY)
        self.ok(self.broker(), "http_request", url=self.drive.base + "/upload", method="POST", body_file="a.png",
                headers={"Content-Type": "image/png"})
        self.assertEqual(self.drive.seen[0]["type"], "image/png")

    def test_body_file_refusals(self):
        b = self.broker()
        (self.root / "a.txt").write_text("x")
        self.assertIn("not both", self.refused(b, "http_request", url=self.drive.base, method="POST",
                                               body="y", body_file="a.txt"))
        self.assertIn("not a file in your folder", self.refused(b, "http_request", url=self.drive.base,
                                                                method="POST", body_file="missing.bin"))
        self.assertIn("relative to your folder", self.refused(b, "http_request", url=self.drive.base,
                                                              method="POST", body_file="../secrets.json"))
        os.symlink(self.project / "outside.bin", self.root / "link.bin")
        (self.project / "outside.bin").write_bytes(b"not yours")
        self.assertIn("is a symlink", self.refused(b, "http_request", url=self.drive.base, method="POST",
                                                   body_file="link.bin"))
        (self.root / "big.bin").write_bytes(BINARY)
        with mock.patch.object(broker_mod, "MAX_BODY_FILE_BYTES", 10):
            self.assertIn("over the 10 limit", self.refused(b, "http_request", url=self.drive.base, method="POST",
                                                            body_file="big.bin"))
        self.assertEqual(self.drive.seen, [])

    def test_http_request_save_to_writes_bytes(self):
        core.store_google_key(self.root, "drive", drive_key())
        out = self.ok(self.broker(), "http_request", url=self.drive.base + "/file?alt=media",
                      save_to="downloads/report.pdf", auth="drive")
        self.assertEqual((self.root / "downloads" / "report.pdf").read_bytes(), BINARY)
        self.assertIn(f"saved downloads/report.pdf: {len(BINARY)} bytes, sha256 "
                      f"{hashlib.sha256(BINARY).hexdigest()}", out)
        self.assertNotIn("%PDF", out)                                             # saved, not shown

    def test_save_to_refuses_existing_file(self):
        (self.root / "report.pdf").write_text("mine")
        err = self.refused(self.broker(), "http_request", url=self.drive.base + "/file", save_to="report.pdf")
        self.assertIn("already exists", err)
        self.assertEqual((self.root / "report.pdf").read_text(), "mine")
        self.assertEqual(self.drive.seen, [])                                     # refused before the request

    def test_save_to_refuses_path_outside_folder(self):
        b = self.broker()
        for path in ("../escape.bin", "/tmp/escape.bin", "a/../../escape.bin"):
            self.assertIn("relative to your folder", self.refused(b, "http_request", url=self.drive.base + "/file",
                                                                  save_to=path))
        self.assertFalse((self.project / "escape.bin").exists())
        self.assertEqual(self.drive.seen, [])

    def test_save_to_error_or_redirect_not_saved(self):
        core.store_google_key(self.root, "drive", drive_key())
        b = self.broker()
        self.drive.status = 404
        out = self.ok(b, "http_request", url=self.drive.base + "/file", save_to="a.pdf", auth="drive")
        self.assertIn("not saved (HTTP 404 is not the file)", out)
        self.assertIn("File not found", out)
        # A keyed request does not follow a redirect: the 302 itself comes back, and must not be saved as the file.
        self.drive.status, self.drive.headers = 302, {"Location": "https://example.com/elsewhere"}
        out = self.ok(b, "http_request", url=self.drive.base + "/file", save_to="a.pdf", auth="drive")
        self.assertIn("not saved (HTTP 302 is not the file)", out)
        self.assertIn("location: https://example.com/elsewhere", out.lower())
        self.assertFalse((self.root / "a.pdf").exists())

    def test_save_to_over_limit_saves_nothing(self):
        # A cut download must never pose as the whole file.
        with mock.patch.object(broker_mod, "MAX_SAVE_BYTES", 100):
            err = self.refused(self.broker(), "http_request", url=self.drive.base + "/file", save_to="a.pdf")
        self.assertIn("over 100 bytes; nothing saved", err)
        self.assertFalse((self.root / "a.pdf").exists())

    def test_text_answer_unchanged(self):
        out = self.ok(self.broker(), "http_request", url=self.drive.base + "/meta")
        self.assertTrue(out.startswith("HTTP 200"))
        self.assertIn('{"id": "f1"}', out)


class DriveReplayTest(DriveCase):
    def replay(self):
        return own_mail.ExamAndReplayTest.replay(self, {}, threading.Lock())

    def test_replay_drive_upload_not_sent(self):
        core.store_google_key(self.root, "drive", drive_key())
        r = self.replay()
        (r.root / "report.pdf").write_bytes(BINARY)                               # the replay's own copy
        out = self.ok(r, "http_request", url=self.drive.base + "/upload?uploadType=media", method="POST",
                      body_file="report.pdf", auth="drive")
        self.assertTrue(out.startswith("HTTP 200"), out)
        self.assertEqual(self.drive.seen, [])

    def test_replay_save_to_writes_into_the_copy(self):
        core.store_google_key(self.root, "drive", drive_key())
        r = self.replay()
        self.ok(r, "http_request", url=self.drive.base + "/file", save_to="report.pdf", auth="drive")
        self.assertEqual((r.root / "report.pdf").read_bytes(), BINARY)
        self.assertFalse((self.root / "report.pdf").exists())                    # never the instance's real folder


if __name__ == "__main__":
    unittest.main()
