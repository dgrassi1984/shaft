"""The self-modification gate restores failing code changes byte-for-byte."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

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
        (self.root / "config.env").unlink()
        (self.root / "memory" / "journal.md").write_text("changed\n")
        (self.root / "shaft" / "__pycache__").mkdir()
        (self.root / "shaft" / "__pycache__" / "a.pyc").write_bytes(b"x")
        self.assertEqual(gate.changed(before, gate.snapshot(self.root)),
                         ["config.env", "shaft/a.py", "shaft/new.py"])

    def test_restore_undoes_everything(self):
        before = gate.snapshot(self.root)
        (self.root / "shaft" / "a.py").write_text("broken(\n")
        (self.root / "shaft" / "new.py").write_text("B = 1\n")
        (self.root / "config.env").unlink()
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


if __name__ == "__main__":
    unittest.main()
