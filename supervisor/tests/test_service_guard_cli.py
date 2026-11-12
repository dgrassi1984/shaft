"""The service guard from a terminal. Live, 2026-10-27: `shaft autonomy on` typed while the scheduler ran restarted it
and killed shaft's sleep and wisp's live cycle — the guard lived only in the two panels, and even there `autonomy on`
was never held back although on a running scheduler it is a restart."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

from supervisor import __main__ as cli
from supervisor import actions, api, autonomy, panel

RUNS = ["shaft (sleep since 11:31)", "wisp (live since 12:08)"]


class CliGuardTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.logged = [], []
        for p in (mock.patch.object(autonomy, "autonomy", lambda action: self.calls.append(("autonomy", action)) or 0),
                  mock.patch.object(autonomy, "service", lambda name, action: self.calls.append((name, action)) or 0),
                  mock.patch.object(api, "command", lambda action, local=False: self.calls.append(("api", action)) or 0),
                  mock.patch.object(actions, "log", self.logged.append)):
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *argv, up=True, runs=RUNS, busy=RUNS):
        err = io.StringIO()
        with mock.patch.object(autonomy, "running", return_value=up), \
                mock.patch.object(panel, "scheduled_runs", return_value=runs), \
                mock.patch.object(actions, "busy_instances", return_value=busy), redirect_stderr(err):
            return cli.main(list(argv)), err.getvalue()

    def test_autonomy_on_over_running_runs_is_refused_and_restarts_nothing(self):
        code, err = self.run_cli("autonomy", "on")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])                       # the bug: it restarted and killed both runs
        self.assertIn("would kill them: shaft (sleep since 11:31), wisp (live since 12:08)", err)
        self.assertIn("--force", err)
        self.assertEqual(self.logged, [])

    def test_every_guarded_verb_is_held_back(self):
        for argv in (("autonomy", "off"), ("api", "on"), ("api", "off"), ("model", "on"), ("embed", "off")):
            self.assertEqual(self.run_cli(*argv)[0], 1, argv)
        self.assertEqual(self.calls, [])

    def test_forced_it_runs_and_is_logged(self):
        code, _ = self.run_cli("autonomy", "on", "--force")
        self.assertEqual((code, self.calls), (0, [("autonomy", "on")]))
        self.assertEqual(len(self.logged), 1)
        self.assertIn("cli: operator forced shaft autonomy on past the guard: the scheduler's runs", self.logged[0])

    def test_a_stopped_service_or_no_runs_goes_ahead_unasked(self):
        self.assertEqual(self.run_cli("autonomy", "on", up=False)[0], 0)
        self.assertEqual(self.run_cli("model", "on", up=False)[0], 0)
        self.assertEqual(self.run_cli("autonomy", "on", runs=[])[0], 0)
        self.assertEqual(self.run_cli("api", "on", busy=[])[0], 0)
        self.assertEqual(self.calls, [("autonomy", "on"), ("model", "on"), ("autonomy", "on"), ("api", "on")])
        self.assertEqual(self.logged, [])

    def test_the_refusal_points_at_the_restart_that_cuts_nothing(self):
        # 2026-11-03: the deploy waited 90 min for an idle moment; the refusal now names the drain-restart.
        self.assertIn("Or `shaft autonomy restart`: it restarts once they end, cutting none.",
                      self.run_cli("autonomy", "on")[1])
        self.assertNotIn("autonomy restart", self.run_cli("autonomy", "off")[1])   # off is no restart

    def test_a_drain_restart_is_never_guarded(self):
        with mock.patch.object(autonomy, "restart", lambda *a: self.calls.append(("restart",) + a) or 0):
            self.assertEqual(self.run_cli("autonomy", "restart", "--within", "30", "--wait")[0], 0)
            self.assertEqual(self.run_cli("autonomy", "restart", "--cancel")[0], 0)
        self.assertEqual(self.calls, [("restart", 30, True, False), ("restart", None, False, True)])

    def test_status_is_never_guarded(self):
        self.assertEqual(self.run_cli("autonomy", "status")[0], 0)
        self.assertEqual(self.calls, [("autonomy", "status")])

    def test_the_panels_guard_autonomy_on_too(self):
        self.assertIn("would kill them", actions.guard_reason("autonomy", "on", RUNS, []))
        self.assertEqual(actions.guard_reason("autonomy", "on", [], []), "")


if __name__ == "__main__":
    unittest.main()
