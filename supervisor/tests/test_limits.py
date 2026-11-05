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

    def test_tool_setting_cas_and_atomic_replace_preserve_other_keys(self):
        core._update_tool_settings("wren", channel="web", cloud_daily_usd=5)
        with self.assertRaises(core.StaleSetting):
            core._update_tool_settings("wren", expect={"channel": "auto"}, channel="telegram")
        real = core.os.replace
        def replace(source, destination):
            self.assertEqual(json.loads(destination.read_text())["channel"], "web")
            self.assertEqual(json.loads(Path(source).read_text()), {"channel": "telegram", "cloud_daily_usd": 5})
            real(source, destination)
        with mock.patch.object(core.os, "replace", side_effect=replace) as replaced:
            core._update_tool_settings("wren", expect={"channel": "web"}, channel="telegram")
        replaced.assert_called_once()
        self.assertEqual(core._tool_settings("wren"), {"channel": "telegram", "cloud_daily_usd": 5})

    def test_tool_writers_serialize_read_modify_write(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        real = core._tool_settings
        def read(name):
            result = real(name)
            if threading.current_thread().name == "first-writer":
                entered.set()
                self.assertTrue(release.wait(3))
            return result
        answers = []
        def write(value):
            try:
                core._update_tool_settings("wren", expect={"channel": None}, channel=value)
                answers.append("saved")
            except core.StaleSetting:
                answers.append("stale")
        with mock.patch.object(core, "_tool_settings", side_effect=read):
            first = threading.Thread(target=write, args=("web",), name="first-writer")
            second = threading.Thread(target=write, args=("telegram",))
            first.start()
            try:
                self.assertTrue(entered.wait(3))
                second.start()
            finally:
                release.set()
                first.join(4)
                if second.ident is not None: second.join(4)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertCountEqual(answers, ["saved", "stale"])

    def test_shared_limit_validation_keeps_cli_refusal_wording_and_exit_code(self):
        for limit, value, unit in (("wake-floor", "2.5", "whole number of minutes"),
                                   ("cloud-cap", "-1", "amount")):
            message = f"{limit} must be a positive {unit} (or off), not {value!r}"
            with self.assertRaisesRegex(ValueError, "must be a positive") as raised:
                core.limit_value(limit, value)
            self.assertEqual(str(raised.exception), message)
            self.assertEqual(self.cli("wren", limit, value), (2, message + "\n"))

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
