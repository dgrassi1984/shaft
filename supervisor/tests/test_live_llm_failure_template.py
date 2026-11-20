"""#241: a live cycle whose model call fails for good still writes its record of what it did, then ends as a failure.

On 11-16 and 11-19 one model call hung for its whole 900 s limit; the cycle exited with "live failed, nothing saved",
and the tool work done before the hang (34 calls once) was lost to the next cycle's hand-off."""
from __future__ import annotations

import inspect
import json
import time
import unittest
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture

from shaft import agent, live   # noqa: E402 — the fixture module puts template/ on sys.path
if "llm_error" not in inspect.getsource(live):
    raise unittest.SkipTest("template/ predates #241 (live cycles keep their record on a model failure): merge it first")
from shaft import __main__ as instance_main   # noqa: E402
from shaft.llm import Client, LLMError, Truncated   # noqa: E402


class LiveModelFailureTest(InstanceFixture, unittest.TestCase):
    def hanging_model(self, replies, hang_on):
        """A model that answers `replies` in order and never answers request number `hang_on` within the client's
        1 s call limit — the shape of the 11-16 hang, where the read timed out."""
        fake, _ = self.model(replies)
        def slow(body):
            if len(fake.requests) == hang_on:
                time.sleep(1.5)
        fake.on_request = slow
        return Client(fake.url, "m", "", 1, self.root / "usage.jsonl")

    def test_bug241_record_is_written_with_the_work_done_before_the_hang(self):
        self.fake_broker(lambda name, **args: "3 results about orchards")
        (self.root / "inbox" / "20261116-060000-from-operator.md").write_text(
            "From: operator\nTo: me\nKind: note\nDate: 2026-11-16 06:00\n\nPlease look up the orchard survey.\n")
        client = self.hanging_model([
            {"tool_calls": [("declare_intent", {"goal": "Find the orchard survey", "success_criterion": "Link sent",
                                                "confidence": 60, "origin": "operator",
                                                "origin_ref": "20261116-060000-from-operator.md"})]},
            {"tool_calls": [("web_search", {"query": "orchard survey"})]},
        ], hang_on=3)
        with self.assertRaises(LLMError) as caught:
            live.run(self.cfg, self.state, client)
        self.assertIn("timed out", str(caught.exception))
        self.assertIsNotNone(getattr(caught.exception, "outcome", None))
        episode = self.state.last_episode()
        self.assertIsNotNone(episode, "the cycle wrote no record: the work before the hang is lost (#241)")
        self.assertEqual(episode["kind"], "live")
        self.assertEqual(episode["ended_by"], "llm_error")
        self.assertIn("timed out", episode["llm_error"])
        self.assertEqual(episode["intent"]["goal"], "Find the orchard survey")
        self.assertIsNone(episode["finish"])
        self.assertIn("web_search", [t["tool"] for t in episode["tools"]])
        # The operator's message may be unanswered: it stays in the inbox for the next cycle.
        self.assertTrue((self.root / "inbox" / "20261116-060000-from-operator.md").exists())
        self.assertEqual(episode["inbox_deferred"], ["20261116-060000-from-operator.md"])
        journal = self.state.journal.read_text()
        self.assertIn("My model call failed:", journal)
        self.assertIn("the cycle failed", journal)

    def test_bug241_entry_point_says_the_record_was_saved(self):
        failure = LLMError("LLM call to http://x failed after 900s: timed out")
        failure.outcome = agent.Outcome(None, "llm_error", 3)
        with mock.patch.object(instance_main.live, "run", side_effect=failure), \
                mock.patch.object(instance_main.config_mod, "load", return_value=self.cfg), \
                mock.patch.object(instance_main.recovery, "run", return_value=0), \
                mock.patch.object(instance_main.pausing, "install"):
            code = instance_main.main(["live"])
        self.assertEqual(code, 1)
        import sys
        self.assertIn("live failed, its record saved: LLM call", sys.stderr.getvalue())

    def test_cut_off_answer_still_saves_nothing(self):
        """llm.py's rule stands: a reply cut off twice is no answer, and the caller saves nothing of it."""
        self.fake_broker(lambda name, **args: "ok")
        fake, client = self.model([{"content": "x", "finish_reason": "length"}] * 2)
        with self.assertRaises(Truncated):
            live.run(self.cfg, self.state, client)
        self.assertIsNone(self.state.last_episode())


if __name__ == "__main__":
    unittest.main()
