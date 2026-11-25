"""Issues #133/#135: reverting loses reply-first pacing and corrections carried into sleep."""
from __future__ import annotations

import copy
import io
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

    def respond(self, replies, *, advance=None, talk_id=""):
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
        self.box, self.view = Toolbox(self.root), mock.Mock()
        self.box.commit_talk_id = talk_id
        with mock.patch.object(talk.time, "monotonic", side_effect=lambda: ticks[0]), \
                mock.patch.object(clock, "now", return_value=NOW):
            result = talk._respond(client, self.cfg, self.box,
                                   [{"role": "user", "content": "Investigate."}], self.view)
        self.assertIs(result, replies[-1])
        return seen

    def test_six_tool_calls_in_one_round_force_the_next_call_to_answer(self):
        """Guard the 2026-11-08 fixed 3-call budget bug and enforce the default 6-call limit."""
        seen = self.respond([Reply("", tool_calls=[tool("read_file", {"path": f"missing-{i}"}, str(i))
                            for i in range(6)]),
                             Reply("I will finish next cycle.")])
        self.assertTrue(seen[0][1])
        # only the queue is left: reverting to no tools at all makes queueing the rest impossible (#133)
        self.assertEqual([t["function"]["name"] for t in seen[1][1]], ["queue_for_next_cycle"])
        self.assertIn("Reply now", seen[1][0][-1]["content"])
        self.assertIn("what you will do next", seen[1][0][-1]["content"])

    def test_ninety_seconds_force_the_next_call_with_a_fake_clock(self):
        """Guard the 2026-11-08 bug where a 46-second browse could never fit in 45 seconds."""
        seen = self.respond([Reply("", tool_calls=[tool("read_file", {"path": "self/goals.md"})]),
                             Reply("Later.")], advance=[90, 0])
        self.assertEqual([t["function"]["name"] for t in seen[-1][1]], ["queue_for_next_cycle"])
        self.assertIn("Reply now", seen[-1][0][-1]["content"])

    def test_default_budget_allows_a_forty_six_second_page(self):
        """Guard the 2026-11-08 bug where one 46-second browse exhausted the default 45-second budget."""
        seen = self.respond([Reply("", tool_calls=[tool("read_file", {"path": "missing"})]),
                             Reply("Done.")], advance=[46, 0])
        self.assertIn("read_file", [t["function"]["name"] for t in seen[-1][1]])

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

    def test_queued_work_names_its_conversation_so_the_cycle_answers_there(self):
        """The 2026-11-10 case: a queued verdict was answered in the From feed, blind to the conversation. The note
        names the conversation (header and instruction), and the reply is told its answer comes back there."""
        seen = self.respond([Reply("", tool_calls=[tool("queue_for_next_cycle", {
            "task": "Read the whole call", "why": "Budget used"})]), Reply("I will come back here.")],
            talk_id="api-0a1b2c3d4e")
        [(_, body)] = live.read_inbox(self.state)
        self.assertIn("Conversation: api-0a1b2c3d4e — answer in it: send_message(conversation=api-0a1b2c3d4e)\n",
                      body)
        self.assertIn("read it first with read_thread", body)
        self.assertNotIn("report back to the operator", body)
        result = next(m["content"] for m in seen[-1][0] if m.get("role") == "tool")
        self.assertIn("its answer will come back in this conversation", result)
        self.assertIn("Conversation: api-0a1b2c3d4e", live.inbox_line(*live.read_inbox(self.state)[0]))

    def test_talk_and_api_prompts_show_defaults_and_disabled_budgets(self):
        self.assertEqual(config.FIELDS["TALK_REPLY_CALLS"], (int, 6))
        self.assertEqual(config.FIELDS["TALK_REPLY_S"], (int, 90))
        self.assertEqual(config.FIELDS["TALK_REPLY_MAX_CALLS"], (int, 20))
        self.assertEqual(config.FIELDS["TALK_REPLY_MAX_S"], (int, 300))
        for mode in (prompts.TALK_MODE, prompts.API_MODE):
            text = mode.format(operator="operator", app="app", since="") + prompts.reply_budget(self.cfg)
            self.assertIn("6 tool calls or 90 seconds", text)
            self.assertIn("queue_for_next_cycle", text)
        self.cfg.values.update(TALK_REPLY_CALLS=0, TALK_REPLY_S=0)
        self.assertIn("no calls or time limit", prompts.reply_budget(self.cfg))


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
                self.assertIn("6 tool calls or 90 seconds", call.args[0][0]["content"])
                self.assertIn("queue_for_next_cycle", [t["function"]["name"] for t in call.kwargs["tools"]])

    def test_planned_status_is_visible_in_plain_and_json_views(self):
        """Guard a reply plan disappearing when tool_end immediately replaces its transient status."""
        detail = "up to 8 calls / 200 s: Read sources"
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            view = talk.TerminalView(self.cfg, False)
            view.status("planned", detail)
            view.status("waiting for the model")
            self.assertIn("planned · " + detail, out.getvalue())
        out = io.StringIO()
        talk.JsonView(out=out).status("planned", detail)
        self.assertEqual(json.loads(out.getvalue()), dict(type="status", phase="planned", detail=detail))

    def test_plan_keeps_fourth_fifth_and_sixth_calls_available(self):
        """Guard the 2026-11-08 fixed 3-call budget that queued 37% of replies despite a feasible plan."""
        seen = self.respond([Reply("", tool_calls=[tool("plan_reply", {
            "calls": 8, "seconds": 200, "why": "Read six sources"})])] + [
            Reply("", tool_calls=[tool("read_file", {"path": f"missing-{i}"}, str(i))])
            for i in range(6)] + [Reply("Done.")], advance=[0, 46, 0, 0, 0, 0, 0, 0])
        for index in (4, 5, 6, 7):
            self.assertIn("read_file", [t["function"]["name"] for t in seen[index][1]])
        self.view.status.assert_any_call("planned", "up to 8 calls / 200 s: Read six sources")
        self.assertEqual(self.box.reply_plan, dict(calls=8, seconds=200, why="Read six sources",
                         planned=True, refused=0, used_calls=6, used_s=46, queued=False))

    def test_refused_ceiling_preserves_previous_budget(self):
        """Guard plans bypassing the ceiling or replacing the previous budget after refusal."""
        for calls, seconds, ceiling in ((21, 100, "TALK_REPLY_MAX_CALLS"), (8, 301, "TALK_REPLY_MAX_S")):
            with self.subTest(ceiling=ceiling):
                self.cfg.values["TALK_REPLY_CALLS"] = 1
                seen = self.respond([Reply("", tool_calls=[tool("plan_reply", {
                    "calls": calls, "seconds": seconds, "why": "Too much"})]),
                    Reply("", tool_calls=[tool("read_file", {"path": "missing"})]), Reply("Later.")])
                result = seen[1][0][-1]["content"]
                self.assertTrue(result.startswith("ERROR:"))
                self.assertIn(ceiling, result)
                self.assertIn("answer now with what you can, and queue_for_next_cycle the rest", result)
                self.assertEqual([t["function"]["name"] for t in seen[2][1]], ["queue_for_next_cycle"])
                self.assertEqual(self.box.reply_plan["calls"], 1)
                self.assertEqual(self.box.reply_plan["refused"], 1)
                self.assertFalse(self.box.reply_plan["planned"])

    def test_controls_do_not_spend_calls_and_replanning_does_not_reset_usage(self):
        """Guard logged controls consuming the calls budget or a revised plan resetting the reply's start."""
        self.cfg.values["TALK_REPLY_CALLS"] = 1
        seen = self.respond([Reply("", tool_calls=[tool("plan_reply", {
            "calls": 2, "seconds": 100, "why": "Two reads"})]),
            Reply("", tool_calls=[tool("queue_for_next_cycle", {"task": "Other work", "why": "Later"})]),
            Reply("", tool_calls=[tool("read_file", {"path": "one"})]),
            Reply("", tool_calls=[tool("plan_reply", {"calls": 1, "seconds": 90, "why": "Enough"})]),
            Reply("Done.")])
        for index in (1, 2, 3):
            self.assertIn("read_file", [t["function"]["name"] for t in seen[index][1]])
        self.assertEqual([t["function"]["name"] for t in seen[-1][1]], ["queue_for_next_cycle"])
        self.assertEqual(self.box.reply_plan["used_calls"], 1)
        self.assertTrue(self.box.reply_plan["queued"])

    def test_plan_time_counts_from_reply_start(self):
        """Guard plan_reply restarting the clock and extending the operator's wait beyond its plan."""
        seen = self.respond([Reply("", tool_calls=[tool("plan_reply", {
            "calls": 8, "seconds": 50, "why": "One browse"})]), Reply("Done.")], advance=[50, 0])
        self.assertEqual([t["function"]["name"] for t in seen[-1][1]], ["queue_for_next_cycle"])

    def test_plan_validates_positive_integers_and_reason(self):
        """Guard malformed plans silently disabling the budget or omitting the operator's explanation."""
        for args in ({"calls": True, "seconds": 20, "why": "x"},
                     {"calls": 1.5, "seconds": 20, "why": "x"},
                     {"calls": 0, "seconds": 20, "why": "x"},
                     {"calls": 1, "seconds": -1, "why": "x"},
                     {"calls": 1, "seconds": 20, "why": " "}):
            with self.subTest(args=args):
                seen = self.respond([Reply("", tool_calls=[tool("plan_reply", args)]), Reply("Done.")])
                self.assertTrue(seen[-1][0][-1]["content"].startswith("ERROR:"))
                self.assertEqual(self.box.reply_plan["refused"], 1)
                self.assertEqual(self.box.reply_plan["calls"], 6)

    def test_zero_disables_plan_ceilings(self):
        """Guard zero-valued ceilings refusing plans instead of disabling their own limit."""
        self.cfg.values.update(TALK_REPLY_MAX_CALLS=0, TALK_REPLY_MAX_S=0)
        self.respond([Reply("", tool_calls=[tool("plan_reply", {
            "calls": 21, "seconds": 301, "why": "Ceilings disabled"})]), Reply("Done.")])
        self.assertTrue(self.box.reply_plan["planned"])
        self.assertEqual(self.box.reply_plan["refused"], 0)

    def test_episode_records_reply_plan_in_terminal_and_api(self):
        """Guard losing reply-plan accounting between _respond and either conversation episode writer."""
        for api in (False, True):
            for planned in (False, True):
                with self.subTest(api=api, planned=planned):
                    replies = ([Reply("", tool_calls=[tool("plan_reply", {
                        "calls": 8, "seconds": 200, "why": "Read sources"})]),
                        Reply("", tool_calls=[tool("plan_reply", {
                            "calls": 21, "seconds": 200, "why": "Refuse"})]),
                        Reply("", tool_calls=[tool("read_file", {"path": "missing"}),
                            tool("queue_for_next_cycle", {"task": "Follow up", "why": "Later"})])]
                        if planned else []) + [Reply("Done.")]
                    client = mock.Mock()
                    client.stream.side_effect = replies
                    with mock.patch.object(talk.time, "monotonic", return_value=10):
                        if api:
                            with mock.patch.object(talk, "JsonView"):
                                talk.answer(self.cfg, self.state, client, {"messages": [
                                    {"role": "user", "content": "Hello"}]})
                        else:
                            with mock.patch.object(talk, "TerminalView") as view:
                                view.return_value.next_message.side_effect = ["Hello", None]
                                talk.run(self.cfg, self.state, client)
                    episode = [e for e in self.episodes() if e.get("role") == "assistant"][-1]
                    self.assertEqual(episode["reply_plan"], dict(calls=8 if planned else 6,
                        seconds=200 if planned else 90, why="Read sources" if planned else "",
                        planned=planned, refused=int(planned), used_calls=int(planned), used_s=0,
                        queued=planned))


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

    @mock.patch.object(sleep, "entry_problems", new=lambda *a, **k: [], create=True)   # not about the #13 entry check
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

    @mock.patch.object(sleep, "entry_problems", new=lambda *a, **k: [], create=True)   # not about the #13 entry check
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
