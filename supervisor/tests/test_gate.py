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
        ok, out = self.gate()
        self.assertTrue(ok, out)
        self.assertIn("Ran 3 tests", out)
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


if __name__ == "__main__":
    unittest.main()
