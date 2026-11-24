"""Run directly with Python 3.12: the setup wizard's routes (docs/app-api.md §18) on the fixture server, through the real
/app/v1 dispatcher, no socket or app: what a developer drives the Mac app's wizard against."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import fixture_server as fixture

KEY = "sk-or-v1-fixture-never-written-91c3"


class FixtureSetupTest(unittest.TestCase):
    def test_lists_the_names_of_saved_server_keys(self):
        with tempfile.TemporaryDirectory(prefix="shaft-fixture-test-") as directory, mock.patch.dict(os.environ):
            self.addCleanup(mock.patch.stopall)
            web, *_ = fixture.setup(Path(directory), 8899)
            device, secret = web.add_device("mac-app", "operator@local", "macos")
            headers = {"host": "127.0.0.1:8899", "authorization": f"Bearer {device}.{secret}",
                       "content-type": "application/json"}

            def api(method, path, body=b""):
                response = web.respond(web.Request(method, f"/app/v1/setup/{path}", headers, body))
                self.assertEqual(response.status, 200, response.body)
                return json.loads(response.body)

            self.assertEqual(api("GET", "secrets"), {"names": []})
            for name, value in (("SCRAPEBADGER_API_KEY", KEY), ("HF_TOKEN", "hf_example")):
                self.assertEqual(api("POST", "secret", json.dumps({"name": name, "value": value}).encode()),
                                 {"said": f"{name} saved"})
            self.assertEqual(api("GET", "secrets"), {"names": ["SCRAPEBADGER_API_KEY", "HF_TOKEN"]})

    def test_the_wizard_from_model_to_hello(self):
        with tempfile.TemporaryDirectory(prefix="shaft-fixture-test-") as directory:
            base = Path(directory).resolve()   # setup resolves it too: /var is /private/var on macOS
            previous = os.environ.get("SHAFT_SUPERVISOR_DIR")
            try:
                web, chat, messages, push = fixture.setup(base, 8899)
                from supervisor import actions, core
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

                status, paired = request("/app/v1/pair", "POST", {"link": web.new_pairing("mac-app"), "platform": "macos"})
                self.assertEqual(status, 200, paired)
                token = f"{paired['device']}.{paired['secret']}"

                def api(path, method="GET", data=None, expected=200):
                    status, body = request(path, method, data, token)
                    self.assertEqual(status, expected, body)
                    return body

                # Model: OpenRouter is off without its key, and its check fails with the provider's words.
                used = api("/app/v1/setup/provider", "POST", {"provider": "openrouter"})
                self.assertEqual(used["covers"], [])
                self.assertIn("OPENROUTER_API_KEY", used["off"][0]["why"])
                self.assertEqual(used["model"], "openrouter/xiaomi/mimo-v2.6-pro")
                check = api("/app/v1/setup/provider/check", "POST", {"provider": "openrouter"})
                self.assertFalse(check["ok"])
                self.assertIn("401", check["models"][0]["error"])
                # The key: kept by name only, never its value anywhere in the fixture's folder.
                self.assertEqual(api("/app/v1/setup/secret", "POST", {"name": "OPENROUTER_API_KEY", "value": KEY}),
                                 {"said": "OPENROUTER_API_KEY saved"})
                for path in base.rglob("*"):
                    if path.is_file():
                        self.assertNotIn(KEY, path.read_text(errors="ignore"), str(path))
                used = api("/app/v1/setup/provider", "POST", {"provider": "openrouter"})
                self.assertEqual(used["off"], [])
                self.assertIn("instances", used["covers"])
                self.assertTrue(api("/app/v1/setup/provider/check", "POST", {"provider": "openrouter"})["ok"])
                # Custom: the verb's own refusals, and a model whose name says fail fails its check.
                err = api("/app/v1/setup/provider", "POST", {"provider": "custom", "model": "m"}, expected=409)
                self.assertIn("--price-in", err["error"]["message"])
                used = api("/app/v1/setup/provider", "POST", {
                    "provider": "custom", "model": "big", "base_url": "http://127.0.0.1:9/v1", "price_in": 0,
                    "price_out": 0, "judge_model": "fail-judge", "judge_price_in": 0, "judge_price_out": 0})
                self.assertEqual(used["model"], "custom/big")
                self.assertEqual([o["function"] for o in used["off"]], ["describe"])
                rows = api("/app/v1/setup/provider/check", "POST", {"provider": "custom"})["models"]
                self.assertEqual([r["ok"] for r in rows], [True, False])
                api("/app/v1/setup/provider", "POST", {"provider": "codex"})
                self.assertTrue(api("/app/v1/setup/provider/check", "POST", {"provider": "codex"})["ok"])

                # Your instance: the drives, then the instance made here, paused until the first reply.
                drives = api("/app/v1/setup/drives")["drives"]
                self.assertEqual([d["id"] for d in drives], ["assistant", "research", "builder", "improve"])
                self.assertEqual(drives[3]["text"], fixture.TEMPLATE_DRIVE)
                made = api("/app/v1/setup/instance", "POST", {"name": "ledger", "operator": "Ada",
                                                              "model": "codex/gpt-6-astra", "drive": drives[0]["text"],
                                                              "autonomy": "own"})
                self.assertEqual(made, {"instance": "ledger", "done": ["new", "config", "drive"], "paused": True})
                self.assertEqual((base / "instances" / "ledger" / "drive.md").read_text().strip(), drives[0]["text"])
                # Paused until its first reply, it is listed under `paused`, not in the sidebar's `instances` (v0.64.0).
                listed = api("/app/v1/instances")
                self.assertIn("ledger", [i["name"] for i in listed["paused"]])
                self.assertNotIn("ledger", [i["name"] for i in listed["instances"]])
                settings = api("/app/v1/settings/ledger")
                self.assertTrue(settings["paused"])
                self.assertEqual(settings["operator_name"]["value"], "Ada")
                self.assertEqual(settings["model"]["value"], "codex/gpt-6-astra")
                # Asked again while it waits for its first reply (the app quit before it heard): finished, no second new.
                again = api("/app/v1/setup/instance", "POST", {"name": "ledger", "operator": "Ada",
                                                               "model": "codex/gpt-6-astra", "drive": drives[0]["text"],
                                                               "autonomy": "own"})
                self.assertEqual(again["done"], ["new", "config", "drive"])
                err = api("/app/v1/setup/instance", "POST", {"name": "wisp", "operator": "Ada", "model": "m",
                                                             "drive": "d", "autonomy": "own"}, expected=409)
                self.assertIn("already exists", err["error"]["message"])

                # Access (the wizard's non-Google rows): nothing granted yet; then a folder, the browser, web search
                # and a Telegram bot, each answered with the fresh access.
                none = api("/app/v1/access/ledger")
                self.assertEqual((none["folder"], none["browser"], none["web_search"], none["telegram"]),
                                 (None, {"installed": None, "run": True}, False, False))
                outside = Path(tempfile.mkdtemp())          # SHAFT's own tree may not be shared: this one may
                self.addCleanup(outside.rmdir)
                granted = api("/app/v1/access/ledger", "POST", {"grant": "folder", "action": "grant", "what": str(outside)})
                self.assertEqual(granted["folder"], str(outside.resolve()))
                err = api("/app/v1/access/ledger", "POST", {"grant": "folder", "action": "grant",
                                                             "what": str(base / "nowhere")}, expected=409)
                self.assertIn("is not a directory", err["error"]["message"])
                self.assertEqual(api("/app/v1/access/ledger", "POST", {"grant": "browser", "action": "grant"})["browser"],
                                 {"installed": "1.58.0-fixture", "run": True})
                self.assertEqual(api("/app/v1/setup/secret", "POST", {"name": "SCRAPEBADGER_API_KEY", "value": KEY}),
                                 {"said": "SCRAPEBADGER_API_KEY saved"})
                err = api("/app/v1/setup/telegram", "POST", {"name": "ledger", "token": "bad-token"}, expected=409)
                self.assertIn("Telegram refused the token", err["error"]["message"])
                self.assertIn("ledger", api("/app/v1/setup/telegram", "POST",
                                            {"name": "ledger", "token": "123456:fixture-token-never-kept"})["said"])
                after = api("/app/v1/access/ledger")
                self.assertEqual((after["web_search"], after["telegram"]), (True, True))
                # Telegram on, then a pairing code the fixture "sends" to the bot: the status shows it land.
                status = api("/app/v1/setup/telegram/status/ledger")
                self.assertEqual((status["token"], status["bot"], status["service"], status["paired"]),
                                 (True, "ledger_fixture_bot", False, False))
                self.assertIn("installed", api("/app/v1/setup/telegram/service", "POST", {})["said"])
                mock.patch.object(fixture, "PAIR_AFTER_S", 0.2).start()     # stopped with the others below
                pair = api("/app/v1/setup/telegram/pair/ledger", "POST", {})
                self.assertRegex(pair["code"], r"^[0-9A-F]{8}$")
                self.assertTrue(pair["pairing"])
                for _ in range(100):
                    status = api("/app/v1/setup/telegram/status/ledger")
                    if status["paired"]:
                        break
                    time.sleep(0.05)
                self.assertEqual((status["paired"], status["account"], status["service"]),
                                 (True, "fixture_operator", True))
                self.assertIsNone(api("/app/v1/setup/telegram/pair/ledger", "POST", {})["code"])
                for path in base.rglob("*"):
                    if path.is_file():
                        self.assertNotIn("fixture-token-never-kept", path.read_text(errors="ignore"), str(path))

                # Google: no account until `act google` (the app's own local login); then the pickers' lists and the
                # grants, each answered with the fresh access; a refused label stops with the verb's words.
                err = api("/app/v1/google/google/labels", expected=409)
                self.assertIn("not logged in", err["error"]["message"])
                fixture.act(base, web, chat, messages, push, "google", [])
                self.assertEqual(api("/app/v1/google/google/labels")["labels"], fixture.GOOGLE_LABELS)
                cals = api("/app/v1/google/google/calendars")["calendars"]
                self.assertEqual([(c["id"], c["primary"]) for c in cals],
                                 [(fixture.ME, True), ("team@group.calendar.google.com", False)])
                granted = api("/app/v1/access/ledger", "POST", {"grant": "mail", "action": "grant", "alias": "google",
                                                                "what": "Receipts"})
                self.assertEqual(granted["mail"], [{"account": "google", "label": "Receipts", "draft": False}])
                self.assertEqual([a["alias"] for a in granted["google_accounts"]], ["google"])
                err = api("/app/v1/access/ledger", "POST", {"grant": "mail", "action": "grant", "alias": "google",
                                                            "what": "Refused here"}, expected=409)
                self.assertIn("no label 'Refused here'", err["error"]["message"])
                granted = api("/app/v1/access/ledger", "POST", {"grant": "calendar", "action": "grant", "alias": "google",
                                                                "what": fixture.ME, "writable": True})
                self.assertEqual(granted["calendar"], [{"account": "google", "calendar": fixture.ME,
                                                        "name": fixture.ME, "write": True}])
                granted = api("/app/v1/access/ledger", "POST", {"grant": "drive", "action": "grant", "alias": "google",
                                                                "all": True})
                self.assertEqual(granted["drive"], [{"account": "google", "reach": "all"}])
                granted = api("/app/v1/access/ledger", "POST", {"grant": "chat", "action": "grant", "alias": "google"})
                self.assertEqual(granted["chat"], [{"account": "google"}])
                # The wizard's downgrades and revocations: write off keeps the read; Drive back to its own files.
                granted = api("/app/v1/access/ledger", "POST", {"grant": "calendar", "action": "revoke",
                                                                "alias": "google", "what": fixture.ME, "writable": True})
                self.assertEqual(granted["calendar"], [{"account": "google", "calendar": fixture.ME,
                                                        "name": fixture.ME, "write": False}])
                granted = api("/app/v1/access/ledger", "POST", {"grant": "drive", "action": "grant", "alias": "google"})
                self.assertEqual(granted["drive"], [{"account": "google", "reach": "own"}])
                granted = api("/app/v1/access/ledger", "POST", {"grant": "chat", "action": "revoke", "alias": "google"})
                self.assertEqual(granted["chat"], [])
                granted = api("/app/v1/access/ledger", "POST", {"grant": "mail", "action": "revoke", "alias": "google",
                                                                "what": "Receipts"})
                self.assertEqual(granted["mail"], [])

                # How it works alone: the real first-reply verb on the fixture's marker, then a budget.
                self.assertEqual(api("/app/v1/setup/autonomy/ledger", "POST", {"then": "stay"}),
                                 {"instance": "ledger", "then": "stay"})
                marker = base / "sup" / "first-reply-ledger"
                self.assertEqual(json.loads(marker.read_text())["then"], "stay")
                saved = api("/app/v1/settings/ledger", "POST", {"field": "cloud_cap", "value": 4.0, "expect": None})
                self.assertEqual(saved["settings"]["cloud_cap"]["value"], 4.0)
                api("/app/v1/setup/autonomy/wisp", "POST", {"then": "stay"}, expected=409)   # never waited for one

                # Say hello: the hidden opening, answered in Main.
                hello = api("/app/v1/setup/hello/ledger", "POST", {})
                self.assertEqual(hello["said"], "ledger is writing its welcome")
                self.assertTrue(hello["chat"]["id"].startswith("web-"))
                again = api("/app/v1/setup/hello/ledger", "POST", {})        # Try again: that chat, nothing asked twice
                self.assertEqual(again, {"said": "ledger was asked for its welcome already", "chat": hello["chat"]})
                api("/app/v1/setup/autonomy/nobody", "POST", {"then": "stay"}, expected=404)

                with self.assertRaises(ValueError):
                    actions.run_json(["unknown-fixture-verb"])
                self.assertEqual(core.TEMPLATE, base / "template")
            finally:
                mock.patch.stopall()
                if previous is None:
                    os.environ.pop("SHAFT_SUPERVISOR_DIR", None)
                else:
                    os.environ["SHAFT_SUPERVISOR_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
