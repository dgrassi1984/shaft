"""The template's model client, against the wire format of SHAFT's own model server (mlx_lm)."""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
import unittest.mock
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


class ToolAnnouncementTest(unittest.TestCase):
    def test_a_tool_call_being_written_is_reported(self):
        # Daniele: "when shaft calls a tool, the CLI doesn't show it until the tool has returned" — the server
        # buffers a tool call until complete; SHAFT's server now announces it while it is written.
        wire = (b": tool write_file 812\n\n: tool write_file 2304\n\n"
                b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n')
        seen = []
        Client._read_stream(io.BytesIO(wire), Reply(content="", max_tokens=10), None, None,
                            None, lambda name, n: seen.append((name, n)))
        self.assertEqual(seen, [("write_file", 812), ("write_file", 2304)])

    def test_an_error_mid_stream_is_an_error_not_an_empty_reply(self):
        from shaft.llm import LLMError
        wire = b'data: {"error": {"message": "Provider returned error", "code": 502}}\n\ndata: [DONE]\n\n'
        with self.assertRaises(LLMError) as ctx:
            Client._read_stream(io.BytesIO(wire), Reply(content="", max_tokens=10), None, None)
        self.assertIn("Provider returned error", str(ctx.exception))


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


class ReasoningEffortTest(unittest.TestCase):
    def test_medium_effort_reaches_the_local_server_and_a_typo_cannot_break_it(self):
        # Bonsai's template defaults to xhigh: on anything non-trivial it thought until the 4,096 cap (~4 min);
        # medium scaled with the request (117-1,234 tokens) and found a bug xhigh missed (_bench/RESULTS.md).
        from supervisor.tests.fakellm import FakeLLM
        from shaft import config
        fake = FakeLLM([{"content": "ok"}, {"content": "ok"}])
        self.addCleanup(fake.close)
        root = Path(tempfile.mkdtemp(prefix="shaft-cfg-"))
        (root / "config.env").write_text(f'LLM_BASE_URL="{fake.url}"\nLLM_MODEL="m"\n')
        cfg = config.load(root, environ={})
        Client.from_config(cfg).complete([{"role": "user", "content": "x"}], 100, "test")
        self.assertEqual(fake.requests[-1]["chat_template_kwargs"]["reasoning_effort"], "medium")
        err = io.StringIO()
        with unittest.mock.patch("sys.stderr", err):
            bad = config.load(root, environ={"LLM_REASONING_EFFORT": "maximum"})
        self.assertEqual(bad.LLM_REASONING_EFFORT, "medium")
        self.assertIn("must be one of medium, xhigh, low", err.getvalue())
        Client.from_config(cfg).complete([{"role": "user", "content": "x"}], 100, "test", think=False)
        self.assertNotIn("reasoning_effort", fake.requests[-1]["chat_template_kwargs"])


class RepeatGuardTest(unittest.TestCase):
    """shaft called the identical `run` eleven times in a row, copying its previous step whole (2026-10-13)."""

    def loop(self, replies):
        from shaft import agent
        from shaft.tools import Toolbox
        from supervisor.tests.fakellm import FakeLLM
        fake = FakeLLM(replies)
        self.addCleanup(fake.close)
        root = Path(tempfile.mkdtemp(prefix="shaft-steps-"))
        box = Toolbox(root)
        out = agent.run(Client(fake.url, "m", "", 30, root / "u.jsonl"), [{"role": "user", "content": "go"}], box,
                        max_tokens=100, purpose="test", max_steps=0)
        return out, box, fake, root

    def test_an_identical_call_in_a_row_is_not_run_and_the_third_ends_the_reply(self):
        same = {"tool_calls": [("list_files", {"path": "."})]}
        out, box, fake, _ = self.loop([same, same, same, same, {"content": "never reached"}])
        self.assertEqual(out.ended_by, "repeating")
        self.assertEqual(len(fake.requests), 3)                       # not a fourth model call
        ran = [e for e in box.log if e["result"] != "(repeat: not run)"]
        self.assertEqual(len(ran), 1)                                  # executed once
        told = [m["content"] for m in fake.requests[2]["messages"] if m["role"] == "tool"][-1]
        self.assertIn("not run again", told)

    def test_reading_again_after_a_write_is_not_a_repeat(self):
        read = {"tool_calls": [("read_file", {"path": "notes/a.md"})]}
        write = {"tool_calls": [("write_file", {"path": "notes/a.md", "content": "v2\n"})]}
        (Path(tempfile.gettempdir())).exists()
        out, box, fake, root = self.loop([write, read, write, read, {"content": "done"}])
        self.assertEqual(out.ended_by, "answer")
        self.assertNotIn("(repeat: not run)", [e["result"] for e in box.log])


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
        self.assertIn("the file now contains exactly what you wrote (2 lines)", out)   # shaft read the old note as
        self.assertIn("2 earlier line(s) are no longer in it", out)                      # "the write did not land"
        self.assertIn("history(path='notes/trend.md')", out)
        self.assertNotIn("gone", call("write_file", path="notes/new.md", content="fresh\n"))
        self.assertIn("append-only", call("append_file", path="memory/journal.md", content="x"))

    def test_config_env_is_changed_with_set_config_not_written(self):
        # Regression: the sandbox denies config.env writes (only the broker's set_config records an instance's
        # line, so the gate never reverts the operator's), and write_file("config.env") surfaced a bare OS
        # PermissionError that did not say what to do. The template refuses it first and names set_config.
        import json as _json
        from shaft.tools import Toolbox
        root = Path(tempfile.mkdtemp(prefix="shaft-tools-"))
        (root / "config.env").write_text('LLM_MODEL="bonsai"\n')
        t = Toolbox(root)
        for name, path in (("write_file", "config.env"), ("append_file", "config.env"),
                           ("write_file", "./config.env"), ("append_file", "notes/../config.env")):
            out = t.run(name, _json.dumps({"path": path, "content": 'LLM_MODEL="other"\n'}))
            self.assertTrue(out.startswith("ERROR: PermissionError"), (name, path, out))
            self.assertIn("set_config", out, (name, path))
        self.assertEqual((root / "config.env").read_text(), 'LLM_MODEL="bonsai"\n')


if __name__ == "__main__":
    unittest.main()
