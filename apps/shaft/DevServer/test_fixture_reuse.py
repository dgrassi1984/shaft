"""Run directly with Python 3.12: fixture.setup called twice in one process (one test file after another) puts every
supervisor path on the second folder. Bug: setup patched each module's SUP_DIR but not the paths modules build from it
at import (feedback.FILE = SUP_DIR / "feedback.jsonl"), so a second test file wrote into the first one's deleted folder
(FileNotFoundError in test_fixture_threads after test_fixture_settings)."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import fixture_server as fixture


def sup_paths() -> dict[str, Path]:
    """Every module-level Path in the supervisor's modules, by module.attribute."""
    return {f"{name}.{attr}": value for name, module in list(sys.modules.items()) if name.startswith("supervisor.")
            for attr, value in list(vars(module).items()) if isinstance(value, Path)}


class FixtureReuseTest(unittest.TestCase):
    def test_second_setup_moves_every_sup_path(self):
        previous = os.environ.get("SHAFT_SUPERVISOR_DIR")
        try:
            with tempfile.TemporaryDirectory(prefix="shaft-fixture-test-") as first, \
                    tempfile.TemporaryDirectory(prefix="shaft-fixture-test-") as second:
                fixture.setup(Path(first), 8899)
                mock.patch.stopall()
                fixture.setup(Path(second), 8899)
                old, new = (Path(d).resolve() / "sup" for d in (first, second))
                from supervisor import feedback
                self.assertEqual(feedback.FILE, new / "feedback.jsonl")
                stale = sorted(k for k, v in sup_paths().items() if v == old or old in v.parents)
                self.assertEqual(stale, [], "paths still on the first test's folder")
        finally:
            mock.patch.stopall()
            if previous is None:
                os.environ.pop("SHAFT_SUPERVISOR_DIR", None)
            else:
                os.environ["SHAFT_SUPERVISOR_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
