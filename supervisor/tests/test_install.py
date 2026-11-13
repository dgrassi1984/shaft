"""`shaft install --json`: the fresh-Mac steps the Mac app runs after it cloned the server. Nothing here touches
launchd, Tailscale or the real project: the steps run through an injected runner, or against throwaway paths."""
from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import __main__ as cli
from supervisor import autonomy, core, install


def run_install(runner, operator="Ada", email=None):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = install.run(operator, email, runner=runner)
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


class _RealLaunchctl:
    """autonomy.subprocess in these tests: anything that would reach the real launchctl binary fails loudly."""

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, argv, *a, **k):
        if argv and Path(str(argv[0])).name == "launchctl":
            raise AssertionError(f"the real launchctl was invoked: {argv}")
        return subprocess.run(argv, *a, **k)


class Hermetic(unittest.TestCase):
    """Every test here runs on throwaway state: its own project (a git repo), .supervisor, LaunchAgents folder, global
    git config and a fake launchctl, so the outcome never depends on this Mac's .env or on which services are loaded."""

    def setUp(self):
        from supervisor import api
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-install-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.project, self.agents, self.sup = self.tmp / "p", self.tmp / "agents", self.tmp / "p" / ".supervisor"
        self.project.mkdir()
        self.calls, self.loaded = [], set()
        self.glob = self.tmp / "gitconfig"
        self.glob.write_text("")
        (self.tmp / "git-templates").mkdir()
        # git's isolation comes first, so the `git init` below never reads the operator's init.templateDir or hooks
        env = {"GIT_CONFIG_GLOBAL": str(self.glob), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_TEMPLATE_DIR": str(self.tmp / "git-templates")}
        git_env = mock.patch.dict(os.environ, env)
        git_env.start()
        self.addCleanup(git_env.stop)
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        patches = [mock.patch.object(core, "PROJECT", self.project), mock.patch.object(core, "SUP_DIR", self.sup),
                   mock.patch.object(core, "SETTINGS_FILE", self.sup / "settings.env"),
                   mock.patch.object(autonomy, "PROJECT", self.project), mock.patch.object(autonomy, "SUP_DIR", self.sup),
                   mock.patch.object(autonomy, "AGENTS", self.agents), mock.patch.object(autonomy, "log"),
                   mock.patch.object(autonomy, "subprocess", _RealLaunchctl()),
                   mock.patch.object(autonomy, "_launchctl", self.launchctl),
                   mock.patch.object(api, "_tailscale", side_effect=AssertionError("tailscale")),
                   mock.patch.object(cli, "TEMPLATE", self.project / "template"),
                   mock.patch.object(cli, "INSTANCES", self.project / "instances")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for k in ("SHAFT_API_LOGINS", "SHAFT_API_KEY", "SHAFT_INBOX_KEY", "SHAFT_WEB_LOCAL"):
            os.environ.pop(k, None)

    def launchctl(self, *args):
        self.calls.append(args)
        label = args[1].rsplit("/", 1)[-1] if len(args) > 1 else ""
        if args[0] == "bootstrap":
            self.loaded.add(Path(args[2]).stem)
        elif args[0] == "bootout":
            self.loaded.discard(label)
        rc = 0 if args[0] != "print" or label in self.loaded else 113
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="")

    def test_the_real_launchctl_cannot_be_reached(self):
        with self.assertRaisesRegex(AssertionError, "real launchctl"):
            autonomy.subprocess.run(["launchctl", "print", "x"])


class StepsTest(Hermetic):
    def test_steps_run_in_order_and_report_json(self):
        seen = []

        def runner(step, operator, email):
            seen.append((step, operator, email))
            return f"{step} done"
        code, lines = run_install(runner, "Ada", "ada@example.com")
        self.assertEqual(code, 0)
        self.assertEqual([l["step"] for l in lines], ["identity", "setup", "api", "web", "scheduler"])
        self.assertEqual(lines[0], {"step": "identity", "ok": True, "detail": "identity done"})
        self.assertEqual([s[0] for s in seen], ["identity", "setup", "api", "web", "scheduler"])
        self.assertEqual(seen[0][1:], ("Ada", "ada@example.com"))

    def test_a_failed_step_stops_and_names_the_real_error(self):
        seen = []

        def runner(step, operator, email):
            seen.append(step)
            if step == "setup":
                raise subprocess.CalledProcessError(128, ["git"], stderr="fatal: not a git repository")
            return "ok"
        code, lines = run_install(runner)
        self.assertEqual(code, 1)
        self.assertEqual(seen, ["identity", "setup"])               # api, web, scheduler never ran
        self.assertEqual([l["ok"] for l in lines], [True, False])
        self.assertIn("fatal: not a git repository", lines[1]["detail"])
        self.assertEqual(lines[1]["error"], "fatal: not a git repository")   # the contract: "error" holds the message
        self.assertNotIn("error", lines[0])

    def test_a_step_that_returns_nonzero_is_a_failure_with_its_output(self):
        def runner(step, operator, email):
            if step == "api":
                raise install.StepFailed("api refused: nothing to add")
            return "ok"
        code, lines = run_install(runner)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], {"step": "api", "ok": False, "detail": "api refused: nothing to add",
                                    "error": "api refused: nothing to add"})


class DefaultRunnerTest(Hermetic):
    """The real steps, in process, with every side effect patched out."""

    def test_the_default_runner_calls_the_real_functions_and_keeps_stdout_clean(self):
        calls = []

        def talk(name):
            def f(*a, **k):
                calls.append((name, a, k))
                print(f"{name} chatter")   # the verbs print: that must not reach the JSON lines
                return 0
            return f
        with mock.patch.object(install, "_identity", return_value="kept"), \
                mock.patch.object(cli, "cmd_setup", talk("setup")), \
                mock.patch("supervisor.api.command", talk("api")), \
                mock.patch("supervisor.web.command", talk("web")), \
                mock.patch.object(autonomy, "autonomy", talk("scheduler")):
            code, lines = run_install(None)
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in calls], ["setup", "api", "web", "scheduler"])
        self.assertEqual(calls[1][1:], (("on",), {"local": True}))
        self.assertEqual(calls[2][1:], (("on",), {"local": True}))
        self.assertEqual(calls[3][1], ("on",))
        self.assertEqual(len(lines), 5)

    def test_a_nonzero_return_fails_the_step_with_what_it_printed(self):
        def bad(*a, **k):
            print("web: operator@local is not an allowed login", file=__import__("sys").stderr)
            return 1
        with mock.patch.object(install, "_identity", return_value="kept"), \
                mock.patch.object(cli, "cmd_setup", return_value=0), \
                mock.patch("supervisor.api.command", return_value=0), \
                mock.patch("supervisor.web.command", bad), \
                mock.patch.object(autonomy, "autonomy") as sched:
            code, lines = run_install(None)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1]["step"], "web")
        self.assertIn("not an allowed login", lines[-1]["detail"])
        sched.assert_not_called()


class IdentityTest(Hermetic):
    def setUp(self):
        super().setUp()
        self.repo = self.project

    def cfg(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "config", *args], capture_output=True, text=True).stdout.strip()

    def test_identity_is_repo_local_and_never_global(self):
        detail = install._identity("Ada", "ada@example.com")
        self.assertEqual(self.cfg("--local", "user.name"), "Ada")
        self.assertEqual(self.cfg("--local", "user.email"), "ada@example.com")
        self.assertEqual(self.glob.read_text(), "")                 # the global file is untouched
        self.assertIn("Ada", detail)

    def test_the_default_email_is_user_at_host_local(self):
        with mock.patch("getpass.getuser", return_value="ada"), mock.patch("socket.gethostname", return_value="mbp"):
            install._identity("Ada", None)
        self.assertEqual(self.cfg("--local", "user.email"), "ada@mbp.local")

    def test_a_global_identity_is_kept_and_nothing_is_written(self):
        self.glob.write_text("[user]\n\tname = Someone\n\temail = s@example.com\n")
        self.assertEqual(install._identity("Ada", None), "kept")
        self.assertEqual(self.cfg("--local", "user.name"), "")

    def test_a_rerun_keeps_the_identity_it_set(self):
        install._identity("Ada", "ada@example.com")
        self.assertEqual(install._identity("Bob", "bob@example.com"), "kept")
        self.assertEqual(self.cfg("--local", "user.name"), "Ada")


class RerunTest(Hermetic):
    def test_a_rerun_after_success_is_all_ok_and_changes_nothing(self):
        # Review Focus 1: the app may run install twice (a retry after a later failure); the second run must not
        # duplicate logins or rewrite the identity. State is kept by a fake that mirrors the real idempotence.
        state = {"logins": [], "identity": None, "writes": 0}

        def runner(step, operator, email):
            if step == "identity":
                if state["identity"]:
                    return "kept"
                state["identity"], state["writes"] = operator, state["writes"] + 1
                return "set"
            if step == "api" and "operator@local" not in state["logins"]:
                state["logins"].append("operator@local")
                state["writes"] += 1
            return "ok"
        first, _ = run_install(runner)
        writes = state["writes"]
        second, lines = run_install(runner)
        self.assertEqual((first, second), (0, 0))
        self.assertTrue(all(l["ok"] for l in lines))
        self.assertEqual(state["writes"], writes)
        self.assertEqual(state["logins"], ["operator@local"])


class DefaultRunnerRerunTest(Hermetic):
    """Review: a rerun must not restart services that are already right (a restarted scheduler cuts running cycles).
    The default runner twice over isolated state; only launchctl and tailscale are faked."""

    def snapshot(self):
        files = [p for p in (list(self.agents.glob("*.plist")) + [self.project / ".env", self.sup / "settings.env",
                                                                  self.project / ".git" / "config"]) if p.exists()]
        # content AND file identity: an atomic rewrite with the same bytes (os.replace) still changes the inode/mtime
        return {str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in files}

    def test_a_second_default_run_writes_nothing_and_restarts_nothing(self):
        first, lines1 = run_install(None, "Ada", "ada@example.com")
        self.assertEqual(first, 0, lines1)
        self.assertEqual([l["step"] for l in lines1], list(install.STEPS))
        self.assertEqual(len(list(self.agents.glob("*.plist"))), 3)       # api, web, scheduler
        before, self.calls[:] = self.snapshot(), []
        second, lines2 = run_install(None, "Ada", "ada@example.com")
        self.assertEqual(second, 0, lines2)
        self.assertTrue(all(l["ok"] for l in lines2))
        self.assertEqual([l["detail"] for l in lines2[2:]], ["running"] * 3)
        self.assertEqual(self.snapshot(), before)                                   # no config or plist rewritten
        self.assertEqual({c[0] for c in self.calls} & {"bootout", "bootstrap", "kickstart"}, set())
        logins = [l for l in (self.project / ".env").read_text().splitlines() if l.startswith("SHAFT_API_LOGINS")]
        self.assertEqual(logins, ["SHAFT_API_LOGINS=operator@local"])

    def test_a_service_that_is_not_loaded_or_whose_plist_differs_is_installed_again(self):
        run_install(None, "Ada", "ada@example.com")
        self.loaded.discard("com.shaft.scheduler")                 # launchd lost it
        (self.agents / "com.shaft.web.plist").write_bytes(plistlib.dumps({"Label": "com.shaft.web"}))   # stale plist
        self.calls[:] = []
        code, lines = run_install(None, "Ada", "ada@example.com")
        self.assertEqual(code, 0, lines)
        self.assertEqual([l["detail"] == "running" for l in lines[2:]], [True, False, False])
        self.assertIn("bootstrap", {c[0] for c in self.calls})
        self.assertTrue(autonomy.service_current("web") and autonomy.service_current("scheduler"))


class ServicePathTest(Hermetic):
    def plist_path(self, with_runtime: bool) -> str:
        tmp = Path(tempfile.mkdtemp(prefix="shaft-install-svc-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        if with_runtime:
            (tmp / ".runtime" / "bin").mkdir(parents=True)
        agents = tmp / "agents"
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(autonomy, "PROJECT", tmp), mock.patch.object(core, "RUNTIME", tmp / ".runtime"), \
                mock.patch.object(autonomy, "AGENTS", agents), \
                mock.patch.object(autonomy, "SUP_DIR", tmp / ".supervisor"), mock.patch.object(autonomy, "log"), \
                mock.patch.object(autonomy, "_launchctl", return_value=ok), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(autonomy.service("scheduler", "on"), 0)
        (plist,) = agents.glob("*.plist")
        return plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["PATH"], str(tmp)

    def test_services_get_the_runtime_bin_on_PATH(self):
        path, root = self.plist_path(True)
        self.assertEqual(path.split(":")[0], f"{root}/.runtime/bin")     # where uv lives, so runs find it
        self.assertIn("/opt/homebrew/bin", path.split(":"))

    def test_without_a_runtime_folder_the_PATH_is_unchanged(self):
        path, _ = self.plist_path(False)
        self.assertEqual(path, "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")


class SetupFailureTest(Hermetic):
    def test_cmd_setup_returns_1_with_gits_stderr_instead_of_a_traceback(self):
        err = subprocess.CalledProcessError(128, ["git"], stderr="fatal: not a git repository")
        with mock.patch.object(cli, "git", side_effect=err), contextlib.redirect_stderr(io.StringIO()) as e:
            self.assertEqual(cli.cmd_setup(), 1)
        self.assertIn("fatal: not a git repository", e.getvalue())


if __name__ == "__main__":
    unittest.main()
