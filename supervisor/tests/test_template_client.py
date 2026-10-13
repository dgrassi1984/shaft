"""The template's model client, against the wire format of SHAFT's own model server (mlx_lm)."""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft.llm import Client, Reply  # noqa: E402


class StreamTest(unittest.TestCase):
    def test_prompt_reading_progress_from_mlx_keepalives(self):
        # Regression: mlx_lm reports prompt reading only as SSE comments (": keepalive N/M"); the client
        # skipped every non-"data:" line, so `shaft talk` sat on "waiting for the model" for ~30 s.
        wire = (b": keepalive 2048/8850\n\n: keepalive 8850/8850\n\n"
                b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        seen, reply = [], Reply(content="", max_tokens=10)
        Client._read_stream(io.BytesIO(wire), reply, None, None, lambda d, t: seen.append((d, t)))
        self.assertEqual(seen, [(2048, 8850), (8850, 8850)])
        self.assertEqual(reply.content, "hi")


class LeakedThinkingTest(unittest.TestCase):
    def test_thinking_closed_late_is_not_the_answer(self):
        # Regression: cut at its budget, Bonsai went on thinking in the answer and closed the block itself;
        # shaft's stored reply began with 4,435 chars of reasoning.
        wire = (b'data: {"choices":[{"delta":{"reasoning":"plan A"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"Hmm, let me tighten it.\\n</think>\\n\\nThe answer."}}]}\n\n'
                b"data: [DONE]\n\n")
        reply = Reply(content="", max_tokens=10)
        Client._read_stream(io.BytesIO(wire), reply, None, None)
        self.assertEqual(reply.content, "The answer.")
        self.assertIn("let me tighten it", reply.reasoning)
        self.assertTrue(reply.reasoning.startswith("plan A"))


class FollowupBudgetTest(unittest.TestCase):
    """Bonsai used its whole 4,096-token thinking budget on every tool round (~4.5 min each, 30+ min for one
    reply); after a tool result the client asks SHAFT's server for a smaller budget."""

    def bodies(self, messages, **client_kw):
        from supervisor.tests.fakellm import FakeLLM
        fake = FakeLLM([{"content": "ok"}])
        self.addCleanup(fake.close)
        Client(fake.url, "m", "", 30, Path(tempfile.mkdtemp()) / "u.jsonl", **client_kw).complete(messages, 100, "test")
        return fake.requests[-1]

    def test_after_a_tool_result_the_budget_is_smaller(self):
        msgs = [{"role": "user", "content": "x"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function",
                                                                     "function": {"name": "f", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "1", "content": "result"}]
        body = self.bodies(msgs, think_followup=1024)
        self.assertEqual(body["chat_template_kwargs"]["thinking_budget"], 1024)

    def test_first_call_and_cloud_get_the_full_budget(self):
        first = self.bodies([{"role": "user", "content": "x"}], think_followup=1024)
        self.assertNotIn("thinking_budget", first["chat_template_kwargs"])
        tool_last = [{"role": "tool", "tool_call_id": "1", "content": "r"}]
        self.assertNotIn("thinking_budget", self.bodies(tool_last, think_followup=0)["chat_template_kwargs"])
        self.assertNotIn("chat_template_kwargs", self.bodies(tool_last, think_followup=1024, broker_token="t"))


class ReasoningAcrossStepsTest(unittest.TestCase):
    def test_each_step_sees_the_thinking_of_the_steps_before(self):
        # Regression (2026-10-13): shaft repeated the same three tool rounds verbatim in one reply. The template
        # renders an earlier step's reasoning_content inside <think>; we sent none, so every step had an empty
        # plan and the model re-planned from scratch.
        import json as _json
        from shaft import agent
        from shaft.tools import Toolbox
        from supervisor.tests.fakellm import FakeLLM
        fake = FakeLLM([{"reasoning": "PLAN: read the agenda, then build a feed plugin",
                         "tool_calls": [("list_files", {"path": "."})]},
                        {"content": "done"}])
        self.addCleanup(fake.close)
        root = Path(tempfile.mkdtemp(prefix="shaft-steps-"))
        client = Client(fake.url, "m", "", 30, root / "u.jsonl")
        agent.run(client, [{"role": "user", "content": "build something"}], Toolbox(root),
                  max_tokens=100, purpose="test", max_steps=0)
        second = fake.requests[1]
        step = next(m for m in second["messages"] if m["role"] == "assistant")
        self.assertIn("PLAN: read the agenda", step.get("reasoning_content", ""))
        self.assertEqual(second["chat_template_kwargs"]["preserve_thinking"], False)


class FileToolsTest(unittest.TestCase):
    def test_a_running_record_is_not_silently_lost(self):
        # Regression: shaft rewrote notes/ai-misalignment-trend.md to add a dated entry and dropped the
        # earlier entries without noticing (the judge did). append_file adds; write_file now says what went.
        import json as _json
        from shaft.tools import Toolbox
        root = Path(tempfile.mkdtemp(prefix="shaft-tools-"))
        t = Toolbox(root)
        call = lambda name, **a: t.run(name, _json.dumps(a))   # noqa: E731
        call("write_file", path="notes/trend.md", content="# Trend\n- 10-12 entry A\n")
        self.assertTrue(call("append_file", path="notes/trend.md", content="- 10-13 entry B").startswith("appended"))
        self.assertEqual((root / "notes/trend.md").read_text(), "# Trend\n- 10-12 entry A\n- 10-13 entry B\n")
        out = call("write_file", path="notes/trend.md", content="# Trend\n- 10-13 entry C\n")
        self.assertIn("2 line(s) that were in the file are gone", out)
        self.assertIn("history(path='notes/trend.md')", out)
        self.assertNotIn("gone", call("write_file", path="notes/new.md", content="fresh\n"))
        self.assertIn("append-only", call("append_file", path="memory/journal.md", content="x"))


if __name__ == "__main__":
    unittest.main()
