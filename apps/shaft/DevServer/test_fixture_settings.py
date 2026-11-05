"""Run directly with Python 3.12: fixture settings and services through the real /app/v1 dispatcher, no socket or app."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fixture_server as fixture


class FixtureSettingsTest(unittest.TestCase):
    def test_settings_and_services_stay_in_the_fixture(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory)
            previous = os.environ.get("SHAFT_SUPERVISOR_DIR")
            try:
                web, chat, messages, push = fixture.setup(base, 8899)
                from supervisor import actions, autonomy, core, opconfig, version
                for method in ("run", "Popen"):
                    mock.patch(f"subprocess.{method}", side_effect=AssertionError("fixture spawned a process")).start()

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

                path = "/app/v1/settings/thelos"
                settings = api(path)
                self.assertEqual(settings["operator_name"]["value"], "Operator")
                for field, value in (("operator_name", "Fixture operator"), ("wake_interval", 0),
                                     ("effort", "high"), ("model", settings["model"]["choices"][0]["model"]),
                                     ("cloud_cap", 2.5), ("channel", "telegram"), ("cloud_cap", None)):
                    settings = api(path, "POST", {"field": field, "value": value, "expect": settings[field]["value"]})["settings"]
                    self.assertEqual(settings[field]["value"], value)
                self.assertEqual(core.instance_root("thelos"), base / "instances" / "thelos")
                self.assertEqual(opconfig.instance_root("thelos"), base / "instances" / "thelos")
                self.assertEqual(core._tool_settings("thelos")["channel"], "telegram")
                self.assertNotIn("cloud_daily_usd", core._tool_settings("thelos"))
                self.assertTrue(api(path + "/pause", "POST")["settings"]["paused"])
                self.assertFalse(api(path + "/resume", "POST")["settings"]["paused"])

                services = api("/app/v1/services")["services"]
                self.assertEqual([s["pid"] for s in services], [os.getpid(), os.getpid()])
                self.assertTrue(all(s["running"] for s in services))
                fixture.act(base, web, chat, messages, push, "services", ["busy", "thelos replying"])
                api("/app/v1/services/api/restart", "POST", expected=409)
                self.assertEqual(version.service("api", stale=False)["pid"], os.getpid())
                pending = api("/app/v1/services/scheduler/restart", "POST")["services"][1]["drain"]
                self.assertEqual(pending["waiting"], ["thelos replying"])
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
