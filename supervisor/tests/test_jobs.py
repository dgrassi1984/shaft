"""Scheduled jobs (docs/plans/PLAN-jobs.md): an instance's own plugins, run on the timer their manifest declares, in the job
sandbox, their output delivered to its inbox, a wake started only when the job may and the run asks. The real
sandbox runs the plugins; only the scheduler's children are fakes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import core, jobs

TEMPLATE = Path(__file__).resolve().parents[2] / "template"


class JobCase(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-jobs-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        self.sup = self.project / ".supervisor"
        self.root = self.project / "instances" / "wren"
        git = lambda *a, cwd=self.project: subprocess.run(["git", "-C", str(cwd), *a], check=True,  # noqa: E731
                                                           capture_output=True)
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "main")
        git("worktree", "add", "-q", "--orphan", "-b", "instance/wren", str(self.root))
        self.git = lambda *a: git("-c", "user.name=t", "-c", "user.email=t@t", *a, cwd=self.root)  # noqa: E731
        shutil.copytree(TEMPLATE / "shaft", self.root / "shaft", ignore=shutil.ignore_patterns("__pycache__"))
        for d in ("notes", "memory", "inbox", "plugins"):
            (self.root / d).mkdir()
        (self.root / "config.env").write_text('OPERATOR_NAME="the operator"\n')
        for p in (mock.patch.object(core, "SUP_DIR", self.sup), mock.patch.object(jobs, "SUP_DIR", self.sup),
                  mock.patch.object(core, "INSTANCES", self.project / "instances")):
            p.start()
            self.addCleanup(p.stop)

    def plugin(self, name: str, body: str, schedule: dict | None = None, commit: bool = True, **extra) -> None:
        d = self.root / "plugins" / name
        d.mkdir(parents=True, exist_ok=True)
        spec = {"name": name, "description": "test", "parameters": {"type": "object", "properties": {}}, **extra}
        if schedule is not None:
            spec["schedule"] = schedule
        (d / "plugin.json").write_text(json.dumps(spec))
        (d / "main.py").write_text(textwrap.dedent(body))
        if commit:   # a job runs only committed code, as a cycle's commit leaves it
            self.git("add", "-A", "--", f"plugins/{name}")
            self.git("commit", "-q", "-m", f"plugin {name}")

    def inbox(self) -> list[str]:
        return [p.read_text() for p in sorted((self.root / "inbox").glob("*.md"))]

    def rows(self) -> list[dict]:
        path = self.sup / "jobs" / "wren.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.is_file() else []


class DeclaredTest(JobCase):
    def test_job_runs_on_its_interval_and_not_faster_than_the_floor(self):
        self.plugin("ok", "def run(): return ''", {"every_minutes": 15})
        self.plugin("fast", "def run(): return ''", {"every_minutes": jobs.MIN_MINUTES - 1})
        declared, problems = jobs.declared("wren")
        self.assertEqual([j["plugin"] for j in declared], ["ok"])
        self.assertIn(f"outside {jobs.MIN_MINUTES}", problems[0])
        job, now = declared[0], datetime(2026, 10, 16, 12, 0)
        self.assertTrue(jobs.due("wren", job, now))                         # never run: due
        jobs._update("wren", "ok", last_start=now.isoformat())
        self.assertFalse(jobs.due("wren", job, now + timedelta(minutes=14)))
        self.assertTrue(jobs.due("wren", job, now + timedelta(minutes=15)))

    def test_an_uncommitted_plugin_waits_for_its_commit(self):
        # Regression (2026-10-16): wren's watcher ran mid-conversation, half-written ("NameError: name 'broker' is not
        # defined"), and a plugin.json written before its main.py was reported to it as "no main.py".
        self.plugin("watch", "def run(): return 'v1'", {"every_minutes": 10})
        (self.root / "plugins" / "watch" / "main.py").write_text("def run(: broken mid-edit")
        (self.root / "plugins" / "half").mkdir()
        (self.root / "plugins" / "half" / "plugin.json").write_text(json.dumps(
            {"name": "half", "description": "x", "parameters": {"type": "object"}, "schedule": {"every_minutes": 10}}))
        self.assertEqual(jobs.declared("wren"), ([], []))                 # neither runs, nothing is reported
        self.assertIn("watch: being changed (not committed yet)", jobs.describe("wren"))
        self.assertEqual(jobs.run("wren", "watch"), 2)
        self.git("checkout", "--", "plugins/watch")
        self.assertEqual([j["plugin"] for j in jobs.declared("wren")[0]], ["watch"])

    def test_bad_manifest_skipped_and_reported_once(self):
        self.plugin("bad", "def run(): return ''", {"every_minutes": "often"})
        (self.root / "plugins" / "broken").mkdir()
        (self.root / "plugins" / "broken" / "plugin.json").write_text('{"name": "broken", "schedule": {')
        self.git("add", "-A", "--", "plugins/broken")
        self.git("commit", "-q", "-m", "broken")
        for _ in range(2):
            jobs.tick({}, datetime.now(), lambda args: self.fail("nothing should start"), ["wren"], lambda n: "")
        self.assertEqual(len(self.inbox()), 1)                              # once, not every tick
        self.assertIn("every_minutes must be a whole number", self.inbox()[0])
        self.assertIn("plugins/broken/plugin.json does not parse", self.inbox()[0])

    def test_max_jobs_follows_the_knob(self):
        env = {k: v for k, v in os.environ.items() if k != "SHAFT_JOBS_MAX"}
        env["SHAFT_SUPERVISOR_DIR"] = str(self.project / "settings")
        for value, expected in ((None, "12"), ("17", "17")):
            with self.subTest(value=value):
                if value is not None:
                    env["SHAFT_JOBS_MAX"] = value
                out = subprocess.run([core.PYTHON, "-c", "from supervisor import jobs; print(jobs.MAX_JOBS)"],
                                     capture_output=True, text=True, check=True, env=env)
                self.assertEqual(out.stdout.strip(), expected)

    def test_at_most_twelve_jobs(self):
        for i in reversed(range(13)):
            self.plugin(f"p{i:02}", "def run(): return ''", {"every_minutes": 10})
        declared, problems = jobs.declared("wren")
        self.assertEqual([j["plugin"] for j in declared], [f"p{i:02}" for i in range(12)])
        self.assertEqual(problems, ["13 scheduled plugins; at most 12 run — not run: p12"])
        started = []
        jobs.tick({}, datetime.now(), started.append, ["wren"], lambda n: "")
        self.assertEqual(started, [["job", "run", "wren", f"p{i:02}"] for i in range(12)])


class SchedulerTest(JobCase):
    def setUp(self):
        super().setUp()
        self.plugin("watch", "def run(): return ''", {"every_minutes": 10, "can_wake": True})
        self.started: list[list[str]] = []
        p = mock.patch("supervisor.autonomy.record")
        self.record = p.start()
        self.addCleanup(p.stop)

    def start(self, args):
        self.started.append(args)
        return object()

    def test_overrunning_job_is_skipped_not_stacked_and_logged(self):
        now = datetime(2026, 10, 16, 12, 0)
        children = {}
        jobs.tick(children, now, self.start, ["wren"], lambda n: "")
        self.assertEqual(self.started, [["job", "run", "wren", "watch"]])
        jobs._update("wren", "watch", last_start=now.isoformat())
        with mock.patch.object(jobs, "log") as log:
            jobs.tick(children, now + timedelta(minutes=10), self.start, ["wren"], lambda n: "")
        self.assertEqual(len(self.started), 1)                              # skipped, not stacked
        self.assertEqual(self.rows()[-1]["skipped"], "its previous run is still going")
        self.assertIn("skipped — its previous run is still going", log.call_args[0][0])
        del children["job wren watch"]                                     # it ended: next one a full interval later
        jobs.tick(children, now + timedelta(minutes=15), self.start, ["wren"], lambda n: "")
        self.assertEqual(len(self.started), 1)
        jobs.tick(children, now + timedelta(minutes=20), self.start, ["wren"], lambda n: "")
        self.assertEqual(len(self.started), 2)

    def test_wake_only_when_declared_and_requested_coalesced_while_awake(self):
        job = jobs.declared("wren")[0][0]
        self.assertEqual(jobs._outcome('{"message": "new mail", "wake": false}', job)[:2], ("new mail", False))
        self.assertEqual(jobs._outcome("plain text", job)[:2], ("plain text", False))
        self.assertEqual(jobs._outcome('{"message": "new mail", "wake": true}', job)[:2], ("new mail", True))
        no_wake = {**job, "can_wake": False}
        msg, wake, note = jobs._outcome('{"message": "x", "wake": true}', no_wake)
        self.assertFalse(wake)
        self.assertIn("does not say can_wake: true", note)
        jobs.request_wake("wren", "watch")
        jobs.request_wake("wren", "watch")
        jobs.request_wake("wren", "other")
        children = {"job wren watch": (object(), 0)}                        # no job is due: only the wake is at stake
        jobs.tick(children, datetime.now(), self.start, ["wren"], lambda n: "a cycle since 12:00")
        self.assertEqual(self.started, [])                                  # awake: kept
        jobs.tick(children, datetime.now(), self.start, ["wren"], lambda n: "")
        self.assertEqual(self.started, [["wake", "wren"]])                  # one wake for three requests
        self.record.assert_called_once_with("wake", "wren")
        jobs.tick(children, datetime.now(), self.start, ["wren"], lambda n: "")
        self.assertEqual(self.started, [["wake", "wren"]])

    def test_paused_instance_runs_no_jobs_and_its_wake_is_dropped(self):
        jobs.request_wake("wren", "watch")
        with mock.patch.object(core, "paused", lambda n: True):
            jobs.tick({}, datetime.now(), self.start, ["wren"], lambda n: "")
        self.assertEqual(self.started, [])
        self.assertEqual(jobs.pending_wakes(), {})

    def test_operator_switch_off_holds(self):
        jobs.set_jobs_off("wren", True)
        jobs.tick({}, datetime.now(), self.start, ["wren"], lambda n: "")
        self.assertEqual(self.started, [])
        self.assertTrue(json.loads((self.sup / "tools" / "wren.json").read_text())["jobs_off"])
        jobs.set_jobs_off("wren", False)
        jobs.tick({}, datetime.now(), self.start, ["wren"], lambda n: "")
        self.assertEqual(self.started, [["job", "run", "wren", "watch"]])

    def test_no_jobs_during_a_battery_exam(self):
        # An exam runs in the instance's real folder and undoes every file created meanwhile: a message would be lost.
        jobs.tick({"battery wren": (object(), 0)}, datetime.now(), self.start, ["wren"], lambda n: "")
        self.assertEqual(self.started, [])


class RunTest(JobCase):
    """jobs.run under the real sandbox-exec, with a real broker."""

    def test_job_output_lands_in_inbox_and_empty_output_delivers_nothing(self):
        self.plugin("hello", "def run(who): return f'hello {who}'", {"every_minutes": 10, "args": {"who": "wren"}})
        self.plugin("quiet", "def run(): return ''", {"every_minutes": 10})
        self.assertEqual(jobs.run("wren", "hello"), 0)
        self.assertEqual(jobs.run("wren", "quiet"), 0)
        self.assertEqual(len(self.inbox()), 1)
        self.assertIn("From: job hello (my own scheduled job", self.inbox()[0])
        self.assertIn("hello wren", self.inbox()[0])
        self.assertEqual([(r["plugin"], r["delivered"]) for r in self.rows()], [("hello", True), ("quiet", False)])

    def test_job_writes_only_its_state_dir(self):
        self.plugin("writer", """
            import os, pathlib
            def run():
                out = []
                for rel in ("notes/x.md", "plugins/writer/evil.py", "memory/x.md", "config.env", "inbox/x.md",
                            os.environ["SHAFT_JOB_STATE"] + "/state.json", ".tmp/scratch"):
                    try:
                        pathlib.Path(rel).write_text("x")
                        out.append(f"{rel}: WROTE")
                    except OSError:
                        out.append(f"{rel}: refused")
                return "\\n".join(out)
            """, {"every_minutes": 10})
        self.assertEqual(jobs.run("wren", "writer"), 0)
        report = self.inbox()[0]
        for rel in ("notes/x.md", "plugins/writer/evil.py", "memory/x.md", "config.env", "inbox/x.md"):
            self.assertIn(f"{rel}: refused", report)
        self.assertIn(".jobs/writer/state.json: WROTE", report)
        self.assertIn(".tmp/scratch: WROTE", report)
        self.assertEqual((self.root / "config.env").read_text(), 'OPERATOR_NAME="the operator"\n')

    def test_job_has_no_model_and_no_send_message(self):
        self.plugin("asker", """
            import json, os, urllib.request, urllib.error
            from shaft import broker
            def run():
                req = urllib.request.Request(os.environ["SHAFT_BROKER_URL"] + "/llm/v1/chat/completions",
                    data=b'{"model": "x", "messages": []}', headers={"X-Shaft-Token": os.environ["SHAFT_BROKER_TOKEN"],
                    "Content-Type": "application/json"})
                try:
                    urllib.request.urlopen(req, timeout=10)
                    model = "MODEL ANSWERED"
                except urllib.error.HTTPError as e:
                    model = f"model {e.code}: {json.load(e)['error']['message']}"
                try:
                    broker.call("send_message", to="operator", text="hi")
                    sent = "SENT"
                except broker.BrokerError as e:
                    sent = str(e)
                return model + "\\n" + sent
            """, {"every_minutes": 10})
        self.assertEqual(jobs.run("wren", "asker"), 0)
        report = self.inbox()[0]
        self.assertIn("model 403: a scheduled job has no model", report)
        self.assertIn("send_message is not available in a scheduled job", report)
        self.assertFalse((self.sup / "operator-inbox").exists())

    def test_job_mail_grant_applies(self):
        core.set_mail_grants("wren", [{"account": "work", "label": "AssistantAccess"}])
        self.plugin("mailer", """
            import os
            from shaft import broker
            def run():
                try:
                    broker.call("mail_search", query="")
                    return "searched"
                except broker.BrokerError as e:
                    return os.environ.get("SHAFT_MAIL", "") + " | " + str(e)
            """, {"every_minutes": 10})
        self.assertEqual(jobs.run("wren", "mailer"), 0)
        report = self.inbox()[0]
        self.assertIn("work/AssistantAccess", report)                         # granted: the job sees its grant …
        self.assertNotIn("no mail granted to you", report)                 # … and the tool is offered to it

    def test_failures_back_off_and_are_reported_once(self):
        self.plugin("flaky", "def run(): raise RuntimeError('upstream down')", {"every_minutes": 10})
        for _ in range(jobs.FAIL_REPORT + 1):
            self.assertEqual(jobs.run("wren", "flaky"), 1)
        reports = [m for m in self.inbox() if "failed" in m]
        self.assertEqual(len(reports), 1)
        self.assertIn("upstream down", reports[0])
        job = jobs.declared("wren")[0][0]
        e = jobs.entry("wren", "flaky")
        self.assertEqual(e["fails"], jobs.FAIL_REPORT + 1)
        self.assertEqual(jobs.interval(job, e), timedelta(minutes=40))     # doubled past the third failure
        self.plugin("flaky", "def run(): return ''", {"every_minutes": 10})   # fixed, and committed
        self.assertEqual(jobs.run("wren", "flaky"), 0)
        self.assertEqual(jobs.entry("wren", "flaky")["fails"], 0)

    def test_a_job_that_overruns_is_killed(self):
        self.plugin("slow", "import time\ndef run():\n    time.sleep(5)\n    return 'late'", {"every_minutes": 10})
        with mock.patch.object(jobs, "TIMEOUT_S", 1):
            self.assertEqual(jobs.run("wren", "slow"), 1)
        self.assertTrue(self.rows()[-1]["timed_out"])
        self.assertEqual(self.inbox(), [])

    def test_wake_requested_by_a_run(self):
        self.plugin("waker", 'def run(): return {"message": "new tagged mail", "wake": True}',
                    {"every_minutes": 10, "can_wake": True})
        self.assertEqual(jobs.run("wren", "waker"), 0)
        self.assertEqual(jobs.pending_wakes()["wren"]["by"], ["waker"])
        self.assertIn("new tagged mail", self.inbox()[0])


if __name__ == "__main__":
    unittest.main()


class ConcurrentStateTest(unittest.TestCase):
    """Regression (2026-10-19): thelos's and wren's `watcher` jobs ran in the same second. Both wrote
    .supervisor/jobs/state.json through one temp name, state.tmp; the second rename failed (FileNotFoundError) and
    thelos's job counted as failed. Unlocked, one process's update could also overwrite the other's."""

    def test_two_processes_updating_at_once_lose_nothing_and_never_fail(self):
        sup = Path(tempfile.mkdtemp(prefix="shaft-jobs-state-")).resolve()
        self.addCleanup(shutil.rmtree, sup, True)
        script = ("import sys\nfrom supervisor import jobs\n"
                  "for i in range(60): jobs._update(sys.argv[1], 'watcher', n=i)\n"
                  "jobs.request_wake(sys.argv[1], 'watcher')\n")
        env = {**__import__("os").environ, "SHAFT_SUPERVISOR_DIR": str(sup)}
        procs = [subprocess.Popen([core.PYTHON, "-c", script, name], cwd=core.PROJECT, env=env,
                                  stderr=subprocess.PIPE, text=True) for name in ("thelos", "wren", "kant")]
        errs = [p.communicate(timeout=60)[1] for p in procs]
        self.assertEqual([p.returncode for p in procs], [0, 0, 0], errs)
        st = json.loads((sup / "jobs" / "state.json").read_text())
        self.assertEqual({n: st["jobs"][n]["watcher"]["n"] for n in ("thelos", "wren", "kant")},
                         {"thelos": 59, "wren": 59, "kant": 59})                  # no update lost
        self.assertEqual(sorted(st["wake"]), ["kant", "thelos", "wren"])
        self.assertEqual(sorted(p.name for p in (sup / "jobs").glob("*.tmp")), [])


class ReplayRecordTest(JobCase):
    """Bug (review of #89): in a trial replay, record(kind="jobs") answered "(a replay does not see the job record)" —
    the instance learned it was being replayed (the class of group A's "This is a replay"). It now answers as a live
    record would have at the replayed cycle's start."""

    def test_a_replay_sees_the_job_record_as_it_stood_then_and_nothing_says_replay(self):
        import threading
        from supervisor import broker
        self.plugin("watch", "def run(): return ''", {"every_minutes": 15})
        (self.sup / "jobs").mkdir(parents=True)
        (self.sup / "jobs" / "wren.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
            {"ts": "2026-10-20T09:00:00", "plugin": "watch", "seconds": 1.0, "exit": 0, "timed_out": False},
            {"ts": "2026-10-20T10:00:00", "plugin": "watch", "seconds": 2.0, "exit": 1, "timed_out": False,
             "error": "exit 1: boom"},
            {"ts": "2026-10-20T12:00:00", "plugin": "watch", "seconds": 1.5, "exit": 0, "timed_out": False})))
        copy = self.project / "copy"                          # the replay's folder: a commit's tree, no git of its own
        shutil.copytree(self.root / "plugins", copy / "plugins")
        replay = broker.ReplayBroker("wren", self.project, {}, self.project / "replay.log", state_root=copy,
                                     start_commit="a" * 40, as_of=datetime(2026, 10, 20, 11),
                                     cache={}, cache_lock=threading.Lock(), billed="trial-wren",
                                     tools_log=self.project / "tools.log")
        text = replay._record("jobs")
        self.assertNotIn("replay", text.lower())
        self.assertIn("watch: every 15 min · last ok 2026-10-20T09:00:00 · 1 failure(s) in a row", text)
        self.assertIn("last error: exit 1: boom", text)
        self.assertIn("2026-10-20T10:00:00 watch: FAILED 2.0s", text)
        self.assertNotIn("2026-10-20T12:00:00", text)          # logged after the cycle began
        live = jobs.describe("wren")                            # the same shape live, with what came since
        self.assertEqual(live.splitlines()[0], text.splitlines()[0])
        self.assertIn("  watch: every 15 min · ", live)          # (its state comes from the state file, not written here)
        self.assertIn("2026-10-20T12:00:00 watch: ok 1.5s", live)

    def replay_text(self, rows, at=datetime(2026, 10, 20, 11)):
        import threading
        from supervisor import broker
        (self.sup / "jobs").mkdir(parents=True, exist_ok=True)
        (self.sup / "jobs" / "wren.jsonl").write_text("".join(r if isinstance(r, str) else json.dumps(r) + "\n"
                                                              for r in rows))
        copy = self.project / "copy"
        shutil.rmtree(copy, ignore_errors=True)
        shutil.copytree(self.root / "plugins", copy / "plugins")
        return broker.ReplayBroker("wren", self.project, {}, self.project / "replay.log", state_root=copy,
                                   start_commit="a" * 40, as_of=at, cache={}, cache_lock=threading.Lock(),
                                   billed="trial-wren", tools_log=self.project / "tools.log")._record("jobs")

    def test_a_run_still_going_at_the_cycle_start_is_not_shown(self):
        # Codex's review: rows carry their start; a run that started at 10:59 and ended at 11:01 had no outcome yet.
        self.plugin("watch", "def run(): return ''", {"every_minutes": 15})
        for row in ({"ts": "2026-10-20T10:59:30", "plugin": "watch", "seconds": 90, "exit": 1, "error": "late"},
                    {"ts": "2026-10-20T10:59:30", "plugin": "watch", "seconds": 1, "exit": 1, "error": "late",
                     "done": "2026-10-20T11:00:30"}):
            with self.subTest(row=row):
                text = self.replay_text([row])
                self.assertNotIn("late", text)
                self.assertIn("watch: every 15 min · not run yet", text)

    def test_a_schedule_change_restarts_the_failure_count_as_live(self):
        # Codex's review: three failures under schedule A were reported under the newly declared schedule B.
        self.plugin("watch", "def run(): return ''", {"every_minutes": 15})
        new = jobs.declared("wren")[0][0]["hash"]
        old = [{"ts": f"2026-10-20T0{h}:00:00", "plugin": "watch", "seconds": 1, "exit": 1, "error": f"e{h}",
                "hash": "a" * 16} for h in (7, 8, 9)]
        self.assertNotIn("failure(s) in a row", self.replay_text(old))          # B has not failed yet
        once = old + [{"ts": "2026-10-20T10:00:00", "plugin": "watch", "seconds": 1, "exit": 1, "error": "b1",
                       "hash": new}]
        self.assertIn("· 1 failure(s) in a row", self.replay_text(once))

    def test_a_torn_or_undated_log_line_is_skipped(self):
        # Codex's review: one malformed line, a null row or a row without ts crashed the record.
        self.plugin("watch", "def run(): return ''", {"every_minutes": 15})
        rows = ["{torn\n", "null\n", json.dumps({"plugin": "watch", "exit": 0}) + "\n",
                json.dumps({"ts": "2026-10-20T08:00:00", "plugin": "watch", "seconds": 1e309, "exit": 1}) + "\n",
                {"ts": "2026-10-20T09:00:00", "plugin": "watch", "seconds": 1, "exit": 0}]
        self.assertIn("last ok 2026-10-20T09:00:00", self.replay_text(rows))
        self.assertIn("2026-10-20T09:00:00 watch: ok 1s", jobs.describe("wren", limit=2))   # live: tail, then parse

    def test_a_run_logs_when_its_outcome_was_known_and_its_schedule(self):
        self.plugin("watch", "def run(): return ''", {"every_minutes": 15})
        jobs.run("wren", "watch")
        row = self.rows()[-1]
        self.assertGreaterEqual(row["done"], row["ts"])
        self.assertEqual(row["hash"], jobs.declared("wren")[0][0]["hash"])
