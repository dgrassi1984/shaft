"""Native push: signed JWTs, title-only payloads, token failures and the shared device queue."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from supervisor import apns, core, web, web_alerts, webpush

TOKEN = "ab" * 32
TOPIC = "io.example.shaft"
EVENT = {"title": "thelos replied", "body": "private reply only over the tailnet", "url": "/i/thelos/chat",
         "tag": "thelos"}


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


class APNsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-apns-")
        self.addCleanup(tmp.cleanup)
        self.sup = Path(tmp.name)
        self.seen = types.ModuleType("supervisor.web_seen")
        self.seen.badge = mock.Mock(return_value=3)
        for p in (mock.patch.object(core, "SUP_DIR", self.sup), mock.patch.object(apns, "KEY_ID", "KEY123"),
                  mock.patch.object(apns, "TEAM_ID", "TEAM456"), mock.patch.object(apns, "TOPIC", TOPIC),
                  mock.patch.object(apns, "_cached", ()), mock.patch.object(apns, "log"),
                  mock.patch.object(webpush, "log"), mock.patch.dict(sys.modules, {"supervisor.web_seen": self.seen})):
            p.start()
            self.addCleanup(p.stop)
        self.key = self.sup / "web" / "apns" / "AuthKey_KEY123.p8"
        self.key.parent.mkdir(parents=True)
        subprocess.run([webpush.OPENSSL, "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-out", str(self.key)], capture_output=True, check=True)
        self.key.chmod(0o600)

    def register(self, device="phone", token=TOKEN, env="sandbox"):
        apns.register(device, token, env, TOPIC, "ios")
        return apns.devices()[device]

    def send(self, code, reason="", sub=None):
        sub = self.register() if sub is None else sub
        result = subprocess.CompletedProcess([], 0, (json.dumps({"reason": reason}) + "\n" + code).encode(), b"")
        with mock.patch.object(apns, "provider_token", return_value="signed.jwt"), \
                mock.patch.object(apns.subprocess, "run", return_value=result) as curl:
            said = apns._send_one("phone", sub, EVENT)
        return said, curl

    def test_jwt_verifies_with_openssl_and_renews_after_fifty_minutes(self):
        token = apns.provider_token(now=1_800_000_000)
        head, claims, sig = token.split(".")
        self.assertEqual(json.loads(unb64(head)), {"alg": "ES256", "kid": "KEY123"})
        self.assertEqual(json.loads(unb64(claims)), {"iss": "TEAM456", "iat": 1_800_000_000})
        self.assertEqual(len(unb64(sig)), 64)
        pub = subprocess.run([webpush.OPENSSL, "pkey", "-in", str(self.key), "-pubout"],
                             capture_output=True, check=True).stdout
        (self.sup / "pub.pem").write_bytes(pub)
        (self.sup / "sig.der").write_bytes(raw_to_der(unb64(sig)))
        ok = subprocess.run([webpush.OPENSSL, "dgst", "-sha256", "-verify", str(self.sup / "pub.pem"),
                             "-signature", str(self.sup / "sig.der")], input=f"{head}.{claims}".encode(),
                            capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        with mock.patch.object(apns.subprocess, "run") as sign:
            self.assertEqual(apns.provider_token(now=1_800_002_999), token)
            sign.assert_not_called()
        renewed = apns.provider_token(now=1_800_003_000)
        self.assertEqual(json.loads(unb64(renewed.split(".")[1]))["iat"], 1_800_003_000)

    def test_curl_uses_http2_headers_host_and_only_the_title(self):
        for env, host in (("sandbox", "api.sandbox.push.apple.com"), ("production", "api.push.apple.com")):
            with self.subTest(env=env), mock.patch.object(apns.time, "time", return_value=1_800_000_000):
                said, curl = self.send("200", sub=self.register(env=env))
            self.assertTrue(said.startswith("200"))
            args, kwargs = curl.call_args
            argv = args[0]
            self.assertEqual(argv[:2], ["/usr/bin/curl", "--http2"])
            self.assertEqual(argv[-1], f"https://{host}/3/device/{TOKEN}")
            headers = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--header"]
            self.assertEqual(headers, ["authorization: bearer signed.jwt", f"apns-topic: {TOPIC}",
                                      "apns-push-type: alert", "apns-priority: 10",
                                      f"apns-expiration: {1_800_000_000 + webpush.TTL_S}",
                                      "content-type: application/json"])
            self.assertEqual(argv[argv.index("--data-binary") + 1], "@-")
            self.assertEqual(json.loads(kwargs["input"]), {"aps": {"alert": {"title": "thelos replied"},
                              "badge": 3, "sound": "default", "thread-id": "thelos", "mutable-content": 1},
                              "instance": "thelos"})
            self.assertNotIn(EVENT["body"], repr(curl.call_args))
            self.seen.badge.assert_called_with("phone")

    def test_dead_tokens_are_dropped_but_a_renewed_token_is_kept(self):
        for code, reason in (("410", ""), ("400", "BadDeviceToken"), ("400", "Unregistered")):
            with self.subTest(code=code, reason=reason):
                said, _ = self.send(code, reason)
                self.assertIn("removed", said)
                self.assertEqual(apns.devices(), {})
        old = self.register()
        self.register(token="cd" * 32)
        said, _ = self.send("410", "Unregistered", sub=old)
        self.assertIn("stopped", said)
        self.assertEqual(apns.devices()["phone"]["token"], "cd" * 32)

    def test_bad_topic_and_provider_token_are_kept_and_log_actual_config(self):
        for reason in ("BadTopic", "InvalidProviderToken"):
            said, _ = self.send("400", reason)
            self.assertIn(reason, said)
            self.assertIn("phone", apns.devices())
            logged = apns.log.call_args[0][0]
            for value in (reason, "KEY123", "TEAM456", TOPIC, str(self.key)):
                self.assertIn(value, logged)

    def test_provider_rejection_renews_jwt_but_keeps_device_token(self):
        """ExpiredProviderToken and InvalidProviderToken retried the same cached JWT."""
        sub = self.register()
        for reason in ("ExpiredProviderToken", "InvalidProviderToken"):
            with self.subTest(reason=reason):
                token = apns.provider_token(now=1_800_000_000)
                said, _ = self.send("403", reason, sub=sub)
                self.assertIn(reason, said)
                self.assertEqual(apns.devices()["phone"], sub)
                renewed = apns.provider_token(now=1_800_000_001)
                self.assertEqual(json.loads(unb64(renewed.split(".")[1]))["iat"], 1_800_000_001)
                self.assertNotEqual(token, renewed)
                apns._cached = ()

    def test_device_token_not_for_topic_removes_only_matching_registration(self):
        """DeviceTokenNotForTopic left a dead device token registered and retried it."""
        said, _ = self.send("400", "DeviceTokenNotForTopic")
        self.assertIn("removed", said)
        self.assertEqual(apns.devices(), {})
        old = self.register()
        current = self.register(token="cd" * 32)
        said, _ = self.send("400", "DeviceTokenNotForTopic", sub=old)
        self.assertIn("stopped", said)
        self.assertEqual(apns.devices(), {"phone": current})

    def test_retry_stops_after_unpair_or_registration_change(self):
        """APNs retries used a captured subscription after unpair, unregister, or token replacement."""
        for change in ("unpair", "unregister", "replace", "already-unpaired"):
            with self.subTest(change=change):
                sub = self.register()
                paired = {} if change == "already-unpaired" else {"phone": {}}
                attempts = []
                def send(device, subscription, event):
                    attempts.append((device, subscription["token"], event["title"]))
                    return "503: offline"
                def sleep(seconds):
                    if seconds:
                        if change == "unpair":
                            paired.clear()
                        elif change == "unregister":
                            apns.unregister("phone")
                        elif change == "replace":
                            self.register(token="cd" * 32)
                def thread(**kw):
                    return types.SimpleNamespace(start=lambda: kw["target"](*kw["args"]))
                with mock.patch.object(web, "devices", side_effect=lambda: paired), \
                        mock.patch.object(apns, "_send_one", side_effect=send), \
                        mock.patch.object(apns.time, "sleep", side_effect=sleep), \
                        mock.patch.object(apns.threading, "Thread", side_effect=thread):
                    apns._wake({"phone": sub}, EVENT)
                self.assertEqual(attempts, [] if change == "already-unpaired" else [("phone", TOKEN, "thelos replied")])

    def test_digest_reaches_apns_only_operator(self):
        """The alert loop skipped due digests for APNs-only operators."""
        from supervisor import messages
        self.register()
        (self.sup / "messages.jsonl").write_text(json.dumps({"from": "thelos", "to": "operator", "text": "held",
            "file": "held.md", "ts": "2020-01-01T00:00:00", "push_due": "2020-01-01T07:15:00"}) + "\n")
        with mock.patch.object(messages, "appended", return_value=([], 0)), \
                mock.patch.object(web, "devices", return_value={"phone": {}}), \
                mock.patch.object(apns, "_wake"):
            self.assertEqual(web_alerts.forward_new(), 1)
            self.assertEqual(webpush.take("phone"), [{"title": "SHAFT digest (1)", "body": "thelos: held",
                "url": "/overview", "tag": "shaft-digest-held.md"}])
            self.assertEqual(web_alerts.forward_new(), 0)

    def test_unconfigured_apns_without_registration_does_not_log(self):
        (self.sup / "web" / "devices.json").write_text("{}")
        with mock.patch.object(apns, "TEAM_ID", ""):
            webpush.notify(EVENT)
        webpush.log.assert_not_called()

    def test_unconfigured_apns_registration_does_not_claim_digest(self):
        """Stored APNs registrations claimed and lost digests while native push was off."""
        from supervisor import messages, pushes
        self.register()
        (self.sup / "web" / "devices.json").write_text(json.dumps({"phone": {}}))
        (self.sup / "messages.jsonl").write_text(json.dumps({"from": "thelos", "to": "operator", "text": "held",
            "file": "held.md", "ts": "2020-01-01T00:00:00", "push_due": "2020-01-01T07:15:00"}) + "\n")
        claim = self.sup / "push-digest.json"
        for dispatch in (lambda: pushes.digest(webpush.notify), web_alerts.forward_new):
            with self.subTest(dispatch=dispatch), mock.patch.object(pushes, "QUIET_HOURS", None), \
                    mock.patch.object(messages, "appended", return_value=([], 0)), \
                    mock.patch.object(apns, "_wake") as wake:
                claim.unlink(missing_ok=True)
                queue = webpush._queue()
                webpush.log.reset_mock()
                with mock.patch.object(apns, "TEAM_ID", ""):
                    reason = apns.configured()
                    self.assertEqual(webpush.subscriptions(), {})
                    self.assertEqual(dispatch(), 0)
                    self.assertEqual(json.loads(claim.read_text()) if claim.exists() else [], [])
                    self.assertEqual(webpush._queue(), queue)
                    wake.assert_not_called()
                    if dispatch is not web_alerts.forward_new:
                        webpush.log.assert_called_once_with(f"web: {reason}")
                self.assertIsNone(apns.configured())
                self.assertEqual(dispatch(), 1)
                self.assertEqual(json.loads(claim.read_text()), ["held.md"])
                self.assertEqual(webpush.take("phone")[0]["title"], "SHAFT digest (1)")
                self.assertEqual(dispatch(), 0)

    def test_unconfigured_apns_keeps_web_push_recipients(self):
        for device in ("phone", "both"):
            self.register(device)
        (self.sup / "web" / "devices.json").write_text(json.dumps({d: {} for d in ("phone", "both", "web")}))
        for device in ("both", "web"):
            webpush.subscribe(device, {"endpoint": "https://web.push.apple.com/test"})
        webpush.log.reset_mock()
        with mock.patch.object(apns.os, "access", return_value=False), \
                mock.patch.object(webpush, "_wake") as wake, mock.patch.object(apns, "_wake") as native:
            reason = apns.configured()
            self.assertEqual(webpush.notify(EVENT), ["both", "web"])
        self.assertEqual(set(wake.call_args[0][0]), {"both", "web"})
        self.assertEqual(native.call_args[0][0], {})
        webpush.log.assert_called_once_with(f"web: {reason}")
        self.assertNotIn("phone", webpush._queue())
        for device in ("both", "web"):
            self.assertEqual(webpush.take(device), [EVENT])

    def test_missing_config_turns_push_off_and_logs_without_dropping_token(self):
        self.register()
        self.key.unlink()
        self.assertIn(str(self.key), apns.configured())
        self.assertEqual(apns.status("phone")["on"], False)
        with mock.patch.object(apns.subprocess, "run") as curl:
            said = apns._send_one("phone", apns.devices()["phone"], EVENT)
        curl.assert_not_called()
        self.assertIn("missing readable APNs key file", said)
        self.assertIn("phone", apns.devices())
        for value in ("KEY123", "TEAM456", TOPIC, str(self.key)):
            self.assertIn(value, apns.log.call_args[0][0])
        with mock.patch.object(apns, "KEY_ID", ""), mock.patch.object(apns, "TEAM_ID", ""), \
                mock.patch.object(apns, "TOPIC", ""):
            for name in ("SHAFT_APNS_KEY_ID", "SHAFT_APNS_TEAM_ID", "SHAFT_APNS_TOPIC"):
                self.assertIn(name, apns.configured())

    def test_register_validation_status_and_unregister(self):
        self.assertIsNone(apns.configured())
        self.assertEqual(apns.status("phone"), {"on": False, "env": None, "reason": "device has no APNs token"})
        for token, env, topic, platform in (("", "sandbox", TOPIC, "ios"), ("xyz", "sandbox", TOPIC, "ios"),
                ("a", "sandbox", TOPIC, "ios"), ("ab cd", "sandbox", TOPIC, "ios"),
                (TOKEN, "dev", TOPIC, "ios"), (TOKEN, "sandbox", "other", "ios"),
                (TOKEN, "sandbox", TOPIC, "android")):
            with self.subTest(token=token, env=env, topic=topic, platform=platform), self.assertRaises(ValueError):
                apns.register("phone", token, env, topic, platform)
        self.assertEqual(apns.devices(), {})
        apns.register("phone", TOKEN.upper(), "sandbox", TOPIC, "macos")
        self.assertEqual(apns.devices()["phone"]["token"], TOKEN)
        self.assertIn("registered", apns.devices()["phone"])
        self.assertEqual(apns._path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(apns.status("phone"), {"on": True, "env": "sandbox", "reason": ""})
        self.assertTrue(apns.unregister("phone"))
        self.assertFalse(apns.unregister("phone"))

    def test_notify_reaches_both_transports_once_per_device_and_skips_watchers_and_unpaired(self):
        (self.sup / "web" / "devices.json").write_text(json.dumps({d: {} for d in ("web", "phone", "both", "watch")}))
        for device in ("phone", "both", "watch", "revoked"):
            self.register(device)
        for device in ("web", "both", "watch", "revoked"):
            webpush.subscribe(device, {"endpoint": "https://web.push.apple.com/test"})
        with mock.patch.object(webpush, "_wake") as web, mock.patch.object(apns, "_wake") as native:
            self.assertEqual(webpush.notify(EVENT, skip={"watch"}), ["both", "phone", "web"])
        self.assertEqual(set(web.call_args[0][0]), {"web", "both"})
        self.assertEqual(set(native.call_args[0][0]), {"phone", "both"})
        self.assertEqual(native.call_args[0][1], EVENT)
        for device in ("both", "phone", "web"):
            self.assertEqual(webpush.take(device), [EVENT])
        self.assertEqual(webpush.take("watch"), [webpush.NOTHING])
        self.assertNotIn("revoked", apns.devices())
        self.assertNotIn("revoked", webpush.subscriptions())
        self.assertNotIn("revoked", webpush._queue())

    def test_native_wakes_retry_independently_and_stop_on_success_or_dead_token(self):
        subs = {device: self.register(device) for device in ("phone", "mac")}
        attempts = {device: 0 for device in subs}
        threads = []
        def send(device, sub, event):
            attempts[device] += 1
            return "not sent: offline" if attempts[device] < 3 else "200"
        make_thread = threading.Thread
        def capture(**kwargs):
            thread = make_thread(**kwargs)
            threads.append(thread)
            return thread
        with mock.patch.object(web, "devices", return_value={d: {} for d in subs}), \
                mock.patch.object(apns, "_send_one", side_effect=send), mock.patch.object(apns.time, "sleep") as sleep, \
                mock.patch.object(apns.threading, "Thread", side_effect=capture):
            apns._wake(subs, EVENT)
            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
        self.assertEqual(attempts, {"phone": 3, "mac": 3})
        self.assertCountEqual([call.args[0] for call in sleep.call_args_list], [0, 0, *webpush.RETRY_S, *webpush.RETRY_S])
        with mock.patch.object(web, "devices", return_value={"phone": {}}), \
                mock.patch.object(apns, "_send_one", return_value="410: removed") as send, \
                mock.patch.object(apns.time, "sleep"):
            threads.clear()
            with mock.patch.object(apns.threading, "Thread", side_effect=capture):
                apns._wake({"phone": subs["phone"]}, EVENT)
                threads[0].join(timeout=2)
            self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
