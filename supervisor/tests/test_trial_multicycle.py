"""#108: consecutive trial cycles share one isolated past state and are judged as whole runs."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import broker, experiments, judge, runner, trial
from supervisor.tests import test_trial as existing
from supervisor.tests.fakellm import FakeLLM


def result(goal="g", kind="live"):
    return {"completed": True, "exit": 0, "ended": "ok", "seconds": 2.0, "usd": 0.01, "model": "cloud",
            "episode": {"kind": kind, "intent": {"goal": goal}, "finish": {"what_happened": goal},
                        "steps": 2, "tools": []}, "trace": [], "diff": "", "output_tail": "",
            "usage": {"calls": 1, "prompt_tokens": 10, "completion_tokens": 5}}


class RunTest(unittest.TestCase):
    """#108: deterministic replay orchestration, independent of model and localhost access."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-multicycle-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = {"t0": "2026-10-10T10:00:00", "start_commit": "start"}
        self.calls = []

    def reconstruct(self, project, name, state, code, dest):
        dest.mkdir(parents=True)
        (dest / "state.txt").write_text("past")
        return dest

    def cycle(self, project, name, state, code, tmp, dest, **kw):
        self.calls.append((dest, kw["kind"], kw["clock"], (dest / "state.txt").read_text()))
        (dest / "state.txt").write_text(kw["kind"])
        return result(f"finish-{len(self.calls)}", kw["kind"])

    def replay(self, **kw):
        with mock.patch.object(trial, "reconstruct", side_effect=self.reconstruct) as rebuild, \
                mock.patch.object(trial, "link_env"), mock.patch.object(trial, "_replay_cycle", side_effect=self.cycle):
            rec = trial.replay_arm(self.tmp, "x", self.state, {}, model="cloud", cache={},
                                   cache_lock=threading.Lock(), billed="x@trial:t", **kw)
        rebuild.assert_called_once()
        return rec

    def test_three_cycles_use_one_copy_and_advance_their_own_clock(self):
        rec = self.replay(cycles=3)
        self.assertEqual([c[3] for c in self.calls], ["past", "live", "live"])
        self.assertEqual(len({c[0] for c in self.calls}), 1)
        start = datetime.fromisoformat(self.state["t0"])
        self.assertEqual([c[2] for c in self.calls], [start + timedelta(seconds=i) for i in (0, 2, 4)])
        self.assertEqual(len(rec["cycles"]), 3)
        self.assertEqual(rec["sleeps"], [])
        self.assertEqual(rec["seconds"], 6)
        self.assertEqual(rec["usage"]["calls"], 3)
        self.assertEqual(rec["endpoints"], {"completed_cycles": 3, "steps": 6})

    def test_sleep_between_every_pair_is_visible_to_the_next_cycle(self):
        rec = self.replay(cycles=3, sleep_between=True)
        self.assertEqual([c[1] for c in self.calls], ["live", "sleep", "live", "sleep", "live"])
        self.assertEqual([c[3] for c in self.calls], ["past", "live", "sleep", "live", "sleep"])
        self.assertEqual(len(rec["sleeps"]), 2)
        self.assertEqual(rec["usd"], 0.05)
        self.assertEqual(rec["usage"]["calls"], 5)

    def test_one_cycle_keeps_the_original_result_shape(self):
        self.assertEqual(self.replay(cycles=1), result("finish-1"))
        self.assertEqual(self.replay(cycles=1, sleep_between=True), result("finish-2"))

    def test_failed_cycle_stops_the_run_and_completion_rule_applies(self):
        with mock.patch.object(trial, "reconstruct", side_effect=self.reconstruct), \
                mock.patch.object(trial, "link_env"), \
                mock.patch.object(trial, "_replay_cycle", return_value={**result(), "completed": False, "exit": 1}):
            rec = trial.replay_arm(self.tmp, "x", self.state, {}, model="cloud", cache={},
                                   cache_lock=threading.Lock(), billed="x@trial:t", cycles=3)
        self.assertEqual(len(rec["cycles"]), 1)
        self.assertEqual(trial.decide({"A": result(), "B": rec}, [None, None])[0], "loss")

    def test_between_operation_stop_keeps_completed_cycle_and_stops_whole_run(self):
        charged = []
        rec = self.replay(cycles=3, on_unit=charged.append, should_stop=lambda: bool(charged))
        self.assertEqual(len(charged), 1)
        self.assertTrue(rec["cycles"][0]["completed"])
        self.assertFalse(rec["completed"])
        self.assertEqual(rec["ended"], "preempted")

    def test_replay_unit_limit_counts_sleeps(self):
        with mock.patch.object(trial, "N_MAX", 4):
            self.assertEqual(trial.replay_units(3), 3)
            with self.assertRaisesRegex(trial.TrialError, "SHAFT_TRIAL_N_MAX"):
                trial.replay_units(3, True)
        for cycles in (0, -1, True, 1.5):
            with self.assertRaises(trial.TrialError):
                trial.replay_units(cycles)

    def test_pairwise_judge_receives_all_cycles_and_sleeps(self):
        a = self.replay(cycles=3, sleep_between=True)
        b = self.replay(cycles=3, sleep_between=True)
        with mock.patch.object(judge, "ask", return_value={"winner": "tie"}) as ask:
            trial.judge_pair("past", a, b, "test")
        system, prompt = ask.call_args.args[:2]
        self.assertIn("whole runs", system)
        for i in range(1, 11):
            self.assertIn(f'"what_happened": "finish-{i}"', prompt)
        self.assertEqual(prompt.count("## Live cycle"), 6)
        self.assertEqual(prompt.count("## Sleep after cycle"), 4)

    def test_operation_sets_broker_and_prompt_clock_and_counts_only_its_usage(self):
        dest = self.tmp / "x"
        dest.mkdir()
        fake = mock.Mock(trace=[])
        clock = datetime.fromisoformat(self.state["t0"]) + timedelta(hours=1)
        with mock.patch.object(broker, "ReplayBroker", return_value=fake) as make, \
                mock.patch.object(trial, "secrets_env", return_value={}), \
                mock.patch.object(trial, "_config", return_value={}), \
                mock.patch.object(trial, "_llm_usage", side_effect=[{"calls": 2}, {"calls": 3}]), \
                mock.patch.object(trial, "_cloud_usd", side_effect=[0.5, 0.51]), \
                mock.patch.object(trial, "_new_episodes", return_value=[result()["episode"]]), \
                mock.patch.object(runner, "agent_command", return_value=(["fake"], {})) as command, \
                mock.patch.object(trial.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            rec = trial._replay_cycle(self.tmp, "x", self.state, {}, self.tmp, dest, kind="live", clock=clock,
                                      model="cloud", cache={}, cache_lock=threading.Lock(),
                                      billed="x@trial:t:u1", should_stop=lambda: False)
        self.assertEqual(make.call_args.kwargs["as_of"], clock)
        self.assertEqual(fake.history_as_of, datetime.fromisoformat(self.state["t0"]))
        self.assertEqual(command.call_args.args[-1]["SHAFT_NOW"], "2026-10-10T11:00:00")
        self.assertEqual(rec["usage"], {"calls": 1})
        self.assertEqual(rec["usd"], 0.01)

    def test_usage_counts_the_supervisors_late_rows(self):
        """Bug 2026-11-12: a plan channel that reports a turn's usage when the turn ends books it after the instance's
        last reply, so a replay of a real cycle on antigravity/ read 0 tokens though 471,589 were booked."""
        dest = self.tmp / "y"
        dest.mkdir()
        zero = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "model_seconds": 0.0, "errors": 0}
        with mock.patch.object(broker, "ReplayBroker", return_value=mock.Mock(trace=[])), \
                mock.patch.object(trial, "secrets_env", return_value={}), \
                mock.patch.object(trial, "_config", return_value={}), \
                mock.patch.object(trial, "_llm_usage", side_effect=[zero, {**zero, "calls": 16}]), \
                mock.patch.object(trial, "_cloud_usd", side_effect=[0.0, 0.0]), \
                mock.patch.object(trial, "_cloud_tokens", side_effect=[(100, 10), (471_689, 2_136)]), \
                mock.patch.object(trial, "_new_episodes", return_value=[result()["episode"]]), \
                mock.patch.object(runner, "agent_command", return_value=(["fake"], {})), \
                mock.patch.object(trial.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            rec = trial._replay_cycle(self.tmp, "y", self.state, {}, self.tmp, dest, kind="live",
                                      clock=datetime.fromisoformat(self.state["t0"]), model="antigravity/gemini-4-argon",
                                      cache={}, cache_lock=threading.Lock(), billed="y@trial:t", should_stop=lambda: False)
        self.assertEqual((rec["usage"]["prompt_tokens"], rec["usage"]["completion_tokens"]), (471_589, 2_126))
        self.assertEqual(rec["usage"]["calls"], 16)

    def test_experiment_endpoint_is_any_over_cycles(self):
        a, b = result(), result()
        b["episode"]["ended_by"] = "steps"
        self.assertFalse(experiments.endpoint("forecast", {"cycles": [a, b]}, "ran out"))   # it still finished
        b["episode"]["finish"] = None
        self.assertTrue(experiments.endpoint("forecast", {"cycles": [a, b]}, "ran out"))


class LedgerTest(existing.Fixture):
    """#108: each cycle and sleep has its own ledger row, without double charging aggregates."""

    def setUp(self):
        super().setUp()
        # its own ledger: ten local rows left in the shared one made test_trial's local-time tests, run after this
        # module, see today's local units as spent and send their replays to the cloud
        ledger = Path(tempfile.mkdtemp(prefix="shaft-ledger-")) / "trial_usage.jsonl"
        self.addCleanup(shutil.rmtree, ledger.parent, True)
        self.enterContext(mock.patch.object(trial, "LEDGER", ledger))

    def test_ledger_counts_three_cycles_and_two_sleeps_per_arm(self):
        t = trial.create(existing.NAME, {}, "test", by="operator", n=2, cycles=3, sleep_between=True,
                         allow_empty=True, project=self.project)
        t["by"] = "instance"
        trial.save(t)
        usage_before = trial.usage_today(existing.NAME)

        def unit(*a, **kw):
            return result(kind=kw["kind"])
        with mock.patch.object(trial, "_replay_cycle", side_effect=unit), \
                mock.patch.object(trial, "local_free_for", return_value=(True, "")), \
                mock.patch.object(judge, "pick", return_value="judge"), \
                mock.patch.object(trial, "judge_pair", return_value={"winner": "tie", "reasoning": "equal"}), \
                mock.patch.object(trial, "_judge_usd", return_value=0), \
                mock.patch.object(trial, "LOCAL_UNITS_PER_DAY", 1000):   # other modules' trials share today's ledger
            trial.run_pair(self.project, t, 0, "local")
        rows = [json.loads(line) for line in trial.LEDGER.read_text().splitlines()]
        rows = [r for r in rows if r.get("trial") == t["id"] and r["kind"] == "arm"]
        self.assertEqual(len(rows), 10)
        self.assertEqual(sum(r["replay_kind"] == "sleep" for r in rows), 4)
        self.assertAlmostEqual(sum(r["usd"] for r in rows), 0.1)
        self.assertEqual(sum(r["tokens_in"] for r in rows), 100)
        usage = trial.usage_today(existing.NAME)
        self.assertAlmostEqual(usage["usd"] - usage_before["usd"], 0.1)
        self.assertEqual(usage["local_units"] - usage_before["local_units"], 10)


class LiveRunTest(existing.Fixture):
    """#108: real agent and ReplayBroker against the existing scripted fake LLM."""

    def setUp(self):
        self.fake = FakeLLM([])
        self.addCleanup(self.fake.close)
        self.llm_url = self.fake.url
        super().setUp()
        self.keep = self.project / "kept"

    def live(self, i):
        return [
            {"tool_calls": [("declare_intent", {"goal": f"cycle {i}", "success_criterion": "saved",
                                                 "confidence": 60, "origin": "instance", "origin_ref": "own goal"})]},
            {"tool_calls": [("write_file", {"path": f"notes/cycle-{i}.md", "content": f"CYCLE-{i}"})]},
            {"tool_calls": [("send_message", {"to": "operator", "text": f"cycle {i}"})]},
            {"tool_calls": [("http_request", {"url": "https://example.com/mail", "method": "POST", "body": "mail"})]},
            {"tool_calls": [("finish_cycle", {"delivered_via": "none", "what_happened": f"cycle {i}",
                                               "criterion_met": "yes", "learned": "saved"})]},
        ]

    def run_replay(self, cycles, sleep_between=False):
        with mock.patch.object(trial, "secrets_env", return_value={}), \
                mock.patch.object(broker, "notify") as notify, \
                mock.patch.object(broker.Broker, "_send", side_effect=AssertionError("delivered")), \
                mock.patch.object(runner, "_settle", side_effect=AssertionError("settled")):
            rec = trial.replay_arm(self.project, existing.NAME, self.state(),
                                   trial.committed_code(self.project, existing.NAME), model="local", cache={},
                                   cache_lock=threading.Lock(), billed="tw@trial:test", cycles=cycles,
                                   sleep_between=sleep_between, keep=self.keep)
        notify.assert_not_called()
        self.assertTrue(rec["completed"], rec["output_tail"])
        return rec

    def test_three_live_cycles_preserve_live_bytes_and_record_writes(self):
        second = self.live(2)
        second.insert(1, {"tool_calls": [("read_file", {"path": "notes/cycle-1.md"})]})
        self.fake.replies = self.live(1) + second + self.live(3)
        before = existing.snapshot(self.root)
        tip = existing.git(self.root, "rev-parse", "HEAD")
        rec = self.run_replay(3)
        self.assertEqual(existing.snapshot(self.root), before)
        self.assertEqual(existing.git(self.root, "rev-parse", "HEAD"), tip)
        self.assertEqual([c["episode"]["intent"]["goal"] for c in rec["cycles"]], ["cycle 1", "cycle 2", "cycle 3"])
        self.assertIn("CYCLE-1", json.dumps(rec["cycles"][1]["episode"]["tools"]))
        writes = [c for c in rec["trace"] if c["tool"] in ("send_message", "http_request")]
        self.assertEqual(len(writes), 6)
        self.assertTrue(all(any("simulated" in tag for tag in c["tags"]) for c in writes))

    def test_sleep_digest_is_in_cycle_twos_prompt(self):
        self.fake.replies = self.live(1) + [
            {"tool_calls": [("write_digest", {"text": "SLEEP-DIGEST-108"})]},
            {"content": "Consolidated the first cycle."},
        ] + self.live(2)
        rec = self.run_replay(2, True)
        self.assertEqual(len(rec["sleeps"]), 1)
        self.assertIn("SLEEP-DIGEST-108", json.dumps(self.fake.requests[-5]))
