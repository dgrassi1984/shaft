"""A message the operator sends while the instance is still replying reaches that reply at its next step (the operator,
2026-10-16: "it should be injected immediately into it"; docs/plans/PLAN-interject.md). The agent side: the real talk.answer
against a scripted model, with the supervisor's .tmp/interject-<talk id>.jsonl written while the model works."""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TEMPLATE = Path(__file__).resolve().parents[2] / "template"
sys.path.insert(0, str(TEMPLATE))

from supervisor.tests.fakellm import FakeLLM  # noqa: E402

TALK = "api-0123456789"


class AnswerHearsInterjectionsTest(unittest.TestCase):
    def answer(self, replies, send_on_request: int):
        """Run one reply; when the model gets its `send_on_request`-th request, the operator sends "and skip Friday"."""
        from shaft import config, talk
        from shaft.llm import Client
        from shaft.state import State
        fake = FakeLLM(replies)
        self.addCleanup(fake.close)
        root = Path(tempfile.mkdtemp(prefix="shaft-interject-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "config.env").write_text(f'LLM_BASE_URL="{fake.url}"\nLLM_MODEL="m"\n')
        (root / ".tmp").mkdir()

        def operator_writes(body):
            if len(fake.requests) == send_on_request:   # 0: before the first call
                with (root / ".tmp" / f"interject-{TALK}.jsonl").open("a") as f:
                    f.write(json.dumps({"text": "and skip Friday"}) + "\n")

        fake.on_request = operator_writes
        if send_on_request == 0:
            operator_writes(None)
        cfg = config.load(root, environ={"SHAFT_ROOT": str(root)})
        state = State(root)
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"SHAFT_ROOT": str(root)}), mock.patch("sys.stdout", out):
            talk.answer(cfg, state, Client(fake.url, "m", "", 30, root / "u.jsonl"),
                        {"talk_id": TALK, "client": "Telegram",
                         "messages": [{"role": "user", "content": "check my calendar"}]})
        events = [json.loads(l) for l in out.getvalue().splitlines() if l.startswith("{")]
        episodes = [json.loads(l) for f in sorted((root / "memory").rglob("*.jsonl")) for l in f.read_text().splitlines()
                    if json.loads(l).get("talk_id") == TALK]
        return fake, events, episodes

    def test_a_message_sent_while_a_step_runs_is_in_the_next_model_call(self):
        fake, events, episodes = self.answer([{"tool_calls": [("list_files", {"path": "."})]},
                                              {"content": "Calendar checked, Friday skipped."}], send_on_request=1)
        self.assertEqual(len(fake.requests), 2)
        second = fake.requests[1]["messages"]
        roles = [m["role"] for m in second]
        self.assertEqual(roles[-3:], ["assistant", "tool", "user"])        # right after the tool result
        self.assertIn("and skip Friday", second[-1]["content"])
        self.assertIn("while you were still replying", second[-1]["content"])
        self.assertNotIn("and skip Friday", json.dumps(fake.requests[0]["messages"]))   # it was not there before
        self.assertEqual([e["text"] for e in events if e["type"] == "heard"], ["and skip Friday"])
        self.assertEqual([(e["role"], e["content"], e.get("interjected")) for e in episodes],
                         [("user", "check my calendar", None), ("user", "and skip Friday", True),
                          ("assistant", "Calendar checked, Friday skipped.", None)])   # the write-up has it, in order

    def test_a_message_after_the_last_step_is_left_for_the_next_reply(self):
        # The model was already writing its answer: nothing reads the message, so heard stays 0 and the client
        # (Telegram) answers it straight after — never silently dropped, never answered twice.
        fake, events, episodes = self.answer([{"content": "Calendar checked."}], send_on_request=1)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual([e for e in events if e["type"] == "heard"], [])
        self.assertNotIn("and skip Friday", [e["content"] for e in episodes])

    def test_a_message_there_before_the_first_step_is_in_the_first_call(self):
        # Sent while the reply still waited for the instance: the supervisor writes it as the agent starts.
        fake, events, _ = self.answer([{"content": "Both noted."}], send_on_request=0)
        self.assertEqual(len(fake.requests), 1)
        msgs = fake.requests[0]["messages"]
        self.assertEqual([m["role"] for m in msgs][-2:], ["user", "user"])
        self.assertIn("and skip Friday", msgs[-1]["content"])
        self.assertEqual(len([e for e in events if e["type"] == "heard"]), 1)


if __name__ == "__main__":
    unittest.main()
