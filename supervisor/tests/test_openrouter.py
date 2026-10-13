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


class FakeOpenRouter:
    def __init__(self, status=200, reply=None):
        self.requests, self.headers = [], []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                fake.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                fake.headers.append(dict(self.headers))
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


class OpenRouterTest(unittest.TestCase):
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

    def test_request_carries_the_data_policy_and_the_cost_is_openrouters(self):
        f = self.fake()
        out = cloudllm.complete("shaft", dict(self.BODY), {"OPENROUTER_API_KEY": "sk-or-test"}, {})
        sent = f.requests[0]
        self.assertEqual(sent["model"], "xiaomi/mimo-v2.6-pro")
        self.assertEqual(sent["provider"], {"data_collection": "deny", "zdr": True, "require_parameters": True})
        for local_only in ("stream", "stream_options", "chat_template_kwargs"):
            self.assertNotIn(local_only, sent)
        self.assertEqual(f.headers[0]["Authorization"], "Bearer sk-or-test")
        self.assertEqual(out["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "read_file")
        rec = json.loads(self.usage.read_text())
        self.assertEqual((rec["usd"], rec["input_tokens"], rec["error"]), (0.0055, 12000, None))
        self.assertGreater(rec["usd"], 0)   # the daily cap counts real money, not a price-table guess

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

    def test_through_the_broker_as_an_instance_streams_it(self):
        from supervisor.broker import Broker
        self.fake()
        root = Path(tempfile.mkdtemp(prefix="shaft-proj-"))
        b = Broker("shaft", root, {"OPENROUTER_API_KEY": "k"}, root / "broker.log")
        base = b.start()
        self.addCleanup(b.stop)
        req = urllib.request.Request(base + "/llm/v1/chat/completions", data=json.dumps(self.BODY).encode(),
                                     headers={"X-Shaft-Token": b.token, "Content-Type": "application/json"})
        raw = urllib.request.urlopen(req, timeout=10).read().decode()
        chunks = [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: {")]
        deltas = [c["choices"][0]["delta"] for c in chunks if c.get("choices")]
        self.assertEqual(deltas[0], {"reasoning": "check the agenda first"})
        self.assertEqual(deltas[1]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertTrue(raw.rstrip().endswith("data: [DONE]"))


if __name__ == "__main__":
    unittest.main()
