"""The web UI's actions: each runs the exact CLI subcommand `shaft top` runs for it (the panel's
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


def actions_services() -> list[str]:
    from supervisor import dashboard
    return list(dashboard.SERVICES)


class ActionsTest(WebTest):
    def setUp(self):
        super().setUp()
        self.ran, self.result = [], (True, "ok")
        self.running = [n for n in actions_services() if n != "web"]      # the guards apply to a running service
        for p in (mock.patch.object(actions, "run_now", self._now), mock.patch.object(actions, "run_detached", self._bg),
                  mock.patch.object(web, "_names", return_value=["shaft", "wisp"]),
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

    def act(self, verb: str, state: str = "idle", alerts: list[dict] | None = None, **form) -> tuple[int, str]:
        s = snap(state=state)
        s["services"] = [{"name": n, "running": n in self.running, "detail": ""} for n in actions_services()]
        s["alerts"] = alerts or [{"id": "a1", "text": "one"}, {"id": "a2", "text": "two"},
                                 {"id": self.LOG_ID, "text": "log"}]
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
        self.assertEqual(self.act("cycle", name="wisp")[0], 200)
        self.assertRan(["cycle", "wisp"])

    def test_pause_and_resume(self):
        self.act("pause", name="wisp")
        self.act("resume", name="wisp", state="paused by you")
        self.assertEqual([r[1] for r in self.ran], [["pause", "wisp"], ["resume", "wisp"]])

    def test_kill_runs_the_cli_verb_and_a_slow_settle_is_no_refusal(self):
        self.result = (False, "wisp paused; stopped shaft live; after 20s it is still settling: live since 12:00")
        status, body = self.act("kill", name="wisp", state="live since 12:00")
        self.assertEqual(status, 200)
        self.assertIn("still settling", body)
        self.assertRan(["kill", "wisp", "--wait", str(web_actions.KILL_WAIT_S)], web_actions.KILL_WAIT_S + 8)

    def test_delete_needs_the_name_typed_and_an_instance_that_runs_nothing(self):
        from supervisor import kill
        with mock.patch.object(kill, "running", return_value=[]):
            self.assertEqual(self.act("delete", name="wisp")[0], 409)                 # no typed name
            self.assertEqual(self.act("delete", name="wisp", confirm="wre")[0], 409)  # a typo
            self.assertNothingRan()
            self.assertEqual(self.act("delete", name="wisp", confirm="wisp")[0], 200)
        self.assertRan(["detached", "delete", "wisp", "--yes"], None)
        self.ran.clear()
        with mock.patch.object(kill, "running", return_value=["live since 12:00"]):
            status, body = self.act("delete", name="wisp", confirm="wisp")
        self.assertEqual(status, 409)
        self.assertIn("kill it first", body)
        self.assertNothingRan()

    def test_delete_is_offered_on_the_instance_page_only_and_typed(self):
        page = str(web_actions.instance_buttons(snap()["instances"][0], delete=True))
        self.assertIn('data-post="/action/delete"', page)
        self.assertIn('data-confirm-type="wisp"', page)
        self.assertIn('data-after="home"', page)
        self.assertIn('data-post="/action/kill"', page)
        card = str(web_actions.instance_buttons(snap()["instances"][0]))
        self.assertNotIn("/action/delete", card)
        self.assertIn("/action/kill", card)
        busy = str(web_actions.instance_buttons(snap(state="live since 12:00")["instances"][0], delete=True))
        self.assertNotIn('data-post="/action/delete"', busy)                     # off, with why
        self.assertIn("kill it first", busy)

    def test_sleep_is_detached(self):
        self.act("sleep", name="wisp")
        self.assertRan(["detached", "sleep", "wisp"], None)

    def test_update_is_detached(self):
        with mock.patch.object(actions, "update_question", return_value=("merge 2?", "")):
            self.act("update", name="wisp")
        self.assertRan(["detached", "update", "wisp"], None)

    def test_send_and_reply_carry_the_text_as_typed(self):
        self.act("send", name="wisp", text="hello%20there%2C%20%3Cfriend%3E")
        self.act("reply", name="wisp", text="yes")
        self.assertEqual([r[1] for r in self.ran], [["send", "wisp", "hello there, <friend>"], ["reply", "wisp", "yes"]])

    def test_tool_switch(self):
        with mock.patch("supervisor.broker.SPECS", {"delegate_coding": {}}):
            self.act("tool", name="wisp", tool="delegate_coding", to="off")
        self.assertRan(["tools", "wisp", "off", "delegate_coding"])

    def test_aa_trial(self):
        self.act("aa-trial", name="wisp")
        self.assertRan(["trial", "run", "wisp", "--aa", "--hypothesis", "A/A calibration (from the web UI)"])

    def test_battery_is_detached(self):
        with mock.patch.object(actions, "battery_refusal", return_value=""):
            self.act("battery", name="wisp")
        self.assertRan(["detached", "battery", "run", "wisp"], None)

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

    def test_dismiss_all_when_more_are_open_than_shown(self):
        """Live, 2026-10-20: 60 lookalike log alerts took 12 clicks of "Dismiss these alerts", five at a time, and the
        list never seemed to change. With more than five open, one more button dismisses all, asked first."""
        import html as _html
        import json as _json
        import urllib.parse as up
        from supervisor import web_actions
        few = [{"id": f"a{i}"} for i in range(5)]
        self.assertNotIn("Dismiss all", str(web_actions.alert_buttons(few)))        # all on screen: one button
        many = [{"id": f"a{i}"} for i in range(7)] + [{"id": self.LOG_ID}]
        form = str(web_actions.alert_buttons(many))
        self.assertIn("Dismiss these alerts", form)
        self.assertIn("Dismiss all 8", form)
        self.assertIn("the 3 not shown too?", form)                                  # asked: it reaches unseen ones
        posted = [_json.loads(_html.unescape(v.split('"')[0])) for v in form.split('name="ids" value="')[1:]]
        self.assertEqual(posted, [[f"a{i}" for i in range(5)], [a["id"] for a in many]])
        self.assertEqual(self.act("dismiss", alerts=many, ids=up.quote(_json.dumps(posted[1])))[0], 200)
        self.assertRan(["alerts", "dismiss", *[a["id"] for a in many]])

    def test_issue_34_one_alert_dismisses_on_its_own(self):
        """#34. Each alert's Dismiss posts a JSON list of that one id — the same identity `alerts dismiss` takes,
        including a log id with a space and one that is not among the five on view. Reverting to the batch buttons
        only, this post never happens: the page has no single-id Dismiss for a7."""
        import html as _html
        import json as _json
        import urllib.parse as up
        from supervisor import web_pages
        many = [{"id": f"a{i}", "text": f"alert {i}"} for i in range(7)] + [{"id": self.LOG_ID, "text": "a log line"}]
        s = snap()
        s["alerts"] = many
        out = str(web_pages.overview(s))
        posted = [_json.loads(_html.unescape(v.split('"')[0])) for v in out.split('name="ids" value="')[1:]]
        self.assertIn(["a6"], posted)                             # past the five on view
        self.assertIn([self.LOG_ID], posted)                      # the space stays inside the one id
        self.ran.clear()
        self.assertEqual(self.act("dismiss", alerts=many, ids=up.quote(_json.dumps(["a6"])))[0], 200)
        self.assertRan(["alerts", "dismiss", "a6"])

    def test_services_and_autonomy(self):
        self.act("service", service="model", op="on")
        self.act("service", service="scheduler", op="off")
        self.act("autonomy", op="on")
        self.assertEqual([(r[1], r[2]) for r in self.ran],
                         [(["model", "on"], actions.SLOW_S), (["autonomy", "off"], actions.SLOW_S),
                          (["autonomy", "on"], actions.SLOW_S)])


class RefusedWhereTheTuiRefuses(ActionsTest):
    def refused(self, verb: str, why: str, **form):
        status, body = self.act(verb, **form)
        self.assertEqual(status, 409, body)
        self.assertIn(why, body)
        self.assertNothingRan()

    def test_a_cycle_or_a_sleep_only_when_idle(self):
        self.refused("cycle", "a cycle starts only when it is idle", name="wisp", state="in a cycle")
        self.refused("sleep", "a sleep starts only when it is idle", name="wisp", state="talking")

    def test_update_with_nothing_to_merge(self):
        with mock.patch.object(actions, "update_question", return_value=("", "wisp is already on the current template")):
            self.refused("update", "already on the current template", name="wisp")

    def test_an_empty_or_oversized_message(self):
        self.refused("send", "empty message", name="wisp", text="%20%20")
        self.refused("send", "at most", name="wisp", text="x" * (web_actions.TEXT_MAX + 1))

    def test_a_tool_that_does_not_exist(self):
        with mock.patch("supervisor.broker.SPECS", {"delegate_coding": {}}):
            self.refused("tool", "no tool switch", name="wisp", tool="rm_rf", to="off")
            self.refused("tool", "no tool switch", name="wisp", tool="delegate_coding", to="maybe")

    def test_the_battery_when_off_or_not_installed(self):
        with mock.patch.object(actions, "battery_refusal", return_value="the battery is off for wisp"):
            self.refused("battery", "the battery is off", name="wisp")

    def test_alerts_no_longer_open(self):
        self.refused("dismiss", "not open any more", ids="%5B%22gone%22%5D")
        self.refused("dismiss", "not open any more", ids="")
        self.refused("dismiss", "not open any more", ids="not-json")

    def test_a_scheduler_restart_while_its_runs_go_on(self):
        with mock.patch.object(panel, "scheduled_runs", return_value=["wisp (live since 12:00)"]):
            self.refused("service", "would kill them: wisp (live since 12:00)", service="scheduler", op="on")
            self.refused("autonomy", "would kill them", op="off")

    def test_a_model_restart_while_runs_use_it(self):
        with mock.patch.object(actions, "busy_instances", return_value=["shaft (talk since 16:18)"]):
            self.refused("service", "runs in progress use it: shaft (talk since 16:18)", service="model", op="on")

    def test_the_web_service_from_the_web(self):
        self.refused("service", "cannot stop or restart itself", service="web", op="off")

    def test_unknown_instances_services_and_verbs(self):
        self.refused("pause", "no instance", name="nobody")
        self.refused("service", "no service action", service="postgres", op="on")
        self.refused("frobnicate", "no action", name="wisp")

    def test_the_cli_refusing_is_a_refusal(self):
        self.result = (False, "wisp is busy (live since 12:00)")
        status, body = self.act("pause", name="wisp")
        self.assertEqual(status, 409)
        self.assertIn("refused: wisp is busy", body)

    def test_no_action_without_the_page_token(self):
        r = self.req("/action/pause", "POST", cookie=self.cookie, body=b"name=wisp",
                     headers={"sec-fetch-site": "same-origin"})
        self.assertEqual(r.status, 403)
        self.assertNothingRan()


class AGuardTheOperatorForces(ActionsTest):
    """Live, 2026-10-20: two live cycles (tundra, wisp) kept Restart api and Restart scheduler greyed out on Settings
    and Services, with no way to restart from the page. A guarded action is refused unless forced; forced, it runs and
    the log names what it overrode."""

    def setUp(self):
        super().setUp()
        runs = ["tundra (live since 14:06)", "wisp (live since 14:08)"]
        for p in (mock.patch.object(panel, "scheduled_runs", return_value=runs),
                  mock.patch.object(actions, "busy_instances", return_value=runs)):
            p.start()
            self.addCleanup(p.stop)
        self.logged = []
        p = mock.patch.object(actions, "log", self.logged.append)
        p.start()
        self.addCleanup(p.stop)

    def test_unforced_it_is_refused(self):
        for verb, form in (("service", {"service": "scheduler", "op": "on"}), ("service", {"service": "api", "op": "on"}),
                           ("service", {"service": "api", "op": "off"}), ("autonomy", {"op": "off"}),
                           ("autonomy", {"op": "on"})):   # on restarts a running scheduler (2026-10-27)
            status, body = self.act(verb, **form)
            self.assertEqual(status, 409, body)
            self.assertIn("not now:", body)
        self.assertNothingRan()
        self.assertEqual(self.logged, [])

    def test_forced_it_runs_and_is_logged(self):
        self.assertEqual(self.act("service", service="scheduler", op="on", force="1")[0], 200)
        with mock.patch("supervisor.autonomy.installed", return_value=True):
            self.assertEqual(self.act("service", service="api", op="on", force="1")[0], 200)
        self.assertEqual(self.act("autonomy", op="off", force="1")[0], 200)
        self.assertEqual([r[1] for r in self.ran],   # the CLI guards them too: forced, they say so
                         [["autonomy", "on", "--force"], ["api", "restart", "--force"], ["autonomy", "off", "--force"]])
        self.assertEqual(len(self.logged), 3)
        self.assertIn("web: operator forced shaft autonomy on past the guard: the scheduler's runs", self.logged[0])
        self.assertIn("tundra (live since 14:06), wisp (live since 14:08)", self.logged[0])
        self.assertNotIn("Try again", self.logged[0])

    def test_a_stopped_service_starts_unforced_while_runs_are_in_progress(self):
        """Live, 2026-10-20: the model server, stopped between uses, showed "Force start", asking to "force restart
        model" — starting it cuts nothing off, and the runs waiting on it need it."""
        self.running = ["api"]
        self.assertEqual(self.act("service", service="model", op="on")[0], 200)
        self.assertEqual(self.act("service", service="scheduler", op="on")[0], 200)
        self.assertEqual([r[1] for r in self.ran], [["model", "on"], ["autonomy", "on"]])
        self.assertEqual(self.logged, [])
        runs = ["tundra (live since 14:06)"]
        out = str(web_actions.service_buttons("model", False, runs, runs))
        self.assertIn(">Start</button>", out)
        self.assertNotIn("orce", out)

    def test_api_restart_from_the_panel_keeps_the_mode_it_runs_in(self):
        """Bug (2026-11-12): the panel's API restart ran `shaft api on`, which on a local-only Mac needs Tailscale and on a
        tailnet-ready one turns on `tailscale serve`. An installed API restarts as it is (`api restart`); only a missing
        one is installed with `api on`."""
        self.running = []
        with mock.patch("supervisor.autonomy.installed", return_value=True):
            self.assertEqual(self.act("service", service="api", op="on")[0], 200)
        with mock.patch("supervisor.autonomy.installed", return_value=False):
            self.assertEqual(self.act("service", service="api", op="on")[0], 200)
        self.assertEqual([r[1] for r in self.ran], [["api", "restart"], ["api", "on"]])

    def test_the_web_ui_cannot_be_forced_to_restart_itself(self):
        status, body = self.act("service", service="web", op="on", force="1")
        self.assertEqual(status, 409)
        self.assertIn("cannot stop or restart itself", body)
        self.assertNothingRan()

    def test_held_back_buttons_offer_force_and_say_what_it_kills(self):
        runs = ["tundra (live since 14:06)"]
        out = str(web_actions.service_buttons("scheduler", True, runs, runs))
        self.assertNotIn("disabled", out)                                     # was a greyed-out button
        self.assertIn(">Force restart</button>", out)
        self.assertIn(">Force stop</button>", out)
        self.assertIn('name="force" value="1"', out)
        self.assertIn("Force restart scheduler now? Not safe: the scheduler&#x27;s runs share its process group, so "
                      "this would kill them: tundra (live since 14:06). They end unfinished.", out)
        api = str(web_actions.service_buttons("api", True, [], runs))
        self.assertIn("Force restart api now? Not safe: runs in progress use it: tundra (live since 14:06). Their "
                      "calls to it fail while it is down.", api)
        self.assertIn('class="act small force"', api)
        self.assertIn("Force autonomy off", str(web_actions.autonomy_button(True, runs)))
        free = str(web_actions.service_buttons("api", True, [], []))          # nothing held back: the plain buttons
        self.assertIn(">Restart</button>", free)
        self.assertNotIn("force", free)
        self.assertIn('disabled title="not running"', str(web_actions.service_buttons("api", False, [], runs)))


class ARestartOnceTheRunsEnd(ActionsTest):
    """2026-11-03: Restart scheduler offered only Force (cut the runs) while tundra and wisp covered each other's
    cycles; a drain-restart (autonomy.restart) is offered beside it, and it needs no force."""

    def setUp(self):
        super().setUp()
        runs = ["tundra (live since 14:06)"]
        p = mock.patch.object(panel, "scheduled_runs", return_value=runs)
        p.start()
        self.addCleanup(p.stop)
        self.runs = runs

    def test_held_back_the_scheduler_row_offers_restart_when_idle(self):
        with mock.patch("supervisor.autonomy.drain_line", return_value=""):
            out = str(web_actions.service_buttons("scheduler", True, self.runs, []))
            free = str(web_actions.service_buttons("scheduler", True, [], []))
            api = str(web_actions.service_buttons("api", True, self.runs, self.runs))
        self.assertIn(">Restart when idle</button>", out)
        self.assertIn('name="op" value="restart"', out)
        self.assertIn(">Force restart</button>", out)           # still there, for a run that will not end
        self.assertNotIn("when idle", free)                     # nothing held back: the plain Restart
        self.assertNotIn("when idle", api)

    def test_a_pending_drain_shows_what_it_waits_for_and_can_be_withdrawn(self):
        line = "scheduler restarting (asked 14:10): nothing new starts; waiting for wake tundra; by 16:20 at the latest"
        with mock.patch("supervisor.autonomy.drain_line", return_value=line):
            out = str(web_actions.service_buttons("scheduler", True, self.runs, []))
        self.assertIn("waiting for wake tundra; by 16:20", out)
        self.assertIn(">Cancel restart</button>", out)
        self.assertIn('name="op" value="cancel-restart"', out)
        self.assertNotIn("when idle", out)

    def test_the_actions_run_unforced_while_runs_go_on(self):
        self.assertEqual(self.act("autonomy", op="restart")[0], 200)
        self.assertEqual(self.act("autonomy", op="cancel-restart")[0], 200)
        self.assertEqual([r[1] for r in self.ran], [["autonomy", "restart"], ["autonomy", "restart", "--cancel"]])
        self.assertEqual(self.act("autonomy", op="reboot")[0], 409)


class PagesShowEachActionAsItRuns(ActionsTest):
    def page(self, path: str, s: dict | None = None) -> str:
        with mock.patch.object(web, "overview", return_value=s or snap()):
            r = self.req(path, cookie=self.cookie)
        self.assertEqual(r.status, 200, r.body[:300])
        return r.body.decode()

    def test_the_overview_asks_before_a_cycle_and_explains_when_it_cannot(self):
        idle = self.page("/fragment/overview")
        self.assertIn('data-post="/action/cycle"', idle)
        self.assertIn('data-confirm="Start a cycle for wisp now?"', idle)
        busy = self.page("/fragment/overview", snap(state="in a cycle"))
        self.assertIn('disabled title="wisp is in a cycle — a cycle starts only when it is idle"', busy)
        self.assertNotIn('data-post="/action/cycle"', busy)

    def test_paused_shows_resume(self):
        self.assertIn('data-post="/action/resume"', self.page("/fragment/overview", snap(state="paused by you")))

    def test_the_trials_page_states_the_cost_first(self):
        with mock.patch.object(panel, "trials", return_value=[{"status": "done", "verdict": {"usd": 2.0}}]), \
                mock.patch.object(panel, "costs", return_value={"today": {"trials": 5.0}, "caps": []}):
            body = self.page("/trials")
        self.assertIn("about $2.00 by the finished trials so far", body)
        self.assertIn("trials today $5.00", body)

    def test_the_services_page_asks_before_a_restart_that_would_kill_runs(self):
        with mock.patch.object(panel, "services_detail", return_value=[
                {"name": "scheduler", "running": True, "detail": ""}, {"name": "web", "running": True, "detail": ""}]), \
                mock.patch.object(panel, "scheduled_runs", return_value=["wisp (live since 12:00)"]), \
                mock.patch.object(panel, "log_tail", return_value=[]):
            body = self.page("/services")
        self.assertIn("Force restart scheduler now? Not safe: the scheduler&#x27;s runs share its process group", body)
        self.assertIn("the web UI cannot stop or restart itself", body)


class Issue20TimeoutTest(ActionsTest):
    def page(self, path: str, s: dict | None = None) -> str:
        with mock.patch.object(web, "overview", return_value=s or snap()):
            r = self.req(path, cookie=self.cookie)
        self.assertEqual(r.status, 200, r.body[:300])
        return r.body.decode()

    def test_issue_20_service_and_autonomy_forms_wait_at_least_the_server_limit(self):
        """#20: the page used to give up at 30s while service and autonomy on/off are allowed SLOW_S on the server,
        then say that sending the action again is safe. The form's data-timeout is that limit plus a grace, from the
        same constant _ran passes to run_now. Reverting the attribute, or a timeout below the server limit, fails
        this: a slow restart looks failed and a second one is invited while the first is still running."""
        import re
        with mock.patch.object(panel, "services_detail", return_value=[
                {"name": "model", "running": True, "detail": ""},
                {"name": "scheduler", "running": True, "detail": ""}]), \
                mock.patch.object(panel, "scheduled_runs", return_value=[]), \
                mock.patch.object(panel, "log_tail", return_value=[]), \
                mock.patch.object(actions, "busy_instances", return_value=[]):
            body = self.page("/services")
        forms = re.findall(r'<form\b[^>]*data-post="/action/(?:service|autonomy)"[^>]*>', body)
        self.assertTrue(any('data-post="/action/service"' in f for f in forms), body)
        self.assertTrue(any('data-post="/action/autonomy"' in f for f in forms), body)
        slow = web_actions.page_wait(actions.SLOW_S)
        self.assertGreaterEqual(slow, actions.SLOW_S)
        for form in forms:
            got = re.search(r'data-timeout="(\d+)"', form)
            self.assertIsNotNone(got, form)
            self.assertEqual(int(got.group(1)), slow, form)
            self.assertIn('data-check="Services"', form)
        overview = self.page("/fragment/overview")
        cycles = re.findall(r'<form\b[^>]*data-post="/action/cycle"[^>]*>', overview)
        self.assertTrue(cycles, overview)
        usual = web_actions.page_wait(actions.RUN_NOW_S)
        self.assertGreaterEqual(usual, actions.RUN_NOW_S)
        for form in cycles:
            got = re.search(r'data-timeout="(\d+)"', form)
            self.assertIsNotNone(got, form)
            self.assertEqual(int(got.group(1)), usual, form)
            self.assertIn('data-check="Overview"', form)


class RecapLockTest(unittest.TestCase):
    """`shaft top`, the web UI and `shaft recap` share one lock per instance: a recap is never paid for twice."""

    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-recap-lock-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        p = mock.patch.object(recap, "SUP_DIR", self.sup)
        p.start()
        self.addCleanup(p.stop)

    def test_a_recap_being_written_elsewhere_is_not_started_again(self):
        path = recap._lock_path("wisp")
        path.parent.mkdir(parents=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR)        # another process's lock (flock: per open file)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        self.assertTrue(recap.running("wisp"))
        with mock.patch.object(recap, "write") as write:
            self.assertEqual(recap.start([{"name": "wisp"}], wait=True), "a recap is already being written")
        write.assert_not_called()

    def test_the_lock_is_released_when_the_recap_is_written(self):
        with mock.patch.object(recap, "write") as write:
            recap.start([{"name": "wisp"}], wait=True)
        write.assert_called_once()
        self.assertFalse(recap.running("wisp"))


if __name__ == "__main__":
    unittest.main()


class NewVerbsTest(unittest.TestCase):
    """The CLI verbs the panels' in-process actions became: shaft cycle, recap, alerts."""

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
        with mock.patch.object(autonomy, "start_wake", return_value="started a cycle for wisp") as sw:
            self.assertEqual(self.cli("cycle", "wisp"), (0, "started a cycle for wisp\n"))
        sw.assert_called_once_with("wisp")
        with mock.patch.object(autonomy, "start_wake", side_effect=RuntimeError("wisp is already awake (live)")):
            code, out = self.cli("cycle", "wisp")
        self.assertEqual(code, 1)
        self.assertIn("not started: wisp is already awake", out)

    def test_alerts_lists_and_dismisses(self):
        with mock.patch.object(panel, "extras", side_effect=lambda s: {**s, "alerts": [{"id": "a1", "text": "one"}]}), \
                mock.patch("supervisor.dashboard.services", return_value=[]):
            self.assertEqual(self.cli("alerts"), (0, "a1  one\n"))
        with mock.patch.object(panel, "dismiss") as dismiss:
            self.assertEqual(self.cli("alerts", "dismiss", "a1", "a2")[0], 0)
        dismiss.assert_called_once_with(["a1", "a2"])
        self.assertEqual(self.cli("alerts", "dismiss")[0], 1)

    def test_recap_writes_and_prints_each_or_says_it_failed(self):
        with mock.patch("supervisor.core.instance_names", return_value=["wisp"]), \
                mock.patch("supervisor.dashboard.instance_state", return_value={"name": "wisp"}), \
                mock.patch.object(recap, "start", return_value="recapping wisp…") as start, \
                mock.patch.object(recap, "load", return_value={"wisp": {"ts": "2026-10-19T12:00:00", "text": "tidying",
                                                                        "error": "HTTP 529",
                                                                        "error_ts": "2026-10-19T12:01:00"}}):
            code, out = self.cli("recap")
        start.assert_called_once_with([{"name": "wisp"}], wait=True)
        self.assertEqual(code, 1)
        self.assertIn("wisp: recap failed: HTTP 529", out)


class OneBrokenInstanceTest(unittest.TestCase):
    def test_an_update_check_that_raises_disables_only_that_button(self):
        """Live, 2026-10-19: version.instance raised SystemExit for one instance and the whole overview failed."""
        web_actions._updates.clear()
        with mock.patch.object(actions, "update_question", side_effect=SystemExit("kiln is not a registered worktree")):
            out = web_actions.instance_buttons({"name": "kiln", "state": "idle"})
        self.assertIn('disabled title="cannot tell whether kiln is behind the template: kiln is not a registered', out)
        self.assertIn('data-post="/action/cycle"', out)                 # the other actions are still there
        web_actions._updates.clear()
