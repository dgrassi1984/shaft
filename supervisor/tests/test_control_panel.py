"""`shaft top` as the control panel (docs/plans/PLAN-control-panel.md, the operator 2026-10-13: "a full control panel of the
infrastructure, including benchmarks, judge results, etc").

Data (supervisor/panel.py) is tested on fixture files; the views and keys (top.py, top_views.py) with the data
functions stubbed, and every action with the subprocess call mocked — asserting the exact CLI subcommand the
panel would run, since running that command is the panel's whole authority.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import actions, cloudllm, coder, core, dashboard, localmodel, panel, selfhosted, trial
from supervisor.core import PROJECT

try:
    from rich.console import Console

    from supervisor import top, top_views
except ModuleNotFoundError:   # rich lives in .venv-ui
    top = top_views = None

TODAY = date.today().isoformat()
NOW = datetime.now()


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class PanelData(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-panel-")).resolve()
        self.addCleanup(shutil.rmtree, self.sup, True)
        for p in (mock.patch("supervisor.selfhosted.SERVERS",
                             {"gpu": {"url": "http://gpu-box.example:8080/v1"}}),
                  mock.patch.object(panel, "SUP_DIR", self.sup),
                  mock.patch.object(trial, "SUP_DIR", self.sup),
                  mock.patch.object(trial, "LEDGER", self.sup / "trial_usage.jsonl"),
                  mock.patch.object(cloudllm, "USAGE", self.sup / "cloud_usage.jsonl"),
                  mock.patch.object(coder, "USAGE", self.sup / "coder_usage.jsonl"),
                  mock.patch.object(panel, "instance_names", lambda: ["shaft", "wren"]),
                  mock.patch.object(panel, "instance_config", lambda n: self.cfgs.get(n, {})),
                  mock.patch.object(localmodel, "plist", lambda: self.sup / "com.shaft.model.plist")):
            p.start()
            self.addCleanup(p.stop)
        (self.sup / "com.shaft.model.plist").write_text("")   # the model server is installed
        self.cfgs = {"shaft": {"LLM_BASE_URL": "broker", "LLM_MODEL": "claude-opus-5-5"},
                     "wren": {"LLM_BASE_URL": "broker", "LLM_MODEL": "gpu/bonsai2"}}   # gpu: local fallback only
        panel._memo.clear()

    def _spend(self):
        ts = f"{TODAY}T10:00:00"
        _write(self.sup / "cloud_usage.jsonl", [
            {"ts": ts, "instance": "wren", "model": "m", "usd": 1.0},
            {"ts": ts, "instance": "shaft@trial:t-1:1A", "model": "m", "usd": 0.5},   # an arm: in the trial ledger
            {"ts": ts, "instance": "probe-claude-code", "model": "m", "usd": 0.1},
            {"ts": "2026-01-01T10:00:00", "instance": "wren", "model": "m", "usd": 50.0}])   # outside the window
        _write(self.sup / "trial_usage.jsonl", [
            {"ts": ts, "instance": "wren", "trial": "t-1", "by": "instance", "kind": "arm", "usd": 0.5},
            {"ts": ts, "instance": "wren", "trial": "t-1", "by": "instance", "kind": "judge", "usd": 0.9}])
        _write(self.sup / "judge_usage.jsonl", [
            {"ts": ts, "purpose": "judge wren", "model": "claude-opus-5-5", "input_tokens": 100_000, "output_tokens": 10_000},
            {"ts": ts, "purpose": "trial wren t-1 s1 AB", "model": "claude-opus-5-5", "input_tokens": 1_000_000,
             "output_tokens": 0},
            {"ts": ts, "purpose": "judge wren", "model": "mystery-model", "input_tokens": 9, "output_tokens": 9},
            {"ts": ts, "purpose": "second wren", "model": "openrouter/z-ai/glm-5.3-flash", "input_tokens": 9,
             "output_tokens": 9, "usd": 0.01}])
        _write(self.sup / "coder_usage.jsonl", [{"ts": ts, "instance": "shaft", "model": "claude-opus-5-5", "usd": 0.3}])

    def test_costs_count_every_dollar_once(self):
        """A trial's arms are also in cloud_usage and its judge calls also in judge_usage: counted only under
        trials, the day's total is what was actually spent, not $5 more."""
        self._spend()
        c = panel.costs(days=7)
        t = c["today"]
        self.assertEqual(t["instances"], {"wren": 1.0})
        self.assertAlmostEqual(t["other"], 0.1)
        self.assertAlmostEqual(t["trials"], 1.4)
        self.assertAlmostEqual(t["judge"], 0.61)         # 100k in × $4/M + 10k out × $20/M + the second judge's $0.01
        self.assertAlmostEqual(t["coder"], 0.3)
        self.assertAlmostEqual(t["total"], 3.41)
        self.assertAlmostEqual(c["judge_today_total"], 4.61)  # the judge's own view includes trial judging
        self.assertEqual(c["unpriced"], 1)                     # an unknown model is counted as unpriced, not $0
        self.assertEqual(len(c["days"]), 7)
        self.assertEqual(sum(d["total"] for d in c["days"][:-1]), 0)
        wren = next(x for x in c["caps"] if x["name"] == "wren")
        self.assertEqual(wren["cloud"], (1.0, cloudllm.DAILY_USD))
        self.assertAlmostEqual(wren["trials"][0], 1.4)

    def test_alerts(self):
        self._spend()
        _write(self.sup / "cloud_usage.jsonl", [{"ts": f"{TODAY}T10:00:00", "instance": "wren", "usd": 7.0}])
        old = (NOW - timedelta(hours=panel.ALERT_HOURS + 1)).strftime("%Y-%m-%d %H:%M:%S")
        recent = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        (self.sup / "supervisor.log").write_text(
            f"[{old}] kant: code change FAILED tests, reverted: ['a']\n"
            f"[{recent}] wren: code change FAILED tests, reverted: ['b']\n"
            f"[{recent}] wren: probation 1234abcd passed (0.9 vs baseline 0.8)\n"
            f"[{recent}] shaft: committed live on instance/shaft (2 files)\n")
        with mock.patch.object(panel, "JUDGE_ALERT_USD", 1.0):
            a = panel.alerts({"services": [{"name": "model", "running": False}, {"name": "api", "running": False},
                                           {"name": "scheduler", "running": False}]})
        text = "\n".join(x["text"] for x in a)
        self.assertIn("service scheduler is not running", text)
        self.assertNotIn("service model", text)               # down by design: nobody needs it (test_model_*)
        self.assertNotIn("service api", text)                 # only the services runs depend on
        self.assertIn("wren: cloud $7.00 of $8.00", text)
        self.assertIn("judge spend $4.61", text)
        self.assertIn("wren: code change FAILED", text)
        self.assertNotIn("kant", text)                        # older than ALERT_HOURS
        self.assertNotIn("passed", text)
        self.assertEqual(len({x["id"] for x in a}), len(a))   # dismissable one by one

    # Regression (2026-10-19, web UI overview): "service model is not running" showed whenever the local model server
    # was down, though it exits after 15 idle minutes by design and its next user starts it (localmodel.py) — a false
    # alert most of the day. Now only a down that is a fault raises it: an instance whose calls go to the server, or
    # a failed on-demand start.
    MODEL_DOWN = {"services": [{"name": "model", "running": False}]}

    def _model_alerts(self):
        return [a for a in panel.alerts(self.MODEL_DOWN, cost=panel.costs(days=1)) if a["id"].startswith("svc:model")]

    def _log(self, *lines):
        with (self.sup / "supervisor.log").open("a") as f:
            f.writelines(f"[{t:%Y-%m-%d %H:%M:%S}] {msg}\n" for t, msg in lines)

    def test_model_server_that_exited_idle_raises_no_alert(self):
        self._log((NOW - timedelta(hours=3), f"{localmodel.START_FAILED} for wren's fallback from gpu/bonsai2: "
                                             "com.shaft.model did not answer on :8780 within 300s of starting it"),
                  (NOW - timedelta(hours=2), f"{localmodel.START_OK} for wren's fallback from gpu/bonsai2 in 2.2s"))
        # ...then, as on 2026-10-19 12:54 -> 13:10, it served and exited idle (that goes to com.shaft.model.log)
        self.assertEqual(self._model_alerts(), [])
        self.assertIsNone(panel.model_down_problem())

    def test_model_server_whose_on_demand_start_failed_raises_an_alert(self):
        with mock.patch.object(core, "SUP_DIR", self.sup), mock.patch.object(localmodel, "answers", return_value=False), \
                mock.patch.object(localmodel, "_launchctl", return_value=subprocess.CompletedProcess([], 113, "",
                                                                                                    "Could not find service")):
            with self.assertRaises(RuntimeError):
                localmodel.ensure("wren's fallback from gpu/bonsai2")   # the real path writes the log line
        a = self._model_alerts()
        self.assertEqual(len(a), 1, a)
        self.assertIn("service model is not running: its on-demand start failed", a[0]["text"])
        self.assertIn("wren's fallback", a[0]["text"])
        self.assertIn("launchctl kickstart: Could not find service", a[0]["text"])   # why, for the operator
        later = datetime.now() + timedelta(seconds=2)
        (self.sup / "services").mkdir()                        # started by hand after it: no longer a fault
        (self.sup / "services" / "model.json").write_text(json.dumps({"pid": 1, "started": f"{later:%Y-%m-%dT%H:%M:%S}"}))
        self.assertEqual(self._model_alerts(), [])
        (self.sup / "services" / "model.json").unlink()
        self._log((later, f"{localmodel.START_OK} for kant's live in 2.0s"))   # or a later start on demand succeeded
        self.assertEqual(self._model_alerts(), [])
        old = NOW - timedelta(hours=panel.ALERT_HOURS + 1)     # a failure older than the alert window is not shown
        (self.sup / "supervisor.log").write_text(f"[{old:%Y-%m-%d %H:%M:%S}] {localmodel.START_FAILED} for x: y\n")
        self.assertEqual(self._model_alerts(), [])

    def test_model_server_down_while_an_instance_uses_it_raises_an_alert(self):
        self.cfgs["wren"] = {"LLM_BASE_URL": "http://localhost:8780/v1"}   # it never idles out: down is a fault
        a = self._model_alerts()
        self.assertEqual([x["text"] for x in a], ["service model is not running: wren uses it (calls go to :8780)"])
        self.cfgs["wren"] = {"LLM_BASE_URL": "broker", "LLM_MODEL": "box/bonsai2"}   # a self-hosted model sent to it
        with mock.patch.object(selfhosted, "SERVERS", {"box": {"url": "http://127.0.0.1:8780/v1"}}):
            self.assertIn("wren uses it", self._model_alerts()[0]["text"])
            self.assertEqual(localmodel.pinned_by(8780, ["wren"], self.cfgs.__getitem__), ["wren"])  # never idles
        self.assertEqual(self._model_alerts(), [])               # gpu/bonsai2 again: fallback only
        (self.sup / "com.shaft.model.plist").unlink()            # uninstalled: no user could start it
        self.assertIn("not installed", self._model_alerts()[0]["text"])

    def test_the_services_line_shows_a_needed_model_server_down_and_an_unneeded_one_idle(self):
        with mock.patch.object(dashboard, "_scheduler_running", return_value=True), \
                mock.patch("supervisor.version.service", return_value=None):
            model = next(s for s in dashboard.services() if s["name"] == "model")
            self.assertEqual((model["idle"], model["detail"]), (True, "idle · starts on demand"))
            self.cfgs["wren"] = {"LLM_BASE_URL": "http://127.0.0.1:8780/v1"}
            model = next(s for s in dashboard.services() if s["name"] == "model")
            self.assertFalse(model["idle"])
            self.assertEqual(model["detail"], "down · wren uses it (calls go to :8780)")

    def test_dismissed_alerts_stay_dismissed(self):
        panel.dismiss(["log:x"])
        panel.dismiss(["cap:y"])
        self.assertEqual(panel.dismissed(), {"log:x", "cap:y"})

    def test_a_log_alerts_id_is_the_same_in_every_process(self):
        # Regression (the operator, 2026-10-14: "dismissed alerts keep popping up on next relaunch"): the id was built
        # with hash(), which Python seeds afresh in every process, so a dismissal never matched after a relaunch.
        line = "[2026-10-14 01:31:59] wren: code change FAILED tests, reverted: ['plugins/x/main.py']"
        code = f"from supervisor import panel; print(panel.log_alert_id({line!r}))"
        ids = {subprocess.run([sys.executable, "-E", "-s", "-c", code], cwd=PROJECT, capture_output=True, text=True,
                              env={**os.environ, "PYTHONHASHSEED": seed}).stdout.strip() for seed in ("1", "2", "3")}
        self.assertEqual(len(ids), 1, ids)
        self.assertEqual(ids, {panel.log_alert_id(line)})
        self.assertTrue(panel.log_alert_id(line).startswith("log:2026-10-14 01:31:59:"))

    def test_old_dismissals_are_pruned_by_age_never_by_name(self):
        # Regression: the file kept sorted(ids)[-500:], the alphabetically last 500 — past 500 entries a cap: or
        # judge: dismissal of today was dropped before old log: ones, and the alert came back.
        old = (NOW - timedelta(days=panel.DISMISS_KEEP_DAYS + 1)).strftime("%Y-%m-%d")
        panel.dismiss([f"log:{old} 09:00:00:abc", f"judge:{old}"])
        panel.dismiss([f"cap:wren:cloud:{TODAY}"])
        panel.dismiss([f"log:{TODAY} 10:{i // 60:02d}:{i % 60:02d}:{i:06x}" for i in range(600)])
        kept = panel.dismissed()
        self.assertIn(f"cap:wren:cloud:{TODAY}", kept)
        self.assertEqual(len([i for i in kept if i.startswith("log:")]), 600)
        self.assertNotIn(f"judge:{old}", kept)                  # an alert of a past day can never come back
        self.assertNotIn(f"log:{old} 09:00:00:abc", kept)

    def test_judgments_newest_first_with_what_each_achieved(self):
        _write(self.sup / "judgments" / "wren.jsonl", [
            {"episode_ts": "a", "criterion_met": "yes", "difficulty": 4},
            {"episode_ts": "b", "criterion_met": "partial", "difficulty": 2},
            {"episode_ts": "c", "criterion_met": "yes"}])                       # judged before ratings
        js = panel.judgments("wren")
        self.assertEqual([(j["episode_ts"], j["achieved"]) for j in js], [("c", None), ("b", 1.0), ("a", 4.0)])

    def test_probation_decisions_from_the_log(self):
        (self.sup / "supervisor.log").write_text(
            "[2026-10-13 14:51:34] wren: probation 8dcd8e41 passed (0.83 vs baseline 0.92)\n"
            "[2026-10-13 15:00:00] shaft: probation 74f4b7a8 failed (0.58 < 0.83): restored\n"
            "[2026-10-13 15:11:30] wren: change ec632826 on probation (baseline 0.9, n=6)\n"
            "[2026-10-13 15:12:00] wren: committed live on instance/wren (2 files)\n")
        ev = panel.probation_events("wren")
        self.assertEqual([e["commit"] for e in ev], ["ec632826", "8dcd8e41"])
        self.assertTrue(ev[1]["text"].startswith("probation 8dcd8e41 passed"))

    def test_files_are_reread_only_when_they_change(self):
        p = self.sup / "x.jsonl"
        _write(p, [{"a": 1}])
        self.assertEqual(panel.jsonl(p), [{"a": 1}])
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("re-read")):
            self.assertEqual(panel.jsonl(p), [{"a": 1}])
        _write(p, [{"a": 1}, {"a": 2}])
        self.assertEqual(len(panel.jsonl(p)), 2)


def _snap(states=("idle", "in a cycle")):
    inst = [{"name": n, "state": s, "since": "", "doing": "", "goal": "", "model": "m", "every": "30 min",
             "next_wake": "due", "conversations": 0, "calls_today": 0, "tokens_today": 0, "usd_today": 0.0,
             "judged": 0, "success": None, "achieved": None, "tools_off": []} for n, s in zip(("shaft", "wren"), states)]
    return {"at": "15:00:00", "services": [{"name": "scheduler", "running": True, "detail": ""}], "instances": inst,
            "events": [], "messages": [], "alerts": [{"id": "a1", "text": "wren: code change FAILED tests"}],
            "spend": 3.4}


class SchedulerRestartGuard(unittest.TestCase):
    """2026-10-13: a battery started by hand from a terminal blocked the scheduler restart ("would kill them:
    trial or battery worker") though it was in another process group and a restart could not touch it."""

    def _runs(self, worker_in_scheduler_group: bool) -> list[str]:
        def fake(cmd, **_):
            out = ""
            if cmd[:3] == ["pgrep", "-f", "supervisor autonomy serve"]:
                out = "98073\n"
            elif cmd[:2] == ["ps", "-o"]:
                out = "98073\n"
            elif cmd[:3] == ["pgrep", "-g", "98073"]:
                out = "4242\n" if worker_in_scheduler_group else ""
            elif cmd[:2] == ["pgrep", "-f"]:
                out = "79847\n"                     # an ungrouped matcher would see the hand-started run
            return mock.Mock(stdout=out, returncode=0)
        with mock.patch.object(panel, "instance_names", return_value=[]), \
             mock.patch.object(panel.subprocess, "run", side_effect=fake):
            return panel.scheduled_runs()

    def test_a_worker_started_by_hand_does_not_block_the_restart(self):
        self.assertEqual(self._runs(worker_in_scheduler_group=False), [])

    def test_a_worker_the_scheduler_started_still_blocks_it(self):
        self.assertEqual(self._runs(worker_in_scheduler_group=True), ["trial or battery worker"])


@unittest.skipIf(top is None, "needs rich (.venv-ui/bin/python -m unittest supervisor.tests.test_control_panel)")
class PanelRestartsOnNewCode(unittest.TestCase):
    """Regression, 2026-10-16: a top left open across the calendar merge crashed on the Benchmarks tab — its lazy
    import loaded the new battery.py (needs broker.CALENDAR_TOOLS) against the broker.py loaded before the merge."""

    def test_the_stamp_covers_the_modules_that_skewed_and_sees_an_edit(self):
        names = {Path(p[0]).name for p in top.code_stamp()}
        self.assertTrue({"battery.py", "broker.py", "panel.py", "top_views.py"} <= names)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(top, "SOURCE", Path(d)):
            f = Path(d) / "broker.py"
            f.write_text("A = 1\n")
            before = top.code_stamp()
            f.write_text("A = 1\nCALENDAR_TOOLS = ()\n")
            self.assertNotEqual(top.code_stamp(), before)

    def test_main_reexecs_itself_when_the_source_changes_instead_of_importing_it(self):
        class Exec(Exception):
            pass

        def select(*a):                                 # without the restart the loop would spin on forever
            select.calls += 1
            if select.calls > 3:
                raise AssertionError("top kept drawing on the old code after its source changed")
            return [], [], []
        select.calls = 0

        live = mock.MagicMock()
        live.__enter__.return_value = live
        stamps = iter([("old",), ("old",), ("new",)])
        execv = mock.Mock(side_effect=Exec)
        with mock.patch.object(top.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(top.sys.stdout, "isatty", return_value=True), \
                mock.patch.object(top.sys.stdin, "fileno", return_value=0), \
                mock.patch.object(top, "termios") as termios, mock.patch.object(top, "tty"), \
                mock.patch.object(top, "Live", return_value=live), \
                mock.patch.object(top, "Console", return_value=mock.Mock(size=(100, 40))), \
                mock.patch.object(top.select, "select", select), \
                mock.patch.object(top.dashboard, "snapshot", return_value={}), \
                mock.patch.object(top, "extras", side_effect=lambda s: s), \
                mock.patch.object(top, "render", return_value=""), \
                mock.patch.object(top, "code_stamp", side_effect=lambda: next(stamps)), \
                mock.patch.object(top.os, "execv", execv):
            with self.assertRaises(Exec):
                top.main([])
        execv.assert_called_once_with(top.sys.executable, top.sys.orig_argv)
        termios.tcsetattr.assert_called_once()              # the terminal is restored before the exec
        self.assertEqual(live.update.call_count, 1)         # one frame on the old code, none after the change


@unittest.skipIf(top is None,"needs rich (.venv-ui/bin/python -m unittest supervisor.tests.test_control_panel)")
class PanelKeys(unittest.TestCase):
    def setUp(self):
        self.ran = []
        for p in (mock.patch.object(top_views, "run_now", lambda args, timeout=60: self.ran.append(args) or "ok"),
                  mock.patch.object(top_views, "run_detached", lambda args: self.ran.append(["detached", *args]) or "ok"),
                  mock.patch.object(panel, "dismiss", lambda ids: None)):
            p.start()
            self.addCleanup(p.stop)

    def keys(self, ui, snap, *ks, start=None):
        return [top.dispatch(ui, k, snap, 10, start) for k in ks]

    def test_number_keys_switch_tabs_and_q_quits(self):
        ui, snap = top.UI(), _snap()
        self.keys(ui, snap, "3")
        self.assertEqual(ui.tab, "judge")
        self.keys(ui, snap, "7")
        self.assertEqual((ui.tab, ui.view), ("messages", "list"))
        self.keys(ui, snap, "esc")                                   # back from the messages list to the overview
        self.assertEqual((ui.tab, ui.view), ("overview", "main"))
        self.keys(ui, snap, "enter")                                 # the selected instance in full
        self.assertEqual(ui.tab, "instance")
        self.assertEqual(self.keys(ui, snap, "q"), [True])

    def test_an_open_editor_owns_every_key_q_included(self):
        ui, snap = top.UI(), _snap()
        self.keys(ui, snap, "w")
        self.assertEqual(ui.overlay.kind, "input")
        self.assertEqual(self.keys(ui, snap, "h", "i", " ", "q", "back", "!"), [False] * 6)
        self.keys(ui, snap, "enter")
        self.assertEqual(self.ran, [["send", "shaft", "hi !"]])
        self.assertIsNone(ui.overlay)

    def test_esc_cancels_the_editor_and_sends_nothing(self):
        ui, snap = top.UI(), _snap()
        self.keys(ui, snap, "w", "x", "esc")
        self.assertEqual(self.ran, [])

    def test_pause_asks_first_and_runs_the_cli_verb(self):
        ui, snap = top.UI(), _snap()
        self.keys(ui, snap, "p")
        self.assertIn("pause shaft?", ui.overlay.text)
        self.assertEqual(self.ran, [])
        self.keys(ui, snap, "n")
        self.assertEqual(self.ran, [])
        self.keys(ui, snap, "p", "y")
        self.assertEqual(self.ran, [["pause", "shaft"]])
        snap["instances"][0]["state"] = "paused by you"
        self.keys(ui, snap, "p", "y")
        self.assertEqual(self.ran[-1], ["resume", "shaft"])

    def test_sleep_only_for_an_idle_instance(self):
        ui, snap = top.UI(inst=1), _snap()                           # wren is in a cycle
        self.keys(ui, snap, "s")
        self.assertIsNone(ui.overlay)
        self.assertIn("starts only when it is idle", ui.flash)
        ui = top.UI()
        self.keys(ui, snap, "s", "y")
        self.assertEqual(self.ran, [["detached", "sleep", "shaft"]])

    def test_the_original_cycle_key_still_asks_and_starts(self):
        ui, snap, started = top.UI(), _snap(), []
        self.keys(ui, snap, "c", "y", start=lambda n: started.append(n) or f"started {n}")
        self.assertEqual(started, ["shaft"])

    def test_a_scheduler_restart_asks_to_be_forced_while_its_runs_would_be_killed(self):
        """2026-10-13: the scheduler's children are in its process group — a launchd restart kills them. 2026-10-20: the
        guard held it back with no way past; it now asks whether to force it, naming the runs, and n leaves it."""
        ui, snap = top.UI(tab="services", sel=3), _snap()
        logged = []
        with mock.patch.object(panel, "scheduled_runs", return_value=["wren (live since 13:47)"]), \
                mock.patch.object(actions, "log", logged.append):
            self.keys(ui, snap, "r")
            self.assertIn("force restart scheduler now? Not safe:", ui.overlay.text)
            self.assertIn("wren (live since 13:47). They end unfinished.", ui.overlay.text)
            self.keys(ui, snap, "n")
            self.keys(ui, snap, "a")                                 # autonomy off would kill them too
            self.assertIn("force autonomy off now?", ui.overlay.text)
            self.keys(ui, snap, "n")
            self.assertEqual((self.ran, logged), ([], []))
            self.keys(ui, snap, "r", "y")
        self.assertEqual(self.ran, [["autonomy", "on"]])
        self.assertEqual(len(logged), 1)
        self.assertIn("top: operator forced shaft autonomy on past the guard:", logged[0])
        with mock.patch.object(panel, "scheduled_runs", return_value=[]):
            self.keys(ui, snap, "r")
            self.assertEqual(ui.overlay.text, "start/restart scheduler?")   # nothing held back: the plain question

    def test_the_model_server_is_not_stopped_under_a_running_reply_unless_forced(self):
        ui, snap = top.UI(tab="services", sel=0), _snap()
        snap["services"].append({"name": "model", "running": True, "detail": ""})
        with mock.patch.object(top_views, "busy_instances", return_value=["shaft (talk since 16:18)"]), \
                mock.patch.object(actions, "log", lambda line: None):
            self.keys(ui, snap, "o")
            self.assertIn("force stop model now? Not safe: runs in progress use it: shaft (talk since 16:18). Their "
                          "calls to it fail while it is down.", ui.overlay.text)
            self.keys(ui, snap, "n")
            snap["services"] = [{**s, "running": False} if s["name"] == "model" else s for s in snap["services"]]
            self.keys(ui, snap, "r")                                 # stopped: starting it cuts nothing off
            self.assertEqual(ui.overlay.text, "start/restart model?")
            self.keys(ui, snap, "n")
        self.assertIsNone(ui.overlay)
        self.assertEqual(self.ran, [])

    def test_an_operator_trial_states_its_cost_before_it_is_queued(self):
        ui, snap = top.UI(tab="trials"), _snap()
        with mock.patch.object(panel, "trials", return_value=[{"status": "done", "verdict": {"usd": 2.0}},
                                                               {"status": "done", "verdict": {"usd": 1.0}}]), \
                mock.patch.object(panel, "costs", return_value={"today": {"trials": 5.0}}):
            self.keys(ui, snap, "a")
        self.assertIn("about $1.50", ui.overlay.text)
        self.assertIn("trials today $5.00", ui.overlay.text)
        self.keys(ui, snap, "y")
        self.assertEqual(self.ran[0][:4], ["trial", "run", "shaft", "--aa"])

    def test_tool_picker_switches_the_chosen_tool(self):
        ui, snap = top.UI(tab="instance"), _snap()
        with mock.patch("supervisor.core.disabled_tools", return_value={"delegate_coding"}), \
                mock.patch("supervisor.broker.SPECS", {"delegate_coding": {}, "web_search": {}}):
            self.keys(ui, snap, "t")
            self.assertEqual(ui.overlay.kind, "pick")
            self.keys(ui, snap, "enter")                             # the first: delegate_coding, now off
            self.keys(ui, snap, "y")
        self.assertEqual(self.ran, [["tools", "shaft", "on", "delegate_coding"]])

    def test_alerts_are_dismissed_with_x(self):
        ui, snap = top.UI(), _snap()
        self.keys(ui, snap, "x")
        self.assertEqual(snap["alerts"], [])


DETAIL = {"name": "shaft", "paused": False, "model": "bonsai", "via": "local", "wake": "10", "effort": "medium",
          "tools_off": [], "shared": "", "mail": "", "calendar": "", "python": ".venv · Python 3.13.1", "internet": "3 today · pypi.org ×2",
          "browser": "Chromium 141 · profile 3.1 MB", "jobs": "watcher every 15 min (can wake)", "stats": "# My measured performance\n- rate 70%",
          "journal": ["2026-10-13 17:32 — Conversation with the operator"], "questions": [],
          "probation": [{"commit": "abcdef1234", "since": "2026-10-13T15:00", "files": ["plugins/x/main.py"],
                         "baseline": 0.5, "restore_failed": "file changed since"}],
          "changes": [{"ts": "2026-10-13T10:40", "files": ["plugins/weather/main.py"], "delegated": []}],
          "merges": ["2026-10-13 17:28 shaft: merge template (operator update)"], "traits": None, "judged": 5}
JUDGMENT = {"episode_ts": "2026-10-13T17:42:56", "goal": "Audit the skill file", "criterion_met": "yes",
            "claimed": "yes", "difficulty": 3, "novelty": 2, "origin": "self", "achieved": 3.0,
            "reasoning": "Step 2 reads the full skill file.", "model": "openrouter/x/mimo",
            "value": 2, "value_for": "own_capability", "value_reason": "a corrected skill file it reads every cycle"}
TRIAL = {"id": "t-1", "instance": "wren", "by": "operator", "created": "2026-10-13T16:26:55", "status": "done",
         "hypothesis": "planted blatant harm", "states": [{"status": "done"}] * 6,
         "verdict": {"label": "worse", "W": 0, "L": 6, "T": 0, "p": 0.0156, "usd": 1.7}}
COSTS = {"days": [{"day": TODAY, "instances": {"wren": 1.0}, "judge": 0.6, "trials": 1.4, "coder": 0.3, "recaps": 0.0,
                   "other": 0.0, "total": 3.3}], "today": {"instances": {"wren": 1.0}, "judge": 0.6, "trials": 1.4,
                                                          "coder": 0.3, "recaps": 0.0, "other": 0.0, "total": 3.3},
         "caps": [{"name": "wren", "cloud": (7.0, 8.0), "coder": (0.0, 5.0), "trials": (1.4, 3.0),
                   "trials_started": (1, 2)}], "judge_today": {"trial": 4.0, "judge": 0.6}, "judge_today_total": 4.6,
         "judge_alert": 10.0, "unpriced": 0}


@unittest.skipIf(top is None, "needs rich")
class PanelViews(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(panel, "instance_detail", return_value=DETAIL),
                  mock.patch.object(panel, "judgments", return_value=[JUDGMENT]),
                  mock.patch.object(panel, "traits", return_value={"traits": [{"supported": "yes"}]}),
                  mock.patch.object(panel, "trials", return_value=[TRIAL]),
                  mock.patch.object(panel, "costs", return_value=COSTS),
                  mock.patch.object(panel, "battery_status", return_value={"installed": False, "results": [], "lines": []}),
                  mock.patch.object(panel, "evals", return_value=[{"kind": "distinct", "who": "wren", "at": "2026-10-13T08:39",
                                                                   "data": {"identified": 4, "n": 4, "consistent_with_self": 4,
                                                                            "effectiveness_agent": 7.25,
                                                                            "effectiveness_base": 6.25}}]),
                  mock.patch.object(panel, "surprise_summary", return_value={"tool_calls": {"live": 16}, "flagged": {"live": 1},
                                                                             "flagged_by_class": {"stderr": 1}}),
                  mock.patch.object(panel, "probation_events", return_value=[{"ts": "2026-10-13 15:00:00",
                                                                              "text": "probation 74f4b7a8 failed"}]),
                  mock.patch.object(panel, "template_merges", return_value=DETAIL["merges"]),
                  mock.patch.object(panel, "services_detail", return_value=[
                      {"name": "model", "running": True, "detail": "abc", "pid": 7, "stale": True},
                      {"name": "scheduler", "running": False, "detail": "", "pid": None, "stale": False}]),
                  mock.patch.object(panel, "scheduled_runs", return_value=[]),
                  mock.patch.object(panel, "log_tail", return_value=["line one"])):
            p.start()
            self.addCleanup(p.stop)

    def text(self, ui, snap=None, width=130, height=60):
        c = Console(record=True, width=width, height=height, file=io.StringIO())
        c.print(top.render(snap or _snap(), ui, width, height))
        return c.export_text()

    def test_every_tab_renders_its_data(self):
        expect = {"overview": ["⚠ wren: code change FAILED tests", "$3.40 today"],
                  "instance": ["Recent journal", "abcdef12", "plugins/weather/main.py",
                               "My measured performance"],
                  "judge": ["Audit the skill file", "traits supported 1/1", " v2 ", "value: "],
                  "trials": ["planted blatant harm", "worse", "W0 L6", "operator trials are not capped"],
                  "benchmarks": ["not installed", "identified 4/4", "effectiveness 7.2", "16 tool calls"],
                  "changes": ["plugins/weather/main.py", "RESTORE FAILED", "probation 74f4b7a8 failed", "merge template"],
                  "costs": ["Today $3.30", "$7.00 / $8.00", "Judge today $4.60", "trial $4.00"],
                  "services": ["older code than checked out", "NOT RUNNING", "line one"]}
        for tab, wants in expect.items():
            out = self.text(top.UI(tab=tab))
            for w in wants:
                self.assertIn(w, out, f"{tab}: {w!r} missing")

    def test_a_verdict_in_full(self):
        out = self.text(top.UI(tab="judge", detail=True))
        self.assertIn("Step 2 reads the full skill file.", out)
        self.assertIn("achieved         3.0 / 5", out)
        self.assertIn("value for        own_capability", out)
        self.assertIn("a corrected skill file it reads every cycle", out)

    def test_help_and_picker(self):
        self.assertIn("Secrets, new, breed and finetune stay in the CLI", self.text(top.UI(help=True)))
        ui = top.UI(overlay=top_views.Overlay("pick", "tools", items=[("on  web_search", "web_search")]))
        self.assertIn("on  web_search", self.text(ui))

    def test_narrow_terminal(self):
        for tab, _ in top_views.TABS:
            out = self.text(top.UI(tab=tab, view="list" if tab == "messages" else "main"), width=80, height=24)
            self.assertTrue(all(len(l) <= 80 for l in out.splitlines()), tab)
            self.assertIn("9 svc", out.splitlines()[0], f"{tab}: every tab reachable from the bar at 80 columns")
        costs = self.text(top.UI(tab="costs"), width=80, height=30)
        self.assertIn("$7.00 / $8.00", costs)                 # the amounts stay on the instance's row
        row = next(l for l in costs.splitlines() if l.strip().startswith("wren"))
        self.assertIn("$1.40 / $3.00", row)

    def test_plain_output_has_spend_and_alerts(self):
        out = top.plain(_snap())
        self.assertIn("$3.40 spent today", out)
        self.assertIn("ALERT wren: code change FAILED tests", out)

    def test_spark(self):
        self.assertEqual(top_views.spark([0, None, 5]), "▁·█")


if __name__ == "__main__":
    unittest.main()
