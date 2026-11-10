"""Model consultation in the template: the consult allowance and menu in the prompt's envelope, the
`broker.consult` transport a plugin uses, and the skill page listed among my skills. The supervisor side is
test_consult (transport, allowance, books) and test_consultstats (M2)."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

TEMPLATE = Path(__file__).resolve().parents[2] / "template"
sys.path.insert(0, str(TEMPLATE))
from shaft import broker, prompts

if not hasattr(broker, "consult"):
    raise unittest.SkipTest("template/ predates model consultation: merge claude/model-choice-template into template")

CONSULT = {"spent_usd": 0.5, "reserved_usd": 0.25, "cap_usd": 3.0, "models": [
    {"model": "openrouter/z-ai/glm-5.3", "in_per_m": 0.07, "out_per_m": 1.4, "available": True, "why": None},
    {"model": "openrouter/anthropic/claude-opus-5.5", "in_per_m": 4.0, "out_per_m": 20.0, "available": True,
     "why": None},
    {"model": "openrouter/openai/gpt-6.1-terra", "in_per_m": 1.0, "out_per_m": 5.0, "available": False,
     "why": "no provider keeps no data"}]}
ENV = {"calls_used": 1, "calls_max": 60, "consult": CONSULT}


class EnvelopeLineTest(unittest.TestCase):
    def test_allowance_left_counts_reservations_and_the_menu_is_priced(self):
        line = prompts.consult_line(CONSULT)
        self.assertIn("Consult allowance today: $2.25 left of $3.00", line)
        self.assertIn("z-ai/glm-5.3 0.07/1.4, anthropic/claude-opus-5.5 4/20", line)
        self.assertIn("unavailable: openai/gpt-6.1-terra", line)
        self.assertIn("skills/choosing-a-model.md", line)
        self.assertIn(line, prompts.envelope_text(ENV, None))

    def test_not_mine_this_run_or_malformed_says_nothing(self):
        for consult in (None, {}, {"cap_usd": 3}, {"spent_usd": "x", "cap_usd": 3}):
            self.assertEqual(prompts.consult_line(consult), "", consult)
        self.assertNotIn("Consult", prompts.envelope_text({"calls_used": 1, "calls_max": 60, "consult": None}, None))

    def test_never_shows_a_negative_balance(self):
        self.assertIn("$0.00 left of $3.00", prompts.consult_line({**CONSULT, "spent_usd": 3.4}))


class TransportTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"SHAFT_BROKER_URL": "http://127.0.0.1:9", "SHAFT_BROKER_TOKEN": "tok"})
        p.start()
        self.addCleanup(p.stop)

    def test_posts_to_the_consult_endpoint_with_token_and_caller(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout):
            seen.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data), timeout=timeout)
            return Resp(json.dumps({"choices": [{"message": {"content": "42"}}], "shaft": {"usd": 0.01}}).encode())
        with mock.patch.object(broker.urllib.request, "urlopen", urlopen):
            out = broker.consult("openrouter/z-ai/glm-5.3", [{"role": "user", "content": "q"}], max_tokens=500,
                                 caller="ask_model" + "x" * 80, temperature=0)
        self.assertEqual(out["choices"][0]["message"]["content"], "42")
        self.assertEqual(seen["url"], "http://127.0.0.1:9/llm/consult/v1/chat/completions")
        self.assertEqual(seen["headers"]["X-shaft-token"], "tok")
        self.assertEqual(len(seen["headers"]["X-shaft-caller"]), 60)
        self.assertEqual(seen["body"], {"model": "openrouter/z-ai/glm-5.3", "messages": [{"role": "user", "content": "q"}],
                                        "temperature": 0, "max_tokens": 500})
        self.assertLess(seen["timeout"], 120)   # under a plugin's own limit

    def test_a_refusal_carries_the_supervisor_s_reason(self):
        def urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(
                json.dumps({"error": {"message": "over your consult allowance"}}).encode()))
        with mock.patch.object(broker.urllib.request, "urlopen", urlopen):
            with self.assertRaisesRegex(broker.BrokerError, "HTTP 429: over your consult allowance"):
                broker.consult("openrouter/z-ai/glm-5.3", [])

    def test_outside_the_supervisor_it_says_so(self):
        with mock.patch.dict(os.environ, {"SHAFT_BROKER_URL": ""}):
            with self.assertRaisesRegex(broker.BrokerError, "SHAFT_BROKER_URL"):
                broker.consult("m", [])


class EpisodeRunStartedTest(unittest.TestCase):
    """The episode carries its run's start (SHAFT_RUN_STARTED, from runner._launch): consults are booked to the cycle
    by that exact time, so a run killed before writing an episode leaks into no other cycle (Grok's review)."""

    def test_a_live_episode_records_the_supervisor_s_run_start(self):
        try:
            from supervisor.tests.test_consciousness_template import DECLARE, FINISH, Fixture
        except unittest.SkipTest as e:
            self.skipTest(str(e))

        class Cycle(Fixture, unittest.TestCase):
            def runTest(self):
                pass
        t = Cycle()
        t.setUp()
        try:
            with mock.patch.dict(os.environ, {"SHAFT_RUN_STARTED": "2026-10-27T14:00:00"}):
                t.live([{"tool_calls": [("declare_intent", DECLARE)]}, {"tool_calls": [("finish_cycle", FINISH)]}])
            self.assertEqual(t.last()["run_started"], "2026-10-27T14:00:00")
            with mock.patch.dict(os.environ, {"SHAFT_RUN_STARTED": ""}):
                t.live([{"tool_calls": [("declare_intent", DECLARE)]}, {"tool_calls": [("finish_cycle", FINISH)]}])
            self.assertNotIn("run_started", t.last())   # outside the supervisor: nothing invented
        finally:
            t.doCleanups()


class SkillTest(unittest.TestCase):
    def test_the_skill_page_is_listed_by_its_first_line(self):
        first = (TEMPLATE / "skills" / "choosing-a-model.md").read_text().splitlines()[0]
        self.assertIn("Consulting another model", first)
        self.assertIn("broker.consult(", (TEMPLATE / "skills" / "choosing-a-model.md").read_text())


if __name__ == "__main__":
    unittest.main()
