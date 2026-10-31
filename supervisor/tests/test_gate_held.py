"""The gate tells a red baseline from a failing change and holds the change instead of losing it
(docs/plans/PLAN-thelos-review-2.md M2, S2).

Regression for Thelos's record (measured 2026-10-31): 24 of its 69 gate reverts were the supervisor's — shared
supervisor tests failing, the invariant tests timing out, a protected-copy regression, the gate erroring — and each
lost the change; nothing told it when the gate was green again (~14 h once), and its gate logs were out of its reach.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from supervisor import autonomy, broker, core, gate, gate_held, runner
from supervisor.tests import test_gate as tg
from supervisor.tests import test_run_transaction as trx

NEW = "def run(**args):\n    return 'stats, better'\n"
OTHER = "def run(**args):\n    return 'stats, another way'\n"
EDITS = f"import pathlib\npathlib.Path('plugins/text_stats/main.py').write_text({NEW!r})\n"
EDITS_OTHER = f"import pathlib\npathlib.Path('plugins/text_stats/main.py').write_text({OTHER!r})\n"
SHARED_FAIL = "FAIL: test_shared_broken (supervisor.tests.test_x.T.test_shared_broken)"
OWN_FAIL = "FAIL: test_text_stats (tests.test_text_stats.T.test_text_stats)"


class ClassifyTest(tg.SharedFixtureCase):
    """run_tests says whose failure a failed gate is (gate.Output), from the real gate on a committed stand-in project."""

    def cause(self, timeout_s: int = 120) -> gate.Output:
        with mock.patch("supervisor.core.log"):
            ok, out = self.gate(timeout_s)
        self.assertIsInstance(out, gate.Output)
        self.assertEqual(ok, out.cause is None, out)
        return out

    def test_a_passing_gate_has_no_cause(self):
        self.assertIsNone(self.cause().cause)

    def test_failing_shared_supervisor_tests_are_the_baseline_s(self):
        self.fail_flag.write_text("x")
        out = self.cause()
        self.assertEqual(out.cause, "baseline", out)
        self.assertIn("the shared supervisor tests fail", out.why)
        self.assertTrue(any("test_counted" in n for n in out.failing), out.failing)
        self.assertEqual(out.recheck, ())

    def test_shared_supervisor_tests_out_of_time_are_the_baseline_s(self):
        self.hang_flag.write_text("x")
        out = self.cause(timeout_s=3)
        self.assertEqual(out.cause, "baseline", out)
        self.assertIn("ran out of time", out.why)

    def test_the_gate_s_own_error_is_the_baseline_s(self):
        with mock.patch.object(gate, "_stage", side_effect=FileNotFoundError("/usr/bin/sandbox-exec")):
            out = self.cause()
        self.assertEqual(out.cause, "baseline", out)
        self.assertIn("the gate itself failed", out.why)

    def test_no_committed_copy_is_the_baseline_s(self):
        shutil.rmtree(self.project / ".git")
        out = self.cause()
        self.assertEqual(out.cause, "baseline", out)

    def test_a_supervisor_test_that_reads_the_change_failing_is_the_change_s_to_recheck(self):
        (self.inst / "BAD").write_text("x")
        out = self.cause()
        self.assertEqual(out.cause, "candidate", out)
        self.assertTrue(out.recheck and all("test_instance_is_good" in n for n in out.recheck), out.recheck)

    def test_the_instance_s_own_failing_test_is_the_change_s_never_rechecked(self):
        (self.inst / "tests").mkdir()
        (self.inst / "tests" / "test_own.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n"
                                                         "    def test_own(self):\n        self.fail('mine')\n")
        out = self.cause()
        self.assertEqual(out.cause, "candidate", out)
        self.assertIn("my own tests fail", out.why)
        self.assertTrue(any("test_own" in n for n in out.failing), out.failing)
        self.assertEqual(out.recheck, ())

    def test_a_change_s_failure_wins_over_a_red_baseline(self):
        self.fail_flag.write_text("x")
        (self.inst / "tests").mkdir()
        (self.inst / "tests" / "test_own.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n"
                                                         "    def test_own(self):\n        self.fail('mine')\n")
        self.assertEqual(self.cause().cause, "candidate")

    def test_a_link_where_tmp_should_be_is_the_change_s(self):
        (self.inst / ".tmp").symlink_to(self.tmp, target_is_directory=True)
        out = self.cause()
        self.assertEqual(out.cause, "candidate", out)

    def test_the_instance_part_alone_names_what_fails_on_a_tree(self):
        # What runner._cause runs against the approved code: [] when it passes, the failing names when not.
        with mock.patch("supervisor.core.log"):
            self.assertEqual(gate.instance_part_failures(self.inst), [])
            (self.inst / "BAD").write_text("x")
            names = gate.instance_part_failures(self.inst)
        self.assertTrue(names and "test_instance_is_good" in names[0], names)


class HeldCase(trx.RunTransactionCase):
    """A real instance worktree; the gate a stand-in whose verdict each test sets."""

    def setUp(self):
        super().setUp()
        self.sup = self.project / ".supervisor"
        self.verdict = "pass"
        self.recheck: list[str] | None = None

    def fake_gate(self, root: Path, home: Path, env_root: Path | None = None, data_root: Path | None = None):
        self.gated.append((root / "plugins" / "text_stats" / "main.py").read_text())
        if self.verdict == "pass":
            return True, gate.Output("== supervisor invariants (abc) ==\n-- instance part: 1 tests --\n")
        if self.verdict == "baseline":
            return False, gate.Output(f"== supervisor invariants (abc) ==\n{SHARED_FAIL}\nTraceback …\nfull log: x",
                                      "baseline", f"the shared supervisor tests fail: {SHARED_FAIL}", [SHARED_FAIL])
        return False, gate.Output(f"== instance tests ==\n{OWN_FAIL}\nAssertionError", "candidate",
                                  "my own tests fail", [OWN_FAIL])

    @contextmanager
    def patched(self):
        with ExitStack() as s:
            for target, attr, value in ((runner, "SUP_DIR", self.sup), (core, "SUP_DIR", self.sup),
                                        (runner, "PROJECT", self.project), (core, "PROJECT", self.project),
                                        (core, "INSTANCES", self.project / "instances")):
                s.enter_context(mock.patch.object(target, attr, value))
            s.enter_context(mock.patch.object(runner, "instance_root", return_value=self.root))
            s.enter_context(mock.patch.object(gate, "run_tests", side_effect=self.fake_gate))
            self.logged = s.enter_context(mock.patch.object(runner, "log"))
            s.enter_context(mock.patch("supervisor.metrics.start_probation"))
            yield

    def regate(self) -> None:
        with self.patched():
            runner.regate_held("kant")

    def held(self) -> list[dict]:
        with mock.patch.object(core, "SUP_DIR", self.sup):
            return gate_held.pending("kant")

    def rows(self) -> list[dict]:
        with mock.patch.object(core, "SUP_DIR", self.sup):
            return gate_held.rows("kant")

    def told(self) -> list[str]:
        """The gate's messages in the instance's inbox, oldest first."""
        return [p.read_text() for p in sorted((self.root / "inbox").glob("*-from-supervisor-gate.md"))]

    def record(self) -> str:
        b = broker.Broker("kant", self.project, {}, self.sup / "broker.log")
        with mock.patch.object(core, "SUP_DIR", self.sup):
            return b._record("gate")

    def hold_one(self) -> dict:
        self.verdict = "baseline"
        self.assertEqual(self.cycle(EDITS), 0)
        held, = self.held()
        return held


class HoldTest(HeldCase):
    def test_a_red_baseline_holds_the_change_and_tells_the_instance(self):
        held = self.hold_one()
        self.assertEqual(self.plugin(), trx.COMMITTED)                         # reverted, as every failed change is
        self.assertEqual(self.code_status(), "")
        self.assertEqual(list(held["files"]), ["plugins/text_stats/main.py"])
        d = self.sup / gate_held.HELD / "kant" / held["id"]
        self.assertEqual((d / "files" / "plugins/text_stats/main.py").read_text(), NEW)   # the bytes it wrote
        self.assertIn("+    return 'stats, better'", (d / "candidate.patch").read_text())
        self.assertEqual(held["base_sha"], trx.git(self.root, "rev-parse", "HEAD").strip())
        self.assertIn(SHARED_FAIL, held["failing"])
        told, = self.told()
        self.assertIn("held: the gate's baseline is red", told)
        self.assertIn(SHARED_FAIL, told)
        self.assertIn("re-gated automatically when the baseline is green", told)
        self.assertIn("code change held", (self.root / "memory" / "journal.md").read_text())
        self.assertEqual(self.rows()[-1]["outcome"], "held")
        self.assertIn(f"held as {held['id']}", self.logs())

    def test_a_genuine_failure_of_the_change_is_reverted_as_today_and_not_held(self):
        self.verdict = "candidate"
        self.assertEqual(self.cycle(EDITS), 0)
        self.assertEqual(self.plugin(), trx.COMMITTED)
        self.assertEqual(self.held(), [])
        self.assertEqual(self.told(), [])
        self.assertIn("were reverted because the tests failed", (self.root / "memory" / "journal.md").read_text())
        row = self.rows()[-1]
        self.assertEqual(row["outcome"], "candidate-failed")
        self.assertEqual(row["failing"], [OWN_FAIL])

    def test_a_kept_change_is_a_row_too(self):
        self.assertEqual(self.cycle(EDITS), 0)
        self.assertEqual(self.plugin(), NEW)
        self.assertEqual(self.rows()[-1]["outcome"], "kept")
        self.assertEqual(self.held(), [])


class RegateTest(HeldCase):
    def test_a_green_baseline_re_gates_the_held_change_and_keeps_it(self):
        held = self.hold_one()
        self.verdict = "pass"
        self.regate()
        self.assertEqual(self.plugin(), NEW)                                      # back, as it wrote it
        self.assertEqual(self.code_status(), "", "kept but not committed")
        self.assertIn("kant: re-gate", trx.git(self.root, "log", "--format=%s", "-n", "1"))
        self.assertEqual(self.held(), [])
        self.assertIn("re-gated: kept", self.told()[-1])
        self.assertEqual(self.rows()[-1]["outcome"], "re-gated")
        self.assertEqual(self.rows()[-1]["result"], "kept")
        self.assertEqual(self.gated[-1], NEW)
        self.assertIn(held["id"], self.logs())

    def test_a_held_change_that_no_longer_applies_is_dropped_with_a_message(self):
        self.hold_one()
        self.verdict = "pass"
        self.assertEqual(self.cycle(EDITS_OTHER), 0)                              # it wrote the same file again
        self.assertEqual(self.plugin(), OTHER)
        gated = len(self.gated)
        self.regate()
        self.assertEqual(self.plugin(), OTHER)                                    # never written over newer work
        self.assertEqual(len(self.gated), gated)                                  # nothing gated
        self.assertEqual(self.held(), [])
        told = self.told()[-1]
        self.assertIn("no longer applies", told)
        self.assertIn("plugins/text_stats/main.py changed since", told)
        self.assertIn("+    return 'stats, better'", told)                       # what it held, still readable
        self.assertEqual(self.rows()[-1]["result"], "dropped")

    def test_a_re_gate_that_fails_on_the_change_reverts_it_and_drops_it(self):
        self.hold_one()
        self.verdict = "candidate"
        self.regate()
        self.assertEqual(self.plugin(), trx.COMMITTED)                            # as strict as any gate
        self.assertEqual(self.code_status(), "")
        self.assertEqual(self.held(), [])
        told = self.told()[-1]
        self.assertIn("re-gated: failed", told)
        self.assertIn(OWN_FAIL, told)
        self.assertEqual(self.rows()[-1]["result"], "candidate-failed")

    def test_a_red_baseline_on_the_first_re_gate_leaves_the_others_their_tries(self):
        self.hold_one()
        self.assertEqual(self.cycle("import pathlib\npathlib.Path('shaft/extra.py').write_text('X = 1\\n')\n"), 0)
        first, second = self.held()
        self.regate()
        self.assertEqual([h["tries"] for h in self.held()], [1, 0])
        self.assertEqual(len(self.gated), 3)                     # two cycles and the first re-gate; never the second

    def test_a_still_red_baseline_keeps_it_held_until_its_tries_run_out(self):
        self.hold_one()
        for i in range(1, gate_held.TRIES):
            self.regate()
            held, = self.held()
            self.assertEqual(held["tries"], i)
            self.assertEqual(self.plugin(), trx.COMMITTED)
            self.assertIn("re-gated: still held", self.told()[-1])
        self.regate()
        self.assertEqual(self.held(), [])
        self.assertEqual(self.plugin(), trx.COMMITTED)
        self.assertIn("re-gated: dropped", self.told()[-1])
        self.assertEqual(self.rows()[-1]["result"], "dropped")

    def test_a_busy_instance_is_not_re_gated_and_keeps_its_held_change(self):
        self.hold_one()
        self.verdict = "pass"
        with mock.patch.object(runner, "SUP_DIR", self.sup), runner.run_lock("kant", wait=True, kind="live"):
            self.regate()
        self.assertEqual(self.plugin(), trx.COMMITTED)
        self.assertEqual(len(self.held()), 1)
        self.assertIn("busy", self.logs())

    def test_a_paused_instance_is_not_re_gated(self):
        self.hold_one()
        self.verdict = "pass"
        (self.sup / "paused-kant").write_text("")
        self.regate()
        self.assertEqual(self.plugin(), trx.COMMITTED)
        self.assertEqual(len(self.held()), 1)


class RecheckTest(HeldCase):
    """A supervisor test that reads the change fails: it is the baseline's only if it fails on the approved code too."""

    def cause(self, on_baseline):
        approved = gate.snapshot(self.root)
        out = gate.Output("x", "candidate", "the supervisor's tests that read my code fail", ["FAIL: t (m.T.t)"],
                          ["FAIL: t (m.T.t)"])
        with self.patched(), mock.patch.object(gate, "instance_part_failures", return_value=on_baseline) as again:
            cause = runner._cause("kant", self.root, approved, out)
        self.assertEqual(again.call_count, 1)
        self.assertNotEqual(again.call_args.args[0], self.root)                  # a protected copy, never the folder
        return cause

    def test_failing_on_the_approved_code_too_is_the_baseline_s(self):
        cause, why = self.cause(["FAIL: t (m.T.t)", "FAIL: other (m.T.other)"])
        self.assertEqual(cause, "baseline")
        self.assertIn("on my approved code too", why)

    def test_passing_on_the_approved_code_is_the_change_s(self):
        self.assertEqual(self.cause([])[0], "candidate")

    def test_no_answer_from_the_approved_code_leaves_it_the_change_s(self):
        self.assertEqual(self.cause(None)[0], "candidate")

    def test_a_plain_text_verdict_is_the_change_s_as_before(self):
        self.assertEqual(runner._cause("kant", self.root, {}, "FAIL: x")[0], "candidate")


class RecordTest(HeldCase):
    def test_record_gate_lists_the_runs_with_failing_names_and_never_another_instance_s(self):
        self.hold_one()
        with mock.patch.object(core, "SUP_DIR", self.sup):
            gate_held.log_run("wren", "candidate-failed", ["shaft/wren_only.py"], "my own tests fail",
                              ["FAIL: test_wren_secret (tests.test_w.T.test_wren_secret)"], "wren's output")
        text = self.record()
        self.assertIn("held: plugins/text_stats/main.py", text)
        self.assertIn(SHARED_FAIL, text)
        self.assertIn("last output:", text)
        self.assertIn("held changes: 1", text)
        self.assertNotIn("wren", text)
        self.assertNotIn("test_wren_secret", text)

    def test_the_output_kept_in_a_row_is_its_last_3000_characters(self):
        with mock.patch.object(core, "SUP_DIR", self.sup):
            gate_held.log_run("kant", "candidate-failed", ["shaft/x.py"], "w", [], "a" * 5000 + "THE END")
        row, = self.rows()
        self.assertEqual(len(row["output"]), gate_held.TAIL_CHARS)
        self.assertTrue(row["output"].endswith("THE END"))

    def test_a_replay_sees_only_the_runs_before_its_cycle(self):
        self.hold_one()
        with mock.patch.object(core, "SUP_DIR", self.sup):
            before = gate_held.describe("kant", as_of=gate_held.datetime(2000, 1, 1))
            after = gate_held.describe("kant", as_of=gate_held.datetime(2999, 1, 1))
        self.assertNotIn("held:", before)
        self.assertIn("held: plugins/text_stats/main.py", after)
        self.assertNotIn("held changes", after)                                  # what is held now is not then's


class SchedulerTest(unittest.TestCase):
    """A green warm-up starts `shaft gate regate` when a change is held; a red one does not."""

    def setUp(self):
        self.started = []

    def start(self, args):
        self.started.append(args)
        return object()

    def test_a_held_change_starts_one_re_gate(self):
        children = {}
        with mock.patch.object(gate_held, "waiting", return_value=["kant"]):
            autonomy.regate_after_warm(children, self.start)
            autonomy.regate_after_warm(children, self.start)                    # running: not a second one
        self.assertEqual(self.started, [["gate", "regate"]])

    def test_nothing_held_starts_nothing(self):
        with mock.patch.object(gate_held, "waiting", return_value=[]):
            autonomy.regate_after_warm({}, self.start)
        self.assertEqual(self.started, [])

    def test_a_re_gate_that_cannot_start_never_stops_the_scheduler(self):
        def broken(args):
            raise OSError("fork: resource temporarily unavailable")
        with mock.patch.object(gate_held, "waiting", return_value=["kant"]), mock.patch.object(autonomy, "log") as log:
            autonomy.regate_after_warm({}, broken)
        self.assertIn("could not start", log.call_args.args[0])

    def serve_with_warm_ending(self, code: int) -> list:
        class Stop(Exception):
            pass

        class Done:
            returncode, stderr = code, None

            def poll(self):
                return code
        seen, regates = {}, []

        def fake_warm(children, st, start, now=None):
            seen.setdefault("children", children)

        def fake_sleep(_):
            if "ticked" in seen:
                raise Stop
            seen["ticked"] = True
            seen["children"]["gate warm"] = (Done(), 0.0)
        sup = Path(tempfile.mkdtemp(prefix="shaft-sched-"))
        self.addCleanup(shutil.rmtree, sup, True)
        with mock.patch.object(autonomy, "gate_warm", fake_warm), mock.patch.object(autonomy, "warm_ended"), \
                mock.patch.object(autonomy, "regate_after_warm", lambda c, s: regates.append(s)), \
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
        return regates

    def test_the_scheduler_loop_re_gates_after_a_green_warm_up_only(self):
        self.assertEqual(self.serve_with_warm_ending(0), [autonomy._start])
        self.assertEqual(self.serve_with_warm_ending(gate.WARM_FAILED), [])

    def test_shaft_gate_regate_runs_the_re_gate(self):
        from supervisor import __main__ as cli
        with mock.patch.object(gate, "stop_stages_on_signal"), \
                mock.patch.object(runner, "regate_all", return_value=0) as regate:
            self.assertEqual(cli.main(["gate", "regate"]), 0)
        regate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
