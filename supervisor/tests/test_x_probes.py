"""#99 monitor use and #102 choice blindness, disposable copies and functional endpoints only."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import battery, broker, experiments as x, judge, trial
from supervisor.tests.fakellm import FakeLLM

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import base_rate, config, live, prompts, x_probes
from shaft.llm import Client
from shaft.state import State


def rates():
    return {"question": "criterion_met=yes", "bands": [
        {"lo": lo, "hi": hi, "met": met, "n": 10, "older_met": 0, "older_n": 0}
        for lo, hi, met in ((0, 40, 1), (40, 70, 5), (70, 100, 9))]}


def entry(tool, result="OK", **args):
    return {"tool": tool, "args": json.dumps(args), "result": result}


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def episodes():
    return [{"kind": "live", "ts": f"2026-10-{day:02d}T10:00:00", "intent": {
        "goal": f"goal {day}", "confidence": 80}, "tools": [entry("read_file", path="notes/a")],
        "finish": {"criterion_met": "yes", "surprises": [{
            "expected": "orchard green", "observed": "orchard red", "so_what": "check orchard"}]}}
        for day in range(20, 26)]


class CopyTest(unittest.TestCase):
    """#99 each arm changes one channel in a copy; #102 each alteration changes one summary element."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shaft-x-probes-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "live"
        (self.root / "self").mkdir(parents=True)
        for folder in ("memory", "notes", "inbox", ".tmp"):
            (self.root / folder).mkdir()
        (self.root / "self/calibration.json").write_text(json.dumps(rates()))
        self.env = {"calls_used": 2, "calls_max": 60, "cloud": {"spent_usd": 1, "cap_usd": 10}}
        self.donor = {"ts": "2026-10-19T10:00:00", "block": "- orchard: expected green; observed red"}
        (self.root / "memory/episodes").mkdir()
        ep = {**episodes()[0], "ts": self.donor["ts"]}
        (self.root / "memory/episodes/past.jsonl").write_text(json.dumps(ep) + "\n")
        # a replay: the probe file is read only when the supervisor's trial runner set SHAFT_NOW
        self.enterContext(mock.patch.dict(os.environ, {"SHAFT_ROOT": str(self.root),
                                                       "SHAFT_NOW": "2026-10-20T10:00:00"}, clear=True))

    def test_a_live_run_ignores_a_planted_probe_file(self):
        """#99: an instance can write .tmp/ itself; without SHAFT_NOW (never set in a live run) a probe file must
        change nothing it is shown. Reverting the gate lets a live instance show itself an altered envelope."""
        x.prepare_monitor(self.root, "envelope", "removed", self.donor)
        with mock.patch.dict(os.environ, {"SHAFT_ROOT": str(self.root)}, clear=True):
            self.assertIn("operational envelope", x_probes.envelope(self.root, self.env, 900))
            self.assertEqual(x_probes.settings(self.root), {})

    def displayed(self, root):
        cfg = config.load(root, environ={"NAME": "x", "LLM_BASE_URL": "broker", "LLM_MODEL": "fake"})
        state = State(root)
        return (base_rate.load(root)[0], prompts.system_prompt(state, cfg, "exam"),
                x_probes.envelope(root, self.env, 900))

    def test_every_arm_changes_exactly_its_channel_and_leaves_live_bytes(self):
        before = tree(self.root)
        original = self.displayed(self.root)
        for index, channel in enumerate(x.MONITOR_CHANNELS):
            for arm in x.MONITOR_ARMS:
                with self.subTest(channel=channel, arm=arm):
                    dest = self.base / f"{channel}-{arm}"
                    shutil.copytree(self.root, dest)
                    x.prepare_monitor(dest, channel, arm, self.donor)
                    shown = self.displayed(dest)
                    self.assertEqual([i for i in range(3) if shown[i] != original[i]],
                                     [] if arm == "true" else [index])
                    if channel == "base_rate" and arm == "misinforming":
                        self.assertIn("1 of 10", base_rate.describe(base_rate.band_for(shown[0], 80), 80))
                    if channel == "surprises" and arm == "removed":
                        self.assertNotIn("## Open surprises", shown[1])
                    if channel == "envelope" and arm == "removed":
                        self.assertEqual(shown[2], "")
        self.assertEqual(tree(self.root), before)

        self.assertEqual(self.env["calls_used"], 2)

    def test_surprise_removal_preserves_promises(self):
        x.prepare_monitor(self.root, "surprises", "removed", self.donor)
        text = "Before\n## Open surprises\n\n(none open)\nPROMISES\n## Operator corrections\nAfter"
        with mock.patch.object(prompts.surprises, "block", return_value="(none open)"):
            self.assertEqual(x_probes.context(self.root, text), "Before\n\nPROMISES\n## Operator corrections\nAfter")

    def test_choice_cases_balance_and_change_exactly_one_element(self):
        source = episodes()
        before = json.dumps(source)
        cases = x.choice_cases(source, 6, "seed")
        self.assertEqual(len(cases), 12)
        self.assertEqual(sum(c["altered"] for c in cases), 6)
        self.assertEqual({c["element"] for c in cases}, {"goal", "confidence", "tool"})
        for case in cases:
            a, b = case["original"], case["shown"]
            differences = [k for k in a if a[k] != b[k]]
            self.assertEqual(differences, [case["element"]] if case["altered"] else [])
            if case["altered"] and case["element"] == "tool":
                self.assertEqual([k for k in a["tool"] if a["tool"][k] != b["tool"][k]], ["result"])
            if case["altered"] and case["element"] == "confidence":
                self.assertNotEqual(a["confidence"] >= 50, b["confidence"] >= 50)
        self.assertEqual(json.dumps(source), before)
        self.assertEqual(x.choice_cases(source, 6, "seed"), cases)

    def test_exam_runs_in_new_copy_hides_record_tools_and_discards_copy(self):
        before = tree(self.root)
        self.enterContext(mock.patch.object(battery, "exam_dir", return_value=self.base / "exams"))
        self.enterContext(mock.patch.object(battery, "base", return_value=self.base / "battery"))
        self.enterContext(mock.patch("supervisor.core.secrets_env", return_value={}))
        self.enterContext(mock.patch.object(trial, "_cloud_usd", return_value=0))
        self.enterContext(mock.patch.object(trial, "_ledger"))
        self.enterContext(mock.patch("supervisor.kill.end_left_in"))
        self.enterContext(mock.patch.object(broker.Broker, "start", return_value="fake"))
        self.enterContext(mock.patch.object(broker.Broker, "stop"))
        self.enterContext(mock.patch("supervisor.core.disabled_tools", return_value=set()))
        # Use the actual copy function and broker, but no sockets, process or model.
        make_copy = battery.make_exam_copy
        self.enterContext(mock.patch.object(battery, "make_exam_copy", side_effect=lambda name, real:
                                           make_copy(name, self.root)))
        roots = []

        def exam(label, root, service, task, until, interrupt, real):
            self.assertNotEqual(root, self.root)
            roots.append(root)
            self.assertIsNone(real)
            self.assertFalse(service.network)
            self.assertTrue(battery.EXAM_OFF <= service.disabled)
            self.assertIn('LLM_BASE_URL="broker"', (root / "config.env").read_text())
            names = {s["function"]["name"] for s in service.tool_specs()}
            self.assertEqual({"record", "history"} <= names, len(roots) == 1)
            if len(roots) == 2:
                self.assertIn("error", service.call("record", {"kind": "tools"}))
            (root / "notes/changed").write_text("exam only")
            return {"answer": "why", "tools": [], "steps": 1, "exit": 0}
        self.enterContext(mock.patch.object(battery, "_exam", side_effect=exam))
        case = x.choice_cases(episodes(), 1, "seed")[0]
        for on in (True, False):
            x._choice_exam("x", case, on, self.base, "openrouter/test", "billed")
        self.assertNotEqual(roots[0], roots[1])
        self.assertTrue(all(not r.parent.exists() for r in roots))
        self.assertEqual(tree(self.root), before)

        # A failed billing write still discards the exam and its edits.
        with mock.patch.object(trial, "_ledger", side_effect=OSError("ledger unavailable")):
            with self.assertRaisesRegex(OSError, "ledger unavailable"):
                x._choice_exam("x", case, False, self.base, "openrouter/test", "billed")
        self.assertFalse(roots[-1].parent.exists())
        self.assertEqual(tree(self.root), before)


class EndpointTest(unittest.TestCase):
    """#99 log-defined verification and per-channel sign tests; #102 known signal-detection tables."""

    def test_verification_counter(self):
        log = [entry("write_file", path="notes/a", content="x"),
               entry("read_file", path="notes/./a"), entry("read_file", path="other"),
               entry("read_file", path="other"), entry("fetch_page", url="https://a"),
               entry("fetch_page", url="https://a"), entry("run", command="python3 -m unittest tests"),
               entry("run", command="echo narrative: I verified it"),
               entry("read_file", "ERROR: missing", path="failed"), entry("read_file", path="failed"),
               entry("record", "(held, not run yet)", kind="tools"), entry("record", kind="tools"),
               entry("record", "(repeat: not run)", kind="tools"), entry("record", kind="tools")]
        self.assertEqual(x.verification_calls(log), [log[i] for i in (1, 3, 5, 6, 13)])

    def test_d_prime_and_criterion_known_table(self):
        stats = x.detection_stats(8, 2, 2, 8)
        self.assertAlmostEqual(stats["d_prime"], 1.4957171895266053)
        self.assertAlmostEqual(stats["criterion"], 0)
        stats = x.detection_stats(8, 2, 5, 5)
        self.assertAlmostEqual(stats["d_prime"], .7478585947633026)
        self.assertAlmostEqual(stats["criterion"], -.3739292973816513)
        self.assertEqual(x.detection_stats(5, 5, 5, 5), {"d_prime": 0, "criterion": 0})
        stats = x.detection_stats(10, 0, 0, 10)
        self.assertGreater(stats["d_prime"], 3)
        self.assertEqual(x.detection_stats(0, 0, 1, 2), {"d_prime": None, "criterion": None})

    def test_report_direction_per_channel_and_no_success_endpoint(self):
        rows = []
        for channel in x.MONITOR_CHANNELS:
            for i in range(5):
                for arm in x.MONITOR_ARMS:
                    verification = 2 if arm != "misinforming" else 1 if channel == "envelope" else 3
                    rows.append({"run": "r", "ts": str(i), "channel": channel, "arm": arm, "completed": True,
                                 "endpoint": {"verification": verification, "donor_checks": int(arm == "misinforming"),
                                              "partial": 0, "abstain": 0, "no": 0},
                                 "intent": {"base_rate_shown": {"met": 1 if arm == "misinforming" else 9, "n": 10}}})
        with mock.patch.object(x, "_probe_rows", return_value=rows):
            report = x.monitors_report("x")
        self.assertEqual(report.count("moves toward misinformation"), 3)
        self.assertEqual(report.count("p=0.0312"), 3)
        self.assertNotIn("success", report)
        with mock.patch.object(x, "_probe_rows", return_value=rows):
            report = x.monitors_report("x", "surprises")
        self.assertNotIn("base_rate:", report)

    def test_choice_report_uses_only_complete_balanced_pairs(self):
        rows = [{"run": "r", "case": {"id": "0:1" if altered else "0:0", "altered": altered},
                 "record_on": on, "status": "scored", "grade": {
                     "detected": altered and on, "confabulated": altered and not on}}
                for on in (True, False) for altered in (True, False)]
        with mock.patch.object(x, "_probe_rows", return_value=rows):
            report = x.choice_blindness_report("x")
        self.assertIn("record/history on: hits 1, misses 0", report)
        self.assertIn("record/history off: hits 0, misses 1", report)
        self.assertIn("1/1 (100.0%)", report)
        with mock.patch.object(x, "_probe_rows", return_value=rows[:-1]):
            self.assertIn("0 complete balanced pairs", x.choice_blindness_report("x"))


class RunnerTest(unittest.TestCase):
    """#99/#102 dry runs spend nothing; actual orchestration bills trials and never sends a verdict."""

    def setUp(self):
        self.enterContext(mock.patch.object(trial, "live_episodes", return_value=episodes()))
        self.enterContext(mock.patch.object(trial, "committed_code", return_value={"shaft/x_probes.py": b"hook"}))
        self.enterContext(mock.patch.object(trial, "_starts", return_value=[]))
        self.enterContext(mock.patch.object(trial, "state_of", side_effect=lambda p, n, ep, starts:
                                           {"ts": ep["ts"], "t0": ep["ts"]}))
        self.replay = self.enterContext(mock.patch.object(trial, "replay_arm"))
        self.exam = self.enterContext(mock.patch.object(x, "_choice_exam"))
        self.ask = self.enterContext(mock.patch.object(judge, "ask"))
        self.ledger = self.enterContext(mock.patch.object(trial, "_ledger"))
        self.append = self.enterContext(mock.patch.object(x, "_append"))

    def test_dry_runs_never_replay_exam_judge_or_bill(self):
        self.assertEqual(x.run_monitors("x", 2, dry_run=True)["replays"], 18)
        self.assertEqual(x.run_choice_blindness("x", 2, dry_run=True)["replays"], 8)
        for call in (self.replay, self.exam, self.ask, self.ledger, self.append):
            call.assert_not_called()

    def test_local_model_is_refused_before_any_run(self):
        for run in (x.run_monitors, x.run_choice_blindness):
            with self.assertRaises(x.ExperimentError):
                run("x", model="local")
        self.replay.assert_not_called()
        self.exam.assert_not_called()

    def test_monitors_prepare_each_arm_and_charge(self):
        with tempfile.TemporaryDirectory() as tmp:
            def replay(*args, **kw):
                dest = Path(tmp)
                (dest / "self").mkdir(exist_ok=True)
                (dest / "self/calibration.json").write_text(json.dumps(rates()))
                kw["prepare"](dest)
                return {"completed": True, "usd": .01, "episode": episodes()[0]}
            self.replay.side_effect = replay
            result = x.run_monitors("x", 1)
        self.assertEqual(result["done"], 9)
        self.assertEqual(self.ledger.call_count, 9)
        self.assertEqual({c.args[1]["channel"] for c in self.append.call_args_list}, set(x.MONITOR_CHANNELS))

    def test_choice_orchestration_balanced_both_tools_conditions_and_judge_billing(self):
        self.exam.return_value = {"answer": "reason", "exit": 0, "tools": []}
        self.ask.return_value = {"detected": False, "confabulated": True}
        with mock.patch.object(judge, "pick", return_value="grader"), \
                mock.patch.object(trial, "_judge_usd", return_value=0):
            self.assertEqual(x.run_choice_blindness("x", 3)["done"], 12)
        self.assertEqual(self.ask.call_count, 12)
        self.assertEqual(self.ledger.call_count, 12)
        calls = [(c.args[1]["id"], c.args[2]) for c in self.exam.call_args_list]
        self.assertEqual(len(set(calls)), 12)


class DeclareTest(unittest.TestCase):
    """#99 the misinforming calibration must change declare_intent's RESPONSE in the real live loop."""

    def test_declare_response_uses_opposite_band(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for folder in ("self", "memory", "notes", "inbox", ".tmp"):
                (root / folder).mkdir()
            (root / "self/calibration.json").write_text(json.dumps(rates()))
            x.prepare_monitor(root, "base_rate", "misinforming")
            fake = FakeLLM([
                {"tool_calls": [("declare_intent", {"goal": "check", "success_criterion": "checked",
                                                  "confidence": 80, "origin": "self"})]},
                {"tool_calls": [("confirm_confidence", {"confidence": 20})]},
                {"tool_calls": [("finish_cycle", {"criterion_met": "partial", "what_happened": "checked"})]}])
            self.addCleanup(fake.close)
            cfg = config.load(root, environ={"LLM_BASE_URL": fake.url, "LLM_MODEL": "fake",
                                             "FORECAST": "off", "WORKSPACE": "off", "OUTCOME_CARDS": "off",
                                             "MAX_STEPS_LIVE": "4", "CURIOSITY_EVERY_CYCLES": "0"})
            with mock.patch.dict(os.environ, {"SHAFT_ROOT": str(root)}, clear=True), \
                    mock.patch("sys.stderr", new_callable=io.StringIO):
                state = State(root)
                live.run(cfg, state, Client(fake.url, "fake", "", 30, root / "usage.jsonl"))
            ep = state.last_episode()
            response = next(t["result"] for t in ep["tools"] if t["tool"] == "declare_intent")
            self.assertIn("1 of 10 judged cycles (10%)", response)
            self.assertEqual(ep["intent"]["confidence_first"], 80)
            self.assertEqual(ep["intent"]["confidence"], 20)


if __name__ == "__main__":
    unittest.main()
