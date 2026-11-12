"""The custom/<model> route: any OpenAI-compatible endpoint, with prices the operator declares, so the daily dollar
cap counts every call. A loopback http.server stands in for the endpoint; no test reaches a real one."""
from __future__ import annotations

import io
import json
import math
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supervisor import broker, cloudllm, core, sandbox

MODEL = "custom/my-model"
KEY = "sk-custom-canary"
MESSAGES = [{"role": "system", "content": "You are terse."}, {"role": "user", "content": "Say hello to the world."}]


class Stub:
    """A loopback endpoint: records each request, answers with `reply` (JSON) or `sse` (stream lines)."""

    def __init__(self):
        self.requests, self.reply, self.sse, self.status = [], None, None, 200
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                stub.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
                if stub.status != 200:
                    self.send_response(stub.status)
                    self.end_headers()
                    self.wfile.write(b'{"error": "nope"}')
                    return
                if stub.sse is not None:
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write("".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n"
                                             for c in stub.sse).encode())
                    return
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(stub.reply).encode())

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def chat(content="hi there", usage=None, **extra):
    out = {"id": "x", "object": "chat.completion", "model": "my-model",
           "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}
    if usage is not None:
        out["usage"] = usage
    return {**out, **extra}


class Case(unittest.TestCase):
    PRICES = {"my-model": (2.0, 8.0)}      # $ per million tokens, in and out

    def setUp(self):
        self.stub = Stub()
        self.addCleanup(self.stub.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-custom-"))
        self.usage = self.tmp / "cloud_usage.jsonl"
        for target, name, value in ((cloudllm, "USAGE", self.usage), (cloudllm, "CUSTOM_BASE_URL", self.stub.url + "/"),
                                    (cloudllm, "CUSTOM_PRICES", self.PRICES), (cloudllm, "CLOUD_MODELS", [MODEL]),
                                    (cloudllm, "DAILY_USD", 1.0), (core, "SUP_DIR", self.tmp)):
            self.enterContext(mock.patch.object(target, name, value))
        self.enterContext(mock.patch.object(cloudllm.time, "sleep"))
        self.enterContext(mock.patch.object(cloudllm.ledger, "record_charge"))

    def body(self, **extra):
        return {"model": MODEL, "messages": MESSAGES, **extra}

    def rows(self):
        return [json.loads(line) for line in self.usage.read_text().splitlines()] if self.usage.exists() else []

    def complete(self, body=None, secrets=None):
        return cloudllm.complete("demo", body or self.body(), {"SHAFT_CUSTOM_API_KEY": KEY} if secrets is None
                                 else secrets, {})


class CallTest(Case):
    def test_custom_model_is_called_at_the_base_url_with_the_key_and_no_prefix(self):
        self.stub.reply = chat("hello", {"prompt_tokens": 10, "completion_tokens": 2})
        out = self.complete(self.body(temperature=0.2))
        (req,) = self.stub.requests
        self.assertEqual(req["path"], "/v1/chat/completions")      # the trailing slash of the base is stripped
        self.assertEqual(req["headers"]["authorization"] if "authorization" in req["headers"]
                         else req["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(req["body"]["model"], "my-model")          # no custom/ prefix on the wire
        self.assertEqual(req["body"]["messages"], MESSAGES)
        self.assertEqual(req["body"]["temperature"], 0.2)
        self.assertEqual(out["choices"][0]["message"]["content"], "hello")

    def test_cost_is_tokens_times_declared_price_and_counts_toward_the_cap(self):
        self.stub.reply = chat(usage={"prompt_tokens": 1000, "completion_tokens": 500})
        self.complete()
        (row,) = self.rows()
        self.assertEqual((row["model"], row["input_tokens"], row["output_tokens"]), (MODEL, 1000, 500))
        self.assertAlmostEqual(row["usd"], (1000 * 2.0 + 500 * 8.0) / 1e6)
        self.assertFalse(row["unpriced"])
        self.assertNotIn("estimated", {k for k, v in row.items() if v})
        # the cap counts it: with the day's cap equal to what was spent, the next call is refused before any request
        with mock.patch.object(cloudllm, "DAILY_USD", row["usd"]):
            with self.assertRaises(cloudllm.CloudError) as ctx:
                self.complete()
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(len(self.stub.requests), 1)

    def test_an_unpriced_custom_model_is_refused_before_any_call(self):
        ghost = "custom/ghost"
        with mock.patch.object(cloudllm, "CLOUD_MODELS", [MODEL, ghost]):
            with self.assertRaises(cloudllm.CloudError) as ctx:
                self.complete(self.body(model=ghost))
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("SHAFT_CUSTOM_PRICES", str(ctx.exception))
        self.assertEqual(self.stub.requests, [])
        self.assertEqual(self.rows(), [])
        # and it can never be configured as a model: only a priced custom/ name passes the validators
        for validate in (core.cloud_model, core.instance_model):
            self.assertEqual(validate(MODEL), MODEL)
            with self.assertRaises(ValueError) as bad:
                validate(ghost)
            self.assertIn("SHAFT_CUSTOM_PRICES", str(bad.exception))
        self.assertEqual(core.cloud_models(f"claude-haiku-5-5,{MODEL}"), ["claude-haiku-5-5", MODEL])

    def test_zero_price_is_allowed_and_labelled_unpriced_by_choice(self):
        with mock.patch.object(cloudllm, "CUSTOM_PRICES", {"my-model": (0.0, 0.0)}):
            self.assertEqual(core.cloud_model(MODEL), MODEL)
            self.stub.reply = chat(usage={"prompt_tokens": 1000, "completion_tokens": 500})
            self.complete()
        (row,) = self.rows()
        self.assertEqual(row["usd"], 0.0)                 # a priced zero, not None
        self.assertIs(row["unpriced"], False)             # distinguishable from "no price known"

    def test_missing_usage_reserves_the_worst_case(self):
        # Review Focus 4: an endpoint that returns no usage must never be recorded as $0.
        chars = len(json.dumps(MESSAGES))
        self.stub.reply = chat("some answer")
        self.complete(self.body(max_tokens=200))
        (row,) = self.rows()
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (math.ceil(chars / 3), 200))
        self.assertAlmostEqual(row["usd"], (math.ceil(chars / 3) * 2.0 + 200 * 8.0) / 1e6)
        self.assertGreater(row["usd"], 0)
        self.assertIs(row["estimated"], True)
        # no max_tokens either: the output is the reply text's chars / 3
        self.usage.unlink()
        self.stub.reply = chat("x" * 31)
        self.complete()
        (row,) = self.rows()
        self.assertEqual(row["output_tokens"], math.ceil(31 / 3))
        self.assertIs(row["estimated"], True)
        self.assertGreater(row["usd"], 0)

    def test_the_endpoint_failing_is_a_recorded_502_naming_the_endpoint(self):
        self.stub.status = 400
        with self.assertRaises(cloudllm.CloudError) as ctx:
            self.complete()
        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn(MODEL, str(ctx.exception))

    def test_missing_base_url_or_key_names_the_setting(self):
        for patch, secrets, needle in ((("CUSTOM_BASE_URL", ""), None, "SHAFT_CUSTOM_BASE_URL"),
                                       (("CUSTOM_BASE_URL", self.stub.url), {}, "SHAFT_CUSTOM_API_KEY")):
            with mock.patch.object(cloudllm, *patch), self.assertRaises(cloudllm.CloudError) as ctx:
                self.complete(secrets=secrets)
            self.assertEqual(ctx.exception.status, 503)
            self.assertIn(needle, str(ctx.exception))
        self.assertEqual(self.stub.requests, [])


class StreamTest(Case):
    def drain(self, body=None):
        return list(cloudllm.custom_stream("demo", body or self.body(stream=True), {"SHAFT_CUSTOM_API_KEY": KEY}))

    def test_stream_relays_and_records_usage(self):
        self.stub.sse = [{"choices": [{"index": 0, "delta": {"content": "hel"}, "finish_reason": None}]},
                         {"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]},
                         {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 7}}, "[DONE]"]
        lines = self.drain()
        text = b"".join(lines).decode()
        self.assertIn('"hel"', text)
        self.assertIn('"lo"', text)
        self.assertIn("[DONE]", text)
        (req,) = self.stub.requests
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["body"]["model"], "my-model")
        self.assertIs(req["body"]["stream"], True)
        self.assertEqual(req["body"]["stream_options"], {"include_usage": True})
        (row,) = self.rows()
        self.assertEqual((row["model"], row["input_tokens"], row["output_tokens"]), (MODEL, 40, 7))
        self.assertAlmostEqual(row["usd"], (40 * 2.0 + 7 * 8.0) / 1e6)
        self.assertEqual(row["stop_reason"], "stop")
        self.assertFalse(row.get("estimated"))

    def test_a_stream_without_usage_reserves_the_worst_case(self):
        self.stub.sse = [{"choices": [{"index": 0, "delta": {"content": "abc"}, "finish_reason": "stop"}]}, "[DONE]"]
        self.drain(self.body(stream=True, max_tokens=300))
        (row,) = self.rows()
        self.assertEqual(row["output_tokens"], 300)
        self.assertEqual(row["input_tokens"], math.ceil(len(json.dumps(MESSAGES)) / 3))
        self.assertIs(row["estimated"], True)
        self.assertGreater(row["usd"], 0)

    def test_unpriced_or_capped_streams_are_refused_before_any_call(self):
        with mock.patch.object(cloudllm, "CUSTOM_PRICES", {}):
            with self.assertRaises(cloudllm.CloudError) as ctx:
                self.drain()
        self.assertEqual(ctx.exception.status, 403)
        with mock.patch.object(cloudllm, "spent_today", return_value=5.0):
            with self.assertRaises(cloudllm.CloudError) as ctx:
                self.drain()
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(self.stub.requests, [])


class BrokerRouteTest(Case):
    def handler(self):
        b = broker.Broker.__new__(broker.Broker)
        b.name = b.usage_name = "demo"
        b.channel, b.channel_down, b._channel_rate = None, None, None
        b._channel_lock = threading.Lock()
        b._log, b.secrets, b.remembered, b.model_allowed = mock.Mock(), {"SHAFT_CUSTOM_API_KEY": KEY}, {}, True
        b.root, b.egress = self.tmp, mock.Mock()
        captured = {}

        def server(addr, handler):
            captured["handler"] = handler
            return mock.Mock(server_address=("127.0.0.1", 1))
        with mock.patch.object(broker, "ThreadingHTTPServer", side_effect=server), \
                mock.patch.object(broker.threading, "Thread"):
            b.start()
        return captured["handler"]

    def test_a_custom_stream_goes_to_the_custom_stream_not_a_buffered_reply(self):
        handler = self.handler()
        h = handler.__new__(handler)
        data = json.dumps(self.body(stream=True)).encode()
        h.rfile, h.wfile, h.headers = io.BytesIO(data), io.BytesIO(), {"Content-Length": str(len(data))}
        h.send_response, h.send_header, h.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(cloudllm, "custom_stream", return_value=(line for line in [b"data: [DONE]\n"])) as stream, \
                mock.patch.object(cloudllm, "openrouter_stream") as other, \
                mock.patch.object(cloudllm, "complete") as buffered:
            h._llm()
        stream.assert_called_once()
        other.assert_not_called()
        buffered.assert_not_called()
        h.send_response.assert_called_once_with(200)


class KnobTest(unittest.TestCase):
    def test_the_base_url_parser_takes_only_http_urls(self):
        self.assertEqual(core.http_url("https://api.example.com/v1"), "https://api.example.com/v1")
        self.assertEqual(core.http_url(""), "")
        for bad in ("ftp://x/v1", "api.example.com/v1", "file:///etc/passwd", "http://"):
            with self.assertRaises(ValueError, msg=bad):
                core.http_url(bad)

    def test_the_price_parser_takes_non_negative_pairs_only(self):
        self.assertEqual(core.custom_prices('{"a": [1, 2.5], "b": [0, 0]}'), {"a": (1.0, 2.5), "b": (0.0, 0.0)})
        for bad in ('{"a": [-1, 2]}', '{"a": [1]}', '{"a": "free"}', '{"a": [true, 1]}', '[1, 2]', '{"a": [1, null]}'):
            with self.assertRaises(ValueError, msg=bad):
                core.custom_prices(bad)

    def test_custom_price_looks_up_the_full_name(self):
        with mock.patch.object(cloudllm, "CUSTOM_PRICES", {"m": (1.0, 2.0)}):
            self.assertEqual(cloudllm.custom_price("custom/m"), (1.0, 2.0))
            self.assertIsNone(cloudllm.custom_price("custom/other"))
            self.assertIsNone(cloudllm.custom_price("m"))
            self.assertIsNone(cloudllm.custom_price("claude-haiku-5-5"))

    def test_the_knobs_are_registered_with_the_model_knobs(self):
        for name in ("SHAFT_CUSTOM_BASE_URL", "SHAFT_CUSTOM_PRICES"):
            self.assertEqual(core.KNOBS[name].group, "Models and spend")


class SecretTest(unittest.TestCase):
    def test_the_key_never_reaches_the_run_environment(self):
        environ = {"PATH": "/usr/bin", "SHAFT_CUSTOM_API_KEY": KEY, "SHAFT_CUSTOM_BASE_URL": "http://x"}
        self.assertNotIn("SHAFT_CUSTOM_API_KEY", sandbox.agent_env(environ))
        b = mock.Mock()
        b.egress.env.return_value = {}
        with mock.patch.dict("os.environ", environ), \
                mock.patch("supervisor.pyenv.python", return_value="/py"), \
                mock.patch("supervisor.browser.env", return_value={}):
            run_env = broker.Broker.sandbox_env(b)
        self.assertNotIn(KEY, json.dumps(run_env))
        self.assertNotIn("SHAFT_CUSTOM_API_KEY", run_env)
        # the key is a secret the supervisor stores, so it is read from the project .env only
        self.assertTrue(core.SECRET_NAMES_RE.fullmatch("SHAFT_CUSTOM_API_KEY"))


if __name__ == "__main__":
    unittest.main()
