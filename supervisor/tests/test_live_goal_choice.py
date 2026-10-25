"""E4 (docs/plans/PLAN-measurement.md §3): LIVE step 2 asks for the most valuable goal that can be finished, not
the smallest. Held until the PLAN-surprise M5 window closes; until template/ has it, this test skips and says so."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import prompts

E4 = "Choose the most valuable goal you can finish"


class LiveGoalChoiceTest(unittest.TestCase):
    def test_step_2_no_longer_prefers_small_goals(self):
        if E4 not in prompts.LIVE:
            self.skipTest("template/ predates E4 (claude/e4-live-step2, held until the M5 window closes)")
        step2 = prompts.LIVE.split("\n2. ", 1)[1].split("\n3. ", 1)[0]
        self.assertNotIn("small finished thing", step2)
        self.assertIn("finish a checkable piece of", step2)          # the "finished" half stays
        self.assertIn("a goal that is easy for you teaches you little", step2)
        self.assertIn("{steps}", prompts.LIVE)                        # the turn budget it refers to is still given


if __name__ == "__main__":
    unittest.main()
