"""Prompt instructions and tool schemas must describe the configured mode."""
import copy
import hashlib
import json
import unittest
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from shaft import config, live, prompts, recovery, sleep, talk
from shaft.llm import Reply


class PromptKnobsTest(InstanceFixture, unittest.TestCase):
    def capture_live(self):
        seen = []

        def complete(messages, *args, **kwargs):
            seen.append((copy.deepcopy(messages), copy.deepcopy(kwargs.get("tools"))))
            return Reply("", tool_calls=[{"id": "finish", "name": "finish_cycle", "arguments": json.dumps({
                "what_happened": "No work needed", "criterion_met": "no", "learned": "Nothing new",
                "delivered_via": "none"})}])

        live.run(self.cfg, self.state, mock.Mock(complete=complete))
        return seen[0]

    def capture_conversation(self, api):
        seen = []

        def stream(messages, *args, **kwargs):
            seen.append((copy.deepcopy(messages), copy.deepcopy(kwargs.get("tools"))))
            return Reply("I will handle the report.")

        client = mock.Mock(stream=stream)
        with mock.patch("sys.stdout"):
            if api:
                talk.answer(self.cfg, self.state, client, {
                    "talk_id": "orders-api", "client": "test app",
                    "messages": [{"role": "user", "content": "Send the report tomorrow."}]})
            else:
                talk.run(self.cfg, self.state, client, input_fn=mock.Mock(side_effect=[
                    "Send the report tomorrow.", "/exit"]), talk_id="orders-terminal")
        return seen[0]

    def test_conversation_prompt_queues_take_your_time_requests(self):
        """Guard the 2026-11-08 bug where take-your-time requests consumed a live reply instead of a cycle."""
        sentence = ("When the operator says to take your time, or anything that means the same, do it in your "
                    "next cycle: reply briefly with what you will do and call queue_for_next_cycle with the full task.")
        for api in (False, True):
            messages, tools = self.capture_conversation(api)
            self.assertIn(sentence, messages[0]["content"])
            self.assertIn("6 tool calls or 90 seconds", messages[0]["content"])
            self.assertIn("Plans above 20 calls or 300 seconds are refused", messages[0]["content"])
            self.assertNotIn("TALK_REPLY_MAX", messages[0]["content"])   # plain words, not knob names
            self.assertIn("plan_reply", [t["function"]["name"] for t in tools])

    def test_new_knobs_default_and_validate_both_states(self):
        self.assertEqual(self.cfg.DOUBTS, "on")
        self.assertEqual(self.cfg.LOOK_AHEAD, "off")
        for knob in ("DOUBTS", "LOOK_AHEAD"):
            self.assertEqual(config.CHOICES[knob], ("on", "off"))
            for value in ("on", "off"):
                with self.subTest(knob=knob, value=value):
                    cfg = config.load(self.root, environ={"LLM_BASE_URL": "http://unused/v1", "LLM_MODEL": "fake",
                                                          knob: value})
                    self.assertEqual(getattr(cfg, knob), value)

    def test_default_live_instruction_bytes_are_unchanged(self):
        # SHA-256 of LIVE, including PUSHBACK; last changed for the improvement list (#15).
        self.assertEqual(hashlib.sha256(prompts.live_instructions(self.cfg).encode()).hexdigest(),
                         "82e0ddc2b4eda298a96614d9cb1e5eb04a507e75ff0a563437f6168e22bad344")

    def test_doubts_on_keeps_the_original_optional_schema(self):
        _, tools = self.capture_live()
        finish = next(t["function"] for t in tools if t["function"]["name"] == "finish_cycle")
        self.assertEqual(finish["parameters"]["properties"]["doubts"], {
            "type": "array", "description": "optional steps you doubt, using 1-based tool-log indices; "
            "these never change criterion_met", "items": {"type": "object", "properties": {
                "step": {"type": "integer", "minimum": 1}, "what": {"type": "string"}},
                "required": ["step", "what"]}})
        self.assertNotIn("doubts", finish["parameters"]["required"])

    def test_doubts_off_removes_the_parameter_and_prompt_mentions(self):
        self.cfg.values["DOUBTS"] = "off"
        messages, tools = self.capture_live()
        self.assertNotIn("doubt", json.dumps([messages, tools]).lower())

    def test_look_ahead_on_adds_only_the_operator_question_to_step_one(self):
        self.cfg.values.update(LOOK_AHEAD="on", OPERATOR_NAME="Alex")
        messages, _ = self.capture_live()
        text = messages[1]["content"]
        line = (" Also ask: what will Alex need within 7 days that they have not asked for? "
                "Prepare it, then offer it in one line.")
        self.assertIn(line + "\n2. ", text)
        self.assertIn(line, text.split("\n1. ", 1)[1].split("\n2. ", 1)[0])
        self.assertEqual(prompts.live_instructions(self.cfg).replace(prompts.LOOK_AHEAD, ""), prompts.LIVE)

    def test_look_ahead_off_leaves_instructions_byte_identical(self):
        self.cfg.values["LOOK_AHEAD"] = "off"
        self.assertEqual(prompts.live_instructions(self.cfg), prompts.LIVE)
        messages, _ = self.capture_live()
        self.assertNotIn("within 7 days", json.dumps(messages))

    def test_noconfirm_and_clauses_do_not_tell_the_model_to_confirm(self):
        for mode in ("noconfirm", "clauses"):
            with self.subTest(mode=mode):
                self.cfg.values["CONFIDENCE_MODE"] = mode
                messages, tools = self.capture_live()
                self.assertNotIn("confirm_confidence", json.dumps([messages, tools]))
                self.assertNotIn("on your next turn, call", messages[1]["content"])

    def test_confirm_mode_keeps_the_original_confirmation_instruction(self):
        messages, _ = self.capture_live()
        self.assertIn(prompts.CONFIRM_INSTRUCTION, messages[1]["content"])

    def test_attention_off_does_not_ask_where_to_look(self):
        self.cfg.values["ATTENTION_SCHEMA"] = "off"
        messages, tools = self.capture_live()
        text = json.dumps([messages, tools])
        self.assertNotIn('"attention"', text)
        self.assertNotIn("where you expect to look", text)

    def test_workspace_off_keeps_attention_without_claiming_it_focuses_workspace(self):
        for mode in ("off", "competitive"):
            with self.subTest(mode=mode):
                self.cfg.values["WORKSPACE"] = mode
                messages, tools = self.capture_live()
                intent = next(t["function"] for t in tools if t["function"]["name"] == "declare_intent")
                attention = intent["parameters"]["properties"]["attention"]
                if mode == "off":
                    self.assertNotIn("workspace", json.dumps([messages, tools]).lower())
                    self.assertIn("the supervisor compares it with the calls you make", attention["description"])
                else:
                    self.assertEqual(attention, live.ATTENTION)

    def test_orders_use_commit_to_only_when_conversations_offer_it(self):
        for api in (False, True):
            for setting in ("on", "off"):
                with self.subTest(api=api, commitments=setting):
                    self.cfg.values["COMMITMENTS"] = setting
                    messages, tools = self.capture_conversation(api)
                    system = messages[0]["content"]
                    names = [t["function"]["name"] for t in tools]
                    self.assertEqual("commit_to" in names, setting == "on")
                    self.assertIn("When the operator orders something to be done later", system)
                    self.assertIn("criterion someone else can check", system)
                    self.assertIn("If blocked, tell the operator in one line.", system)
                    if setting == "on":
                        self.assertIn("record it with commit_to, with a due date", system)
                    else:
                        self.assertNotIn("commit_to", system)
                        self.assertIn("queue_for_next_cycle", names)
                        self.assertIn("order's exact words marked ORDER", system)

    def test_sleep_and_recovery_do_not_receive_conversation_orders_instruction(self):
        seen = []

        def answer(messages, *args, **kwargs):
            seen.append(copy.deepcopy(messages))
            return Reply("Journal entry.")

        sleep.run(self.cfg, self.state, mock.Mock(complete=answer))
        turns = [{"ts": "2026-11-06T09:00:00", "role": "user", "content": "Send the report tomorrow."},
                 {"ts": "2026-11-06T09:01:00", "role": "assistant", "content": "Understood."}]
        with mock.patch.object(recovery, "orphan_talks", return_value=[("orders", turns)]):
            self.assertEqual(recovery.run(self.cfg, self.state, mock.Mock(stream=answer)), 1)
        self.assertNotIn("When the operator orders something to be done later", json.dumps(seen))

    def test_disabled_features_are_absent_from_all_modes(self):
        self.cfg.values.update(CONFIDENCE_MODE="noconfirm", ATTENTION_SCHEMA="off", WORKSPACE="off", DOUBTS="off")
        requests = [self.capture_live(), self.capture_conversation(False), self.capture_conversation(True)]

        def answer(messages, *args, **kwargs):
            requests.append((copy.deepcopy(messages), copy.deepcopy(kwargs.get("tools"))))
            return Reply("Journal entry.")

        sleep.run(self.cfg, self.state, mock.Mock(complete=answer))
        self.assertGreater(recovery.run(self.cfg, self.state, mock.Mock(stream=answer)), 0)
        for messages, tools in requests:
            with self.subTest(mode=messages[0]["content"].split("Right now")[-1][:80]):
                text = json.dumps([messages, tools]).lower()
                for phrase in ("confirm_confidence", "where you expect to look", '"attention"', "workspace", "doubt"):
                    self.assertNotIn(phrase, text)

    def test_recovery_receives_only_capped_order_records_with_results_before_arguments(self):
        order = "Send the report tomorrow."
        result = "commitment c1106-0901-1 recorded"
        turns = [{"ts": "2026-11-06T09:00:00", "role": "user", "content": order},
                 {"ts": "2026-11-06T09:01:00", "role": "assistant", "content": "Understood.",
                  "tools": [{"tool": "read_file", "args": "{}", "result": "UNRELATED-LOG" * 2700},
                            {"tool": "commit_to", "args": json.dumps({"promise": order}) + "x" * 1000,
                             "result": result},
                            {"tool": "settle_commitment", "args": "{}", "result": "settled " + "s" * 1000},
                            {"tool": "queue_for_next_cycle", "args": "q" * 1000, "result": "Queued in inbox/order.md"}]}]
        seen = []

        def stream(messages, *args, **kwargs):
            seen.append(copy.deepcopy(messages))
            return Reply("ORDER: Send the report tomorrow. A commitment was recorded.")

        with mock.patch.object(recovery, "orphan_talks", return_value=[("orders", turns)]):
            self.assertEqual(recovery.run(self.cfg, self.state, mock.Mock(stream=stream)), 1)
        instruction = seen[0][1]["content"]
        self.assertIn(prompts.TALK_ORDERS, instruction)
        self.assertNotIn("No entry for a turn means", instruction)
        self.assertNotIn("UNRELATED-LOG", instruction)
        records = instruction.split("Conversation order records:\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(len(records), 3)
        self.assertEqual([len(line) for line in records], [300, 300, 300])
        self.assertTrue(records[0].startswith("commit_to: " + result + "; args: "))
        self.assertTrue(records[1].startswith("settle_commitment: settled "))
        self.assertTrue(records[2].startswith("queue_for_next_cycle: Queued in inbox/order.md; args: "))
        self.assertIn(order, instruction)
        self.assertIn(result, instruction)
        self.assertIn("ORDER: Send the report tomorrow.", self.state.journal.read_text())

    def test_recovery_without_order_records_preserves_transcript_bytes(self):
        turns = [{"ts": "2026-11-06T09:00:00", "role": "user", "content": "Send the report tomorrow."},
                 {"ts": "2026-11-06T09:01:00", "role": "assistant", "content": "Understood."}]
        expected = ("Transcript page:\n[2026-11-06T09:00:00] the operator: Send the report tomorrow.\n\n"
                    "[2026-11-06T09:01:00] me: Understood.\n\n")
        for tools in (None, "bad", 42, {}, [], [None, [], "bad", {}, {"tool": []}, {"tool": {}}],
                      [{"tool": "read_file", "args": "{}", "result": "UNRELATED-LOG" * 2700}]):
            with self.subTest(tools_present=bool(tools)):
                turns[1]["tools"] = tools
                seen = []

                def stream(messages, *args, **kwargs):
                    seen.append(copy.deepcopy(messages))
                    return Reply("Journal entry.")

                with mock.patch.object(recovery, "orphan_talks", return_value=[("orders", turns)]):
                    self.assertEqual(recovery.run(self.cfg, self.state, mock.Mock(stream=stream)), 1)
                self.assertEqual(len(seen), 1)
                transcript = seen[0][1]["content"].split("\n\nBefore the journal entry", 1)[0]
                self.assertEqual(transcript, expected)

    def test_missing_and_mock_knobs_keep_default_prompt_bytes_and_doubts_schema(self):
        for cfg in (object(), mock.Mock()):
            self.assertEqual(prompts.live_instructions(cfg), prompts.LIVE)
        for value in (None, mock.Mock()):
            self.cfg.values.pop("DOUBTS", None)
            if value is not None:
                self.cfg.values["DOUBTS"] = value
            _, tools = self.capture_live()
            finish = next(t["function"] for t in tools if t["function"]["name"] == "finish_cycle")
            self.assertIn("doubts", finish["parameters"]["properties"])

    def test_order_tool_names_are_exact_and_closed_commit_tool_falls_back_to_queue(self):
        self.assertNotIn("record it with commit_to", prompts.conversation_orders({"my_commit_to_plugin"}))
        self.assertIn("use queue_for_next_cycle", prompts.conversation_orders({"my_commit_to_plugin"}))
        self.assertIn("If commit_to has closed because the reply budget ran out, use queue_for_next_cycle in its place",
                      prompts.conversation_orders({"commit_to"}))

    def test_talk_summary_keeps_its_original_bytes_without_order_wording(self):
        self.assertEqual(prompts.TALK_SUMMARY, (
            "The conversation is over. Write the journal entry your future self will read about it, "
            "in first person: what was discussed, what you learned about {operator} or about "
            "yourself, and anything you committed to or want to follow up on. Only what actually "
            "happened in the conversation above. Plain markdown, no heading, under 250 words.") + prompts.SELF_CLAIMS)

    def test_recovery_orders_are_operator_only_including_fallback(self):
        for speaker in ("", "delegate"):
            turns = [{"ts": "2026-11-06T09:00:00", "role": "user", "content": "Send the report tomorrow.",
                      "speaker": speaker},
                     {"ts": "2026-11-06T09:01:00", "role": "assistant", "content": "Understood.",
                      "tools": [{"tool": "commit_to"}]}]
            seen = []

            def stream(messages, *args, **kwargs):
                seen.append(copy.deepcopy(messages))
                return Reply("<tool_call>not an entry" if len(seen) == 1 else "Journal entry.")

            with self.subTest(speaker=speaker), \
                 mock.patch.object(recovery, "orphan_talks", return_value=[("orders", turns)]):
                self.assertEqual(recovery.run(self.cfg, self.state, mock.Mock(stream=stream)), 1)
            self.assertEqual(len(seen), 2)
            for request in seen:
                self.assertEqual(prompts.TALK_ORDERS in request[1]["content"], not bool(speaker))

    def test_order_audit_distinguishes_failed_records_and_requires_checking_results(self):
        self.assertIn("A commit_to or queue_for_next_cycle line without ERROR counts as recorded; one with ERROR does not.",
                      prompts.TALK_ORDERS)
        self.assertIn("Other actions are not listed here, so check the conversation's text for a result before listing an order.",
                      prompts.TALK_ORDERS)
        self.assertIn("exact words marked ORDER, with a checkable criterion", prompts.TALK_ORDERS)


if __name__ == "__main__":
    unittest.main()
