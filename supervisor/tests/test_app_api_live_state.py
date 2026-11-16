"""Sidebar and plan polling must not rebuild the dashboard."""
from contextlib import ExitStack
from unittest import mock

from supervisor import autonomy, dashboard, runner, web
from supervisor.dashboard import live_state
from supervisor.tests.test_app_api import AppTest


class LiveStateTest(AppTest):
    def setUp(self):
        super().setUp()
        self.cycles_scenario()
        stack = self.enterContext(ExitStack())
        for patch in (
            mock.patch.object(dashboard, "live_state", wraps=live_state),
            mock.patch.object(dashboard, "instance_root", return_value=self.inst / "wisp"),
            mock.patch.object(dashboard, "instance_config", return_value={
                "LLM_BASE_URL": "broker", "LLM_MODEL": "openrouter/example/model"}),
            mock.patch.object(runner, "busy_with", return_value="live since 18:06"),
            mock.patch.object(runner, "open_conversations", return_value={}),
            mock.patch.object(dashboard, "activity", return_value={"goal": "Check totals"}),
            mock.patch.object(dashboard, "paused", return_value=False),
            mock.patch.object(dashboard, "_usd_today", return_value=1.25),
            mock.patch.object(dashboard, "_today_usage", return_value=(0, 0)),
            mock.patch.object(dashboard, "_judgments", return_value=[]),
            mock.patch.object(dashboard, "_trial_line", return_value=""),
            mock.patch.object(dashboard, "disabled_tools", return_value=[]),
        ):
            stack.enter_context(patch)
        self.expected = dashboard.instance_state("wisp")
        self.assertEqual({k: self.expected[k] for k in ("state", "since", "goal", "model", "usd_today")},
                         {"state": "in a cycle", "since": "18:06", "goal": "Check totals",
                          "model": "cloud model", "usd_today": 1.25})
        for obj, key in ((web, "overview"), (dashboard, "snapshot")):
            stack.enter_context(mock.patch.object(obj, key, side_effect=AssertionError("dashboard read")))

    def test_instances_poll_matches_instance_state_without_dashboard(self):
        row = self.get("/app/v1/instances")["instances"][0]
        self.assertEqual({k: row[k] for k in ("state", "since", "model", "usd_today")},
                         {k: self.expected[k] for k in ("state", "since", "model", "usd_today")})

    def test_plan_poll_matches_instance_state_without_dashboard(self):
        self.assertEqual(self.get("/app/v1/plan/wisp")["now"],
                         {k: self.expected[k] for k in ("state", "since", "goal")})

    def test_live_state_does_not_read_history_or_schedule_wakes(self):
        with ExitStack() as stack:
            for obj, keys in ((dashboard, ("_today_usage", "_judgments", "_trial_line")),
                              (autonomy, ("wake_interval", "next_wake", "last_skip"))):
                for key in keys:
                    stack.enter_context(mock.patch.object(obj, key, side_effect=AssertionError(key)))
            state = dashboard.live_state("wisp")
        self.assertEqual(state, {k: self.expected[k] for k in state})
