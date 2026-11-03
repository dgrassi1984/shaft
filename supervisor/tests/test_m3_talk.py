"""Issues #133/#135: reverting loses reply-first pacing and corrections carried into sleep."""
from __future__ import annotations

import copy
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from shaft import clock, config, corrections, live, prompts, recovery, sleep, talk
from shaft.llm import Reply
from shaft.tools import Toolbox
from supervisor import core, corrections as correction_alerts, metrics, panel

NOW = datetime(2026, 10, 28, 14, 5)


def tool(name, args, cid="1"):
    return {"id": cid, "name": name, "arguments": json.dumps(args)}


class ReplyFirstTest(InstanceFixture, unittest.TestCase):
    """#133: the next call must reply after either budget, without cutting a running answer."""

    def respond(self, replies, *, advance=None):
        seen = []
        client = mock.Mock()
        ticks = [0]

        def stream(messages, *args, **kwargs):
            seen.append((copy.deepcopy(messages), kwargs.get("tools")))
            reply = replies[len(seen) - 1]
            if advance:
                ticks[0] += advance[len(seen) - 1]
            return reply

        client.stream.side_effect = stream
        with mock.patch.object(talk.time, "monotonic", side_effect=lambda: ticks[0]), \
                mock.patch.object(clock, "now", return_value=NOW):
            result = talk._respond(client, self.cfg, Toolbox(self.root),
                                   [{"role": "user", "content": "Investigate."}], mock.Mock())
        self.assertIs(result, replies[-1])
        return seen

    def test_three_tool_calls_in_one_round_force_the_next_call_to_answer(self):
        seen = self.respond([Reply("", tool_calls=[tool("read_file", {"path": path}, str(i))
                            for i, path in enumerate(("self/goals.md", "self/self.md", "memory/facts.md"))]),
                             Reply("I will finish next cycle.")])
        self.assertTrue(seen[0][1])
        # only the queue is left: reverting to no tools at all makes queueing the rest impossible (#133)
        self.assertEqual([t["function"]["name"] for t in seen[1][1]], ["queue_for_next_cycle"])
        self.assertIn("Reply now", seen[1][0][-1]["content"])
        self.assertIn("what you will do next", seen[1][0][-1]["content"])

    def test_forty_five_seconds_force_the_next_call_with_a_fake_clock(self):
        seen = self.respond([Reply("", tool_calls=[tool("read_file", {"path": "self/goals.md"})]),
                             Reply("Later.")], advance=[45, 0])
        self.assertEqual([t["function"]["name"] for t in seen[-1][1]], ["queue_for_next_cycle"])
        self.assertIn("Reply now", seen[-1][0][-1]["content"])

    def test_zero_disables_each_budget_independently(self):
        for calls, seconds, elapsed, rounds in ((0, 45, 0, 4), (3, 0, 100, 1), (0, 0, 100, 4)):
            with self.subTest(calls=calls, seconds=seconds):
                self.cfg.values.update(TALK_REPLY_CALLS=calls, TALK_REPLY_S=seconds)
                replies = [Reply("", tool_calls=[tool("read_file", {"path": f"missing-{i}"}, str(i))])
                           for i in range(rounds)] + [Reply("Done.")]
                seen = self.respond(replies, advance=[elapsed] * len(replies))
                self.assertTrue(all(tools for _, tools in seen))

    def test_a_running_answer_is_never_cut_at_the_time_budget(self):
        text = "An answer that finishes after the budget. " * 200
        seen = self.respond([Reply(text)], advance=[90])
        self.assertEqual(len(seen), 1)

    def test_queue_is_a_self_addressed_inbox_item_shown_in_the_next_live_prompt(self):
        self.respond([Reply("", tool_calls=[tool("queue_for_next_cycle", {
            "task": "Finish the orchard survey", "why": "Reply first"})]), Reply("Queued.")])
        inbox = live.read_inbox(self.state)
        self.assertEqual(len(inbox), 1)
        body = inbox[0][1]
        for text in (f"From: {self.cfg.NAME}", f"To: {self.cfg.NAME}", "Kind: queued from conversation",
                     "Date: 2026-10-28 14:05", "Finish the orchard survey", "Why: Reply first"):
            self.assertIn(text, body)
        client = mock.Mock()
        captured = []
        client.complete.side_effect = lambda ms, *a, **kw: (captured.append(copy.deepcopy(ms)) or Reply("Done."))
        live.run(self.cfg, self.state, client)
        # one line, its header but To and its opening (review E2/C4: the inbox was pasted whole), and the handle
        shown = captured[0][1]["content"]
        self.assertIn(live.inbox_line(inbox[0][0], body), shown)
        self.assertIn("read_file inbox/<file> shows one whole", shown)
        for text in (f"From: {self.cfg.NAME}", "Kind: queued from conversation", "Date: 2026-10-28 14:05",
                     "Finish the orchard survey", "Why: Reply first"):
            self.assertIn(text, shown.split("\n\n## ", 1)[0])

    def test_talk_and_api_prompts_show_budgets_and_zero_omits_them(self):
        self.assertEqual(config.FIELDS["TALK_REPLY_CALLS"], (int, 3))
        self.assertEqual(config.FIELDS["TALK_REPLY_S"], (int, 45))
        for mode in (prompts.TALK_MODE, prompts.API_MODE):
            text = mode.format(operator="operator", app="app", since="") + prompts.reply_budget(self.cfg)
            self.assertIn("3 tool calls or 45 seconds", text)
            self.assertIn("queue_for_next_cycle", text)
        self.cfg.values.update(TALK_REPLY_CALLS=0, TALK_REPLY_S=0)
        self.assertEqual(prompts.reply_budget(self.cfg), "")


    def test_terminal_and_api_entry_points_offer_queue_and_show_the_budget(self):
        for api in (False, True):
            with self.subTest(api=api):
                client = mock.Mock()
                client.stream.return_value = Reply("Done.")
                if api:
                    with mock.patch.object(talk, "JsonView"):
                        talk.answer(self.cfg, self.state, client, {"talk_id": "api", "messages": [
                            {"role": "user", "content": "Hello"}]})
                else:
                    with mock.patch.object(talk, "TerminalView") as view:
                        view.return_value.next_message.side_effect = ["Hello", None]
                        talk.run(self.cfg, self.state, client, talk_id="terminal")
                call = client.stream.call_args
                self.assertIn("3 tool calls or 45 seconds", call.args[0][0]["content"])
                self.assertIn("queue_for_next_cycle", [t["function"]["name"] for t in call.kwargs["tools"]])


class CorrectionsTest(InstanceFixture, unittest.TestCase):
    """#135: an operator correction stays open until an explicit, evidenced closure."""

    def record(self, **overrides):
        args = {"what_was_wrong": "Delayed replies", "what_operator_said": "Answer first",
                "when": NOW.isoformat(), "reopens": None, **overrides}
        return corrections.record(self.root, args, "conversation-1", NOW)

    def test_numbering_append_only_reopens_counts_and_contract(self):
        self.assertEqual(self.record(), "correction k1028-1405-1 recorded")
        prefix = (self.root / corrections.FILE).read_text()
        self.assertEqual(self.record(what_was_wrong="Repeated delay", reopens="k1028-1405-1"),
                         "correction k1028-1405-2 recorded")
        self.assertTrue((self.root / corrections.FILE).read_text().startswith(prefix))
        self.assertEqual(corrections.fold(self.root)["k1028-1405-2"]["reopens"], "k1028-1405-1")
        self.assertIn("reopened 1 of 2 in 7 days", corrections.block(self.root, NOW))
        self.assertEqual(corrections.fold(self.root), correction_alerts._fold((self.root / corrections.FILE).read_text()))
        self.assertTrue(self.record(when=(NOW + timedelta(days=8)).isoformat(),
                                    reopens="k1028-1405-1").startswith("ERROR"))

    def test_retried_writeup_does_not_duplicate_the_same_correction(self):
        self.record()
        self.assertEqual(self.record(), "correction k1028-1405-1 already recorded")
        self.assertEqual(len(corrections.fold(self.root)), 1)

    def test_open_correction_appears_in_sleep_and_live_and_survives_unclosed_sleep(self):
        self.record()
        for run in (sleep.run, live.run):
            client = mock.Mock()
            seen = []
            client.complete.side_effect = lambda ms, *a, **kw: (seen.append(copy.deepcopy(ms)) or Reply("Done."))
            run(self.cfg, self.state, client)
            self.assertIn("k1028-1405-1", seen[0][0]["content"])
            self.assertIn("Answer first", seen[0][0]["content"])
            self.assertEqual(corrections.fold(self.root)["k1028-1405-1"]["status"], "open")

    def test_close_requires_closed_by_and_evidence_and_is_offered_in_sleep(self):
        self.record()
        for args in ({"id": "k1028-1405-1"}, {"id": "k1028-1405-1", "closed_by": "fact"},
                     {"id": "k1028-1405-1", "closed_by": "other", "evidence": "fact memory/facts.md"}):
            self.assertTrue(corrections.close(self.root, args, NOW).startswith("ERROR"))
        client = mock.Mock()
        client.complete.side_effect = [Reply("", tool_calls=[tool("close_correction", {
            "id": "k1028-1405-1", "closed_by": "not_accepted",
            "evidence": "episode 2026-10-28T14:06:00: record shows a timely answer"})]), Reply("Closed.")]
        sleep.run(self.cfg, self.state, client)
        offered = client.complete.call_args_list[0].kwargs["tools"]
        self.assertIn("close_correction", [t["function"]["name"] for t in offered])
        it = corrections.fold(self.root)["k1028-1405-1"]
        self.assertEqual((it["status"], it["closed_by"]), ("closed", "not_accepted"))
        self.assertIn("episode 2026", it["evidence"])
        self.assertNotIn("k1028-1405-1", corrections.block(self.root, NOW))
        self.assertEqual(corrections.fold(self.root), correction_alerts._fold((self.root / corrections.FILE).read_text()))

    def test_ledger_cannot_be_overwritten_with_file_tools_and_read_errors_are_shown(self):
        self.record()
        box = Toolbox(self.root)
        result = box.run("write_file", json.dumps({"path": corrections.FILE, "content": "", "replace": True}))
        self.assertTrue(result.startswith("ERROR"))
        (self.root / corrections.FILE).write_text("broken\n")
        self.assertIn("corrections could not be read", corrections.block(self.root, NOW))

    def test_broken_ledger_does_not_crash_sleep_when_a_close_is_attempted(self):
        (self.root / corrections.FILE).write_text("broken\n")
        client = mock.Mock()
        seen = []
        replies = iter([Reply("", tool_calls=[tool("close_correction", {
            "id": "k1028-1405-1", "closed_by": "test", "evidence": "episode 2026-10-28T14:06:00"})]),
                        Reply("Ledger needs repair.")])
        def complete(ms, *a, **kw):
            seen.append(copy.deepcopy(ms))
            return next(replies)
        client.complete.side_effect = complete
        sleep.run(self.cfg, self.state, client)
        self.assertIn("corrections could not be read", seen[0][0]["content"])
        self.assertIn("ERROR: correction not closed", seen[1][-1]["content"])

    def test_writeup_does_not_attribute_another_speakers_corrections_to_operator(self):
        for role, text in (("user", "Answer first"), ("assistant", "Understood")):
            self.state.append_episode({"kind": "talk", "talk_id": "guest", "role": role,
                                       "content": text, "speaker": "Claude Code", "ts": NOW.isoformat()})
        client = mock.Mock()
        client.stream.return_value = Reply("Spoke with Claude Code.")
        self.assertEqual(recovery.run(self.cfg, self.state, client), 1)
        self.assertIsNone(client.stream.call_args.kwargs["tools"])
        self.assertEqual(corrections.fold(self.root), {})

    def test_writeup_extracts_corrections_from_all_pages_and_excludes_open_conversations(self):
        for role, text in (("user", "Answer first"), ("assistant", "x" * 25000), ("user", "Again, answer first"),
                           ("assistant", "Understood")):
            self.state.append_episode({"kind": "talk", "talk_id": "conversation-1", "role": role,
                                       "content": text, "ts": NOW.isoformat()})
        client = mock.Mock()
        seen = []
        def stream(ms, *a, **kw):
            seen.append((copy.deepcopy(ms), kw.get("tools")))
            if len(seen) == 1:
                return Reply("", tool_calls=[tool("record_correction", {
                    "what_was_wrong": "Delayed replies", "what_operator_said": "Answer first",
                    "when": NOW.isoformat(), "reopens": None})])
            return Reply("Recorded the correction.")
        client.stream.side_effect = stream
        with mock.patch.dict(os.environ, {"SHAFT_OPEN_TALKS": "conversation-1"}):
            self.assertEqual(recovery.run(self.cfg, self.state, client), 0)
        client.stream.assert_not_called()
        self.assertEqual(recovery.run(self.cfg, self.state, client), 1)
        self.assertIn("Answer first", seen[0][0][1]["content"])
        self.assertTrue(any("Again, answer first" in ms[1]["content"] for ms, _ in seen))
        self.assertEqual([t["function"]["name"] for t in seen[0][1]], ["record_correction"])
        self.assertEqual(corrections.fold(self.root)["k1028-1405-1"]["conversation"], "conversation-1")

    def test_panel_alert_after_more_than_n_live_cycles_and_never_for_broken_ledgers(self):
        self.record()
        self.enterContext(mock.patch.object(core, "INSTANCES", self.root.parent))
        self.enterContext(mock.patch.object(panel, "instance_names", return_value=[self.root.name]))
        self.enterContext(mock.patch.object(panel, "_log_failures", return_value=[]))
        self.enterContext(mock.patch.object(core, "long_pauses", return_value=[]))
        self.enterContext(mock.patch.object(panel, "CORRECTION_ALERT_CYCLES", 2))
        episodes = []
        self.enterContext(mock.patch.object(metrics, "live_episodes", side_effect=lambda name: episodes))
        cost = {"caps": [], "judge_today_total": 0, "judge_alert": 3}
        def alerts():
            return panel.alerts({}, NOW + timedelta(hours=1), cost)
        for n in range(4):
            episodes[:] = [{"kind": "live", "ts": (NOW + timedelta(minutes=i + 1)).isoformat()} for i in range(n)]
            actual = alerts()
            self.assertEqual(len(actual), int(n > 2))
            if actual:
                self.assertEqual(actual[0]["id"], f"correction:{self.root.name}:k1028-1405-1")
        with mock.patch.object(panel, "CORRECTION_ALERT_CYCLES", 0):
            self.assertEqual(alerts(), [])
        for text in ("broken\n", "[]\n", '{"id": []}\n'):
            (self.root / corrections.FILE).write_text(text)
            self.assertEqual(alerts(), [])
        with mock.patch.object(core, "safe_read", side_effect=PermissionError("denied")):
            self.assertEqual(alerts(), [])
        (self.root / corrections.FILE).unlink()
        self.assertEqual(alerts(), [])


if __name__ == "__main__":
    unittest.main()
