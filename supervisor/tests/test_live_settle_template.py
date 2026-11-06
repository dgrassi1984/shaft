"""Overdue commitments must be settleable during a live cycle, with the sleep evidence rule."""
from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from shaft import clock, commitments, live
from shaft.llm import Reply

NOW = datetime(2026, 11, 6, 12)
CID = "c1104-1000-1"
EVIDENCE = "2026-11-05T11:00:00"


def call(name, args):
    return Reply("", tool_calls=[{"id": name, "name": name, "arguments": json.dumps(args)}])


class LiveSettlementTest(InstanceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg.values.update(COMMITMENTS="on", CONFIDENCE_MODE="noconfirm", WORKSPACE="off", MAX_STEPS_LIVE=5)
        self.enterContext(mock.patch.object(clock, "now", return_value=NOW))
        self.enterContext(mock.patch.object(clock, "started", return_value=NOW))
        event = {"event": "made", "id": CID, "promise": "Send the report", "criterion": "The operator has it",
                 "made_to": "operator", "due": "2026-11-04T23:59:59", "source": "cycle", "at": "2026-11-04T10:00:00"}
        (self.root / commitments.FILE).write_text(json.dumps(event) + "\n")
        self.state.append_episode({"kind": "live", "ts": EVIDENCE,
                                   "tools": [{"tool": "send_message", "result": "delivered"}]})

    def cycle(self, evidence):
        replies = iter([
            call("declare_intent", {"goal": "Settle the report promise", "success_criterion": "Settled with evidence",
                                    "confidence": 90, "origin": "self"}),
            call("settle_commitment", {"id": CID, "status": "kept", "evidence": evidence}),
            call("finish_cycle", {"what_happened": "Reviewed the promise", "criterion_met": "partial",
                                  "learned": "Use the record", "delivered_via": "none"})])
        seen = []
        def complete(messages, *args, **kwargs):
            seen.append((copy.deepcopy(messages), kwargs.get("tools")))
            return next(replies)
        client = mock.Mock()
        client.complete.side_effect = complete
        live.run(self.cfg, self.state, client)
        episode = self.episodes()[-1]
        result = next(c for c in episode["tools"] if c["tool"] == "settle_commitment")
        return seen, episode, result

    def test_live_settle_commitment_uses_episode_and_logs_settlement(self):
        seen, episode, result = self.cycle(EVIDENCE + "#1")
        self.assertEqual(commitments.fold(self.root, NOW)[CID]["status"], "kept")
        self.assertTrue(result["result"].startswith("settled " + CID), result)
        system = seen[0][0][0]["content"]
        self.assertIn(CID, system)
        self.assertIn("overdue", system)
        self.assertIn("settle_commitment is available", system)
        self.assertIn("settle_commitment", [s["function"]["name"] for s in seen[0][1]])
        self.assertTrue(any("settle_commitment" in s for s in episode["completed"]))

    def test_live_kept_without_evidence_is_refused(self):
        _, _, result = self.cycle("")
        self.assertEqual(commitments.fold(self.root, NOW)[CID]["status"], "overdue")
        self.assertTrue(result["result"].startswith("ERROR: evidence must cite an episode"), result)

    def test_live_settlement_io_error_does_not_stop_cycle(self):
        with mock.patch.object(commitments, "settle", side_effect=OSError("unreadable")):
            _, episode, result = self.cycle(EVIDENCE)
        self.assertIn("ERROR: commitment not settled: OSError: unreadable", result["result"])
        self.assertEqual(episode["ended_by"], "control")
