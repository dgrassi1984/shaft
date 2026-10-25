"""The confirm step of PLAN-calibration-feedback (Task 3, template side): when the declared confidence lands in a band
with data in self/calibration.json, declare_intent shows that band's base rate, every other tool stays closed until
confirm_confidence, and the intent keeps both numbers (v3). Without a usable band the cycle runs exactly as v2."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import agent, config, live
try:
    from shaft import base_rate
except ImportError:
    raise unittest.SkipTest("template/ predates PLAN-calibration-feedback Task 3 (no shaft.base_rate): "
                            "merge claude/calibration-feedback-template into template first") from None

from shaft.llm import Client
from shaft.state import State
from shaft.tools import Toolbox, _spec
from supervisor import metrics
from supervisor.tests.fakellm import FakeLLM

STEPS = 4
REFUSED = "ERROR: call confirm_confidence first"


def band(lo, hi, met=0, n=0, older_met=0, older_n=0):
    return {"lo": lo, "hi": hi, "met": met, "n": n, "older_met": older_met, "older_n": older_n}


def rates(low=None, mid=None, high=None):
    """The shape metrics.write_stats writes (Task 2); the last band's hi is 100."""
    return {"written": "2026-10-25T03:00:00", "question": "criterion_met=yes",
            "bands": [low or band(0, 40), mid or band(40, 70), high or band(70, 100)]}


WITH_DATA = rates(high=band(70, 100, met=63, n=100, older_met=600, older_n=950))


def declare(confidence, goal="Count trees"):
    return ("declare_intent", {"goal": goal, "success_criterion": "All trees counted",
                               "confidence": confidence, "origin": "self"})


def confirm(confidence):
    return ("confirm_confidence", {"confidence": confidence})


def read(i):
    return ("read_file", {"path": f"notes/{i}.md"})   # distinct paths: the agent does not rerun an identical call


FINISH = ("finish_cycle", {"what_happened": "Counted", "criterion_met": "partial", "learned": "Need a map"})


class Fixture:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-calibration-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for folder in ("self", "memory", "inbox", "notes"):
            (self.root / folder).mkdir()
        self.state = State(self.root)
        self.cfg = config.load(self.root, environ={"LLM_BASE_URL": "http://127.0.0.1:1/v1", "LLM_MODEL": "fake",
                                                 "MAX_STEPS_LIVE": str(STEPS)})
        self.enterContext(mock.patch.dict(os.environ, {"SHAFT_ROOT": str(self.root)}, clear=True))
        self.enterContext(mock.patch("sys.stderr", new_callable=io.StringIO))

    def write_rates(self, data):
        (self.root / base_rate.FILE).write_text(data if isinstance(data, str) else json.dumps(data))

    def cycle(self, replies):
        fake = FakeLLM([{"tool_calls": list(r)} for r in replies])
        self.addCleanup(fake.close)
        live.run(self.cfg, self.state, Client(fake.url, "m", "", 30, self.root / "usage.jsonl"))
        return fake, self.state.last_episode()

    def results(self, episode, tool):
        return [t["result"] for t in episode["tools"] if t["tool"] == tool]

    def never_finishing(self, first):
        """`first` turns, then reads until the steps run out: the request count is max_steps."""
        return [*first, *([read(i)] for i in range(10))]


class BaseRateModuleTest(unittest.TestCase):
    def test_band_for_edges_and_empty_bands(self):
        r = rates(low=band(0, 40, met=1, n=2), mid=band(40, 70, older_met=3, older_n=4), high=band(70, 100, met=5, n=9))
        self.assertEqual(base_rate.band_for(r, 0)["lo"], 0)
        self.assertEqual(base_rate.band_for(r, 39)["lo"], 0)
        self.assertEqual(base_rate.band_for(r, 40)["lo"], 40)
        self.assertEqual(base_rate.band_for(r, 70)["lo"], 70)
        self.assertEqual(base_rate.band_for(r, 100)["lo"], 70)    # c == 100 belongs to the last band
        self.assertIsNone(base_rate.band_for(rates(), 50))         # both n and older_n are 0

    def test_describe_exact_copy(self):
        self.assertEqual(
            base_rate.describe(band(70, 100, met=63, n=100, older_met=600, older_n=950), 85),
            "Before acting, look at your record: when you said 70–100%, the criterion was fully met in 63 of 100 "
            "judged cycles (63%); under the older question, 600 of 950 (63%). Call confirm_confidence with your "
            "final probability (0-100) that the criterion will be fully met — keep 85 or change it. Your other "
            "tools stay closed until you do.")
        tail = (" Call confirm_confidence with your final probability (0-100) that the criterion will be fully met — "
                "keep {} or change it. Your other tools stay closed until you do.")
        self.assertEqual(base_rate.describe(band(40, 70, met=1, n=3), 50),
                         "Before acting, look at your record: when you said 40–70%, the criterion was fully met in 1 of 3 "
                         "judged cycles (33%)." + tail.format(50))
        self.assertEqual(base_rate.describe(band(0, 40, older_met=2, older_n=8), 30),   # n == 0: no "0 of 0" clause
                         "Before acting, look at your record: when you said 0–40%, under the older question, the "
                         "criterion was fully met in 2 of 8 (25%)." + tail.format(30))

    def test_question_strings_match_supervisor(self):
        # The supervisor never imports agent code: the string exists twice and this pins them equal.
        self.assertEqual(live.CONFIDENCE_OF_SHOWN, metrics.CONFIDENCE_V3)
        self.assertEqual(live.CONFIDENCE_OF, metrics.CONFIDENCE_V2)

    def test_file_name_matches_supervisor(self):
        self.assertEqual(base_rate.FILE, metrics.CALIBRATION_FILE)

    def test_round_trip_with_the_supervisor_writer(self):
        # Final fix F6 (O5): the template's reader on what metrics.base_rates really writes — the edges, the last
        # band's hi of 100 and the question are a contract between two programs that never import each other.
        judge = metrics.judge.JUDGE_MODELS[0]
        rows = [{"episode_ts": f"2026-10-25T10:{i:02d}:00", "judge": judge, "confidence": c, "confidence_of": q,
                 "criterion_met": met}
                for i, (c, q, met) in enumerate([(0, metrics.CONFIDENCE_V2, "no"), (39, metrics.CONFIDENCE_V3, "yes"),
                                                 (40, metrics.CONFIDENCE_V2, "yes"), (69, None, "partial"),
                                                 (70, metrics.CONFIDENCE_V3, "no"), (100, metrics.CONFIDENCE_V2, "yes"),
                                                 (100, None, "yes")])]
        with tempfile.TemporaryDirectory(prefix="shaft-roundtrip-") as tmp:
            (Path(tmp) / "self").mkdir()
            (Path(tmp) / base_rate.FILE).write_text(json.dumps(metrics.base_rates(rows), indent=2) + "\n")
            loaded, error = base_rate.load(Path(tmp))
        self.assertIsNone(error)
        first, middle, last = loaded["bands"]
        self.assertEqual(base_rate.band_for(loaded, 0), first)
        self.assertEqual(base_rate.band_for(loaded, 40), middle)
        self.assertEqual(base_rate.band_for(loaded, 100), last)
        self.assertEqual((last["hi"], last["met"], last["n"], last["older_n"]), (100, 1, 2, 1))
        self.assertEqual(base_rate.QUESTION, metrics.CONFIDENCE_V2)


class ConfirmStepTest(Fixture, unittest.TestCase):
    def test_no_limit_survives_a_shown_rate(self):
        # Review round 1, MINOR 3: MAX_STEPS_LIVE <= 0 means no limit; 0 + 1 would have been a one-turn cycle.
        self.cfg.MAX_STEPS_LIVE = 0
        self.write_rates(WITH_DATA)
        fake, episode = self.cycle([[declare(85)], [confirm(70)], *([read(i)] for i in range(4)), [FINISH]])
        self.assertEqual((len(fake.requests), episode["ended_by"]), (7, "control"))
        self.assertEqual(episode["intent"]["confidence"], 70)

    def test_band_with_data_shows_rate_and_closes_tools(self):
        self.write_rates(WITH_DATA)
        fake, episode = self.cycle([[declare(85)], [read(1)], [confirm(65)], [read(2)], [FINISH]])
        shown = self.results(episode, "declare_intent")[0]
        self.assertTrue(shown.startswith("intent recorded. Before acting"), shown)
        self.assertIn("when you said 70–100%, the criterion was fully met in 63 of 100", shown)
        reads = self.results(episode, "read_file")
        self.assertTrue(reads[0].startswith(REFUSED), reads[0])
        self.assertFalse(reads[1].startswith("ERROR: call"), reads[1])   # open again after the confirm
        intent = episode["intent"]
        self.assertEqual(intent["confidence"], 65)
        self.assertEqual(intent["confidence_first"], 85)
        self.assertEqual(intent["confidence_of"], live.CONFIDENCE_OF_SHOWN)
        self.assertEqual(intent["base_rate_shown"], WITH_DATA["bands"][2])
        self.assertNotIn("base_rate_error", intent)
        self.assertIn("**Confidence beforehand:** 85% → 65% after my base rate", self.state.read(self.state.journal))
        self.assertIn("confirm_confidence", [s["function"]["name"] for s in fake.requests[0]["tools"]])

    def test_same_turn_confirm_refused(self):
        # Review focus 1: a confirm in the declare's own reply was written before the base rate was seen.
        self.write_rates(WITH_DATA)
        _, episode = self.cycle([[declare(85), confirm(60)], [confirm(65)], [FINISH]])
        first, second = self.results(episode, "confirm_confidence")
        self.assertTrue(first.startswith("ERROR"), first)
        self.assertFalse(second.startswith("ERROR"), second)
        self.assertEqual(episode["intent"]["confidence"], 65)
        self.assertEqual(episode["intent"]["confidence_first"], 85)

    def test_same_turn_retry_with_the_same_number_is_not_a_repeat(self):
        # Review round 1, IMPORTANT 1: the retry the refusal asks for repeats the call exactly; the repeat guard
        # skipped it ("running it again cannot change it"), a third try ended the cycle with the tools still closed.
        self.write_rates(WITH_DATA)
        fake, episode = self.cycle([[declare(85), confirm(85)], [confirm(85)], [confirm(85)], [read(1)], [FINISH]])
        first, second, third = self.results(episode, "confirm_confidence")
        self.assertTrue(first.startswith("ERROR: confirm_confidence in the same reply"), first)
        self.assertEqual(second, "confidence 85% recorded — now act")
        self.assertEqual(third, "ERROR: confidence already confirmed for this cycle")   # run, not skipped as a repeat
        self.assertFalse(self.results(episode, "read_file")[0].startswith("ERROR: call"))
        self.assertEqual(episode["ended_by"], "control")
        self.assertEqual(episode["finish"]["criterion_met"], "partial")
        self.assertEqual(episode["intent"]["confidence_of"], live.CONFIDENCE_OF_SHOWN)
        self.assertEqual((episode["intent"]["confidence"], episode["intent"]["confidence_first"]), (85, 85))

    def test_confirm_once_and_in_range(self):
        self.write_rates(WITH_DATA)
        _, episode = self.cycle([[declare(85)], [confirm(101)], [confirm(70)], [confirm(50)], [FINISH]])
        bad, ok, again = self.results(episode, "confirm_confidence")
        self.assertTrue(bad.startswith("ERROR"), bad)
        self.assertFalse(ok.startswith("ERROR"), ok)
        self.assertTrue(again.startswith("ERROR"), again)
        self.assertEqual(episode["intent"]["confidence"], 70)

    def assert_shown_never_confirmed(self, episode):
        """v2, with what was offered kept for audit: the instance never read the rate, so it is not treated (F2)."""
        intent = episode["intent"]
        self.assertEqual(intent["confidence_of"], live.CONFIDENCE_OF)
        self.assertNotIn("confidence_first", intent)
        self.assertEqual(intent["base_rate_shown"], WITH_DATA["bands"][2])
        self.assertEqual(intent["confidence"], 85)
        self.assertIn("**Confidence beforehand:** 85%\n", self.state.read(self.state.journal))

    def test_wrap_up_without_confirm_is_v2(self):
        # Review focus 2: declared on the wrap-up turn, where no later turn exists; finish is still accepted.
        # Final fix F2 (ruling): it was v3 with confidence == confidence_first, a "treated" cycle that never saw the rate.
        self.write_rates(WITH_DATA)
        fake, episode = self.cycle([*([read(i)] for i in range(STEPS - 1)), [declare(85), FINISH]])
        self.assertEqual(len(fake.requests), STEPS)
        self.assertTrue(fake.requests[-1]["messages"][-1]["content"].startswith("[supervisor] You have one turn"))
        self.assertEqual(episode["ended_by"], "control")
        self.assertEqual(episode["finish"]["criterion_met"], "partial")
        self.assert_shown_never_confirmed(episode)

    def test_declare_and_finish_in_one_reply_is_v2(self):
        # Final fix F2: a finish beside the declare ends the cycle before the rate could be read.
        self.write_rates(WITH_DATA)
        _, episode = self.cycle([[declare(85), FINISH]])
        self.assertEqual(episode["ended_by"], "control")
        self.assert_shown_never_confirmed(episode)

    def test_v3_only_from_the_accepted_confirm(self):
        # Final fix F2: the declare records what it offered; the version and the first number come with the confirm.
        self.write_rates(WITH_DATA)
        _, episode = self.cycle([[declare(85), confirm(60)], [read(1)], [FINISH]])   # refused, then never retried
        self.assert_shown_never_confirmed(episode)
        _, episode = self.cycle([[declare(85)], [confirm(85)], [FINISH]])            # kept the number: still v3
        self.assertEqual(episode["intent"]["confidence_of"], live.CONFIDENCE_OF_SHOWN)
        self.assertEqual((episode["intent"]["confidence_first"], episode["intent"]["confidence"]), (85, 85))

    def test_judges_never_see_the_step(self):
        # Final fix F1 on a real episode: the strings the supervisor takes out are the ones this template writes.
        self.write_rates(WITH_DATA)
        _, episode = self.cycle([[declare(85)], [read(1)], [confirm(65)], [read(2)], [FINISH]])
        self.assertTrue(self.results(episode, "read_file")[0].startswith(metrics.CONFIRM_FIRST))
        case = metrics._case(episode)
        for mark in ("base_rate_shown", "confidence_first", "fully met in", "confirm_confidence", "your record"):
            self.assertNotIn(mark, case)
        self.assertIn("→ " + metrics.NEUTRAL_DECLARE + "\n\n### 2. read_file(", case)
        (self.root / base_rate.FILE).unlink()
        _, plain = self.cycle([[declare(85)], [FINISH]])
        self.assertEqual(self.results(plain, "declare_intent"), [metrics.NEUTRAL_DECLARE])   # live's own v2 answer


class RunsAsV2Test(Fixture, unittest.TestCase):
    def assert_v2(self, episode):
        intent = episode["intent"]
        self.assertEqual(intent["confidence_of"], "criterion_met=yes")
        self.assertNotIn("confidence_first", intent)
        self.assertNotIn("base_rate_shown", intent)
        self.assertEqual(self.results(episode, "declare_intent"), ["intent recorded — now act"])
        self.assertNotIn(REFUSED, " ".join(self.results(episode, "read_file")))

    def test_no_file_runs_as_v2(self):
        fake, episode = self.cycle(self.never_finishing([[declare(85)]]))
        self.assert_v2(episode)
        self.assertNotIn("base_rate_error", episode["intent"])
        self.assertEqual(len(fake.requests), self.cfg.MAX_STEPS_LIVE)
        self.assertNotIn("confirm_confidence", [s["function"]["name"] for s in fake.requests[0]["tools"]])

    def test_empty_band_runs_as_v2(self):
        self.write_rates(rates())
        fake, episode = self.cycle(self.never_finishing([[declare(85)]]))
        self.assert_v2(episode)
        self.assertNotIn("base_rate_error", episode["intent"])
        self.assertEqual(len(fake.requests), self.cfg.MAX_STEPS_LIVE)

    def test_declared_band_empty_while_another_has_data_runs_as_v2(self):
        self.write_rates(WITH_DATA)
        _, episode = self.cycle([[declare(20)], [read(1)], [FINISH]])
        self.assert_v2(episode)
        self.assertNotIn("**Confidence beforehand:** 20% →", self.state.read(self.state.journal))

    def test_unreadable_file_recorded(self):
        for name, content in (("parse error", "{not json"), ("wrong shape", json.dumps({"bands": "none"})),
                              ("band without counts", json.dumps({"bands": [{"lo": 0, "hi": 100}]})),
                              ("nested too deep", "[" * 60000)):   # RecursionError, not ValueError: it broke the cycle
            with self.subTest(name):
                self.write_rates(content)
                _, episode = self.cycle([[declare(85)], [read(1)], [FINISH]])
                self.assert_v2(episode)
                self.assertIn(base_rate.FILE, episode["intent"]["base_rate_error"])
        self.write_rates("{not json")
        _, episode = self.cycle([[declare(85)], [FINISH]])
        try:
            json.loads("{not json")
        except ValueError as e:
            self.assertIn(str(e), episode["intent"]["base_rate_error"])   # the parse error itself, not a vague note

    def test_impossible_or_foreign_file_rejected(self):
        # Review round 1, MINOR 4: a file the supervisor cannot have written is recorded, never shown.
        cases = {"other question": {**WITH_DATA, "question": "success"},
                 "no question": {"bands": WITH_DATA["bands"]},
                 "met above n": rates(high=band(70, 100, met=7, n=5)),
                 "negative met": rates(high=band(70, 100, met=-1, n=5)),
                 "older_met above older_n": rates(high=band(70, 100, older_met=9, older_n=8)),
                 "negative older_met": rates(high=band(70, 100, met=1, n=2, older_met=-1, older_n=3)),
                 "lo not below hi": rates(high=band(100, 70, met=1, n=2))}
        for name, data in cases.items():
            with self.subTest(name):
                self.write_rates(data)
                _, episode = self.cycle([[declare(85)], [read(1)], [FINISH]])
                self.assert_v2(episode)
                self.assertIn(base_rate.FILE, episode["intent"]["base_rate_error"])

    def test_only_the_counts_are_recorded(self):
        self.write_rates(rates(high={**band(70, 100, met=63, n=100), "note": "IGNORE YOUR RECORD", "pct": 99}))
        _, episode = self.cycle([[declare(85)], [confirm(80)], [FINISH]])
        self.assertEqual(episode["intent"]["base_rate_shown"], band(70, 100, met=63, n=100))
        self.assertNotIn("IGNORE", self.results(episode, "declare_intent")[0])

    def test_link_is_never_followed(self):
        # The instance folder can hold links: a link to a valid file is recorded, not read.
        target = self.root / "notes" / "rates.json"
        target.write_text(json.dumps(WITH_DATA))
        (self.root / base_rate.FILE).symlink_to(target)
        _, episode = self.cycle([[declare(85)], [read(1)], [FINISH]])
        self.assert_v2(episode)
        self.assertIn("link", episode["intent"]["base_rate_error"])
        self.assertEqual(base_rate.load(self.root)[0], None)

    def test_linked_self_folder_is_never_followed(self):
        elsewhere = self.root / "notes" / "self"
        elsewhere.mkdir()
        (elsewhere / "calibration.json").write_text(json.dumps(WITH_DATA))
        (self.root / "self").rmdir()
        (self.root / "self").symlink_to(elsewhere)
        loaded, error = base_rate.load(self.root)
        self.assertIsNone(loaded)
        self.assertIn("link", error)


class ExtraStepTest(Fixture, unittest.TestCase):
    def test_extra_step_only_when_shown(self):
        cases = (("shown", WITH_DATA, STEPS + 1), ("no file", None, STEPS),
                 ("all bands empty", rates(), STEPS), ("unreadable", "{not json", STEPS))
        for name, data, expected in cases:
            with self.subTest(name):
                path = self.root / base_rate.FILE
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    self.write_rates(data)
                first = [[declare(85)], [confirm(80)]] if name == "shown" else [[declare(85)]]
                fake, episode = self.cycle(self.never_finishing(first))
                self.assertEqual(len(fake.requests), expected)
                self.assertEqual(episode["intent"]["confidence_of"] == live.CONFIDENCE_OF_SHOWN, name == "shown")

    def test_no_extra_step_when_the_declared_band_is_empty(self):
        # Regression (task-3 concern 1): the step was granted up front whenever the file had a band with data, so a
        # declare in an empty band ran as v2 with one step more than v2.
        self.write_rates(WITH_DATA)
        fake, episode = self.cycle(self.never_finishing([[declare(20)]]))
        self.assertEqual(episode["intent"]["confidence_of"], "criterion_met=yes")
        self.assertEqual(len(fake.requests), STEPS)



class AgentStepLimitTest(unittest.TestCase):
    """agent.run reads its limit at the top of each turn, so live can grant the confirm turn after the declare."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-steps-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {"SHAFT_ROOT": str(self.root)}, clear=True))

    def drive(self, max_steps, replies, controls=None):
        fake = FakeLLM([{"tool_calls": list(r)} for r in replies])
        self.addCleanup(fake.close)
        client = Client(fake.url, "m", "", 30, self.root / "usage.jsonl")
        stop = {"stop": agent.Control(_spec("stop", "stop", {}), lambda a: "stopped", stops=True)}
        outcome = agent.run(client, [{"role": "user", "content": "go"}], Toolbox(self.root), max_tokens=100,
                            purpose="live", max_steps=max_steps, controls={**stop, **(controls or {})},
                            wrap_up="Wrap up.")
        return fake, outcome

    @staticmethod
    def wrap_up_at(fake):
        return [i + 1 for i, r in enumerate(fake.requests)
                if r["messages"][-1]["role"] == "user" and r["messages"][-1]["content"].startswith("[supervisor]")]

    def test_int_limit_unchanged(self):
        fake, outcome = self.drive(3, [[read(i)] for i in range(10)])
        self.assertEqual((len(fake.requests), outcome.ended_by, outcome.steps), (3, "steps", 3))
        self.assertEqual(self.wrap_up_at(fake), [3])
        self.assertEqual([t["function"]["name"] for t in fake.requests[2]["tools"]], ["stop"])   # controls only
        self.assertIn("(step 1 of 3)", fake.requests[1]["messages"][-1]["content"])

    def test_repeat_guard_still_stops_identical_tool_calls(self):
        # Pinned: only a repeatable control opts out of the guard; three identical reads still end the run.
        fake, outcome = self.drive(5, [[read(0)], [read(0)], [read(0)], [read(1)]])
        self.assertEqual((len(fake.requests), outcome.ended_by), (3, "repeating"))

    def test_repeatable_control_resets_the_guard(self):
        # read, a repeatable control, then the same read: compared across the control it would count as a repeat.
        ping = agent.Control(_spec("ping", "ping", {}), lambda a: "pong", repeatable=True)
        fake, outcome = self.drive(6, [[read(0)], [("ping", {})], [("ping", {})], [("ping", {})], [read(0)],
                                       [("stop", {})]], controls={"ping": ping})
        self.assertEqual((len(fake.requests), outcome.ended_by), (6, "control"))
        self.assertNotIn(agent.NOT_RUN, [t["result"] for t in outcome.transcript])

    def test_no_limit_unchanged(self):
        fake, outcome = self.drive(0, [*([read(i)] for i in range(5)), [("stop", {})]])
        self.assertEqual((len(fake.requests), outcome.ended_by, self.wrap_up_at(fake)), (6, "control", []))

    def test_callable_raised_mid_run_grants_exactly_one_turn(self):
        limit = [3]
        raise_limit = agent.Control(_spec("raise_limit", "raise", {}), lambda a: limit.__setitem__(0, 4) or "raised")
        fake, outcome = self.drive(lambda: limit[0], [[read(0)], [("raise_limit", {})], *([read(i)] for i in range(1, 10))],
                                   controls={"raise_limit": raise_limit})
        self.assertEqual((len(fake.requests), outcome.ended_by, outcome.steps), (4, "steps", 4))
        self.assertEqual(self.wrap_up_at(fake), [4])   # the wrap-up lands on the new last step, not on step 3
        self.assertIn("(step 3 of 4)", fake.requests[3]["messages"][-2]["content"])   # [-1] is the wrap-up


if __name__ == "__main__":
    unittest.main()
