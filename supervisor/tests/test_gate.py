"""The self-modification gate restores failing code changes byte-for-byte."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from supervisor import gate


class GateTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-gate-"))
        (self.root / "shaft").mkdir()
        (self.root / "shaft" / "a.py").write_text("A = 1\n")
        (self.root / "config.env").write_text("X=1\n")
        (self.root / "memory").mkdir()
        (self.root / "memory" / "journal.md").write_text("j\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_detects_edit_add_delete_and_ignores_state(self):
        before = gate.snapshot(self.root)
        (self.root / "shaft" / "a.py").write_text("A = 2\n")
        (self.root / "shaft" / "new.py").write_text("B = 1\n")
        (self.root / "config.env").unlink()            # not code: the operator edits it too (test_config_writers)
        (self.root / "memory" / "journal.md").write_text("changed\n")
        (self.root / "shaft" / "__pycache__").mkdir()
        (self.root / "shaft" / "__pycache__" / "a.pyc").write_bytes(b"x")
        self.assertEqual(gate.changed(before, gate.snapshot(self.root)), ["shaft/a.py", "shaft/new.py"])

    def test_restore_undoes_everything(self):
        before = gate.snapshot(self.root)
        (self.root / "shaft" / "a.py").write_text("broken(\n")
        (self.root / "shaft" / "new.py").write_text("B = 1\n")
        touched = gate.changed(before, gate.snapshot(self.root))
        gate.restore(self.root, before, touched)
        self.assertEqual(gate.snapshot(self.root), before)
        self.assertFalse((self.root / "shaft" / "new.py").exists())


class InstanceTestsTest(unittest.TestCase):
    def test_plain_tests_folder_without_init_is_discovered(self):
        # Regression: the first coder handoff wrote tests/test_word_count.py into an instance with no
        # tests/ package; discovery with -t . failed and correct code was rejected.
        import os
        import sys
        from supervisor import sandbox
        root = Path(tempfile.mkdtemp(prefix="shaft-itests-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n"
                                                  "    def test_ok(self):\n        self.assertTrue(True)\n")
        prof = root / "p.sb"
        prof.write_text(sandbox.profile(root, Path.home(), []))
        ok, out = gate.run_instance_tests(root, prof, sandbox.agent_env(os.environ, TMPDIR=str(root)), sys.executable)
        self.assertTrue(ok, out)
        self.assertIn("Ran 1 test", out)

    def _run(self, files: dict[str, str]):
        import os
        import sys
        from supervisor import sandbox
        root = Path(tempfile.mkdtemp(prefix="shaft-itests-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "tests").mkdir()
        for rel, text in files.items():
            (root / "tests" / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / "tests" / rel).write_text(text)
        prof = root / "p.sb"
        prof.write_text(sandbox.profile(root, Path.home(), []))
        return gate.run_instance_tests(root, prof, sandbox.agent_env(os.environ, TMPDIR=str(root)), sys.executable)

    def test_a_tests_folder_with_no_tests_passes(self):
        # Regression (2026-10-13): kant's tests/ held only a stale __pycache__. unittest exits 5 ("NO TESTS
        # RAN"), the gate read that as a failure, and every template update and every change kant made to its
        # own code was rolled back.
        ok, out = self._run({"__pycache__/x.cpython-312.pyc": ""})
        self.assertTrue(ok, out)
        self.assertIn("no tests", out)

    def test_a_test_file_that_cannot_import_still_fails(self):
        # "No tests" must not swallow a broken test: an import error is a failed test, not an empty folder.
        ok, out = self._run({"test_broken.py": "import does_not_exist\n"})
        self.assertFalse(ok, out)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


class FakeProjectCase(unittest.TestCase):
    """A tiny committed project for the gate to take its supervisor tests from, and an empty instance beside it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-gate-tree-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project, self.inst = self.tmp / "project", self.tmp / "inst"
        tests = self.project / "supervisor" / "tests"
        tests.mkdir(parents=True)
        self.inst.mkdir()
        (self.project / "supervisor" / "__init__.py").write_text("")
        (tests / "__init__.py").write_text("")
        self.sandbox = self.project / "supervisor" / "sandbox.py"
        self.sandbox.write_text('PROFILE = "closed"\n')
        # The gate runs the committed tree's own split runner, as it does the real project's.
        shutil.copy(gate.PROJECT / "supervisor" / "gate_suite.py", self.project / "supervisor" / "gate_suite.py")
        (tests / "test_sandbox.py").write_text(
            "import unittest\nfrom pathlib import Path\nfrom supervisor import sandbox\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_profile(self):\n        self.assertEqual(sandbox.PROFILE, 'closed')\n\n"
            "    def test_no_real_instance_within_reach(self):\n"
            "        project = Path(__file__).resolve().parents[2]\n"
            "        self.assertEqual(list((project / 'instances').iterdir()), [])\n"
            "        self.assertNotIn('KEY', (project / '.env').read_text())\n\n"
            "    def test_its_own_repository(self):\n"   # the runner refuses a project without .git
            "        import subprocess\n"
            "        project = Path(__file__).resolve().parents[2]\n"
            "        top = subprocess.run(['git', '-C', str(project), 'rev-parse', '--show-toplevel'],\n"
            "                             capture_output=True, text=True).stdout.strip()\n"
            "        self.assertEqual(Path(top).resolve(), project)\n")
        (self.project / ".gitignore").write_text("/instances/\n/.supervisor/\n.env\n")
        (self.project / "instances" / "kant").mkdir(parents=True)
        (self.project / ".env").write_text("KEY=secret\n")
        git(self.project, "init", "-q")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "start")
        patch = mock.patch.object(gate, "PROJECT", self.project)
        patch.start()
        self.addCleanup(patch.stop)

    def gate(self, timeout_s: int = 120) -> tuple[bool, str]:
        return gate.run_tests(self.inst, Path.home(), timeout_s=timeout_s)


class CommittedTreeTest(FakeProjectCase):
    """The supervisor's tests in the gate come from the last commit, not from the checkout other sessions edit."""

    def test_half_written_sandbox_in_the_checkout_does_not_fail_the_gate_20261016(self):
        # Regression (2026-10-16 09:40): another session was mid-edit on supervisor/sandbox.py in the main checkout;
        # the gate ran the supervisor's tests from that half-written file, 37 failed, and shaft's good
        # plugins/write_gate change was reverted. An edit nobody committed must not decide a verdict.
        ok, out = self.gate()
        self.assertTrue(ok, out)
        self.sandbox.write_text('PROFILE = "half-writ')                       # a syntax error, mid-edit
        (self.project / "supervisor" / "tests" / "test_wip.py").write_text("import unittest\n"
            "class W(unittest.TestCase):\n    def test_unfinished(self):\n        self.fail('wip')\n")
        tree, _ = gate.committed_tree(self.project)
        for record in tree.parent.glob("shared-passed-*.json"):            # run them again, not from the record
            record.unlink()
        ok, out = self.gate()
        self.assertTrue(ok, out)
        self.assertIn("Ran 3 tests", out)                                 # the committed three, not the checkout's
        self.assertIn(f"supervisor invariants ({gate._git(self.project, 'rev-parse', 'HEAD').decode()[:10]})", out)

    def test_a_committed_breaking_change_still_fails_the_gate(self):
        self.assertTrue(self.gate()[0])
        self.sandbox.write_text('PROFILE = "open"\n')
        git(self.project, "commit", "-qam", "break the sandbox")
        ok, out = self.gate()
        self.assertFalse(ok, out)
        self.assertIn("test_profile", out)

    def test_no_committed_copy_fails_closed(self):
        # Raising would leave the change on disk unsettled, and the next run would take it as its baseline: ungated.
        shutil.rmtree(self.project / ".git")
        ok, out = self.gate()
        self.assertFalse(ok)
        self.assertIn("no committed copy", out)

    def test_one_tree_per_commit_and_idle_trees_go(self):
        first, _ = gate.committed_tree(self.project)
        self.assertEqual(gate.committed_tree(self.project)[0], first)            # reused, not copied again
        (self.project / "notes.txt").write_text("unfinished")                   # untracked: not in the tree
        git(self.project, "commit", "-q", "--allow-empty", "-m", "next")
        self.assertEqual(first.name, "project")       # named like the project: tests may read its name
        old = time.time() - gate.TREE_IDLE_S - 60
        os.utime(first.parent, (old, old))
        second, _ = gate.committed_tree(self.project)
        self.assertNotEqual(second, first)
        self.assertFalse(first.parent.exists())
        self.assertFalse((second / "notes.txt").exists())


# A shared supervisor test that says each time it runs, in a file outside the tree (the tree is the test's cwd).
COUNTS_ITS_RUNS = ("import unittest\nfrom pathlib import Path\n\nclass C(unittest.TestCase):\n"
                   "    def test_counted(self):\n"
                   "        with open({log!r}, 'a') as f:\n            f.write('ran\\n')\n"
                   "        if Path({fail!r}).exists():\n            self.fail('asked to fail')\n")
# An instance-part test: it reads the instance under test, and fails when the instance holds a file named BAD.
READS_THE_INSTANCE = ("import os, unittest\nfrom pathlib import Path\n\n"
                      "TARGET = Path(os.environ['SHAFT_UNDER_TEST'])\n\n"
                      "class I(unittest.TestCase):\n    def test_instance_is_good(self):\n"
                      "        self.assertFalse((TARGET / 'BAD').exists(), 'the instance is bad')\n")


class SharedRecordTest(FakeProjectCase):
    """PLAN-gate-cache M1: the shared part of the supervisor's tests runs once per commit, its pass recorded beside the
    committed tree; the instance part runs at every gate. Regression for 2026-10-27 14:13–14:23: thelos's settle held
    the instance for 10 minutes re-running 2,000 supervisor tests that had not changed."""

    def setUp(self):
        super().setUp()
        self.log, self.fail_flag = self.tmp / "runs.log", self.tmp / "fail-now"
        tests = self.project / "supervisor" / "tests"
        (tests / "test_counted.py").write_text(COUNTS_ITS_RUNS.format(log=str(self.log), fail=str(self.fail_flag)))
        (tests / "test_invariants.py").write_text(READS_THE_INSTANCE)
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "counted and instance tests")

    def runs(self) -> int:
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def record(self) -> Path:
        """The record of a gate run with this process's settings."""
        tree, _ = gate.committed_tree(self.project)
        return gate.record_path(tree, gate.shared_key(tree, {**os.environ, "SHAFT_UNDER_TEST": str(self.inst)}))

    def test_a_second_gate_on_the_same_commit_does_not_run_the_shared_part_again(self):
        ok, out = self.gate()
        self.assertTrue(ok, out)
        self.assertEqual(self.runs(), 1)
        self.assertTrue(self.record().is_file())
        ok, out = self.gate()
        self.assertTrue(ok, out)
        self.assertEqual(self.runs(), 1)
        self.assertIn("-- instance part: 1 tests --", out)
        self.assertIn("-- shared part: passed on this commit at ", out)
        self.assertIn("(4 tests, ", out)
        self.assertIn("not run again --", out)

    def test_a_new_commit_runs_it_again(self):
        self.assertTrue(self.gate()[0])
        git(self.project, "commit", "-q", "--allow-empty", "-m", "next")
        self.assertTrue(self.gate()[0])
        self.assertEqual(self.runs(), 2)

    def test_a_failure_is_not_recorded(self):
        self.fail_flag.write_text("x")
        ok, out = self.gate()
        self.assertFalse(ok, out)
        self.assertIn("FAIL: test_counted", out)
        self.assertFalse(self.record().exists())
        self.fail_flag.unlink()
        self.assertTrue(self.gate()[0])                  # ran again, passed, recorded
        self.assertTrue(self.gate()[0])
        self.assertEqual(self.runs(), 2)

    def test_a_record_that_does_not_match_is_ignored(self):
        import json
        self.assertTrue(self.gate()[0])
        good = json.loads(self.record().read_text())
        old = (gate.datetime.now() - gate.timedelta(seconds=gate.TREE_IDLE_S + 60)).isoformat(timespec="seconds")
        future = (gate.datetime.now() + gate.timedelta(hours=1)).isoformat(timespec="seconds")
        bad = {"python": {**good, "python": "/usr/bin/python3:0:0"}, "env": {**good, "env": "0" * 64},
               "commits": {**good, "commits": "0" * 16}, "old": {**good, "passed_at": old},
               "future": {**good, "passed_at": future}, "none ran": {**good, "tests_run": 0},
               "count not a number": {**good, "tests_run": "4"}, "not a dict": [good]}
        runs = 1
        for why, record in bad.items():
            with self.subTest(why=why):
                self.record().write_text(json.dumps(record))
                ok, out = self.gate()
                self.assertTrue(ok, out)
                runs += 1
                self.assertEqual(self.runs(), runs, why)
        self.record().write_text("{not json")
        self.assertTrue(self.gate()[0])
        self.assertEqual(self.runs(), runs + 1)

    def test_the_settings_the_tests_see_are_part_of_the_key(self):
        self.assertTrue(self.gate()[0])
        with mock.patch.dict(os.environ, {"SHAFT_SOME_KNOB": "2"}):
            self.assertTrue(self.gate()[0])
        self.assertEqual(self.runs(), 2)
        self.assertTrue(self.gate()[0])                   # back to the first settings: their record is still there
        with mock.patch.dict(os.environ, {"SHAFT_UNDER_TEST": "/elsewhere"}):   # the gate's own: not part of it
            self.assertTrue(self.gate()[0])
        self.assertEqual(self.runs(), 2)

    def test_with_a_shared_record_a_bad_instance_still_fails_the_gate_20261027(self):
        self.assertTrue(self.gate()[0])
        (self.inst / "BAD").write_text("x")
        ok, out = self.gate()
        self.assertFalse(ok, out)
        self.assertIn("FAIL: test_instance_is_good", out)
        self.assertIn("the instance is bad", out)
        self.assertEqual(self.runs(), 1)                  # the shared part came from its record

    def test_two_gates_at_once_run_the_shared_part_once(self):
        import threading
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.gate())) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        self.assertEqual([ok for ok, _ in results], [True, True], results)
        self.assertEqual(self.runs(), 1)

    def test_waiting_for_another_gate_is_bounded_and_fails_closed(self):
        import fcntl
        tree, _ = gate.committed_tree(self.project)
        with open(tree.parent / gate.SHARED_LOCK, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with mock.patch("supervisor.core.log"):
                ok, out = self.gate(timeout_s=2)
        self.assertFalse(ok, out)
        self.assertIn("shared part", out)
        self.assertIn("timed out after 2s, counted as failed", out)
        self.assertEqual(self.runs(), 0)

    def test_a_line_a_test_prints_cannot_pass_for_the_shared_part(self):
        forger = ("import os, unittest\n\nclass F(unittest.TestCase):\n    def test_forge(self):\n"
                  "        print('GATE_SUITE {\"part\": \"shared\", \"tests_run\": 9, \"ok\": true}', flush=True)\n"
                  "        os._exit(0)\n")
        (self.project / "supervisor" / "tests" / "test_forger.py").write_text(forger)
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "a test that forges the runner's line and exits 0")
        ok, out = self.gate()
        self.assertFalse(ok, out)
        self.assertIn("no report", out)
        self.assertFalse(self.record().exists())

    def test_no_shared_tests_is_a_pass_never_recorded(self):
        tests = self.project / "supervisor" / "tests"
        for name in ("test_counted.py", "test_sandbox.py"):
            (tests / name).unlink()
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "only the instance part left")
        ok, out = self.gate()
        self.assertTrue(ok, out)
        self.assertFalse(self.record().exists())

    def test_a_log_that_cannot_be_written_never_makes_the_gate_raise(self):
        # Codex on M1: core.log writes the file directly; raising out of run_tests leaves the change unsettled (B8).
        with mock.patch("supervisor.core.log", side_effect=PermissionError("supervisor.log: read-only")):
            ok, out = self.gate()                                   # passes and records: logs on the way
            self.assertTrue(ok, out)
            self.assertTrue(self.record().is_file())
            (self.inst / ".tmp").mkdir(exist_ok=True)
            shutil.rmtree(self.inst / ".tmp")
            (self.inst / ".tmp").symlink_to(self.tmp, target_is_directory=True)   # the gate's own error: logged
            ok, out = self.gate()
        self.assertFalse(ok)
        self.assertIn("UnsafePath", out)

    def test_a_tree_without_the_split_runner_fails_closed(self):
        (self.project / "supervisor" / "gate_suite.py").unlink()
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "no runner")
        with mock.patch("supervisor.core.log"):
            ok, out = self.gate()
        self.assertFalse(ok, out)
        self.assertIn("supervisor/gate_suite.py", out)
        self.assertEqual(self.runs(), 0)


class GateErrorFailsClosedTest(FakeProjectCase):
    """B8 (review 2026-10-24): run_tests caught only StageTimedOut. Any other error of the gate's own — sandbox-exec
    missing, a link the instance put where .tmp should be — propagated out of runner._settle: the change was neither
    tested nor reverted, and the next run took it as its baseline."""

    def test_a_link_where_tmp_should_be_fails_the_gate(self):
        (self.inst / ".tmp").symlink_to(self.tmp, target_is_directory=True)
        with mock.patch("supervisor.core.log") as log:
            ok, out = self.gate()
        self.assertFalse(ok)
        self.assertIn("UnsafePath", out)
        self.assertIn("counted as failed", "\n".join(c.args[0] for c in log.call_args_list))

    def test_a_stage_that_cannot_start_fails_the_gate(self):
        with mock.patch.object(gate, "_stage", side_effect=FileNotFoundError("/usr/bin/sandbox-exec")), \
                mock.patch("supervisor.core.log"):
            ok, out = self.gate()
        self.assertFalse(ok)
        self.assertIn("FileNotFoundError: /usr/bin/sandbox-exec", out)
        self.assertIn("full log: ", out)


HANGS = ("import sys, time, unittest\n\nclass T(unittest.TestCase):\n    def test_hangs(self):\n"
         "        sys.stderr.write('test_hangs started\\n'); sys.stderr.flush(); time.sleep(120)\n")


class StageTimeoutTest(FakeProjectCase):
    """Regression (found 2026-10-16, by reading): a gate stage that ran past its timeout raised TimeoutExpired out of
    run_tests, and runner._settle has no handler: the instance's change stayed on disk neither kept nor reverted, and
    the next run's snapshot took it as its baseline, so it passed without ever being gated. Each stage's timeout must
    fail the gate, named in the output the instance is shown and in the supervisor log."""

    def gate_timing_out(self, timeout_s: int) -> tuple[bool, str, str]:
        with mock.patch("supervisor.core.log") as log:
            ok, out = self.gate(timeout_s)
        logged = "\n".join(c.args[0] for c in log.call_args_list)
        return ok, out, logged

    def test_supervisor_invariants_timing_out_fail_the_gate(self):
        (self.project / "supervisor" / "tests" / "test_slow.py").write_text(HANGS)
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "a supervisor test that hangs")
        (self.inst / "tests").mkdir()
        (self.inst / "tests" / "test_ok.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n"
                                                       "    def test_ok(self):\n        pass\n")
        ok, out, logged = self.gate_timing_out(3)
        self.assertFalse(ok, out)
        self.assertIn("supervisor invariants (", out)
        self.assertIn("timed out after 3s, counted as failed", out)
        self.assertNotIn("== instance tests ==", out)                   # the checks after it did not run
        self.assertIn("supervisor invariants (", logged)
        self.assertIn("timed out after 3s", logged)

    def test_the_plugins_check_timing_out_fails_the_gate(self):
        (self.inst / "plugins").mkdir()
        (self.inst / "shaft").mkdir()
        (self.inst / "shaft" / "__init__.py").write_text("")
        (self.inst / "shaft" / "plugin_runner.py").write_text("import time\ntime.sleep(120)\n")
        with mock.patch.object(gate, "PLUGIN_CHECK_S", 3):
            ok, out, logged = self.gate_timing_out(60)
        self.assertFalse(ok, out)
        self.assertIn("== plugins ==\n", out)
        self.assertIn("plugins: timed out after 3s, counted as failed", out)
        self.assertIn("plugins: timed out after 3s", logged)

    def test_instance_tests_timing_out_fail_the_gate_and_say_which_test(self):
        (self.inst / "tests").mkdir()
        (self.inst / "tests" / "test_slow.py").write_text(HANGS)
        ok, out, logged = self.gate_timing_out(5)
        self.assertFalse(ok, out)
        self.assertIn("instance tests: timed out after 5s, counted as failed", out)
        self.assertIn("test_hangs started", out)                       # what it printed before it was stopped
        self.assertIn("instance tests: timed out after 5s", logged)


LEAVES_A_WRITER = (   # a test that starts a background worker: it says it started, then writes the instance's code
    "import subprocess, sys, time, unittest\nfrom pathlib import Path\n\n"
    "WORKER = (\"import time\\nfrom pathlib import Path\\nPath('started').write_text('x')\\ntime.sleep({late})\\n\"\n"
    "          \"Path('shaft/late.py').write_text('WRITTEN_AFTER_THE_GATE = True\\\\n')\\n\")\n\n"
    "class T(unittest.TestCase):\n    def test_server(self):\n"
    "        subprocess.Popen([sys.executable, '-c', WORKER], stdin=subprocess.DEVNULL,\n"
    "                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "        while not Path('started').exists():\n            time.sleep(0.05)\n"
    "        {then}\n")


class StageProcessGroupTest(FakeProjectCase):
    """Regression (found 2026-10-16, by reading): on a timeout subprocess.run killed only the stage's direct child
    (sandbox-exec, exec'd into python). A process a test had started kept running in the instance's sandbox after the
    gate had failed and runner._settle had reverted the change, so it could write the instance's code after the
    revert. Nothing a stage started may outlive it."""

    def assert_no_write_after_the_gate(self, then: str, timeout_s: int, late_s: int) -> str:
        (self.inst / "shaft").mkdir()
        (self.inst / "tests").mkdir()
        (self.inst / "tests" / "test_server.py").write_text(LEAVES_A_WRITER.format(late=late_s, then=then))
        ok, out = self.gate(timeout_s)
        returned = time.monotonic()
        self.assertFalse(ok, out)
        self.assertTrue((self.inst / "started").exists(), out)            # the worker did start: not a vacuous pass
        while time.monotonic() - returned < late_s + 2:                    # past the moment it would have written
            self.assertFalse((self.inst / "shaft" / "late.py").exists(),
                             "a process the gate's stage started wrote the instance's code after run_tests returned")
            time.sleep(0.2)
        return out

    def test_a_process_a_timed_out_test_started_cannot_write_the_code_after_the_gate_20261016(self):
        out = self.assert_no_write_after_the_gate("time.sleep(120)", timeout_s=4, late_s=4)
        self.assertIn("instance tests: timed out after 4s, counted as failed", out)

    def test_a_process_a_failing_test_left_behind_cannot_write_the_code_after_the_gate(self):
        # The same race without a timeout: the stage ends, the gate fails, _settle reverts, the worker writes.
        out = self.assert_no_write_after_the_gate("self.fail('broken')", timeout_s=60, late_s=3)
        self.assertIn("broken", out)


REACH_OUT = (   # tries the operator's listener, says what happened in a file the regression test reads
    "import socket\nfrom pathlib import Path\n\n"
    "def reach(stage):\n"
    "    try:\n"
    "        socket.create_connection(('127.0.0.1', {port}), timeout=3).close()\n"
    "        seen = 'CONNECTED'\n"
    "    except OSError as e:\n"
    "        seen = 'REFUSED ' + repr(e)\n"
    "    Path(stage + '.seen').write_text(seen)\n")


class GateLocalhostTest(FakeProjectCase):
    """Regression (found 2026-10-18, planning docs/plans/PLAN-browser.md): the gate sandboxed an instance's plugin check and its
    own tests with network* allowed on "localhost:*", so instance-written code could connect to any service on the
    operator's Mac — his dev servers, databases, Jupyter, the model and embedding servers. Nothing of his may be
    reachable from an instance. A listener the gate's sandbox did not open, started here outside it, must see no
    connection from either stage."""

    def setUp(self):
        super().setUp()
        import socket
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(self.listener.close)
        self.listener.setblocking(False)
        probe = REACH_OUT.format(port=self.listener.getsockname()[1])
        (self.inst / "shaft").mkdir()
        (self.inst / "shaft" / "__init__.py").write_text("")
        (self.inst / "shaft" / "reach.py").write_text(probe)
        (self.inst / "shaft" / "plugin_runner.py").write_text("from shaft.reach import reach\n\nreach('plugins')\n")
        (self.inst / "plugins").mkdir()
        (self.inst / "tests").mkdir()
        (self.inst / "tests" / "test_reach.py").write_text(
            "import sys, unittest\nsys.path.insert(0, '.')\nfrom shaft.reach import reach\n\n"
            "class T(unittest.TestCase):\n    def test_reach(self):\n        reach('tests')\n")

    def test_gate_sandbox_cannot_reach_a_localhost_port_it_did_not_open_20261018(self):
        ok, out = self.gate()
        for stage in ("plugins", "tests"):
            seen = self.inst / f"{stage}.seen"
            self.assertTrue(seen.exists(), f"the {stage} stage never tried: not a real check\n{out}")
            self.assertTrue(seen.read_text().startswith("REFUSED"),
                            f"the gate's {stage} stage reached the operator's localhost listener: "
                            f"{seen.read_text()}")
        with self.assertRaises(BlockingIOError, msg="the operator's listener accepted a connection from the gate"):
            self.listener.accept()
        self.assertTrue(ok, out)   # refused, the probe still ran clean: the gate itself did not fail on the way


if __name__ == "__main__":
    unittest.main()
