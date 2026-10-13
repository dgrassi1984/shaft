"""The schedule SHAFT's scheduler keeps — including the sleep launchd silently skipped on 2026-10-13."""
from __future__ import annotations

import unittest
from datetime import datetime

from supervisor.autonomy import due

D = datetime.fromisoformat


class DueTest(unittest.TestCase):
    def test_a_missed_sleep_is_made_up_later_that_day(self):
        # Regression: launchd's domain went on-demand-only; the 04:00 sleep never started and nobody noticed.
        st = {"sleep": "2026-10-12T04:00:05", "tick": "2026-10-13T04:30:00"}
        self.assertEqual(due(D("2026-10-13T04:45:00"), st), ["sleep"])
        self.assertEqual(due(D("2026-10-13T15:00:00"), st), ["sleep", "tick"])

    def test_one_sleep_per_day_and_not_before_its_time(self):
        st = {"sleep": "2026-10-13T04:00:02", "tick": "2026-10-13T03:00:00"}
        self.assertEqual(due(D("2026-10-13T04:30:00"), st), [])
        self.assertEqual(due(D("2026-10-14T03:59:00"), {"sleep": "2026-10-13T04:00:02",
                                                       "tick": "2026-10-14T03:00:00"}), [])
        self.assertIn("sleep", due(D("2026-10-14T04:00:00"), st))

    def test_cycle_every_two_hours(self):
        self.assertEqual(due(D("2026-10-13T03:24:00"), {"sleep": "2026-10-13T04:00", "tick": "2026-10-13T01:25:10"}), [])
        self.assertEqual(due(D("2026-10-13T03:26:00"), {"sleep": "2026-10-12T04:00", "tick": "2026-10-13T01:25:10"}),
                         ["tick"])
        self.assertEqual(due(D("2026-10-13T03:26:00"), {"sleep": "2026-10-13T00:10"}), ["tick"])   # never ticked


if __name__ == "__main__":
    unittest.main()
