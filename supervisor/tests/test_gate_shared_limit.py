"""#243: the shared supervisor tests' time limit comes from their recent passing runs, and a change whose only failure
is that limit running out stays on the instance's code instead of being taken off.

Measured 2026-11-13..20: 71 of 164 hourly warm-ups timed out at the fixed 300 s, green runs had crept from about 120 s
to 250-299 s at the same test count, and five instance changes were reverted with the timeout as their only cause (one
waited 7 h 21 for its re-gate). An instance's own test of its live data also rolled back six template merges."""
from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
import unittest

from supervisor import core, gate, gate_held
from supervisor import __main__ as cli
from supervisor.tests import test_gate as tg
from supervisor.tests import test_run_transaction as trx
from supervisor.tests.test_gate_held import EDITS, EDITS_OTHER, NEW, OTHER, OWN_FAIL, HeldCase

OWN_TEST = ("import unittest\n\nclass T(unittest.TestCase):\n"
            "    def test_own(self):\n        self.fail('mine')\n")


class SharedLimitTest(unittest.TestCase):
    """shared_limit learns from PARTS_LOG: the passing shared runs no other run of a part overlapped."""

    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-limit-"))
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.sup))
        self.now = datetime.now()

    def run_(self, start_min: float, seconds: float, part="shared", ok=True, tests=4900):
        start = self.now - timedelta(minutes=start_min)
        with (self.sup / gate.PARTS_LOG).open("a") as f:
            f.write(json.dumps({"part": part, "label": "abc", "start": start.isoformat(timespec="milliseconds"),
                                "end": (start + timedelta(seconds=seconds)).isoformat(timespec="milliseconds"),
                                "ok": ok, "tests_run": tests, "limit_s": 300, "pid": 1}) + "\n")

    def test_too_few_runs_keep_the_stage_limit_and_say_so(self):
        for i in range(gate.LIMIT_RUNS - 1):
            self.run_(60 * (i + 1), 250)
        limit, how, runs = gate.shared_limit(300)
        self.assertEqual(limit, 300)
        self.assertEqual(len(runs), gate.LIMIT_RUNS - 1)
        self.assertIn(f"{gate.LIMIT_RUNS - 1} of the {gate.LIMIT_RUNS} passing runs measured alone", how)

    def test_bug243_limit_is_twice_the_95th_percentile_of_alone_passing_runs(self):
        for i, s in enumerate((200, 210, 220, 230, 240, 250, 260, 270, 280, 290)):
            self.run_(60 * (i + 1), s)
        limit, how, runs = gate.shared_limit(300)
        self.assertEqual(limit, 2 * 290)          # the 95th percentile of ten is the largest
        self.assertGreater(limit, 300, "the fixed 300 s that timed out 71 of 164 warm-ups")
        self.assertEqual(len(runs), 10)
        self.assertIn("passing runs measured alone", how)

    def test_overlapped_failed_timed_out_old_and_empty_runs_are_not_learned_from(self):
        for i in range(5):
            self.run_(60 * (i + 1), 100)
        self.run_(10, 900)                         # overlapped by an instance part of another gate
        self.run_(8, 30, part="instance")
        self.run_(500, 900, ok=False)              # failed
        self.run_(520, 900, ok=None)               # timed out
        self.run_(60 * 24 * (gate.LIMIT_DAYS + 1), 900)   # too old
        self.run_(540, 900, tests=0)               # ran no test
        limit, _, runs = gate.shared_limit(150)
        self.assertEqual(limit, 200)
        self.assertEqual({r["seconds"] for r in runs}, {100})

    def test_never_under_the_stage_limit(self):
        for i in range(6):
            self.run_(60 * (i + 1), 20)
        self.assertEqual(gate.shared_limit(300)[0], 300)


class WaitsTest(tg.SharedFixtureCase):
    """The real gate: a shared part out of time no longer stops the checks of the change."""

    def gate_hanging(self) -> gate.Output:
        self.hang_flag.write_text("x")
        sup = Path(tempfile.mkdtemp(prefix="shaft-waits-"))
        with mock.patch.object(core, "SUP_DIR", sup), mock.patch("supervisor.core.log"):
            ok, out = self.gate(timeout_s=3)
        self.assertFalse(ok, out)
        rows = [json.loads(line) for line in (sup / gate.PARTS_LOG).read_text().splitlines()]
        self.assertEqual([(r["part"], r["ok"]) for r in rows][:1], [("shared", None)])   # the timeout is recorded
        return out

    def test_bug243_shared_timeout_alone_waits_and_the_change_s_checks_ran(self):
        out = self.gate_hanging()
        self.assertTrue(out.waits, out)
        self.assertEqual(out.cause, "baseline")
        self.assertIn("ran out of time", out.why)
        self.assertIn("-- instance part: 1 tests --", out)          # the check that reads the change still ran
        self.assertIn("its limit: 3 s, the stage limit", out)        # and the gate says how its limit was set

    def test_a_failing_check_of_the_change_beside_it_is_the_change_s(self):
        (self.inst / "tests").mkdir()
        (self.inst / "tests" / "test_own.py").write_text(OWN_TEST)
        out = self.gate_hanging()
        self.assertFalse(out.waits)
        self.assertEqual(out.cause, "candidate")

    def test_own_tests_alone_name_what_fails(self):
        with mock.patch("supervisor.core.log"):
            self.assertIsNone(gate.own_tests_failures(self.inst, Path.home(), env_root=self.inst))   # no tests
            (self.inst / "tests").mkdir()
            (self.inst / "tests" / "test_own.py").write_text(OWN_TEST)
            names = gate.own_tests_failures(self.inst, Path.home(), env_root=self.inst)
        self.assertTrue(names and "test_own" in names[0], names)


class HoldOnCodeCase(HeldCase):
    def fake_gate(self, root, home, env_root=None, data_root=None):
        if self.verdict != "waits":
            return super().fake_gate(root, home, env_root, data_root)
        self.gated.append((root / "plugins" / "text_stats" / "main.py").read_text())
        out = gate.Output("== supervisor invariants (abc) ==\n-- shared part: timed out --\n-- instance part: 1 tests --\n",
                          "baseline", "the shared supervisor tests ran out of time (300 s)", [])
        out.waits = True
        return False, out

    def hold_on_code(self) -> dict:
        self.verdict = "waits"
        self.assertEqual(self.cycle(EDITS), 0)
        held, = self.held()
        return held


class HoldOnCodeTest(HoldOnCodeCase):
    def test_bug243_timeout_only_change_stays_on_the_code_uncommitted(self):
        held = self.hold_on_code()
        self.assertEqual(self.plugin(), NEW, "reverted although only the shared tests' clock failed (#243)")
        self.assertNotEqual(self.code_status(), "", "held on the code is not committed")
        self.assertTrue(held["on_code"])
        self.assertEqual(self.rows()[-1]["outcome"], "held")
        told, = self.told()
        self.assertIn("held on my code", told)
        self.assertIn("I can use it now", told)
        self.assertIn("still on my code", self.record())

    def test_the_next_run_starts_from_it_without_gating_it(self):
        self.hold_on_code()
        gated = len(self.gated)
        self.verdict = "pass"
        self.assertEqual(self.cycle(), 0)                    # not refused: its code is not "unapproved"
        self.assertEqual(self.plugin(), NEW)
        self.assertEqual(len(self.gated), gated)             # nothing of it gated by the next run
        self.assertEqual(len(self.held()), 1)

    def test_re_gate_on_green_keeps_and_commits_it(self):
        held = self.hold_on_code()
        self.verdict = "pass"
        self.regate()
        self.assertEqual(self.plugin(), NEW)
        self.assertEqual(self.code_status(), "", "kept but not committed")
        self.assertEqual(self.held(), [])
        self.assertIn("re-gated: kept", self.told()[-1])
        self.assertEqual(self.rows()[-1]["result"], "kept")
        self.assertEqual(self.gated[-1], NEW)
        self.assertIn(held["id"], self.logs())

    def test_re_gate_that_fails_on_the_change_puts_its_base_back(self):
        self.hold_on_code()
        self.verdict = "candidate"
        self.regate()
        self.assertEqual(self.plugin(), trx.COMMITTED)
        self.assertEqual(self.held(), [])
        self.assertIn("re-gated: failed", self.told()[-1])
        self.assertIn(OWN_FAIL, self.told()[-1])

    def test_out_of_time_again_it_stays_on_the_code_and_counts_a_try(self):
        self.hold_on_code()
        self.regate()                                        # verdict still "waits"
        held, = self.held()
        self.assertEqual(held["tries"], 1)
        self.assertTrue(held["on_code"])
        self.assertEqual(self.plugin(), NEW)
        self.assertIn("stays on my code", self.told()[-1])

    def test_a_red_baseline_at_its_re_gate_takes_it_off_and_holds_it_as_before(self):
        self.hold_on_code()
        self.verdict = "baseline"
        self.regate()
        held, = self.held()
        self.assertFalse(held["on_code"])
        self.assertEqual(self.plugin(), trx.COMMITTED)
        self.verdict = "pass"
        self.regate()                                        # held off the code: put back and gated as before
        self.assertEqual(self.plugin(), NEW)
        self.assertEqual(self.held(), [])

    def test_withdrawing_it_says_it_is_still_on_the_code(self):
        held = self.hold_on_code()
        with mock.patch.object(core, "SUP_DIR", self.sup):
            answer = gate_held.withdrawal("kiln", held["id"], "not needed", gate_held.pending("kiln"))[1]
        self.assertIn("still on my code", answer)

    def test_superseded_by_a_later_kept_change_it_is_dropped(self):
        self.hold_on_code()
        self.verdict = "pass"
        self.assertEqual(self.cycle(EDITS_OTHER), 0)         # the same file written again, and kept
        self.assertEqual(self.plugin(), OTHER)
        self.regate()
        self.assertEqual(self.plugin(), OTHER)
        self.assertEqual(self.held(), [])
        self.assertIn("superseded", self.told()[-1])


class TemplateMergeTest(unittest.TestCase):
    """shaft update: the instance's own tests failing the same way before the merge never roll it back."""

    def output(self, own_only=True, failing=("FAIL: test_live_file_has_no_undated_row (t.T)",)):
        out = gate.Output("== instance tests ==\nFAIL", "candidate", "my own tests fail", list(failing))
        out.own_only = own_only
        return out

    def check(self, before, **kw):
        with mock.patch("supervisor.protected.copy") as copy, \
                mock.patch.object(gate, "own_tests_failures", return_value=before) as own, \
                mock.patch.object(cli, "log"):
            copy.return_value.__enter__.return_value = Path("/tmp/copy")
            got = cli._fails_without_merge("kiln", Path("/tmp/kiln"), {}, self.output(**kw))
        return got, own

    def test_bug243_a_live_data_test_failing_before_the_merge_too_keeps_the_merge(self):
        got, own = self.check(["FAIL: test_live_file_has_no_undated_row (t.T)"])
        self.assertEqual(got, ["FAIL: test_live_file_has_no_undated_row (t.T)"])
        own.assert_called_once()

    def test_a_test_the_merge_broke_still_rolls_it_back(self):
        self.assertEqual(self.check([])[0], [])
        self.assertEqual(self.check(None)[0], [])            # could not tell: the gate's verdict stands

    def test_only_when_the_own_tests_are_the_only_failure(self):
        got, own = self.check(["FAIL: test_live_file_has_no_undated_row (t.T)"], own_only=False)
        self.assertEqual(got, [])
        own.assert_not_called()


class BudgetReportTest(unittest.TestCase):
    def test_budget_reports_the_shared_limit_and_its_runs(self):
        runs = [{"label": "abc", "start": "2026-11-20T03:00:00.000", "seconds": 140.0}] * 5
        part = (True, "", "", 4900, 120.0)
        with mock.patch.object(gate, "committed_tree", return_value=(Path("/tmp/t"), "abc")), \
                mock.patch.object(gate, "shared_limit", return_value=(400, "400 s: 2 × the 95% of 5 passing runs", runs)), \
                mock.patch.object(gate, "_part", return_value=part) as ran, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(gate.budget(300), 0)
        text = out.getvalue()
        self.assertIn("shared part's limit: 400 s: 2 × the 95% of 5 passing runs", text)
        self.assertIn("from: 11-20 03:00 140s", text)
        self.assertEqual([c.args[0] for c in ran.call_args_list], ["instance", "shared"])
        self.assertEqual(ran.call_args_list[1].args[3], 400)   # the shared part runs within its own limit


if __name__ == "__main__":
    unittest.main()
