"""Regressions for same-turn closures and truthful mode tool lists."""
import copy
import json
import unittest
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from shaft import agent, commitments, corrections, exam, forecast, projects, prompts, recovery, surprises
from shaft.llm import Reply
from shaft.tools import Toolbox, _spec, completed


def reply(name, *, same_turn=False):
    return Reply("", tool_calls=[{"id": name, "name": name, "arguments": "{}"}], same_turn=same_turn)


class ReviewFollowupsTest(InstanceFixture, unittest.TestCase):
    def loop(self, replies, limit, **kwargs):
        seen = []
        replies = iter(replies)

        def call(messages, tools=None):
            seen.append(copy.deepcopy(messages))
            return next(replies)

        controls = {name: agent.Control(_spec(name, name, {}), lambda _: "ok",
                                       stops=name == "finish_cycle", repeatable=True)
                    for name in ("tick", "settle_commitment", "finish_cycle")}
        out = agent.run(None, [], Toolbox(self.root), max_tokens=100, purpose="test", max_steps=limit,
                        call=call, controls=controls, wrap_up="Finish now.", **kwargs)
        return out, seen

    def test_same_turn_finish_does_not_report_zero_steps(self):
        out, _ = self.loop([reply("finish_cycle", same_turn=True)], 1)
        self.assertEqual((out.steps, out.ended_by), (1, "control"))

    def test_yield_is_honoured_during_first_turn_continuations(self):
        with mock.patch.object(agent, "yield_requested", return_value=True):
            out, seen = self.loop([reply("tick", same_turn=True), reply("finish_cycle")], 5)
        self.assertIn("operator needs the model", seen[1][-1]["content"])
        self.assertEqual(out.wrapped_up, "yield")

    def test_extended_callable_budget_gets_a_new_final_wrap_up(self):
        limits = iter([1, 3, 3, 3])
        out, seen = self.loop([reply("tick", same_turn=True), reply("tick"), reply("tick"), reply("finish_cycle")],
                              lambda: next(limits))
        self.assertEqual(sum(m["content"].startswith("[supervisor] You have")
                             for m in seen[-1] if m["role"] == "user"), 2)
        self.assertEqual(out.wrapped_up, "steps")

    def test_yield_and_deadline_wrap_up_allow_same_turn_settlement_before_finish(self):
        for cause in ("yield", "deadline"):
            with self.subTest(cause=cause), \
                 mock.patch.object(agent, "yield_requested", return_value=cause == "yield"), \
                 mock.patch.object(agent, "time_left", side_effect=[None, None, 1, 1, 1]
                                   if cause == "deadline" else lambda: None):
                out, seen = self.loop([reply("tick"), reply("settle_commitment", same_turn=True),
                                       reply("finish_cycle")], 5)
                self.assertEqual((out.ended_by, len(seen)), ("control", 3))
                self.assertEqual([e["tool"] for e in out.transcript],
                                 ["tick", "settle_commitment", "finish_cycle"])
                self.assertEqual(out.wrapped_up, cause)

    def test_multipage_recovery_synthesis_does_not_advertise_correction_tool(self):
        turns = [{"ts": "2026-10-29T09:12:00", "role": "user", "content": "a"},
                 {"ts": "2026-10-29T09:13:00", "role": "assistant", "content": "b"}]
        seen = []

        def stream(messages, *args, **kwargs):
            seen.append((copy.deepcopy(messages), kwargs.get("tools")))
            return Reply("Journal entry.")

        with mock.patch.object(recovery, "orphan_talks", return_value=[("pages", turns)]), \
             mock.patch.object(recovery, "MAX_TRANSCRIPT_CHARS", 40):
            self.assertEqual(recovery.run(self.cfg, self.state, mock.Mock(stream=stream)), 1)
        self.assertEqual(len(seen), 3)
        self.assertIn("no tools", seen[-1][0][0]["content"])
        self.assertNotIn("only tool is record_correction", seen[-1][0][0]["content"])
        self.assertIsNone(seen[-1][1])

    def test_grants_do_not_advertise_tools_trimmed_out_of_sleep(self):
        grants = {"SHAFT_SHARED": "/shared", "SHAFT_MAIL": "mail", "SHAFT_GDRIVE": "drive",
                  "SHAFT_CALENDAR": "calendar"}
        operator = self.cfg.OPERATOR_NAME
        abilities = [("run", prompts.SHARED.format(operator=operator, path="/shared")),
                     ("mail_search", prompts.MAIL.format(operator=operator, grants="mail")),
                     ("drive_search", prompts.GDRIVE.format(operator=operator, grants="drive")),
                     ("calendar_events", prompts.CALENDAR.format(operator=operator, grants="calendar"))]
        with mock.patch.dict("os.environ", grants):
            for mode in (prompts.LIVE_MODE, prompts.TALK_MODE, prompts.SLEEP_MODE, prompts.EXAM_MODE):
                for tools in ("supervisor: record, history", "run, mail_search, drive_search, calendar_events",
                              *(tool for tool, _ in abilities)):
                    with self.subTest(mode=mode, tools=tools):
                        system = prompts.system_prompt(self.state, self.cfg, mode, tools)
                        for tool, ability in abilities:
                            self.assertEqual(ability in system, tool in tools)

    def test_describe_includes_extra_tools_without_changing_empty_extra_output(self):
        for box in (Toolbox(self.root), exam.ExamToolbox(self.root, "scratch")):
            with self.subTest(box=type(box).__name__):
                original = box.describe()
                box.extra = [projects.OPEN, corrections.CLOSE, commitments.SETTLE]
                self.assertEqual(box.describe(), original + "; extra: open_project, close_correction, settle_commitment")
                box.extra = []
                self.assertEqual(box.describe(), original)

    def test_forecast_does_not_count_live_settlement_as_supervisor_call(self):
        ep = {"tools": [{"tool": "settle_commitment"}, {"tool": "mail_search"}]}
        self.assertEqual(forecast.summarise([ep], self.root)["tool_calls"], 1)

    def test_withdrawal_is_reported_as_completed(self):
        entry = {"tool": "withdraw_held", "args": json.dumps({"id": "h1", "reason": "obsolete"}),
                 "result": "withdrawn held change h1: shaft/x.py; obsolete"}
        self.assertEqual(len(completed([entry], {})), 1)
        self.assertIn("withdraw_held", completed([entry], {})[0])

    def test_withdrawal_is_inward_in_template_mirror(self):
        self.assertTrue(surprises.inward({"observed": "withdraw_held accepted the request"}))
