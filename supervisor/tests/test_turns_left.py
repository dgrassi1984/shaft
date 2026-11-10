"""2026-10-27: tundra reached its ceiling with closing work unrun in 13 of 47 cycles.
Exercise the shared loop's actual model messages and tool log, including a late confirm-turn grant.
"""
from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import agent
from shaft.llm import Reply
from shaft.tools import Toolbox, _spec


class TurnsLeftTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-turns-left-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {"SHAFT_ROOT": str(self.root)}, clear=True))

    def drive(self, limit=16, *, wrap_up="Wrap up.", left=None, grow=False, ledger=True):
        requests = []
        controls = {"work": agent.Control(_spec("work", "work", {}), lambda a: "done", repeatable=True)}

        def call(messages, tools=None):
            requests.append((deepcopy(messages), tools))
            if not limit and len(requests) == 16:
                return Reply("answered")
            return Reply("", tool_calls=[{"id": str(len(requests)), "name": "work", "arguments": "{}"}])

        # Grant the confirm turn exactly when the old countdown would otherwise fall from four to three.
        maximum = (lambda: 16 + (len(requests) >= 13)) if grow else limit
        with mock.patch.object(agent, "time_left", return_value=left):
            outcome = agent.run(None, [{"role": "user", "content": "go"}], Toolbox(self.root),
                                max_tokens=100, purpose="talk" if not limit else "live", max_steps=maximum,
                                controls=controls, call=call, wrap_up=wrap_up, ledger=ledger)
        self.assertEqual(outcome.steps, len(requests))
        self.assertNotIn("[supervisor]", str(outcome.transcript))
        self.assertEqual(outcome.transcript[0]["result"], "done")
        return requests

    def warnings(self, requests):
        return {step: [line for message in messages for line in message["content"].splitlines()
                       if line.startswith("[supervisor]")]
                for step, (messages, _) in enumerate(requests, 1)
                if any("[supervisor]" in message["content"] for message in messages)}

    def test_last_three_steps_before_final_and_no_stale_warnings(self):
        requests = self.drive()
        self.assertEqual(self.warnings(requests), {
            13: ["[supervisor] 4 turns left: close what you opened."],
            14: ["[supervisor] 3 turns left: close what you opened."],
            15: ["[supervisor] 2 turns left: close what you opened."],
            16: ["[supervisor] You have one turn left. Wrap up."]})
        for messages, _ in requests[12:15]:
            self.assertEqual(messages[-1]["role"], "tool")
            self.assertIn("[runtime] Calls actually made", messages[-1]["content"])
        self.assertEqual(requests[-1][0][-1],
                         {"role": "user", "content": "[supervisor] You have one turn left. Wrap up."})
        self.assertEqual([t["function"]["name"] for t in requests[-1][1]], ["work"])

    def test_short_budget_warns_for_only_a_quarter(self):
        self.assertEqual(self.warnings(self.drive(8, ledger=False)), {
            6: ["[supervisor] 3 turns left: close what you opened."],
            7: ["[supervisor] 2 turns left: close what you opened."],
            8: ["[supervisor] You have one turn left. Wrap up."]})

    def test_callable_limit_is_read_before_each_warning(self):
        self.assertEqual(self.warnings(self.drive(grow=True)), {
            13: ["[supervisor] 4 turns left: close what you opened."],
            14: ["[supervisor] 4 turns left: close what you opened."],
            15: ["[supervisor] 3 turns left: close what you opened."],
            16: ["[supervisor] 2 turns left: close what you opened."],
            17: ["[supervisor] You have one turn left. Wrap up."]})

    def test_no_warning_without_both_limit_and_wrap_up(self):
        for limit, wrap_up in ((0, None), (0, "Wrap up."), (16, None)):
            with self.subTest(limit=limit, wrap_up=wrap_up):
                self.assertEqual(self.warnings(self.drive(limit, wrap_up=wrap_up)), {})

    def test_deadline_minutes_only_when_tighter_and_final_message_unchanged(self):
        warnings = self.warnings(self.drive(left=1800))
        self.assertEqual(warnings[13], ["[supervisor] 4 turns left (about 30 minutes): close what you opened."])
        self.assertEqual(warnings[14], ["[supervisor] 3 turns left (about 30 minutes): close what you opened."])
        self.assertEqual(warnings[15], ["[supervisor] 2 turns left: close what you opened."])
        self.assertEqual(self.warnings(self.drive(left=120)), {
            1: ["[supervisor] You have about 2 minutes left. Wrap up."]})
