"""Review rows 8 (I9/E4), 11, 23 (E8), 25 (C10): instance feedback and search."""
from __future__ import annotations

import io
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture, record_json, verdict
from shaft import agent, config, loopguard, memory, sleep
from shaft.llm import Client, Reply, Truncated
from shaft.tools import Toolbox


class SleepFeedbackTest(InstanceFixture, unittest.TestCase):
    def test_row_11_reads_past_three_pages_and_keeps_cursor_when_caught_up(self):
        pages = [record_json(f"VERDICT {n}", [verdict(f"2026-10-01T0{n}:00:00")],
                             next_since=f"2026-10-01 0{n}:00:00") for n in range(1, 6)]
        calls = self.fake_broker(mock.Mock(side_effect=pages + [record_json("")]))
        client = mock.Mock()
        client.complete.return_value = Reply("Reviewed.")
        sleep.run(self.cfg, self.state, client)
        judgments = [c for c in calls.call_args_list if c.kwargs.get("kind") == "judgments"]
        self.assertEqual(len(judgments), 6)
        self.assertIn("VERDICT 5", client.complete.call_args.args[0][1]["content"])
        self.assertEqual((self.root / sleep.VERDICTS_REVIEWED).read_text(), "2026-10-01 05:00:00\n")
        self.assertEqual(len(memory.verdict_rows(self.root)), 5)

    def full_pages(self, chars=24000):
        """An endless backlog of full sleep pages (supervisor/broker.py SLEEP_JUDGMENTS_BUDGET), cursor advancing."""
        def page(*a, since, **kw):
            n = int(since[-2:]) + 1 if since.startswith("2026-10-01") else 1
            return record_json(f"PAGE{n:02d} " + "v" * (chars - 7),
                               [verdict(f"2026-10-01T00:{n:02d}:00", reviewed=f"2026-10-01 00:00:{n:02d}")],
                               next_since=f"2026-10-01 00:00:{n:02d}")
        return page

    def test_row_11_bound_is_visible_and_next_sleep_resumes(self):
        self.cfg.values["SLEEP_VERDICT_CHARS"] = 50000   # two full pages fit, the third would not
        calls = self.fake_broker(self.full_pages())
        client = mock.Mock()
        client.complete.return_value = Reply("Reviewed.")
        sleep.run(self.cfg, self.state, client)
        prompt = client.complete.call_args.args[0][1]["content"]
        self.assertIn("cut short at SLEEP_VERDICT_CHARS=50000", sys.stderr.getvalue())
        self.assertIn("cut short at SLEEP_VERDICT_CHARS=50000", prompt)
        self.assertIn("PAGE02", prompt)
        self.assertNotIn("PAGE03", prompt)   # fetched but left for the next sleep, its verdict not recorded as read
        self.assertEqual(len(memory.verdict_rows(self.root)), 2)
        self.assertEqual((self.root / sleep.VERDICTS_REVIEWED).read_text(), "2026-10-01 00:00:02\n")
        calls.reset_mock()
        sleep.run(self.cfg, self.state, client)
        self.assertEqual(calls.call_args_list[0].kwargs["since"], "2026-10-01 00:00:02")
        self.assertIn("PAGE03", client.complete.call_args.args[0][1]["content"])

    def test_row_11_default_budget_fits_the_context_window_on_an_endless_backlog(self):
        # Grok's review: the earlier bound of 100 pages let a backlog of full 24,000-character pages build a
        # 2.4M-character prompt; the context error then failed every sleep and the marker never moved.
        calls = self.fake_broker(self.full_pages())
        client = mock.Mock()
        client.complete.return_value = Reply("Reviewed.")
        sleep.run(self.cfg, self.state, client)
        prompt = client.complete.call_args.args[0][1]["content"]
        block = prompt.split("## Verdicts the supervisor's judge gave since my last sleep")[1].split("\n## ")[0]
        self.assertLessEqual(len(block), self.cfg.SLEEP_VERDICT_CHARS + 300)
        self.assertLess(self.cfg.SLEEP_VERDICT_CHARS / 3, self.cfg.LLM_CONTEXT_TOKENS / 4)   # >=3 chars a token
        # Four full pages and their separators fit 120,000; the fifth is fetched, left for the next sleep, and the read stops.
        self.assertEqual(len([c for c in calls.call_args_list if c.kwargs.get("kind") == "judgments"]), 5)
        self.assertIn("PAGE04", block)
        self.assertNotIn("PAGE05", block)
        self.assertTrue((self.root / sleep.VERDICTS_REVIEWED).exists())

    def test_row_11_a_cursor_that_stands_still_ends_the_read(self):
        calls = self.fake_broker(lambda *a, since, **kw: record_json("SAME", next_since=since))
        client = mock.Mock()
        client.complete.return_value = Reply("Reviewed.")
        sleep.run(self.cfg, self.state, client)
        self.assertEqual(len([c for c in calls.call_args_list if c.kwargs.get("kind") == "judgments"]), 1)

    def test_row_11_budget_is_positive_config_and_described(self):
        cfg = config.load(self.root, environ={"LLM_MODEL": "fake", "LLM_BASE_URL": "broker",
                                             "SLEEP_VERDICT_CHARS": "0"})
        self.assertEqual(cfg.SLEEP_VERDICT_CHARS, 120000)
        self.assertIn("SLEEP_VERDICT_CHARS=120000 [default]", cfg.describe())

    def wire(self, reason, cut=True, stream=False):
        choice = {reason: "length" if cut else "stop"}
        if stream:
            choice["delta"] = {"content": "cut" if cut else "Consolidated."}
            return io.BytesIO(b"data: " + json.dumps({"choices": [choice]}).encode() + b"\n\ndata: [DONE]\n")
        choice["message"] = {"content": "cut" if cut else "Consolidated."}
        if cut:
            choice["message"]["tool_calls"] = [{"function": {"arguments": "{"}}]
        return io.BytesIO(json.dumps({"choices": [choice]}).encode())

    def test_row_8_sleep_retries_length_and_never_parses_cut_tool_calls(self):
        for reason in ("finish_reason", "stop_reason"):
            with self.subTest(reason=reason):
                client = Client("http://fake.invalid", "fake", "", 30, self.root / "usage.jsonl")
                with mock.patch("urllib.request.urlopen", side_effect=[self.wire(reason), self.wire(reason, False)]) as send:
                    entry = sleep.run(self.cfg, self.state, client)
                bodies = [json.loads(c.args[0].data) for c in send.call_args_list]
                self.assertEqual([b["max_tokens"] for b in bodies], [16384, 32768])
                self.assertIn("Consolidated.", entry)
                self.assertNotIn("cut", entry)
                self.assertEqual(self.state.last_episode()["tools"], [])
                self.assertIn("sleep: reply cut off at max_tokens=16384; retrying once with max_tokens=32768",
                              sys.stderr.getvalue())
        rows = [json.loads(line) for line in (self.root / "usage.jsonl").read_text().splitlines()]
        self.assertEqual([r["finish_reason"] for r in rows], ["length", "stop", "length", "stop"])

    def test_row_8_length_twice_does_not_finish_sleep_or_advance_feedback(self):
        self.fake_broker(lambda *a, **kw: record_json("VERDICT"))
        client = Client("http://fake.invalid", "fake", "", 30, self.root / "usage.jsonl")
        with mock.patch("urllib.request.urlopen", side_effect=[self.wire("stop_reason"), self.wire("stop_reason")]) as send:
            with self.assertRaises(Truncated):
                sleep.run(self.cfg, self.state, client)
        self.assertEqual(send.call_count, 2)
        self.assertFalse((self.root / sleep.VERDICTS_REVIEWED).exists())
        self.assertFalse(self.state.journal.exists())

    def test_row_8_stream_stop_reason_length_retries(self):
        client = Client("http://fake.invalid", "fake", "", 30, self.root / "usage.jsonl")
        with mock.patch("urllib.request.urlopen", side_effect=[self.wire("stop_reason", stream=True),
                                                               self.wire("stop_reason", False, stream=True)]) as send:
            reply = client.stream([], 100, "sleep")
        self.assertEqual(reply.content, "Consolidated.")
        self.assertEqual([json.loads(c.args[0].data)["max_tokens"] for c in send.call_args_list], [100, 200])

    @staticmethod
    def cut_call(stream, cut=True):
        """No text, a write_file call cut mid-arguments (or, cut=False, a plain answer)."""
        call = {"index": 0, "id": "c1", "function": {"name": "write_file", "arguments": '{"path": "notes/a.md", "te'}}
        if stream:
            delta = {"tool_calls": [call]} if cut else {"content": "Consolidated."}
            return io.BytesIO(b"data: " + json.dumps({"choices": [{"delta": delta}]}).encode() + b"\n\n"
                              + b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "length" if cut else "stop"}]}).encode()
                              + b"\n\ndata: [DONE]\n")
        message = {"content": "", "tool_calls": [call]} if cut else {"content": "Consolidated."}
        return io.BytesIO(json.dumps({"choices": [{"message": message, "finish_reason": "length" if cut else "stop"}]}).encode())

    def test_row_8_cut_tool_call_is_never_run_and_the_retry_keeps_thinking(self):
        # Grok's review: the stream kept a cut call and the non-stream path dropped it, so the same cut retried with
        # thinking on one path and without it on the other. A cut while writing a call came after the thinking (the
        # documented rule in shaft/llm.py): both paths retry with thinking, and neither runs the partial call.
        for stream in (False, True):
            with self.subTest(stream=stream):
                client = Client("http://fake.invalid", "fake", "", 30, self.root / f"usage-{stream}.jsonl")
                box = Toolbox(self.root)
                call = (lambda msgs, t=box.specs(): client.stream(msgs, 100, "live", tools=t)) if stream else None
                with mock.patch("urllib.request.urlopen", side_effect=[self.cut_call(stream),
                                                                       self.cut_call(stream, cut=False)]) as send, \
                     mock.patch.object(box, "_dispatch", side_effect=AssertionError("cut call run")) as dispatch:
                    outcome = agent.run(client, [{"role": "user", "content": "x"}], box, max_tokens=100,
                                        max_steps=3, purpose="live", ledger=False, call=call)
                first, second = [json.loads(c.args[0].data) for c in send.call_args_list]
                self.assertEqual((first["max_tokens"], second["max_tokens"]), (100, 200))
                self.assertNotIn("enable_thinking", second["chat_template_kwargs"])
                self.assertEqual(dispatch.call_count, 0)
                self.assertEqual((outcome.ended_by, outcome.reply.content), ("answer", "Consolidated."))
                self.assertFalse((self.root / "notes/a.md").exists())
                logged = [json.loads(l) for l in (self.root / f"usage-{stream}.jsonl").read_text().splitlines()]
                self.assertEqual([(r["finish_reason"], r["tool_calls"]) for r in logged],
                                 [("length", ["write_file"]), ("stop", [])])

    def test_row_8_cut_tool_call_twice_raises_and_returns_nothing(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                client = Client("http://fake.invalid", "fake", "", 30, self.root / "usage.jsonl")
                with mock.patch("urllib.request.urlopen", side_effect=[self.cut_call(stream), self.cut_call(stream)]):
                    with self.assertRaises(Truncated):
                        (client.stream if stream else client.complete)([], 100, "live")

    def test_row_8_cut_nameless_call_is_not_a_malformed_response(self):
        cut = {"choices": [{"message": {"content": "", "tool_calls": [{"function": {"arguments": "{"}}]},
                            "finish_reason": "length"}]}
        client = Client("http://fake.invalid", "fake", "", 30, self.root / "usage.jsonl")
        with mock.patch("urllib.request.urlopen", side_effect=[io.BytesIO(json.dumps(cut).encode()),
                                                               self.cut_call(False, cut=False)]):
            self.assertEqual(client.complete([], 100, "live").content, "Consolidated.")


class GuardDefaultTest(InstanceFixture, unittest.TestCase):
    def test_row_23_on_default_advises_effect_repeats(self):
        self.assertEqual(self.cfg.LOOP_GUARD, "on")
        guard = loopguard.Guard(self.cfg.LOOP_GUARD, self.cfg.LOOP_EFFECT_ROUNDS)
        notes = [guard.observe("read_file", '{"path":"missing"}', "ERROR: missing", n, {}) for n in range(1, 4)]
        self.assertEqual(notes, ["", "", "\n\n" + loopguard.NOTE])

    def test_row_23_exact_repeat_is_refused_in_cycle_with_on_default(self):
        self.assertEqual(self.cfg.LOOP_GUARD, "on")
        box = Toolbox(self.root)
        box.loop_guard = loopguard.Guard(self.cfg.LOOP_GUARD, 3)
        call = {"id": "r", "name": "read_file", "arguments": '{"path":"missing"}'}
        client = mock.Mock()
        client.complete.side_effect = [Reply("", tool_calls=[call])] * 3
        messages = []
        with mock.patch.object(box, "_dispatch", return_value="ERROR: missing") as dispatch:
            outcome = agent.run(client, messages, box, max_tokens=100, max_steps=5, purpose="live", ledger=False)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(outcome.ended_by, "repeating")
        self.assertIn("(not run again)", messages[3]["content"])
        self.assertEqual(box.log[-1]["result"], agent.NOT_RUN)


class IncrementalMemoryTest(InstanceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        (self.root / "notes").mkdir()
        self.a = self.root / "notes/a.md"
        self.b = self.root / "notes/b.md"
        self.a.write_text("orchard weather")
        self.b.write_text("orchard apple")
        self.embed = self.enterContext(mock.patch.object(memory, "_embed", side_effect=lambda texts, query:
                                                       [[1.0, 0.0] for _ in texts]))

    def test_row_25_unchanged_queries_do_not_read_rechunk_or_decode(self):
        expected = memory.search(self.root, "orchard")
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("unchanged file read")), \
             mock.patch.object(memory, "_chunks", side_effect=AssertionError("unchanged file chunked")), \
             mock.patch.object(memory.base64, "b64decode", side_effect=AssertionError("unchanged vector decoded")):
            self.assertEqual(memory.search(self.root, "orchard"), expected)
        self.assertEqual([c.kwargs["query"] for c in self.embed.call_args_list], [True, False, True])

    def test_row_25_only_changed_file_is_parsed_and_embedded_and_deletions_disappear(self):
        memory.search(self.root, "orchard")
        self.embed.reset_mock()
        self.a.write_text("orchard rain forecast")
        self.b.unlink()
        with mock.patch.object(memory, "_chunks", wraps=memory._chunks) as chunks, \
             mock.patch.object(memory.base64, "b64decode", wraps=memory.base64.b64decode) as decode:
            hits = memory.search(self.root, "orchard")
        self.assertEqual(chunks.call_args_list, [mock.call("orchard rain forecast")])
        self.assertEqual(decode.call_count, 0)
        self.assertEqual([h.text for h in hits], ["orchard rain forecast"])
        self.assertEqual(self.embed.call_args_list[-1], mock.call(["orchard rain forecast"], query=False))
        self.assertFalse((self.root / memory.INDEX).exists())
        with (self.root / memory.BINARY_INDEX).open("rb") as index:
            self.assertEqual(json.loads(index.readline())["version"], 2)
            self.assertEqual(len(index.read()), 24 + 2 * 4)  # one SHA1/length record and two floats
        self.b.write_text("orchard new note")
        self.assertEqual({h.text for h in memory.search(self.root, "orchard")}, {"orchard rain forecast", "orchard new note"})

    def test_row_25_verdict_changes_refresh_old_cycles_without_reading_episodes(self):
        self.state.append_episode({"kind": "live", "ts": "2026-10-01T01:00:00", "intent": {"goal": "orchard"}})
        self.assertIn("Not judged yet", " ".join(t for _, t in memory.corpus(self.root)))
        (self.root / memory.VERDICTS).write_text(json.dumps(verdict("2026-10-01T01:00:00")) + "\n")
        original = Path.read_text
        def read(path, *args, **kwargs):
            self.assertNotEqual(path.parent.name, "episodes")
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", read):
            self.assertIn("Judge: criterion met partial", " ".join(t for _, t in memory.corpus(self.root)))

    def test_row_25_file_size_invalidates_even_with_same_mtime(self):
        import os
        memory.corpus(self.root)
        stat = self.a.stat()
        self.a.write_text("orchard longer weather report")
        os.utime(self.a, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertIn(("notes/a.md", "orchard longer weather report"), memory.corpus(self.root))

    def test_row_25_fact_ages_refresh_on_next_day(self):
        (self.root / "memory/facts.md").write_text("- [2026-10-01] orchard (src: operator)\n")
        with mock.patch.object(memory.clock, "now", return_value=datetime(2026, 10, 2)):
            self.assertIn("1 days old", memory.corpus(self.root)[0][0])
        with mock.patch.object(memory.clock, "now", return_value=datetime(2026, 10, 3)):
            self.assertIn("2 days old", memory.corpus(self.root)[0][0])


    def test_row_25_loaded_vectors_decode_once_and_roots_do_not_mix(self):
        """Loading the new binary cache must not decode vectors again on an unchanged query."""
        memory.search(self.root, "orchard")
        memory._vector_root = None   # simulate the next process loading the disk cache
        self.embed.reset_mock()
        with mock.patch.object(memory, "array", wraps=memory.array) as decode:
            expected = memory.search(self.root, "orchard")
            self.assertEqual(decode.call_count, 2)
            self.assertEqual(memory.search(self.root, "orchard"), expected)
            self.assertEqual(decode.call_count, 2)
        self.assertTrue(all(c.kwargs["query"] for c in self.embed.call_args_list))
        other = self.root / "other"
        (other / "notes").mkdir(parents=True)
        (other / "notes/only.md").write_text("orchard different root")
        self.assertEqual([h.text for h in memory.search(other, "orchard")], ["orchard different root"])
        self.assertEqual(memory.search(self.root, "orchard"), expected)

    def test_row_25_surprises_expire_without_a_file_change(self):
        from shaft import surprises
        at = datetime(2026, 10, 1, 12)
        self.state.append_episode({"kind": "live", "ts": at.isoformat(), "finish": {"surprises": [
            {"expected": "orchard open", "observed": "orchard closed", "so_what": "check hours"}]}})
        expires = at + timedelta(days=surprises.EXPIRE_DAYS)
        with mock.patch.object(memory.clock, "now", return_value=expires - timedelta(seconds=1)):
            docs = memory.corpus(self.root)
        self.assertIn("Status: open", next(text for source, text in docs if source.startswith("surprise ")))
        with mock.patch.object(memory.clock, "now", return_value=expires + timedelta(seconds=1)):
            docs = memory.corpus(self.root)
        self.assertIn("Status: expired", next(text for source, text in docs if source.startswith("surprise ")))


class RanOutByModelTest(unittest.TestCase):
    def test_i9_cut_off_rate_shown_per_model(self):
        """Review I9: wisp's cut-offs went 13% -> 54% after a model switch and nothing it read showed it."""
        from supervisor import metrics
        eps = [{"kind": "live", "ts": f"t{i}", "finish": "done"} for i in range(4)] + \
              [{"kind": "live", "ts": f"u{i}", "finish": None} for i in range(3)] + \
              [{"kind": "live", "ts": "u3", "finish": "done", "wrapped_up": "deadline"},
               {"kind": "live", "ts": "u4", "finish": "done"}, {"kind": "live", "ts": "x", "finish": None}]
        js = [{"episode_ts": f"t{i}", "model": "old"} for i in range(4)] + \
             [{"episode_ts": f"u{i}", "model": "new"} for i in range(5)]
        with mock.patch.object(metrics, "live_episodes", return_value=eps):
            line, = metrics.ran_out_by_model("wisp", js)
        self.assertIn("old 0% of 4", line)
        self.assertIn("new 80% of 5", line)            # 3 unfinished + 1 deadline wrap-up; x is unjudged
        with mock.patch.object(metrics, "live_episodes", return_value=[]):
            self.assertEqual(metrics.ran_out_by_model("wisp", js), [])
