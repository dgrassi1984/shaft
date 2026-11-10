"""A test run never reaches the real supervisor state or the real instances (supervisor/tests/__init__.py)."""
import os
import unittest

from supervisor import core, metrics, provenance, surprise


class TestRunIsolationTest(unittest.TestCase):
    def test_the_instances_folder_is_not_the_real_one(self):
        """test_judge2 read wisp's real facts and episodes through core.INSTANCES: 72 s, and an outcome that
        depended on live data (2026-10-29)."""
        real = core.PROJECT / "instances"
        for module in (core, metrics, surprise):
            with self.subTest(module=module.__name__):
                self.assertNotEqual(module.INSTANCES, real)
        self.assertEqual(provenance.facts("wisp"), [])

    def test_no_module_holds_a_folder_a_test_patched_in_while_it_was_imported(self):
        """trial.SUP_DIR froze on test_browser's temporary folder when trial was first imported inside a patch, and
        test_kill_delete's trials vanished whenever the gate's workers ran those two in one process (2026-10-29).
        supervisor/tests/__init__.py imports every module before any test runs."""
        import sys
        for name, module in sorted(sys.modules.items()):
            if not name.startswith("supervisor.") or name.startswith("supervisor.tests") or module is None:
                continue
            for attr in ("SUP_DIR", "INSTANCES"):
                if attr in vars(module):
                    with self.subTest(module=name, attr=attr):
                        self.assertEqual(vars(module)[attr], getattr(core, attr))
        for name in ("trial", "kill", "jobs", "runner", "metrics"):   # among those the run above found frozen
            self.assertIn(f"supervisor.{name}", sys.modules)

    def test_the_supervisor_folder_is_not_the_real_one(self):
        self.assertNotEqual(core.SUP_DIR, core.PROJECT / ".supervisor")
        self.assertEqual(str(core.SUP_DIR), os.environ["SHAFT_SUPERVISOR_DIR"])


if __name__ == "__main__":
    unittest.main()
