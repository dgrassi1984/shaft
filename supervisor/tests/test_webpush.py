"""Web Push — the key, the signed token a push service checks, the subscriptions, what is sent and when."""
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
SAME = {"sec-fetch-site": "same-origin"}                     # the service worker's own fetch


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


class QueueTest(PushTest):
    def test_instance_takes_leave_other_chats_queued_and_remember_each_last_event(self):
        from supervisor import web_alerts, web_chat
        a = web_chat.reply_event({"instance": "wisp", "turns": [{"role": "assistant", "content": "Done."}]})
        b = web_alerts.event({"from": "wisp-message", "file": "one.md", "text": "Which one?", "kind": "question"})
        webpush.subscribe("phone", {"endpoint": APPLE})
        with mock.patch.object(webpush, "_wake"):
            webpush.notify(a)
            webpush.notify(b)
        self.assertEqual(webpush.take("phone", instance="wisp"), [a])
        self.assertEqual([e["body"] for e in webpush._queue()["phone"]["pending"]], ["Which one?"])
        self.assertEqual(webpush.take("phone", instance="wisp-message"), [b])
        self.assertEqual(webpush.take("phone", instance="wisp"), [a])
        self.assertEqual(webpush.take("phone", instance="wisp-message"), [b])
        self.assertEqual(webpush.take("phone", instance="unknown"), [webpush.NOTHING])
        self.assertEqual(webpush.take("phone"), [b])

    def test_instance_fallback_uses_only_a_matching_legacy_last(self):
        event = {"title": "wisp replied", "body": "Done.", "url": "/i/wisp/chat", "tag": "wisp", "at": 100}
        webpush._save_queue({"phone": {"pending": [], "last": event}})
        self.assertEqual(webpush.take("phone", instance="other", now=100), [webpush.NOTHING])
        self.assertEqual(webpush.take("phone", instance="wisp", now=100),
                         [{k: v for k, v in event.items() if k != "at"}])

    def test_unscoped_take_remembers_every_instances_fallback(self):
        a = {"title": "A", "body": "one", "url": "/i/a/chat", "tag": "a", "at": 100}
        b = {"title": "B", "body": "two", "url": "/i/b/chat", "tag": "b-message", "at": 100}
        webpush._save_queue({"phone": {"pending": [a, b]}})
        self.assertEqual([e["body"] for e in webpush.take("phone", now=100)], ["one", "two"])
        self.assertEqual(webpush.take("phone", instance="a", now=100)[0]["body"], "one")
        self.assertEqual(webpush.take("phone")[0]["body"], "two")

    def test_each_device_gets_its_own_copy_old_ones_expire_and_unsubscribing_drops_them(self):
        for d in ("phone", "laptop"):
            webpush.subscribe(d, {"endpoint": APPLE})
        with mock.patch.object(webpush, "_wake"):
            webpush.notify({"title": "a", "body": "", "url": "/", "tag": "a"})
            with mock.patch.object(webpush.time, "time", return_value=time.time() - webpush.TTL_S - 5):
                webpush.notify({"title": "stale", "body": "", "url": "/", "tag": "s"})
            webpush.notify({"title": "b", "body": "", "url": "/", "tag": "b"})
        self.assertEqual([e["title"] for e in webpush.take("phone")], ["a", "b"])   # older than its TTL: skipped
        self.assertEqual([e["title"] for e in webpush.take("laptop")], ["a", "b"])  # the laptop's own copy
        webpush.unsubscribe("laptop")
        self.assertEqual(webpush.take("laptop"), [webpush.NOTHING])

    def test_a_wake_that_failed_shows_with_the_next_and_an_empty_wake_shows_the_last_again(self):
        """Codex review 2026-10-20: A's wake failed, B's got through and took A — B was never shown. A wake now shows
        everything waiting; one with nothing waiting re-shows the last under its own tag (iOS shows every push)."""
        webpush.subscribe("phone", {"endpoint": APPLE})
        with mock.patch.object(webpush, "_wake"):
            webpush.notify({"title": "A", "body": "", "url": "/", "tag": "a"})     # its wake failed: nothing taken
            webpush.notify({"title": "B", "body": "", "url": "/", "tag": "b"})
        self.assertEqual([e["title"] for e in webpush.take("phone")], ["A", "B"])
        self.assertEqual(webpush.take("phone"), [{"title": "B", "body": "", "url": "/", "tag": "b"}])

    def test_a_failed_wake_is_tried_again_a_gone_one_is_not(self):
        said = iter(["not sent: no network", "201"])
        with mock.patch.object(webpush, "_send_one", side_effect=lambda d, e: next(said)) as send, \
                mock.patch.object(webpush.time, "sleep"), \
                mock.patch.object(webpush.threading, "Thread", lambda target, args, **kw: mock.Mock(
                    start=lambda: target(*args))):
            webpush._wake({"phone": {"endpoint": APPLE}}, "x")
            self.assertEqual(send.call_count, 2)
            send.reset_mock(side_effect=True)
            send.return_value = "410: gone, removed"
            webpush._wake({"phone": {"endpoint": APPLE}}, "x")
            self.assertEqual(send.call_count, 1)

    def test_a_device_off_for_long_keeps_only_the_newest(self):
        webpush.subscribe("phone", {"endpoint": APPLE})
        with mock.patch.object(webpush, "_wake"):
            for k in range(webpush.QUEUE_MAX + 5):
                webpush.notify({"title": str(k), "body": "", "url": "/", "tag": "x"})
        shown = webpush.take("phone")
        self.assertEqual((len(shown), shown[0]["title"]), (webpush.QUEUE_MAX, "5"))


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
        self.assertEqual(self.req("/push/latest", headers=SAME).status, 403)  # unpaired
        self.assertEqual(json.loads(self.latest().body), {"events": [webpush.NOTHING]})
        # It takes what it returns: a link from another site (a top-level navigation carries the Lax cookie) never
        # does (Codex review, 2026-10-20).
        self.assertEqual(self.req("/push/latest", cookie=self.cookie, headers={"sec-fetch-site": "cross-site"}).status,
                         403)
        self.assertEqual(self.req("/push/latest", cookie=self.cookie).status, 403)

    def latest(self):
        return self.req("/push/latest", cookie=self.cookie, headers=SAME)

    def test_two_quick_pushes_show_two_notifications_each_its_own(self):
        """Issue #23 (part 2): each push fetched "the newest reply of any instance", so A's reply then B's, both pushed
        before the phone fetched, showed B's text twice and A's never. Each push now shows its own event."""
        from supervisor import web
        self.assertEqual(self.post("/push/subscribe", {"endpoint": APPLE}).status, 200)
        dev = web.device_from_cookie(self.cookie)[0]
        with mock.patch.object(webpush, "_wake"):
            webpush.notify({"title": "tundra replied", "body": "Done", "url": "/i/tundra/chat", "tag": "tundra"})
            webpush.notify({"title": "wisp asks", "body": "Which one?", "url": "/i/wisp/chat", "tag": "wisp-message"})
        shown = json.loads(self.latest().body)["events"]
        self.assertEqual([s["title"] for s in shown], ["tundra replied", "wisp asks"])
        self.assertEqual(shown[1], {"title": "wisp asks", "body": "Which one?", "url": "/i/wisp/chat",
                                    "tag": "wisp-message"})
        self.assertEqual(webpush._queue()[dev]["pending"], [])               # all taken

    def test_the_service_worker_shows_what_it_fetches_and_opens_the_chat(self):
        sw = self.req("/sw.js").body.decode()
        self.assertIn('self.addEventListener("push"', sw)
        self.assertIn('fetch("/push/latest", { credentials: "same-origin", cache: "no-store", signal: ctl.signal })', sw)
        self.assertIn("self.registration.showNotification(n.title", sw)        # always one: iOS requires it
        self.assertIn("for (const n of shown)", sw)                            # each waiting event its own
        self.assertIn('self.addEventListener("notificationclick"', sw)


class WhenTest(unittest.TestCase):
    def test_a_chat_on_screen_is_watched_by_that_device_only_and_until_it_goes_away(self):
        """W9 (review 2026-10-24): watched was one flag per instance, so a desktop tab silenced the phone's pushes, and
        a phone that locked stayed "watching" for WATCH_S more. Per device now, and ?away=1 ends it at once."""
        from supervisor import web_chat
        web_chat._watched.clear()
        self.assertEqual(web_chat.watchers("tundra"), set())
        web_chat.watching("tundra", "laptop")
        self.assertEqual(web_chat.watchers("tundra"), {"laptop"})
        self.assertEqual(web_chat.watchers("wisp"), set())                    # another instance's chat
        web_chat.watching("tundra", "phone")
        web_chat.unwatch("tundra", "phone")                                  # the phone locked
        self.assertEqual(web_chat.watchers("tundra"), {"laptop"})
        with mock.patch.object(web_chat.time, "monotonic", return_value=time.monotonic() + web_chat.WATCH_S + 1):
            self.assertEqual(web_chat.watchers("tundra"), set())


class RenewRaceTest(PushTest):
    def test_a_failed_wake_of_the_old_endpoint_never_drops_the_renewed_one(self):
        """Grok review of 8735c62 (S2): notify() snapshots the endpoint, _wake retries it for 2 minutes; meanwhile the
        page renews the subscription. A 404/410/401/403 for the OLD endpoint then deleted the NEW record. It is dropped
        only if the endpoint SHAFT holds is still the one that failed."""
        old, new = APPLE + "-old", APPLE + "-new"
        webpush.subscribe("phone", {"endpoint": old})
        webpush.subscribe("phone", {"endpoint": new})                        # renewed while the old wake retries
        for code in (404, 410, 401, 403):
            gone = urllib.error.HTTPError(old, code, "x", {}, None)
            gone.read = lambda *a: b""
            with mock.patch("urllib.request.urlopen", side_effect=gone):
                said = webpush._send_one("phone", old)
            self.assertNotIn("removed", said, code)
            self.assertEqual(webpush.subscriptions()["phone"]["endpoint"], new, code)
        gone = urllib.error.HTTPError(new, 410, "Gone", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=gone):
            self.assertEqual(webpush._send_one("phone", new), "410: gone, removed")
        self.assertEqual(webpush.subscriptions(), {})

    def test_a_subscription_recorded_before_keys_were_recorded_is_not_stale(self):
        """Grok review of 8735c62 (S2): every subscription stored before this change has no key, so all of them read
        as stale and every device renewed at once on the first visit. Unknown is not stale; the page still compares
        the browser's own key."""
        webpush.subscribe("phone", {"endpoint": APPLE})
        subs = webpush.subscriptions()
        del subs["phone"]["key"]
        webpush._save(subs)
        self.assertIs(webpush.status("phone")["stale"], False)


class PairedOnlyTest(PushTest):
    def pair(self, *devices):
        (self.sup / "web").mkdir(parents=True, exist_ok=True)
        (self.sup / "web" / "devices.json").write_text(json.dumps({d: {"name": d} for d in devices}))

    def test_w1_an_unpaired_device_is_never_pushed_and_loses_its_subscription(self):
        """W1 (review 2026-10-24): unpairing deleted the device but not its push subscription, so a revoked phone kept
        getting a lock-screen push and a badge for every reply. notify() pushes only to paired devices, and drops the
        subscription of one that is not."""
        self.pair("phone")
        for d in ("phone", "revoked"):
            webpush.subscribe(d, {"endpoint": APPLE})
        with mock.patch.object(webpush, "_wake") as wake:
            self.assertEqual(webpush.notify({"title": "a", "body": "", "url": "/", "tag": "a"}), ["phone"])
        self.assertEqual(set(wake.call_args[0][0]), {"phone"})
        self.assertEqual(set(webpush.subscriptions()), {"phone"})
        self.assertNotIn("revoked", webpush._queue())

    def test_w1_a_devices_file_that_cannot_be_read_drops_nobody(self):
        webpush.subscribe("phone", {"endpoint": APPLE})                      # no devices.json at all
        with mock.patch.object(webpush, "_wake"):
            self.assertEqual(webpush.notify({"title": "a", "body": "", "url": "/", "tag": "a"}), ["phone"])
        self.assertEqual(set(webpush.subscriptions()), {"phone"})

    def test_w9_a_device_with_the_chat_on_screen_is_skipped(self):
        self.pair("phone", "laptop")
        for d in ("phone", "laptop"):
            webpush.subscribe(d, {"endpoint": APPLE})
        with mock.patch.object(webpush, "_wake") as wake:
            self.assertEqual(webpush.notify({"title": "a", "body": "", "url": "/", "tag": "a"}, skip={"laptop"}),
                             ["phone"])
            self.assertEqual(set(wake.call_args[0][0]), {"phone"})
            self.assertEqual(webpush.notify({"title": "b", "body": "", "url": "/", "tag": "b"},
                                            skip={"laptop", "phone"}), [])
        self.assertEqual(webpush.take("laptop"), [webpush.NOTHING])          # nothing queued for the one looking

    def test_w5_a_refused_subscription_is_dropped_and_status_says_which_key(self):
        """W5 (review 2026-10-24): fingerprint() was never used and a 401/403 left the subscription in place, so after
        the key changed every push failed while the button still said on. A refusal drops it; status names the
        endpoint, the key now, and whether the subscription was made for another key."""
        webpush.subscribe("phone", {"endpoint": APPLE})
        state = webpush.status("phone")
        self.assertEqual((state["subscribed"], state["endpoint"], state["stale"]), (True, APPLE, False))
        self.assertEqual(state["key"], webpush.public_key())
        subs = webpush.subscriptions()
        subs["phone"]["key"] = "an-older-key"
        webpush._save(subs)
        self.assertIs(webpush.status("phone")["stale"], True)
        refused = urllib.error.HTTPError(APPLE, 403, "Forbidden", {}, None)
        refused.read = lambda *a: b'{"reason":"BadJwtToken"}'
        with mock.patch("urllib.request.urlopen", side_effect=refused):
            self.assertRegex(webpush._send_one("phone", APPLE), r'^403: refused \(\{"reason":"BadJwtToken"\}\), removed$')
        self.assertEqual(webpush.subscriptions(), {})
        self.assertEqual(webpush.status("phone")["subscribed"], False)


if __name__ == "__main__":
    unittest.main()
