"""The local Bonsai server runs only while something uses it (Daniele, 2026-10-19).

Regression: com.shaft.model ran always (launchd KeepAlive + RunAtLoad), holding ~7.6 GB, while no instance was
configured on it — five days idle, its only user the racer -> local fallback, last fired 2026-10-14. Now it exits
when idle and unpinned, launchd does not restart that exit, and every path that reaches it starts it first."""
from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from supervisor import autonomy, bonsai_server, localmodel

LOCAL = "http://127.0.0.1:8780/v1"


class IdleRuleTest(unittest.TestCase):
    def test_exits_only_when_unpinned_quiet_and_nothing_in_progress(self):
        quiet = 15 * 60
        self.assertTrue(localmodel.idle_exit_due(1000 + quiet, 1000, 0, [], 15))
        self.assertFalse(localmodel.idle_exit_due(1000 + quiet - 1, 1000, 0, [], 15))   # not quiet long enough
        self.assertFalse(localmodel.idle_exit_due(1000 + quiet, 1000, 1, [], 15))       # a reply is being written
        self.assertFalse(localmodel.idle_exit_due(1000 + quiet, 1000, 0, ["kant"], 15)) # an instance is on it
        self.assertFalse(localmodel.idle_exit_due(10 ** 9, 1000, 0, [], 0))            # 0 = never

    def test_pinned_by_the_instances_configured_straight_on_it_paused_or_not(self):
        cfgs = {"a": {"LLM_BASE_URL": LOCAL}, "b": {"LLM_BASE_URL": "http://localhost:8780/v1"},
                "racer": {"LLM_BASE_URL": "broker", "LLM_MODEL": "racer/bonsai2"},   # fallback only: not pinned
                "cloud": {"LLM_BASE_URL": "broker", "LLM_MODEL": "claude-opus-5-5"},
                "other": {"LLM_BASE_URL": "http://127.0.0.1:8781/v1"}, "none": {}}
        self.assertEqual(localmodel.pinned_by(8780, list(cfgs), cfgs.__getitem__), ["a", "b"])
        self.assertEqual(localmodel.pinned_by(8781, list(cfgs), cfgs.__getitem__), ["other"])


class ServerIdleExitTest(unittest.TestCase):
    """bonsai_server.exit_when_idle on a stand-in for mlx_lm.server (the real one needs MLX)."""

    def setUp(self):
        self.now, self.exits, self.pinned = 0.0, [], []
        self.release = None

        class APIHandler:
            def do_POST(handler):
                if self.release:
                    self.release(handler)
                return "replied"

        self.S = NS(APIHandler=APIHandler)
        self.activity, self.check = bonsai_server.exit_when_idle(
            self.S, 8780, idle_minutes=15, exit=self.exits.append, pinned=lambda port: self.pinned,
            start=False, clock=lambda: self.now)

    def test_an_idle_unpinned_server_exits_cleanly_after_the_idle_time(self):
        self.now = 14 * 60
        self.assertFalse(self.check())
        self.now = 15 * 60
        self.assertTrue(self.check())
        self.assertEqual(self.exits, [0])   # 0: launchd's KeepAlive SuccessfulExit=false does not restart it

    def test_a_request_resets_the_clock_and_one_in_progress_keeps_it(self):
        self.now = 10 * 60
        self.assertEqual(self.S.APIHandler().do_POST(), "replied")
        self.now = 20 * 60
        self.assertFalse(self.check())   # 10 min since the last request ended
        seen = []

        def mid_reply(handler):          # a long streamed reply: the decision is taken while it is running
            self.now = 60 * 60
            seen.append((self.activity.in_flight, self.check()))
        self.release = mid_reply
        self.S.APIHandler().do_POST()
        self.assertEqual(seen, [(1, False)])
        self.assertEqual(self.activity.in_flight, 0)
        self.assertEqual(self.exits, [])

    def test_a_pinned_server_never_exits(self):
        self.pinned = ["kant"]
        self.now = 10 ** 6
        self.assertFalse(self.check())
        self.assertEqual(self.exits, [])


class LaunchdTest(unittest.TestCase):
    def setUp(self):
        self.agents = Path(tempfile.mkdtemp(prefix="shaft-agents-"))
        for p in (mock.patch.object(autonomy, "AGENTS", self.agents),
                  mock.patch.object(autonomy, "SUP_DIR", self.agents / "sup"),
                  mock.patch.object(autonomy, "_launchctl", return_value=NS(returncode=0, stdout="", stderr="")),
                  mock.patch.object(autonomy, "log")):
            p.start()
            self.addCleanup(p.stop)

    def plist(self, name):
        with mock.patch("builtins.print"):
            autonomy.service(name, "on")
        return plistlib.loads((self.agents / f"{autonomy.SERVICES[name]['label']}.plist").read_bytes())

    def test_the_model_server_is_not_started_at_login_nor_after_its_idle_exit_but_after_a_crash(self):
        p = self.plist("model")
        self.assertIs(p["RunAtLoad"], False)
        self.assertEqual(p["KeepAlive"], {"SuccessfulExit": False})   # exit 70 (dead generation thread) restarts

    def test_a_bootstrap_refused_while_launchd_lets_go_of_the_old_job_is_retried(self):
        # Regression (2026-10-19): `shaft model on` booted the running server out and bootstrapped at once; launchd
        # answered "Bootstrap failed: 5: Input/output error" and the service was left unloaded — the server down.
        refused = NS(returncode=5, stdout="", stderr="Bootstrap failed: 5: Input/output error")
        ok = NS(returncode=0, stdout="", stderr="")
        calls = []

        def launchctl(*args):
            calls.append(args[0])
            return refused if args[0] == "bootstrap" and calls.count("bootstrap") <= 2 else ok
        with mock.patch.object(autonomy, "_launchctl", side_effect=launchctl), \
                mock.patch.object(autonomy.time, "sleep"), mock.patch("builtins.print"):
            self.assertEqual(autonomy.service("model", "on"), 0)
        self.assertEqual(calls, ["bootout", "bootstrap", "bootstrap", "bootstrap", "kickstart"])

    def test_the_other_services_still_always_run(self):
        for name in ("api", "embed", "scheduler", "telegram"):
            p = self.plist(name)
            self.assertIs(p["RunAtLoad"], True, name)
            self.assertIs(p["KeepAlive"], True, name)


class EnsureTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shaft-ensure-"))
        self.calls = []
        for p in (mock.patch.object(localmodel, "plist", return_value=self.dir / "com.shaft.model.plist"),
                  mock.patch.object(localmodel, "_launchctl", side_effect=self.launchctl),
                  mock.patch.object(localmodel, "log"), mock.patch.object(localmodel.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def launchctl(self, *args):
        self.calls.append(args[0])
        return NS(returncode=0, stdout="", stderr="")

    def test_a_running_server_is_left_alone(self):
        with mock.patch.object(localmodel, "answers", return_value=True):
            localmodel.ensure("kant's live")
        self.assertEqual(self.calls, [])

    def test_an_idle_server_is_started_and_waited_for(self):
        (self.dir / "com.shaft.model.plist").write_text("x")
        with mock.patch.object(localmodel, "answers", side_effect=[False, False, True]):
            localmodel.ensure("kant's live")
        self.assertEqual(self.calls, ["kickstart"])

    def test_not_installed_fails_and_says_how_to_install(self):
        with mock.patch.object(localmodel, "answers", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                localmodel.ensure("kant's live")
        self.assertIn("shaft model on", str(ctx.exception))

    def test_a_server_that_never_answers_fails_instead_of_hanging(self):
        (self.dir / "com.shaft.model.plist").write_text("x")
        with mock.patch.object(localmodel, "answers", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                localmodel.ensure("kant's live", timeout_s=0)
        self.assertIn("did not answer", str(ctx.exception))


class CallersTest(unittest.TestCase):
    """Every path that reaches the local server starts it first."""

    def test_the_racer_fallback_starts_the_local_server(self):
        from supervisor import selfhosted
        opened = object()
        servers = {"racer": {"url": "http://100.120.0.1:8080/v1", "sampling": {}, "think_budget": 0}}
        with mock.patch.object(selfhosted, "SERVERS", servers), mock.patch.object(selfhosted, "FALLBACK", LOCAL), \
                mock.patch.object(selfhosted, "_open", side_effect=[(None, "network: refused"), (opened, None)]), \
                mock.patch.object(localmodel, "ensure") as ensure:
            resp, label, _ = selfhosted.open_reply("shaft", {"model": "racer/bonsai2", "messages": []})
        ensure.assert_called_once()
        self.assertIs(resp, opened)
        self.assertEqual(label, "racer/bonsai2 (local fallback)")

    def test_a_fallback_whose_server_cannot_start_says_why(self):
        from supervisor import cloudllm, selfhosted
        servers = {"racer": {"url": "http://100.120.0.1:8080/v1", "sampling": {}, "think_budget": 0}}
        with mock.patch.object(selfhosted, "SERVERS", servers), mock.patch.object(selfhosted, "FALLBACK", LOCAL), \
                mock.patch.object(selfhosted, "_open", side_effect=[(None, "network: refused")]) as op, \
                mock.patch.object(selfhosted, "_record"), \
                mock.patch.object(localmodel, "ensure", side_effect=RuntimeError("not installed: shaft model on")):
            with self.assertRaises(cloudllm.CloudError) as ctx:
                selfhosted.open_reply("shaft", {"model": "racer/bonsai2", "messages": []})
        self.assertIn("shaft model on", str(ctx.exception))
        self.assertEqual(op.call_count, 1)   # the local server was not tried while down

    def test_a_run_of_an_instance_on_the_local_model_starts_it_and_a_cloud_one_does_not(self):
        from supervisor import runner

        class Stop(Exception):
            pass
        cfgs = {"kant": {"LLM_BASE_URL": LOCAL}, "wren": {"LLM_BASE_URL": "broker", "LLM_MODEL": "claude-opus-5-5"}}
        with mock.patch.object(runner, "instance_config", side_effect=cfgs.__getitem__), \
                mock.patch.object(runner, "Broker", side_effect=Stop), \
                mock.patch.object(localmodel, "ensure") as ensure:
            for name in cfgs:
                with self.assertRaises(Stop):
                    runner._launch(name, Path("/nonexistent"), "live", ["live"], None)
        ensure.assert_called_once_with("kant's live")


class StatusTest(unittest.TestCase):
    def test_an_idle_model_server_is_not_an_outage(self):
        from supervisor import dashboard, panel
        with mock.patch("supervisor.version.service", return_value=None), \
                mock.patch.object(localmodel, "plist", return_value=Path(__file__)), \
                mock.patch.object(dashboard, "_scheduler_running", return_value=True):
            svcs = {s["name"]: s for s in dashboard.services()}
        self.assertTrue(svcs["model"]["idle"])
        self.assertIn("starts on demand", svcs["model"]["detail"])
        self.assertFalse(svcs["api"]["idle"])   # only the model server runs on demand
        alerts = panel.alerts({"services": list(svcs.values())}, cost={"caps": [], "judge_today_total": 0, "judge_alert": 10 ** 9})
        texts = " ".join(a["text"] for a in alerts)
        self.assertNotIn("service model", texts)

    def test_an_uninstalled_model_server_is_still_an_outage(self):
        from supervisor import dashboard
        with mock.patch("supervisor.version.service", return_value=None), \
                mock.patch.object(localmodel, "plist", return_value=Path("/nonexistent.plist")), \
                mock.patch.object(dashboard, "_scheduler_running", return_value=True):
            svcs = {s["name"]: s for s in dashboard.services()}
        self.assertFalse(svcs["model"]["idle"])


if __name__ == "__main__":
    unittest.main()
