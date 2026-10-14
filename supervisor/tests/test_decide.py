"""Decision models (TypeSafe's Jev) through the broker's `decide` tool (Daniele, 2026-10-14).

Jev is not a chat model, so the broker's /llm path could not reach it: wren had to ask Daniele for a key of its own.
Now the supervisor calls OpenRouter's decisions endpoint with its own key, under the same allowlist, daily cap,
usage log and data policy as the chat models; a trial replay's pair shares one real answer.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import battery, broker, cloudllm
from supervisor.tests.test_openrouter import FakeOpenRouter

ANSWER = {"model": "typesafe/jev-1.13-20260917", "id": "gen-dec-1", "provider": "TypeSafe",
          "answers": {"is_bug": {"type": "noul", "noul": 0.95}},
          "usage": {"input_tokens": 437, "output_tokens": 69, "cost": 1.8354e-05}}
QUESTIONS = {"is_bug": {"type": "noul", "instructions": "Is this a software defect?",
                        "criteria": {"true": "Broken behaviour.", "false": "A question."}}}


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-decide-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "instances" / "wren").mkdir(parents=True)
        self.fake = FakeOpenRouter(reply=ANSWER)
        self.addCleanup(self.fake.close)
        for p in (mock.patch.object(cloudllm, "USAGE", self.tmp / "cloud_usage.jsonl"),
                  mock.patch.object(cloudllm, "DECISIONS_API", self.fake.url)):
            p.start()
            self.addCleanup(p.stop)
        self.b = broker.Broker("wren", self.tmp, {"OPENROUTER_API_KEY": "sk-or-test"}, self.tmp / "broker.log")

    def test_answers_come_back_and_the_cost_is_recorded_for_the_instance(self):
        r = self.b.call("decide", {"state": "checkout page blank after Pay", "questions": QUESTIONS})
        self.assertTrue(r["ok"], r)
        out = json.loads(r["result"])
        self.assertEqual(out["answers"]["is_bug"]["noul"], 0.95)
        self.assertEqual(out["model"], "typesafe/jev-1.13-20260917")
        sent = self.fake.requests[-1]
        self.assertEqual(sent["model"], "typesafe/jev-1.13")                  # the default
        self.assertEqual(sent["questions"], QUESTIONS)
        self.assertEqual(sent["provider"], cloudllm.DECISION_PROVIDER)       # the data policy, on every request
        self.assertEqual(sent["provider"]["data_collection"], "deny")
        self.assertTrue(sent["provider"]["zdr"])
        headers = {k.lower(): v for k, v in self.fake.headers[-1].items()}
        self.assertEqual(headers["authorization"], "Bearer sk-or-test")
        self.assertNotIn("sk-or-test", r["result"])
        row = json.loads((self.tmp / "cloud_usage.jsonl").read_text().splitlines()[-1])
        self.assertEqual((row["instance"], row["model"]), ("wren", "typesafe/jev-1.13"))
        self.assertGreater(row["usd"], 0)                                      # spend counts toward the daily cap

    def test_only_allowed_models_and_within_the_daily_cap(self):
        r = self.b.call("decide", {"state": "x", "questions": QUESTIONS, "model": "openai/gpt-decide"})
        self.assertFalse(r["ok"])
        self.assertIn("SHAFT_DECISION_MODELS", r["error"])
        with mock.patch.object(cloudllm, "spent_today", return_value=cloudllm.DAILY_USD):
            r = self.b.call("decide", {"state": "x", "questions": QUESTIONS})
        self.assertFalse(r["ok"])
        self.assertIn("cap", r["error"])
        self.assertEqual(self.fake.requests, [])                               # nothing was sent

    def test_no_key_says_where_it_is_missing(self):
        b = broker.Broker("wren", self.tmp, {}, self.tmp / "broker.log")
        r = b.call("decide", {"state": "x", "questions": QUESTIONS})
        self.assertIn("OPENROUTER_API_KEY", r["error"])

    def test_questions_must_be_an_object(self):
        r = self.b.call("decide", {"state": "x", "questions": ["is it a bug?"]})
        self.assertFalse(r["ok"])
        self.assertIn("questions must be an object", r["error"])

    def test_a_provider_that_fails_the_data_policy_fails_the_call_and_says_why(self):
        bad = FakeOpenRouter(status=404, reply={"error": {"code": 404, "message": "No endpoints found matching "
                                                                                  "your data policy (provider)"}})
        self.addCleanup(bad.close)
        with mock.patch.object(cloudllm, "DECISIONS_API", bad.url):
            r = self.b.call("decide", {"state": "x", "questions": QUESTIONS})
        self.assertFalse(r["ok"])
        self.assertIn("SHAFT_DECISION_PROVIDER", r["error"])

    def test_a_trial_replays_pair_shares_one_real_answer(self):
        cache, lock = {}, threading.Lock()
        def replay():
            state = Path(tempfile.mkdtemp(dir=self.tmp, prefix="copy-"))
            return broker.ReplayBroker("wren", self.tmp, {"OPENROUTER_API_KEY": "sk-or-test"}, self.tmp / "b.log",
                                       state_root=state, start_commit="0", as_of=datetime.now(), cache=cache,
                                       cache_lock=lock, billed="wren@trial:t", tools_log=self.tmp / "t.log")
        first, second = replay(), replay()
        a = first.call("decide", {"state": "x", "questions": QUESTIONS})
        b = second.call("decide", {"state": "x", "questions": QUESTIONS})
        self.assertEqual(a["result"], b["result"])
        self.assertEqual(len(self.fake.requests), 1)
        row = json.loads((self.tmp / "cloud_usage.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["instance"], "wren@trial:t")                     # billed to the trial, not the instance

    def test_battery_exams_keep_their_tools(self):
        self.assertIn("decide", battery.EXAM_OFF)


if __name__ == "__main__":
    unittest.main()
