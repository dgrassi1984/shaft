"""Consult transport, reservations and broker integration. All upstream HTTP is mocked."""
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy, broker, consult, core, delete
from supervisor.tests import test_limits


class ConsultTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-consult-")
        self.addCleanup(tmp.cleanup)
        self.sup = Path(tmp.name)
        for module, attr, value in ((consult, "SUP_DIR", self.sup), (consult, "USAGE", self.sup / "consult_usage.jsonl"),
                                    (core, "SUP_DIR", self.sup), (delete, "SUP_DIR", self.sup)):
            self.enterContext(mock.patch.object(module, attr, value))
        self.enterContext(mock.patch.object(autonomy, "next_wake", return_value=None))
        self.urlopen = self.enterContext(mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected HTTP")))
        self.model = consult.MODELS[0]
        self.body = {"model": self.model, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 100}
        self.catalogue = {"fetched": time.time(), "models": [
            {"id": self.model.removeprefix("openrouter/"), "pricing": {"prompt": "0.000004", "completion": "0.00002"}}],
            "endpoints": [{"model_id": self.model.removeprefix("openrouter/"), "tag": "bedrock",
                           "pricing": {"prompt": "0.000004", "completion": "0.00002"}}]}
        self.cache()
        self.b = broker.Broker("wisp", self.sup, {"OPENROUTER_API_KEY": "fake"}, self.sup / "broker.log",
                               root=self.sup / "copy", share=False, network=False)
        self.b.run_kind, self.b.run_started = "live", "2026-10-27T12:00:00"

    def cache(self):
        (self.sup / "consult_catalogue.json").write_text(json.dumps(self.catalogue))

    def reply(self, cost=0.001):
        usage = {"prompt_tokens": 10, "completion_tokens": 20}
        if cost is not None:
            usage["cost"] = cost
        return {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}], "usage": usage}

    def complete(self, body=None):
        return consult.complete("wisp", body or self.body, self.b.secrets, "plugin", "live", self.b.run_started)

    def upstream(self, data):
        self.urlopen.side_effect = lambda *a, **k: io.BytesIO(json.dumps(data).encode())

    def rows(self):
        return [json.loads(l) for l in consult.USAGE.read_text().splitlines()]

    def http(self, body=None, token=None, caller="plugin"):
        # Exercise the actual HTTP handler without binding sockets or making a local network request.
        with mock.patch.object(broker, "ThreadingHTTPServer") as server, \
                mock.patch.object(broker.threading, "Thread"), mock.patch.object(self.b.egress, "start"):
            self.b.start()
        handler = object.__new__(server.call_args.args[1])
        handler.path = "/llm/consult/v1/chat/completions"
        raw = json.dumps(body or self.body).encode()
        handler.headers = {"X-Shaft-Token": self.b.token if token is None else token,
                           "Content-Length": str(len(raw)), "X-Shaft-Caller": caller}
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        handler.do_POST()
        return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())

    def test_menu_refuses_non_menu(self):
        with self.assertRaisesRegex(consult.ConsultError, "not on the consult menu"):
            self.complete({**self.body, "model": "openrouter/other/model"})
        self.urlopen.assert_not_called()

    def test_no_zdr_provider_refuses_with_reason(self):
        self.catalogue["endpoints"] = []
        self.cache()
        self.assertFalse(consult.menu("wisp")[0]["available"])
        with self.assertRaisesRegex(consult.ConsultError, "no qualifying zero-retention provider"):
            self.complete()

    def test_unknown_price_refused(self):
        self.catalogue["models"][0]["pricing"].pop("prompt")
        self.cache()
        with self.assertRaisesRegex(consult.ConsultError, "unknown price"):
            self.complete()

    def test_stream_refused(self):
        with self.assertRaisesRegex(consult.ConsultError, "stream"):
            self.complete({**self.body, "stream": True})

    def test_job_refused(self):
        self.b.model_allowed = False
        self.assertEqual(self.http()[0], 403)
        self.urlopen.assert_not_called()

    def test_replay_and_battery_refused(self):
        for billed in ("wisp@trial:t-1", "battery-wisp"):
            self.b.usage_name = billed
            status, reply = self.http()
            self.assertEqual(status, 403)
            self.assertIn("replay and battery", reply["error"]["message"])
        self.urlopen.assert_not_called()

    def test_token_authentication(self):
        self.assertEqual(self.http(token="bad")[0], 403)
        self.urlopen.assert_not_called()

    def test_cost_provider_policy_and_http_reply(self):
        self.upstream(self.reply(0.00123))
        status, result = self.http({**self.body, "provider": {"zdr": False}}, caller="x" * 70)
        self.assertEqual(status, 200)
        self.assertEqual(result["shaft"], {"usd": 0.00123, "spent_usd": 0.00123, "reserved_usd": 0, "cap_usd": 3})
        sent = json.loads(self.urlopen.call_args.args[0].data)
        self.assertEqual(sent["provider"], consult.PROVIDER)
        self.assertEqual(sent["usage"], {"include": True})
        row = self.rows()[0]
        self.assertEqual(row["caller"], "x" * 60)
        self.assertEqual((row["run"], row["run_started"], row["usd"]), ("live", self.b.run_started, 0.00123))
        self.assertEqual(set(row), {"ts", "instance", "model", "caller", "run", "run_started", "input_tokens",
                                    "output_tokens", "usd", "reserved_usd", "seconds", "error"})
        self.assertEqual(consult._reservations(), [])

    def test_missing_cost_records_worst_case_and_caps_tokens(self):
        self.upstream(self.reply(None))
        self.complete({**self.body, "max_tokens": 99999})
        row = self.rows()[0]
        self.assertEqual(row["usd"], row["reserved_usd"])
        sent = json.loads(self.urlopen.call_args.args[0].data)
        prompt = json.dumps({k: v for k, v in sent.items() if k not in ("provider", "usage")}, ensure_ascii=False)
        self.assertAlmostEqual(row["usd"], len(prompt) / 3 * 0.000004 + 8000 * 0.00002)
        self.assertEqual(json.loads(self.urlopen.call_args.args[0].data)["max_tokens"], 8000)

    def test_upstream_error_releases_reservation_and_records_worst_case(self):
        """A 5xx or timeout may have generated (and been billed): the worst case is charged. An outright refusal
        (4xx, e.g. no qualifying provider) generated nothing and charges nothing — it once cost the full worst case."""
        with mock.patch.object(consult.cloudllm.time, "sleep"):
            self.urlopen.side_effect = urllib.error.HTTPError("https://fake", 502, "bad", {}, io.BytesIO(b"bad"))
            with self.assertRaisesRegex(consult.ConsultError, "OpenRouter consult failed"):
                self.complete()
            self.assertEqual(consult._reservations(), [])
            row = self.rows()[0]
            self.assertEqual(row["usd"], row["reserved_usd"])
            self.assertIsNotNone(row["error"])
            self.urlopen.side_effect = urllib.error.HTTPError("https://fake", 404, "no", {}, io.BytesIO(b"no endpoints"))
            with self.assertRaisesRegex(consult.ConsultError, "OpenRouter consult failed"):
                self.complete()
        refused = self.rows()[1]
        self.assertEqual((refused["usd"], consult._reservations()), (0.0, []))
        self.assertGreater(refused["reserved_usd"], 0)

    def test_the_menu_shows_the_list_price_and_reserves_the_dearest_provider(self):
        """Live, 2026-10-27: the menu showed glm-5.3 at its dearest zero-retention provider (7.43/8.14 per million,
        like Opus) while it lists at 0.07/1.40 and a real call cost $0.000024."""
        cheap = self.model.removeprefix("openrouter/")
        self.catalogue["models"][0]["pricing"] = {"prompt": "0.00000007", "completion": "0.0000014"}
        self.catalogue["endpoints"] = [
            {"model_id": cheap, "tag": "wafer", "pricing": {"prompt": "0.00000007", "completion": "0.0000014"}},
            {"model_id": cheap, "tag": "dear", "pricing": {"prompt": "0.000007429", "completion": "0.000008144"}}]
        self.cache()
        item = consult.menu("wisp")[0]
        self.assertEqual((round(item["in_per_m"], 4), round(item["out_per_m"], 4)), (0.07, 1.4))
        self.assertEqual((round(item["reserve_in_per_m"], 3), round(item["reserve_out_per_m"], 3)), (7.429, 8.144))
        self.upstream(self.reply(None))                 # no cost back: the worst case is what is recorded
        self.complete()
        sent = json.loads(self.urlopen.call_args.args[0].data)
        prompt = json.dumps({k: v for k, v in sent.items() if k not in ("provider", "usage")}, ensure_ascii=False)
        self.assertAlmostEqual(self.rows()[0]["reserved_usd"], (len(prompt) / 3 * 7.429 + 100 * 8.144) / 1e6)

    def test_non_text_input_the_worst_case_cannot_see_is_refused(self):
        """Grok's review (2026-10-27, blocker): a hundred image URLs reserved cents and could bill dollars."""
        image = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
        for messages in ([{"role": "user", "content": [{"type": "text", "text": "what is this?"}, image]}],
                         [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "x"}}]}],
                         [{"role": "user", "content": [{"type": "file", "file": {"file_data": "x"}}]}], []):
            with self.assertRaises(consult.ConsultError):
                self.complete({**self.body, "messages": messages})
        with self.assertRaisesRegex(consult.ConsultError, "text only"):
            self.complete({**self.body, "modalities": ["text", "image"]})
        self.urlopen.assert_not_called()
        self.upstream(self.reply())
        self.complete({**self.body, "messages": [{"role": "user", "content": [{"type": "text", "text": "ok"}]}]})

    def test_a_thinking_budget_is_clamped_to_max_tokens(self):
        self.upstream(self.reply())
        self.complete({**self.body, "reasoning": {"effort": "high", "max_tokens": 50000}})
        self.assertEqual(json.loads(self.urlopen.call_args.args[0].data)["reasoning"],
                         {"effort": "high", "max_tokens": 100})

    def test_one_attempt_only_and_a_zero_cost_is_not_free(self):
        """Grok's review: the chat path's three 600 s attempts outlived the 30-minute reservation, and an earlier
        attempt's generation went unbilled; a reported cost of 0 recorded $0."""
        self.urlopen.side_effect = urllib.error.HTTPError("https://fake", 503, "busy", {}, io.BytesIO(b"busy"))
        with self.assertRaises(consult.ConsultError):
            self.complete()
        self.assertEqual(self.urlopen.call_count, 1)
        self.assertEqual(self.urlopen.call_args.kwargs.get("timeout"), consult.SEND_TIMEOUT_S)
        self.assertLess(consult.SEND_TIMEOUT_S, consult.RESERVATION_S)
        self.upstream(self.reply(0))
        self.complete()
        row = self.rows()[-1]
        self.assertEqual(row["usd"], row["reserved_usd"])

    def test_a_cost_above_its_reservation_is_recorded_and_logged(self):
        self.upstream(self.reply(5.0))
        with mock.patch.object(core, "log") as logged:
            self.complete()
        self.assertEqual(self.rows()[-1]["usd"], 5.0)
        self.assertIn("over its", logged.call_args.args[0])

    def test_paid_extras_the_reservation_cannot_see_are_refused(self):
        for extra in ({"plugins": [{"id": "web"}]}, {"web_search_options": {"search_context_size": "high"}}):
            with self.assertRaisesRegex(consult.ConsultError, "no OpenRouter plugins"):
                self.complete({**self.body, **extra})
        self.urlopen.assert_not_called()

    def test_concurrent_reservation_blocks_second_call(self):
        core._update_tool_settings("wisp", consult_daily_usd=0.004)
        entered, release = threading.Event(), threading.Event()
        def blocking(*a, **k):
            entered.set()
            if not release.wait(5):
                raise AssertionError("upstream never released")
            return io.BytesIO(json.dumps(self.reply()).encode())
        self.urlopen.side_effect = blocking
        results = []
        def first():
            try:
                results.append(self.complete())
            except Exception as e:
                results.append(e)
        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(consult.ConsultError, "spent .*reserved .*worst case .*operator") as caught:
                self.complete()
            self.assertEqual(caught.exception.status, 429)
            self.assertEqual(self.urlopen.call_count, 1)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], dict)
        self.assertEqual(consult._reservations(), [])

    def test_spent_and_stale_reservations(self):
        self.upstream(self.reply(0.002))
        self.complete()
        core._update_tool_settings("wisp", consult_daily_usd=0.003)
        with self.assertRaisesRegex(consult.ConsultError, "spent"):
            self.complete()
        core._update_tool_settings("wisp", consult_daily_usd=0.005)
        consult._save_reservations([{"id": "old", "instance": "wisp", "usd": 10, "created": time.time() - 1801}])
        self.complete()
        self.assertEqual(consult.spent_today("wisp"), 0.004)
        self.assertEqual(consult.spent_today("other"), 0)

    def test_envelope(self):
        env = self.b.envelope()["consult"]
        self.assertEqual((env["spent_usd"], env["reserved_usd"], env["cap_usd"]), (0, 0, 3))
        self.assertEqual(env["models"][0]["model"], self.model)
        self.assertTrue(env["models"][0]["available"])
        self.b._envelope = None
        self.b.usage_name = "battery-wisp"
        self.assertIsNone(self.b.envelope()["consult"])

    def test_the_envelope_never_waits_on_the_catalogue(self):
        """The template asks for the envelope with a 2 s timeout; a day-old catalogue made it fetch OpenRouter (30 s
        timeout) inline. It now answers from the cache — or says prices are not loaded — and refreshes behind."""
        (self.sup / "consult_catalogue.json").unlink()
        started = threading.Event()

        def slow(*a, **k):
            started.set()
            raise OSError("offline")
        self.urlopen.side_effect = slow
        with mock.patch.object(core, "log"):
            models = self.b.envelope()["consult"]["models"]
        self.assertTrue(all(not m["available"] and "not loaded yet" in m["why"] for m in models))
        self.assertTrue(started.wait(5))          # the refresh ran, in the background
        stale = {**self.catalogue, "fetched": time.time() - 2 * 86400}
        (self.sup / "consult_catalogue.json").write_text(json.dumps(stale))
        self.assertTrue(consult.menu("wisp", fetch=False)[0]["available"])   # stale prices beat none

    def test_delete_rows_and_reservations(self):
        self.upstream(self.reply())
        self.complete()
        other = {**self.rows()[0], "instance": "other"}
        with consult.USAGE.open("a") as f:
            f.write(json.dumps(other) + "\n")
        consult._save_reservations([{"id": n, "instance": n, "usd": 1, "created": time.time()}
                                    for n in ("wisp", "other")])
        for edit in delete.shared_edits("wisp"):
            delete._edit(edit, apply=True)
        self.assertEqual(self.rows(), [other])
        self.assertEqual([r["instance"] for r in consult._reservations()], ["other"])

    def test_catalogue_refresh_and_stale_cache(self):
        self.catalogue["fetched"] = time.time() - 86401
        self.cache()
        self.urlopen.side_effect = OSError("offline")
        self.assertTrue(consult.menu("wisp")[0]["available"])
        self.assertEqual(self.urlopen.call_count, 1)
        self.urlopen.side_effect = [io.BytesIO(json.dumps({"data": self.catalogue["models"]}).encode()),
                                  io.BytesIO(json.dumps({"data": self.catalogue["endpoints"]}).encode())]
        self.assertTrue(consult.menu("wisp")[0]["available"])
        self.assertGreater(json.loads((self.sup / "consult_catalogue.json").read_text())["fetched"], time.time() - 60)
        self.urlopen.reset_mock()
        consult.menu("wisp")
        self.urlopen.assert_not_called()

    def test_no_cache_refuses(self):
        (self.sup / "consult_catalogue.json").unlink()
        self.urlopen.side_effect = OSError("offline")
        with self.assertRaisesRegex(consult.ConsultError, "no cached prices"):
            self.complete()

    def test_record_own_last_seven_days(self):
        self.upstream(self.reply())
        self.complete()
        row = self.rows()[0]
        with consult.USAGE.open("a") as f:
            for changes in ({"instance": "other", "caller": "private"},
                            {"ts": (datetime.now() - timedelta(days=8)).isoformat(), "caller": "too-old"}):
                f.write(json.dumps({**row, **changes}) + "\n")
        text = self.b._record("consults")
        self.assertIn(self.model, text)
        self.assertIn("caller=plugin", text)
        self.assertNotIn("private", text)
        self.assertNotIn("too-old", text)


class ConsultLimitsTest(test_limits.LimitsTest):
    def test_consult_cap_sets_shows_resets(self):
        self.assertEqual(self.cli("wisp", "consult-cap", "5")[0], 0)
        self.assertEqual(core._tool_settings("wisp")["consult_daily_usd"], 5)
        self.assertIn("consult cap $5.00 (yours)", self.cli("wisp")[1])
        self.assertEqual(self.cli("wisp", "consult-cap", "off")[0], 0)
        self.assertNotIn("consult_daily_usd", core._tool_settings("wisp"))
        self.assertEqual(self.cli("wisp", "consult-cap", "-1")[0], 2)
