"""An instance's own Python environment (the operator, 2026-10-14): .venv/ and .uv/ in its folder, driven by uv in `run`.

Regression for the gap that made wren plan a pure-stdlib RS256 signer: every process of an instance ran on the
system Python with `-E -s` and nothing could be installed. Now its plugins, its tests and `python3` in `run` use its
own environment; copies of it (coder hand-offs, trial gates, replays) read that environment read-only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import broker, core, gate, pyenv, runner, sandbox
from supervisor.core import HOME, PROJECT, PYTHON, TEMPLATE

NAME = "wren"
HAS_SANDBOX = Path(sandbox.SANDBOX_EXEC).exists()


def make_venv(root: Path) -> Path:
    """A real environment with one module the system Python does not have."""
    subprocess.run([PYTHON, "-m", "venv", "--without-pip", str(root / ".venv")], check=True)
    site = next((root / ".venv" / "lib").glob("python3*/site-packages"))
    (site / "shaftprobe.py").write_text("WHERE = 'from the instance environment'\n")
    return root / ".venv" / "bin" / "python"


class ChoiceTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-pyenv-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_system_python_until_the_instance_has_an_environment(self):
        self.assertEqual(pyenv.python(self.root), PYTHON)
        (self.root / ".venv").mkdir()
        self.assertEqual(pyenv.python(self.root), str(self.root / ".venv" / "bin" / "python"))

    def test_a_broken_environment_is_still_chosen_so_the_failure_is_loud(self):
        (self.root / ".venv" / "bin").mkdir(parents=True)   # no interpreter in it
        self.assertEqual(pyenv.python(self.root), str(self.root / ".venv" / "bin" / "python"))

    def test_uv_state_stays_inside_the_instance(self):
        env = pyenv.uv_env(self.root)
        for key in ("UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "UV_TOOL_DIR"):
            self.assertTrue(env[key].startswith(str(self.root / ".uv")), key)

    def test_rebuild_only_when_declared_and_missing(self):
        self.assertFalse(pyenv.needs_rebuild(self.root))
        (self.root / "requirements.txt").write_text("PyJWT[crypto]\n")
        self.assertTrue(pyenv.needs_rebuild(self.root))
        self.assertIn("uv pip install -r requirements.txt", pyenv.rebuild_command(self.root))
        (self.root / ".venv").mkdir()
        self.assertFalse(pyenv.needs_rebuild(self.root))

    def test_the_panel_line_names_the_version_or_says_broken(self):
        self.assertEqual(pyenv.describe(self.root), "system Python (no .venv yet)")
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / ".venv" / "pyvenv.cfg").write_text("home = /x\nversion_info = 3.13.1\n")
        self.assertEqual(pyenv.describe(self.root), ".venv · Python 3.13.1 (BROKEN: interpreter missing)")
        (self.root / ".venv" / "bin" / "python").write_text("")
        self.assertEqual(pyenv.describe(self.root), ".venv · Python 3.13.1")

    def test_the_declaration_is_code_tested_and_committed(self):
        for f in pyenv.DECLARED:
            self.assertIn(f, gate.CODE_PATHS)
        (self.root / "requirements.txt").write_text("PyJWT\n")
        self.assertIn("requirements.txt", gate.snapshot(self.root))


@unittest.skipUnless(HAS_SANDBOX, "macOS sandbox")
class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-pyenv-", dir=PROJECT / ".supervisor")).resolve()   # in HOME
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "project"
        self.real = self.project / "instances" / NAME
        for d in ("notes", ".tmp"):
            (self.real / d).mkdir(parents=True)
        self.py = make_venv(self.real)
        p = mock.patch.object(core, "SUP_DIR", self.tmp / "sup")
        p.start()
        self.addCleanup(p.stop)

    def sandboxed(self, prof: str, cwd: Path, argv: list[str]) -> str:
        path = self.tmp / "p.sb"
        path.write_text(prof)
        r = subprocess.run(sandbox.wrap(path, argv), cwd=cwd, capture_output=True, text=True)
        return r.stdout + r.stderr

    def test_a_copy_reads_the_real_environment_and_cannot_change_it(self):
        copy = Path(tempfile.mkdtemp(prefix="shaft-copy-")).resolve()   # where coder and trial copies live
        self.addCleanup(shutil.rmtree, copy, True)
        probe = ("import shaftprobe, pathlib; print('IMPORT', shaftprobe.WHERE)\n"
                 f"p = pathlib.Path({str(self.real / '.venv' / 'planted.txt')!r})\n"
                 "try: p.write_text('x'); print('WRITE OK')\nexcept PermissionError: print('WRITE DENIED')\n")
        out = self.sandboxed(sandbox.profile(copy, HOME, [], env_root=self.real), copy, [str(self.py), "-c", probe])
        self.assertIn("IMPORT from the instance environment", out)
        self.assertIn("WRITE DENIED", out)
        self.assertFalse((self.real / ".venv" / "planted.txt").exists())
        # Without the read grant the copy could not even start the interpreter: the grant is what makes it work.
        out = self.sandboxed(sandbox.profile(copy, HOME, []), copy, [str(self.py), "-c", "print('STARTED')"])
        self.assertNotIn("STARTED", out)

    def test_run_uses_the_environment_and_may_change_it(self):
        b = broker.Broker(NAME, self.project, {}, self.tmp / "broker.log")
        b.start()
        self.addCleanup(b.stop)
        out = b.call("run", {"command": "python3 -c 'import shaftprobe, sys; print(shaftprobe.WHERE, sys.prefix)'"})
        self.assertIn("from the instance environment", out["result"])
        self.assertIn(str(self.real / ".venv"), out["result"])
        out = b.call("run", {"command": "echo 3.12 > .python-version && echo pyjwt > requirements.txt && "
                                        "touch .venv/marker && mkdir -p .uv/cache && echo ok"})["result"]
        self.assertIn("exit 0", out)
        self.assertTrue((self.real / ".venv" / "marker").exists())
        self.assertEqual((self.real / ".python-version").read_text().strip(), "3.12")
        out = b.call("run", {"command": "echo x > elsewhere.txt"})["result"]   # the rest of the root stays closed
        self.assertNotIn("exit 0", out)
        self.assertFalse((self.real / "elsewhere.txt").exists())

    @unittest.skipUnless(shutil.which("uv"), "needs uv")
    def test_uv_is_there_and_keeps_its_state_in_the_instance(self):
        b = broker.Broker(NAME, self.project, {}, self.tmp / "broker.log")
        b.start()
        self.addCleanup(b.stop)
        out = b.call("run", {"command": "uv --version && echo $UV_CACHE_DIR"})["result"]
        self.assertIn("exit 0", out)
        self.assertIn("uv ", out)
        self.assertIn(str(self.real / ".uv" / "cache"), out)

    @unittest.skipUnless(shutil.which("uv"), "needs uv")
    def test_a_lost_environment_is_rebuilt_from_its_declaration_at_wake(self):
        shutil.rmtree(self.real / ".venv")
        (self.real / "requirements.txt").write_text("")   # nothing to download: the test stays offline
        b = broker.Broker(NAME, self.project, {}, self.tmp / "broker.log")
        b.start()
        self.addCleanup(b.stop)
        with mock.patch.object(runner, "log") as log:
            runner.rebuild_env(NAME, self.real, b)
        self.assertTrue((self.real / ".venv" / "bin" / "python").exists(), log.call_args)
        self.assertIn("rebuilt", log.call_args[0][0])

    def test_a_failed_rebuild_is_noted_for_the_instance_not_fatal(self):
        shutil.rmtree(self.real / ".venv")
        (self.real / "requirements.txt").write_text("nosuchpackage\n")
        b = broker.Broker(NAME, self.project, {}, self.tmp / "broker.log")
        with mock.patch.object(b, "_run", return_value="exit 1 · 0.2 s\n--- stderr ---\nNo solution found"), \
                mock.patch.object(runner, "log") as log:
            runner.rebuild_env(NAME, self.real, b)
        self.assertIn("NOT rebuilt", log.call_args[0][0])
        journal = (self.real / "memory" / "journal.md").read_text()
        self.assertIn("could not be rebuilt", journal)
        self.assertIn("No solution found", journal)


class GateTest(unittest.TestCase):
    """The gate checks plugins and the instance's tests on its own environment; the supervisor's invariants, which
    are trusted code, stay on the supervisor's Python."""

    def test_plugins_and_tests_on_the_instance_python_invariants_on_the_supervisors(self):
        real = Path(tempfile.mkdtemp(prefix="shaft-pyenv-")).resolve()
        copy = Path(tempfile.mkdtemp(prefix="shaft-copy-")).resolve()
        for d in (real, copy):
            self.addCleanup(shutil.rmtree, d, True)
        (real / ".venv").mkdir()
        (copy / "plugins").mkdir()
        (copy / "tests").mkdir()
        calls, profiles = [], []

        def fake_stage(stage, argv, timeout_s, **kw):   # every command the gate runs goes through _stage
            calls.append((argv, kw))
            if argv[0] == sandbox.SANDBOX_EXEC:          # the profile is deleted after the gate: read it now
                profiles.append(Path(argv[2]).read_text())
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(gate, "_stage", side_effect=fake_stage), \
                mock.patch.object(gate, "committed_tree", return_value=(real, "abc")):   # test_gate covers it
            ok, _ = gate.run_tests(copy, HOME, env_root=real)
        self.assertTrue(ok)
        venv_py = str(real / ".venv" / "bin" / "python")
        invariants, plugins, tests = calls
        self.assertEqual(invariants[0][0], PYTHON)
        self.assertEqual(invariants[1]["cwd"], real)                 # the committed tree, not the checkout
        self.assertIn(venv_py, plugins[0])
        self.assertIn(venv_py, tests[0])
        self.assertEqual(plugins[1]["env"]["SHAFT_PYTHON"], venv_py)
        self.assertTrue(profiles and all(str(real / ".venv") in prof for prof in profiles))


class TemplateTest(unittest.TestCase):
    """The template's side: plugins run on SHAFT_PYTHON, and list_files does not walk the environment."""

    def setUp(self):
        sys.path.insert(0, str(TEMPLATE))
        self.addCleanup(sys.path.remove, str(TEMPLATE))
        self.root = Path(tempfile.mkdtemp(prefix="shaft-inst-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copytree(TEMPLATE / "shaft", self.root / "shaft", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "notes").mkdir()

    def box(self, **env):
        from shaft.tools import Toolbox
        with mock.patch.dict(os.environ, env, clear=False):
            return Toolbox(self.root)

    def test_a_plugin_imports_a_package_from_the_instance_environment(self):
        py = make_venv(self.root)
        plug = self.root / "plugins" / "probe"
        plug.mkdir(parents=True)
        (plug / "plugin.json").write_text(json.dumps({"name": "probe", "description": "probe",
                                                     "parameters": {"type": "object", "properties": {}}}))
        (plug / "main.py").write_text("import shaftprobe\n\ndef run(**args):\n    return shaftprobe.WHERE\n")
        with mock.patch.dict(os.environ, {"SHAFT_PYTHON": str(py)}):
            out = self.box()._run_plugin("probe", {})
        self.assertIn("from the instance environment", out)
        os.environ.pop("SHAFT_PYTHON", None)
        out = self.box()._run_plugin("probe", {})   # the system Python has no such package: the bug this fixes
        self.assertIn("ERROR", out)

    def test_list_files_names_the_environment_without_walking_it(self):
        deep = self.root / ".venv" / "lib" / "python3.12" / "site-packages" / "pkg"
        deep.mkdir(parents=True)
        (deep / "mod.py").write_text("")
        (self.root / ".uv" / "cache").mkdir(parents=True)
        (self.root / "notes" / "a.md").write_text("a")
        listing = self.box().run("list_files", json.dumps({"path": "."}))
        self.assertIn("notes/a.md", listing)
        self.assertIn(".venv/ (not listed: list_files .venv)", listing)
        self.assertIn(".uv/ (not listed", listing)
        self.assertNotIn("site-packages", listing)
        inside = self.box().run("list_files", json.dumps({"path": ".venv"}))
        self.assertIn("site-packages/pkg/mod.py", inside)


if __name__ == "__main__":
    unittest.main()
