"""Run directly with Python 3.12: fixture settings and services through the real /app/v1 dispatcher, no socket or app."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fixture_server as fixture


class FixtureSettingsTest(unittest.TestCase):
    def test_fixture_refuses_repository_root_and_ancestors_before_writing(self):
        for base in (fixture.ROOT, *fixture.ROOT.parents):
            with self.subTest(base=base), mock.patch.object(Path, "mkdir", side_effect=AssertionError("wrote before refusal")), self.assertRaises(SystemExit):
                fixture.setup(base, 8899)

    def test_fixture_html_avoids_launchctl_and_real_secrets_and_settings_stay_isolated(self):
        with tempfile.TemporaryDirectory(prefix="shaft-fixture-test-") as directory:
            base = Path(directory).resolve()   # setup resolves it too: /var is /private/var on macOS
            previous = os.environ.get("SHAFT_SUPERVISOR_DIR")
            try:
                web, chat, messages, push = fixture.setup(base, 8899)
                from supervisor import actions, autonomy, core, dashboard, opconfig, version, web_config, web_settings
                for method in ("run", "Popen"):
                    mock.patch(f"subprocess.{method}", side_effect=AssertionError("fixture spawned a process")).start()

                # These HTML dependencies must never query launchctl or load PROJECT/.env.
                self.assertTrue(dashboard._scheduler_running())
                self.assertEqual(core.secrets_env(), {})
                self.assertEqual(web_settings.secrets_env(), {})
                self.assertTrue(next(s for s in dashboard.services() if s["name"] == "scheduler")["running"])
                html = str(web_settings.page_body({"settings": [], "file": "fixture"}, [], []))
                self.assertIn("none", html)

                def request(path, method="GET", data=None, token=None):
                    headers = {"host": "127.0.0.1:8899"}
                    if token:
                        headers["authorization"] = f"Bearer {token}"
                    if data is not None:
                        headers["content-type"] = "application/json"
                    response = web.respond(web.Request(method, path, headers,
                                                       json.dumps(data).encode() if data is not None else b""))
                    return response.status, json.loads(response.body)

                status, paired = request("/app/v1/pair", "POST", {"link": web.new_pairing("fixture-app"), "platform": "ios"})
                self.assertEqual(status, 200, paired)
                token = f"{paired['device']}.{paired['secret']}"

                def api(path, method="GET", data=None, expected=200):
                    status, body = request(path, method, data, token)
                    self.assertEqual(status, expected, body)
                    return body

                path = "/app/v1/settings/tundra"
                settings = api(path)
                self.assertEqual(settings["operator_name"]["value"], "Operator")
                for field, value in (("operator_name", "Fixture operator"), ("wake_interval", 0),
                                     ("effort", "high"), ("model", settings["model"]["choices"][0]["model"]),
                                     ("cloud_cap", 2.5), ("channel", "telegram"), ("cloud_cap", None)):
                    settings = api(path, "POST", {"field": field, "value": value, "expect": settings[field]["value"]})["settings"]
                    self.assertEqual(settings[field]["value"], value)
                self.assertEqual(core.instance_root("tundra"), base / "instances" / "tundra")
                self.assertEqual(opconfig.instance_root("tundra"), base / "instances" / "tundra")
                self.assertEqual(core._tool_settings("tundra")["channel"], "telegram")
                self.assertNotIn("cloud_daily_usd", core._tool_settings("tundra"))
                # HTML access-page actions still need the limits/channel command stubs.
                self.assertEqual(web_config.run("limit", "tundra", {"limit": "cloud-cap", "value": "3.5"})[0], 200)
                self.assertEqual(core._tool_settings("tundra")["cloud_daily_usd"], 3.5)
                self.assertEqual(web_config.run("channel", "tundra", {"value": "web"})[0], 200)
                self.assertEqual(core._tool_settings("tundra")["channel"], "web")
                self.assertTrue(api(path + "/pause", "POST")["settings"]["paused"])
                self.assertFalse(api(path + "/resume", "POST")["settings"]["paused"])

                services = api("/app/v1/services")["services"]
                self.assertEqual([s["pid"] for s in services], [os.getpid(), os.getpid()])
                self.assertTrue(all(s["running"] for s in services))
                fixture.act(base, web, chat, messages, push, "services", ["busy", "tundra replying"])
                api("/app/v1/services/api/restart", "POST", expected=409)
                self.assertEqual(version.service("api", stale=False)["pid"], os.getpid())
                pending = api("/app/v1/services/scheduler/restart", "POST")["services"][1]["drain"]
                self.assertEqual(pending["waiting"], ["tundra replying"])
                at = autonomy.restart_request()["at"]
                api("/app/v1/services/scheduler/restart", "POST")
                self.assertEqual(autonomy.restart_request()["at"], at)
                api("/app/v1/services/scheduler/cancel", "POST")
                self.assertIsNone(autonomy.restart_request())
                self.assertEqual(api("/app/v1/services")["services"][1]["drain"]["state"], "cancelled")
                fixture.act(base, web, chat, messages, push, "services", ["busy"])
                restarted = api("/app/v1/services/api/restart", "POST")["services"][0]
                self.assertTrue(restarted["running"])
                self.assertNotEqual(restarted["pid"], os.getpid())
                api("/app/v1/services/scheduler/restart", "POST")
                fixture.act(base, web, chat, messages, push, "services", ["drain-end"])
                scheduler = api("/app/v1/services")["services"][1]
                self.assertTrue(scheduler["running"])
                self.assertNotEqual(scheduler["pid"], os.getpid())
                self.assertEqual(scheduler["drain"]["state"], "drained")
                with self.assertRaises(ValueError):
                    actions.run_now(["unknown-fixture-verb"])
                with self.assertRaises(AssertionError):
                    autonomy._launchctl("print", "anything")
            finally:
                mock.patch.stopall()
                if previous is None:
                    os.environ.pop("SHAFT_SUPERVISOR_DIR", None)
                else:
                    os.environ["SHAFT_SUPERVISOR_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
