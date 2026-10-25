"""The surprise loop in the template (docs/plans/PLAN-surprise.md M1 and M3), black-box through FakeLLM.

M1: predictions on declare_intent, surprises/resolves on finish_cycle, the rule's flag and [unexpected …] note on the
tool log, the open-surprise queue in every prompt, surprises in search_memory. M3: the curiosity draw, close_surprise
and the sleep step. The rule and the fold are checked against surprise_fixtures.py, the contract with the
supervisor's copy (test_surprise_rules.py, test_surprise_ledger.py).
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import agent, config, live, memory, prompts, sleep
try:
    from shaft import surprises
except ImportError:
    raise unittest.SkipTest("template/ predates PLAN-surprise M1: merge claude/surprise-m1-m3-template into template")

from shaft.llm import Client
from shaft.state import State
from shaft.tools import Toolbox
from supervisor.tests import surprise_fixtures as fx
from supervisor.tests.fakellm import FakeLLM

DECLARE = {"goal": "Rewrite my self-model", "success_criterion": "self/self.md says X", "confidence": 60,
           "origin": "self"}
FINISH = {"what_happened": "Refused, then used update_self_model", "criterion_met": "partial", "learned": "own tool"}
SURPRISE = {"expected": "write_file self/self.md works", "observed": "PermissionError: use update_self_model",
            "so_what": "self.md has its own tool"}


class Fixture:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-surprise-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for folder in ("self", "memory", "inbox"):
            (self.root / folder).mkdir()
        self.state = State(self.root)
        self.cfg = config.load(self.root, environ={"LLM_BASE_URL": "http://127.0.0.1:1/v1", "LLM_MODEL": "fake",
                                                 "MAX_STEPS_SLEEP": "4", "NAME": "wren"})
        # Agent time fixed the day after the seeded surprises (clock.now() follows SHAFT_NOW, as in a replay)
        self.enterContext(mock.patch.dict(os.environ, {"SHAFT_ROOT": str(self.root), "SHAFT_NOW": "2026-10-13T08:00:00"},
                                          clear=True))
        self.enterContext(mock.patch("sys.stderr", new_callable=io.StringIO))

    def model(self, replies):
        fake = FakeLLM(replies)
        self.addCleanup(fake.close)
        return fake, Client(fake.url, "m", "", 30, self.root / "usage.jsonl")

    def live(self, replies, every=None):
        if every is not None:
            self.cfg.values["CURIOSITY_EVERY_CYCLES"] = every
        fake, client = self.model(replies)
        live.run(self.cfg, self.state, client)
        return fake

    def seed(self, n, ts="2026-10-12T09:00:00", text="x"):
        """An earlier cycle that reported n surprises."""
        self.state.append_episode({"ts": ts, "kind": "live", "intent": dict(DECLARE, predictions=[]),
                                   "finish": dict(FINISH, surprises=[{"expected": f"{text}{i}", "observed": f"y{i}",
                                                                      "so_what": ""} for i in range(n)],
                                                  resolves=[]),
                                   "ended_by": "control", "steps": 2, "tools": []})


class RuleAndFoldContractTest(unittest.TestCase):
    """The template's copy against the shared fixtures (the supervisor's copy passes the same ones)."""

    def test_flags_match_the_shared_fixtures(self):
        for result, expected in fx.FLAG_FIXTURES:
            self.assertEqual(surprises.flag(result), expected, result[:80])

    def test_fold_matches_the_shared_fixtures(self):
        items = surprises.fold(fx.FOLD_EPISODES, datetime.fromisoformat(fx.FOLD_NOW))
        self.assertEqual({i: (it["status"], it["depth"], it["reason"]) for i, it in items.items()}, fx.FOLD_EXPECTED)
        self.assertEqual({i: it["ended"] for i, it in items.items()}, fx.FOLD_ENDED)
        for sid, content in fx.FOLD_CONTENT.items():
            self.assertEqual((items[sid]["expected"], items[sid]["observed"], items[sid]["so_what"]), content)
        with tempfile.TemporaryDirectory() as tmp:   # the queue: open, visible, oldest first
            self.assertEqual([it["id"] for it in surprises.queue(Path(tmp), items=items)], fx.FOLD_QUEUE)
        later = surprises.fold(fx.FOLD_EPISODES, datetime.fromisoformat(fx.FOLD_LATER_NOW))
        self.assertEqual({it["status"] for it in later.values() if it["status"] not in ("resolved", "closed")},
                         {"expired"})


class PredictionLoopTest(Fixture, unittest.TestCase):
    """M1 acceptance (§3): a contradicted prediction lands in the episode, shows in the next cycle's rendered system
    prompt, and is found by search_memory; the write gets the error flag and the note; same number of turns."""

    def test_contradicted_prediction_reaches_episode_next_prompt_and_memory(self):
        fake = self.live([
            {"tool_calls": [("declare_intent", dict(DECLARE, predictions=["write_file self/self.md works"]))]},
            {"tool_calls": [("write_file", {"path": "self/self.md", "content": "# Me"})]},
            {"tool_calls": [("finish_cycle", dict(FINISH, surprises=[SURPRISE]))]}])
        ep = self.state.last_episode()
        self.assertEqual(ep["intent"]["predictions"], ["write_file self/self.md works"])
        self.assertEqual(ep["finish"]["surprises"], [SURPRISE])
        write = next(t for t in ep["tools"] if t["tool"] == "write_file")
        self.assertEqual((write["flag"], write["flag_detail"]), ("error", "PermissionError"))
        self.assertNotIn("[unexpected", write["result"])          # the log keeps what the tool returned
        seen = fake.requests[2]["messages"][-1]["content"]         # what the model got back for the write
        self.assertIn('[unexpected: error (PermissionError); you expected: "write_file self/self.md works"]', seen)
        sid = f"s{datetime.fromisoformat(ep['ts']):%m%d-%H%M}-1"
        self.assertIn(f"**Surprises:** {sid}: expected write_file self/self.md works", self.state.journal.read_text())

        nxt = self.live([{"content": "Nothing to do."}])
        system = nxt.requests[0]["messages"][0]["content"]
        queue = system.split("## Open surprises", 1)[1].split("\n## ", 1)[0]
        self.assertIn(sid, queue)
        self.assertIn("observed PermissionError: use update_self_model", queue)
        hits = memory.search(self.root, "self.md PermissionError update_self_model")
        self.assertIn(f"surprise {sid}", [h.source.split(" (")[0] for h in hits])
        found = Toolbox(self.root)._t_search_memory("write_file self/self.md works")
        self.assertIn(f"[surprise {sid}", found)

    def test_m1_adds_no_model_turn(self):
        # The same script with and without the new arguments: the note rides on a result, never a turn of its own.
        def script(extra_intent, extra_finish):
            return [{"tool_calls": [("declare_intent", dict(DECLARE, **extra_intent))]},
                    {"tool_calls": [("write_file", {"path": "self/self.md", "content": "# Me"})]},
                    {"tool_calls": [("list_files", {"path": "notes"})]},
                    {"tool_calls": [("finish_cycle", dict(FINISH, **extra_finish))]}]
        plain = self.live(script({}, {}))
        steps_plain = self.state.last_episode()["steps"]
        full = self.live(script({"predictions": ["notes/ holds my trend file"]}, {"surprises": [SURPRISE]}))
        self.assertEqual(self.state.last_episode()["steps"], steps_plain)
        self.assertEqual(len(full.requests), len(plain.requests))

    def test_malformed_predictions_never_break_declare_intent(self):
        for value, kept in (("one claim", ["one claim"]), (7, ["7"]), ({"a": 1}, []), (None, []),
                            (["a", None, {"x": 1}, ["n"], "b", "c", "d"], ["a", "b", "c"]), ([""], []),
                            (["  spaced \n out  "], ["spaced out"]), (["z" * 999], ["z" * 300])):
            with self.subTest(value=value):
                self.live([{"tool_calls": [("declare_intent", dict(DECLARE, predictions=value))]},
                           {"tool_calls": [("finish_cycle", FINISH)]}])
                ep = self.state.last_episode()
                self.assertEqual(ep["intent"]["predictions"], kept)
                declared = next(t for t in ep["tools"] if t["tool"] == "declare_intent")
                self.assertEqual(declared["result"], "intent recorded — now act")

    def test_malformed_surprises_and_resolves_are_dropped_not_errors(self):
        self.live([{"tool_calls": [("declare_intent", DECLARE)]},
                   {"tool_calls": [("finish_cycle", dict(FINISH, surprises=["text", {}, {"observed": "o"}, 3],
                                                         resolves=["s1", {"how": "no id"}, {"id": "surprise:s9"}]))]}])
        fin = self.state.last_episode()["finish"]
        self.assertEqual(fin["surprises"], [{"expected": "", "observed": "o", "so_what": ""}])
        self.assertEqual(fin["resolves"], [{"id": "s9", "how": ""}])
        self.assertIn("**Not resolved (no open surprise by that id):** s9", self.state.journal.read_text())

    def test_anticipated_failures_get_no_note_and_no_note_before_intent(self):
        fake = self.live([
            {"tool_calls": [("write_file", {"path": "self/self.md", "content": "x"})]},   # refused: no intent yet
            {"tool_calls": [("declare_intent", dict(DECLARE, predictions=["the plain write fails: it is protected"]))]},
            {"tool_calls": [("write_file", {"path": "self/self.md", "content": "x"})]},
            {"tool_calls": [("finish_cycle", FINISH)]}])
        self.assertNotIn("[unexpected", fake.requests[1]["messages"][-1]["content"])
        self.assertNotIn("[unexpected", fake.requests[3]["messages"][-1]["content"])
        flags = [t.get("flag") for t in self.state.last_episode()["tools"]]
        self.assertEqual(flags.count("error"), 2)                 # both still flagged in the log

    def test_a_prediction_of_success_never_hides_the_failure(self):
        # Codex's review: "the tests pass without errors" once counted as anticipating an error (it says "error").
        for prediction, hit in (("the tests pass without errors", ("error", "")),
                                ("the command returns exit 0", ("nonzero_exit", "")),
                                ("the HTTP request succeeds", ("http_error", "500")),
                                ("it will not fail", ("error", "")), ("the list is not empty", ("empty", ""))):
            with self.subTest(prediction=prediction):
                self.assertIn("[unexpected", surprises.note(hit, [prediction]))
        for prediction, hit in (("the plain write fails: it is protected", ("error", "PermissionError")),
                                ("the watch finds no new primary source", ("empty", "")),
                                ("the server answers 503 until it is restarted", ("http_error", "503")),
                                ("the read is cut short", ("truncated", ""))):
            with self.subTest(prediction=prediction):
                self.assertEqual(surprises.note(hit, [prediction]), "")

    def test_a_malformed_result_never_kills_the_cycle(self):
        # A tool may print anything; the rule runs outside the toolbox's exception boundary (Codex's review).
        plugin = self.root / "plugins" / "shout"
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text(json.dumps({"name": "shout", "description": "d", "parameters": {}}))
        (plugin / "main.py").write_text("def run(**a):\n    return 'exit ' + '9' * 5000 + ' · 0.1 s'\n")
        self.live([{"tool_calls": [("declare_intent", DECLARE)]}, {"tool_calls": [("shout", {})]},
                   {"tool_calls": [("finish_cycle", FINISH)]}])
        self.assertEqual(self.state.last_episode()["finish"]["criterion_met"], "partial")

    def test_no_prediction_still_gets_the_note(self):
        fake = self.live([{"tool_calls": [("declare_intent", DECLARE)]},
                          {"tool_calls": [("list_files", {"path": "notes"})]},
                          {"tool_calls": [("finish_cycle", FINISH)]}])
        self.assertIn("[unexpected: empty; you expected: nothing stated about this]",
                      fake.requests[2]["messages"][-1]["content"])

    def test_note_is_live_only_flag_everywhere(self):
        tb = Toolbox(self.root)
        fake, client = self.model([{"tool_calls": [("list_files", {"path": "notes"})]}, {"content": "done"}])
        agent.run(client, [{"role": "user", "content": "hi"}], tb, max_tokens=100, purpose="talk", max_steps=0)
        self.assertEqual(tb.log[-1]["flag"], "empty")
        self.assertNotIn("[unexpected", fake.requests[1]["messages"][-1]["content"])

    def test_queue_block_is_capped_at_8_items_and_1200_chars(self):
        self.seed(12, text="a very long expectation " * 20)
        text = surprises.block(self.root, now=datetime(2026, 10, 13, 9))
        self.assertLessEqual(len(text), surprises.QUEUE_CHARS)
        self.assertEqual(text.count("\n- s1012-0900-") + text.startswith("- s1012"), 8)
        self.assertIn("s1012-0900-5", text)                       # 1-4 expired at the cap: oldest first after that
        self.assertNotIn("s1012-0900-4 ", text)

    def test_long_expectations_never_push_out_what_was_observed(self):
        # Codex's review: eight 300-character expectations once filled the block before any "observed" was shown.
        self.state.append_episode({"ts": "2026-10-12T09:00:00", "kind": "live", "intent": dict(DECLARE, predictions=[]),
                                   "finish": dict(FINISH, resolves=[], surprises=[
                                       {"expected": "E" * 300, "observed": f"OBSERVED{i} " + "o" * 200,
                                        "so_what": "S" * 300} for i in range(8)]), "tools": []})
        text = surprises.block(self.root, now=datetime(2026, 10, 13, 9))
        self.assertLessEqual(len(text), surprises.QUEUE_CHARS)
        for i in range(8):
            self.assertIn(f"OBSERVED{i}", text)
        self.assertIn("so what: S", text)

    def test_resolves_close_the_loop(self):
        self.seed(1, ts="2026-10-12T09:00:00")
        self.live([{"tool_calls": [("declare_intent", dict(DECLARE, origin_ref="surprise:s1012-0900-1"))]},
                   {"tool_calls": [("finish_cycle", dict(FINISH, resolves=[{"id": "s1012-0900-1", "how": "tested"}]))]}])
        items = surprises.all_items(self.root)
        self.assertEqual(items["s1012-0900-1"]["status"], "resolved")
        self.assertIn("**Resolved:** s1012-0900-1: tested", self.state.journal.read_text())
        self.assertEqual(self.state.last_episode()["intent"]["origin_ref"], "surprise:s1012-0900-1")

    def test_origin_ref_must_name_one_of_my_surprises_and_be_mine(self):
        self.seed(1)
        replies = [{"tool_calls": [("declare_intent", dict(DECLARE, origin_ref="surprise:s0101-0000-9"))]},
                   {"tool_calls": [("declare_intent", dict(DECLARE, origin="operator",
                                                           origin_ref="surprise:s1012-0900-1"))]},
                   {"tool_calls": [("declare_intent", dict(DECLARE, origin_ref="surprise:s1012-0900-1"))]},
                   {"tool_calls": [("finish_cycle", FINISH)]}]
        self.live(replies)
        results = [t["result"] for t in self.state.last_episode()["tools"] if t["tool"] == "declare_intent"]
        self.assertEqual(results[0], "ERROR: origin_ref 'surprise:s0101-0000-9': no surprise of mine has that id "
                                     "(open in my queue: s1012-0900-1)")
        self.assertTrue(results[1].startswith("ERROR: a goal taken from one of my surprises has origin self"))
        self.assertEqual(results[2], "intent recorded — now act")


class QueueGuardTest(Fixture, unittest.TestCase):
    """M3 loop guards (§1.3): one follow-up unless it was judged useful; depth 2 hidden; expiry."""

    def follow_up(self, verdict):
        for p in self.state.episodes_dir.glob("*.jsonl"):
            p.unlink()
        self.seed(1)
        self.state.append_episode({"ts": "2026-10-12T10:00:00", "kind": "live",
                                   "intent": dict(DECLARE, origin_ref="surprise:s1012-0900-1", predictions=[]),
                                   "finish": dict(FINISH, surprises=[{"expected": "deeper", "observed": "hole"}],
                                                  resolves=[]), "tools": []})
        if verdict is not None:
            (self.root / memory.VERDICTS).write_text(json.dumps(
                {"episode_ts": "2026-10-12T10:00:00", "reviewed_at": "x", "followup_useful": verdict}) + "\n")
        return [it["id"] for it in surprises.queue(self.root, datetime(2026, 10, 13))]

    def test_one_follow_up_unless_it_was_judged_useful(self):
        self.assertEqual(self.follow_up(None), [])               # followed up, not judged yet: not offered again
        self.assertEqual(self.follow_up("no"), [])
        self.assertEqual(self.follow_up("partial"), ["s1012-0900-1"])
        self.assertEqual(self.follow_up("yes"), ["s1012-0900-1"])

    def test_a_goal_cannot_name_a_surprise_the_queue_hides(self):
        # Codex's review: the guards held only for what the queue showed; an id recalled from memory got through.
        self.follow_up("no")
        self.state.append_episode({"ts": "2026-10-12T11:00:00", "kind": "sleep", "ended_by": "answer", "tools": [],
                                   "closed": [{"id": "s1012-1000-1", "outcome": "duplicate", "note": "n"}]})
        self.seed(1, ts="2026-10-12T12:00:00")
        for sid, why in (("s1012-0900-1", "already had its follow-up cycle"), ("s1012-1000-1", "that surprise is closed")):
            self.assertIn(why, surprises.ineligible(self.root, sid))
        self.assertEqual(surprises.ineligible(self.root, "s1012-1200-1"), "")
        self.follow_up("yes")
        self.assertIn("opened by a follow-up", surprises.ineligible(self.root, "s1012-1000-1"))
        self.live([{"tool_calls": [("declare_intent", dict(DECLARE, origin_ref="surprise:s1012-1000-1"))]},
                   {"tool_calls": [("finish_cycle", FINISH)]}])
        declared = next(t for t in self.state.last_episode()["tools"] if t["tool"] == "declare_intent")
        self.assertIn("is not pursued further", declared["result"])

    def test_a_surprise_from_a_follow_up_is_kept_but_hidden(self):
        self.follow_up("yes")
        items = surprises.all_items(self.root, datetime(2026, 10, 13))
        self.assertEqual(items["s1012-1000-1"]["depth"], 2)
        self.assertEqual(items["s1012-1000-1"]["status"], "open")
        self.assertNotIn("s1012-1000-1", surprises.block(self.root, datetime(2026, 10, 13)))

    def test_open_surprises_expire_after_7_days(self):
        self.seed(1, ts="2026-10-01T09:00:00")
        self.assertEqual(surprises.block(self.root, datetime(2026, 10, 8, 8)).count("s1001"), 1)
        self.assertEqual(surprises.block(self.root, datetime(2026, 10, 8, 10)), "(none open)")


class CuriosityDrawTest(Fixture, unittest.TestCase):
    """M3 acceptance (§3): the share of drawn cycles, no draw on an empty queue or with an operator message
    waiting, 0 disables; the draw is recorded and a replay of the cycle draws the same."""

    def test_share_of_drawn_cycles_is_one_in_five(self):
        drawn = sum(surprises.draw(5, True, [], f"wren|seed-{i}")["drawn"] for i in range(1000))
        self.assertAlmostEqual(drawn / 1000, 0.20, delta=0.03)

    def test_no_draw_when_empty_operator_waiting_or_disabled(self):
        for every, queue, inbox, why in ((5, False, [], "empty_queue"),
                                         (5, True, ["20261025-080000-1-from-operator.md"], "operator_message"),
                                         (0, True, [], "disabled")):
            for i in range(50):
                self.assertEqual(surprises.draw(every, queue, inbox, f"s{i}"), {"drawn": False, "skipped_for": why})
        # a message from another instance or a job does not hold the draw back
        self.assertIsNone(surprises.draw(5, True, ["20261025-080000-1-from-wren.md"], "s")["skipped_for"])

    def test_drawn_cycle_is_told_and_recorded(self):
        self.seed(1)
        with mock.patch.object(surprises.random, "Random") as rng:
            rng.return_value.random.return_value = 0.0
            fake = self.live([{"content": "ok"}], every=5)
        self.assertIn(prompts.CURIOSITY.strip(), fake.requests[0]["messages"][1]["content"])
        self.assertEqual(self.state.last_episode()["curiosity"], {"drawn": True, "skipped_for": None})
        self.assertIn("**Curiosity cycle** (drawn)", self.state.journal.read_text())

    def test_undrawn_and_operator_first(self):
        self.seed(1)
        (self.root / "inbox" / "20261025-080000-1-from-operator.md").write_text("From: operator\n\nPlease X")
        fake = self.live([{"content": "ok"}], every=1)            # 1 = every cycle, but the operator comes first
        self.assertNotIn(prompts.CURIOSITY.strip(), fake.requests[0]["messages"][1]["content"])
        self.assertEqual(self.state.last_episode()["curiosity"], {"drawn": False, "skipped_for": "operator_message"})

    def test_a_replay_of_the_cycle_draws_what_it_drew(self):
        # A trial replay runs the cycle from its start (SHAFT_NOW): the coin is seeded by name and start minute.
        self.seed(1)
        draws = set()
        for _ in range(3):
            with mock.patch.dict(os.environ, {"SHAFT_NOW": "2026-10-13T08:15:20"}):
                self.live([{"content": "ok"}], every=2)
            draws.add(self.state.last_episode()["curiosity"]["drawn"])
        self.assertEqual(len(draws), 1)

    def test_both_arms_of_a_replay_draw_alike_across_a_minute_boundary(self):
        # Codex's review: the seed read the advancing clock, so an arm that got going a second later, at 08:13, drew
        # with another seed than one still at 08:12:59. It reads the run's fixed start now.
        self.seed(1)
        seeds = []
        with mock.patch.object(surprises, "draw", side_effect=lambda e, q, i, seed: seeds.append(seed) or
                               {"drawn": False, "skipped_for": None}), \
                mock.patch.dict(os.environ, {"SHAFT_NOW": "2026-10-13T08:12:59"}):
            for elapsed in (0, 5):
                with mock.patch("time.monotonic", return_value=live.clock.STARTED + elapsed):
                    self.live([{"content": "ok"}])
        self.assertEqual(seeds, ["wren|2026-10-13T08:12"] * 2)

    def test_nothing_in_a_live_cycle_says_replay(self):
        self.seed(1)
        with mock.patch.dict(os.environ, {"SHAFT_NOW": "2026-10-13T08:15:20"}), \
                mock.patch.object(surprises.random, "Random") as rng:
            rng.return_value.random.return_value = 0.0
            fake = self.live([{"content": "ok"}], every=5)
        shown = json.dumps(fake.requests[0]).lower()
        for word in ("replay", "trial run", "simulat"):
            self.assertNotIn(word, shown)

    def test_config_default(self):
        self.assertEqual(config.FIELDS["CURIOSITY_EVERY_CYCLES"], (int, 5))
        shipped = config.parse_env_file(Path(config.__file__).resolve().parents[1] / "config.env")
        self.assertEqual(shipped["CURIOSITY_EVERY_CYCLES"], "5")


class SleepDispositionTest(Fixture, unittest.TestCase):
    """M3: the sleep step goes through every surprise since the last sleep; close_surprise records it."""

    def test_close_surprise_and_the_list_since_my_last_sleep(self):
        self.seed(2, ts="2026-10-12T09:00:00")
        fake, client = self.model([
            {"tool_calls": [("close_surprise", {"id": "s1012-0900-1", "outcome": "not_worth_pursuing",
                                                "note": "a one-off"})]},
            {"tool_calls": [("close_surprise", {"id": "s1012-0900-1", "outcome": "duplicate", "note": "again"}),
                            ("close_surprise", {"id": "s1012-0900-2", "outcome": "maybe", "note": "x"}),
                            ("close_surprise", {"id": "s1012-0900-2", "outcome": "answered", "note": ""}),
                            ("close_surprise", {"id": "nope", "outcome": "answered", "note": "x"})]},
            {"content": "Slept. s1012-0900-2 stays open: worth a cycle to test."}])
        with mock.patch.object(sleep.broker, "available", return_value=False):
            sleep.run(self.cfg, self.state, client)
        user = fake.requests[0]["messages"][1]["content"]
        self.assertIn("7. Go through the surprises since your last sleep", user)
        listed = user.split("## Surprises since my last sleep", 1)[1]
        self.assertIn("- s1012-0900-1 [open]: expected x0 — observed y0", listed)
        tools = {t["function"]["name"] for t in fake.requests[0]["tools"]}
        self.assertIn("close_surprise", tools)
        ep = self.state.last_episode()
        self.assertEqual(ep["closed"], [{"id": "s1012-0900-1", "outcome": "not_worth_pursuing", "note": "a one-off"}])
        results = [t["result"] for t in ep["tools"] if t["tool"] == "close_surprise"]
        self.assertEqual(results[0], "closed s1012-0900-1 (not_worth_pursuing)")
        self.assertEqual(results[1], "ERROR: s1012-0900-1 is already closed")
        self.assertTrue(results[2].startswith("ERROR: outcome must be one of answered"))
        self.assertEqual(results[3], "ERROR: say in note why it is closed")
        self.assertTrue(results[4].startswith("ERROR: I have no surprise 'nope' (open: s1012-0900-2)"))
        self.assertEqual(surprises.all_items(self.root)["s1012-0900-1"]["status"], "closed")

        # the next sleep lists only what came after this one
        self.seed(1, ts=(datetime.fromisoformat(ep["ts"]) + timedelta(minutes=5)).isoformat(timespec="seconds"))
        fake2, client2 = self.model([{"content": "Slept again."}])
        with mock.patch.object(sleep.broker, "available", return_value=False):
            sleep.run(self.cfg, self.state, client2)
        listed = fake2.requests[0]["messages"][1]["content"].split("## Surprises since my last sleep", 1)[1]
        self.assertNotIn("s1012-0900-", listed)
        self.assertEqual(listed.count("\n- s"), 1)

    def test_close_is_not_offered_on_the_wrap_up_turn(self):
        # A sleep's last turn must write the entry: no tool, close_surprise included, may be called then.
        self.cfg.values["MAX_STEPS_SLEEP"] = 1
        fake, client = self.model([{"content": "Entry."}])
        with mock.patch.object(sleep.broker, "available", return_value=False):
            sleep.run(self.cfg, self.state, client)
        self.assertNotIn("tools", fake.requests[0])

    def test_only_sleep_offers_close_surprise(self):
        self.assertNotIn("close_surprise", {t["function"]["name"] for t in Toolbox(self.root).specs()})


if __name__ == "__main__":
    unittest.main()
