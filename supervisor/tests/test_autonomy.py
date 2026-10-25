"""The schedule SHAFT's scheduler keeps: per-instance wakes (the operator: an interval in each config.env; a wake
that comes while the agent is already awake is skipped) and the nightly sleep — including the sleep launchd
silently skipped on 2026-10-13."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy
from supervisor.autonomy import decide_wakes, sleep_due

D = datetime.fromisoformat
H2 = timedelta(hours=2)


class SleepDueTest(unittest.TestCase):
    def test_a_missed_sleep_is_made_up_later_that_day(self):
        # Regression: launchd's domain went on-demand-only; the 04:00 sleep never started and nobody noticed.
        st = {"sleep": "2026-10-12T04:00:05"}
        self.assertTrue(sleep_due(D("2026-10-13T04:45:00"), st))
        self.assertTrue(sleep_due(D("2026-10-13T15:00:00"), st))

    def test_one_sleep_per_day_and_not_before_its_time(self):
        st = {"sleep": "2026-10-13T04:00:02"}
        self.assertFalse(sleep_due(D("2026-10-13T04:30:00"), st))
        self.assertFalse(sleep_due(D("2026-10-14T03:59:00"), st))
        self.assertTrue(sleep_due(D("2026-10-14T04:00:00"), st))


class WakeTest(unittest.TestCase):
    def test_each_instance_on_its_own_interval(self):
        st = {"wakes": {"shaft": "2026-10-13T10:00:00", "wren": "2026-10-13T10:00:00"}}
        every = {"shaft": H2, "wren": timedelta(minutes=30)}
        start, skip = decide_wakes(D("2026-10-13T10:45:00"), st, ["shaft", "wren"], every.get, lambda n: "")
        self.assertEqual((start, skip), (["wren"], {}))
        start, _ = decide_wakes(D("2026-10-13T12:00:00"), st, ["shaft", "wren"], every.get, lambda n: "")
        self.assertEqual(start, ["shaft", "wren"])

    def test_a_wake_while_already_awake_is_skipped_not_queued(self):
        st = {"wakes": {"shaft": "2026-10-13T10:00:00"}}
        start, skip = decide_wakes(D("2026-10-13T12:00:00"), st, ["shaft"], lambda n: H2,
                                   lambda n: "talk since 11:40")
        self.assertEqual(start, [])
        self.assertEqual(skip, {"shaft": "talk since 11:40"})

    def test_a_queued_update_starts_once_idle_and_holds_back_the_wake(self):
        # Regression (2026-10-25): an update was refused while busy and lost every retry to the next cycle.
        queued = {"x": "2026-10-25T12:45:00", "y": "2026-10-25T12:45:00"}
        with mock.patch.object(autonomy, "update_queued", queued.get):
            self.assertEqual(autonomy.update_jobs({}, ["x", "y", "z"], {"y": "live since 12:39"}.get), ["x"])
            self.assertEqual(autonomy.update_jobs({"update x": None}, ["x"], lambda n: ""), [])   # running
            self.assertEqual(autonomy.update_jobs({"wake x": None}, ["x"], lambda n: ""), [])     # waking
            self.assertTrue(autonomy.updating("y", {}))
            self.assertTrue(autonomy.updating("z", {"update z": None}))
            self.assertFalse(autonomy.updating("z", {}))

    def test_never_woken_is_due_and_zero_means_never(self):
        start, _ = decide_wakes(D("2026-10-13T12:00:00"), {}, ["new", "off"],
                                {"new": H2, "off": None}.get, lambda n: "")
        self.assertEqual(start, ["new"])

    def test_interval_from_config_env_with_a_floor(self):
        def interval(value):
            root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
            (root / "config.env").write_text(f"WAKE_INTERVAL_MINUTES={value}\n" if value is not None else "")
            with mock.patch("supervisor.core.instance_root", return_value=root), mock.patch.object(autonomy, "log"):
                return autonomy.wake_interval("x")
        self.assertEqual(interval(None), timedelta(minutes=120))      # not set: the old two hours
        self.assertEqual(interval(45), timedelta(minutes=45))
        self.assertEqual(interval(1), timedelta(minutes=10))          # its own file: no faster than the floor
        self.assertIsNone(interval(0))
        self.assertEqual(interval("soon"), timedelta(minutes=120))    # not a number: the default, logged

    def test_the_instance_is_told_its_own_interval(self):
        with mock.patch.object(autonomy, "wake_interval", return_value=timedelta(minutes=45)), \
             mock.patch.object(Path, "exists", return_value=True):
            text = autonomy.describe_schedule("wren")
        self.assertIn("every 45 minutes", text)
        self.assertIn("is skipped", text)


if __name__ == "__main__":
    unittest.main()
