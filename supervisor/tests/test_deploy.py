"""`shaft deploy TAG --json` and `--check`: the deploy procedure (docs/deploy.md) on throwaway git repositories —
origin is a local bare repository, the primary checkout a clone of it with its template worktree. launchd, the
scheduler's drain, the instances' update and their conversations are fakes: nothing here reaches launchctl or the real
checkout."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import __main__ as cli
from supervisor import autonomy, core, deploy, runner


def git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, path, text, message):
    (Path(repo) / path).write_text(text)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)


class FakeServices:
    """launchd and the service records: each service runs as a PID; a kickstart gives it a new one that records itself
    on the code checked out now (no longer stale)."""

    def __init__(self, running=("api", "web", "scheduler")):
        self.running = set(running)
        self.pids = {name: 100 + i for i, name in enumerate(sorted(autonomy.SERVICES))}
        self.stale = {name: True for name in autonomy.SERVICES}
        self.kicked: list[str] = []
        self.restarts = 0
        self.drain = None
        self.down: set[str] = set()       # loaded, but its process never comes back (launchd runs none)

    def pid(self, name):
        return self.pids[name] if name in self.running and name not in self.down else None

    def launchctl(self, *args):
        if args[0] == "print":            # loaded: running, or down after its restart
            label = args[1].rsplit("/", 1)[-1]
            name = next(n for n, s in autonomy.SERVICES.items() if s["label"] == label)
            return subprocess.CompletedProcess(args, 0 if name in self.running else 113, "", "")
        if args[:2] != ("kickstart", "-k"):
            raise AssertionError(f"unexpected launchctl call: {args}")
        label = args[2].rsplit("/", 1)[-1]
        name = next(n for n, s in autonomy.SERVICES.items() if s["label"] == label)
        self.kicked.append(name)
        self.pids[name] += 1000
        self.stale[name] = False
        return subprocess.CompletedProcess(args, 0, "", "")

    def record(self, name, stale=True):
        if name not in self.running or name in self.down:
            return None
        rec = {"pid": self.pids[name], "commit": "abc", "describe": "v1.1.0"}
        return {**rec, "stale": self.stale[name]} if stale else rec

    def restart(self, within_min=None, wait=False, cancel=False, poll_s=5):
        assert wait is False and not cancel, "deploy asks a drain-restart and never waits or cancels"
        self.restarts += 1
        self.drain = {"pid": self.pids["scheduler"]}
        print(f"asked scheduler {self.pids['scheduler']} to restart once its runs end")
        return 0


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-deploy-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        glob = self.tmp / "gitconfig"
        glob.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n")
        (self.tmp / "git-templates").mkdir()
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(glob), "GIT_CONFIG_NOSYSTEM": "1",
                                           "GIT_TEMPLATE_DIR": str(self.tmp / "git-templates")})
        env.start()
        self.addCleanup(env.stop)
        self.origin, seed, self.project = self.tmp / "origin.git", self.tmp / "seed", self.tmp / "project"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        commit(seed, "app.py", "1\n", "one")
        git(seed, "tag", "v1.0.0")
        git(seed, "checkout", "-q", "-b", "template")
        commit(seed, "agent.py", "1\n", "template one")
        git(seed, "tag", "template-v1.0.0")
        git(seed, "checkout", "-q", "main")
        commit(seed, "app.py", "2\n", "two")
        git(seed, "tag", "v1.1.0")
        git(seed, "checkout", "-q", "template")
        commit(seed, "agent.py", "2\n", "template two")
        git(seed, "tag", "template-v1.1.0")
        git(seed, "checkout", "-q", "main")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-q", "origin", "main", "template", "--tags")
        self.seed = seed
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.project)], check=True)
        git(self.project, "reset", "-q", "--hard", "v1.0.0")
        git(self.project, "worktree", "add", "-q", "-b", "template", str(self.project / "template"), "template-v1.0.0")
        self.sup = self.project / ".supervisor"
        self.svc = FakeServices()
        self.open: dict[str, dict] = {}
        self.updated: list[list[str]] = []
        self.update_answer = (0, "alpha: updated from template (abc1234)")
        self.behind = {"alpha": 2}
        patches = [mock.patch.object(core, "PROJECT", self.project), mock.patch.object(core, "SUP_DIR", self.sup),
                   mock.patch.object(core, "log"),
                   mock.patch.object(deploy, "_instances", lambda: sorted(self.behind)),
                   mock.patch.object(deploy, "_behind", lambda n: self.behind[n]),
                   mock.patch.object(deploy, "_update_all", self.fake_update),
                   mock.patch.object(deploy, "_record", self.svc.record),
                   mock.patch.object(deploy, "VERIFY_S", 2), mock.patch.object(deploy, "POLL_S", 0.01),
                   mock.patch.object(runner, "open_conversations", lambda n: self.open.get(n, {})),
                   mock.patch.object(autonomy, "_launchctl", self.svc.launchctl),
                   mock.patch.object(autonomy, "running", lambda n: n in self.svc.running),
                   mock.patch.object(autonomy, "launchd_pid", self.svc.pid),
                   mock.patch.object(autonomy, "restart", self.svc.restart),
                   mock.patch.object(autonomy, "draining_now", lambda: None),
                   mock.patch.object(autonomy, "restart_request", lambda pid=None: self.svc.drain)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_update(self, names):
        self.updated.append(names)
        code, said = self.update_answer
        if not code:
            for n in names:
                self.behind[n] = 0
        return code, said

    def deploy(self, *argv) -> tuple[int, list[dict]]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["deploy", *argv, "--json"])
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def head(self, repo=None) -> str:
        return git(repo or self.project, "rev-parse", "HEAD")

    # -- the steps -----------------------------------------------------------------------------------------------------
    def test_deploy_json_steps_in_order(self):
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 0, rows)
        self.assertEqual([r["step"] for r in rows], list(deploy.STEPS))
        self.assertTrue(all(r["ok"] and r["detail"] and "error" not in r for r in rows), rows)
        self.assertEqual(self.head(), git(self.project, "rev-parse", "v1.1.0^{commit}"))
        self.assertEqual(self.head(self.project / "template"),
                         git(self.project, "rev-parse", "template-v1.1.0^{commit}"))
        self.assertEqual(self.updated, [["alpha"]])
        self.assertEqual(self.svc.restarts, 1)
        self.assertEqual(sorted(self.svc.kicked), ["api", "web"])   # telegram is not loaded: not restarted
        self.assertEqual(rows[6]["restarted"], ["api", "web"])        # the app's cue: its connection is about to drop
        verify = rows[-1]["detail"]
        self.assertIn(f"api pid {self.svc.pids['api']}", verify)
        self.assertIn(f"web pid {self.svc.pids['web']}", verify)
        self.assertIn("the scheduler restarts once its runs end", verify)

    def test_deploy_refuses_a_dirty_tree_with_the_path(self):
        before = self.head()
        (self.project / "app.py").write_text("edited\n")
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 1)
        self.assertEqual([r["step"] for r in rows], ["preflight"])
        self.assertFalse(rows[0]["ok"])
        self.assertIn("app.py", rows[0]["error"])
        self.assertIn("tracked changes", rows[0]["error"])
        self.assertEqual(self.head(), before)
        self.assertEqual((self.project / "app.py").read_text(), "edited\n")

    def test_deploy_refuses_a_dirty_template_with_the_path(self):
        (self.project / "template" / "agent.py").write_text("edited\n")
        code, rows = self.deploy("v1.1.0")
        self.assertEqual((code, rows[-1]["step"]), (1, "preflight"))
        self.assertIn("agent.py", rows[-1]["error"])
        self.assertIn("template worktree", rows[-1]["error"])

    def test_deploy_refuses_a_checkout_off_main(self):
        git(self.project, "checkout", "-q", "-b", "work")
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 1)
        self.assertIn("is on work, not main", rows[0]["error"])

    def test_deploy_moves_an_app_install_detached_at_a_release(self):
        """The Mac app clones `--branch vX.Y.Z`: a detached HEAD at a release tag is its primary checkout."""
        git(self.project, "checkout", "-q", "--detach", "v1.0.0")
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 0, rows)
        self.assertEqual(self.head(), git(self.project, "rev-parse", "v1.1.0^{commit}"))
        self.assertEqual(git(self.project, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")   # still detached
        self.assertEqual(self.deploy("v1.1.0")[0], 0)                                   # and at a release again

    def test_deploy_refuses_a_detached_head_off_a_release(self):
        git(self.project, "checkout", "-q", "--detach", "v1.0.0")
        commit(self.project, "app.py", "bisect\n", "someone's detached work")
        code, rows = self.deploy("v1.1.0")
        self.assertEqual((code, rows[-1]["step"]), (1, "preflight"))
        self.assertIn("a detached HEAD, not main", rows[-1]["error"])

    def test_deploy_never_kicks_api_with_an_open_conversation(self):
        self.open = {"alpha": {"web-1": "api"}}
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 0, rows)
        self.assertEqual([r["step"] for r in rows], list(deploy.STEPS[:-1]))   # it ends at services: no verify yet
        services = rows[-1]
        self.assertTrue(services["ok"] and services["waiting"])
        self.assertEqual(services["detail"], "waiting: alpha (1 open conversation)")
        self.assertEqual(self.svc.kicked, [])
        # The app's Retry once the conversation ended: only services and verify run.
        self.open = {}
        code, rows = self.deploy("v1.1.0", "--services-only")
        self.assertEqual(code, 0, rows)
        self.assertEqual([r["step"] for r in rows], ["services", "verify"])
        self.assertEqual(sorted(self.svc.kicked), ["api", "web"])
        self.assertEqual(self.svc.restarts, 1)   # the scheduler is not asked again

    def test_services_only_refuses_a_checkout_not_at_the_tag(self):
        code, rows = self.deploy("v1.1.0", "--services-only")
        self.assertEqual(code, 1)
        self.assertEqual([r["step"] for r in rows], ["services"])
        self.assertIn("not v1.1.0", rows[0]["error"])
        self.assertEqual(self.svc.kicked, [])

    def test_deploy_refuses_a_non_fast_forward_with_gits_message(self):
        commit(self.project, "app.py", "local\n", "someone's local commit")
        before = self.head()
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 1)
        self.assertEqual([r["step"] for r in rows], ["preflight", "fetch", "main"])
        self.assertIn("cannot fast-forward to v1.1.0", rows[-1]["error"])
        self.assertIn("fast-forward", rows[-1]["error"].split(":", 1)[1].lower())   # git's own words follow
        self.assertEqual(self.head(), before)
        self.assertEqual(self.updated, [])

    def test_deploy_refuses_a_checkout_already_past_the_tag(self):
        commit(self.seed, "app.py", "3\n", "three")
        git(self.seed, "push", "-q", "origin", "main")
        git(self.project, "fetch", "-q", "origin")
        git(self.project, "merge", "-q", "--ff-only", "origin/main")
        code, rows = self.deploy("v1.1.0")
        self.assertEqual((code, rows[-1]["step"]), (1, "main"))
        self.assertIn("already past the release", rows[-1]["error"])

    def test_deploy_refuses_a_release_without_its_template_tag(self):
        commit(self.seed, "app.py", "3\n", "three")
        git(self.seed, "tag", "v1.2.0")
        git(self.seed, "push", "-q", "origin", "main", "--tags")
        code, rows = self.deploy("v1.2.0")
        self.assertEqual(code, 1)
        self.assertEqual([r["step"] for r in rows], ["preflight"])
        self.assertIn("template-v1.2.0 is missing", rows[0]["error"])
        self.assertEqual(self.head(), git(self.project, "rev-parse", "v1.0.0^{commit}"))

    def test_deploy_refuses_an_unknown_tag(self):
        code, rows = self.deploy("v9.9.9")
        self.assertEqual(code, 1)
        self.assertIn("v9.9.9 is not a release tag on origin", rows[0]["error"])

    def test_deploy_stops_at_a_refused_instance_update(self):
        self.update_answer = (1, "alpha: merge conflict with template, nothing changed:\nCONFLICT agent.py")
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]["step"], "update")
        self.assertIn("merge conflict", rows[-1]["error"])
        self.assertEqual((self.svc.restarts, self.svc.kicked), (0, []))

    def test_deploy_rerun_after_success_is_all_ok_and_restarts_nothing(self):
        self.assertEqual(self.deploy("v1.1.0")[0], 0)
        restarts, kicked, updated = self.svc.restarts, list(self.svc.kicked), list(self.updated)
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 0, rows)
        self.assertEqual([r["step"] for r in rows], list(deploy.STEPS))
        self.assertTrue(all(r["ok"] for r in rows), rows)
        self.assertEqual((self.svc.restarts, self.svc.kicked, self.updated), (restarts, kicked, updated))
        details = {r["step"]: r["detail"] for r in rows}
        self.assertIn("already at v1.1.0", details["main"])
        self.assertIn("already at template-v1.1.0", details["template"])
        self.assertIn("already on the template", details["update"])
        self.assertIn("already asked", details["scheduler"])
        self.assertIn("already current", details["services"])
        # Once the scheduler has restarted on the new code, it is simply current.
        self.svc.drain, self.svc.stale["scheduler"] = None, False
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 0)
        self.assertIn("already running v1.1.0", rows[5]["detail"])
        self.assertEqual(self.svc.restarts, restarts)

    def test_deploy_takes_a_release_tag_only(self):
        code, rows = self.deploy("main")
        self.assertEqual(code, 2)
        self.assertFalse(rows[0]["ok"])
        self.assertIn("vX.Y.Z", rows[0]["error"])

    # -- --check -------------------------------------------------------------------------------------------------------
    def check(self) -> dict:
        code, rows = self.deploy("--check")
        self.assertEqual(code, 0, rows)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_check_offers_the_newest_release_with_a_template_tag(self):
        commit(self.seed, "app.py", "9\n", "nine")
        git(self.seed, "tag", "v1.9.0")
        git(self.seed, "checkout", "-q", "template")
        commit(self.seed, "agent.py", "9\n", "template nine")
        git(self.seed, "tag", "template-v1.9.0")
        git(self.seed, "checkout", "-q", "main")
        commit(self.seed, "app.py", "10\n", "ten")
        git(self.seed, "tag", "v1.10.0")                                # newer, numerically, but no template tag yet
        git(self.seed, "push", "-q", "origin", "main", "template", "--tags")
        self.assertEqual(self.check(), {"ok": True, "current": "v1.0.0", "latest": "v1.9.0", "template_tag": False,
                                        "incomplete": None, "incomplete_step": None})
        self.assertEqual(self.deploy("v1.9.0")[0], 0)
        self.assertEqual(self.check(), {"ok": True, "current": "v1.9.0", "latest": None, "template_tag": False,
                                        "incomplete": None, "incomplete_step": None})
        git(self.seed, "checkout", "-q", "template")
        commit(self.seed, "agent.py", "10\n", "template ten")
        git(self.seed, "tag", "template-v1.10.0")
        git(self.seed, "push", "-q", "origin", "template", "--tags")
        self.assertEqual(self.check()["latest"], "v1.10.0")

    def test_check_is_up_to_date_at_the_newest_release(self):
        self.assertEqual(self.deploy("v1.1.0")[0], 0)
        self.assertEqual(self.check(), {"ok": True, "current": "v1.1.0", "latest": None, "template_tag": True,
                                        "incomplete": None, "incomplete_step": None})

    def test_check_names_a_deploy_left_unfinished_and_a_rerun_finishes_it(self):
        """Fix round 1: main moved, then a later step failed. The checkout is at the tag, so `latest` is null, but the
        deploy is not done: --check says so (the app's "Finish updating"), and running it again finishes it."""
        self.update_answer = (1, "alpha: merged template failed the gate; rolled back")
        self.assertEqual(self.deploy("v1.1.0")[0], 1)
        got = self.check()
        self.assertEqual((got["latest"], got["incomplete"], got["incomplete_step"]), (None, "v1.1.0", "update"))
        state = json.loads((self.sup / "deploy-state.json").read_text())
        self.assertEqual(state["steps"]["update"], "failed")
        self.update_answer = (0, "alpha: updated from template (abc1234)")
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 0, rows)
        self.assertEqual((self.check()["incomplete"], self.check()["incomplete_step"]), (None, None))

    def test_check_names_services_left_waiting(self):
        self.open = {"alpha": {"web-1": "api"}}
        self.assertEqual(self.deploy("v1.1.0")[0], 0)
        got = self.check()
        self.assertEqual((got["incomplete"], got["incomplete_step"]), ("v1.1.0", "services"))
        self.open = {}
        self.assertEqual(self.deploy("v1.1.0", "--services-only")[0], 0)
        self.assertIsNone(self.check()["incomplete"])

    def test_check_ignores_a_deploy_the_checkout_has_moved_past(self):
        self.update_answer = (1, "refused")
        self.deploy("v1.1.0")
        git(self.project, "reset", "-q", "--hard", "v1.0.0")   # moved by hand since: not this deploy's business
        self.assertIsNone(self.check()["incomplete"])

    def test_a_rerun_finishes_from_where_it_stopped_without_origin(self):
        """Fix round 2: stopped at services (a conversation open), then origin became unreachable. Running the same
        deploy again resumes at services: the fetches, merges, update and scheduler recorded done (and the checkout
        still at the tags) are not repeated, so it finishes."""
        self.open = {"alpha": {"web-1": "api"}}
        self.assertEqual(self.deploy("v1.1.0")[0], 0)
        state = json.loads((self.sup / "deploy-state.json").read_text())
        self.assertEqual([k for k, v in state["steps"].items() if v == "ok"],
                         ["preflight", "fetch", "main", "template", "update", "scheduler"])
        git(self.project, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        self.open = {}
        self.svc.drain = None                                  # the scheduler has not drained yet: not asked again
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 0, rows)
        self.assertEqual([r["step"] for r in rows], list(deploy.STEPS))
        self.assertTrue(all(r["ok"] for r in rows), rows)
        self.assertIn("finishing the deploy from its services step", rows[0]["detail"])
        self.assertIn("done by the deploy this finishes", rows[1]["detail"])          # fetch: origin not asked
        self.assertEqual((self.updated, self.svc.restarts), ([["alpha"]], 1))           # update and drain not redone
        self.assertEqual(sorted(self.svc.kicked), ["api", "web"])
        self.assertIsNone(self.check_local()["incomplete"])

    def test_a_retried_verify_checks_the_services_the_deploy_restarted(self):
        """Fix round 3: web never came back after its restart, so verify failed. A retry resumes at verify; it must
        check web again (its old PID is kept in deploy-state.json), failing with the real reason until web runs a new
        process, and never pass as "nothing to verify"."""
        real_kick = self.svc.launchctl

        def kick_web_down(*args):
            answer = real_kick(*args)
            if args[0] == "kickstart" and args[2].endswith("com.shaft.web"):
                self.svc.down.add("web")
            return answer
        with mock.patch.object(autonomy, "_launchctl", kick_web_down):
            code, rows = self.deploy("v1.1.0")
        self.assertEqual((code, rows[-1]["step"]), (1, "verify"))
        self.assertIn("web: no new process recorded itself", rows[-1]["error"])
        state = json.loads((self.sup / "deploy-state.json").read_text())
        self.assertEqual(sorted(state["kicked"]), ["api", "web"])
        self.assertEqual(self.check()["incomplete_step"], "verify")
        code, rows = self.deploy("v1.1.0")                      # the retry: still down
        self.assertEqual((code, rows[-1]["step"]), (1, "verify"))
        self.assertIn("done by the deploy this finishes", rows[6]["detail"])      # services not restarted again
        self.assertIn("web: no new process recorded itself", rows[-1]["error"])
        self.assertEqual(sorted(self.svc.kicked), ["api", "web"])
        self.svc.down.clear()                                   # web comes up (launchd's KeepAlive, by hand)
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 0, rows)
        self.assertIn(f"web pid {self.svc.pids['web']}", rows[-1]["detail"])
        self.assertIsNone(self.check()["incomplete"])

    def test_a_retried_verify_without_the_record_checks_every_loaded_service(self):
        """A deploy-state.json from before the restarted services were kept: verify checks each loaded service it
        would restart (api, web; telegram is not loaded) runs on the checked-out code."""
        self.svc.stale.update(api=False, web=False)
        self.svc.down.add("web")
        self.open = {"alpha": {"web-1": "api"}}
        self.deploy("v1.1.0")                                   # waits at services
        state = json.loads((self.sup / "deploy-state.json").read_text())
        state["steps"]["services"] = "ok"
        del state["kicked"]
        (self.sup / "deploy-state.json").write_text(json.dumps(state))
        code, rows = self.deploy("v1.1.0")
        self.assertEqual((code, rows[-1]["step"]), (1, "verify"))
        self.assertIn("web: it is not running", rows[-1]["error"])
        self.svc.down.clear()
        code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 0, rows)
        self.assertIn("api pid", rows[-1]["detail"])
        self.assertIn("web pid", rows[-1]["detail"])

    def test_a_rerun_after_the_checkout_moved_runs_every_step(self):
        """deploy-state.json is not trusted blindly: once the checkout no longer shows what it records, every step runs
        again (here the fetch, which fails on the unreachable origin, instead of skipping to services)."""
        self.open = {"alpha": {"web-1": "api"}}
        self.deploy("v1.1.0")
        git(self.project, "reset", "-q", "--hard", "v1.0.0")
        git(self.project, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        code, rows = self.deploy("v1.1.0")
        self.assertEqual((code, [r["step"] for r in rows]), (1, ["preflight"]))
        self.assertIn("git fetch origin --tags failed", rows[0]["error"])

    def check_local(self) -> dict:
        code, rows = self.deploy("--check", "--local")
        self.assertEqual((code, len(rows)), (0, 1), rows)
        return rows[0]

    def test_a_local_check_needs_no_origin(self):
        self.open = {"alpha": {"web-1": "api"}}
        self.deploy("v1.1.0")
        git(self.project, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        self.assertEqual(self.check_local(), {"ok": True, "current": "v1.1.0", "latest": None, "template_tag": None,
                                              "incomplete": "v1.1.0", "incomplete_step": "services"})

    # -- the restart barrier ----------------------------------------------------------------------------------------------
    def test_no_conversation_opens_between_the_look_and_the_restart(self):
        """Fix round 1: from the look at the open conversations until the old process is gone, a new conversation is
        refused ("restarting, try again"), so the kickstart can never cut one that opened in between."""
        tried = []

        def open_one(name):
            try:
                with core.admitting():
                    tried.append("admitted")
            except core.Restarting as e:
                tried.append(str(e))

        def looked(name):
            open_one(name)                # a conversation trying to open right after the look
            return {}
        real_kick = self.svc.launchctl

        def kick(*args):
            open_one("alpha")             # and right before the kickstart
            return real_kick(*args)
        with mock.patch.object(runner, "open_conversations", looked), mock.patch.object(autonomy, "_launchctl", kick):
            code, rows = self.deploy("v1.1.0")
        self.assertEqual(code, 0, rows)
        self.assertEqual(sorted(self.svc.kicked), ["api", "web"])
        self.assertTrue(tried)
        self.assertEqual(set(tried), {core.RESTARTING_SAID})
        with core.admitting():            # once the services are replaced, conversations open again
            pass

    def test_a_terminal_conversation_does_not_register_during_the_restart(self):
        with mock.patch.object(runner, "SUP_DIR", self.sup):
            with core.restart_barrier(wait_s=1), self.assertRaises(core.Restarting):
                with runner.Presence("alpha", "talk-1"):
                    pass
            self.assertEqual(runner._held(self.sup / "conversations" / "alpha"), {})
            with runner.Presence("alpha", "talk-2"):     # after it: as before
                self.assertIn("talk-2", runner._held(self.sup / "conversations" / "alpha"))
            with core.restart_barrier(wait_s=1), runner.Reply("alpha", "web"):
                pass                                     # a reply in an open conversation is not a new one

    def test_a_registering_conversation_holds_the_restart_back(self):
        """The other order: a conversation registering holds its share, so the deploy cannot look meanwhile."""
        with core.admitting(), self.assertRaises(TimeoutError):
            with core.restart_barrier(wait_s=0.2):
                pass

    def test_check_reports_an_unreachable_origin(self):
        git(self.project, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        code, rows = self.deploy("--check")
        self.assertEqual(code, 1)
        self.assertFalse(rows[0]["ok"])
        self.assertIn("ls-remote", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
