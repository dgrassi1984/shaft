"""#103: detect repeated effects without changing shadow-mode model input."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import agent, config, live, loopguard
from shaft.llm import Reply
from shaft.tools import Toolbox
from supervisor.tests.test_autonomy_gaps_template import InstanceFixture

CORPUS = json.loads(Path(__file__).with_name("loop_effect_fixtures.json").read_text())


class EffectTest(unittest.TestCase):
    """#103: replay planted loops and held-out pagination, polling and retries."""

    def test_corpus(self):
        for episode in CORPUS:
            with self.subTest(episode=episode["name"]):
                guard = loopguard.Guard()
                for turn, entry in enumerate(episode["tools"], 1):
                    guard.observe(entry["tool"], entry["args"], entry["result"], turn, {})
                self.assertEqual(bool(guard.events), episode["loop_expected"])
                if episode["loop_expected"]:
                    self.assertEqual([e["turn"] for e in guard.events], [3])
                    self.assertEqual(guard.events[0]["rounds"], 3)

    def test_digest_normalises_metadata_but_preserves_evidence(self):
        a = '{"id": "aaa", "timestamp": "2026-10-28T10:00:00Z", "status": "pending"}'
        b = '{ "status": "pending", "id": "bbb", "timestamp": "2026-10-28T10:01:00Z" }'
        self.assertEqual(loopguard.signature("status", "{}", a), loopguard.signature("status", "{}", b))
        self.assertNotEqual(loopguard.signature("status", "{}", a),
                            loopguard.signature("status", "{}", b.replace("pending", "done")))
        self.assertEqual(loopguard.signature("status", "{}", "2026-10-28T10:00:00Z id=aaa  pending"),
                         loopguard.signature("status", "{}", "2026-10-28T10:01:00Z id=bbb\npending"))

    def test_unchanged_success_and_changed_failure(self):
        guard = loopguard.Guard()
        for turn in range(1, 5):
            guard.observe("status", "{}", "pending", turn, {})
        self.assertEqual([e["turn"] for e in guard.events], [4])
        guard = loopguard.Guard()
        for turn, result in enumerate(("ERROR: missing", "(empty)", "HTTP 404 Not Found"), 1):
            guard.observe("fetch_page", '{"url": "https://example.org/a"}', result, turn, {})
        self.assertEqual([e["turn"] for e in guard.events], [3])

    def test_successful_writes_with_a_constant_answer_are_new_work(self):
        """#103, from thelos's real record: its sleep of 2026-10-27 13:40 stored five facts, each answered
        "remembered"; comparing results alone flagged that as a loop. A successful write is new work."""
        guard = loopguard.Guard()
        for turn in range(1, 6):
            guard.observe("remember", json.dumps({"fact": f"fact {turn}", "source": "operator"}), "remembered", turn, {})
        self.assertEqual(guard.events, [])

    def test_shell_runs_are_grouped_by_program_and_subcommand(self):
        """#103, from thelos's real record: every `run` was one family, so three unrelated failing commands looked
        like one loop. Different programs are different families; the same command failing three times is not."""
        guard = loopguard.Guard()
        for turn, command in enumerate(("python3 near_read.py extract", "git commit -m x", "pytest -q"), 1):
            guard.observe("run", json.dumps({"command": command}), "exit 1 · 0.1 s", turn, {})
        self.assertEqual(guard.events, [])
        for turn in range(4, 7):
            guard.observe("run", json.dumps({"command": "python3 near_read.py extract a"}), "exit 64 · usage", turn, {})
        self.assertEqual([e["turn"] for e in guard.events], [6])

    def test_bad_url_does_not_interrupt_observation_of_tool_error(self):
        guard = loopguard.Guard()
        for turn in range(1, 4):
            guard.observe("http_request", '{"url": "http://["}', "ERROR: Invalid IPv6 URL", turn, {})
        self.assertEqual([e["turn"] for e in guard.events], [3])

    def test_report_prefers_saved_signature_over_cut_arguments(self):
        entries = []
        guard = loopguard.Guard()
        for turn, source in enumerate(CORPUS[0]["tools"], 1):
            entry = dict(source)
            guard.observe(entry["tool"], entry["args"], entry["result"], turn, entry)
            entry["args"] = "{cut"
            entries.append(entry)
        measured = loopguard.report([{"tools": entries, "loop_expected": True}])
        self.assertEqual(measured["caught_loops"], 1)

    def test_new_family_breaks_streak_and_success_resets_it(self):
        guard = loopguard.Guard()
        for turn, (path, result) in enumerate((("a", "(empty)"), ("a", "(empty)"),
                                               ("b", "(empty)"), ("a", "(empty)"),
                                               ("a", "new data"), ("a", "(empty)")), 1):
            guard.observe("read_file", json.dumps({"path": path}), result, turn, {})
        self.assertEqual(guard.events, [])

    def test_first_failure_does_not_extend_an_unchanged_success_streak(self):
        guard = loopguard.Guard()
        for turn, result in enumerate(("pending", "pending", "pending", "HTTP 503 Unavailable"), 1):
            guard.observe("job_status", "{}", result, turn, {})
        self.assertEqual(guard.events, [])
        self.assertEqual(guard.rounds, 1)

    def test_report_measures_labels_and_does_not_mutate(self):
        before = copy.deepcopy(CORPUS)
        measured = loopguard.report(CORPUS)
        self.assertEqual(measured, {"episodes": 8, "flagged": 3, "legitimate": 5, "false_positives": 0,
                                    "false_positive_rate": 0.0, "labelled_loops": 3, "caught_loops": 3})
        self.assertEqual(CORPUS, before)
        mislabelled = dict(CORPUS[0], loop_expected=False)
        self.assertEqual(loopguard.report([mislabelled, CORPUS[3]])["false_positive_rate"], 0.5)
        self.assertIsNone(loopguard.report([{"tools": []}])["false_positive_rate"])

    def test_report_cli_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.jsonl"
            raw = "\n".join(json.dumps(ep) for ep in CORPUS)
            path.write_text(raw)
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["shaft.loopguard", "report", directory]), contextlib.redirect_stdout(output):
                loopguard.main()
            self.assertEqual(json.loads(output.getvalue())["false_positive_rate"], 0.0)
            self.assertEqual(path.read_text(), raw)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_config_defaults_choices_and_positive_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"LLM_BASE_URL": "http://localhost:1", "LLM_MODEL": "fake"}
            cfg = config.load(Path(directory), environ=env)
            self.assertEqual((cfg.LOOP_GUARD, cfg.LOOP_EFFECT_ROUNDS), ("shadow", 3))
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                cfg = config.load(Path(directory), environ=dict(env, LOOP_GUARD="typo", LOOP_EFFECT_ROUNDS="0"))
            self.assertEqual((cfg.LOOP_GUARD, cfg.LOOP_EFFECT_ROUNDS), ("shadow", 3))


class AgentEffectTest(InstanceFixture, unittest.TestCase):
    """#103: exercise the shared agent loop and the persisted live episode."""

    def run_sequence(self, mode, episode=CORPUS[0], box=None):
        box = box or Toolbox(self.root)
        box.loop_guard = loopguard.Guard(mode)
        messages = [{"role": "user", "content": "investigate"}]
        entries = episode["tools"]
        replies = [Reply("", tool_calls=[{"id": str(i), "name": e["tool"], "arguments": e["args"]}])
                   for i, e in enumerate(entries)] + [Reply("done")]
        client = mock.Mock()
        client.complete.side_effect = replies
        with mock.patch.object(box, "_dispatch", side_effect=[e["result"] for e in entries]) as dispatch:
            outcome = agent.run(client, messages, box, max_tokens=100, purpose="test", max_steps=10, ledger=False)
        self.assertEqual(dispatch.call_count, len(entries))
        self.assertEqual(outcome.ended_by, "answer")
        return messages, box

    def test_shadow_changes_nothing_visible_and_on_still_runs_calls(self):
        off, off_box = self.run_sequence("off")
        shadow, box = self.run_sequence("shadow")
        self.assertEqual(shadow, off)
        self.assertEqual(box.loop_guard.record()["loop_shadow"][0]["turn"], 3)
        self.assertEqual(off_box.loop_guard.record(), {})
        self.assertTrue(all("effect" not in e for e in off_box.log))
        on, on_box = self.run_sequence("on")
        results = [m["content"] for m in on if m["role"] == "tool"]
        self.assertEqual(results[:2], [e["result"] for e in CORPUS[0]["tools"][:2]])
        self.assertEqual(results[2], "no results\n\n" + loopguard.NOTE)
        self.assertEqual([e["result"] for e in on_box.log], [e["result"] for e in CORPUS[0]["tools"]])

    def test_legitimate_sequences_through_agent(self):
        for episode in CORPUS[3:]:
            with self.subTest(episode=episode["name"]):
                _, box = self.run_sequence("on", episode)
                self.assertEqual(box.loop_guard.record(), {})

    def test_state_is_per_run_even_when_toolbox_is_reused(self):
        _, box = self.run_sequence("shadow", dict(CORPUS[0], tools=CORPUS[0]["tools"][:2]))
        messages = []
        client = mock.Mock()
        client.complete.side_effect = [Reply("", tool_calls=[{"id": "1", "name": "web_search",
                                       "arguments": CORPUS[0]["tools"][2]["args"]}]), Reply("done")]
        with mock.patch.object(box, "_dispatch", return_value="no results"):
            agent.run(client, messages, box, max_tokens=100, purpose="test", max_steps=4, ledger=False)
        self.assertEqual(box.loop_guard.record(), {})

    def test_live_episode_keeps_shadow_record(self):
        self.cfg.values.update(WORKSPACE="off", FORECAST="off", OUTCOME_CARDS="off")
        entries = CORPUS[0]["tools"]
        def call(name, args):
            return Reply("", tool_calls=[{"id": name, "name": name, "arguments": json.dumps(args)}])
        client = mock.Mock()
        client.complete.side_effect = [call("declare_intent", {"goal": "Check orchard hours", "success_criterion": "hours known",
                                                               "confidence": 50, "origin": "self"})] + [
            call(e["tool"], json.loads(e["args"])) for e in entries] + [
            call("finish_cycle", {"what_happened": "no result", "criterion_met": "no", "learned": "try elsewhere",
                                  "delivered_via": "none"})]
        with mock.patch.object(Toolbox, "_dispatch", side_effect=[e["result"] for e in entries]):
            live.run(self.cfg, self.state, client)
        episode = self.state.last_episode()
        self.assertEqual(episode["loop_shadow"][0]["turn"], 4)
        self.assertEqual(episode["loop_shadow"][0]["would_say"], loopguard.NOTE)
        self.assertEqual(loopguard.report([dict(episode, loop_expected=True)])["caught_loops"], 1)


if __name__ == "__main__":
    unittest.main()
