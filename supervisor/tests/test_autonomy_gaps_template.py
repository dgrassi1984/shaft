"""Regression tests for the agent-side fixes of REVIEW-autonomy-2026-10-24, gaps A1–A8 (template commit 90ae18c).
Each test fails on the template before that commit; the comment in each says why."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import agent, broker, config, live, memory, prompts, recovery, sleep, talk
if not hasattr(live, "CONFIDENCE_OF"):
    raise unittest.SkipTest("template/ predates REVIEW-autonomy A1–A8 (dgrassi1984/shaft#83): merge it into template first")
from shaft import clock   # noqa: E402 — after the check: a template without it skips instead of failing to import

from shaft.llm import Client, LLMError
from shaft.state import State
from shaft.tools import Toolbox, _spec
from supervisor.tests.fakellm import FakeLLM


def record_json(text, verdicts=(), left_out=(), next_since=None):
    """What record(kind='judgments', since=..., format='json') returns (supervisor/broker.py `_record`)."""
    return json.dumps({"text": text, "verdicts": list(verdicts), "left_out": list(left_out), "next_since": next_since})


def verdict(ts, met="partial", reviewed="2026-10-01 02:00:00"):
    return {"episode_ts": ts, "judged_at": reviewed, "reviewed_at": reviewed, "criterion_met": met,
            "claim_supported": True, "failure_mode": "skipped a step", "difficulty": 2, "second_criterion_met": None}


class InstanceFixture:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-autonomy-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for folder in ("self", "memory", "inbox"):
            (self.root / folder).mkdir()
        self.state = State(self.root)
        self.cfg = config.load(self.root, environ={"LLM_BASE_URL": "http://127.0.0.1:1/v1",
                                                 "LLM_MODEL": "fake", "MAX_STEPS_SLEEP": "3"})
        # No inherited broker, replay clock, deadline, embedding service or shared folder.
        self.enterContext(mock.patch.dict(os.environ, {"SHAFT_ROOT": str(self.root)}, clear=True))
        self.enterContext(mock.patch("sys.stderr", new_callable=io.StringIO))

    def model(self, replies):
        fake = FakeLLM(replies)
        self.addCleanup(fake.close)
        return fake, Client(fake.url, "m", "", 30, self.root / "usage.jsonl")

    def episodes(self):
        return [json.loads(line) for p in sorted(self.state.episodes_dir.glob("*.jsonl"))
                for line in p.read_text().splitlines()]

    def fake_broker(self, call):
        self.enterContext(mock.patch.dict(os.environ, {"SHAFT_BROKER_URL": "http://fake.invalid"}))
        specs = [_spec(name, name, {}) for name in ("record", "history", "web_search", "send_message")]
        self.enterContext(mock.patch.object(broker, "tools", return_value=specs))
        # Patch the transport, not envelope(), so these tests also import on the parent.
        self.enterContext(mock.patch.object(broker, "_request", return_value={"envelope": None}))
        return self.enterContext(mock.patch.object(broker, "call", side_effect=call))


class OpenTalkContextTest(InstanceFixture, unittest.TestCase):
    """A1 (REVIEW-autonomy-2026-10-24): autonomous runs cannot see open conversations."""

    def check_context(self, run):
        old = "OLD-OPERATOR-TURN " + "x" * 9000
        last = "Please postpone the orchard survey until Friday."
        for role, content in (("user", old), ("assistant", "Understood."), ("user", last)):
            self.state.append_episode({"kind": "talk", "talk_id": "open-orchard", "role": role,
                                       "content": content, "ts": "2026-10-01T08:00:00"})
        fake, client = self.model([{"content": "Done."}])
        with mock.patch.dict(os.environ, {"SHAFT_OPEN_TALKS": "open-orchard"}):
            self.assertEqual(recovery.run(self.cfg, self.state, client), 0)
            run(self.cfg, self.state, client)
        self.assertEqual(len(fake.requests), 1)
        system = fake.requests[0]["messages"][0]["content"]
        self.assertIn(last, system)
        tail = system.split("## Recent conversations I have not written up yet (transcripts)\n\n", 1)[1]
        tail = tail.split("\n\n", 1)[1]                            # after the paragraph that says what they are
        cut = "[... earlier lines cut ...]\n"
        self.assertTrue(tail.startswith(cut), tail[:80])          # starts at a line, not mid-sentence
        self.assertLessEqual(len(tail), recovery.AUTONOMOUS_CONTEXT_CHARS + len(cut))
        self.assertTrue(tail.endswith("the operator: " + last))
        self.assertNotIn("OLD-OPERATOR-TURN", tail)
        self.assertFalse(any(e["kind"] in ("talk_summary", "talk_recovered") for e in self.episodes()))
        journal = self.state.journal.read_text()
        self.assertNotIn(last, journal)
        self.assertNotIn("Conversation with", journal)

    def test_live_sees_capped_open_transcript_without_writing_it_up(self):
        # Parent live.run only builds system_prompt; it never adds unwritten_context.
        self.check_context(live.run)

    def test_sleep_sees_capped_open_transcript_without_writing_it_up(self):
        # Parent sleep.run likewise omits the open transcript from its system message.
        self.check_context(sleep.run)


class SleepOpenTalkGuardTest(InstanceFixture, unittest.TestCase):
    """A1 (REVIEW-autonomy-2026-10-24, Codex's review): in sleep, an open conversation is awareness only."""

    def test_durable_writes_copied_from_an_open_talk_are_refused(self):
        said = "Please postpone the orchard survey until Friday because the ground is still wet"
        for role, content in (("user", said), ("assistant", "Understood, I will wait.")):
            self.state.append_episode({"kind": "talk", "talk_id": "open", "role": role, "content": content})
        copied = "The operator wants me to postpone the orchard survey until Friday because the ground is still wet."
        fake, client = self.model([
            {"tool_calls": [("remember", {"fact": copied})]},
            {"tool_calls": [("write_file", {"path": "memory/people/the-operator.md", "content": copied})]},
            {"tool_calls": [("remember", {"fact": "Orchard surveys need dry ground."})]},
            {"content": "Done."}])
        self.cfg.values["MAX_STEPS_SLEEP"] = 5
        with mock.patch.dict(os.environ, {"SHAFT_OPEN_TALKS": "open"}):
            sleep.run(self.cfg, self.state, client)
        log = self.state.last_episode()["tools"]
        self.assertTrue(log[0]["result"].startswith("ERROR: open-conversation transcripts are awareness only"))
        self.assertTrue(log[1]["result"].startswith("ERROR: open-conversation"))
        self.assertEqual(log[2]["result"], "remembered")               # its own words pass
        facts = (self.root / "memory/facts.md").read_text()
        self.assertNotIn("postpone", facts)
        self.assertIn("dry ground", facts)
        self.assertFalse((self.root / "memory/people/the-operator.md").exists())
        self.assertIn("Awareness only", fake.requests[0]["messages"][0]["content"])


class SleepRecordTest(InstanceFixture, unittest.TestCase):
    """A2 (REVIEW-autonomy-2026-10-24): sleep lacks read-only access to judged outcomes."""

    def test_record_and_readonly_history_reach_model_and_refusals_are_logged(self):
        # Parent removes every remote tool and never fetches or prompts with judgments.
        calls = self.fake_broker(lambda name, **args: record_json("VERDICT: orchard criterion not met")
                                 if name == "record" else "VERDICT: orchard criterion not met")
        fake, client = self.model([
            {"tool_calls": [("history", {"path": "self/goals.md", "save_as": "notes/restored.md"})]},
            {"tool_calls": [("history", {"path": "self/goals.md"})]},
            {"content": "Reviewed."}])
        sleep.run(self.cfg, self.state, client)
        offered = {s["function"]["name"] for s in fake.requests[0]["tools"]}
        self.assertEqual(offered & {"record", "history", "web_search", "send_message"}, {"record", "history"})
        user = fake.requests[0]["messages"][1]["content"]
        self.assertIn("VERDICT: orchard criterion not met", user)
        self.assertIn("record", user.split("5. ", 1)[1].split("6. ", 1)[0])
        refusal = next(m["content"] for m in fake.requests[1]["messages"] if m["role"] == "tool")
        self.assertTrue(refusal.startswith("ERROR: history is read-only"), refusal)
        logged = self.state.last_episode()["tools"][0]
        self.assertEqual(logged["tool"], "history")
        self.assertEqual(logged["result"], refusal.split("\n\n[runtime]", 1)[0])
        self.assertFalse((self.root / "notes/restored.md").exists())
        self.assertEqual(calls.call_args_list, [
            mock.call("record", kind="judgments", limit=20, since="1970-01-01 00:00:00", format="json"),
            mock.call("history", path="self/goals.md")])
        results = [m["content"] for m in fake.requests[2]["messages"] if m["role"] == "tool"]
        self.assertIn("VERDICT: orchard criterion not met", results[-1])

    def test_marker_takes_next_since_only_after_a_finished_sleep(self):
        # Round 2 (Codex's review): the marker was the fetch time, which a verdict written in that same second, or a
        # first sleep's newest-first page, could pass for good. It is now the record's own next_since, or unchanged.
        marker = self.root / sleep.VERDICTS_REVIEWED
        for initial, next_since in ((None, "2026-10-01 03:59:58"), ("2026-09-30 04:00:00", None)):
            with self.subTest(initial=initial):
                marker.unlink(missing_ok=True)
                if initial:
                    marker.write_text(initial + "\n")
                calls = self.fake_broker(lambda name, **args: record_json("VERDICT: reviewed orchard",
                                                                          next_since=next_since))
                for finished in (False, True):
                    self.cfg.values["MAX_STEPS_SLEEP"] = 1
                    reply = {"content": "Reviewed."} if finished else {
                        "tool_calls": [("read_file", {"path": "self/goals.md"})]}
                    fake, client = self.model([reply])
                    if finished:
                        sleep.run(self.cfg, self.state, client)
                        self.assertEqual(marker.read_text(), (next_since or initial) + "\n")
                    else:
                        with self.assertRaises(LLMError):
                            sleep.run(self.cfg, self.state, client)
                        self.assertEqual(marker.exists(), initial is not None)
                    self.assertEqual(calls.call_args, mock.call("record", kind="judgments", limit=20,
                                                                since=initial or "1970-01-01 00:00:00", format="json"))
                    self.assertIn("VERDICT: reviewed orchard", fake.requests[0]["messages"][1]["content"])

    def test_bad_record_answer_leaves_marker_and_verdicts_alone(self):
        self.fake_broker(lambda name, **args: "not json")
        fake, client = self.model([{"content": "Reviewed."}])
        sleep.run(self.cfg, self.state, client)
        self.assertIn("could not be read", fake.requests[0]["messages"][1]["content"])
        self.assertFalse((self.root / sleep.VERDICTS_REVIEWED).exists())
        self.assertFalse((self.root / memory.VERDICTS).exists())


class SleepVerdictOverflowTest(InstanceFixture, unittest.TestCase):
    """A2 (REVIEW-autonomy-2026-10-24, review fixes): no verdict is skipped — not past a cut, not on a first sleep."""

    def test_first_sleep_starts_at_last_sleep_and_keeps_what_it_received(self):
        # Codex's review: with no marker the record was read newest first and the marker set to now, so verdicts
        # past the first page were never reviewed. The first sleep now starts at the last sleep in the journal.
        self.state.append_journal("Sleep", "Rested.", when=datetime(2026, 10, 1, 3, 0))
        rows = [verdict("2026-09-30T22:00:00"), verdict("2026-09-30T23:00:00", "no", "2026-10-01 03:30:00")]
        calls = self.fake_broker(lambda name, **args: record_json(
            "VERDICTS", rows, ["2026-09-30T21:00:00"], "2026-10-01 03:30:00"))
        for _ in range(2):   # the same answer twice: nothing is stored twice
            fake, client = self.model([{"content": "Reviewed."}])
            sleep.run(self.cfg, self.state, client)
        self.assertEqual(calls.call_args_list[0].kwargs["since"], "2026-10-01 03:00:00")
        self.assertEqual(calls.call_args_list[1].kwargs["since"], "2026-10-01 03:30:00")
        self.assertEqual((self.root / sleep.VERDICTS_REVIEWED).read_text(), "2026-10-01 03:30:00\n")
        kept = memory.verdict_rows(self.root)
        self.assertEqual([(r["episode_ts"], r.get("left_out", False)) for r in kept],
                         [("2026-09-30T22:00:00", False), ("2026-09-30T23:00:00", False), ("2026-09-30T21:00:00", True)])


class CycleRecallTest(InstanceFixture, unittest.TestCase):
    """A3 (REVIEW-autonomy-2026-10-24): past cycle goals and failed tools are not searchable."""

    def test_cycle_goal_and_error_are_searchable_without_finish_prose(self):
        # Parent corpus ignores kind=live, so neither query has a hit.
        self.state.append_episode({"kind": "live", "ts": "2026-09-30T12:00:00",
                                   "intent": {"goal": "Map the orchard", "success_criterion": "Count every tree"},
                                   "finish": {"criterion_met": "no", "what_happened": "FABULATEDHAPPENING",
                                              "learned": "FABULATEDLESSON"},
                                   "ended_by": "control", "steps": 3,
                                   "tools": [{"tool": "web_search", "result": "ERROR: quincequota exhausted"},
                                             {"tool": "web_search", "result": "(repeat: not run)"}]})
        docs = memory.corpus(self.root)
        self.assertEqual(len(docs), 1)
        source, chunk = docs[0]
        self.assertEqual(source, "cycle 2026-09-30T12:00:00")
        self.assertIn("Not judged yet", chunk)
        self.assertNotIn("my own claim", chunk)          # round 2: the judge's word, never my claim
        self.assertIn("Ended by: control; steps: 3", chunk)
        self.assertIn("Success criterion: Count every tree", chunk)
        self.assertIn("Flagged/refused call web_search: (repeat: not run)", chunk)
        for query, expected in (("orchard", "Goal: Map the orchard"),
                                ("quincequota", "ERROR: quincequota exhausted")):
            hits = memory.search(self.root, query)
            self.assertEqual([h.source for h in hits], [source])
            self.assertIn(expected, hits[0].text)
        for _, text in docs:
            self.assertNotIn("FABULATEDHAPPENING", text)
            self.assertNotIn("FABULATEDLESSON", text)
        with (self.root / memory.VERDICTS).open("a") as f:          # what a sleep received from the record
            f.write(json.dumps(verdict("2026-09-30T12:00:00", "partial")) + "\n")
        self.assertIn("Judge: criterion met partial; claim supported True; failure: skipped a step",
                      memory.corpus(self.root)[0][1])
        (self.root / memory.VERDICTS).write_text(json.dumps({"episode_ts": "2026-09-30T12:00:00", "left_out": True}) + "\n")
        self.assertIn("Not judged: left out by the judge's sampling", memory.corpus(self.root)[0][1])


class ConfidenceQuestionTest(InstanceFixture, unittest.TestCase):
    """A4 (REVIEW-autonomy-2026-10-24): confidence does not identify the scored question."""

    def test_intent_persists_question_and_tool_spec_explains_partial(self):
        # Parent has no confidence_of and asks for a "probability of success".
        fake, client = self.model([{"tool_calls": [("declare_intent", {
            "goal": "Count trees", "success_criterion": "All trees counted", "confidence": 73,
            "origin": "self"})]}, {"tool_calls": [("finish_cycle", {
                "what_happened": "Counted some", "criterion_met": "partial", "learned": "Need a map"})]}])
        live.run(self.cfg, self.state, client)
        episode = self.state.last_episode()
        self.assertEqual(episode["kind"], "live")
        self.assertEqual(episode["intent"]["confidence_of"], "criterion_met=yes")
        self.assertEqual(episode["intent"]["confidence"], 73)
        spec = next(s["function"] for s in fake.requests[0]["tools"] if s["function"]["name"] == "declare_intent")
        description = spec["parameters"]["properties"]["confidence"]["description"]
        self.assertNotIn("probability of success", description)
        self.assertIn("partial", description)


class OperationalEnvelopeTest(InstanceFixture, unittest.TestCase):
    """A5 (REVIEW-autonomy-2026-10-24): runs cannot see their operational limits."""

    def test_broker_envelope_success_absence_http_error_and_timeout(self):
        # Parent broker has no envelope endpoint client.
        self.assertIsNone(broker.envelope())
        env = {"calls_used": 2, "calls_max": 9}
        with mock.patch.dict(os.environ, {"SHAFT_BROKER_URL": "http://fake.invalid"}):
            with mock.patch.object(broker, "_request", return_value={"envelope": env}) as request:
                self.assertEqual(broker.envelope(), env)
                request.assert_called_once_with("/envelope", timeout=2)
            for error in (urllib.error.HTTPError("http://fake.invalid/envelope", 503, "down", {}, io.BytesIO(b"down")),
                          TimeoutError("timed out")):
                with self.subTest(error=type(error).__name__), \
                     mock.patch("urllib.request.urlopen", side_effect=error):
                    self.assertIsNone(broker.envelope())

    def test_malformed_envelope_never_breaks_a_cycle(self):
        # Grok's review of 90ae18c: a partial envelope raised KeyError while the prompt or ledger was built.
        with mock.patch.dict(os.environ, {"SHAFT_BROKER_URL": "http://fake.invalid"}):
            for bad in ({"calls_used": 1}, {"calls_used": None, "calls_max": 9}, [], "x", None):
                with self.subTest(bad=bad), mock.patch.object(broker, "_request", return_value={"envelope": bad}):
                    self.assertIsNone(broker.envelope())
        partial = {"calls_used": 1, "calls_max": 9, "cloud": {"spent_usd": 1.0}, "trials": {"started": 1},
                   "coder": "?", "probation": [{"commit": "abc"}, {"commit": "deadbeef99", "files": ["shaft/x.py"],
                                                                   "cycles_used": 2, "cycles_needed": 6}]}
        text = prompts.envelope_text(partial, None)
        self.assertIn("8 left", text)
        self.assertIn("change deadbeef (shaft/x.py): 2 of 6", text)
        self.assertNotIn("Cloud model today", text)

    def test_envelope_text_handles_missing_replay_and_null_optional_fields(self):
        # Parent prompts has no envelope formatter.
        self.assertEqual(prompts.envelope_text(None, None), "The supervisor gave no envelope.")
        self.assertIn("about 2 minutes", prompts.envelope_text(None, 120))
        env = {"calls_used": 2, "calls_max": 9, "cloud": None, "coder": None, "trials": None,
               "probation": None, "next_sleep": None, "next_wake": None}
        self.assertEqual(prompts.envelope_text(env, None), "Supervisor calls this run: 2 of 9 used (7 left).")
        # Round 2 (Codex's review): a replay must not be able to tell; even a stray replay key changes nothing.
        self.assertEqual(prompts.envelope_text({**env, "replay": True}, None), prompts.envelope_text(env, None))
        full = {**env, "cloud": {"spent_usd": 1, "cap_usd": 8}, "next_sleep": "2026-10-25 04:00"}
        self.assertNotIn("replay", prompts.envelope_text(full, 60).lower())

    def test_live_without_broker_still_runs_and_shows_envelope_section(self):
        # Parent runs, but the first user message has no operational-envelope section.
        fake, client = self.model([{"content": "No broker needed."}])
        live.run(self.cfg, self.state, client)
        self.assertEqual(self.state.last_episode()["ended_by"], "answer")
        user = fake.requests[0]["messages"][1]["content"]
        self.assertIn("## My operational envelope", user)
        self.assertIn("The supervisor gave no envelope.", user)

    def test_ledger_preserves_old_bytes_and_adds_available_calls(self):
        # Literal old output from the parent's call_ledger; parent rejects envelope/left_s.
        entries = [{"tool": "read_file", "args": '{"path": "missing"}', "result": "ERROR: missing"},
                   {"tool": "append_file", "args": '{"path": "notes/a.md"}', "result": "appended 1 chars"}]
        expected = ("[runtime] Calls actually made so far this cycle (step 2 of 12), from my tool log: "
                    "read_file (ERROR), append_file. File writes: append_file notes/a.md → appended 1 chars.")
        self.assertEqual(agent.call_ledger(entries, 2, 12, "live").encode(), expected.encode())
        shown = agent.call_ledger(entries, 2, 12, "live", envelope={"calls_used": 2, "calls_max": 9}, left_s=120)
        self.assertEqual(shown, expected + " Envelope: 7 of 9 supervisor calls left; about 2 minutes left.")


class TalkInboxTest(InstanceFixture, unittest.TestCase):
    """A6 (REVIEW-autonomy-2026-10-24): conversations leave handled messages in the inbox."""

    def check_archiving(self, path):
        for scenario in ("discarded", "read_error", "answered"):
            with self.subTest(scenario=scenario):
                (self.root / "inbox/read.md").write_text("Orchard message")
                (self.root / "inbox/unread.md").write_text("Unread message")
                fake, client = self.model([{"tool_calls": [("read_file", {"path": "inbox/read.md"})]},
                                           {"content": "Handled."}])
                real_stream = client.stream

                def stream(*args, **kwargs):
                    if scenario == "discarded" and fake.requests:
                        raise LLMError("model disconnected after reading")
                    return real_stream(*args, **kwargs)

                real_read = Toolbox._t_read_file

                def read(box, **args):
                    if scenario == "read_error":
                        raise OSError("inbox read failed")
                    return real_read(box, **args)

                with mock.patch.object(client, "stream", side_effect=stream), \
                     mock.patch.object(Toolbox, "_t_read_file", read), \
                     mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                    if path == "run":
                        inputs = iter(["Read my inbox", "/exit"])
                        talk.run(self.cfg, self.state, client, input_fn=lambda _: next(inputs), talk_id=scenario)
                    else:
                        talk.answer(self.cfg, self.state, client, {"talk_id": scenario, "client": "Test",
                                    "messages": [{"role": "user", "content": "Read my inbox"}]})
                archive = self.root / "memory/messages/read.md"
                self.assertEqual((self.root / "inbox/unread.md").read_text(), "Unread message")
                self.assertFalse((self.root / "memory/messages/unread.md").exists())
                if scenario == "answered":
                    self.assertFalse((self.root / "inbox/read.md").exists())
                    self.assertEqual(archive.read_text(), "Orchard message")
                else:
                    self.assertEqual((self.root / "inbox/read.md").read_text(), "Orchard message")
                    self.assertFalse(archive.exists())
                if scenario == "discarded":   # it was read (the view shows the call), then the reply was lost
                    self.assertRegex(output.getvalue(), r'read_file.*inbox/read.md')
                    self.assertIn("model disconnected after reading", output.getvalue())
                    self.assertFalse(any(e.get("talk_id") == scenario and e.get("role") == "assistant"
                                         for e in self.episodes()))
                else:
                    result = next(m["content"] for m in fake.requests[1]["messages"] if m["role"] == "tool")
                    self.assertTrue(result.startswith("ERROR: OSError: inbox read failed" if scenario == "read_error"
                                                      else "Orchard message"), result)

    def test_run_archives_only_successful_reads_from_kept_replies(self):
        # Parent talk.run never archives, so the answered case leaves read.md in inbox/.
        self.check_archiving("run")

    def test_answer_archives_only_successful_reads_from_kept_replies(self):
        # Parent talk.answer also leaves the successfully handled message in inbox/.
        self.check_archiving("answer")


class TalkPartialReadTest(InstanceFixture, unittest.TestCase):
    """A6 (REVIEW-autonomy-2026-10-24, review fix): a message read only in part is not handled."""

    def test_long_message_needs_reads_covering_all_of_it(self):
        # Grok's review of 90ae18c: read_file shows 6,000 characters, and a longer message was archived unread.
        from shaft.tools import MAX_RESULT_CHARS
        body = "start " + "y" * (MAX_RESULT_CHARS + 500) + " END"
        for reads, archived in (([0], False), ([0, MAX_RESULT_CHARS], True), ([MAX_RESULT_CHARS], False),
                                ([-100], False), ([0, -100], False)):   # Codex's review: a negative offset read
                                # as covering the start; text[-100:5900] is empty here
            with self.subTest(reads=reads):
                (self.root / "inbox/long.md").write_text(body)
                (self.root / "memory/messages/long.md").unlink(missing_ok=True)
                entries = [{"tool": "read_file", "args": json.dumps({"path": "inbox/long.md", "offset": o}),
                            "result": body[o:o + MAX_RESULT_CHARS]} for o in reads]
                talk.archive_read_inbox(self.state, entries)
                self.assertEqual((self.root / "memory/messages/long.md").exists(), archived)
                self.assertEqual((self.root / "inbox/long.md").exists(), not archived)


class GoalsGuardTest(InstanceFixture, unittest.TestCase):
    """A7 (REVIEW-autonomy-2026-10-24): write_file silently drops existing goals."""

    def test_goals_need_explicit_replace_but_dedicated_self_edits_work(self):
        # Parent RECORD_DIRS excludes self, so the first write succeeds and drops a goal.
        self.state.goals_md.write_text("# Goals\n- Count trees\n- Repair fence\n")
        self.state.append_journal("Evidence", "Checked every tree.", when=datetime(2026, 10, 1, 8))
        box = Toolbox(self.root)
        new = "# Goals\n- Count trees\n"
        args = {"path": "self/goals.md", "content": new}
        self.assertTrue(box.run("write_file", json.dumps(args)).startswith("ERROR:"))
        self.assertEqual(self.state.goals_md.read_text(), "# Goals\n- Count trees\n- Repair fence\n")
        self.assertTrue(box.run("write_file", json.dumps({**args, "replace": True})).startswith("wrote "))
        self.assertEqual(self.state.goals_md.read_text(), new)
        self.state.self_md.write_text("# Old self\nOld belief.\n")
        model = "# Self\n" + "I verify the tree count against my observations. " * 6
        self.assertEqual(box.run("update_self_model", json.dumps({"text": model})), "self-model updated")
        self.assertEqual(self.state.self_md.read_text(), model.strip() + "\n")
        trait = "- I check counts against observations [2026-10-01 08:00]"
        self.assertEqual(box.run("update_personality", json.dumps({"layer": "surface", "text": trait})),
                         "surface personality updated (1 traits)")
        self.assertIn(trait, (self.root / "self/personality.md").read_text())


class ReplayClockTest(InstanceFixture, unittest.TestCase):
    """A8 (REVIEW-autonomy-2026-10-24): prompt dates and sleep age ignore replay time."""

    def test_replay_now_and_sleep_age_with_wall_clock_fallbacks(self):
        # Parent uses datetime.now directly for both Now and last_sleep, ignoring SHAFT_NOW.
        self.state.append_journal("Sleep", "Rested.", when=datetime(2026, 10, 1, 3))
        for setting in ("2026-10-01T09:00:00", None, "not-a-date", "2026-10-01T09:00:00+00:00"):
            with self.subTest(setting=setting):
                os.environ.pop("SHAFT_NOW", None)
                if setting is not None:
                    os.environ["SHAFT_NOW"] = setting
                # Elapsed replay time starts at zero, independent of suite/import duration.
                with mock.patch("time.monotonic", return_value=100), \
                     mock.patch.object(clock, "STARTED", 100):
                    before = datetime.now()
                    fake, client = self.model([{"content": "Done."}])
                    live.run(self.cfg, self.state, client)
                    after = datetime.now()
                system = fake.requests[0]["messages"][0]["content"]
                now_text = system.split("## Now\n\n", 1)[1].splitlines()[0]
                if setting == "2026-10-01T09:00:00":   # round 2: what a replay writes is dated then too
                    self.assertIn("## 2026-10-01 09:00 — Autonomous cycle", self.state.journal.read_text())
                    self.assertEqual(self.state.last_episode()["ts"], "2026-10-01T09:00:00")
                if setting and setting.endswith("+00:00"):   # review fix: it raised TypeError in last_sleep
                    self.assertTrue(now_text.endswith(datetime(2026, 10, 1, 9).replace(
                        tzinfo=__import__("datetime").timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")))
                elif setting == "2026-10-01T09:00:00":
                    self.assertEqual(now_text, "Thursday 2026-10-01 09:00")
                    self.assertIn("2026-10-01 03:00 (6 h ago)", system)
                else:
                    self.assertIn(now_text, {before.strftime("%A %Y-%m-%d %H:%M"),
                                             after.strftime("%A %Y-%m-%d %H:%M")})


if __name__ == "__main__":
    unittest.main()
