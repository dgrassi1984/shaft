"""The scheduler's drain-restart (`shaft autonomy restart`, docs/deploy.md step 3). A SIGTERM restart stops the
scheduler's live runs on purpose, so deploys waited for a moment with no instance awake, and on 2026-11-03 none came in
90 min: the scheduler stayed on 11-01 code through four releases. A drain starts nothing new, lets what runs end, then
restarts the scheduler — it never cuts a run, and past its deadline it gives up instead."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from supervisor import autonomy, panel

# A stand-in for a scheduled run: it ends on its own after `secs`, writing "done"; a SIGTERM (what a restart used to
# send it) makes it write "cut" first.
CHILD = """
import signal, sys, time
out = sys.argv[1]
def cut(*_):
    open(out, "w").write("cut"); sys.exit(143)
signal.signal(signal.SIGTERM, cut)
time.sleep(float(sys.argv[2]))
open(out, "w").write("done")
"""


class Stop(Exception):
    pass


class DrainRestartTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-drain-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        self.lines: list[str] = []
        self.starts: list[list[str]] = []
        self.procs: dict[str, subprocess.Popen] = {}

    def start(self, secs: float):
        def _start(args):
            self.starts.append(args)
            key = "-".join(args)
            proc = subprocess.Popen([sys.executable, "-c", CHILD, str(self.sup / f"{key}.out"), str(secs)])
            self.procs.setdefault(key, proc)
            return proc
        return _start

    def out(self, key: str) -> str:
        path = self.sup / f"{key}.out"
        return path.read_text() if path.exists() else ""

    def serve_in_thread(self, secs: float, decide=None):
        """The real serve loop, every stage but the wakes stubbed; wakes start real child processes."""
        def wakes(now, st, names, interval_of, awake_of, facts_of=None, idle=None):
            return {n: "due" for n in names if not awake_of(n)}, {}
        stack = ExitStack()
        self.addCleanup(stack.close)
        for patch in [
                mock.patch.object(autonomy, "SUP_DIR", self.sup),
                mock.patch.object(autonomy, "log", side_effect=self.lines.append),
                mock.patch.object(autonomy, "record"),
                mock.patch.object(autonomy, "_start", self.start(secs)),
                mock.patch.object(autonomy, "decide_wakes", decide or wakes),
                mock.patch.object(autonomy, "stop_children_on_signal"),   # not the main thread
                mock.patch.object(autonomy, "launchd_pid", return_value=None),   # started by hand
                mock.patch("supervisor.version.record_service"),
                mock.patch("supervisor.browser.startup_note", return_value=""),
                mock.patch.object(autonomy, "instance_names", return_value=["a"]),
                mock.patch.object(autonomy, "paused", return_value=False),
                mock.patch.object(autonomy, "updating", return_value=False),
                mock.patch.object(autonomy, "update_jobs", return_value=[]),
                mock.patch.object(autonomy, "_awake", return_value=""),
                mock.patch.object(autonomy, "_state", return_value={}),
                mock.patch.object(autonomy, "sleep_due", return_value=False),
                mock.patch.object(autonomy, "_batteries"), mock.patch.object(autonomy, "_night_replays"),
                mock.patch.object(autonomy, "commitment_wakes"),
                mock.patch.object(autonomy, "gate_warm", lambda children, *a: setattr(self, "children", children)),
                mock.patch("supervisor.jobs.tick"),
                mock.patch("supervisor.watch.tick"),
                mock.patch("supervisor.watch.busy", return_value=False),
                mock.patch("supervisor.calendar_proposals.expire"),
                mock.patch("supervisor.trial.has_work", return_value=False)]:
            stack.enter_context(patch)
        result: dict = {}

        def run():
            try:
                result["code"] = autonomy.serve(poll_s=0.05)
            except Stop:
                result["stopped"] = True
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, result

    def wait_for(self, cond, timeout=10.0):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                self.fail("timed out")
            time.sleep(0.02)

    def test_a_drain_restart_cuts_no_live_run(self):
        # Regression (2026-11-03): the only restart was SIGTERM, which cut every live cycle, so the deploy waited for
        # an idle moment that never came. A drain lets the running wake end on its own and starts no other one.
        thread, result = self.serve_in_thread(secs=1.0)
        self.wait_for(lambda: self.starts)
        with mock.patch.object(autonomy, "SUP_DIR", self.sup):
            autonomy.request_restart(os.getpid(), within_s=60)
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "serve did not return: the scheduler never drained")
        self.assertEqual(result.get("code"), 0)
        wake = self.procs["wake-a"]
        self.assertEqual(wake.wait(timeout=5), 0)
        self.assertEqual(self.out("wake-a"), "done")             # it ran to its end ...
        self.assertNotEqual(self.out("wake-a"), "cut")           # ... and was never stopped
        self.assertEqual(self.starts, [["wake", "a"]])           # nothing started while draining
        self.assertFalse((self.sup / "scheduler-restart.json").exists())
        status = json.loads((self.sup / "scheduler-drain.json").read_text())
        self.assertEqual((status["state"], status["waiting"]), ("drained", []))
        self.assertTrue(any("draining for a restart" in l and "wake a" in l for l in self.lines), self.lines)
        self.assertTrue(any(l.startswith("scheduler: drained after") for l in self.lines), self.lines)

    def test_past_its_deadline_a_drain_gives_up_and_cuts_nothing(self):
        seen = {"after": False}

        def wakes(now, st, names, interval_of, awake_of, facts_of=None, idle=None):
            if any("FAILED to drain" in l for l in self.lines):
                seen["after"] = True
                raise Stop                                       # scheduling resumed: the loop is back here
            return {n: "due" for n in names if not awake_of(n)}, {}
        thread, result = self.serve_in_thread(secs=3.0, decide=wakes)
        self.wait_for(lambda: self.starts)
        with mock.patch.object(autonomy, "SUP_DIR", self.sup):
            autonomy.request_restart(os.getpid(), within_s=1)
        thread.join(timeout=15)
        self.assertTrue(result.get("stopped") and seen["after"], (result, self.lines))
        alert = next(l for l in self.lines if "FAILED to drain" in l)
        self.assertRegex(alert, panel.ALERT_LINE)                # the operator hears of it
        self.assertIn("wake a", alert)
        self.assertEqual(self.procs["wake-a"].wait(timeout=10), 0)
        self.assertEqual(self.out("wake-a"), "done")             # not signalled, not killed
        self.assertFalse((self.sup / "scheduler-restart.json").exists())
        self.assertEqual(json.loads((self.sup / "scheduler-drain.json").read_text())["state"], "abandoned")

    def test_a_request_for_another_scheduler_process_is_stale_and_dropped(self):
        # Left for the scheduler this one replaced: acting on it would drain the new code again, in a loop.
        with mock.patch.object(autonomy, "SUP_DIR", self.sup), \
                mock.patch.object(autonomy, "log", side_effect=self.lines.append):
            autonomy.request_restart(os.getpid() + 100000)
            self.assertEqual(autonomy.drain_tick({}, None), (None, ""))
            self.assertFalse((self.sup / "scheduler-restart.json").exists())
            self.assertIn("dropped", self.lines[-1])

    def test_a_cancelled_drain_resumes(self):
        with mock.patch.object(autonomy, "SUP_DIR", self.sup), \
                mock.patch.object(autonomy, "log", side_effect=self.lines.append), \
                mock.patch("supervisor.watch.busy", return_value=False):
            autonomy.request_restart(os.getpid())
            running = FakeProc()
            drain, what = autonomy.drain_tick({"wake a": (running, 0.0)}, None)
            self.assertEqual((what, drain["waiting"]), ("draining", ["wake a"]))
            self.assertTrue(autonomy.cancel_restart())
            self.assertEqual(autonomy.drain_tick({"wake a": (running, 0.0)}, drain), (None, ""))
            self.assertEqual(autonomy.drain_status()["state"], "cancelled")
            self.assertEqual(running.signals, [])

    def test_no_child_is_signalled_a_battery_included(self):
        # Codex on the drain: a battery's SIGTERM interrupts its running exam. It ends between arms on its own
        # (battery.run asks parent_draining), so the drain only waits for it.
        battery, wake = FakeProc(), FakeProc()
        children = {"battery a": (battery, 0.0), "wake b": (wake, 0.0)}
        with mock.patch.object(autonomy, "SUP_DIR", self.sup), \
                mock.patch.object(autonomy, "log", side_effect=self.lines.append), \
                mock.patch("supervisor.watch.busy", return_value=False):
            autonomy.request_restart(os.getpid())
            drain, what = autonomy.drain_tick(children, None)
            drain, what = autonomy.drain_tick(children, drain)
        self.assertEqual(what, "draining")
        self.assertEqual((battery.signals, wake.signals), ([], []))
        self.assertEqual(drain["waiting"], ["battery a", "wake b"])

    def test_drain_step_waits_for_the_watch_pass_too(self):
        now = time.time()
        self.assertEqual(autonomy.drain_step({}, now, now + 60, watch_busy=True),
                         ("wait", ["the mail and calendar watch"]))
        self.assertEqual(autonomy.drain_step({}, now, now + 60, watch_busy=False), ("done", []))
        self.assertEqual(autonomy.drain_step({"x": (FakeProc(ended=True), 0)}, now, now + 60, False)[0], "done")
        self.assertEqual(autonomy.drain_step({"x": (FakeProc(), 0)}, now, now - 1, False), ("abandon", ["x"]))

    def test_a_warm_up_that_ends_green_while_draining_starts_no_regate(self):
        # Codex on the drain: the reap started `gate regate` (new work) before the drain was looked at. The held
        # changes keep waiting (gate_held) for the next scheduler's own first green warm-up.
        class Green:
            returncode = 0

            def poll(self):
                return 0
        regates, calls = [], []

        def wakes(now, st, names, interval_of, awake_of, facts_of=None, idle=None):
            calls.append(1)
            if len(calls) == 2:   # one tick in (the loop's children known): the request and a green warm-up together
                autonomy.request_restart(os.getpid(), within_s=60)
                self.children["gate warm"] = (Green(), time.monotonic())
            return {}, {}
        with mock.patch.object(autonomy, "regate_after_warm", side_effect=lambda c, s: regates.append(1)), \
                mock.patch.object(autonomy, "warm_ended") as ended:
            thread, result = self.serve_in_thread(secs=0.5, decide=wakes)
            thread.join(timeout=15)
        ended.assert_called_once()                                 # it was reaped, on a draining tick
        self.assertEqual(result.get("code"), 0)
        self.assertEqual(regates, [])

    def test_the_trial_worker_stops_submitting_only_for_its_draining_scheduler(self):
        with mock.patch.object(autonomy, "SUP_DIR", self.sup):
            self.assertFalse(autonomy.parent_draining())
            autonomy.request_restart(os.getppid())
            self.assertTrue(autonomy.parent_draining())
            autonomy.request_restart(os.getpid() + 100000)        # some other scheduler: a worker started by hand
            self.assertFalse(autonomy.parent_draining())

    def test_a_draining_scheduler_s_trial_worker_starts_no_pair(self):
        from supervisor import trial
        with mock.patch.object(trial, "SUP_DIR", self.sup), mock.patch.object(trial, "log"), \
                mock.patch.object(trial, "sweep_scratch", return_value=0), \
                mock.patch.object(trial, "all_trials", return_value=[]), \
                mock.patch.object(trial, "runnable", return_value=[({"instance": "a", "id": "t1"}, 0, "cloud")]), \
                mock.patch.object(trial, "_pair_job") as pair, \
                mock.patch.object(autonomy, "parent_draining", return_value=True):
            self.assertEqual(trial.work(), 0)
        pair.assert_not_called()


class RelaunchTest(unittest.TestCase):
    def test_under_launchd_it_kickstarts_its_own_label_from_its_own_session(self):
        # An explicit start: launchd's on-demand-only mode (2026-10-13) may not honour KeepAlive's own restart.
        with mock.patch.object(autonomy, "launchd_pid", return_value=os.getpid()), \
                mock.patch.object(autonomy.subprocess, "Popen") as popen, \
                mock.patch.object(autonomy.time, "sleep"), mock.patch.object(autonomy, "log") as log:
            self.assertEqual(autonomy.relaunch(), 0)
        args, kw = popen.call_args
        self.assertEqual(args[0], ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.shaft.scheduler"])
        self.assertTrue(kw["start_new_session"])                 # outside the process group launchd ends with it
        self.assertIn("KeepAlive", log.call_args.args[0])        # said when launchd's SIGTERM did not come

    def test_started_by_hand_it_only_exits(self):
        with mock.patch.object(autonomy, "launchd_pid", return_value=os.getpid() + 1), \
                mock.patch.object(autonomy.subprocess, "Popen") as popen, mock.patch.object(autonomy, "log"):
            self.assertEqual(autonomy.relaunch(), 0)
        popen.assert_not_called()

    def test_the_pid_is_read_from_launchctl_print(self):
        out = subprocess.CompletedProcess([], 0, "com.shaft.scheduler = {\n\tstate = running\n\tpid = 4242\n}\n", "")
        with mock.patch.object(autonomy, "_launchctl", return_value=out):
            self.assertEqual(autonomy.launchd_pid("scheduler"), 4242)
        with mock.patch.object(autonomy, "_launchctl", return_value=subprocess.CompletedProcess([], 113, "", "")):
            self.assertIsNone(autonomy.launchd_pid("scheduler"))


class RestartVerbTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-drain-verb-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(autonomy, "SUP_DIR", self.sup))
        stack.enter_context(mock.patch.object(autonomy, "log"))
        stack.enter_context(mock.patch.object(autonomy, "running", return_value=True))

    def run_verb(self, records, **kw):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("supervisor.version.service", side_effect=records), redirect_stdout(out), \
                redirect_stderr(err):
            code = autonomy.restart(**kw)
        return code, out.getvalue(), err.getvalue()

    def test_a_scheduler_older_than_the_drain_is_refused_with_the_way_that_cuts_nothing(self):
        old = {"pid": 111, "commit": "a1b2c3d", "started": "2026-11-01T13:10:00", "settings": {}}
        code, _, err = self.run_verb([old])
        self.assertEqual(code, 2)
        self.assertIn("predates the drain-restart", err)
        self.assertIn("shaft pause", err)
        self.assertIn("2026-11-01T13:10:00", err)
        self.assertFalse((self.sup / "scheduler-restart.json").exists())   # never asked of a scheduler that ignores it

    def test_it_asks_the_running_scheduler_by_pid(self):
        rec = {"pid": 111, "commit": "c0ffee1", "started": "x", "settings": {"drain": 1}}
        code, out, _ = self.run_verb([rec], within_min=20)
        self.assertEqual(code, 0)
        req = json.loads((self.sup / "scheduler-restart.json").read_text())
        self.assertEqual(req["pid"], 111)
        self.assertAlmostEqual(req["deadline"] - req["at"], 20 * 60, delta=1)
        self.assertIn("restart once its runs end", out)

    def test_the_default_deadline_follows_the_sleep_and_cycle_budgets(self):
        rec = {"pid": 111, "commit": "c", "started": "x", "settings": {"drain": 1}}
        self.run_verb([rec])
        req = json.loads((self.sup / "scheduler-restart.json").read_text())
        self.assertAlmostEqual(req["deadline"] - req["at"], autonomy.SLEEP_TIMEOUT_S + autonomy.CYCLE_TIMEOUT_S,
                               delta=1)

    def test_cancel_withdraws_it(self):
        autonomy.request_restart(111)
        code, out, _ = self.run_verb([], cancel=True)
        self.assertEqual(code, 0)
        self.assertIn("withdrawn", out)
        self.assertFalse((self.sup / "scheduler-restart.json").exists())

    def test_wait_returns_once_a_new_scheduler_has_recorded_itself(self):
        old = {"pid": 111, "commit": "c", "started": "x", "settings": {"drain": 1}}
        new = {"pid": 222, "commit": "d", "describe": "v0.34.0", "started": "y", "settings": {"drain": 1}}

        def records(*_a, **_k):
            calls.append(1)
            if len(calls) == 3:                                 # the scheduler drained and is restarting
                req = json.loads((self.sup / "scheduler-restart.json").read_text())
                autonomy._write_json(self.sup / "scheduler-drain.json", {**req, "state": "drained", "waiting": []})
                autonomy.cancel_restart()                       # in drain_tick's order: outcome first
                return None
            return old if len(calls) < 3 else new
        calls: list = []
        with mock.patch.object(autonomy.time, "sleep"):
            code, out, _ = self.run_verb(records, wait=True)
        self.assertEqual(code, 0)
        self.assertIn("restarted: scheduler 222 on v0.34.0", out)

    def test_a_drain_writes_its_outcome_before_it_removes_the_request(self):
        # Found by the --wait test: the request went first, and a waiting verb read "dropped" in between.
        order = []
        autonomy.request_restart(os.getpid())
        real_write, real_cancel = autonomy._write_json, autonomy._drop_request
        with mock.patch.object(autonomy, "_write_json", side_effect=lambda p, d: (order.append(d["state"]),
                                                                                    real_write(p, d))), \
                mock.patch.object(autonomy, "_drop_request", side_effect=lambda: (order.append("gone"),
                                                                                  real_cancel())[1]), \
                mock.patch("supervisor.watch.busy", return_value=False):
            self.assertEqual(autonomy.drain_tick({}, None)[1], "drained")
        self.assertEqual(order, ["drained", "gone"])

    def test_a_request_written_while_the_scheduler_decides_is_never_lost(self):
        # Codex on the drain: the scheduler read a stale request, the operator wrote a valid one, and the stale drop
        # unlinked the valid one. Both go through one lock now: the write waits until the decision is made.
        autonomy.request_restart(999999)                         # stale: for the scheduler this one replaced
        entered, release, wrote = threading.Event(), threading.Event(), {}
        real_read = autonomy.restart_request

        def slow_read(pid=None):
            got = real_read(pid)
            entered.set()
            release.wait(5)
            return got

        def operator():
            wrote["req"] = autonomy.request_restart(os.getpid())
        with mock.patch.object(autonomy, "restart_request", slow_read), \
                mock.patch("supervisor.watch.busy", return_value=False):
            t = threading.Thread(target=autonomy.drain_tick, args=({}, None))
            t.start()
            entered.wait(5)
            w = threading.Thread(target=operator)
            w.start()
            time.sleep(0.2)
            self.assertNotIn("req", wrote)                       # held back while the scheduler decides
            release.set()
            t.join(5)
            w.join(5)
        self.assertEqual(json.loads((self.sup / "scheduler-restart.json").read_text())["at"], wrote["req"]["at"])

    def test_a_cancel_after_the_drain_committed_says_it_is_too_late(self):
        # Codex on the drain: a cancel landing after the drain was done said "scheduling resumes" while it restarted.
        with mock.patch("supervisor.watch.busy", return_value=False):
            autonomy.request_restart(os.getpid())
            self.assertEqual(autonomy.drain_tick({}, None)[1], "drained")
        code, _, err = self.run_verb([], cancel=True)
        self.assertEqual(code, 1)
        self.assertIn("too late: the scheduler drained at", err)

    def test_wait_reports_an_abandoned_drain_as_a_failure(self):
        old = {"pid": 111, "commit": "c", "started": "x", "settings": {"drain": 1}}

        def records(*_a, **_k):
            if (self.sup / "scheduler-restart.json").exists():
                req = json.loads((self.sup / "scheduler-restart.json").read_text())
                autonomy._write_json(self.sup / "scheduler-drain.json",
                                     {**req, "state": "abandoned", "waiting": ["wake a"]})
                autonomy.cancel_restart()
            return old
        with mock.patch.object(autonomy.time, "sleep"):
            code, _, err = self.run_verb(records, wait=True)
        self.assertEqual(code, 1)
        self.assertIn("abandoned — still running wake a", err)

    def test_status_says_what_a_drain_waits_for(self):
        self.assertEqual(autonomy.drain_line(), "")
        req = autonomy.request_restart(111)
        self.assertIn("taken up at its next tick", autonomy.drain_line())
        autonomy._write_json(self.sup / "scheduler-drain.json",
                             {**req, "pid": os.getpid(), "state": "draining", "waiting": ["wake tundra"]})
        self.assertRegex(autonomy.drain_line(), r"scheduler restarting .*waiting for wake tundra; by \d\d:\d\d")


class FakeProc:
    def __init__(self, ended: bool = False):
        self.signals: list[int] = []
        self.ended = ended

    def poll(self):
        return 0 if self.ended else None

    def send_signal(self, s):
        self.signals.append(s)


if __name__ == "__main__":
    unittest.main()
