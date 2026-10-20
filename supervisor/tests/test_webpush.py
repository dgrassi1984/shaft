"""docs/plans/PLAN-pwa P4: Web Push — the key, the signed token a push service checks, the subscriptions, what is sent and when."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from supervisor import webpush
from supervisor.tests.test_web import WebTest

APPLE = "https://web.push.apple.com/FAKE-endpoint-for-tests"


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def raw_to_der(raw: bytes) -> bytes:
    def integer(b: bytes) -> bytes:
        b = b.lstrip(b"\0") or b"\0"
        if b[0] & 0x80:
            b = b"\0" + b
        return b"\x02" + bytes([len(b)]) + b
    body = integer(raw[:32]) + integer(raw[32:])
    return b"\x30" + bytes([len(body)]) + body


class PushTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-push-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        for p in (mock.patch("supervisor.core.SUP_DIR", self.sup), mock.patch.object(webpush, "log")):
            p.start()
            self.addCleanup(p.stop)


class KeyAndTokenTest(PushTest):
    def test_the_key_is_made_once_private_and_its_public_point_is_65_bytes(self):
        key = webpush.public_key()
        self.assertEqual(len(unb64(key)), 65)
        self.assertEqual(unb64(key)[0], 4)                                   # an uncompressed point
        self.assertEqual(webpush.public_key(), key)                          # made once
        self.assertEqual((self.sup / "web" / "vapid.pem").stat().st_mode & 0o777, 0o600)

    def test_the_token_is_a_valid_es256_signature_for_the_push_services_origin(self):
        token = webpush.vapid_token(APPLE, now=1_800_000_000)
        head, claims, sig = token.split(".")
        self.assertEqual(json.loads(unb64(head)), {"typ": "JWT", "alg": "ES256"})
        self.assertEqual(json.loads(unb64(claims)), {"aud": "https://web.push.apple.com", "exp": 1_800_043_200,
                                                     "sub": webpush.SUBJECT})
        self.assertEqual(len(unb64(sig)), 64)                               # r || s, not DER
        # openssl checks it: the raw signature back to DER, verified with the public key
        pub = subprocess.run([webpush.OPENSSL, "ec", "-in", str(self.sup / "web" / "vapid.pem"), "-pubout"],
                             capture_output=True).stdout
        (self.sup / "pub.pem").write_bytes(pub)
        (self.sup / "sig.der").write_bytes(raw_to_der(unb64(sig)))
        ok = subprocess.run([webpush.OPENSSL, "dgst", "-sha256", "-verify", str(self.sup / "pub.pem"), "-signature",
                             str(self.sup / "sig.der")], input=f"{head}.{claims}".encode(), capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

    def test_der_signatures_of_every_shape_become_64_bytes(self):
        for r, s in ((b"\x80" + b"\x01" * 31, b"\x00" * 31 + b"\x05"), (b"\x01" * 32, b"\xff" * 32)):
            self.assertEqual(webpush._der_to_raw(raw_to_der(r + s)), r + s)


class SubscriptionsTest(PushTest):
    def test_only_known_push_services(self):
        for ok in (APPLE, "https://fcm.googleapis.com/fcm/send/x", "https://updates.push.services.mozilla.com/wpush/v2/x",
                   "https://db5p.notify.windows.com/w/?token=x"):
            self.assertTrue(webpush.allowed_endpoint(ok), ok)
        for bad in ("http://web.push.apple.com/x", "https://evil.example/x", "https://web.push.apple.com.evil.example/x",
                    "https://127.0.0.1:8785/x", "https://localhost/x", "file:///etc/passwd", ""):
            self.assertFalse(webpush.allowed_endpoint(bad), bad)
            with self.assertRaises(webpush.PushError):
                webpush.subscribe("dev1", {"endpoint": bad})
        self.assertEqual(webpush.subscriptions(), {})

    def test_a_push_carries_nothing_and_a_gone_subscription_is_dropped(self):
        webpush.subscribe("dev1", {"endpoint": APPLE, "keys": {"p256dh": "x", "auth": "y"}})
        sent = []

        class Answer:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout):
            sent.append(req)
            return Answer()
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(webpush._send_one("dev1", APPLE), "201")
        req = sent[0]
        self.assertEqual((req.full_url, req.method, req.data), (APPLE, "POST", b""))
        self.assertEqual(req.get_header("Ttl"), str(webpush.TTL_S))
        self.assertRegex(req.get_header("Authorization"), r"^vapid t=[\w-]+\.[\w-]+\.[\w-]+, k=[\w-]{87}$")
        gone = urllib.error.HTTPError(APPLE, 410, "Gone", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=gone):
            self.assertEqual(webpush._send_one("dev1", APPLE), "410: gone, removed")
        self.assertEqual(webpush.subscriptions(), {})


class RoutesTest(WebTest):
    def setUp(self):
        super().setUp()
        for p in (mock.patch("supervisor.core.SUP_DIR", self.sup), mock.patch.object(webpush, "log")):
            p.start()
            self.addCleanup(p.stop)
        self.cookie = self.paired()

    def post(self, path, body, token=True):
        h = {"sec-fetch-site": "same-origin", "content-type": "application/json"}
        if token:
            h["x-csrf-token"] = self.csrf(self.cookie)
        return self.req(path, "POST", cookie=self.cookie, body=json.dumps(body).encode(), headers=h)

    def test_subscribing_needs_the_page_token_and_a_known_service(self):
        self.assertEqual(self.post("/push/subscribe", {"endpoint": APPLE}, token=False).status, 403)
        self.assertEqual(self.post("/push/subscribe", {"endpoint": "https://evil.example/x"}).status, 409)
        self.assertEqual(self.post("/push/subscribe", {"endpoint": APPLE}).status, 200)
        self.assertEqual([s["endpoint"] for s in webpush.subscriptions().values()], [APPLE])
        self.assertEqual(self.post("/push/unsubscribe", {}).status, 200)
        self.assertEqual(webpush.subscriptions(), {})

    def test_issue_23_status_says_whether_shaft_holds_this_device(self):
        """#23: the button's "on" asks /push/status. Without it a device SHAFT dropped (or refused) read as on."""
        self.assertEqual(self.req("/push/status").status, 403)                # unpaired
        status = lambda: json.loads(self.req("/push/status", cookie=self.cookie).body)["subscribed"]
        self.assertIs(status(), False)
        self.assertEqual(self.post("/push/subscribe", {"endpoint": "https://evil.example/x"}).status, 409)
        self.assertIs(status(), False)                                        # refused: still off
        self.assertEqual(self.post("/push/subscribe", {"endpoint": APPLE}).status, 200)
        self.assertIs(status(), True)
        self.assertEqual(self.post("/push/unsubscribe", {}).status, 200)
        self.assertIs(status(), False)

    def test_the_key_and_what_to_show_are_for_a_paired_device_only(self):
        self.assertEqual(self.req("/push/key").status, 403)                   # unpaired
        key = json.loads(self.req("/push/key", cookie=self.cookie).body)["key"]
        self.assertEqual(len(unb64(key)), 65)
        with mock.patch("supervisor.web_chat.latest_reply", return_value={"title": "thelos replied", "body": "Done",
                                                                          "url": "/i/thelos/chat", "tag": "thelos"}):
            shown = json.loads(self.req("/push/latest", cookie=self.cookie).body)
        self.assertEqual(shown["title"], "thelos replied")

    def test_the_service_worker_shows_what_it_fetches_and_opens_the_chat(self):
        sw = self.req("/sw.js").body.decode()
        self.assertIn('self.addEventListener("push"', sw)
        self.assertIn('fetch("/push/latest", { credentials: "same-origin", cache: "no-store" })', sw)
        self.assertIn("self.registration.showNotification(n.title", sw)        # always one: iOS requires it
        self.assertIn('self.addEventListener("notificationclick"', sw)


class WhenTest(unittest.TestCase):
    def test_a_reply_watched_on_screen_sends_no_push(self):
        from supervisor import web_chat
        web_chat._watched.clear()
        self.assertFalse(web_chat.watched("thelos"))
        web_chat.watching("thelos")
        self.assertTrue(web_chat.watched("thelos"))
        with mock.patch.object(web_chat.time, "monotonic", return_value=time.monotonic() + web_chat.WATCH_S + 1):
            self.assertFalse(web_chat.watched("thelos"))


if __name__ == "__main__":
    unittest.main()
