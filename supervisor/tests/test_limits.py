"""The operator's own limits for an instance (`shaft limits`): a wake floor below the default and a daily cloud cap
above it, kept in supervisor/tools/<name>.json — never in its config.env, which the instance can change itself."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy, cloudllm, core


class LimitsTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-limits-"))
        p = mock.patch.object(core, "SUP_DIR", self.sup)
        p.start()
        self.addCleanup(p.stop)

    def cli(self, *argv) -> tuple[int, str]:
        from supervisor import __main__ as cli
        out = io.StringIO()
        with mock.patch.object(cli, "instance_root", lambda n: self.sup), \
                mock.patch.object(cli, "instance_names", lambda: ["wren"]), \
                mock.patch.object(autonomy, "instance_config", lambda n: {"WAKE_INTERVAL_MINUTES": "5"}), \
                mock.patch.object(cli, "log", lambda m: None), redirect_stdout(out), redirect_stderr(out):
            code = cli.main(["limits", *argv])
        return code, out.getvalue()

    def test_the_operator_s_floor_lets_wren_wake_every_5_minutes(self):
        config = {"WAKE_INTERVAL_MINUTES": "5"}
        self.assertEqual(autonomy.wake_interval("wren", config), timedelta(minutes=autonomy.WAKE_FLOOR_MIN))
        core._update_tool_settings("wren", wake_floor_minutes=5)
        self.assertEqual(autonomy.wake_interval("wren", config), timedelta(minutes=5))
        self.assertEqual(autonomy.wake_interval("kant", config), timedelta(minutes=autonomy.WAKE_FLOOR_MIN))

    def test_the_instance_cannot_lower_its_own_floor(self):
        # The floor lives in the supervisor's folder; a key in config.env (the instance's own file) is ignored.
        config = {"WAKE_INTERVAL_MINUTES": "1", "WAKE_FLOOR_MINUTES": "1", "wake_floor_minutes": "1"}
        self.assertEqual(autonomy.wake_interval("wren", config), timedelta(minutes=autonomy.WAKE_FLOOR_MIN))

    def test_the_operator_s_cap_is_the_one_enforced(self):
        body = {"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "hi"}]}
        with mock.patch.object(cloudllm, "spent_today", lambda i: 9.0):
            with self.assertRaisesRegex(cloudllm.CloudError, r"spent \$9.00 .* \(cap \$8.00\)"):
                cloudllm.complete("wren", dict(body), {"ANTHROPIC_API_KEY": "k"}, {})
            core._update_tool_settings("wren", cloud_daily_usd=15)
            self.assertEqual(cloudllm.daily_cap("wren"), 15.0)
            with mock.patch.object(cloudllm, "_send", return_value=(None, "stopped here")), \
                    self.assertRaisesRegex(cloudllm.CloudError, "cloud model call failed"):   # past the cap check
                cloudllm.complete("wren", dict(body), {"ANTHROPIC_API_KEY": "k"}, {})
        self.assertEqual(cloudllm.daily_cap("wren@trial:t-1"), cloudllm.DAILY_USD)        # trials keep the default
        self.assertEqual(cloudllm.daily_cap("kant"), cloudllm.DAILY_USD)

    def test_cli_sets_shows_and_resets_and_keeps_other_settings(self):
        core._update_tool_settings("wren", off=["delegate_coding"], mail=[{"account": "a", "label": "L"}])
        self.assertEqual(self.cli("wren", "wake-floor", "5")[0], 0)
        self.assertEqual(self.cli("wren", "cloud-cap", "15")[0], 0)
        code, out = self.cli("wren")
        self.assertIn("wakes every 5 min · wake floor 5 min (yours) · cloud cap $15.00 (yours)", out)
        saved = json.loads((self.sup / "tools" / "wren.json").read_text())
        self.assertEqual((saved["wake_floor_minutes"], saved["cloud_daily_usd"]), (5, 15.0))
        self.assertEqual(saved["off"], ["delegate_coding"])                                # other settings kept
        for bad in (("wake-floor", "0"), ("wake-floor", "2.5"), ("cloud-cap", "-1"), ("cloud-cap", "lots")):
            self.assertEqual(self.cli("wren", *bad)[0], 2, bad)
        self.cli("wren", "wake-floor", "off")
        self.assertNotIn("wake_floor_minutes", json.loads((self.sup / "tools" / "wren.json").read_text()))
        self.assertEqual(autonomy.wake_floor("wren"), autonomy.WAKE_FLOOR_MIN)


if __name__ == "__main__":
    unittest.main()
