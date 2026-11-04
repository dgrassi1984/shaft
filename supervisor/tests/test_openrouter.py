"""Instances on OpenRouter models through the broker: the key stays with the supervisor, the data policy is
enforced on every request, the cost is OpenRouter's own figure, and failures say why."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supervisor import cloudllm

REPLY = {"id": "gen-1", "object": "chat.completion", "model": "xiaomi/mimo-v2.6-pro",
         "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
             "role": "assistant", "content": "", "reasoning": "check the agenda first",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "read_file", "arguments": "{\"path\": \"notes/agenda.md\"}"}}]}}],
         "usage": {"prompt_tokens": 12000, "completion_tokens": 300, "cost": 0.0055}}


STREAM = ["COMMENT",
          {"choices": [{"index": 0, "delta": {"reasoning": "check the agenda"}, "finish_reason": None}]},
          {"choices": [{"index": 0, "delta": {"content": "Teal is "}, "finish_reason": None}]},
          "PAUSE",
          {"choices": [{"index": 0, "delta": {"content": "blue-green."}, "finish_reason": "stop"}]},
          {"choices": [], "usage": {"prompt_tokens": 9000, "completion_tokens": 40, "cost": 0.0042}}]


class FakeOpenRouter:
    def __init__(self, status=200, reply=None):
        self.requests, self.headers = [], []
        self.paused = threading.Event()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                fake.headers.append(dict(self.headers))
                if body.get("stream") and status == 200:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for chunk in STREAM:
                        if chunk == "PAUSE":
                            fake.paused.wait(5)   # the test checks the first chunks arrived before the rest exist
                            continue
                        self.wfile.write(b": OPENROUTER PROCESSING\n\n" if chunk == "COMMENT" else
                                         b"data: " + json.dumps(chunk).encode() + b"\n\n")
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                payload = json.dumps(reply if reply is not None else REPLY).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/v1/chat/completions"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _Base(unittest.TestCase):
    def setUp(self):
        self.usage = Path(tempfile.mkdtemp(prefix="shaft-cloud-")) / "cloud_usage.jsonl"
        for p in (mock.patch.object(cloudllm, "USAGE", self.usage),
                  mock.patch.object(cloudllm, "CLOUD_MODELS", ["openrouter/xiaomi/mimo-v2.6-pro"])):
            p.start()
            self.addCleanup(p.stop)

    def fake(self, **kw):
        f = FakeOpenRouter(**kw)
        self.addCleanup(f.close)
        p = mock.patch.object(cloudllm, "OPENROUTER_API", f.url)
        p.start()
        self.addCleanup(p.stop)
        return f

    BODY = {"model": "openrouter/xiaomi/mimo-v2.6-pro", "stream": True, "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"preserve_thinking": False},
            "messages": [{"role": "user", "content": "what is on my agenda?"}], "tools": [], "max_tokens": 8192}


class OpenRouterTest(_Base):

    def test_request_carries_the_data_policy_and_the_cost_is_openrouters(self):
        f = self.fake()
        out = cloudllm.complete("shaft", dict(self.BODY), {"OPENROUTER_API_KEY": "sk-or-test"}, {})
        sent = f.requests[0]
        self.assertEqual(sent["model"], "xiaomi/mimo-v2.6-pro")
        # Novita first, DeepInfra only if Novita is down, never another provider (2026-10-20)
        self.assertEqual(sent["provider"], {"only": ["novita", "deepinfra"], "order": ["novita", "deepinfra"],
                                            "allow_fallbacks": False, "data_collection": "deny",
                                            "zdr": True, "require_parameters": True})
        for local_only in ("stream", "stream_options", "chat_template_kwargs"):
            self.assertNotIn(local_only, sent)
        self.assertEqual(f.headers[0]["Authorization"], "Bearer sk-or-test")
        self.assertEqual(out["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "read_file")
        rec = json.loads(self.usage.read_text())
        self.assertEqual((rec["usd"], rec["input_tokens"], rec["error"]), (0.0055, 12000, None))
        self.assertGreater(rec["usd"], 0)   # the daily cap counts real money, not a price-table guess

    def test_the_instance_s_reasoning_settings_reach_openrouter_20261030(self):
        # Regression (thelos and wren, 2026-10-29..30): LLM_REASONING_EFFORT went out only as template kwargs, which
        # are dropped for OpenRouter; every sleep since 10-29 ended "cut off at max_tokens=32768 twice".
        cases = (({"preserve_thinking": False, "reasoning_effort": "medium"}, {"effort": "medium"}),
                 ({"preserve_thinking": False, "enable_thinking": False}, {"enabled": False}),
                 ({"preserve_thinking": False}, None))
        for kwargs, expected in cases:
            f = self.fake()
            cloudllm.complete("shaft", {**self.BODY, "chat_template_kwargs": kwargs}, {"OPENROUTER_API_KEY": "k"}, {})
            self.assertEqual(f.requests[0].get("reasoning"), expected, kwargs)
            self.assertNotIn("chat_template_kwargs", f.requests[0])
        f = self.fake()                                               # streamed, as a live cycle calls it
        list(cloudllm.openrouter_stream("shaft", {**self.BODY, "chat_template_kwargs": {"reasoning_effort": "low"}},
                                        {"OPENROUTER_API_KEY": "k"}))
        self.assertEqual(f.requests[0]["reasoning"], {"effort": "low"})
        sent = cloudllm._openrouter_request({**self.BODY, "reasoning": {"effort": "high"},   # the judge's own wins
                                             "chat_template_kwargs": {"reasoning_effort": "low"}}, "k", stream=False)
        self.assertEqual(json.loads(sent.data)["reasoning"], {"effort": "high"})

    def test_mandatory_reasoning_uses_minimal_when_thinking_is_disabled_20261104(self):
        # Sol rejects reasoning.enabled=false with HTTP 400 during recovery and cut-off retries; minimal works.
        sol = "openrouter/openai/gpt-6.1-sol"
        with mock.patch.object(cloudllm, "CLOUD_MODELS", [sol, self.BODY["model"]]):
            for model, expected in ((sol, {"effort": "minimal"}), (self.BODY["model"], {"enabled": False})):
                f = self.fake()
                f.paused.set()
                body = {**self.BODY, "model": model, "chat_template_kwargs": {"enable_thinking": False}}
                cloudllm.complete("shaft", body, {"OPENROUTER_API_KEY": "k"}, {})
                list(cloudllm.openrouter_stream("shaft", body, {"OPENROUTER_API_KEY": "k"}))
                self.assertEqual(len(f.requests), 2)
                for sent in f.requests:
                    self.assertEqual(sent["reasoning"], expected, model)
                    if model == sol:
                        self.assertNotIn("enabled", sent["reasoning"])
                sent = cloudllm._openrouter_request({**body, "reasoning": {"effort": "high"}}, "k", stream=False)
                self.assertEqual(json.loads(sent.data)["reasoning"], {"effort": "high"})

    def test_a_provider_error_without_an_error_object_is_recorded_20261030(self):
        # Regression: a reply ended by finish_reason "error", with tokens and no cost, was booked as $0 with error
        # None (12 of 42,000 mimo calls): neither the failure nor the unbooked spend was visible in the usage log.
        self.fake(reply={"choices": [{"message": {"role": "assistant", "content": "half"}, "finish_reason": "error"}],
                         "usage": {"prompt_tokens": 24754, "completion_tokens": 396}})
        cloudllm.complete("thelos", dict(self.BODY, stream=False), {"OPENROUTER_API_KEY": "k"}, {})
        rec = json.loads(self.usage.read_text())
        self.assertIsNone(rec["usd"])
        self.assertIn("finish_reason=error", rec["error"])
        self.assertIn("no cost in its usage", rec["error"])
        self.assertIsNone(cloudllm._unsaid({"prompt_tokens": 5, "cost": 0.001}, "stop"))   # a normal reply says nothing

    def test_no_key_is_said_plainly(self):
        with self.assertRaises(cloudllm.CloudError) as ctx:
            cloudllm.complete("shaft", dict(self.BODY), {}, {})
        self.assertIn("no OPENROUTER_API_KEY", str(ctx.exception))

    def test_no_provider_meeting_the_policy_fails_and_names_the_policy(self):
        self.fake(status=404, reply={"error": {"message": "No endpoints found matching your data policy"}})
        with self.assertRaises(cloudllm.CloudError) as ctx:
            cloudllm.complete("shaft", dict(self.BODY), {"OPENROUTER_API_KEY": "k"}, {})
        self.assertIn('"zdr": true', str(ctx.exception))
        self.assertIn("HTTP 404", json.loads(self.usage.read_text())["error"])

    def test_error_inside_a_200_is_a_failure_not_an_empty_reply(self):
        self.fake(reply={"error": {"message": "upstream timeout", "code": 502}})
        with self.assertRaises(cloudllm.CloudError):
            cloudllm.complete("shaft", dict(self.BODY), {"OPENROUTER_API_KEY": "k"}, {})

    def test_through_the_broker_without_streaming(self):
        from supervisor.broker import Broker
        self.fake()
        root = Path(tempfile.mkdtemp(prefix="shaft-proj-"))
        b = Broker("shaft", root, {"OPENROUTER_API_KEY": "k"}, root / "broker.log")
        base = b.start()
        self.addCleanup(b.stop)
        req = urllib.request.Request(base + "/llm/v1/chat/completions",
                                     data=json.dumps({**self.BODY, "stream": False}).encode(),
                                     headers={"X-Shaft-Token": b.token, "Content-Type": "application/json"})
        out = json.load(urllib.request.urlopen(req, timeout=10))
        msg = out["choices"][0]["message"]
        self.assertEqual(msg["reasoning"], "check the agenda first")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "read_file")


class StreamingTest(_Base):
    def test_wren_sees_its_reply_as_it_is_written(self):
        # the operator: "wren does not use streaming in its response (mimo)" — the broker called OpenRouter without
        # streaming and replayed the finished reply in one burst.
        from supervisor.broker import Broker
        f = self.fake()
        root = Path(tempfile.mkdtemp(prefix="shaft-proj-"))
        b = Broker("wren", root, {"OPENROUTER_API_KEY": "k"}, root / "broker.log")
        base = b.start()
        self.addCleanup(b.stop)
        req = urllib.request.Request(base + "/llm/v1/chat/completions", data=json.dumps(self.BODY).encode(),
                                     headers={"X-Shaft-Token": b.token, "Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        seen = b""
        while b"Teal is " not in seen:   # arrives while the rest of the reply does not exist yet
            seen += resp.readline()
        self.assertNotIn(b"blue-green", seen)
        self.assertTrue(f.requests[0]["stream"])
        f.paused.set()
        rest = resp.read()
        self.assertIn(b"blue-green", rest)
        self.assertTrue(rest.rstrip().endswith(b"data: [DONE]"))
        rec = json.loads(self.usage.read_text().splitlines()[-1])
        self.assertEqual((rec["usd"], rec["input_tokens"], rec["stop_reason"]), (0.0042, 9000, "stop"))

    def test_the_agent_reads_the_relayed_stream(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
        from shaft.llm import Client
        from supervisor.broker import Broker
        f = self.fake()
        f.paused.set()
        root = Path(tempfile.mkdtemp(prefix="shaft-proj-"))
        b = Broker("wren", root, {"OPENROUTER_API_KEY": "k"}, root / "broker.log")
        base = b.start()
        self.addCleanup(b.stop)
        chunks = []
        c = Client(base + "/llm/v1", "openrouter/xiaomi/mimo-v2.6-pro", "", 30, root / "u.jsonl", broker_token=b.token)
        reply = c.stream([{"role": "user", "content": "teal?"}], 100, "test", on_content=chunks.append)
        self.assertEqual(chunks, ["Teal is ", "blue-green."])
        self.assertEqual(reply.reasoning, "check the agenda")
        self.assertEqual(reply.finish_reason, "stop")


if __name__ == "__main__":
    unittest.main()
