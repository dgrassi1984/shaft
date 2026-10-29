"""A test run never reaches the real supervisor state or the real instances (supervisor/tests/__init__.py)."""
import os
import unittest

from supervisor import core, metrics, provenance, surprise


class TestRunIsolationTest(unittest.TestCase):
    def test_the_instances_folder_is_not_the_real_one(self):
        """test_judge2 read wren's real facts and episodes through core.INSTANCES: 72 s, and an outcome that
        depended on live data (2026-10-29)."""
        real = core.PROJECT / "instances"
        for module in (core, metrics, surprise):
            with self.subTest(module=module.__name__):
                self.assertNotEqual(module.INSTANCES, real)
        self.assertEqual(provenance.facts("wren"), [])

    def test_the_supervisor_folder_is_not_the_real_one(self):
        self.assertNotEqual(core.SUP_DIR, core.PROJECT / ".supervisor")
        self.assertEqual(str(core.SUP_DIR), os.environ["SHAFT_SUPERVISOR_DIR"])


if __name__ == "__main__":
    unittest.main()
