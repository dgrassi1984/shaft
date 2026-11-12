"""/app/v1 on loopback (SHAFT_WEB_LOCAL): the Mac app on the same Mac reaches the web service at 127.0.0.1 with a paired
device's bearer and no Tailscale. Only /app/v1 gains that path: the pages, the pairing route and every request with
the switch off keep today's refusals, and the identity of a local request is the device's recorded login, never a
header any local process could forge."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import api, core, web, web_chat
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

    def test_version_names_the_checkout_only_on_loopback(self):
        """§19, fix round 1: the Mac app updates the server only from the checkout it runs from, so a loopback answer
        names it; the tailnet's never does (no path on the Mac leaves it)."""
        local = json.loads(self.local_api("/app/v1/version").body)
        self.assertEqual(local["project"], str(core.PROJECT.resolve()))
        self.assertEqual(local["api"], 1)
        tailnet = self.get("/app/v1/version")
        self.assertNotIn("project", tailnet)

    def test_localhost_name_is_a_local_host_too(self):
        self.assertEqual(web.local_hosts(), {f"127.0.0.1:{web.PORT}", f"localhost:{web.PORT}"})
        self.assertEqual(self.local_api("/app/v1/instances", host=f"localhost:{web.PORT}").status, 200)

    def test_local_without_bearer_is_unpaired(self):
        self.error(self.local_api("/app/v1/instances", bearer=False), 401, "unpaired")

    def test_local_with_a_wrong_secret_is_unpaired(self):
        r = self.local_api("/app/v1/instances", bearer=f"Bearer {self.dev}.not-the-secret")
        self.error(r, 401, "unpaired")

    def test_local_host_with_a_proxy_or_login_header_takes_the_tailnet_path(self):
        # tailscale serve forwards a tailnet request with whatever Host it names, adding these headers: such a request
        # must pass the tailnet login and Host checks even when it names the loopback host with a valid bearer
        for header in web.PROXY_HEADERS:
            with self.subTest(header=header):
                r = self.local_api("/app/v1/instances", headers={header.title(): "100.64.0.9"})
                self.error(r, 403, "login")
        self.error(self.local_api("/app/v1/instances", login="evil@x"), 403, "login")       # a forged login header
        self.error(self.local_api("/app/v1/instances", headers={"X-Forwarded-For": "100.64.0.9"}), 403, "login")
        # an allowed login still has to name the tailnet host
        self.error(self.local_api("/app/v1/instances", login=ME), 403, "host")
        r = self.send_local(login="evil@x")
        self.error(r, 403, "login")

    def test_local_identity_is_the_devices_login(self):
        with mock.patch.object(web_chat, "send", wraps=web_chat.send) as send:
            r = self.send_local()
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], "operator@local")

    def test_a_device_unpaired_after_its_bearer_was_checked_is_unpaired_not_a_500(self):
        # the race: the bearer passed web.paired_device, then `shaft web unpair` removed the record before _login read it
        def checked_then_unpaired(dev, secret):
            web.unpair(dev)
            return True
        with mock.patch.object(web, "paired_device", side_effect=checked_then_unpaired), \
                mock.patch.object(web_chat, "send", wraps=web_chat.send) as send:
            r = self.send_local()
        err = self.error(r, 401, "unpaired")
        self.assertIn("shaft web pair --local", err["message"])
        self.assertEqual(send.call_count, 0)
        self.assertNotIn(self.dev, web.devices())

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


class LocalInstallTest(LocalTest):
    """`shaft api on --local`, `shaft web on --local` and `shaft web pair --local`: the Mac app's setup installs the API
    and web services and pairs itself on 127.0.0.1 without Tailscale. Tailscale and launchctl are never run: a call to
    api._tailscale fails the test, autonomy.service is a stub."""

    def setUp(self):
        super().setUp()
        self.project = Path(tempfile.mkdtemp(prefix="shaft-local-project-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        self.services = []
        for p in (mock.patch.object(core, "PROJECT", self.project),
                  mock.patch.object(core, "SETTINGS_FILE", self.sup / "settings.env"),
                  mock.patch.dict(os.environ, {}),
                  mock.patch.object(api, "_tailscale", side_effect=AssertionError("tailscale must not run")),
                  mock.patch("supervisor.autonomy.service", side_effect=lambda *a: self.services.append(a) or 0),
                  mock.patch("supervisor.autonomy.running", return_value=False),
                  mock.patch.object(web, "allowed_logins", return_value={ME, "operator@local"})):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("SHAFT_API_LOGINS", None)
        os.environ.pop("SHAFT_WEB_LOCAL", None)

    def cli(self, *argv):
        from supervisor import __main__ as m
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = m.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    # -- shaft api on --local ----------------------------------------------------------------------------------------
    def test_api_on_local_sets_the_local_login_and_never_calls_tailscale(self):
        code, out, err = self.cli("api", "on", "--local")
        self.assertEqual(code, 0, out + err)
        env = core.secrets_env()
        self.assertEqual(env["SHAFT_API_LOGINS"], "operator@local")
        self.assertEqual(api.LOCAL_LOGIN, "operator@local")
        self.assertTrue(env["SHAFT_API_KEY"].startswith("sk-shaft-"))
        self.assertTrue(env["SHAFT_INBOX_KEY"].startswith("sk-shaft-inbox-"))
        self.assertEqual(self.services, [("api", "on")])
        self.assertNotIn(env["SHAFT_API_KEY"], out + err)               # the key is shown by `shaft api key` only

    def test_api_on_local_keeps_existing_logins(self):
        (self.project / ".env").write_text("SHAFT_API_LOGINS=me@x\nSHAFT_API_KEY=k\nSHAFT_INBOX_KEY=i\n")
        self.assertEqual(self.cli("api", "on", "--local")[0], 0)
        self.assertEqual(core.secrets_env()["SHAFT_API_LOGINS"], "me@x,operator@local")
        self.assertEqual(self.cli("api", "on", "--local")[0], 0)          # again: no duplicate
        self.assertEqual(core.secrets_env()["SHAFT_API_LOGINS"], "me@x,operator@local")
        self.assertEqual(core.secrets_env()["SHAFT_API_KEY"], "k")       # existing keys are kept

    def test_api_on_tailnet_adds_its_login_next_to_the_local_one(self):
        (self.project / ".env").write_text("SHAFT_API_LOGINS=operator@local\nSHAFT_API_KEY=k\nSHAFT_INBOX_KEY=i\n")
        status = json.dumps({"Self": {"UserID": 7, "DNSName": "mac.example.ts.net."},
                             "User": {"7": {"LoginName": "me@x"}}})

        def tailscale(*a):
            return subprocess.CompletedProcess(a, 0, stdout=status if "status" in a else "", stderr="")
        with mock.patch.object(api, "_tailscale", side_effect=tailscale), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(api.command("on"), 0)
        self.assertEqual(core.secrets_env()["SHAFT_API_LOGINS"], "operator@local,me@x")

    def test_api_on_tailnet_with_only_the_local_login_and_no_tailscale_refuses(self):
        # plain `api on` is the tailnet install: operator@local is no tailnet login, so it cannot go ahead without one
        (self.project / ".env").write_text("SHAFT_API_LOGINS=operator@local\nSHAFT_API_KEY=k\nSHAFT_INBOX_KEY=i\n")
        with mock.patch.object(api, "_tailscale", side_effect=lambda *a: subprocess.CompletedProcess(
                a, 0, stdout="{}", stderr="")), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(api.command("on"), 1)
        self.assertIn("Tailscale login", out.getvalue())
        self.assertEqual(self.services, [])
        self.assertEqual(core.secrets_env()["SHAFT_API_LOGINS"], "operator@local")

    def test_local_flag_on_another_api_action_is_a_usage_error(self):
        self.assertEqual(self.cli("api", "status", "--local")[0], 2)
        self.assertEqual(self.services, [])

    # -- shaft web on --local ----------------------------------------------------------------------------------------
    def test_web_on_local_sets_the_knob_and_never_calls_tailscale(self):
        code, out, err = self.cli("web", "on", "--local")
        self.assertEqual(code, 0, out + err)
        self.assertIn("SHAFT_WEB_LOCAL=on", (self.sup / "settings.env").read_text().splitlines())
        self.assertEqual(self.services, [("web", "on")])
        self.assertFalse((self.sup / "web" / "hosts.json").exists())
        self.assertIn(f"http://127.0.0.1:{web.PORT}", out)
        self.assertIn("shaft web pair --local", out)

    def test_web_on_local_without_the_local_login_names_api_on_local(self):
        # a tailnet login alone is not enough: the app's chats reach the API as operator@local (web_chat's relay)
        for logins in (set(), {ME}):
            with self.subTest(logins=logins), mock.patch.object(web, "allowed_logins", return_value=logins):
                code, out, err = self.cli("web", "on", "--local")
            self.assertEqual(code, 1)
            self.assertIn("shaft api on --local", out + err)
        self.assertEqual(self.services, [])
        self.assertFalse((self.sup / "settings.env").exists())

    def test_pair_local_without_the_local_login_names_api_on_local(self):
        before = set(web.devices())
        with mock.patch.object(web, "allowed_logins", return_value={ME}):
            code, out, _ = self.cli("web", "pair", "--local", "--json")
        self.assertEqual(code, 1)
        self.assertIn("shaft api on --local", json.loads(out)["error"])
        self.assertEqual(set(web.devices()), before)

    # -- shaft web pair --local --------------------------------------------------------------------------------------
    def test_pair_local_json_prints_a_working_bearer(self):
        code, out, err = self.cli("web", "pair", "--local", "--name", "my-mac", "--json")
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, out)
        got = json.loads(lines[0])
        self.assertEqual(got["base_url"], f"http://127.0.0.1:{web.PORT}")
        self.assertEqual((got["name"], got["login"]), ("my-mac", "operator@local"))
        r = self.local_api("/app/v1/instances", bearer=f"Bearer {got['device']}.{got['secret']}",
                           host=got["base_url"].removeprefix("http://"))
        self.assertEqual(r.status, 200, r.body)
        rec = web.devices()[got["device"]]
        self.assertEqual((rec["name"], rec["login"], rec["platform"]), ("my-mac", "operator@local", "macos"))
        stored = (self.sup / "web" / "devices.json").read_text()
        self.assertIn(rec["hash"], stored)
        self.assertNotIn(got["secret"], stored)
        log = self.sup / "supervisor.log"
        self.assertNotIn(got["secret"], log.read_text() if log.exists() else "")

    def test_pair_local_refuses_when_local_is_off(self):
        before = set(web.devices())
        with mock.patch.object(web, "LOCAL", False):
            code, out, err = self.cli("web", "pair", "--local", "--name", "my-mac", "--json")
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, out)
        self.assertIn("shaft web on --local", json.loads(lines[0])["error"])
        self.assertEqual(set(web.devices()), before)

    def test_pair_local_storage_failure_is_a_json_refusal_not_a_traceback(self):
        # review finding: a PermissionError writing devices.json escaped _pair_local, leaving stdout empty under --json
        with mock.patch.object(web, "add_device", side_effect=PermissionError(13, "Permission denied", "devices.json")):
            code, out, err = self.cli("web", "pair", "--local", "--name", "my-mac", "--json")
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, out)
        got = json.loads(lines[0])
        self.assertIs(got["ok"], False)
        self.assertIn("Permission denied", got["error"])
        self.assertNotIn("Traceback", out + err)

    def test_pair_local_unreadable_env_is_a_json_refusal(self):
        with mock.patch.object(web, "allowed_logins", side_effect=PermissionError(13, "Permission denied", ".env")):
            code, out, err = self.cli("web", "pair", "--local", "--json")
        self.assertEqual(code, 1)
        self.assertIn("Permission denied", json.loads(out)["error"])
        self.assertNotIn("Traceback", out + err)

    def test_web_on_local_settings_write_failure_is_a_refusal(self):
        from supervisor import settings
        for exc in (PermissionError(13, "Permission denied", "settings.env"), settings.Refused("SHAFT_WEB_LOCAL bad")):
            with self.subTest(exc=type(exc).__name__), mock.patch.object(settings, "set_values", side_effect=exc):
                code, out, err = self.cli("web", "on", "--local")
            self.assertEqual(code, 1)
            self.assertIn(str(exc), err)
            self.assertNotIn("Traceback", out + err)
        self.assertEqual(self.services, [])

    def test_pair_local_without_json_prints_the_fields_as_lines(self):
        code, out, _ = self.cli("web", "pair", "--local", "--name", "my-mac")
        self.assertEqual(code, 0, out)
        fields = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
        self.assertEqual(fields["base_url"], f"http://127.0.0.1:{web.PORT}")
        self.assertTrue(web.paired_device(fields["device"], fields["secret"]))
        self.assertEqual(fields["login"], "operator@local")

    # -- Tailscale added later (the Mac app's "Use SHAFT from iPhone/iPad") ------------------------------------------
    def fake_tailscale(self, state="Running", code=0, login="me@x"):
        """api._tailscale as a Mac with Tailscale in `state` answers it; every call is recorded, none reaches the Mac."""
        self.ts_calls = []
        status = json.dumps({"BackendState": state, "Self": {"UserID": 7, "DNSName": "mac.example.ts.net."},
                             "User": {"7": {"LoginName": login}}})

        def tailscale(*a):
            self.ts_calls.append(a)
            if code:
                return subprocess.CompletedProcess(a, code, stdout="", stderr="tailscale is not installed")
            return subprocess.CompletedProcess(a, 0, stdout=status if "status" in a else "", stderr="")
        return mock.patch.object(api, "_tailscale", side_effect=tailscale)

    def only_json(self, out):
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, out)               # the Mac app reads exactly one line
        return json.loads(lines[0])

    def test_web_on_tailnet_after_local_keeps_local_on_and_adds_the_login(self):
        self.assertEqual(self.cli("api", "on", "--local")[0], 0)
        self.assertEqual(self.cli("web", "on", "--local")[0], 0)
        with self.fake_tailscale():
            code, out, err = self.cli("api", "on", "--json")
            self.assertEqual((code, self.only_json(out)), (0, {"ok": True}), out + err)
            code, out, err = self.cli("web", "on", "--json")
            self.assertEqual((code, self.only_json(out)), (0, {"ok": True}), out + err)
        # the tailnet login joined the local one; neither the login nor the loopback switch went away
        self.assertEqual(core.secrets_env()["SHAFT_API_LOGINS"], "operator@local,me@x")
        self.assertIn("SHAFT_WEB_LOCAL=on", (self.sup / "settings.env").read_text().splitlines())
        self.assertEqual(json.loads((self.sup / "web" / "hosts.json").read_text())["hosts"],
                         [f"mac.example.ts.net:{web.HTTPS_PORT}"])
        self.assertEqual(self.services, [("api", "on"), ("web", "on")] * 2)
        serves = [c for c in self.ts_calls if c[0] == "serve"]
        self.assertEqual(len(serves), 2)                    # the API's and the web UI's tailnet mapping
        # and the Mac's own pairing still works afterwards
        code, out, _ = self.cli("web", "pair", "--local", "--name", "my-mac", "--json")
        self.assertEqual(code, 0, out)

    def test_api_on_json_says_why_tailscale_is_missing_or_signed_out(self):
        (self.project / ".env").write_text("SHAFT_API_LOGINS=operator@local\nSHAFT_API_KEY=k\nSHAFT_INBOX_KEY=i\n")
        cases = [(dict(code=127), "not installed"), (dict(state="NeedsLogin"), "not signed in"),
                 (dict(state="Stopped"), "switched off")]
        for kw, expect in cases:
            with self.subTest(expect=expect), self.fake_tailscale(**kw):
                code, out, err = self.cli("api", "on", "--json")
            got = self.only_json(out)
            self.assertEqual(code, 1)
            self.assertIs(got["ok"], False)
            self.assertIn(expect, got["error"])
            self.assertNotIn("shaft ", got["error"])        # no terminal command in what the app shows
        self.assertEqual(self.services, [])                 # nothing was installed or restarted
        self.assertEqual(core.secrets_env()["SHAFT_API_LOGINS"], "operator@local")

    def test_web_on_json_says_why_tailscale_is_missing_and_keeps_local_on(self):
        self.assertEqual(self.cli("api", "on", "--local")[0], 0)
        self.assertEqual(self.cli("web", "on", "--local")[0], 0)
        self.services.clear()
        with self.fake_tailscale(code=127):
            code, out, err = self.cli("web", "on", "--json")
        got = self.only_json(out)
        self.assertEqual(code, 1)
        self.assertIn("not installed", got["error"])
        self.assertEqual(self.services, [])
        self.assertIn("SHAFT_WEB_LOCAL=on", (self.sup / "settings.env").read_text().splitlines())
        self.assertFalse((self.sup / "web" / "hosts.json").exists())

    def test_web_pair_json_prints_the_tailnet_link(self):
        with mock.patch.object(web, "allowed_hosts", return_value={HOST}):
            code, out, err = self.cli("web", "pair", "--name", "iphone", "--json")
        got = self.only_json(out)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(got["ok"])
        self.assertTrue(got["link"].startswith(f"https://{HOST}/pair?t="), got)
        self.assertEqual(got["expires_minutes"], web.PAIR_MINUTES)
        self.assertEqual(set(got), {"ok", "link", "expires_minutes"})

    def test_web_pair_json_without_a_tailnet_name_is_a_refusal_not_a_bad_link(self):
        with mock.patch.object(web, "allowed_hosts", return_value=set()):
            code, out, _ = self.cli("web", "pair", "--name", "iphone", "--json")
        got = self.only_json(out)
        self.assertEqual(code, 1)
        self.assertIs(got["ok"], False)
        self.assertNotIn("not running", got.get("link", ""))

    def test_api_on_json_refused_for_a_running_reply_is_one_json_line_without_terminal_words(self):
        with mock.patch("supervisor.autonomy.running", return_value=True), \
                mock.patch("supervisor.actions.service_guard", return_value="2 replies are running."):
            code, out, err = self.cli("api", "on", "--json")
        got = self.only_json(out)
        self.assertEqual(code, 1)
        self.assertIn("2 replies are running", got["error"])
        self.assertNotIn("--force", got["error"])
        self.assertEqual(self.services, [])

    def test_json_goes_with_the_tailnet_on_only(self):
        self.assertEqual(self.cli("api", "status", "--json")[0], 2)
        self.assertEqual(self.cli("web", "on", "--local", "--json")[0], 2)
        self.assertEqual(self.services, [])

    def test_json_and_local_on_other_web_actions_are_usage_errors(self):
        self.assertEqual(self.cli("web", "status", "--json")[0], 2)
        self.assertEqual(self.cli("web", "off", "--local")[0], 2)
        self.assertEqual(self.services, [])


class TailscaleMissingTest(unittest.TestCase):
    """Local-only Macs may have no Tailscale at all: `shaft api off`/`shaft web off` still stop their service."""

    def test_tailscale_not_installed_is_a_failed_call_not_a_crash(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("no such file")):
            r = api._tailscale("status", "--json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not installed", r.stderr)

    def test_off_without_tailscale_still_stops_the_services(self):
        calls = []
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("no such file")), \
                mock.patch("supervisor.autonomy.service", side_effect=lambda *a: calls.append(a) or 0), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(api.command("off"), 0)
            self.assertEqual(web.command("off"), 0)
        self.assertEqual(calls, [("api", "off"), ("web", "off")])


class LocalServeTest(LocalTest):
    """web.serve() in local-only mode (no tailnet name recorded) does not say Tailscale is broken."""

    def serve_logs(self):
        said = []
        server = mock.MagicMock()
        with mock.patch.object(web, "ThreadingHTTPServer", return_value=server), \
                mock.patch.object(web, "allowed_hosts", return_value=set()), \
                mock.patch.object(web, "log", side_effect=said.append), \
                mock.patch("supervisor.web_alerts.start"), mock.patch.object(web_chat, "reap"), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(web.serve(), 0)
        server.serve_forever.assert_called_once()
        return " ".join(said), out.getvalue()

    def test_local_only_serve_says_local_not_tailscale_is_missing(self):
        said, out = self.serve_logs()
        self.assertNotIn("tailscale cannot be asked", said)
        self.assertIn("local", said)
        self.assertNotIn("tailscale is not running", out)
        self.assertIn(f"http://127.0.0.1:{web.PORT}/app/v1", out)

    def test_tailnet_serve_without_a_name_still_warns(self):
        with mock.patch.object(web, "LOCAL", False):
            said, _ = self.serve_logs()
        self.assertIn("tailscale cannot be asked", said)
