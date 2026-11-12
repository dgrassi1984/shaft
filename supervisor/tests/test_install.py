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


class StepsTest(unittest.TestCase):
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

    def test_a_step_that_returns_nonzero_is_a_failure_with_its_output(self):
        def runner(step, operator, email):
            if step == "api":
                raise install.StepFailed("api refused: nothing to add")
            return "ok"
        code, lines = run_install(runner)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], {"step": "api", "ok": False, "detail": "api refused: nothing to add"})


class DefaultRunnerTest(unittest.TestCase):
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


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-install-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.glob = self.tmp / "global-gitconfig"
        self.glob.write_text("")
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.glob), "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        proj = mock.patch.object(core, "PROJECT", self.repo)
        proj.start()
        self.addCleanup(proj.stop)

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


class RerunTest(unittest.TestCase):
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


class ServicePathTest(unittest.TestCase):
    def plist_path(self, with_runtime: bool) -> str:
        tmp = Path(tempfile.mkdtemp(prefix="shaft-install-svc-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        if with_runtime:
            (tmp / ".runtime" / "bin").mkdir(parents=True)
        agents = tmp / "agents"
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(autonomy, "PROJECT", tmp), mock.patch.object(autonomy, "AGENTS", agents), \
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


class SetupFailureTest(unittest.TestCase):
    def test_cmd_setup_returns_1_with_gits_stderr_instead_of_a_traceback(self):
        err = subprocess.CalledProcessError(128, ["git"], stderr="fatal: not a git repository")
        with mock.patch.object(cli, "git", side_effect=err), contextlib.redirect_stderr(io.StringIO()) as e:
            self.assertEqual(cli.cmd_setup(), 1)
        self.assertIn("fatal: not a git repository", e.getvalue())


if __name__ == "__main__":
    unittest.main()
