"""/app/v1 on loopback (SHAFT_WEB_LOCAL): the Mac app on the same Mac reaches the web service at 127.0.0.1 with a paired
device's bearer and no Tailscale. Only /app/v1 gains that path: the pages, the pairing route and every request with
the switch off keep today's refusals, and the identity of a local request is the device's recorded login, never a
header any local process could forge."""
from __future__ import annotations

import json
from unittest import mock

from supervisor import core, web, web_chat
from supervisor.tests.test_app_api import AppTest
from supervisor.tests.test_web import HOST, ME


class LocalTest(AppTest):
    def setUp(self):
        super().setUp()
        self.local = f"127.0.0.1:{web.PORT}"
        self.on = mock.patch.object(web, "LOCAL", True)
        self.on.start()
        self.addCleanup(self.on.stop)
        self.dev, self.sec = web.add_device("mac-app", "operator@local", "macos")

    def local_api(self, path, method="GET", bearer="device", login=None, host=None, **kw):
        bearer = f"Bearer {self.dev}.{self.sec}" if bearer == "device" else bearer
        return self.api(path, method, bearer=bearer, login=login, host=host or self.local, **kw)


class LoopbackAppTest(LocalTest):
    def test_local_app_request_with_bearer_and_no_login_is_served(self):
        r = self.local_api("/app/v1/instances")
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual([i["name"] for i in json.loads(r.body)["instances"]], ["wisp"])

    def test_localhost_name_is_a_local_host_too(self):
        self.assertEqual(web.local_hosts(), {f"127.0.0.1:{web.PORT}", f"localhost:{web.PORT}"})
        self.assertEqual(self.local_api("/app/v1/instances", host=f"localhost:{web.PORT}").status, 200)

    def test_local_without_bearer_is_unpaired_even_with_a_forged_login(self):
        r = self.local_api("/app/v1/instances", bearer=False, login=ME)
        self.error(r, 401, "unpaired")

    def test_local_with_a_wrong_secret_is_unpaired(self):
        r = self.local_api("/app/v1/instances", bearer=f"Bearer {self.dev}.not-the-secret", login=ME)
        self.error(r, 401, "unpaired")

    def test_local_identity_is_the_devices_login_not_the_header(self):
        with mock.patch.object(web_chat, "send", wraps=web_chat.send) as send:
            r = self.send_local(login="evil@x")
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], "operator@local")
        self.assertNotEqual(send.call_args.args[1], "evil@x")

    def send_local(self, login=None):
        body = b"".join(f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                        for k, v in {"text": "hi", "mid": "m-local", "mode": "now"}.items()) + b"--B--\r\n"
        return self.local_api("/app/v1/chat/wisp/send", "POST", body=body, login=login,
                              headers={"content-type": "multipart/form-data; boundary=B"})

    def test_local_pages_and_pair_are_refused(self):
        page = self.req("/", host=self.local)                    # even with the operator's login header
        self.assertEqual(page.status, 403)
        self.assertNotIn(b"wisp", page.body)
        self.assertEqual(self.req("/", host=self.local, login=None).status, 403)
        link = web.new_pairing("other")
        r = self.local_api("/app/v1/pair", "POST", data={"link": link, "platform": "macos"}, bearer=False)
        err = self.error(r, 403, "local_pair")
        self.assertIn("shaft web pair --local", err["message"])
        self.assertIsNotNone(web._pairing(link.split("t=")[1]), "a refused local pair must not spend the link")

    def test_tailnet_requests_still_need_the_login_with_the_switch_on(self):
        self.assertEqual(self.api("/app/v1/instances", host=HOST).status, 200)
        self.error(self.api("/app/v1/instances", login=None), 403, "login")
        self.error(self.api("/app/v1/instances", login="evil@x"), 403, "login")

    def test_local_switch_only_opens_the_loopback_hosts(self):
        self.error(self.local_api("/app/v1/instances", host="127.0.0.1:9999"), 403, "login")
        self.error(self.local_api("/app/v1/instances", host="evil.example:8785"), 403, "login")


class LocalOffTest(LocalTest):
    def setUp(self):
        super().setUp()
        self.off = mock.patch.object(web, "LOCAL", False)
        self.off.start()
        self.addCleanup(self.off.stop)

    def test_local_off_keeps_todays_refusals(self):
        self.error(self.local_api("/app/v1/instances"), 403, "login")
        self.error(self.local_api("/app/v1/instances", login=ME), 403, "host")

    def test_tailnet_requests_are_unchanged(self):
        self.assertEqual(self.api("/app/v1/instances", host=HOST).status, 200)
        self.error(self.api("/app/v1/instances", login=None), 403, "login")


class OnOffTest(LocalTest):
    def test_on_off_parses_exactly_on_and_off(self):
        self.assertIs(core.on_off("on"), True)
        self.assertIs(core.on_off("off"), False)
        for bad in ("", "yes", "1", "true", "ON "):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                core.on_off(bad)

    def test_knob_is_registered_cli_only_for_the_web_service(self):
        k = core.KNOBS["SHAFT_WEB_LOCAL"]
        self.assertEqual((k.default, k.services, k.web), ("off", ("web",), False))
        self.assertIs(k.parse, core.on_off)


class AddDeviceTest(LocalTest):
    def test_add_device_records_a_paired_device_with_its_login(self):
        rec = web.devices()[self.dev]
        self.assertEqual((rec["name"], rec["login"], rec["platform"]), ("mac-app", "operator@local", "macos"))
        self.assertTrue(web.paired_device(self.dev, self.sec))
        self.assertNotIn(self.sec, json.dumps(web.devices()))          # only the hash is kept

    def test_pairing_through_a_link_still_works(self):
        dev, _ = self.pair_app("ipad-app", "ios")
        self.assertEqual(web.devices()[dev]["login"], ME)
