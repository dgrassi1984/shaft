"""The web UI's actions (PLAN-webui.md M2): each runs the exact CLI subcommand `shaft top` runs for it (the panel's
whole authority), logged as the web's, and is refused — before anything runs — where the TUI or its CLI refuses it."""
from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import actions, panel, recap, web, web_actions
from supervisor.tests.test_web import WebTest, snap


class ActionsTest(WebTest):
    def setUp(self):
        super().setUp()
        self.ran, self.result = [], (True, "ok")
        for p in (mock.patch.object(actions, "run_now", self._now), mock.patch.object(actions, "run_detached", self._bg),
                  mock.patch.object(web, "_names", return_value=["shaft", "wren"]),
                  mock.patch.object(actions, "busy_instances", return_value=[]),
                  mock.patch.object(panel, "scheduled_runs", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        self.cookie = self.paired()

    def _now(self, args, timeout=60, who="top"):
        self.ran.append((who, args, timeout))
        return self.result

    def _bg(self, args, who="top"):
        self.ran.append((who, ["detached", *args], None))
        return "started"

    LOG_ID = "log:2026-10-19 09:48:34:2373c79a19f2"           # a real log alert's id: its time has a space

    def act(self, verb: str, state: str = "idle", **form) -> tuple[int, str]:
        s = snap(state=state)
        s["alerts"] = [{"id": "a1", "text": "one"}, {"id": "a2", "text": "two"}, {"id": self.LOG_ID, "text": "log"}]
        body = "&".join(f"{k}={v}" for k, v in form.items()).encode()
        with mock.patch.object(web, "overview", return_value=s):
            r = self.req(f"/action/{verb}", "POST", cookie=self.cookie, body=body,
                         headers={"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(self.cookie)})
        return r.status, r.body.decode()

    def assertRan(self, args, timeout=60):
        self.assertEqual(self.ran, [("web", args, timeout)])

    def assertNothingRan(self):
        self.assertEqual(self.ran, [])


class EveryActionRunsItsCommand(ActionsTest):
    def test_cycle(self):
        self.assertEqual(self.act("cycle", name="wren")[0], 200)
        self.assertRan(["cycle", "wren"])

    def test_pause_and_resume(self):
        self.act("pause", name="wren")
        self.act("resume", name="wren", state="paused by you")
        self.assertEqual([r[1] for r in self.ran], [["pause", "wren"], ["resume", "wren"]])

    def test_sleep_is_detached(self):
        self.act("sleep", name="wren")
        self.assertRan(["detached", "sleep", "wren"], None)

    def test_update_is_detached(self):
        with mock.patch.object(actions, "update_question", return_value=("merge 2?", "")):
            self.act("update", name="wren")
        self.assertRan(["detached", "update", "wren"], None)

    def test_send_and_reply_carry_the_text_as_typed(self):
        self.act("send", name="wren", text="hello%20there%2C%20%3Cfriend%3E")
        self.act("reply", name="wren", text="yes")
        self.assertEqual([r[1] for r in self.ran], [["send", "wren", "hello there, <friend>"], ["reply", "wren", "yes"]])

    def test_tool_switch(self):
        with mock.patch("supervisor.broker.SPECS", {"delegate_coding": {}}):
            self.act("tool", name="wren", tool="delegate_coding", to="off")
        self.assertRan(["tools", "wren", "off", "delegate_coding"])

    def test_aa_trial(self):
        self.act("aa-trial", name="wren")
        self.assertRan(["trial", "run", "wren", "--aa", "--hypothesis", "A/A calibration (from the web UI)"])

    def test_battery_is_detached(self):
        with mock.patch.object(actions, "battery_refusal", return_value=""):
            self.act("battery", name="wren")
        self.assertRan(["detached", "battery", "run", "wren"], None)

    def test_recap_is_detached(self):
        self.act("recap")
        self.assertRan(["detached", "recap"], None)

    def test_dismiss_the_alerts_shown_including_a_log_alert_whose_id_has_a_space(self):
        """Live, 2026-10-19: the ids went space-separated, a log alert's id split in two, and every dismiss was
        refused as "not open any more"."""
        import json as _json
        import urllib.parse as up
        from supervisor import web_actions
        form = str(web_actions.alert_buttons([{"id": "a1"}, {"id": self.LOG_ID}]))
        value = form.split('name="ids" value="')[1].split('"')[0]
        import html as _html
        ids = _html.unescape(value)
        self.assertEqual(_json.loads(ids), ["a1", self.LOG_ID])          # what the page posts
        self.assertEqual(self.act("dismiss", ids=up.quote(ids))[0], 200)
        self.assertRan(["alerts", "dismiss", "a1", self.LOG_ID])

    def test_services_and_autonomy(self):
        self.act("service", service="model", op="on")
        self.act("service", service="scheduler", op="off")
        self.act("autonomy", op="on")
        self.assertEqual([(r[1], r[2]) for r in self.ran],
                         [(["model", "on"], 90), (["autonomy", "off"], 90), (["autonomy", "on"], 90)])


class RefusedWhereTheTuiRefuses(ActionsTest):
    def refused(self, verb: str, why: str, **form):
        status, body = self.act(verb, **form)
        self.assertEqual(status, 409, body)
        self.assertIn(why, body)
        self.assertNothingRan()

    def test_a_cycle_or_a_sleep_only_when_idle(self):
        self.refused("cycle", "a cycle starts only when it is idle", name="wren", state="in a cycle")
        self.refused("sleep", "a sleep starts only when it is idle", name="wren", state="talking")

    def test_update_with_nothing_to_merge(self):
        with mock.patch.object(actions, "update_question", return_value=("", "wren is already on the current template")):
            self.refused("update", "already on the current template", name="wren")

    def test_an_empty_or_oversized_message(self):
        self.refused("send", "empty message", name="wren", text="%20%20")
        self.refused("send", "at most", name="wren", text="x" * (web_actions.TEXT_MAX + 1))

    def test_a_tool_that_does_not_exist(self):
        with mock.patch("supervisor.broker.SPECS", {"delegate_coding": {}}):
            self.refused("tool", "no tool switch", name="wren", tool="rm_rf", to="off")
            self.refused("tool", "no tool switch", name="wren", tool="delegate_coding", to="maybe")

    def test_the_battery_when_off_or_not_installed(self):
        with mock.patch.object(actions, "battery_refusal", return_value="the battery is off for wren"):
            self.refused("battery", "the battery is off", name="wren")

    def test_alerts_no_longer_open(self):
        self.refused("dismiss", "not open any more", ids="%5B%22gone%22%5D")
        self.refused("dismiss", "not open any more", ids="")
        self.refused("dismiss", "not open any more", ids="not-json")

    def test_a_scheduler_restart_while_its_runs_go_on(self):
        with mock.patch.object(panel, "scheduled_runs", return_value=["wren (live since 12:00)"]):
            self.refused("service", "would kill them: wren (live since 12:00)", service="scheduler", op="on")
            self.refused("autonomy", "would kill them", op="off")

    def test_a_model_restart_while_runs_use_it(self):
        with mock.patch.object(actions, "busy_instances", return_value=["shaft (talk since 16:18)"]):
            self.refused("service", "runs in progress use it: shaft (talk since 16:18)", service="model", op="on")

    def test_the_web_service_from_the_web(self):
        self.refused("service", "cannot stop or restart itself", service="web", op="off")

    def test_unknown_instances_services_and_verbs(self):
        self.refused("pause", "no instance", name="nobody")
        self.refused("service", "no service action", service="postgres", op="on")
        self.refused("frobnicate", "no action", name="wren")

    def test_the_cli_refusing_is_a_refusal(self):
        self.result = (False, "wren is busy (live since 12:00)")
        status, body = self.act("pause", name="wren")
        self.assertEqual(status, 409)
        self.assertIn("refused: wren is busy", body)

    def test_no_action_without_the_page_token(self):
        r = self.req("/action/pause", "POST", cookie=self.cookie, body=b"name=wren",
                     headers={"sec-fetch-site": "same-origin"})
        self.assertEqual(r.status, 403)
        self.assertNothingRan()


class PagesShowEachActionAsItRuns(ActionsTest):
    def page(self, path: str, s: dict | None = None) -> str:
        with mock.patch.object(web, "overview", return_value=s or snap()):
            r = self.req(path, cookie=self.cookie)
        self.assertEqual(r.status, 200, r.body[:300])
        return r.body.decode()

    def test_the_overview_asks_before_a_cycle_and_explains_when_it_cannot(self):
        idle = self.page("/fragment/overview")
        self.assertIn('data-post="/action/cycle"', idle)
        self.assertIn('data-confirm="Start a cycle for wren now?"', idle)
        busy = self.page("/fragment/overview", snap(state="in a cycle"))
        self.assertIn('disabled title="wren is in a cycle — a cycle starts only when it is idle"', busy)
        self.assertNotIn('data-post="/action/cycle"', busy)

    def test_paused_shows_resume(self):
        self.assertIn('data-post="/action/resume"', self.page("/fragment/overview", snap(state="paused by you")))

    def test_the_trials_page_states_the_cost_first(self):
        with mock.patch.object(panel, "trials", return_value=[{"status": "done", "verdict": {"usd": 2.0}}]), \
                mock.patch.object(panel, "costs", return_value={"today": {"trials": 5.0}, "caps": []}):
            body = self.page("/trials")
        self.assertIn("about $2.00 by the finished trials so far", body)
        self.assertIn("trials today $5.00", body)

    def test_the_services_page_refuses_a_restart_that_would_kill_runs(self):
        with mock.patch.object(panel, "services_detail", return_value=[
                {"name": "scheduler", "running": True, "detail": ""}, {"name": "web", "running": True, "detail": ""}]), \
                mock.patch.object(panel, "scheduled_runs", return_value=["wren (live since 12:00)"]), \
                mock.patch.object(panel, "log_tail", return_value=[]):
            body = self.page("/services")
        self.assertIn("not now: the scheduler&#x27;s runs share its process group", body)
        self.assertIn("the web UI cannot stop or restart itself", body)


class RecapLockTest(unittest.TestCase):
    """`shaft top`, the web UI and `shaft recap` share one lock per instance: a recap is never paid for twice."""

    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-recap-lock-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        p = mock.patch.object(recap, "SUP_DIR", self.sup)
        p.start()
        self.addCleanup(p.stop)

    def test_a_recap_being_written_elsewhere_is_not_started_again(self):
        path = recap._lock_path("wren")
        path.parent.mkdir(parents=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR)        # another process's lock (flock: per open file)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        self.assertTrue(recap.running("wren"))
        with mock.patch.object(recap, "write") as write:
            self.assertEqual(recap.start([{"name": "wren"}], wait=True), "a recap is already being written")
        write.assert_not_called()

    def test_the_lock_is_released_when_the_recap_is_written(self):
        with mock.patch.object(recap, "write") as write:
            recap.start([{"name": "wren"}], wait=True)
        write.assert_called_once()
        self.assertFalse(recap.running("wren"))


if __name__ == "__main__":
    unittest.main()


class NewVerbsTest(unittest.TestCase):
    """The CLI verbs the panels' in-process actions became (PLAN-webui.md M2): shaft cycle, recap, alerts."""

    def cli(self, *argv) -> tuple[int, str]:
        import contextlib
        import io
        from supervisor import __main__ as cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(cli, "instance_root", return_value=Path("/tmp")):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_cycle_starts_through_start_wake_and_says_why_not(self):
        from supervisor import autonomy
        with mock.patch.object(autonomy, "start_wake", return_value="started a cycle for wren") as sw:
            self.assertEqual(self.cli("cycle", "wren"), (0, "started a cycle for wren\n"))
        sw.assert_called_once_with("wren")
        with mock.patch.object(autonomy, "start_wake", side_effect=RuntimeError("wren is already awake (live)")):
            code, out = self.cli("cycle", "wren")
        self.assertEqual(code, 1)
        self.assertIn("not started: wren is already awake", out)

    def test_alerts_lists_and_dismisses(self):
        with mock.patch.object(panel, "extras", side_effect=lambda s: {**s, "alerts": [{"id": "a1", "text": "one"}]}), \
                mock.patch("supervisor.dashboard.services", return_value=[]):
            self.assertEqual(self.cli("alerts"), (0, "a1  one\n"))
        with mock.patch.object(panel, "dismiss") as dismiss:
            self.assertEqual(self.cli("alerts", "dismiss", "a1", "a2")[0], 0)
        dismiss.assert_called_once_with(["a1", "a2"])
        self.assertEqual(self.cli("alerts", "dismiss")[0], 1)

    def test_recap_writes_and_prints_each_or_says_it_failed(self):
        with mock.patch("supervisor.core.instance_names", return_value=["wren"]), \
                mock.patch("supervisor.dashboard.instance_state", return_value={"name": "wren"}), \
                mock.patch.object(recap, "start", return_value="recapping wren…") as start, \
                mock.patch.object(recap, "load", return_value={"wren": {"ts": "2026-10-19T12:00:00", "text": "tidying",
                                                                        "error": "HTTP 529",
                                                                        "error_ts": "2026-10-19T12:01:00"}}):
            code, out = self.cli("recap")
        start.assert_called_once_with([{"name": "wren"}], wait=True)
        self.assertEqual(code, 1)
        self.assertIn("wren: recap failed: HTTP 529", out)


class OneBrokenInstanceTest(unittest.TestCase):
    def test_an_update_check_that_raises_disables_only_that_button(self):
        """Live, 2026-10-19: version.instance raised SystemExit for one instance and the whole overview failed."""
        web_actions._updates.clear()
        with mock.patch.object(actions, "update_question", side_effect=SystemExit("kant is not a registered worktree")):
            out = web_actions.instance_buttons({"name": "kant", "state": "idle"})
        self.assertIn('disabled title="cannot tell whether kant is behind the template: kant is not a registered', out)
        self.assertIn('data-post="/action/cycle"', out)                 # the other actions are still there
        web_actions._updates.clear()
