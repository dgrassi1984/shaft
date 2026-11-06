"""Plan-channel tool continuations must not spend the instance's model-turn budget."""
from __future__ import annotations

import copy
import io
import json
import threading
import unittest
from unittest import mock

from supervisor import cloudllm, planchannel
from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from shaft import agent, pausing
from shaft.llm import Client, Reply
from shaft.tools import Toolbox, _spec


class ModelTurnsTest(InstanceFixture, unittest.TestCase):
    def run_flags(self, flags, limit, *, pause_at=None):
        channel = planchannel.Channel.__new__(planchannel.Channel)
        channel._cv = threading.Condition()
        channel._closed = False
        seen, selected = [], []

        def select(body):
            seen.append(copy.deepcopy(body))
            if pause_at is not None and len(seen) == pause_at:
                raise pausing.Paused()
            i = len(selected)
            step = planchannel.Step("codex/fake", "anchor")
            step.calls = [{"id": str(i), "type": "function", "function": {"name": "tick", "arguments": "{}"}}]
            step.finish = "tool_calls"
            step.usage_received = not flags[i]
            step.event.set()
            selected.append(step)
            return step

        channel._select = select

        def call(messages, tools=None):
            data = channel.complete({"messages": messages, "tools": tools})
            reply = Reply("")
            Client._read_stream(io.BytesIO(cloudllm.as_sse(data)), reply, None, None)
            return reply

        out = agent.run(None, [{"role": "user", "content": "work"}], Toolbox(self.root),
                        max_tokens=100, purpose="test", max_steps=limit, call=call,
                        controls={"tick": agent.Control(_spec("tick", "tick", {}), lambda _: "ok", repeatable=True)},
                        wrap_up="Finish now.")
        return out, seen

    def test_twelve_codex_replies_spend_three_model_turns(self):
        out, seen = self.run_flags([True, True, True, False] * 3, 3)
        self.assertEqual((out.steps, out.ended_by, len(seen), len(out.transcript)), (3, "steps", 12, 12))
        ledgers = [m["content"] for b in seen for m in b["messages"][-1:] if m["role"] == "tool"]
        for n in (1, 2, 3):
            self.assertTrue(any(f"step {n} of 3" in s for s in ledgers))
        self.assertEqual(sum(m["content"].startswith("[supervisor] You have")
                             for m in seen[-1]["messages"] if m["role"] == "user"), 1)
        self.assertEqual(out.wrapped_up, "steps")

    def test_provider_without_same_turn_flag_counts_each_reply(self):
        calls = []
        def call(messages, tools=None):
            calls.append(1)
            return Reply("", tool_calls=[{"id": str(len(calls)), "name": "tick", "arguments": "{}"}])
        out = agent.run(None, [], Toolbox(self.root), max_tokens=100, purpose="test", max_steps=3, call=call,
                        controls={"tick": agent.Control(_spec("tick", "tick", {}), lambda _: "ok", repeatable=True)})
        self.assertEqual((out.steps, len(calls)), (3, 3))

    def test_eleven_flagged_replies_force_one_counted_step(self):
        out, seen = self.run_flags([True] * 11, 1)
        self.assertEqual((out.steps, len(seen)), (1, 11))

    def test_pause_redoes_continuation_without_spending_step(self):
        with mock.patch.object(pausing, "wait_while_paused"):
            out, seen = self.run_flags([True, False], 1, pause_at=2)
        self.assertEqual((out.steps, len(seen), len(out.transcript)), (1, 3, 2))

    def test_warning_counts_model_turns_including_current_continuation(self):
        out, seen = self.run_flags([False] * 8 + [True, False, False, False], 11)
        for body in seen[8:10]:
            self.assertIn("3 turns left", body["messages"][-1]["content"])
        self.assertEqual(out.steps, 11)

    def test_same_turn_flag_parsed_from_nonstream_reply(self):
        data = {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "1", "function": {"name": "tick", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}],
                "shaft_same_turn": True}
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
            reply = Client("http://fake/v1", "m", "", 1, self.root / "usage.jsonl").complete([], 100, "test")
        self.assertIs(reply.same_turn, True)
        self.assertEqual(reply.tool_calls[0]["name"], "tick")
