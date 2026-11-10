"""Native settings and service routes over existing domain writers, with no live services."""
from __future__ import annotations

import contextlib
import io
import json
import threading
from unittest import mock

from supervisor import actions, app_api, autonomy, channel, cloudllm, core, opconfig, runner, version, web_actions
from supervisor.tests.test_app_api import AppTest
from supervisor.tests.test_opconfig import CONFIG, TEMPLATE


class SettingsTest(AppTest):
    def setUp(self):
        super().setUp()
        self.root = self.inst / "wisp"
        self.fake = "fake-secret-never-send-7c9a"
        (self.root / "config.env").write_text(CONFIG.replace("sk-secret-value", self.fake))
        (self.root / "shaft/config.py").write_text(
            'raise RuntimeError("never execute agent code")\n'
            'CHOICES = {\n "LLM_REASONING_EFFORT": ("medium", "xhigh", "low"),\n "OTHER": ("on", "off"),\n}\n')
        self.tmpl = self.inst / "template"
        self.tmpl.mkdir()
        (self.tmpl / "config.env").write_text(TEMPLATE)
        self.command = mock.Mock(side_effect=self.run_command)
        self.guard = mock.Mock(return_value="")
        for p in (mock.patch.object(opconfig, "TEMPLATE", self.tmpl),
                  mock.patch.object(opconfig, "_commit", return_value=False),
                  mock.patch.object(opconfig, "log"),
                  mock.patch.object(autonomy, "SUP_DIR", self.sup),
                  mock.patch.object(runner, "SUP_DIR", self.sup),
                  mock.patch.object(core, "instance_names", return_value=["wisp"]),
                  mock.patch.object(actions, "service_guard", self.guard),
                  mock.patch.object(actions, "run_now", self.command),
                  mock.patch.object(cloudllm, "spent_today", return_value=0.25),
                  mock.patch.object(opconfig, "model_choices", return_value=[
                      {"model": "new-model", "label": "New model", "base_url": self.fake}]),
                  mock.patch.object(opconfig, "base_url_for", side_effect=lambda m: "broker" if m == "new-model" else None)):
            p.start()
            self.addCleanup(p.stop)

    def run_command(self, args, timeout=actions.RUN_NOW_S, who="top"):
        # Exercise the existing command's validation and writer, without spawning the CLI or any service.
        from supervisor import __main__ as cli
        if args[0] == "limits":
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli, "log"), \
                    mock.patch("supervisor.consult.spent_today", return_value=0):
                return cli.cmd_limits(*args[1:]) == 0, "limit result"
        if args[0] == "channel":
            try:
                channel.set_preference(*args[1:])
            except ValueError as e:
                return False, str(e)
        return True, "done"

    def save(self, field, value, expect):
        return self.api("/app/v1/settings/wisp", "POST", {"field": field, "value": value, "expect": expect})

    def test_get_exact_contract_and_no_secret_or_base_url(self):
        r = self.api("/app/v1/settings/wisp")
        self.assertEqual(r.status, 200, r.body)
        got = json.loads(r.body)
        self.assertEqual(got, {
            "instance": "wisp", "paused": False,
            "watch_mail": {"value": "off", "choices": ["off", "inbox", "wake"]},
            "watch_calendar": {"value": "off", "choices": ["off", "inbox", "wake"]},
            "watch_gchat": {"value": "off", "choices": ["off", "inbox", "wake"]},
            "model": {"value": "openrouter/xiaomi/mimo-v2.6-pro", "choices": [
                {"model": "new-model", "label": "New model"},
                {"model": "openrouter/xiaomi/mimo-v2.6-pro", "label": "openrouter/xiaomi/mimo-v2.6-pro"}]},
            "effort": {"value": "medium", "choices": ["medium", "xhigh", "low"]},
            "wake_interval": {"value": 5, "floor": 1, "effective": 5},
            "cloud_cap": {"value": None, "spent_today": 0.25},
            "channel": {"value": "auto", "choices": ["auto", "web", "telegram"]},
            "operator_name": {"value": "the operator"},
            "voice_mode": {"value": "off", "choices": ["on", "off"]},             # not in its config.env: the defaults
            "voice_effort": {"value": "", "choices": ["", "medium", "xhigh", "low"]}})
        self.assertIs(type(got["wake_interval"]["floor"]), int)
        self.assertNotIn(self.fake.encode(), r.body)
        self.assertNotIn(b"base_url", r.body)
        self.assertNotIn(b"LLM_API_KEY", r.body)

    def test_each_config_field_uses_writer_and_reports_when_it_applies(self):
        for field, value, expect, key, applies in (
                ("model", "new-model", "openrouter/xiaomi/mimo-v2.6-pro", "LLM_MODEL", "next run"),
                ("effort", "low", "medium", "LLM_REASONING_EFFORT", "next run"),
                ("operator_name", "Ada", "the operator", "OPERATOR_NAME", "next run"),
                ("wake_interval", 0, 5, "WAKE_INTERVAL_MINUTES", "now"),
                ("watch_mail", "inbox", "off", "WATCH_MAIL", "now"),
                ("watch_calendar", "wake", "off", "WATCH_CALENDAR", "now"),
                ("watch_gchat", "wake", "off", "WATCH_GCHAT", "now")):
            with self.subTest(field=field):
                with mock.patch.object(opconfig, "set_values", wraps=opconfig.set_values) as writer:
                    r = self.save(field, value, expect)
                self.assertEqual(r.status, 200, r.body)
                got = json.loads(r.body)
                self.assertEqual((got["settings"][field]["value"], got["applies"]), (value, applies))
                self.assertEqual(writer.call_args.args[2], {key: None if field.startswith("watch_") else str(expect)})
                self.assertNotIn(self.fake.encode(), r.body)
        self.assertEqual(core.instance_config("wisp")["LLM_BASE_URL"], "broker")
        self.assertEqual(got["settings"]["wake_interval"]["effective"], 0)
        app_api.log.assert_any_call("app iphone-app: operator setting model for wisp")

    def test_effective_wake_uses_scheduler_floor_and_fallback(self):
        core._update_tool_settings("wisp", wake_floor_minutes=20)
        self.assertEqual(self.get("/app/v1/settings/wisp")["wake_interval"],
                         {"value": 5, "floor": 20, "effective": 20})
        (self.root / "config.env").write_text('WAKE_INTERVAL_MINUTES="bad"\n')
        with mock.patch.object(autonomy, "log"):
            got = self.get("/app/v1/settings/wisp")["wake_interval"]
        self.assertEqual(got, {"value": autonomy.WAKE_DEFAULT_MIN, "floor": 20,
                               "effective": max(autonomy.WAKE_DEFAULT_MIN, 20)})

    def test_effort_can_be_unset_and_missing_choices_are_null(self):
        self.assertEqual(json.loads(self.save("effort", None, "medium").body)["settings"]["effort"]["value"], None)
        (self.root / "shaft/config.py").unlink()
        self.assertEqual(self.get("/app/v1/settings/wisp")["effort"], {"value": None, "choices": None})

    def test_tool_settings_save_in_process_and_auto_removes_channel(self):
        for field, value, expect in (("cloud_cap", 2.5, None), ("cloud_cap", None, 2.5),
                                     ("channel", "web", "auto"), ("channel", "auto", "web")):
            r = self.save(field, value, expect)
            self.assertEqual(r.status, 200, r.body)
            self.assertEqual(json.loads(r.body)["settings"][field]["value"], value)
        self.command.assert_not_called()
        self.assertNotIn("channel", core._tool_settings("wisp"))
        app_api.log.assert_any_call("wisp: cloud-cap set to 2.5 by the operator (app iphone-app)")

    def test_cli_write_between_app_read_and_save_is_stale_not_lost(self):
        real = core._update_tool_settings
        for field, key, value, other, expect in (("channel", "channel", "web", "telegram", "auto"),
                                                ("cloud_cap", "cloud_daily_usd", 2.5, 4.0, None)):
            def raced(name, **kwargs):
                real(name, **{key: other})  # the CLI-path writer commits after the app read
                return real(name, **kwargs)
            with mock.patch.object(core, "_update_tool_settings", side_effect=raced):
                err = self.error(self.save(field, value, expect), 409, "stale")
            self.assertEqual(err["settings"][field]["value"], other)
            self.assertEqual(core._tool_settings("wisp")[key], other)

    def test_channel_help_is_refused_instead_of_silent_cli_success(self):
        with mock.patch.object(actions, "run_now", return_value=(True, "usage: shaft channel")) as command:
            self.error(self.save("channel", "--help", "auto"), 409, "refused")
            command.assert_not_called()
        self.assertEqual(channel.preference("wisp"), "auto")

    def test_stale_for_every_field_returns_fresh_settings_without_writing(self):
        for field, value, expect in (("model", "new-model", "old"), ("effort", "low", "xhigh"),
                                     ("wake_interval", 0, 99), ("operator_name", "Ada", "old"),
                                     ("channel", "web", "telegram"), ("cloud_cap", 2.0, 9.0)):
            with self.subTest(field=field), mock.patch.object(opconfig, "set_values") as writer:
                r = self.save(field, value, expect)
                err = self.error(r, 409, "stale")
                self.assertEqual(err["settings"], self.get("/app/v1/settings/wisp"))
                self.assertNotIn(self.fake.encode(), r.body)
                writer.assert_not_called()
        self.command.assert_not_called()

    def test_config_changed_between_read_and_writer_is_stale(self):
        real = opconfig.set_values

        def raced(*args):
            real("wisp", {"LLM_REASONING_EFFORT": "xhigh"})
            return real(*args)
        with mock.patch.object(opconfig, "set_values", side_effect=raced):
            err = self.error(self.save("effort", "low", "medium"), 409, "stale")
        self.assertEqual(err["settings"]["effort"]["value"], "xhigh")

    def test_refused_values_keep_the_writers_message(self):
        for field, value, expect, message in (("effort", "impossible", "medium", "must be one of"),
                                               ("model", "unknown", "openrouter/xiaomi/mimo-v2.6-pro", "not a model"),
                                               ("operator_name", "two\nlines", "the operator", "one line")):
            err = self.error(self.save(field, value, expect), 409, "refused")
            self.assertIn(message, err["message"])
        for field, value, expect in (("channel", "unknown", "auto"), ("cloud_cap", -1, None)):
            self.error(self.save(field, value, expect), 409, "refused")

    def test_voice_settings_round_trip_through_config_env(self):
        """The bug: voice mode and voice effort could not be set from the app, or saved without the
        compare-and-set the other config fields have."""
        for field, value, expect, key, applies in (("voice_mode", "on", "off", "VOICE_MODE", "now"),
                                                   ("voice_effort", "low", "", "VOICE_REASONING_EFFORT", "next run"),
                                                   ("voice_effort", "", "low", "VOICE_REASONING_EFFORT", "next run"),
                                                   ("voice_mode", "off", "on", "VOICE_MODE", "now")):
            with self.subTest(field=field, value=value):
                r = self.save(field, value, expect)
                self.assertEqual(r.status, 200, r.body)
                got = json.loads(r.body)
                self.assertEqual((got["settings"][field]["value"], got["applies"]), (value, applies))
                self.assertEqual(core.instance_config("wisp")[key], value)
        self.assertEqual(self.get("/app/v1/instances")["instances"][0]["voice_mode"], "off")
        self.save("voice_mode", "on", "off")
        self.assertEqual(self.get("/app/v1/instances")["instances"][0]["voice_mode"], "on")

    def test_voice_settings_stale_and_bad_values_are_refused(self):
        """The bug: a voice setting changed elsewhere since the app drew it was overwritten, or a value the talk loop
        cannot use (VOICE_MODE "yes", an effort its code does not accept) was written to config.env."""
        opconfig.set_values("wisp", {"VOICE_MODE": "on"})
        with mock.patch.object(opconfig, "set_values") as writer:
            err = self.error(self.save("voice_mode", "off", "off"), 409, "stale")    # shown off, it is on now
            writer.assert_not_called()
        self.assertEqual(err["settings"]["voice_mode"]["value"], "on")
        self.error(self.save("voice_effort", "low", "medium"), 409, "stale")
        for field, value, expect, message in (("voice_mode", "yes", "on", "on or off"),
                                               ("voice_effort", "turbo", "", "must be empty")):
            err = self.error(self.save(field, value, expect), 409, "refused")
            self.assertIn(message, err["message"])
        self.assertNotIn("VOICE_REASONING_EFFORT", core.instance_config("wisp"))
        for field, value, expect in (("voice_mode", None, "on"), ("voice_mode", True, "on"), ("voice_effort", None, "")):
            self.error(self.save(field, value, expect), 400, "bad_request")

    def test_the_web_config_page_offers_the_voice_keys(self):
        """The bug: the web's instance settings (Config page) had no voice keys until the template documented them,
        or offered a free text box where only on/off or an effort works."""
        fields = {f["key"]: f for f in opconfig.fields("wisp")}
        self.assertEqual((fields["VOICE_MODE"]["choices"], fields["VOICE_MODE"]["default"], fields["VOICE_MODE"]["set"]),
                         (["on", "off"], "off", False))
        self.assertEqual(fields["VOICE_REASONING_EFFORT"]["choices"], ["", "medium", "xhigh", "low"])
        self.assertTrue(fields["VOICE_MODE"]["help"])
        from supervisor import web_config
        row = str(web_config.config_row("wisp", fields["VOICE_MODE"]))
        self.assertIn('<option value="on">on</option>', row)

    def test_wrong_shapes_are_bad_request(self):
        for data in ([], {}, {"field": "model", "value": "new-model"},
                     {"field": [], "value": 1, "expect": 1},
                     {"field": "LLM_API_KEY", "value": "x", "expect": "x"},
                     {"field": "channel", "value": "web", "expect": "auto", "extra": 1}):
            self.error(self.api("/app/v1/settings/wisp", "POST", data), 400, "bad_request")
        for field, value, expect in (("wake_interval", True, 5), ("wake_interval", 1.5, 5),
                                     ("cloud_cap", True, None), ("cloud_cap", float("inf"), None),
                                     ("model", None, "old"), ("operator_name", 2, "the operator"),
                                     ("channel", "web", None), ("effort", [], "medium")):
            self.error(self.save(field, value, expect), 400, "bad_request")
        self.command.assert_not_called()

    def test_pause_and_resume_share_web_action_and_device_logging(self):
        for verb in ("pause", "resume"):
            with mock.patch.object(web_actions, "pause_resume", wraps=web_actions.pause_resume) as shared:
                r = self.api(f"/app/v1/settings/wisp/{verb}", "POST")
            self.assertEqual(r.status, 200, r.body)
            self.assertEqual(json.loads(r.body), {"settings": self.get("/app/v1/settings/wisp")})
            shared.assert_called_once_with(verb, "wisp", who="app iphone-app")
            self.assertEqual(self.command.call_args.args[0], [verb, "wisp"])
        with mock.patch.object(actions, "run_now", return_value=(False, "paused refused")):
            self.error(self.api("/app/v1/settings/wisp/pause", "POST"), 409, "refused")

    def test_routes_require_auth_and_reject_unknown_instances_and_methods(self):
        routes = [("/settings/wisp", "GET"), ("/settings/wisp", "POST"),
                  ("/settings/wisp/pause", "POST"), ("/settings/wisp/resume", "POST"),
                  ("/services", "GET"), ("/services/api/restart", "POST"),
                  ("/services/scheduler/restart", "POST"), ("/services/scheduler/cancel", "POST")]
        for path, method in routes:
            self.error(self.api("/app/v1" + path, method, bearer=False), 401, "unpaired")
            self.error(self.api("/app/v1" + path, method, login="intruder"), 403, "login")
            self.error(self.api("/app/v1" + path, method, host="intruder"), 403, "host")
        self.error(self.api("/app/v1/settings/missing"), 404, "not_found")
        self.error(self.api("/app/v1/settings/wisp", "DELETE"), 405, "method")
        self.command.assert_not_called()

    def test_dead_scheduler_leftover_request_does_not_block_restart(self):
        pending = {"pid": 99999999, "at": 10, "deadline": 100}
        with mock.patch.object(autonomy, "restart_request", return_value=pending), \
                mock.patch.object(autonomy, "drain_status", return_value=None), \
                mock.patch.object(version, "service", return_value=None):
            self.assertIsNone(self.get("/app/v1/services")["services"][1]["drain"])
            self.assertEqual(self.api("/app/v1/services/scheduler/restart", "POST").status, 200)
        self.command.assert_called_once_with(["autonomy", "restart"], actions.RUN_NOW_S, who="app iphone-app")

    def test_web_and_app_restarts_exclude_each_other_in_both_directions(self):
        for service in ("api", "scheduler"):
            form = {"service": service, "op": "on"}
            def web_restart():
                return web_actions.run("service", form, {"services": []}, [], [service])
            def app_restart():
                return self.api(f"/app/v1/services/{service}/restart", "POST")
            for first, second, app_second in ((web_restart, app_restart, True), (app_restart, web_restart, False)):
                def overlap(*args, **kwargs):
                    self.assertEqual(command.call_count, 1, "overlapping service command entered")
                    answer = second()
                    if app_second:
                        self.error(answer, 409, "restarting")
                    else:
                        self.assertEqual(answer, (409, f"{service} is already restarting"))
                    return True, "done"
                with mock.patch.object(actions, "run_now", side_effect=overlap) as command:
                    answer = first()
                    self.assertEqual(answer[0] if isinstance(answer, tuple) else answer.status, 200)
                    self.assertEqual(command.call_count, 1)

    def test_services_only_project_allowed_record_fields(self):
        rec = {"pid": 123, "started": "2026-11-05T10:00:00", "commit": "abc", "describe": "v0.43.0", "settings": {"key": self.fake}}
        drain = {"state": "draining", "waiting": ["wisp live"], "deadline": 1793876400, "secret": self.fake}
        with mock.patch.object(version, "service", side_effect=lambda n, **kw: {"api": rec, "web": {"describe": "v0.44.0"}}.get(n)), \
                mock.patch.object(autonomy, "drain_status", return_value=drain):
            r = self.api("/app/v1/services")
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(json.loads(r.body), {"services": [
            {"name": "api", "running": True, "pid": 123, "started": app_api.stamp(rec["started"]),
             "commit": "abc", "version": "v0.43.0", "busy": ""},
            {"name": "scheduler", "running": False, "pid": None, "started": None, "commit": None, "version": None, "busy": "",
             "drain": {"state": "draining", "waiting": ["wisp live"], "deadline": app_api.stamp(1793876400)}}], "version": "v0.44.0"})
        self.assertNotIn(self.fake.encode(), r.body)

    def test_services_responses_include_server_version(self):
        for rec, expected in (({"describe": "v0.44.0"}, "v0.44.0"), (None, "unknown — restart to record")):
            with mock.patch.object(version, "service", side_effect=lambda n, **kw: rec if n == "web" else None):
                for path, method in (("", "GET"), ("/api/restart", "POST"),
                                     ("/scheduler/restart", "POST"), ("/scheduler/cancel", "POST")):
                    with self.subTest(path=path, record=rec):
                        r = self.api("/app/v1/services" + path, method)
                        self.assertEqual(r.status, 200, r.body)
                        self.assertEqual(json.loads(r.body)["version"], expected)

    def test_service_restart_and_cancel_use_existing_commands_and_timeouts(self):
        for path, args, timeout in (("api/restart", ["api", "on"], actions.SLOW_S),
                                    ("scheduler/restart", ["autonomy", "restart"], actions.RUN_NOW_S),
                                    ("scheduler/cancel", ["autonomy", "restart", "--cancel"], actions.RUN_NOW_S)):
            r = self.api("/app/v1/services/" + path, "POST")
            self.assertEqual(r.status, 200, r.body)
            self.assertEqual(json.loads(r.body), self.get("/app/v1/services"))
            self.command.assert_called_with(args, timeout, who="app iphone-app")
        with mock.patch.object(actions, "run_now", return_value=(False, "cannot restart")):
            self.error(self.api("/app/v1/services/api/restart", "POST"), 409, "refused")

    def test_busy_api_restart_refused_without_force(self):
        self.guard.return_value = "wisp is replying"
        err = self.error(self.api("/app/v1/services/api/restart", "POST", {"force": True}), 409, "busy")
        self.assertEqual(err["message"], "wisp is replying")
        self.command.assert_not_called()
        self.assertEqual(self.get("/app/v1/services")["services"][0]["busy"], "wisp is replying")

    def test_run_lock_for_an_api_reply_makes_existing_service_guard_busy(self):
        # Exercise API._chat's run entry point and its real lock, stubbing only the agent execution.
        def reply(*args, **kw):
            rows = self.get("/app/v1/services")["services"]
            self.assertIn("wisp (api since", rows[0]["busy"])
            self.error(self.api("/app/v1/services/api/restart", "POST"), 409, "busy")
            return 0

        with mock.patch.object(actions, "service_guard", ORIGINAL_GUARD), \
                mock.patch("supervisor.panel.scheduled_runs", return_value=[]), \
                mock.patch.object(runner, "_run", side_effect=reply) as agent:
            self.assertEqual(runner.run_agent("wisp", "api", ["answer"], interactive=True), 0)
            agent.assert_called_once()
            self.assertEqual(self.get("/app/v1/services")["services"][0]["busy"], "")
        self.command.assert_not_called()

    def test_repeated_drain_is_not_requested_again_even_before_first_tick(self):
        req = {"pid": 123, "at": 10, "deadline": 100}
        for record in (None, {**req, "state": "draining", "waiting": ["wisp live"]}):
            with mock.patch.object(version, "service", return_value={"pid": 123, "describe": "v0.44.0"}), \
                    mock.patch.object(autonomy, "restart_request", return_value=req), \
                    mock.patch.object(autonomy, "drain_status", return_value=record):
                r = self.api("/app/v1/services/scheduler/restart", "POST")
                self.assertEqual(r.status, 200, r.body)
                self.assertEqual(json.loads(r.body)["version"], "v0.44.0")
                drain = json.loads(r.body)["services"][1]["drain"]
                self.assertEqual(drain["state"], "draining")
                self.assertEqual(drain["deadline"], app_api.stamp(100))
        self.command.assert_not_called()

    def test_double_restart_of_each_service_is_refused_and_lock_is_released(self):
        for service in ("api", "scheduler"):
            entered, release = threading.Event(), threading.Event()
            answers = []

            def held(*args, **kw):
                entered.set()
                self.assertTrue(release.wait(3))
                return True, "done"
            self.command.side_effect = held
            thread = threading.Thread(target=lambda: answers.append(
                self.api(f"/app/v1/services/{service}/restart", "POST")))
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.error(self.api(f"/app/v1/services/{service}/restart", "POST"), 409, "restarting")
            finally:
                release.set()
                thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual([r.status for r in answers], [200])
            self.command.side_effect = self.run_command
            self.assertEqual(self.api(f"/app/v1/services/{service}/restart", "POST").status, 200)

    def test_other_services_and_api_cancel_are_not_routes(self):
        for path in ("web/restart", "telegram/restart", "model/restart", "embed/restart", "other/restart", "api/cancel"):
            self.error(self.api("/app/v1/services/" + path, "POST"), 404, "not_found")
        self.command.assert_not_called()


ORIGINAL_GUARD = actions.service_guard
