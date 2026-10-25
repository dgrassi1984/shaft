"""`shaft kill` and `shaft delete` (docs/plans/PLAN-kill-delete.md)."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from supervisor import api, battery, core, delete, jobs, kill, metrics, mind, telegram, trial
from supervisor.kill import Proc
from supervisor.runner import run_lock

PY = sys.executable


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


class TargetsTest(unittest.TestCase):
    """Which processes a kill signals: the instance's own, never the supervisor's or the operator's."""

    root = Path("/p/instances/kdx")

    def table(self, *rows) -> dict[int, Proc]:
        return {pid: Proc(pid, ppid, cmd, cwd) for pid, ppid, cmd, cwd in rows}

    def test_the_agent_and_what_it_started_never_its_holder(self):
        procs = self.table((10, 1, f"{PY} -E -s -m supervisor wake kdx", "/p"),
                           (11, 10, f"{PY} -E -s -m shaft live", str(self.root)),
                           (12, 11, "/bin/sh -c sleep 99", "/tmp"),            # below the agent: whatever its folder
                           (20, 1, "-zsh", str(self.root)),                  # the operator's shell in that folder
                           (21, 20, "vim notes/a.md", str(self.root / "notes")),
                           (30, 1, f"{PY} -E -s -m supervisor api serve", "/p"),
                           (31, 30, f"{PY} -E -s -m shaft serve", "/p/instances/kdx-2"))   # another instance
        self.assertEqual(kill.targets("kdx", procs, self.root), ([11, 12], []))

    def test_the_gate_checking_a_finished_run_is_left_to_finish(self):
        # Killing the gate's tests would fail the gate and revert the run's code; while the agent is alive the same
        # command is one of its `run` commands, and goes.
        gate = (31, 30, f"{PY} -E -s -m unittest discover -s tests -t .", str(self.root))
        plug = (32, 30, f"{PY} -E -s -m shaft.plugin_runner --check", str(self.root))
        holder = (30, 1, f"{PY} -E -s -m supervisor wake kdx", "/p")
        self.assertEqual(kill.targets("kdx", self.table(holder, gate, plug), self.root), ([], []))
        agent = (33, 30, f"{PY} -E -s -m shaft live", str(self.root))
        self.assertEqual(kill.targets("kdx", self.table(holder, gate, plug, agent), self.root)[0], [31, 32, 33])

    def test_a_battery_gets_its_own_sigterm_not_its_exam(self):
        procs = self.table((40, 1, f"{PY} -E -s -m supervisor battery run kdx --until 05:00", "/p"),
                           (41, 40, f"{PY} -E -s -m shaft exam", str(self.root)),
                           (42, 1, f"{PY} -E -s -m supervisor battery run kdx-2", "/p"))
        self.assertEqual(kill.targets("kdx", procs, self.root), ([], [40]))

    def test_an_orphaned_agent_and_a_terminal_conversation(self):
        procs = self.table((50, 1, f"{PY} -E -s -m shaft live", str(self.root)),          # its holder died
                           (51, 1, f"{PY} -E -s -m shaft.plugin_runner news", str(self.root)),
                           (52, 1, f"{PY} -E -s -m http.server", str(self.root)),    # not SHAFT's: left alone
                           (60, 1, f".venv-ui/bin/python -E -s -m supervisor.talk_ui kdx", "/p"),
                           (61, 60, f"{PY} -E -s -m shaft talk", str(self.root)),
                           (62, 1, f"{PY} -E -s -m supervisor.talk_ui kdx-2", "/p"))
        self.assertEqual(kill.targets("kdx", procs, self.root)[0], [50, 51, 61])
        self.assertEqual(kill.talk_clients("kdx", procs), [60])

    def test_a_trial_replay_is_seen_by_its_scratch_copy(self):
        procs = self.table((70, 1, f"{PY} -E -s -m supervisor trial work", "/p"),
                           (71, 70, f"{PY} -E -s -m shaft live", "/private/var/T/shaft-trial-ab12/kdx"),
                           (72, 70, f"{PY} -E -s -m shaft live", "/private/var/T/shaft-trial-cd34/kdx-2"))
        self.assertEqual(kill.replaying("kdx", procs), [71])
        self.assertEqual(kill.targets("kdx", procs, self.root), ([], []))   # the worker stops it itself


class ProcessesTest(unittest.TestCase):
    def test_processes_works_with_the_launchd_services_path(self):
        """Regression: the web panel's kill raised FileNotFoundError 'lsof' — the services' PATH has no /usr/sbin."""
        with mock.patch.dict(os.environ, {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}):
            procs = kill.processes()
        self.assertEqual(procs[os.getpid()].cwd, os.getcwd())   # its own working folder, read through lsof

    def test_no_working_folders_is_an_error_not_an_empty_kill(self):
        with mock.patch.object(kill, "LSOF", "/usr/bin/true"):
            with self.assertRaisesRegex(RuntimeError, "cannot list processes"):
                kill.processes()


class KillTest(unittest.TestCase):
    """A real process tree: a stand-in supervisor holder whose agent runs in the instance folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-kill-")).resolve()
        self.root = self.tmp / "instances" / "kdx"
        (self.root / "shaft").mkdir(parents=True)
        self.patches = [mock.patch.object(kill, "INSTANCES", self.tmp / "instances")]
        for p in self.patches:
            p.start()
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
        (core.SUP_DIR / "paused-kdx").unlink(missing_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def holder(self, agent_code: str) -> tuple[subprocess.Popen, int]:
        """`python -c … -m supervisor wake kdx` starting `python -c … -m shaft live` in the folder; (holder, agent)."""
        pidfile = self.tmp / "agent.pid"
        code = ("import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', %r, '-m', 'shaft', 'live'], "
                "cwd=%r); open(%r, 'w').write(str(p.pid)); sys.exit(p.wait() or 0)") % (agent_code, str(self.root),
                                                                                         str(pidfile))
        h = subprocess.Popen([PY, "-c", code, "-m", "supervisor", "wake", "kdx"], cwd=self.tmp)
        self.procs.append(h)
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text():
                return h, int(pidfile.read_text())
            time.sleep(0.05)
        self.fail("the stand-in agent did not start")

    def test_kill_pauses_and_stops_the_agent_not_its_holder_nor_the_operators_shell(self):
        holder, agent = self.holder("import time; time.sleep(60)")
        shell = subprocess.Popen(["sleep", "60"], cwd=self.root)   # the operator's, in the same folder
        self.procs.append(shell)
        said = []
        self.assertEqual(kill.kill("kdx", wait_s=20, say=said.append), 0)
        self.assertTrue((core.SUP_DIR / "paused-kdx").exists())
        self.assertIsNotNone(holder.wait(timeout=10))               # it ended because its agent was stopped …
        self.assertFalse(kill._alive(agent))
        self.assertIn("shaft live", said[0])
        self.assertIsNone(shell.poll())                             # … and the operator's process is untouched

    def test_an_agent_that_ignores_sigterm_is_killed_after_the_grace(self):
        holder, agent = self.holder("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                    "time.sleep(60)")
        with mock.patch.object(kill, "TERM_GRACE_S", 1):
            self.assertEqual(kill.kill("kdx", wait_s=20, say=lambda s: None), 0)
        holder.wait(timeout=10)
        self.assertFalse(kill._alive(agent))

    def test_nothing_running_still_pauses(self):
        said = []
        self.assertEqual(kill.kill("kdx", wait_s=5, say=said.append), 0)
        self.assertTrue((core.SUP_DIR / "paused-kdx").exists())
        self.assertIn("nothing was running", said[0])


class TrialsOfAPausedOrDeletedInstance(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-trialkd-")).resolve()
        (self.project / "instances" / "kdx" / "shaft").mkdir(parents=True)
        for name, tid, status in (("kdx", "t-1", "testing"), ("gone", "t-2", "testing"), ("kdx", "t-3", "queued"),
                                  ("gone", "t-4", "queued")):
            d = core.SUP_DIR / "trials" / name / tid
            d.mkdir(parents=True, exist_ok=True)
            (d / "trial.json").write_text(json.dumps({"id": tid, "instance": name, "by": "operator", "status": status, "created": tid,
                                                      "states": [{"status": "pending"}]}))

    def tearDown(self):
        for name in ("kdx", "gone"):
            shutil.rmtree(core.SUP_DIR / "trials" / name, ignore_errors=True)
        (core.SUP_DIR / "paused-kdx").unlink(missing_ok=True)
        shutil.rmtree(self.project, ignore_errors=True)

    def runnable(self) -> list[str]:
        with mock.patch.object(trial, "_pair_model", lambda t, s, p: "local"):
            return sorted(t["id"] for t, _, _ in trial.runnable(self.project))

    def test_a_deleted_instances_trials_never_run_again(self):
        # Regression: a `testing` trial of a missing instance failed its gate copy on every pass, and kept the
        # scheduler starting the trial worker every 30 s, forever.
        self.assertEqual(self.runnable(), ["t-1", "t-3"])

    def test_a_paused_instances_replays_wait(self):
        (core.SUP_DIR / "paused-kdx").write_text("x")
        self.assertEqual(self.runnable(), ["t-1"])

    def test_no_verdict_is_delivered_into_a_deleted_instances_folder(self):
        # Regression: delivering recreated instances/<name>/inbox/, a half folder that blocks `shaft new <name>`.
        trial.deliver(self.project, {"id": "t-4", "instance": "gone", "by": "instance", "status": "done"})
        self.assertFalse((self.project / "instances" / "gone").exists())


class NothingWritesIntoADeletedInstance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-gone-")).resolve()
        self.patch = mock.patch.object(metrics, "INSTANCES", self.tmp)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(core.SUP_DIR / "judgments" / "second-cases", ignore_errors=True)

    def test_second_judge_and_stats_do_not_recreate_its_folder(self):
        # Regression: the detached second judge appended a verdict and wrote self/stats.md, recreating the folder.
        metrics.write_stats("gone")
        cases = core.SUP_DIR / "judgments" / "second-cases"
        cases.mkdir(parents=True, exist_ok=True)
        (cases / "gone@2026-10-20T10-00-00.json").write_text(json.dumps({"name": "gone", "episode_ts": "x",
                                                                        "case": "c"}))
        with mock.patch.object(metrics, "second_opinion", side_effect=AssertionError("graded a deleted instance")):
            self.assertEqual(metrics.second_pending(), 0)
        self.assertFalse((self.tmp / "gone").exists())
        self.assertFalse(list(cases.glob("gone@*")))

    def test_a_wake_its_jobs_asked_for_is_dropped(self):
        # Regression: it recreated locks/<name>.lock and spawned a wake that died on "no instance named".
        with jobs._editing_state() as st:
            st.setdefault("wake", {})["gone"] = {"since": "x", "by": ["news"]}
        started = []
        with mock.patch.object(jobs, "INSTANCES", self.tmp):
            jobs.tick({}, __import__("datetime").datetime.now(), started.append, [], lambda n: False)
        self.assertEqual(started, [])
        self.assertNotIn("gone", jobs.pending_wakes())

    def test_the_api_drops_a_deleted_instances_conversation(self):
        # Regression: write_up_idle spawned `recover <name>`, which failed on a missing instance.
        conv = api.Conversations()
        conv.talks = {"k": {"talk_id": "api-1", "model": "gone", "last": 0, "written": False}}
        with mock.patch.object(api, "instance_names", lambda: ["kdx"]), \
                mock.patch.object(api, "spawn_recover", side_effect=AssertionError("wrote up a deleted one")):
            conv.write_up_idle()
        self.assertEqual(conv.talks, {})

    def test_the_running_telegram_service_stops_a_deleted_instances_bot(self):
        with mock.patch.object(telegram, "instance_names", lambda: []), \
                mock.patch.object(telegram, "call", side_effect=AssertionError("polled a deleted bot")):
            telegram.poll("gone", "tok", __import__("threading").Event())   # returns at once


class DeleteTest(unittest.TestCase):
    """A whole instance in a throwaway project: everything of it goes, nothing of anyone else's."""

    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-delete-")).resolve()
        self.backup = self.project / "backup"
        git(self.project, "init", "-q", "-b", "main")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.root = self.project / "instances" / "kdx"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "instance/kdx", str(self.root))
        for rel, text in {"shaft/__init__.py": "", "config.env": "X=1\n", "plugins/p/main.py": "def run(): 1\n"}.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "kdx: created")
        for rel in ("self/self.md", "memory/journal.md", ".venv/bin/python", ".tmp/x", ".browser/Cookies",
                    "secrets/secrets.json"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(rel)
        self.sup = core.SUP_DIR
        mind.init("kdx")
        self.other = self.project / "instances" / "kdx-2"
        (self.other / "shaft").mkdir(parents=True)
        self.patches = [mock.patch.object(delete, "PROJECT", self.project),
                        mock.patch.object(delete, "INSTANCES", self.project / "instances"),
                        mock.patch.object(delete, "git", lambda *a, check=True: subprocess.run(
                            ["git", "-C", str(self.project), *a], capture_output=True, text=True, check=check)),
                        mock.patch.object(kill, "INSTANCES", self.project / "instances"),
                        mock.patch.object(battery, "BACKUP", self.backup)]
        for p in self.patches:
            p.start()
        self.seed()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(mind.store("kdx"), ignore_errors=True)
        shutil.rmtree(self.sup / delete.ARCHIVES, ignore_errors=True)
        for p in self.mine + self.theirs:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        shutil.rmtree(self.project, ignore_errors=True)

    def seed(self):
        """Its files under .supervisor/ (each kind delete knows), the same for kdx-2 (whose name starts with it), and
        rows of both in the shared files."""
        s = self.sup
        files = ["kdx.sb", "core-changed-kdx", "manual-wake-kdx.log", "talk-history-kdx", "locks/recap-kdx.lock",
                 "update-queued-kdx",   # a queued update must not reach a new instance of the same name
                 "conversations/kdx/old.talk", "judgments/kdx.jsonl", "judgments/kdx-second.jsonl",
                 "judgments/kdx-unsampled.jsonl",
                 "judgments/kdx-traits.json", "judgments/second-cases/kdx@2026-10-20T10-00-00.json",
                 "code_changes/kdx.jsonl", "probation/kdx.json", "egress/kdx.jsonl", "egress/kdx-offline.jsonl",
                 "trials/egress-kdx.jsonl", "trials/kdx/t-1/trial.json", "jobs/kdx.jsonl", "battery/off-kdx",
                 "battery/results/kdx.jsonl", "battery/kdx.sb", "battery/items/v1/recall-kdx.json",
                 "gate-logs/kdx-20261020-101010-123456.log", "evals/battery-kdx-20261020-101010.json",
                 "evals/distinct-kdx-20261020-101010.json", "datasets/kdx-20261020-101010/a.jsonl",
                 "web/comments/kdx.json", "tools/kdx.json",
                 "operator-inbox/20261020-101010-123456-from-kdx.md",
                 "operator-inbox/read/20261020-101010-123456-from-kdx.md",
                 "operator-inbox/attachments/20261020-101010-123456-from-kdx/a.png"]
        self.mine = [s / f for f in files] + [s / "web" / "chats" / "web-kdx.json", self.backup / "v1" / "recall-kdx.json",
                                              s / "evals" / "breed-a-kdx-c-20261020-101010.json"]
        self.theirs = [s / f.replace("kdx", "kdx-2") for f in files] + [s / "web" / "chats" / "web-kdx-2.json"]
        for p in self.mine + self.theirs:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}")
        (s / "web" / "chats" / "web-kdx.json").write_text(json.dumps({"instance": "kdx"}))
        (s / "web" / "chats" / "web-kdx-2.json").write_text(json.dumps({"instance": "kdx-2"}))
        (s / "evals" / "breed-a-kdx-c-20261020-101010.json").write_text(json.dumps({"a": "a", "b": "kdx", "child": "c"}))
        self.shared = {
            "cloud_usage.jsonl": [{"instance": "kdx", "usd": 1}, {"instance": "kdx@trial:t-1:0A", "usd": 2},
                                  {"instance": "kdx-2", "usd": 3}],
            "judge_usage.jsonl": [{"purpose": "judge kdx"}, {"purpose": "judge kdx-2"}, {"purpose": "second kdx"}],
            "messages.jsonl": [{"from": "kdx", "to": "operator"}, {"from": "operator", "to": "kdx"},
                               {"from": "kdx-2", "to": "operator"}],
            "trial_usage.jsonl": [{"instance": "kdx"}, {"instance": "kdx-2"}],
        }
        for f, rows in self.shared.items():
            (s / f).write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
        (s / "supervisor.log").write_text("[2026-10-20 10:00:00] kdx: start live\n[2026-10-20 10:00:01] kdx-2: start\n"
                                          "[2026-10-20 10:00:02] web: chat with kdx\n")
        (s / "telegram.json").write_text(json.dumps({"bots": {"kdx": "kbot", "kdx-2": "k2"}, "offsets": {"kdx": 3},
                                                     "questions": {"kdx:4": "f", "kdx-2:5": "g"}, "paired": {}}))
        (s / "schedule.json").write_text(json.dumps({"wakes": {"kdx": "t", "kdx-2": "t"}, "sleep": "t"}))
        (s / "recaps.json").write_text(json.dumps({"kdx": {}, "kdx-2": {}}))
        (s / "top_dismissed.json").write_text(json.dumps(["cap:kdx:usd:2026-10-20", "cap:kdx-2:usd:2026-10-20",
                                                          "judge:2026-10-20"]))
        with jobs._editing_state() as st:
            st.update({"jobs": {"kdx": {}, "kdx-2": {}}, "wake": {"kdx": {}}, "reported": ["kdx", "kdx-2"]})
        (self.project / ".env").write_text("OTHER=1\nTELEGRAM_TOKEN_KDX=secret\nTELEGRAM_TOKEN_KDX_2=s2\n")
        for shared in ("cloud_usage.jsonl", "judge_usage.jsonl", "messages.jsonl", "trial_usage.jsonl", "supervisor.log",
                       "telegram.json", "schedule.json", "recaps.json", "top_dismissed.json"):
            self.theirs.append(s / shared)

    def rows(self, f: str) -> list:
        return [json.loads(l) for l in (self.sup / f).read_text().splitlines() if l.startswith("{")]

    def test_delete_removes_every_trace_and_keeps_an_archive(self):
        said = []
        self.assertEqual(delete.delete("kdx", say=said.append), 0)
        # gone: folder, branch, mind store, every file of its, its rows and entries, its token
        self.assertFalse(self.root.exists())
        self.assertEqual(git(self.project, "branch", "--list", "instance/kdx").strip(), "")
        self.assertNotIn("instances/kdx\n", git(self.project, "worktree", "list") + "\n")
        self.assertFalse(mind.exists("kdx"))
        self.assertEqual([p for p in self.mine if p.exists()], [])
        self.assertEqual(self.rows("cloud_usage.jsonl"), [{"instance": "kdx-2", "usd": 3}])
        self.assertEqual(self.rows("judge_usage.jsonl"), [{"purpose": "judge kdx-2"}])
        self.assertEqual(self.rows("messages.jsonl"), [{"from": "kdx-2", "to": "operator"}])
        self.assertIn("not json", (self.sup / "cloud_usage.jsonl").read_text())      # what it cannot read: kept
        lines = (self.sup / "supervisor.log").read_text().splitlines()
        self.assertEqual(lines[:2], ["[2026-10-20 10:00:01] kdx-2: start", "[2026-10-20 10:00:02] web: chat with kdx"])
        self.assertTrue(lines[2].endswith("delete kdx: done") or "delete kdx by operator" in lines[2])   # the record
        tg = json.loads((self.sup / "telegram.json").read_text())
        self.assertEqual((tg["bots"], tg["offsets"], tg["questions"]), ({"kdx-2": "k2"}, {}, {"kdx-2:5": "g"}))
        self.assertEqual(json.loads((self.sup / "schedule.json").read_text())["wakes"], {"kdx-2": "t"})
        self.assertEqual(json.loads((self.sup / "recaps.json").read_text()), {"kdx-2": {}})
        self.assertEqual(json.loads((self.sup / "top_dismissed.json").read_text()),
                         ["cap:kdx-2:usd:2026-10-20", "judge:2026-10-20"])
        st = jobs._load_state()
        self.assertEqual((st["jobs"], st["wake"], st["reported"]), ({"kdx-2": {}}, {}, ["kdx-2"]))
        self.assertEqual((self.project / ".env").read_text(), "OTHER=1\nTELEGRAM_TOKEN_KDX_2=s2\n")
        self.assertFalse((self.sup / "paused-kdx").exists())
        self.assertFalse((self.sup / "locks" / "kdx.lock").exists())
        # nothing of kdx-2's (whose name starts with kdx) was touched
        self.assertTrue(self.other.exists())
        self.assertEqual([p for p in self.theirs if not p.exists()], [])
        # the archive: mind, branch bundle, folder without what is rebuilt, its files, the purged rows; no token
        archives = list((self.sup / delete.ARCHIVES).glob("kdx-*.tar.gz"))
        self.assertEqual(len(archives), 1)
        with tarfile.open(archives[0]) as tar:
            names = set(tar.getnames())
            body = {n: tar.extractfile(n).read().decode() for n in names if tar.getmember(n).isfile()
                    and not n.startswith("mind.git/") and not n.endswith(".bundle")}
        for n in ("mind.git/HEAD", "instance-kdx.bundle", "folder/self/self.md", "folder/.browser/Cookies",
                  "folder/secrets/secrets.json", "folder/plugins/p/main.py", "supervisor/tools/kdx.json",
                  "supervisor/trials/kdx/t-1/trial.json", "purged/cloud_usage.jsonl", "purged/telegram.json",
                  "RESTORE.md"):
            self.assertIn(n, names)
        self.assertFalse([n for n in names if n.startswith(("folder/.venv", "folder/.tmp")) or n == "folder/.git"])
        self.assertIn('"kdx@trial:t-1:0A"', body["purged/cloud_usage.jsonl"])
        self.assertFalse([n for n, text in body.items() if "secret" == text.strip() or "TELEGRAM_TOKEN_KDX=" in text])
        bundle = self.project / "b.bundle"
        with tarfile.open(archives[0]) as tar:
            bundle.write_bytes(tar.extractfile("instance-kdx.bundle").read())
        self.assertIn("refs/heads/instance/kdx", git(self.project, "bundle", "list-heads", str(bundle)))
        # the name is free again: what `shaft new` checks
        self.assertFalse(self.root.exists() or mind.exists("kdx"))
        self.assertTrue(any("archive" in s for s in said))
        with self.assertRaises(delete.Refused):      # and once it is gone, there is nothing to delete
            delete.delete("kdx")

    def test_refused_while_a_run_holds_it(self):
        with run_lock("kdx", wait=False, kind="live"):
            with self.assertRaises(delete.Refused) as e:
                delete.delete("kdx")
        self.assertIn("shaft kill kdx first", str(e.exception))
        self.assertTrue(self.root.exists() and mind.exists("kdx"))
        (self.sup / "locks" / "kdx.lock").unlink(missing_ok=True)

    def test_leftovers_of_a_half_deleted_instance_are_cleared(self):
        # An interrupted delete (or a folder removed by hand): running it again finishes it.
        git(self.project, "worktree", "remove", "--force", str(self.root))
        self.assertEqual(delete.delete("kdx", keep_archive=False, say=lambda s: None), 0)
        self.assertFalse(mind.exists("kdx"))
        self.assertEqual(git(self.project, "branch", "--list", "instance/kdx").strip(), "")
        self.assertEqual([p for p in self.mine if p.exists()], [])
        self.assertFalse((self.sup / delete.ARCHIVES).exists())


if __name__ == "__main__":
    unittest.main()
