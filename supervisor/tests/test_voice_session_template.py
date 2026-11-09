"""PLAN-voice M1, lane L6, the instance's side of the warm voice session (§7): `answer --loop` answers each request of
one conversation in one process, keeps one system prompt for all of them (so the supervisor's Codex channel keeps one
model session), and brings what changed between turns with each message instead."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import config, talk   # noqa: E402
from shaft.llm import Client     # noqa: E402
from shaft.state import State    # noqa: E402

from supervisor.tests import test_codexcli as codex_fixtures   # noqa: E402
from supervisor.tests.fakellm import FakeLLM                   # noqa: E402

TEMPLATE = Path(__file__).resolve().parents[2] / "template"
ASKED = [{"role": "user", "content": "Weather in Rome?"}]
AGAIN = ASKED + [{"role": "assistant", "content": "Sunny."}, {"role": "user", "content": "And tomorrow?"}]


def request(messages: list[dict]) -> dict:
    """What the API writes for a spoken turn of the web chat (api._chat)."""
    return {"talk_id": "api-voice1", "client": "shaft-web", "voice": True,
            "messages": [{"role": "system", "content": "Only your last message reaches the operator."}, *messages]}


def two_turns(held: dict | None) -> tuple[list[dict], list[dict]]:
    """Two spoken turns of one conversation, with a cycle's journal entry and an inbox message landing in the pause
    between them. Returns (the model request bodies, the talk episodes). held=None: two one-reply processes, as cold."""
    with contextlib.ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="shaft-warm-")))
        for folder in ("self", "memory", "inbox"):
            (root / folder).mkdir()
        cfg = config.load(root, environ={"LLM_BASE_URL": "http://127.0.0.1:1/v1", "LLM_MODEL": "fake"})
        stack.enter_context(mock.patch.dict(os.environ, {"SHAFT_ROOT": str(root)}, clear=True))
        stack.enter_context(mock.patch("sys.stderr", new_callable=io.StringIO))
        stack.enter_context(mock.patch("sys.stdout", new_callable=io.StringIO))
        state = State(root)
        state.append_journal("Cycle: pruned the reading list", "Dropped three stale links.")
        fake = FakeLLM([{"content": "Sunny."}, {"content": "Cloudy."}])
        stack.callback(fake.close)
        client = Client(fake.url, "m", "", 30, root / "usage.jsonl")
        talk.answer(cfg, state, client, request(ASKED), held)
        # the pause: a cycle runs (the instance is free between turns) and a message arrives
        state.append_journal("Cycle: watered the orchard", "The north row needed twice the water: ORCHARD-NOTE.")
        (root / "inbox" / "20261109-101500-from-wren.md").write_text("From: wren\n\nThe weather API is back.\n")
        talk.answer(cfg, state, client, request(AGAIN), held)
        episodes = [json.loads(line) for p in sorted(state.episodes_dir.glob("*.jsonl"))
                    for line in p.read_text().splitlines()]
        return fake.requests, [e for e in episodes if e.get("kind") == "talk"]


class WarmPromptTest(unittest.TestCase):
    def test_a_voice_session_keeps_one_system_prompt_and_says_what_changed_in_the_message(self):
        """Bug guarded: the system prompt rebuilt at every turn (its clock, the journal a cycle wrote in the pause, the
        unwritten conversations) changed the Codex session's key, so every spoken turn opened a new model session
        (M0: 5.8-9.8 s for a new session's first word against ~2-4 s); or, kept, the instance never heard what changed
        in the pause."""
        bodies, episodes = two_turns({})
        system = [b["messages"][0]["content"] for b in bodies]
        self.assertEqual(system[0], system[1])
        self.assertEqual(bodies[0]["tools"], bodies[1]["tools"])
        for dynamic in ("It is now", "ORCHARD-NOTE", "Since your last conversation"):
            self.assertNotIn(dynamic, system[0])
        first, second = bodies[0]["messages"][-1]["content"], bodies[1]["messages"][-1]["content"]
        self.assertIn("It is now", first)
        self.assertIn("Cycle: pruned the reading list", first)              # what I did since my last conversation
        self.assertTrue(first.endswith("[the operator]\n\nWeather in Rome?"), first[-80:])   # theirs, last
        self.assertIn("It is now", second)
        self.assertIn("ORCHARD-NOTE", second)                               # the cycle's entry, read again
        self.assertIn("20261109-101500-from-wren.md", second)               # the message that came in the pause
        self.assertNotIn("Cycle: pruned the reading list", second)          # said once, at the turn it was new
        self.assertIn("And tomorrow?", second)
        self.assertEqual(bodies[1]["messages"][1], {"role": "user", "content": "Weather in Rome?"})   # as resent
        self.assertEqual([(e["role"], e["content"]) for e in episodes if e["role"] == "user"],
                         [("user", "Weather in Rome?"), ("user", "And tomorrow?")])   # the operator's words only
        self.assertTrue(all(e.get("voice") is True for e in episodes))

    def test_one_reply_per_process_still_builds_its_prompt_each_time(self):
        """Control for the test above, and bug guarded: the warm mode leaking into the one-reply path (typed turns,
        Telegram), whose prompt must still carry what changed — there, in the system prompt."""
        bodies, _ = two_turns(None)
        system = [b["messages"][0]["content"] for b in bodies]
        self.assertNotEqual(system[0], system[1])
        self.assertIn("ORCHARD-NOTE", system[1])
        self.assertNotIn("It is now", bodies[1]["messages"][-1]["content"])
        self.assertEqual(bodies[1]["messages"][-1]["content"].split("\n\n")[0], "And tomorrow?")


class WarmCodexSessionTest(unittest.TestCase):
    """The same turns through the supervisor's Codex plan channel (the fake app-server of test_codexcli)."""
    setUp = codex_fixtures.ChannelTest.setUp
    use = codex_fixtures.ChannelTest.use
    requests = codex_fixtures.ChannelTest.requests
    rows = codex_fixtures.ChannelTest.rows

    def through_codex(self, bodies: list[dict]) -> None:
        self.use([[{"text": "Sunny."}], [{"text": "Cloudy."}]])
        for b in bodies:
            self.channel.complete({"model": "codex/gpt-6-astra", "messages": b["messages"], "tools": b["tools"]})

    def test_two_turns_of_a_voice_session_are_one_codex_session(self):
        """Bug guarded: a warm process whose broker (and Codex channel) stayed up still re-opened the model session at
        every turn, because planchannel._select keys it on (model, system prompt, tools): the session's second turn
        must extend the first, never rebuild it."""
        bodies, _ = two_turns({})
        self.through_codex(bodies)
        self.assertEqual(len(self.requests("thread/start")), 1)
        self.assertEqual((self.channel.stats["rebuilds"], self.channel.stats["extends"]), (1, 1))
        sent = self.requests("turn/start")[1]["params"]["input"][0]["text"]
        self.assertIn("And tomorrow?", sent)                     # only the new message went to the kept session
        self.assertNotIn("Weather in Rome?", sent)

    def test_rebuilt_prompts_open_a_new_codex_session_each_turn(self):
        """Control: the measuring instrument of the test above sees the bug when it is there (cold prompts)."""
        bodies, _ = two_turns(None)
        self.through_codex(bodies)
        self.assertEqual(len(self.requests("thread/start")), 2)


class AnswerLoopTest(unittest.TestCase):
    def test_each_request_is_answered_and_closed_by_its_own_marker(self):
        """Bug guarded: the supervisor waiting forever on a turn of the warm process (no end marker), a marker that
        arrives before the turn's events, or a request after an `end` line still answered."""
        answered = []

        def answer(cfg, state, client, req, held):
            answered.append((req["messages"][-1]["content"], id(held)))
            talk.JsonView().content_chunk(f"reply {len(answered)}")
            talk.JsonView().turn_end(1, "answer")

        lines = "".join(json.dumps(r) + "\n" for r in (request(ASKED), "not a request", request(AGAIN),
                                                        {"type": "end"}, request(ASKED)))
        out = io.StringIO()
        with mock.patch.object(talk, "answer", answer), mock.patch("sys.stdout", out):
            talk.answer_loop(None, None, None, io.StringIO(lines))
        events = [json.loads(line)["type"] for line in out.getvalue().splitlines()]
        self.assertEqual(events, ["content", "turn_end", "request_done", "content", "turn_end", "request_done"])
        self.assertEqual([a[0] for a in answered], ["Weather in Rome?", "And tomorrow?"])
        self.assertEqual(answered[0][1], answered[1][1])          # one `held` for the whole session

    def test_a_stop_ends_the_reply_not_the_process(self):
        """Bug guarded: the client's Stop (SIGINT to the agent) ending the warm process, or leaving its turn unclosed
        so the supervisor waited WARM_STOP_GRACE_S and then killed it."""
        calls = []

        def answer(cfg, state, client, req, held):
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt
            talk.JsonView().turn_end(1, "answer")

        out = io.StringIO()
        with mock.patch.object(talk, "answer", answer), mock.patch("sys.stdout", out):
            talk.answer_loop(None, None, None, io.StringIO(json.dumps(request(ASKED)) + "\n"
                                                           + json.dumps(request(AGAIN)) + "\n"))
        events = [json.loads(line)["type"] for line in out.getvalue().splitlines()]
        self.assertEqual(events, ["interrupted", "request_done", "turn_end", "request_done"])

    def test_the_real_agent_answers_two_turns_and_a_stop_in_one_process(self):
        """Bug guarded: `python -m shaft answer --loop` itself (not a stand-in) ending after one reply, dying on the
        client's Stop (SIGINT to its group), or answering the turn after a Stop with the stopped reply's leftovers."""
        import signal
        root = Path(tempfile.mkdtemp(prefix="shaft-loop-"))
        self.addCleanup(__import__("shutil").rmtree, root, True)
        for folder in ("self", "memory", "inbox", ".tmp"):
            (root / folder).mkdir()
        fake = FakeLLM([{"content": "x" * 400, "delay": 0.05}, {"content": "Cloudy."}])
        self.addCleanup(fake.close)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "SHAFT_ROOT": str(root), "LLM_BASE_URL": fake.url,
               "LLM_MODEL": "fake"}
        agent = subprocess.Popen([sys.executable, "-E", "-s", "-m", "shaft", "answer", "--loop"], cwd=TEMPLATE, env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 bufsize=1, start_new_session=True)
        self.addCleanup(lambda: agent.poll() is None and agent.kill())

        def turn(messages, stop_at_content=False) -> list:
            agent.stdin.write(json.dumps(request(messages)) + "\n")
            agent.stdin.flush()
            got = []
            while True:
                ev = json.loads(agent.stdout.readline())
                got.append((ev["type"], ev.get("text")))
                if ev["type"] == "content" and stop_at_content and len(got) < 50:
                    stop_at_content = False
                    os.killpg(agent.pid, signal.SIGINT)              # the client's Stop, as the API sends it
                if ev["type"] == "request_done":
                    return got
        first = [t for t, _ in turn(ASKED, stop_at_content=True)]
        self.assertEqual(first[-2:], ["interrupted", "request_done"])
        self.assertNotIn("turn_end", first)
        second = turn(AGAIN)
        self.assertEqual([t for t in second if t[0] == "content"], [("content", "Cloudy.")])
        self.assertEqual(second[-2:], [("turn_end", None), ("request_done", None)])
        self.assertIsNone(agent.poll())                                    # still up between turns
        agent.stdin.close()
        self.assertEqual(agent.wait(10), 0)                                # EOF ends it, cleanly
        self.assertEqual(fake.requests[0]["messages"][0]["content"], fake.requests[1]["messages"][0]["content"])

    def test_the_agent_accepts_the_loop_argument_the_api_sends(self):
        """Bug guarded (as test_api's for `answer`): the API starting `answer --loop` and the agent's parser rejecting
        it, every voice turn then failing with exit 2."""
        empty = tempfile.mkdtemp(prefix="shaft-args-")       # no config: it stops after parsing the arguments
        r = subprocess.run([sys.executable, "-E", "-s", "-m", "shaft", "answer", "--loop"], cwd=TEMPLATE,
                           env={"SHAFT_ROOT": empty, "PATH": "/usr/bin:/bin"}, input="", capture_output=True, text=True)
        for rejection in ("unrecognized arguments", "invalid choice"):
            self.assertNotIn(rejection, r.stderr)
        self.assertIn("config error", r.stderr)


if __name__ == "__main__":
    unittest.main()
