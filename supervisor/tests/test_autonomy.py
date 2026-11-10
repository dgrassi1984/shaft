"""The schedule SHAFT's scheduler keeps: per-instance wakes (the operator: an interval in each config.env; a wake
that comes while the agent is already awake is skipped) and the nightly sleep — including the sleep launchd
silently skipped on 2026-10-13."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy
from supervisor.autonomy import decide_wakes, sleep_due

D = datetime.fromisoformat
H2 = timedelta(hours=2)


class SleepDueTest(unittest.TestCase):
    def test_a_missed_sleep_is_made_up_later_that_day(self):
        # Regression: launchd's domain went on-demand-only; the 04:00 sleep never started and nobody noticed.
        st = {"sleep": "2026-10-12T04:00:05"}
        self.assertTrue(sleep_due(D("2026-10-13T04:45:00"), st))
        self.assertTrue(sleep_due(D("2026-10-13T15:00:00"), st))

    def test_one_sleep_per_day_and_not_before_its_time(self):
        st = {"sleep": "2026-10-13T04:00:02"}
        self.assertFalse(sleep_due(D("2026-10-13T04:30:00"), st))
        self.assertFalse(sleep_due(D("2026-10-14T03:59:00"), st))
        self.assertTrue(sleep_due(D("2026-10-14T04:00:00"), st))


class WakeTest(unittest.TestCase):
    def test_each_instance_on_its_own_interval(self):
        st = {"wakes": {"shaft": "2026-10-13T10:00:00", "wisp": "2026-10-13T10:00:00"}}
        every = {"shaft": H2, "wisp": timedelta(minutes=30)}
        start, skip = decide_wakes(D("2026-10-13T10:45:00"), st, ["shaft", "wisp"], every.get, lambda n: "")
        self.assertEqual((list(start), skip), (["wisp"], {}))
        start, _ = decide_wakes(D("2026-10-13T12:00:00"), st, ["shaft", "wisp"], every.get, lambda n: "")
        self.assertEqual(list(start), ["shaft", "wisp"])

    def test_a_wake_while_already_awake_is_skipped_not_queued(self):
        st = {"wakes": {"shaft": "2026-10-13T10:00:00"}}
        start, skip = decide_wakes(D("2026-10-13T12:00:00"), st, ["shaft"], lambda n: H2,
                                   lambda n: "talk since 11:40")
        self.assertEqual(start, {})
        self.assertEqual(skip, {"shaft": "talk since 11:40"})

    def test_a_queued_update_starts_once_idle_and_holds_back_the_wake(self):
        # Regression (2026-10-25): an update was refused while busy and lost every retry to the next cycle.
        queued = {"x": "2026-10-25T12:45:00", "y": "2026-10-25T12:45:00"}
        with mock.patch.object(autonomy, "update_queued", queued.get):
            self.assertEqual(autonomy.update_jobs({}, ["x", "y", "z"], {"y": "live since 12:39"}.get), ["x"])
            self.assertEqual(autonomy.update_jobs({"update x": None}, ["x"], lambda n: ""), [])   # running
            self.assertEqual(autonomy.update_jobs({"wake x": None}, ["x"], lambda n: ""), [])     # waking
            self.assertTrue(autonomy.updating("y", {}))
            self.assertTrue(autonomy.updating("z", {"update z": None}))
            self.assertFalse(autonomy.updating("z", {}))

    def test_an_update_that_died_before_taking_the_lock_is_dropped_not_retried(self):
        # Regression (Codex on 784982a): an update child that failed before the lock (e.g. instance_root refusing a
        # folder without .git) left its marker, so the scheduler retried it every tick and held the wakes back.
        queued = {"x": "2026-10-25T12:45:00"}
        with mock.patch.object(autonomy, "update_queued", queued.get), mock.patch.object(autonomy, "log"), \
                mock.patch.object(autonomy, "set_update_queued", lambda n, on: queued.pop(n)) as _:
            autonomy.update_ended("x", 0)          # it found the instance busy and re-queued itself: kept
            self.assertIn("x", queued)
            autonomy.update_ended("x", 1)          # died before running: dropped
            self.assertNotIn("x", queued)
            autonomy.update_ended("x", 1)          # ran and failed (dequeued itself already): nothing to do

    def test_never_woken_is_due_and_zero_means_never(self):
        start, _ = decide_wakes(D("2026-10-13T12:00:00"), {}, ["new", "off"],
                                {"new": H2, "off": None}.get, lambda n: "")
        self.assertEqual(list(start), ["new"])

    def test_interval_from_config_env_without_a_default_floor(self):
        def interval(value):
            root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
            (root / "config.env").write_text(f"WAKE_INTERVAL_MINUTES={value}\n" if value is not None else "")
            with mock.patch("supervisor.core.instance_root", return_value=root), mock.patch.object(autonomy, "log"):
                return autonomy.wake_interval("x")
        self.assertEqual(interval(None), timedelta(minutes=120))      # not set: the old two hours
        self.assertEqual(interval(45), timedelta(minutes=45))
        self.assertEqual(interval(1), timedelta(minutes=1))           # no operator floor: its own interval
        self.assertIsNone(interval(0))
        self.assertEqual(interval("soon"), timedelta(minutes=120))    # not a number: the default, logged

    def test_the_instance_is_told_its_own_interval(self):
        with mock.patch.object(autonomy, "wake_interval", return_value=timedelta(minutes=45)), \
             mock.patch.object(Path, "exists", return_value=True):
            text = autonomy.describe_schedule("wisp")
        self.assertIn("every 45 minutes", text)
        self.assertIn("is skipped", text)



class GateWarmTest(unittest.TestCase):
    """The scheduler starts `shaft gate warm` when main moves to a new commit, checks again hourly
    (the child exits at once while the record is fresh), and never retries a commit whose warm-up failed."""

    def setUp(self):
        self.pair = "a" * 16
        self.started = []
        p = mock.patch.object(autonomy.gate, "commits_id", lambda: self.pair)
        p.start()
        self.addCleanup(p.stop)

    def start(self, args):
        self.started.append(args)
        return object()

    def tick(self, children, st, at):
        autonomy.gate_warm(children, st, self.start, now=at)

    def test_a_new_commit_starts_one_warm_up_and_the_same_commit_is_checked_hourly(self):
        st, children = autonomy.WarmState(), {}
        self.tick(children, st, 0)
        self.assertEqual(self.started, [["gate", "warm"]])
        self.assertIn("gate warm", children)
        self.tick(children, st, 5)                        # running: not a second one
        children.clear()
        self.tick(children, st, 60)                       # same commit, within the hour
        self.assertEqual(len(self.started), 1)
        self.pair = "b" * 16                              # main moved
        self.tick(children, st, 120)
        self.assertEqual(len(self.started), 2)
        children.clear()
        self.tick(children, st, 120 + autonomy.WARM_CHECK_S + 1)   # the hourly check of the same commit
        self.assertEqual(len(self.started), 3)

    def test_timed_out_warm_up_retries_hourly_after_ending_indefinitely(self):
        st, children = autonomy.WarmState(), {}
        self.tick(children, st, 0)
        for attempt in range(3):
            children.clear()
            with mock.patch.object(autonomy, "log"):
                autonomy.warm_ended(st, autonomy.gate.WARM_TIMED_OUT, 300)
            self.assertTrue(st.timed_out)
            self.assertNotIn(self.pair, st.failed)
            ended = st.started + 300
            self.tick(children, st, ended + autonomy.WARM_RETRY_S - 1)
            self.assertEqual(len(self.started), attempt + 1)
            self.tick(children, st, ended + autonomy.WARM_RETRY_S)
            self.assertEqual(len(self.started), attempt + 2)

    def test_a_failed_warm_up_is_not_retried_on_its_commit(self):
        st, children = autonomy.WarmState(), {}
        self.tick(children, st, 0)
        children.clear()
        with mock.patch.object(autonomy, "log"):
            autonomy.warm_ended(st, autonomy.gate.WARM_FAILED)
        self.tick(children, st, autonomy.WARM_CHECK_S * 5)
        self.assertEqual(len(self.started), 1)
        self.pair = "c" * 16                              # a new commit gets its own try
        self.tick(children, st, autonomy.WARM_CHECK_S * 5)
        self.assertEqual(len(self.started), 2)
        children.clear()
        with mock.patch.object(autonomy, "log"):
            autonomy.warm_ended(st, 0)                    # a pass marks nothing
        self.tick(children, st, autonomy.WARM_CHECK_S * 7)
        self.assertEqual(len(self.started), 3)

    def test_the_scheduler_loop_starts_the_warm_up_and_hears_how_it_ended(self):
        # Wired into serve, not only defined: two ticks of the real loop, everything around the warm-up stubbed.
        class Stop(Exception):
            pass

        class Done:                                       # the warm-up child, finished with a failure
            returncode, stderr = 1, None

            def poll(self):
                return 1
        seen, ended = {}, []

        def fake_warm(children, st, start, now=None):
            seen.setdefault("children", children)
            seen.setdefault("state", st)
            seen.setdefault("start", start)

        def fake_sleep(_):
            if "ticked" in seen:
                raise Stop
            seen["ticked"] = True
            seen["children"]["gate warm"] = (Done(), 0.0)  # it ends before the next tick
        sup = Path(tempfile.mkdtemp(prefix="shaft-sched-"))
        self.addCleanup(__import__("shutil").rmtree, sup, True)
        with mock.patch.object(autonomy, "gate_warm", fake_warm), \
                mock.patch.object(autonomy, "warm_ended", lambda st, code, secs: ended.append((st, code))), \
                mock.patch.object(autonomy.time, "sleep", fake_sleep), \
                mock.patch.object(autonomy, "SUP_DIR", sup), mock.patch.object(autonomy, "log"), \
                mock.patch.object(autonomy, "stop_children_on_signal"), \
                mock.patch("supervisor.version.record_service"), \
                mock.patch("supervisor.browser.startup_note", return_value=""), \
                mock.patch.object(autonomy, "instance_names", return_value=[]), \
                mock.patch.object(autonomy, "_state", return_value={}), \
                mock.patch.object(autonomy, "sleep_due", return_value=False), \
                mock.patch.object(autonomy, "_batteries"), mock.patch("supervisor.jobs.tick"), \
                mock.patch("supervisor.trial.has_work", return_value=False):
            with self.assertRaises(Stop):
                autonomy.serve(poll_s=0)
        self.assertIs(seen["start"], autonomy._start)
        self.assertIsInstance(seen["state"], autonomy.WarmState)
        self.assertEqual(ended, [(seen["state"], 1)])

    def test_a_warm_up_that_cannot_start_never_stops_the_scheduler(self):
        # Codex on M2: an OSError from Popen, or from the log itself, escaped into serve's loop.
        st, children = autonomy.WarmState(), {}

        def broken_start(args):
            raise OSError("fork: resource temporarily unavailable")
        with mock.patch.object(autonomy, "log") as log:
            autonomy.gate_warm(children, st, broken_start, now=0)
        self.assertEqual(children, {})
        self.assertIn("could not start", log.call_args.args[0])
        with mock.patch.object(autonomy, "log", side_effect=OSError("log: read-only")), \
                mock.patch.object(autonomy.gate, "commits_id", side_effect=RuntimeError("no git")):
            autonomy.gate_warm(children, autonomy.WarmState(), self.start, now=0)
        with mock.patch.object(autonomy, "log", side_effect=OSError("log: read-only")):
            autonomy.gate_warm(children, autonomy.WarmState(), broken_start, now=0)

    def test_one_failure_one_alert(self):
        # Codex on M2: warm() logs the alert; the scheduler's own "ended exit=…" line raised a second one.
        from supervisor import panel
        lines = []
        st = autonomy.WarmState(pair="d" * 16)
        with mock.patch.object(autonomy, "log", side_effect=lines.append):
            autonomy.warm_ended(st, autonomy.gate.WARM_FAILED, 500)
            autonomy.warm_ended(st, 0, 480)
            autonomy.warm_ended(st, 1, 2)                 # a crash before warm() could say anything
        alert = [bool(panel.ALERT_LINE.search(l)) and "passed" not in l for l in lines]
        self.assertEqual(alert, [False, False, True], lines)

    def test_a_commit_that_cannot_be_read_starts_nothing_and_never_raises(self):
        st, children = autonomy.WarmState(), {}
        with mock.patch.object(autonomy.gate, "commits_id", side_effect=RuntimeError("git: not a repository")), \
                mock.patch.object(autonomy, "log") as log:
            self.tick(children, st, 0)
            self.tick(children, st, 1)
        self.assertEqual(self.started, [])
        self.assertEqual(log.call_count, 1)               # said once, not every tick
        self.assertIn("not a repository", log.call_args.args[0])

if __name__ == "__main__":
    unittest.main()
