"""supervisor/migrate.py: the guided migration commands (docs/migration.md).

Every external command goes through an injected runner (FakeRunner); steps that need real files use temporary
projects with core.PROJECT / core.SUP_DIR / core.HOME pointed at them. One end-to-end test drives a real copy with
real git and rsync and a stand-in ssh.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import core, migrate


def cp(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out, err)


class FakeRunner(migrate.Runner):
    """Records argv and returns scripted results; `script` maps a substring of the joined argv to a result."""

    def __init__(self):
        super().__init__()
        self.calls: list[list[str]] = []
        self.script: dict[str, subprocess.CompletedProcess] = {}
        self.interactive_codes: dict[str, int] = {}
        self.talk_codes: dict[str, int] = {}

    def _answer(self, argv):
        key = " ".join(str(a) for a in argv)
        for sub, result in self.script.items():
            if sub in key:
                return result
        return cp(0)

    def run(self, argv, *, cwd=None, env=None, input=None, live=False):
        self.calls.append([str(a) for a in argv])
        return self._answer(argv)

    def connect(self, host):
        self.calls.append(["connect", host])
        return cp(0)

    def interactive(self, argv, *, cwd=None):
        self.calls.append(["interactive", *[str(a) for a in argv]])
        return self.interactive_codes.get(" ".join(str(a) for a in argv), 0)

    def talk(self, name):
        self.calls.append(["talk", name])
        return self.talk_codes.get(name, 0)


class MigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.sup = self.project / ".supervisor"
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
        self.project.mkdir(parents=True, exist_ok=True)
        self.sup.mkdir(parents=True, exist_ok=True)
        self.patchers = [
            mock.patch.object(core, "PROJECT", self.project),
            mock.patch.object(core, "HOME", self.home),
            mock.patch.object(core, "SUP_DIR", self.sup),
            mock.patch.object(core, "INSTANCES", self.project / "instances"),
            mock.patch.object(core, "TEMPLATE", self.project / "template"),
            mock.patch.object(migrate, "_service_running", return_value=True, create=True),
        ]
        for p in self.patchers:
            p.start()
        self.runner = FakeRunner()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mig(self, stdin=None, check=False, host=None, tty=True):
        m = migrate._Mig(runner=self.runner, stdin=migrate._wrap_stdin(stdin), tty=tty, check=check, host=host)
        m.state_dir = core.SUP_DIR / "migration"
        return m

    def plist(self, name, working_dir=None):
        p = self.home / "Library" / "LaunchAgents" / f"com.shaft.{name}.plist"
        data = {"Label": f"com.shaft.{name}",
                "WorkingDirectory": working_dir if working_dir is not None else str(self.project)}
        p.write_bytes(plistlib.dumps(data))
        return p


class ShaftProcessTest(unittest.TestCase):
    def test_python_first_token_and_word_boundary(self):
        lines = [
            "  123 /usr/bin/python3 -E -s -m supervisor autonomy serve",
            "  456 /opt/homebrew/opt/python@3.12/Frameworks/Python.framework/Versions/3.12/Resources/"
            "Python.app/Contents/MacOS/Python -m supervisor web serve",
            "  789 /bin/zsh -c 'echo -m supervisor'",
            "   10 /usr/bin/python3 -m supervisor.talk_ui wren",
            "   11 /usr/bin/python3 -m supervisorfoo",
            "   12 /bin/sh -c shaft-codex-app-server",
            "   13 /usr/bin/python3 -m supervisor.top",
            "   14 /usr/bin/python3 -m supervisor.embed_server",
        ]
        found = migrate._shaft_processes(lines)
        self.assertEqual([pid for pid, _ in found], [123, 456, 10, 12, 13, 14])
        self.assertNotIn(789, [pid for pid, _ in found])   # zsh: first token is not python
        self.assertNotIn(11, [pid for pid, _ in found])    # not a word boundary

    def test_excludes_this_process_and_its_parent(self):
        lines = [f"  {os.getpid()} /usr/bin/python3 -m supervisor migrate source",
                 f"  {os.getppid()} /usr/bin/python3 -m supervisor migrate source",
                 f"  999 /usr/bin/python3 -m supervisor autonomy serve"]
        self.assertEqual([pid for pid, _ in migrate._shaft_processes(lines, exclude={os.getpid(), os.getppid()})],
                         [999])


class LiveCheckoutTest(MigCase):
    def test_refuses_a_worktree_run(self):
        self.plist("web", working_dir="/Users/someone/Development/shaft")
        with self.assertRaises(migrate.Stop):
            migrate._check_live_checkout()

    def test_passes_when_plists_point_here(self):
        self.plist("web")
        self.plist("scheduler")
        migrate._check_live_checkout()


class InstalledServicesTest(MigCase):
    def test_order_and_verb_mapping(self):
        self.plist("web")
        self.plist("model")
        self.plist("scheduler")
        self.plist("tick")   # legacy: ignored
        self.assertEqual(migrate._installed_services(), ["autonomy", "web", "model"])

    def test_none(self):
        self.assertEqual(migrate._installed_services(), [])


class SchedulerIdleTest(MigCase):
    def test_no_scheduler_returns_empty(self):
        self.runner.script = {"ps -axo pid=,command=": cp(0, "")}
        self.assertEqual(migrate._scheduler_children(self.runner), [])

    def test_scheduler_without_children_returns_empty(self):
        self.runner.script = {
            "ps -axo pid=,command=": cp(0, "  100 /usr/bin/python3 -m supervisor autonomy serve\n"),
            "ps -o pgid= -p 100": cp(0, "200\n"),
            "pgrep -g 200": cp(0, "100\n"),
        }
        self.assertEqual(migrate._scheduler_children(self.runner), [])

    def test_scheduler_has_children(self):
        self.runner.script = {
            "ps -axo pid=,command=": cp(0, "  100 /usr/bin/python3 -m supervisor autonomy serve\n"
                                           "  101 /usr/bin/python3 -m supervisor embed serve\n"),
            "ps -o pgid= -p 100": cp(0, "200\n"),
            "pgrep -g 200": cp(0, "100\n101\n"),
        }
        self.assertEqual(migrate._scheduler_children(self.runner), ["101"])

    def test_zsh_mentioning_the_string_does_not_hide_children(self):
        # a zsh line that merely mentions the string, with a lower pid than the scheduler,
        # must not be mistaken for the scheduler or hide its children
        self.runner.script = {
            "ps -axo pid=,command=": cp(0, "   50 /bin/zsh -c 'echo supervisor autonomy serve'\n"
                                           "  100 /usr/bin/python3 -m supervisor autonomy serve\n"
                                           "  101 /usr/bin/python3 -m supervisor embed serve\n"),
            "ps -o pgid= -p 100": cp(0, "200\n"),
            "pgrep -g 200": cp(0, "100\n101\n"),
        }
        self.assertEqual(migrate._scheduler_children(self.runner), ["101"])

    def test_unreadable_pgid_is_a_blocker(self):
        self.runner.script = {
            "ps -axo pid=,command=": cp(0, "  100 /usr/bin/python3 -m supervisor autonomy serve\n"),
            "ps -o pgid= -p 100": cp(0, ""),
        }
        self.assertEqual(migrate._scheduler_children(self.runner), ["unreadable pgid for scheduler 100"])

    def test_idle_waits_for_busy_update_and_conversations(self):
        self.runner.script = {"ps -axo pid=,command=": cp(0, "")}
        with mock.patch.object(core, "instance_names", return_value=["shaft"]), \
                mock.patch("supervisor.runner.is_busy", side_effect=[True, False]), \
                mock.patch.object(core, "update_queued", side_effect=[None, None]), \
                mock.patch("supervisor.runner.open_conversations", return_value={}):
            self.assertFalse(migrate._idle(self.runner))
            self.assertTrue(migrate._idle(self.runner))


class PauseAndWaitTest(MigCase):
    def test_was_paused_read_before_pause(self):
        (self.sup / "paused").write_text("2026-11-05T00:00:00")
        self.runner.script = {"ps -axo pid=,command=": cp(0, "")}
        m = self.mig()
        with mock.patch.object(core, "instance_names", return_value=[]):
            migrate._pause_and_wait(m)
        self.assertTrue(m.state["was_paused"])                    # read before pause created the flag
        self.assertTrue((self.sup / "paused").exists())           # pause kept it (never overwrote it)


class StopServiceTest(MigCase):
    def test_retry_then_force_on_autonomy(self):
        self.runner.script = {"autonomy off --force": cp(0, "stopped\n")}
        self.runner.script.setdefault("bin/shaft autonomy off", cp(0))
        # first `off` prints not now; retry; then force
        calls = []

        def off(argv):
            if argv[-2:] == ["autonomy", "off"]:
                return cp(1, "not now: a cycle is running\n")
            return cp(0)

        # simpler: drive _stop_service with stdin forcing
        m = self.mig(stdin=["force"])
        with mock.patch.object(migrate.Runner, "shaft", autospec=True) as shaft:
            shaft.side_effect = [cp(1, "not now: a cycle is running\n"), cp(0, "stopped\n")]
            migrate._stop_service(m, "autonomy")
            self.assertEqual(shaft.call_args_list[-1], mock.call(m.runner, "autonomy", "off", "--force"))

    def test_web_never_passes_force(self):
        m = self.mig(stdin=["force", "q"])
        with mock.patch.object(migrate.Runner, "shaft", autospec=True) as shaft:
            shaft.side_effect = [cp(1, "not now: runs need it\n"), cp(1, "not now: runs need it\n")]
            with self.assertRaises(migrate.Stop):
                migrate._stop_service(m, "web")
            for call in shaft.call_args_list:
                self.assertNotIn("--force", call.args[1:])
            self.assertEqual([c.args[1:] for c in shaft.call_args_list],
                             [("web", "off"), ("web", "off")])

    def test_force_flag_set_is_exactly_the_four_verbs(self):
        self.assertEqual(migrate.FORCE_OK, {"autonomy", "api", "embed", "model"})


class ManifestTest(MigCase):
    def test_manifest_fields(self):
        with mock.patch.object(core, "instance_names", return_value=["wren"]), \
                mock.patch.object(core, "shared_folder", return_value=Path("/Users/x/Documents")), \
                mock.patch.object(core, "git", return_value=cp(0, "template abc123\ninstance/wren def456\n")), \
                mock.patch("supervisor.mind.head", return_value="mh789"), \
                mock.patch.object(migrate, "_tailscale_json",
                                  return_value={"MagicDNSSuffix": "ts.net", "Self": {"DNSName": "old.ts.net."}}):
            m = self.mig()
            manifest = migrate._manifest(m, ["autonomy", "web"])
        self.assertEqual(manifest["services"], ["autonomy", "web"])
        self.assertEqual(manifest["branches"], {"template": "abc123", "instance/wren": "def456"})
        self.assertEqual(manifest["minds"], {"wren": "mh789"})
        self.assertEqual(manifest["shared"], {"wren": "/Users/x/Documents"})
        self.assertEqual(manifest["tailnet"]["suffix"], "ts.net")
        self.assertEqual(manifest["browsers"], False)


class CopyTest(MigCase):
    def test_project_rsync_excludes_and_marker(self):
        (self.project / ".venv-ui").mkdir()
        (self.project / "src.txt").write_text("x")
        m = self.mig(host="new")
        with mock.patch.object(migrate, "_require_source_stopped"), \
                mock.patch.object(migrate, "_check_new_mac"), \
                mock.patch.object(migrate, "_remote_home", return_value="/new/home"), \
                mock.patch.object(migrate, "_dest", return_value="/new/home/project"), \
                mock.patch.object(core, "instance_names", return_value=[]):
            migrate._copy(m)
        mkdir = next(c for c in self.runner.calls if c[0] == "ssh")
        self.assertEqual(mkdir[-1], "mkdir -p /new/home/project/.supervisor/migration/incoming")
        project_rsync = next(c for c in self.runner.calls if c[0] == "rsync" and "--delete" in c)
        self.assertIn("--delete", project_rsync)
        self.assertIn("--stats", project_rsync)
        for e in ("--exclude=/.venv-ui", "--exclude=/.venv-bonsai", "--exclude=/.venv-ft", "--exclude=/.browsers",
                  "--exclude=/.browsers.new", "--exclude=/.browsers.old",
                  "--exclude=/.supervisor/migration/incoming",
                  "--exclude=/.supervisor/migration/source-state.json"):
            self.assertIn(e, project_rsync)
        self.assertEqual(project_rsync[-2], str(self.project) + "/")
        self.assertEqual(project_rsync[-1], "new:/new/home/project/")

    def test_application_support_and_shared_have_no_delete(self):
        (self.home / "Library" / "Application Support" / "SHAFT").mkdir(parents=True)
        shared = self.tmp / "Shared"
        shared.mkdir()
        m = self.mig(host="new")
        with mock.patch.object(migrate, "_require_source_stopped"), \
                mock.patch.object(migrate, "_check_new_mac"), \
                mock.patch.object(migrate, "_remote_home", return_value="/new/home"), \
                mock.patch.object(migrate, "_dest", return_value="/new/home/project"), \
                mock.patch.object(core, "instance_names", return_value=["wren"]), \
                mock.patch.object(core, "shared_folder", return_value=str(shared)), \
                mock.patch.object(migrate, "_map_to_remote", return_value="/new/home/Documents/Shared"):
            migrate._copy(m)
        rsyncs = [c for c in self.runner.calls if c[0] == "rsync"]
        self.assertEqual(len(rsyncs), 3)
        for c in rsyncs[1:]:
            self.assertNotIn("--delete", c)
        # the Application Support parent is created before the rsync (rsync creates only one level)
        mkdirs = [c[-1] for c in self.runner.calls if c[0] == "ssh" and c[-1].startswith("mkdir -p")]
        self.assertTrue(any("Application Support" in m for m in mkdirs))
        self.assertTrue(any("Documents" in m for m in mkdirs))


class TargetRefuseTest(MigCase):
    def test_refuses_without_manifest(self):
        self.assertEqual(migrate._target(self.mig()), 1)

    def test_check_writes_nothing(self):
        (self.sup / "migration").mkdir(parents=True)
        m = self.mig(check=True)
        migrate._check_live_checkout()
        self.assertFalse((self.sup / "migration" / "manifest.json").exists())


class RepairTest(MigCase):
    def test_path_change_repairs_and_removes_instance_env(self):
        inst = self.project / "instances" / "wren"
        (inst / ".venv").mkdir(parents=True)
        (inst / ".uv").mkdir(parents=True)
        m = self.mig()
        m.manifest = {"project": "/old/path"}
        with mock.patch.object(core, "instance_names", return_value=["wren"]), \
                mock.patch.object(core, "git", return_value=cp(0)) as git:
            migrate._repair(m)
        self.assertIn(mock.call("worktree", "repair", "template", "instances/wren", check=False), git.call_args_list)
        self.assertFalse((inst / ".venv").exists())
        self.assertFalse((inst / ".uv").exists())

    def test_refuses_removal_outside_instances(self):
        outside = self.tmp / "elsewhere"
        (outside / ".venv").mkdir(parents=True)
        # a symlinked .venv pointing outside instances must be refused, not followed
        inst = self.project / "instances" / "wren"
        inst.mkdir(parents=True)
        (inst / ".venv").symlink_to(outside / ".venv")
        m = self.mig()
        m.manifest = {"project": "/old/path"}
        with mock.patch.object(core, "instance_names", return_value=["wren"]), \
                mock.patch.object(core, "git", return_value=cp(0)):
            with self.assertRaises(migrate.Stop):
                migrate._repair(m)


class SetupTest(MigCase):
    def test_created_line_stops(self):
        self.runner.script = {"shaft setup": cp(0, "created template    template\nok      instance/wren\n")}
        m = self.mig()
        with self.assertRaises(migrate.Stop):
            migrate._setup(m)

    def test_prunes_when_clean(self):
        self.runner.script = {"shaft setup": cp(0, "ok      template\n")}
        with mock.patch.object(core, "git", return_value=cp(0)) as git:
            migrate._setup(self.mig())
        self.assertIn(mock.call("worktree", "prune", check=False), git.call_args_list)


class RebuildTest(MigCase):
    def test_conditions(self):
        (self.sup / "migration").mkdir(parents=True)
        (self.sup / "migration" / "venv-bonsai.txt").write_text("pkg==1\n")
        # no venv-ft freeze, no Brewfile: those are skipped
        m = self.mig()
        m.manifest = {"browsers": True}
        migrate._rebuild(m)
        argv = [" ".join(c) for c in self.runner.calls]
        self.assertTrue(any("venv --clear" in a and ".venv-ui" in a for a in argv))
        self.assertTrue(any(".venv-bonsai" in a and "pip" in a for a in argv))
        self.assertFalse(any(".venv-ft" in a for a in argv))
        self.assertFalse(any("brew bundle" in a for a in argv))
        self.assertTrue(any("shaft browser install" in a for a in argv))


class StartServicesTest(MigCase):
    def test_order_and_model_skip(self):
        m = self.mig(stdin=["yes", "yes"])
        m.manifest = {"services": ["autonomy", "telegram", "web", "api", "embed", "model"]}
        migrate._start_services(m)
        ons = [c for c in self.runner.calls if len(c) == 3 and c[0].endswith("/bin/shaft") and c[-1] == "on"]
        # model pack absent: model on skipped; the rest in start order
        self.assertEqual([c[1] for c in ons], ["embed", "api", "web", "telegram"])
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            migrate._start_services(m)
        self.assertIn("model service skipped", buf.getvalue())


class CodexTest(MigCase):
    def test_missing_runs_login(self):
        m = self.mig()
        self.runner.script = {"shaft codex status": cp(0, "Codex login: missing; run shaft codex login\n")}
        with mock.patch.object(migrate, "_uses_codex", return_value=True), \
                mock.patch.object(core, "instance_names", return_value=["wren"]):
            migrate._codex(m)
        self.assertTrue(any(c[:2] == ["interactive", str(core.PROJECT / "bin" / "shaft")] and c[-1] == "login"
                            for c in self.runner.calls))

    def test_present_is_not_working(self):
        m = self.mig()
        self.runner.script = {"shaft codex status": cp(0, "Codex login: present\n")}
        with mock.patch.object(migrate, "_uses_codex", return_value=True), \
                mock.patch.object(core, "instance_names", return_value=["wren"]):
            migrate._codex(m)
        self.assertFalse(any("login" in c for c in self.runner.calls))


class StepOrderTest(MigCase):
    def test_talk_before_check(self):
        steps = migrate._target_steps(self.mig())
        ids = [s.id for s in steps]
        self.assertLess(ids.index("9"), ids.index("10"))
        # start order has no scheduler
        self.assertEqual([s.id for s in steps], [str(i) for i in range(1, 13)])


class ResumeAndAutonomyTest(MigCase):
    def test_resume_unless_was_paused_and_autonomy_if_installed(self):
        m = self.mig()
        m.manifest = {"was_paused": False, "services": ["autonomy", "web"]}
        migrate._resume_and_autonomy(m)
        argv = [" ".join(c) for c in self.runner.calls]
        self.assertTrue(any("shaft resume" in a for a in argv))
        self.assertTrue(any("shaft autonomy on" in a for a in argv))
        self.assertTrue(any("shaft autonomy status" in a for a in argv))

    def test_no_resume_when_was_paused(self):
        m = self.mig()
        m.manifest = {"was_paused": True, "services": []}
        migrate._resume_and_autonomy(m)
        self.assertFalse(any("resume" in " ".join(c) for c in self.runner.calls))


class TailnetCompareTest(MigCase):
    def test_changed(self):
        m = self.mig()
        m.manifest = {"tailnet": {"dns": "old.ts.net."}}
        with mock.patch.object(migrate, "_tailscale_json", return_value={"Self": {"DNSName": "new.ts.net."}}):
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                migrate._tailnet_compare(m)
            self.assertIn("changed", buf.getvalue())

    def test_unchanged(self):
        m = self.mig()
        m.manifest = {"tailnet": {"dns": "same.ts.net."}}
        with mock.patch.object(migrate, "_tailscale_json", return_value={"Self": {"DNSName": "same.ts.net."}}):
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                migrate._tailnet_compare(m)
            self.assertIn("unchanged", buf.getvalue())


class CommandTest(MigCase):
    def test_requires_a_terminal(self):
        self.assertEqual(migrate.command("source", "host", False, tty=False), 1)

    def test_check_runs_read_only_and_writes_nothing(self):
        self.plist("web")
        with mock.patch.object(migrate, "_check_new_mac"):
            self.assertEqual(migrate.command("source", "host", True, tty=False, runner=self.runner), 0)
        self.assertFalse((self.sup / "migration" / "source-state.json").exists())

    def test_resume_skips_finished_steps(self):
        state_dir = self.sup / "migration"
        state_dir.mkdir(parents=True)
        (state_dir / "target-state.json").write_text('{"done": ["1"]}')
        ran = []
        steps = [migrate.Step("1", "one", "why", [], lambda: ran.append("1")),
                 migrate.Step("2", "two", "why", [], lambda: ran.append("2"))]
        self.assertEqual(migrate._run(self.mig(stdin=[""]), steps, state_dir / "target-state.json",
                                      "bin/shaft migrate target"), 0)
        self.assertEqual(ran, ["2"])

    def test_stop_prints_resume_and_bring_back(self):
        steps = [migrate.Step("1", "one", "why", [], lambda: (_ for _ in ()).throw(migrate.Stop("boom")))]
        m = self.mig(stdin=[""])
        m.state = {"services": ["autonomy", "web"], "was_paused": False}
        self.assertEqual(migrate._run(m, steps, self.sup / "migration" / "source-state.json",
                                      "bin/shaft migrate source --to new", migrate._bring_back_message), 1)


class EndToEndCopyTest(MigCase):
    def test_real_rsync_and_git_with_standin_ssh(self):
        """A real `rsync -a --delete` copies the project (with git) to the destination; .venv-ui is excluded, the
        incoming marker exists, and Application Support lands at a path with a space (no backslash folder)."""
        (self.project / ".venv-ui" / "keep.txt").mkdir(parents=True)
        (self.project / "src.txt").write_text("payload")
        (self.project / ".git").mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(self.project), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-q", "-m", "x"], check=True)

        local_as = self.home / "Library" / "Application Support" / "SHAFT"
        local_as.mkdir(parents=True)
        (local_as / "heldout.txt").write_text("secret")

        bindir = self.tmp / "bin"
        bindir.mkdir()
        ssh = bindir / "ssh"
        ssh.write_text("#!/bin/sh\n"
                       "while [ $# -gt 0 ] && [ \"$1\" = \"-o\" ]; do shift 2; done\n"
                       "shift\n"   # host
                       "exec /bin/sh -c \"$*\"\n")
        ssh.chmod(0o755)
        remote = self.tmp / "remote-home"
        remote.mkdir()

        from supervisor.migrate import Runner
        runner = Runner(host="new")
        with mock.patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}):
            m = migrate._Mig(runner=runner, stdin=migrate._wrap_stdin([""]), tty=True, check=False, host="new")
            m.state_dir = self.sup / "migration"
            with mock.patch.object(migrate, "_require_source_stopped"), \
                    mock.patch.object(migrate, "_check_new_mac"), \
                    mock.patch.object(migrate, "_remote_home", return_value=str(remote)), \
                    mock.patch.object(migrate, "_dest", return_value=str(remote / "project")), \
                    mock.patch.object(core, "instance_names", return_value=[]):
                migrate._copy(m)
        dest = remote / "project"
        self.assertTrue((dest / "src.txt").exists())
        self.assertEqual((dest / "src.txt").read_text(), "payload")
        self.assertFalse((dest / ".venv-ui").exists())          # excluded
        self.assertTrue((dest / ".supervisor" / "migration" / "incoming").exists())

        as_dest = remote / "Library" / "Application Support" / "SHAFT"
        self.assertTrue((as_dest / "heldout.txt").exists())
        self.assertEqual((as_dest / "heldout.txt").read_text(), "secret")
        names = [p.name for p in (remote / "Library").iterdir()]
        self.assertEqual(names, ["Application Support"])
        self.assertFalse(any("\\" in n for n in names))          # no literal backslash folder


class RemoteSpecTest(unittest.TestCase):
    def test_quotes_a_space_without_backslash(self):
        self.assertEqual(migrate._remote_spec("new", "/a/App Support/c/"),
                         "new:'/a/App Support/c/'")
        self.assertNotIn("\\", migrate._remote_spec("new", "/a/App Support/c/"))


class RetryOrYesTest(MigCase):
    def test_yes_returns_yes(self):
        m = self.mig(stdin=["yes"])
        self.assertEqual(migrate._retry_or_yes(m, [(1, "python -m supervisor x")]), "yes")

    def test_enter_returns_retry(self):
        m = self.mig(stdin=[""])
        self.assertEqual(migrate._retry_or_yes(m, [(1, "python -m supervisor x")]), "retry")

    def test_check_mode_never_prompts(self):
        m = self.mig(check=True)
        with self.assertRaises(migrate.Stop):
            migrate._retry_or_yes(m, [(1, "python -m supervisor x")])


class RequireRemoteNoShaftTest(MigCase):
    def test_enter_does_not_pass_a_running_shaft(self):
        m = self.mig(stdin=["", "", ""])
        procs = [(999, "python -m supervisor autonomy serve")]
        with mock.patch.object(migrate, "_remote_plists", return_value=[]), \
                mock.patch.object(migrate, "_remote_shaft_processes", return_value=procs):
            with self.assertRaises(migrate.Stop):
                migrate._require_remote_no_shaft(m, "new")


class RemotePlistsTest(MigCase):
    def test_ssh_failure_is_a_stop(self):
        m = self.mig()
        self.runner.script = {"ls -1 ~/Library/LaunchAgents": cp(255, "", "connection refused")}
        with self.assertRaises(migrate.Stop):
            migrate._remote_plists(self.runner, "new")

    def test_filters_com_shaft_plists(self):
        m = self.mig()
        self.runner.script = {"ls -1 ~/Library/LaunchAgents": cp(0, "com.shaft.web.plist\nother.plist\n")}
        self.assertEqual(migrate._remote_plists(self.runner, "new"), ["com.shaft.web.plist"])

    def test_remote_process_ssh_failure_is_a_stop(self):
        m = self.mig()
        self.runner.script = {"ps -axo pid=,command=": cp(255, "", "connection refused")}
        with self.assertRaises(migrate.Stop):
            migrate._remote_shaft_processes(self.runner, "new")


class PauseResumeStateTest(MigCase):
    def test_was_paused_saved_before_pause_and_not_overwritten_on_resume(self):
        (self.sup / "paused").write_text("2026-11-05T00:00:00")
        self.runner.script = {"ps -axo pid=,command=": cp(0, "")}
        m = self.mig()
        m.state_path = self.sup / "migration" / "source-state.json"
        with mock.patch.object(core, "instance_names", return_value=[]):
            migrate._pause_and_wait(m)
        state = json.loads((m.state_path).read_text())
        self.assertTrue(state["was_paused"])

    def test_services_saved_before_stopping(self):
        self.plist("web")
        self.plist("model")
        m = self.mig()
        m.state_path = self.sup / "migration" / "source-state.json"

        def stop(_, verb):
            (self.home / "Library" / "LaunchAgents" / f"com.shaft.{verb}.plist").unlink(missing_ok=True)

        with mock.patch.object(migrate, "_stop_service", side_effect=stop), \
                mock.patch.object(migrate, "_check_no_shaft_local"):
            migrate._stop_services(m)
        state = json.loads((m.state_path).read_text())
        self.assertEqual(state["services"], ["web", "model"])


class RequireSourceStoppedTest(MigCase):
    def test_refuses_when_not_paused(self):
        m = self.mig()
        with self.assertRaises(migrate.Stop):
            migrate._require_source_stopped(m)

    def test_passes_when_paused_clean_and_matching(self):
        (self.sup / "paused").write_text("x")
        (self.sup / "migration").mkdir(parents=True)
        (self.sup / "migration" / "manifest.json").write_text(json.dumps(
            {"branches": {}, "minds": {}}))
        m = self.mig()
        with mock.patch.object(migrate, "_local_shaft_processes", return_value=[]), \
                mock.patch.object(migrate, "_waiting_on", return_value=[]), \
                mock.patch.object(migrate, "_installed_services", return_value=[]):
            migrate._require_source_stopped(m)


class TargetRefuseSourceStateTest(MigCase):
    def test_target_refuses_on_source_state(self):
        (self.sup / "migration").mkdir(parents=True)
        (self.sup / "migration" / "source-state.json").write_text("{}")
        self.assertEqual(migrate._target(self.mig()), 1)


class FinishMigrationTest(MigCase):
    def test_renames_migration_dir_with_date_and_counter(self):
        from datetime import date
        today = date.today().isoformat()
        (self.sup / "migration").mkdir(parents=True)
        (self.sup / "migration" / "target-state.json").write_text("{}")
        (self.sup / f"migration-done-{today}").mkdir()
        m = self.mig()
        migrate._finish_migration(m)
        self.assertFalse((self.sup / "migration").exists())
        self.assertTrue((self.sup / f"migration-done-{today}-2").exists())


class ControlOptsTest(unittest.TestCase):
    def test_control_master_no_and_control_path(self):
        self.assertEqual(migrate._control_opts()[:2], ["-o", "ControlMaster=no"])
        self.assertTrue(any(x.startswith("ControlPath=") for x in migrate._control_opts()))


class RunnerRunTest(unittest.TestCase):
    def test_missing_program_returns_127(self):
        r = migrate.Runner().run(["definitely-not-a-real-program-xyz", "arg"])
        self.assertEqual(r.returncode, 127)
        self.assertIn("definitely-not-a-real-program-xyz", r.stderr)


class RemoteFreeKbTest(MigCase):
    def test_unreadable_is_none(self):
        m = self.mig()
        self.runner.script = {'df -Pk "$HOME"': cp(1, "", "error")}
        self.assertIsNone(migrate._remote_free_kb(self.runner, "new"))

    def test_parses_available_kb(self):
        m = self.mig()
        self.runner.script = {'df -Pk "$HOME"': cp(0, "Filesystem 1024-blocks Used Available\n/dev/disk 1000 400 600\n")}
        self.assertEqual(migrate._remote_free_kb(self.runner, "new"), 600)


class TailscaleJsonTest(MigCase):
    def test_remote_missing_returns_none(self):
        m = self.mig()
        with mock.patch.object(migrate, "_remote_which_file", return_value=None):
            self.assertIsNone(migrate._tailscale_json(self.runner, remote=True, host="new"))

    def test_remote_unreadable_returns_none(self):
        m = self.mig()
        self.runner.script = {"status --json": cp(1, "", "error")}
        with mock.patch.object(migrate, "_remote_which_file", return_value="/opt/homebrew/bin/tailscale"):
            self.assertIsNone(migrate._tailscale_json(self.runner, remote=True, host="new"))


class WaitingOnTest(MigCase):
    def test_names_each_blocker(self):
        self.runner.script = {"ps -axo pid=,command=": cp(0, "")}
        with mock.patch.object(core, "instance_names", return_value=["thelos", "wren"]), \
                mock.patch("supervisor.runner.is_busy", side_effect=[True, False]), \
                mock.patch.object(core, "update_queued", side_effect=[None, "x"]), \
                mock.patch("supervisor.runner.open_conversations", return_value={}), \
                mock.patch("supervisor.runner._held", return_value={}):
            blockers = migrate._waiting_on(self.runner)
        self.assertEqual(blockers, ["thelos busy", "update queued for wren"])


class DirtyAndUnpushedTest(MigCase):
    def test_lists_dev_worktrees_and_ahead_branches(self):
        project = str(self.project.resolve())
        wt = f"{project}/.claude/worktrees/dev\nHEAD aaaa\nbranch refs/heads/dev\n\n"
        outside = f"{self.tmp}/shaft-x\nHEAD bbbb\nbranch refs/heads/feature\n"
        worktree_list = (f"worktree {project}\nHEAD cccc\nbranch refs/heads/main\n\n"
                         f"worktree {project}/template\nHEAD dddd\nbranch refs/heads/template\n\n"
                         f"worktree {wt}" + f"worktree {outside}\n"
                         f"worktree {self.tmp}/clean-one\n\nworktree {self.tmp}/clean-two\n")
        with mock.patch.object(core, "git", side_effect=[
                cp(0, worktree_list),
                cp(0, "main [ahead 1]\ntemplate [ahead 2]\ninstance/wren [ahead 3]\nfeature [ahead 4]\n")]), \
                mock.patch.object(core, "instance_names", return_value=["wren"]), \
                mock.patch.object(migrate, "_dirty_count", side_effect=[2, 2, 0, 0]):
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                migrate._dirty_and_unpushed(self.mig())
            out = buf.getvalue()
        self.assertIn(".claude/worktrees/dev", out)
        self.assertIn("shaft-x", out)
        self.assertIn("(not copied)", out)
        self.assertNotIn("clean-one", out)
        self.assertNotIn("clean-two", out)
        self.assertIn("2 other development worktrees are clean\n", out)
        self.assertIn("feature [ahead 4]", out)
        self.assertNotIn("instance/wren [ahead 3]", out)

    def test_all_development_worktrees_are_clean(self):
        with mock.patch.object(core, "git", side_effect=[
                cp(0, f"worktree {self.tmp}/clean-one\n\nworktree {self.tmp}/clean-two\n"), cp(0)]), \
                mock.patch.object(core, "instance_names", return_value=[]), \
                mock.patch.object(migrate, "_dirty_count", return_value=0), \
                contextlib.redirect_stdout(io.StringIO()) as buf:
            migrate._dirty_and_unpushed(self.mig())
        out = buf.getvalue()
        self.assertIn("2 other development worktrees are clean\n", out)
        self.assertNotIn("clean-one", out)
        self.assertNotIn("clean-two", out)
        self.assertNotIn("no development worktrees", out)
        self.assertNotIn("development worktrees (uncommitted work):", out)


class StopServiceOptionsTest(MigCase):
    def test_force_hint_only_for_force_verbs(self):
        import contextlib
        import io
        m = self.mig(stdin=["q"])
        buf = io.StringIO()
        with mock.patch.object(migrate.Runner, "shaft", autospec=True) as shaft:
            shaft.side_effect = [cp(1, "not now: runs need it\n")]
            with contextlib.redirect_stderr(buf), self.assertRaises(migrate.Stop):
                migrate._stop_service(m, "web")
        self.assertNotIn("force", buf.getvalue())


class BringBackTest(MigCase):
    def test_empty_until_was_paused_recorded(self):
        m = self.mig()
        self.assertEqual(migrate._bring_back(m), [])


class PrintTargetCommandTest(MigCase):
    def test_prints_cd_and_terminal_instruction(self):
        m = self.mig(host="new")
        with mock.patch.object(migrate, "_dest", return_value="/new/home/project"):
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                migrate._print_target_command(m)
        self.assertIn("cd /new/home/project && bin/shaft migrate target", buf.getvalue())
        self.assertIn("not over SSH", buf.getvalue())


class CompareRefsTest(MigCase):
    def test_mind_uses_verify_refs_heads_mind(self):
        m = self.mig()
        m.manifest = {"branches": {}, "minds": {"wren": "abc"}}
        self.runner.script = {"rev-parse": cp(0, "abc\n")}
        with mock.patch.object(migrate, "_dest", return_value="/new/home/project"):
            migrate._compare_refs(m)
        mind_calls = [c for c in self.runner.calls if c[0] == "ssh" and "rev-parse" in " ".join(c)]
        self.assertTrue(any("--verify" in " ".join(c) and "refs/heads/mind" in " ".join(c) for c in mind_calls))


class LaptopTest(MigCase):
    def test_memory_note_printed_for_desktop(self):
        (self.project / ".models" / "bonsai2-27b-mlx").mkdir(parents=True)
        m = self.mig()
        self.runner.script = {"pmset -g batt": cp(0, "No internal battery\n"),
                              "sysctl -n hw.memsize": cp(0, str(32 * 1024**3) + "\n")}
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            migrate._laptop(m)
        self.assertIn("SHAFT_PROMPT_CACHE_GB", buf.getvalue())


class StartServicesStatusTest(MigCase):
    def test_status_after_each_on(self):
        m = self.mig(stdin=["yes"])
        m.manifest = {"services": ["web"]}
        migrate._start_services(m)
        verbs = [c[1] for c in self.runner.calls if c[0].endswith("/bin/shaft")]
        self.assertEqual(verbs, ["web", "web"])
        self.assertEqual([c[2] for c in self.runner.calls if c[0].endswith("/bin/shaft")], ["on", "status"])


class RebuildTest(MigCase):
    def test_import_check_runs(self):
        (self.sup / "migration").mkdir(parents=True)
        m = self.mig()
        m.manifest = {"browsers": False}
        migrate._rebuild(m)
        argv = [" ".join(c) for c in self.runner.calls]
        self.assertTrue(any("import sys" in a and ".venv-ui" in a for a in argv))
        self.assertTrue(any("import prompt_toolkit, rich" in a for a in argv))


class CodexRegexTest(MigCase):
    def test_missing_elsewhere_does_not_trigger_login(self):
        m = self.mig()
        self.runner.script = {"shaft codex status": cp(0, "some line about missing models, not login\n"
                                                          "Codex login: present\n")}
        with mock.patch.object(migrate, "_uses_codex", return_value=True), \
                mock.patch.object(core, "instance_names", return_value=["wren"]):
            migrate._codex(m)
        self.assertFalse(any(c[:2] == ["interactive", str(core.PROJECT / "bin" / "shaft")] and c[-1] == "login"
                             for c in self.runner.calls))


class TalkEachTest(MigCase):
    def test_l_runs_login_then_talks_again(self):
        m = self.mig(stdin=["l", ""])
        with mock.patch.object(core, "instance_names", return_value=["wren"]), \
                mock.patch.object(migrate, "_uses_codex", return_value=True):
            migrate._talk_each(m)
        self.assertIn(["interactive", str(core.PROJECT / "bin" / "shaft"), "codex", "login"], self.runner.calls)
        talks = [c for c in self.runner.calls if c[:1] == ["talk"]]
        self.assertEqual(len(talks), 2)


class TailnetUnknownTest(MigCase):
    def test_unknown_says_so(self):
        m = self.mig()
        m.manifest = {"tailnet": {"dns": ""}}
        with mock.patch.object(migrate, "_tailscale_json", return_value=None):
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                migrate._tailnet_compare(m)
        self.assertIn("cannot compare", buf.getvalue())


class PrintStepCallableTest(unittest.TestCase):
    def test_callable_commands_are_rendered(self):
        step = migrate.Step("6", "Stop", "why", lambda: ["bin/shaft autonomy off"], lambda: None)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            migrate._print_step(step)
        self.assertIn("$ bin/shaft autonomy off", buf.getvalue())


class SourceHostTest(MigCase):
    def test_check_requires_a_host(self):
        self.plist("web")
        with mock.patch.object(migrate, "_check_new_mac"):
            self.assertEqual(migrate.command("source", None, True, tty=False, runner=self.runner), 2)

    def test_refuses_host_starting_with_dash(self):
        self.assertEqual(migrate.command("source", "-evil", True, tty=False, runner=self.runner), 2)


class FreezeTest(MigCase):
    def test_brew_dump_uses_force(self):
        (self.sup / "migration").mkdir(parents=True)
        (self.sup / "migration" / "Brewfile").write_text("tap ...\n")
        m = self.mig()
        migrate._freeze(m)
        brew = next(c for c in self.runner.calls if c[0] == "brew")
        self.assertIn("--force", brew)
        self.assertIn("dump", brew)

    def test_brew_127_prints_note_and_continues(self):
        (self.sup / "migration").mkdir(parents=True)
        m = self.mig()
        self.runner.script = {"brew bundle dump": cp(127, "", "brew: command not found")}
        migrate._freeze(m)   # must not raise


class CheckTargetFolderTest(MigCase):
    def test_target_state_message_says_overwrite(self):
        with mock.patch.object(migrate, "_target_folder_status", return_value={
                "exists": True, "empty": False, "marker": True, "manifest": True, "target_state": True}):
            with self.assertRaises(migrate.Stop) as cm:
                migrate._check_target_folder(self.runner, "new", "/dest")
        self.assertIn("overwrite", str(cm.exception))


class ControlPathTest(unittest.TestCase):
    def test_control_path_is_a_short_tmp_path(self):
        # macOS's $TMPDIR is long; a socket path must stay under 104 characters
        p = migrate._control_path()
        self.assertEqual(p, f"/tmp/shaft-mig-{os.getpid()}")
        self.assertLess(len(p), 104)


class StopServiceStderrTest(MigCase):
    def test_not_now_on_stderr_is_shown(self):
        m = self.mig(stdin=["q"])
        with mock.patch.object(migrate.Runner, "shaft", autospec=True) as shaft:
            shaft.side_effect = [cp(1, "", "not now: runs need it\n")]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), self.assertRaises(migrate.Stop):
                migrate._stop_service(m, "web")
        self.assertIn("not now: runs need it", buf.getvalue())


class ConfirmPromptTest(MigCase):
    def test_prompt_printed_before_read(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            migrate._confirm(self.mig(stdin=[""]))
        self.assertIn("[Enter] run · q stop", buf.getvalue())


class SourceLiveCheckoutTest(MigCase):
    def test_wrong_checkout_refuses_before_connect(self):
        self.plist("web", working_dir="/elsewhere")
        self.assertEqual(migrate.command("source", "new", False, tty=True, runner=self.runner), 1)
        self.assertNotIn(["connect", "new"], self.runner.calls)

    def test_all_steps_done_returns_without_connect(self):
        (self.sup / "paused").touch()
        state_dir = self.sup / "migration"
        state_dir.mkdir(parents=True)
        (state_dir / "source-state.json").write_text(
            json.dumps({"to": "new", "done": [str(i) for i in range(1, 11)]}))
        self.assertEqual(migrate.command("source", None, False, tty=True, runner=self.runner), 0)
        self.assertNotIn(["connect", "new"], self.runner.calls)


class LaptopRunYourselfTest(MigCase):
    def test_laptop_prints_run_yourself(self):
        m = self.mig()
        self.runner.script = {
            "pmset -g batt": cp(0, "Now drawing from 'AC Power'\n -InternalBattery-0 (id=12345)\t100%\n"),
            "fdesetup status": cp(0, "FileVault is On.\n"),
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            migrate._laptop(m)
        self.assertIn("run this yourself", buf.getvalue())
        self.assertIn("sudo pmset -c sleep 0", buf.getvalue())


class RebuildBrewFailureTest(MigCase):
    def test_failure_asks_to_continue(self):
        (self.sup / "migration").mkdir(parents=True)
        (self.sup / "migration" / "Brewfile").write_text("brew ...\n")
        m = self.mig(stdin=[""])
        m.manifest = {"browsers": False}
        self.runner.script = {"brew bundle": cp(1, "", "Error: brew bundle failed\n")}
        migrate._rebuild(m)   # blank continues, no raise

    def test_failure_q_stops(self):
        (self.sup / "migration").mkdir(parents=True)
        (self.sup / "migration" / "Brewfile").write_text("brew ...\n")
        m = self.mig(stdin=["q"])
        m.manifest = {"browsers": False}
        self.runner.script = {"brew bundle": cp(1, "", "Error: brew bundle failed\n")}
        with self.assertRaises(migrate.Stop):
            migrate._rebuild(m)


class EndToEndFullPassTest(unittest.TestCase):
    """A whole `source` then a whole `target` through migrate.command with the real Runner, real git and rsync, in
    temp folders, on a changed path. Stand-ins for the live-service commands come first on PATH."""

    _SHAFT = r'''#!/bin/sh
PROJECT=$(cd "$(dirname "$0")/.." && pwd)
HOME_DIR=$(cd "$PROJECT/../.." && pwd)
printf '%s\n' "$*" >> '__LOG__'
cmd="$1"
sub="$2"
case "$cmd" in
  pause) mkdir -p "$PROJECT/.supervisor"; touch "$PROJECT/.supervisor/paused" ;;
  resume) rm -f "$PROJECT/.supervisor/paused" ;;
  share) printf '{"shared":"%s"}\n' "$3" > "$PROJECT/.supervisor/tools/$2.json" ;;
  setup) echo 'ok      template'; echo 'ok      instance/wren' ;;
esac
if [ "$sub" = "off" ]; then
  svc="$cmd"; [ "$svc" = "autonomy" ] && svc="scheduler"
  rm -f "$HOME_DIR/Library/LaunchAgents/com.shaft.$svc.plist"
fi
exit 0
'''

    _SSH = r'''#!/bin/sh
export HOME='__NEWHOME__'
for a in "$@"; do [ "$a" = "-fN" ] && exit 0; done
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; *) break ;; esac; done
[ $# -gt 0 ] && shift
[ $# -eq 0 ] && exit 0
cmd="$*"
case "$cmd" in
  "test -x /opt/homebrew/bin/uv"|"test -x /usr/local/bin/uv"|"test -x /usr/bin/git") exit 0 ;;
esac
if [ "${cmd#/bin/zsh -lc }" != "$cmd" ]; then
  inner="${cmd#/bin/zsh -lc }"
  inner=${inner#\'}
  inner=${inner%\'}
  exec /bin/sh -c "$inner"
fi
exec /bin/sh -c "$cmd"
'''

    _PY3 = r'''#!/bin/sh
if [ "$1" = "--version" ]; then echo "Python 3.12.1"; exit 0; fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  dir=""
  for a in "$@"; do case "$a" in -m|venv|--clear) ;; *) dir="$a" ;; esac; done
  mkdir -p "$dir/bin"
  cat > "$dir/bin/python" <<'PYEOF'
#!/bin/sh
exit 0
PYEOF
  chmod +x "$dir/bin/python"
  exit 0
fi
exit 0
'''

    _BREW = r'''#!/bin/sh
FILE=""
for a in "$@"; do case "$a" in --file=*) FILE="${a#--file=}" ;; esac; done
if [ "$2" = "dump" ] && [ -n "$FILE" ]; then echo 'tap "homebrew/core"' > "$FILE"; fi
exit 0
'''

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _write_exec(self, path, text):
        path.write_text(text)
        path.chmod(0o755)

    @contextlib.contextmanager
    def _core_paths(self, project, home):
        patchers = [
            mock.patch.object(core, "PROJECT", project),
            mock.patch.object(core, "HOME", home),
            mock.patch.object(core, "SUP_DIR", project / ".supervisor"),
            mock.patch.object(core, "INSTANCES", project / "instances"),
            mock.patch.object(core, "TEMPLATE", project / "template"),
        ]
        for p in patchers:
            p.start()
        try:
            yield
        finally:
            for p in patchers:
                p.stop()

    def _build(self, pre_paused=False):
        tmp = self._tmp
        old_home = tmp / "oldhome"
        new_home = tmp / "newhome"
        old_project = old_home / "Development" / "shaft"
        new_project = new_home / "Development" / "shaft"
        bindir = tmp / "bin"
        log = tmp / "shaft.log"

        (old_home / "Library" / "LaunchAgents").mkdir(parents=True)
        (old_home / "Library" / "Application Support" / "SHAFT").mkdir(parents=True)
        old_project.mkdir(parents=True)
        new_home.mkdir(parents=True)
        bindir.mkdir()

        (old_project / "requirements-ui.txt").write_text("prompt-toolkit\nrich\n")
        subprocess.run(["git", "init", "-q"], cwd=old_project, check=True)
        subprocess.run(["git", "-C", str(old_project), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(old_project), "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(old_project), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(old_project), "commit", "-q", "-m", "main"], check=True)

        subprocess.run(["git", "-C", str(old_project), "worktree", "add", "-b", "template", "template"],
                       check=True, capture_output=True)
        (old_project / "template" / "shaft").mkdir(parents=True)
        (old_project / "template" / "shaft" / "README.md").write_text("template")
        subprocess.run(["git", "-C", str(old_project / "template"), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(old_project / "template"), "commit", "-q", "-m", "template"], check=True)

        subprocess.run(["git", "-C", str(old_project), "worktree", "add", "-b", "instance/wren", "instances/wren"],
                       check=True, capture_output=True)
        (old_project / "instances" / "wren" / "shaft").mkdir(parents=True)
        (old_project / "instances" / "wren" / "shaft" / "README.md").write_text("wren")
        subprocess.run(["git", "-C", str(old_project / "instances" / "wren"), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(old_project / "instances" / "wren"), "commit", "-q", "-m", "wren"],
                       check=True)

        (old_project / ".venv-ui").mkdir()
        (old_project / ".venv-ui" / "x").write_text("ui")
        (old_project / ".venv-bonsai" / "bin").mkdir(parents=True)
        self._write_exec(old_project / ".venv-bonsai" / "bin" / "python",
                         "#!/bin/sh\necho 'numpy==1.26.0'\nexit 0\n")
        for sub in (".venv", ".uv", ".tmp"):
            (old_project / "instances" / "wren" / sub).mkdir()
            (old_project / "instances" / "wren" / sub / "x").write_text("x")

        minds = old_project / ".supervisor" / "minds" / "wren.git"
        minds.mkdir(parents=True)
        subprocess.run(["git", "--git-dir", str(minds), "init", "--bare", "-q"], check=True)
        mind_work = tmp / "mindwork"
        mind_work.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=mind_work, check=True)
        subprocess.run(["git", "-C", str(mind_work), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(mind_work), "config", "user.name", "t"], check=True)
        (mind_work / "self.md").write_text("mind")
        subprocess.run(["git", "-C", str(mind_work), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(mind_work), "commit", "-q", "-m", "mind"], check=True)
        subprocess.run(["git", "-C", str(mind_work), "branch", "-M", "mind"], check=True)
        subprocess.run(["git", "-C", str(mind_work), "remote", "add", "origin", str(minds)], check=True)
        subprocess.run(["git", "-C", str(mind_work), "push", "-q", "origin", "mind"], check=True)

        shared = old_home / "Shared Folder"
        shared.mkdir()
        (shared / "shared.txt").write_text("shared-data")
        (old_project / ".supervisor" / "tools").mkdir(parents=True, exist_ok=True)
        (old_project / ".supervisor" / "tools" / "wren.json").write_text(json.dumps({"shared": str(shared)}))

        for name in ("scheduler", "telegram", "web", "api", "embed", "model"):
            data = {"Label": f"com.shaft.{name}", "WorkingDirectory": str(old_project)}
            (old_home / "Library" / "LaunchAgents" / f"com.shaft.{name}.plist").write_bytes(plistlib.dumps(data))

        (old_home / "Library" / "Application Support" / "SHAFT" / "backup.txt").write_text("backup")

        if pre_paused:
            (old_project / ".supervisor").mkdir(parents=True, exist_ok=True)
            (old_project / ".supervisor" / "paused").write_text("2026-11-05T00:00:00")

        (old_project / "bin").mkdir()
        self._write_exec(old_project / "bin" / "shaft", self._SHAFT.replace("__LOG__", str(log)))

        self._write_exec(bindir / "ps", "#!/bin/sh\nexit 0\n")
        self._write_exec(bindir / "brew", self._BREW)
        self._write_exec(bindir / "pmset", "#!/bin/sh\necho 'No internal battery'\nexit 0\n")
        self._write_exec(bindir / "fdesetup", "#!/bin/sh\nexit 0\n")
        self._write_exec(bindir / "sysctl", "#!/bin/sh\necho '68719476736'\nexit 0\n")
        self._write_exec(bindir / "tailscale",
                         "#!/bin/sh\necho '{\"MagicDNSSuffix\":\"example.ts.net\",\"Self\":{\"DNSName\":\"mac.example.ts.net.\",\"UserID\":42},\"User\":{\"42\":{\"LoginName\":\"me@example.com\"}}}'\nexit 0\n")
        self._write_exec(bindir / "python3", self._PY3)
        self._write_exec(bindir / "ssh", self._SSH.replace("__NEWHOME__", str(new_home)))

        return {
            "old_home": old_home, "new_home": new_home,
            "old_project": old_project, "new_project": new_project,
            "bindir": bindir, "log": log,
        }

    def _run_source(self, ctx):
        with self._core_paths(ctx["old_project"], ctx["old_home"]), \
                mock.patch.dict(os.environ, {"PATH": str(ctx["bindir"]) + os.pathsep + os.environ.get("PATH", "")}), \
                mock.patch.object(migrate, "TAILSCALE_PATHS", (str(ctx["bindir"] / "tailscale"),)), \
                mock.patch.object(migrate, "_waiting_on", return_value=[]), \
                mock.patch("supervisor.api.allowed_logins", return_value={"me@example.com"}), \
                mock.patch.object(migrate, "_service_running", return_value=True, create=True), \
                mock.patch("supervisor.settings.listing", return_value=[]):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                ctx["source_rc"] = migrate.command("source", "new", False, tty=True, stdin=[""] * 50,
                                                   runner=migrate.Runner())

    def _run_target(self, ctx):
        with self._core_paths(ctx["new_project"], ctx["new_home"]), \
                mock.patch.dict(os.environ, {"PATH": str(ctx["bindir"]) + os.pathsep + os.environ.get("PATH", "")}), \
                mock.patch.object(migrate, "TAILSCALE_PATHS", (str(ctx["bindir"] / "tailscale"),)), \
                mock.patch.object(migrate, "_waiting_on", return_value=[]), \
                mock.patch("supervisor.api.allowed_logins", return_value={"me@example.com"}), \
                mock.patch.object(migrate, "_service_running", return_value=True, create=True), \
                mock.patch("supervisor.settings.listing", return_value=[]):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                ctx["target_rc"] = migrate.command("target", None, False, tty=True, stdin=[""] * 7 + ["yes"] + [""] * 50,
                                                   runner=migrate.Runner())

    def test_full_pass(self):
        ctx = self._build()
        self._run_source(ctx)
        self._run_target(ctx)

        self.assertEqual(ctx["source_rc"], 0)
        self.assertEqual(ctx["target_rc"], 0)

        log_lines = ctx["log"].read_text().splitlines()
        setup_idx = next(i for i, line in enumerate(log_lines) if line.startswith("share wren "))
        src, tgt = log_lines[:setup_idx], log_lines[setup_idx:]

        self.assertEqual(src[0], "pause")
        self.assertEqual([l for l in src if l.endswith(" off")],
                         ["autonomy off", "telegram off", "web off", "api off", "embed off", "model off"])

        new_project, new_home = ctx["new_project"], ctx["new_home"]
        new_sup = new_project / ".supervisor"

        self.assertFalse((new_sup / "paused").exists())
        self.assertTrue((new_home / "Library" / "Application Support" / "SHAFT" / "backup.txt").exists())
        for root, dirs, files in os.walk(new_home):
            for p in [root, *[os.path.join(root, d) for d in dirs], *[os.path.join(root, f) for f in files]]:
                self.assertNotIn("\\", p)
        self.assertTrue((new_home / "Shared Folder" / "shared.txt").exists())
        self.assertFalse((new_project / ".venv-ui" / "x").exists())
        self.assertFalse((new_sup / "migration" / "source-state.json").exists())

        for wt in ("template", "instances/wren"):
            self.assertIn(str(new_project.resolve()), (new_project / wt / ".git").read_text())
        subprocess.run(["git", "-C", str(new_project / "instances" / "wren"), "status"], check=True)
        for sub in (".venv", ".uv", ".tmp"):
            self.assertFalse((new_project / "instances" / "wren" / sub).exists())

        self.assertEqual(tgt[0], f"share wren {new_home / 'Shared Folder'}")
        self.assertLess(tgt.index("setup"), tgt.index("embed on"))
        self.assertEqual(json.loads((new_sup / "tools" / "wren.json").read_text())["shared"],
                         str(new_home / "Shared Folder"))
        self.assertNotIn("model on", tgt)
        self.assertEqual([l for l in tgt if l.endswith(" on")],
                         ["embed on", "api on", "web on", "telegram on", "autonomy on"])
        self.assertLess(tgt.index("talk wren"), tgt.index("check wren"))
        self.assertLess(tgt.index("resume"), tgt.index("autonomy on"))

        self.assertFalse((new_sup / "migration").exists())
        self.assertEqual(len(list(new_sup.glob("migration-done-*"))), 1)

        with self._core_paths(ctx["old_project"], ctx["old_home"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = migrate.command("target", None, False, tty=True, stdin=[""], runner=migrate.Runner())
            self.assertEqual(rc, 1)
            self.assertIn("old Mac", err.getvalue())

    def test_was_paused_skips_resume(self):
        ctx = self._build(pre_paused=True)
        self._run_source(ctx)
        self._run_target(ctx)
        self.assertEqual(ctx["source_rc"], 0)
        self.assertEqual(ctx["target_rc"], 0)
        log_lines = ctx["log"].read_text().splitlines()
        tgt = log_lines[log_lines.index("setup"):]
        self.assertNotIn("resume", tgt)



class FourthPassRegressionTest(MigCase):
    def test_bring_back_never_starts_old_mac_after_target_started(self):
        m = self.mig(host="new")
        m.state = {"copied": True, "was_paused": False, "services": ["web"]}
        for plists, target_state, expected in [
                ([], False, "bin/shaft web on"),
                ([], True, "quit `migrate target`"),
                (["com.shaft.web.plist"], False, "the new Mac is running")]:
            with self.subTest(plists=plists, target_state=target_state), \
                    mock.patch.object(migrate, "_dest", return_value="/new/project"), \
                    mock.patch.object(migrate, "_remote_plists", return_value=plists), \
                    mock.patch.object(self.runner, "ssh", return_value=cp(0 if target_state else 1)), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                migrate._bring_back_message(m)
                self.assertIn(expected, err.getvalue())
                if plists or target_state:
                    self.assertNotIn("bin/shaft web on", err.getvalue())
                    self.assertNotIn("bin/shaft resume", err.getvalue())
                    self.assertIn("leave the old Mac stopped", err.getvalue())
                if target_state:
                    self.assertIn(".supervisor/migration/target-state.json", err.getvalue())
                    self.assertIn("rerun `migrate source`", err.getvalue())

    def test_bring_back_unreadable_target_never_raises_or_prints_on(self):
        m = self.mig(host="new")
        m.state = {"copied": True, "was_paused": False, "services": ["web"]}
        for error in (migrate.Stop("unreachable"), OSError("unreadable")):
            with mock.patch.object(migrate, "_remote_plists", side_effect=error), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                migrate._bring_back_message(m)
            self.assertIn("could not tell", err.getvalue())
            self.assertNotIn("bin/shaft web on", err.getvalue())

    def test_bring_back_unreadable_state_directory_does_not_mean_absent(self):
        m = self.mig(host="new")
        m.state = {"copied": True, "was_paused": False, "services": ["web"]}
        state_dir = self.project / ".supervisor" / "migration"
        state_dir.mkdir()
        (state_dir / "target-state.json").write_text("{}")

        def remote_shell(command, *, host=None):
            # Execute only the lookup in a temporary project, never an SSH connection.
            return subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)

        with mock.patch.object(migrate, "_remote_plists", return_value=[]), \
                mock.patch.object(migrate, "_dest", return_value=str(self.project)), \
                mock.patch.object(self.runner, "ssh", side_effect=remote_shell):
            state_dir.chmod(0)
            try:
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    migrate._bring_back_message(m)
                self.assertIn("could not tell", err.getvalue())
                self.assertNotIn("bin/shaft web on", err.getvalue())
            finally:
                state_dir.chmod(0o700)
            (state_dir / "target-state.json").unlink()
            with contextlib.redirect_stderr(io.StringIO()) as err:
                migrate._bring_back_message(m)
            self.assertIn("bin/shaft web on", err.getvalue())
            shutil.rmtree(state_dir)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                migrate._bring_back_message(m)
            self.assertIn("bin/shaft web on", err.getvalue())

    def test_bring_back_failed_state_probe_never_prints_on(self):
        m = self.mig(host="new")
        m.state = {"copied": True, "was_paused": False, "services": ["web"]}
        for code in (2, 255):
            with mock.patch.object(migrate, "_remote_plists", return_value=[]), \
                    mock.patch.object(migrate, "_dest", return_value="/new/project"), \
                    mock.patch.object(self.runner, "ssh", return_value=cp(code)), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                migrate._bring_back_message(m)
            self.assertIn("could not tell", err.getvalue())
            self.assertNotIn("bin/shaft web on", err.getvalue())

    def test_target_stop_after_step_seven_warns_old_mac_must_stay_stopped(self):
        m = self.mig(stdin=["q"])
        migrate._write_file(m.state_dir / "manifest.json", "{}")
        migrate._save_state(m.state_dir / "target-state.json", {"done": [str(i) for i in range(1, 8)]})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(migrate._target(m), 1)
        self.assertIn("services run on this Mac", err.getvalue())
        self.assertIn("old Mac must stay stopped", err.getvalue())

    def test_target_stop_during_step_seven_warns_old_mac_must_stay_stopped(self):
        m = self.mig()
        m.state = {"done": [str(i) for i in range(1, 7)]}
        self.plist("web")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            migrate._target_stop_message(m)
        self.assertIn("services run on this Mac", err.getvalue())
        self.assertIn("old Mac must stay stopped", err.getvalue())

    def test_source_quiet_process_match_accepts_yes_or_stops_on_q(self):
        (self.sup / "paused").touch()
        for answer in ("yes", "q"):
            with self.subTest(answer=answer), \
                    mock.patch.object(migrate, "_local_shaft_processes", return_value=[(42, "python -m shaft")]), \
                    contextlib.redirect_stderr(io.StringIO()):
                m = self.mig(stdin=[answer])
                if answer == "yes":
                    migrate._require_source_quiet(m)
                else:
                    with self.assertRaisesRegex(migrate.Stop, "SHAFT processes still running"):
                        migrate._require_source_quiet(m)

    def test_manifest_is_not_permission_to_delete_backup(self):
        with mock.patch.object(migrate, "_target_folder_status", return_value={
                "exists": True, "empty": False, "marker": False, "manifest": True, "target_state": False}):
            with self.assertRaisesRegex(migrate.Stop, "moved aside or emptied"):
                migrate._check_target_folder(self.runner, "old", "/backup")

    def test_finished_target_without_manifest_is_successful_noop(self):
        (self.sup / "migration-done-2026-11-05").mkdir()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(migrate._target(self.mig()), 0)
        self.assertIn("already finished here", out.getvalue())
        self.assertEqual(self.runner.calls, [])

    def test_resumed_and_finished_source_rejects_restarted_old_mac_before_connect(self):
        m = self.mig()
        for done in (["6"], [str(i) for i in range(1, 11)]):
            for problem in ("unpaused", "service", "process"):
                with self.subTest(done=done, problem=problem):
                    (self.sup / "paused").unlink(missing_ok=True)
                    if problem != "unpaused":
                        (self.sup / "paused").touch()
                    migrate._save_state(m.state_dir / "source-state.json", {"to": "new", "done": done})
                    with mock.patch.object(migrate, "_installed_services", return_value=["web"] if problem == "service" else []), \
                            mock.patch.object(migrate, "_local_shaft_processes", return_value=[(42, "python -m shaft")] if problem == "process" else []):
                        m.stdin = migrate._wrap_stdin(["q"])
                        message = "SHAFT processes still running" if problem == "process" else "source-state.json"
                        with self.assertRaisesRegex(migrate.Stop, message):
                            migrate._source(None, m)
                    self.assertFalse(any(c[0] == "connect" for c in self.runner.calls))

    def test_last_copy_gate_rejects_waiting_work(self):
        (self.sup / "paused").touch()
        m = self.mig()
        m.manifest = {"branches": {}, "minds": {}}
        with mock.patch.object(migrate, "_waiting_on", return_value=["wren busy", "update queued for wren"]):
            with self.assertRaisesRegex(migrate.Stop, "wren busy.*update queued"):
                migrate._require_source_stopped(m)

    def test_target_requires_typed_yes_that_named_old_mac_stays_stopped(self):
        for lines, starts in [(["", "q"], False), (["yes"], True)]:
            m = self.mig(stdin=lines)
            m.manifest = {"source_host": "old-mac", "services": ["web"]}
            self.runner.calls.clear()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                if starts:
                    migrate._start_services(m)
                else:
                    with self.assertRaises(migrate.Stop):
                        migrate._start_services(m)
            self.assertIn("old-mac", out.getvalue())
            self.assertIn("the old Mac is stopped and stays stopped", out.getvalue())
            self.assertEqual(any(c[-1] == "on" for c in self.runner.calls), starts)

    def test_status_exit_zero_does_not_prove_service_running(self):
        for verb in ("embed", "api", "web", "telegram", "autonomy"):
            m = self.mig(stdin=["yes"])
            m.manifest = {"services": [verb], "was_paused": True}
            name = "scheduler" if verb == "autonomy" else verb
            with self.subTest(verb=verb), \
                    mock.patch.object(migrate, "_service_running", return_value=False, create=True), \
                    mock.patch.object(migrate.time, "monotonic", side_effect=[0, 0, 20]), \
                    mock.patch.object(migrate.time, "sleep"):
                with self.assertRaisesRegex(migrate.Stop, rf"{name}.*com.shaft.{name}.log"):
                    (migrate._resume_and_autonomy if verb == "autonomy" else migrate._start_services)(m)
            self.assertTrue(any(c[-2:] == [verb, "status"] for c in self.runner.calls))

    def test_model_idle_is_allowed_but_missing_plist_is_not(self):
        (self.project / ".models" / "bonsai2-27b-mlx").mkdir(parents=True)
        m = self.mig(stdin=["yes", "yes"])
        m.manifest = {"services": ["model"]}
        with self.assertRaisesRegex(migrate.Stop, "model.*plist"):
            migrate._start_services(m)
        self.plist("model")
        with mock.patch("supervisor.localmodel.down_problem", return_value="idle diagnostic") as down, \
                mock.patch.object(core, "instance_names", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            migrate._start_services(m)
        down.assert_called_once_with([], names=[])
        self.assertIn("note: idle diagnostic", out.getvalue())

    def test_resume_detects_all_paused_even_when_resume_exits_zero(self):
        m = self.mig()
        m.manifest = {"was_paused": False}
        self.runner.script = {"shaft status": cp(0, "SHAFT: ALL PAUSED\nother details\n")}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaisesRegex(migrate.Stop, "ALL PAUSED"):
                migrate._resume_and_autonomy(m)
        self.assertIn("SHAFT: ALL PAUSED", out.getvalue())
        self.assertNotIn("other details", out.getvalue())

    def test_sandbox_agent_and_plugin_count_but_shell_mentions_do_not(self):
        lines = ["1 sandbox-exec -f profile /usr/bin/python3 -m shaft wake",
                 "2 /usr/bin/python3 -m shaft.plugin_runner thing",
                 "3 sandbox-exec -f profile python -m supervisor web serve",
                 "4 zsh -c 'python -m shaft wake'", "5 python -m shaftish"]
        self.assertEqual([pid for pid, _ in migrate._shaft_processes(lines)], [1, 2, 3])

    def test_remote_plist_permission_error_is_not_silenced(self):
        self.runner.script = {"if [ -d ~/Library/LaunchAgents ]; then /bin/ls -1 ~/Library/LaunchAgents; fi": cp(1, "", "Permission denied")}
        with self.assertRaisesRegex(migrate.Stop, "Permission denied"):
            migrate._remote_plists(self.runner, "new")
        self.assertNotIn("zsh", self.runner.calls[-1][-1])

    def test_same_project_path_still_removes_machine_bound_instance_environments(self):
        inst = core.INSTANCES / "wren"
        for rel in (".venv", ".uv", ".tmp"):
            (inst / rel).mkdir(parents=True)
        m = self.mig()
        m.manifest = {"project": str(core.PROJECT)}
        with mock.patch.object(core, "instance_names", return_value=["wren"]):
            migrate._repair(m)
            self.assertIn("rm -rf instances/wren/.venv", " ".join(migrate._repair_commands(m)))
        self.assertFalse(any((inst / rel).exists() for rel in (".venv", ".uv", ".tmp")))

    def test_cleanup_removes_files_and_internal_dangling_environment_symlinks(self):
        inst = core.INSTANCES / "wren"
        inst.mkdir(parents=True)
        (inst / ".uv").write_text("old machine cache")
        (inst / ".venv").symlink_to(inst / "missing-environment")
        m = self.mig()
        m.manifest = {"project": str(core.PROJECT)}
        with mock.patch.object(core, "instance_names", return_value=["wren"]):
            migrate._repair(m)
        self.assertFalse((inst / ".uv").exists())
        self.assertFalse((inst / ".venv").is_symlink())

    def test_failed_instance_check_explains_environment_rebuild_and_retry(self):
        self.runner.script = {"shaft check wren": cp(1)}
        with mock.patch.object(core, "instance_names", return_value=["wren"]):
            with self.assertRaisesRegex(migrate.Stop, "talk wren.*rebuilds.*log.*rerun `migrate target`"):
                migrate._check_each(self.mig())

    def test_shared_pointer_maps_home_even_when_project_does_not_move(self):
        mapped = self.home / "Shared Folder"
        mapped.mkdir()
        m = self.mig()
        m.manifest = {"project": str(core.PROJECT), "home": "/old/home"}
        with mock.patch.object(core, "instance_names", return_value=["wren"]), \
                mock.patch.object(core, "shared_folder", return_value=Path("/old/home/Shared Folder")):
            migrate._repair(m)
            self.assertIn("bin/shaft share wren", " ".join(migrate._repair_commands(m)))
            self.assertIn([str(core.PROJECT / "bin/shaft"), "share", "wren", str(mapped)], self.runner.calls)
            self.runner.script = {"shaft share": cp(1, "", "share refused")}
            with self.assertRaisesRegex(migrate.Stop, "share.*failed"):
                migrate._repair(m)

    def test_missing_shared_pointer_is_reported_and_outside_home_unchanged(self):
        m = self.mig()
        m.manifest = {"project": str(core.PROJECT), "home": "/old/home"}
        with mock.patch.object(core, "instance_names", return_value=["wren"]), \
                mock.patch.object(core, "shared_folder", return_value=Path("/old/home/missing")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            migrate._repair(m)
        self.assertIn("does not exist here", out.getvalue())
        self.assertEqual(self.runner.calls, [])
        with mock.patch.object(core, "instance_names", return_value=["wren"]), \
                mock.patch.object(core, "shared_folder", return_value=Path("/external/shared")):
            migrate._repair(m)
        self.assertEqual(self.runner.calls, [])

    def test_non_directory_shared_folder_is_reported_and_omitted_from_preview(self):
        folder = self.tmp / "missing"
        m = self.mig(host="new")
        with mock.patch.object(core, "instance_names", return_value=["wren"]), \
                mock.patch.object(core, "shared_folder", return_value=folder), \
                mock.patch.object(migrate, "_dest", return_value="/new/project"), \
                mock.patch.object(migrate, "_remote_home", return_value="/new"), \
                mock.patch.object(migrate, "_require_source_stopped"), \
                mock.patch.object(migrate, "_check_new_mac"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertNotIn(str(folder), "\n".join(migrate._copy_commands(m)))
            migrate._copy(m)
        self.assertIn(f"{folder} was not copied", out.getvalue())

    def test_settings_report_hostnames_and_refuse_unknown_or_disallowed_login(self):
        m = self.mig()
        for login in (None, "wrong@example.com", "me@example.com"):
            tail = {"Self": {"UserID": 42}, "User": {"42": {"LoginName": login}}}
            with self.subTest(login=login), \
                    mock.patch("supervisor.settings.listing", return_value=[{"name": "SHAFT_WEB_HOSTS", "value": "old.example.ts.net"}]), \
                    mock.patch("supervisor.api.allowed_logins", return_value={"me@example.com"}), \
                    mock.patch.object(migrate, "_tailscale_json", return_value=tail), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                if login == "me@example.com":
                    migrate._settings_step(m)
                else:
                    with self.assertRaisesRegex(migrate.Stop, r"\.env.*SHAFT_API_LOGINS"):
                        migrate._settings_step(m)
                self.assertIn("SHAFT_WEB_HOSTS=old.example.ts.net", out.getvalue())
                self.assertIn("me@example.com", out.getvalue())
                self.assertIn(login or "unknown", out.getvalue())

    def test_remote_home_df_git_avoid_login_shell(self):
        self.runner.script = {'printf %s "$HOME"': cp(0, "/new/home")}
        self.assertEqual(migrate._remote_home(self.runner, "new"), "/new/home")
        migrate._remote_free_kb(self.runner, "new")
        m = self.mig(host="new")
        m.manifest = {"branches": {"template": "abc"}}
        self.runner.script = {"rev-parse": cp(0, "abc")}
        with mock.patch.object(migrate, "_dest", return_value="/new/project"), \
                mock.patch.object(migrate, "_remote_which_file", return_value="/usr/bin/git"):
            migrate._compare_refs(m)
        self.assertTrue(all("zsh -lc" not in c[-1] for c in self.runner.calls))
        self.assertIn("/usr/bin/git", self.runner.calls[-1][-1])

    def test_python_version_uses_last_matching_line_after_chatty_profile(self):
        m = self.mig(host="new")
        self.runner.script = {"python3 --version": cp(0, "profile Python 3.9\nPython 3.12.1\n")}
        with mock.patch.object(migrate, "_dest", return_value="/new/project"), \
                mock.patch.object(migrate, "_remote_which_file", return_value="/tool"), \
                mock.patch.object(migrate, "_tailscale_json", return_value={"MagicDNSSuffix": "same"}), \
                mock.patch.object(migrate, "_folder_kb", return_value=1), \
                mock.patch.object(migrate, "_remote_free_kb", return_value=100), \
                mock.patch.object(migrate, "_require_remote_no_shaft"), \
                mock.patch.object(migrate, "_check_target_folder"), \
                mock.patch.object(core, "instance_names", return_value=[]):
            migrate._check_new_mac(m)

    def test_unreadable_du_is_a_disk_problem_even_with_numeric_partial_output(self):
        self.runner.script = {"du -sk": cp(1, "123 folder", "Permission denied")}
        self.assertIsNone(migrate._folder_kb(self.runner, self.project))

    def test_disk_counts_project_application_support_and_shared_and_rejects_unreadable_size(self):
        app = self.home / "Library" / "Application Support" / "SHAFT"
        app.mkdir(parents=True)
        shared = self.home / "Shared"
        shared.mkdir()
        m = self.mig(host="new")
        for sizes, expected in [([10, 20, 30], "60 KB"), ([None, 20, 30], "could not read the size")]:
            with self.subTest(sizes=sizes), \
                    mock.patch.object(migrate, "_remote", return_value=cp(0, "Python 3.12")), \
                    mock.patch.object(migrate, "_remote_which_file", return_value="/tool"), \
                    mock.patch.object(migrate, "_tailscale_json", return_value={"MagicDNSSuffix": "same"}), \
                    mock.patch.object(migrate, "_dest", return_value="/new/project"), \
                    mock.patch.object(migrate, "_folder_kb", side_effect=sizes) as size, \
                    mock.patch.object(migrate, "_remote_free_kb", return_value=40), \
                    mock.patch.object(core, "instance_names", return_value=["wren"]), \
                    mock.patch.object(core, "shared_folder", return_value=shared), \
                    mock.patch.object(migrate, "_uses_codex", return_value=False), \
                    mock.patch.object(migrate, "_require_remote_no_shaft"), \
                    mock.patch.object(migrate, "_check_target_folder"):
                with self.assertRaisesRegex(migrate.Stop, expected):
                    migrate._check_new_mac(m)
                self.assertEqual([c.args[1] for c in size.call_args_list], [self.project, app, shared])


class ServiceRunningProbeTest(unittest.TestCase):
    def test_service_running_uses_autonomy_running_instead_of_status_exit_code(self):
        with mock.patch("supervisor.autonomy.running", return_value=False) as running:
            self.assertFalse(migrate._service_running("web"))
        running.assert_called_once_with("web")


if __name__ == "__main__":
    unittest.main()
