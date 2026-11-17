"""Sidebar and plan polling must not rebuild the dashboard."""
from contextlib import ExitStack
from unittest import mock

from supervisor import app_api, autonomy, core, dashboard, runner, web, web_chat, web_seen
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


class PausedSidebarTest(AppTest):
    def setUp(self):
        super().setUp()
        for obj, key, kwargs in (
            (web, "_names", {"return_value": ["wisp", "example"]}),
            (core, "instance_config", {"return_value": {}}),
            (web_chat, "summary", {"return_value": {}}),
            (web_chat, "threads", {"return_value": []}),
            (web_chat, "open_chats", {"return_value": []}),
            (web_seen, "unread_conversations", {"side_effect": lambda device, n: {"from": n == "example"}}),
        ):
            self.enterContext(mock.patch.object(obj, key, **kwargs))

    def test_paused_instances_showed_in_the_sidebar(self):
        flag = self.sup / "paused-example"
        flag.write_text("2026-11-03T14:02:11")
        response = self.api("/app/v1/instances")
        answer = self.get("/app/v1/instances")
        self.assertEqual([row["name"] for row in answer["instances"]], ["wisp"])
        self.assertEqual(answer["paused"], [{"name": "example", "since": app_api.stamp("2026-11-03T14:02:11")}])
        self.assertEqual(answer["badge"], 0)
        self.assertEqual(self.api("/app/v1/instances", headers={"if-none-match": response.headers["ETag"]}).status, 304)
        flag.unlink()
        self.assertEqual(self.api("/app/v1/instances", headers={"if-none-match": response.headers["ETag"]}).status, 200)
        self.assertEqual(self.get("/app/v1/instances")["badge"], 1)

    def test_paused_since_falls_back_to_mtime_and_keeps_server_order(self):
        for name in ("wisp", "example"):
            (self.sup / f"paused-{name}").write_text("old invalid timestamp")
        answer = self.get("/app/v1/instances")
        self.assertEqual(answer["instances"], [])
        self.assertEqual(answer["paused"], [
            {"name": n, "since": app_api.stamp((self.sup / f"paused-{n}").stat().st_mtime)}
            for n in ("wisp", "example")])

    def test_global_pause_keeps_all_instances_visible(self):
        (self.sup / "paused").touch()
        with mock.patch.object(dashboard, "live_state", return_value={"state": "cycle paused for a conversation"}):
            answer = self.get("/app/v1/instances")
        self.assertEqual([row["name"] for row in answer["instances"]], ["wisp", "example"])
        self.assertEqual(answer["paused"], [])
        self.assertEqual(answer["badge"], 1)
