"""shaft thinks on a configured example model server through the broker, with the local Bonsai
as fallback (the operator, 2026-10-14): the request reaches gpu as the agent wrote it plus gpu's sampling and a
thinking budget; an unreachable gpu goes to the local server instead; a trial never does."""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from supervisor import cloudllm, selfhosted
from supervisor.tests.fakellm import FakeLLM

SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.05}


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-selfhosted-"))
        self.usage = self.root / "cloud_usage.jsonl"
        self.gpu, self.local = self.fake([{"content": "from gpu"}] * 3), self.fake([{"content": "from local"}] * 3)
        self.use(gpu_url=self.gpu.url)
        p = mock.patch.object(cloudllm, "USAGE", self.usage)
        p.start()
        self.addCleanup(p.stop)

    def fake(self, replies):
        f = FakeLLM(replies)
        self.addCleanup(f.close)
        return f

    def use(self, gpu_url: str, fallback: str | None = None):
        servers = {"gpu": {"url": gpu_url, "sampling": SAMPLING, "think_budget": 4096}}
        for p in (mock.patch.object(selfhosted, "SERVERS", servers),
                  mock.patch.object(selfhosted, "FALLBACK", self.local.url if fallback is None else fallback)):
            p.start()
            self.addCleanup(p.stop)

    def broker(self, cls=None):
        from supervisor.broker import Broker
        b = (cls or Broker)("shaft", self.root, {}, self.root / "broker.log")
        base = b.start()
        self.addCleanup(b.stop)
        return b, base

    def client(self, b, base):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
        from shaft.llm import Client
        return Client(base + "/llm/v1", "gpu/bonsai2", "", 30, self.root / "u.jsonl", broker_token=b.token,
                      presence_penalty=1.5, think_followup=2048, reasoning_effort="medium")


class HostedServerTest(_Base):
    def test_the_agent_reaches_gpu_with_its_template_settings_sampling_and_a_thinking_budget(self):
        # The agent sent Bonsai's template settings only to the local server, never through the broker: on gpu
        # the template's default (xhigh) would have thought with no bound but max_tokens.
        b, base = self.broker()
        reply = self.client(b, base).complete([{"role": "user", "content": "hi"}], 100, "test")
        self.assertEqual(reply.content, "from gpu")
        sent = self.gpu.requests[-1]
        self.assertEqual(sent["model"], "bonsai2")
        self.assertEqual(sent["chat_template_kwargs"], {"preserve_thinking": False, "reasoning_effort": "medium"})
        self.assertEqual({k: sent[k] for k in SAMPLING}, SAMPLING)
        self.assertEqual(sent["presence_penalty"], 1.5)          # the agent's own setting is not overridden
        self.assertEqual(sent["thinking_budget_tokens"], 4096)
        self.assertEqual(self.local.requests, [])
        rec = json.loads(self.usage.read_text().splitlines()[-1])
        self.assertEqual((rec["model"], rec["usd"], rec["error"]), ("gpu/bonsai2", None, None))

    def test_after_a_tool_result_the_smaller_budget_reaches_gpu_and_no_thinking_means_no_budget(self):
        b, base = self.broker()
        c = self.client(b, base)
        tool_last = [{"role": "user", "content": "x"},
                     {"role": "assistant", "content": "", "tool_calls": [
                         {"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": "1", "content": "r"}]
        c.complete(tool_last, 100, "test")
        self.assertEqual(self.gpu.requests[-1]["thinking_budget_tokens"], 2048)
        c.complete([{"role": "user", "content": "x"}], 100, "test", think=False)
        self.assertNotIn("thinking_budget_tokens", self.gpu.requests[-1])
        self.assertIs(self.gpu.requests[-1]["chat_template_kwargs"]["enable_thinking"], False)

    def test_the_agent_reads_gpus_stream_through_the_broker(self):
        b, base = self.broker()
        chunks = []
        reply = self.client(b, base).stream([{"role": "user", "content": "hi"}], 100, "test", on_content=chunks.append)
        self.assertEqual("".join(chunks), "from gpu")
        self.assertTrue(self.gpu.requests[-1]["stream"])
        self.assertEqual(reply.finish_reason, "stop")


class FallbackTest(_Base):
    def test_gpu_off_means_the_local_bonsai_answers_and_the_log_says_so(self):
        self.use(gpu_url=f"http://127.0.0.1:{closed_port()}/v1")
        b, base = self.broker()
        reply = self.client(b, base).stream([{"role": "user", "content": "hi"}], 100, "test")
        self.assertEqual(reply.content, "from local")
        sent = self.local.requests[-1]
        self.assertEqual(sent["chat_template_kwargs"]["reasoning_effort"], "medium")
        for gpu_only in (*SAMPLING, "thinking_budget_tokens"):   # the local server keeps its own settings
            self.assertNotIn(gpu_only, sent)
        self.assertIn("-> local fallback", (self.root / "broker.log").read_text())
        rec = json.loads(self.usage.read_text().splitlines()[-1])
        self.assertEqual(rec["model"], "gpu/bonsai2 (local fallback)")

    def test_gpu_failing_before_any_reply_falls_back_but_a_bad_request_does_not(self):
        f = object()   # stands for gpu's opened reply
        for status in (503, 400):
            with mock.patch.object(selfhosted, "_open", side_effect=[(None, "HTTP 503: loading model"),
                                                                     (f, None)] if status == 503 else
                                   [selfhosted.CloudError(400, "gpu refused the request: HTTP 400")]):
                if status == 503:
                    resp, label, _ = selfhosted.open_reply("shaft", {"model": "gpu/bonsai2", "messages": []})
                    self.assertEqual(label, "gpu/bonsai2 (local fallback)")
                else:
                    with self.assertRaises(cloudllm.CloudError) as ctx:
                        selfhosted.open_reply("shaft", {"model": "gpu/bonsai2", "messages": []})
                    self.assertEqual(ctx.exception.status, 400)

    def test_a_trial_never_takes_the_local_model_when_gpu_is_off(self):
        from supervisor.broker import ReplayBroker
        self.use(gpu_url=f"http://127.0.0.1:{closed_port()}/v1")
        self.assertFalse(ReplayBroker.local_fallback)
        b, base = self.broker()
        b.local_fallback = False   # what a ReplayBroker is; its own setup needs a whole replayed instance
        req = urllib.request.Request(base + "/llm/v1/chat/completions", data=json.dumps(
            {"model": "gpu/bonsai2", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"X-Shaft-Token": b.token, "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 502)
        self.assertIn("never takes the local model", ctx.exception.read().decode())
        self.assertEqual(self.local.requests, [])

    def test_no_fallback_configured_fails_and_says_why(self):
        self.use(gpu_url=f"http://127.0.0.1:{closed_port()}/v1", fallback="")
        with self.assertRaises(cloudllm.CloudError) as ctx:
            cloudllm.complete("shaft", {"model": "gpu/bonsai2", "messages": []}, {}, {})
        self.assertIn("unreachable", str(ctx.exception))
        self.assertIn("no local fallback", str(ctx.exception))


class RoutingTest(_Base):
    def test_only_configured_servers_are_self_hosted(self):
        self.assertTrue(selfhosted.serves("gpu/bonsai2"))
        for other in ("gpu/", "claude-opus-5-5", "openrouter/xiaomi/mimo-v2.6-pro", "elsewhere/bonsai2", None):
            self.assertFalse(selfhosted.serves(other), other)

    def test_evolutions_baseline_reaches_gpu_without_a_cloud_key_or_cap(self):
        with mock.patch.object(cloudllm, "spent_today", return_value=10_000.0):
            out = cloudllm.complete("shaft-baseline", {"model": "gpu/bonsai2", "messages": []}, {}, {})
        self.assertEqual(out["choices"][0]["message"]["content"], "from gpu")

    def test_an_instance_on_gpu_takes_gpu_not_the_local_model(self):
        from supervisor import runner
        cfgs = {"shaft": {"LLM_BASE_URL": "broker", "LLM_MODEL": "gpu/bonsai2"},
                "other": {"LLM_BASE_URL": "http://127.0.0.1:8780/v1"},
                "twin": {"LLM_BASE_URL": "broker", "LLM_MODEL": "gpu/bonsai2"},
                "kant": {"LLM_BASE_URL": "broker", "LLM_MODEL": "claude-opus-5-5"}}
        with mock.patch.object(runner, "instance_config", side_effect=lambda n: cfgs[n]):
            self.assertEqual(runner.model_server("shaft"), selfhosted.host_port("gpu/bonsai2"))
            self.assertIsNone(runner.model_server("kant"))
            self.assertEqual(runner.sharing_model("shaft", ["other", "twin", "kant"]), ["twin"])


if __name__ == "__main__":
    unittest.main()
