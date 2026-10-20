"""Prompt caching on the supervisor's Claude calls (the operator, 2026-10-15: 1-hour entries), and its billing.

Before this, no Claude request carried cache_control: every turn of an instance's loop re-billed its whole prompt
(kant: 84% of its spend was input). With caching on, a reply's input_tokens is only the uncached remainder — a cost
or cap computed from input_tokens alone would miss the cache reads and writes, and the daily cap would undercount."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import cloudllm, coder

# A turn of a loop: most of the prompt read from the cache, the new tail written, a little uncached.
USAGE = {"input_tokens": 50, "output_tokens": 400, "cache_read_input_tokens": 18000,
         "cache_creation_input_tokens": 2000,
         "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 2000}}
# Opus 5.5: $4 input, $20 output, $0.20 cache read; 1-hour writes at 2× input.
OPUS_USD = (50 * 4 + 400 * 20 + 18000 * 0.20 + 2000 * 4 * 2) / 1e6


def _reply(model="claude-opus-5-5", usage=USAGE, content=None):
    return {"id": "msg_1", "model": model, "stop_reason": "end_turn", "usage": usage,
            "content": content or [{"type": "text", "text": "done"}]}


class PricingTest(unittest.TestCase):
    def test_cache_reads_and_writes_are_priced(self):
        counts = cloudllm.billed(USAGE)
        self.assertEqual(counts, {"input_tokens": 50, "output_tokens": 400, "cache_read_tokens": 18000,
                                  "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 2000})
        self.assertAlmostEqual(cloudllm.usd("claude-opus-5-5", counts), OPUS_USD)
        # the bug this guards: pricing input_tokens alone bills a 20K-token prompt as 50 tokens
        self.assertGreater(cloudllm.usd("claude-opus-5-5", counts), (50 * 4 + 400 * 20) / 1e6 * 3)
        self.assertEqual(cloudllm.prompt_size(counts), 20050)

    def test_writes_without_a_ttl_split_are_priced_at_the_hour_rate(self):
        counts = cloudllm.billed({"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1000})
        self.assertEqual((counts["cache_write_1h_tokens"], counts["cache_write_5m_tokens"]), (1000, 0))
        self.assertAlmostEqual(cloudllm.usd("claude-haiku-5-5", counts), 1000 * 0.10 * 2 / 1e6)

    def test_old_records_without_cache_counts_price_as_before(self):
        self.assertAlmostEqual(cloudllm.usd("claude-sonnet-5-5", {"input_tokens": 1000, "output_tokens": 100}),
                               (1000 * 2 + 100 * 10) / 1e6)
        self.assertIsNone(cloudllm.usd("mystery-model", {"input_tokens": 1}))


class InstanceLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-cache-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(cloudllm, "USAGE", self.tmp / "cloud_usage.jsonl")
        p.start()
        self.addCleanup(p.stop)

    def test_every_request_asks_for_a_one_hour_cache(self):
        body = {"model": "claude-opus-5-5", "messages": [{"role": "system", "content": "S"},
                                                         {"role": "user", "content": "hi"}]}
        self.assertEqual(cloudllm.to_anthropic(body, {})["cache_control"], {"type": "ephemeral", "ttl": "1h"})

    def test_a_cached_turn_is_billed_in_full_and_reports_its_whole_prompt(self):
        sent = []
        def send(req):
            sent.append(json.loads(req.data))
            return _reply(), None
        with mock.patch.object(cloudllm, "_send", side_effect=send):
            out = cloudllm.complete("kant", {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]},
                                    {"ANTHROPIC_API_KEY": "k"}, {})
        self.assertEqual(sent[0]["cache_control"]["ttl"], "1h")
        rec = json.loads((self.tmp / "cloud_usage.jsonl").read_text().splitlines()[-1])
        self.assertAlmostEqual(rec["usd"], round(OPUS_USD, 6))
        self.assertEqual((rec["cache_read_tokens"], rec["cache_write_1h_tokens"]), (18000, 2000))
        self.assertAlmostEqual(cloudllm.spent_today("kant"), round(OPUS_USD, 6))   # what the daily cap counts
        # the instance sizes its context from prompt_tokens: the whole prompt, not the uncached 50
        self.assertEqual(out["usage"]["prompt_tokens"], 20050)
        self.assertEqual(out["usage"]["prompt_tokens_details"]["cached_tokens"], 18000)

    def test_an_unpriced_claude_model_is_refused_before_any_call(self):
        with mock.patch.object(cloudllm, "CLOUD_MODELS", ["claude-opus-9"]), \
                mock.patch.object(cloudllm, "_send") as send:
            with self.assertRaises(cloudllm.CloudError) as ctx:
                cloudllm.complete("x", {"model": "claude-opus-9", "messages": []}, {"ANTHROPIC_API_KEY": "k"}, {})
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("no price", str(ctx.exception))
        send.assert_not_called()


class CoderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-coder-cache-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "shaft").mkdir()
        for p in (mock.patch.object(coder, "USAGE", self.tmp / "coder_usage.jsonl"),
                  mock.patch.object(coder, "secrets_env", return_value={"ANTHROPIC_API_KEY": "k"})):
            p.start()
            self.addCleanup(p.stop)

    def test_coder_turns_are_cached_and_billed_in_full(self):
        read = _reply(content=[{"type": "tool_use", "id": "t1", "name": "list_files", "input": {}}])
        submit = _reply(content=[{"type": "tool_use", "id": "t2", "name": "submit",
                                  "input": {"files": [], "summary": "nothing to do"}}])
        bodies = []

        class Resp:
            def __init__(self, data):
                self.data = data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return json.dumps(self.data).encode()

        replies = iter([read, submit])
        def urlopen(req, timeout):
            bodies.append(json.loads(req.data))
            return Resp(next(replies))
        with mock.patch.object(coder.urllib.request, "urlopen", side_effect=urlopen):
            _, _, usage = coder.write(self.tmp, "kant", "nothing")
        self.assertEqual([b["cache_control"] for b in bodies], [{"type": "ephemeral", "ttl": "1h"}] * 2)
        self.assertEqual((usage["cache_read_tokens"], usage["cache_write_1h_tokens"]), (36000, 4000))
        self.assertAlmostEqual(usage["usd"], round(2 * OPUS_USD, 4))
        rec = json.loads((self.tmp / "coder_usage.jsonl").read_text().splitlines()[-1])
        self.assertAlmostEqual(coder.spent_today("kant"), rec["usd"])
        self.assertNotEqual(rec["usd"], round(2 * (50 * 4 + 400 * 20) / 1e6, 4))   # not input/output only

    def test_an_unpriced_coder_model_is_refused_before_any_call(self):
        with mock.patch.object(coder, "CODER_MODEL", "claude-opus-9"), \
                mock.patch.object(coder.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(coder.CoderError):
                coder.write(self.tmp, "kant", "x")
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
