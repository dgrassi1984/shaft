"""#86: launched imports, gate tests and commits use protected snapshot bytes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from supervisor import battery, broker, core, gate, protected, runner, sandbox
from supervisor.tests.test_run_transaction import RunTransactionCase, git


class ProtectedSnapshotTest(RunTransactionCase):
    """#86: exercise real imports, test discovery and git with deterministic writers in both windows."""

    def settings(self):
        stack = ExitStack()
        sup = self.project / ".supervisor"
        for target, attr, value in ((runner, "SUP_DIR", sup), (core, "SUP_DIR", sup),
                                    (runner, "PROJECT", self.project), (core, "PROJECT", self.project),
                                    (core, "INSTANCES", self.project / "instances")):
            stack.enter_context(mock.patch.object(target, attr, value))
        stack.enter_context(mock.patch.object(runner, "log"))
        stack.enter_context(mock.patch("supervisor.metrics.start_probation"))
        stack.enter_context(mock.patch("supervisor.metrics.after_cycle"))
        return stack

    def fake_broker(self):
        b = mock.Mock(port=1234, token="test", shared=None, env_root=None, handoffs=[], config_writes=[], counts={})
        b.egress.port = 1235
        b.mail_env.return_value = b.calendar_env.return_value = ""
        b.sandbox_env.return_value = {}
        return b

    def launch_settings(self, b):
        stack = self.settings()
        for patch in (mock.patch.object(runner, "Broker", return_value=b),
                      mock.patch.object(runner, "instance_config", return_value={}),
                      mock.patch.object(runner, "secrets_env", return_value={}),
                      mock.patch.object(runner, "rebuild_env"), mock.patch.object(runner, "stage_docs"),
                      mock.patch.object(runner, "_sees_images", return_value=False),
                      mock.patch.object(runner, "open_conversations", return_value={}),
                      mock.patch("supervisor.autonomy.describe_schedule", return_value="test")):
            stack.enter_context(patch)
        return stack

    def test_a_write_after_reconcile_does_not_supply_the_launched_module_or_mind_location(self):
        """#86: the production launch imports approved code while cwd and SHAFT_ROOT keep the live mind."""
        main = self.root / "shaft" / "__main__.py"
        main.write_text("import os\nfrom pathlib import Path\n"
                        "p = Path(os.environ['SHAFT_ROOT']) / 'memory/journal.md'\n"
                        "assert p.read_text() == '# kant — journal\\n'\n"
                        "p.write_text(p.read_text() + 'approved ran\\n')\n"
                        "Path('notes').mkdir(exist_ok=True)\nPath('notes/result').write_text('approved')\n"
                        "print(__file__)\n")
        git(self.root, "add", "shaft")
        git(self.root, "commit", "-qm", "approved agent")
        b, output = self.fake_broker(), []
        with self.launch_settings(b), mock.patch.object(runner, "_settle", return_value=True), \
                mock.patch.object(sandbox, "wrap", side_effect=lambda profile, cmd: cmd):
            reconcile = runner._reconcile

            def checked_then_swapped(name, root):
                approved = reconcile(name, root)
                main.write_text("raise RuntimeError('unchecked agent ran')\n")
                (root / "runpy.py").write_text("raise RuntimeError('cwd supplied bootstrap')\n")
                return approved

            with mock.patch.object(runner, "_reconcile", side_effect=checked_then_swapped):
                self.assertEqual(runner._run("kant", self.root, "live", [], False, output=output), 0)
        code_file = Path(output[0].strip())
        self.assertNotEqual(code_file, main)
        self.assertFalse(code_file.parent.parent.exists(), "launch copy outlived the run")
        self.assertEqual((self.root / "notes/result").read_text(), "approved")
        self.assertEqual((self.root / "memory/journal.md").read_text(), "# kant — journal\napproved ran\n")

    def test_swapped_working_code_is_neither_tested_nor_committed(self):
        """#86: a write after snapshot cannot change actual test imports or the committed plugin bytes."""
        before = runner._snapshot(self.root)
        plugin = self.root / "plugins/text_stats/main.py"
        good = "def run(**args):\n    return 'approved plugin'\n"
        plugin.write_text(good)
        (self.root / "tests").mkdir()
        (self.root / "tests/test_plugin.py").write_text(
            "import unittest\nfrom plugins.text_stats.main import run\n"
            "class T(unittest.TestCase):\n    def test_plugin(self):\n"
            "        self.assertEqual(run(), 'approved plugin')\n")
        seen = []

        def tests(tree, home, env_root=None, data_root=None):
            self.assertEqual(env_root, self.root)
            plugin.write_text("raise RuntimeError('swapped before tests')\n")
            seen.append(tree)
            self.assertEqual(gate.snapshot(tree)["plugins/text_stats/main.py"], good.encode())
            with mock.patch.object(sandbox, "wrap", side_effect=lambda profile, cmd: cmd):
                return gate.run_instance_tests(tree, tree / ".tmp/p.sb",
                                               {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, sys.executable)

        with self.settings(), mock.patch.object(gate, "run_tests", side_effect=tests):
            self.assertTrue(runner._settle("kant", self.root, "live", before, []))
        self.assertEqual(git(self.root, "show", "HEAD:plugins/text_stats/main.py"), good)
        self.assertEqual(plugin.read_text(), good)
        self.assertFalse(seen[0].exists(), "gate copy outlived settlement")

    def test_copy_permissions_profile_and_exception_cleanup(self):
        """#86: code is mode 444 under mode 555 folders, outside the instance's writable set."""
        b = self.fake_broker()
        with self.launch_settings(b):
            _, cmd, env = runner._launch("kant", self.root, "live", [], None, gate.snapshot(self.root))
            tree = Path(env["SHAFT_CODE_ROOT"])
            try:
                self.assertNotIn(self.root, tree.parents)
                self.assertEqual(tree.stat().st_mode & 0o777, 0o555)
                self.assertEqual((tree / "shaft/__init__.py").stat().st_mode & 0o777, 0o444)
                profile = (runner.SUP_DIR / "kant.sb").read_text()
                self.assertIn(f'(deny file-write* (subpath "{tree}"))', profile)
                self.assertIn(f'(allow file-write* (subpath "{self.root}")', profile)
                self.assertIn("-I", cmd)
            finally:
                protected.remove(tree)
            parent = runner.SUP_DIR / "protected/kant"
            try:
                with protected.copy(gate.snapshot(self.root), parent, "kant") as tree:
                    raise RuntimeError("interrupted gate")
            except RuntimeError:
                pass
            self.assertFalse(tree.exists())

    def test_launch_copy_is_removed_when_process_creation_fails(self):
        """#86: a launch failure cannot leak a protected copy."""
        with self.launch_settings(self.fake_broker()), \
                mock.patch.object(runner, "_start", side_effect=OSError("cannot start")):
            with self.assertRaisesRegex(OSError, "cannot start"):
                runner._run("kant", self.root, "live", [], False)
            self.assertEqual(list((runner.SUP_DIR / "protected/kant").iterdir()), [])

    def test_stale_copies_are_swept_and_live_copies_survive(self):
        """#86: startup sweeps a dead supervisor's copy, preserving active conversations."""
        parent = self.project / "protected"
        tree = protected.make(gate.snapshot(self.root), parent, "kant")
        with mock.patch.object(protected.os, "kill", return_value=None):
            protected.sweep(parent)
        self.assertTrue(tree.exists())
        with mock.patch.object(protected.os, "kill", side_effect=ProcessLookupError):
            protected.sweep(parent)
        self.assertFalse(tree.exists())

    def test_copy_limit_and_refused_code_fail_closed(self):
        """#86: materialization never bypasses snapshot refusal or SHAFT_COPY_MAX_GB."""
        with mock.patch.object(core, "COPY_MAX_GB", 0):
            with self.assertRaises(core.TreeTooBig):
                protected.make({"shaft/a.py": b"x"}, self.project / "protected", "kant")
        with self.assertRaises(ValueError):
            protected.make({"shaft": gate.Link("elsewhere")}, self.project / "protected", "kant")

    def test_trial_command_and_battery_copy_keep_their_existing_paths(self):
        """#86: trial agent_command has no live-launch redirect; exams retain their own writable copies."""
        b = self.fake_broker()
        with self.launch_settings(b), mock.patch.object(protected, "make", side_effect=AssertionError("double copy")):
            cmd, env = runner.agent_command(self.root, b, ["live"], self.project / "trial.sb", [], {})
            self.assertEqual(cmd[-6:], [runner.PYTHON, "-E", "-s", "-m", "shaft", "live"])
            self.assertNotIn("SHAFT_CODE_ROOT", env)
            with mock.patch.object(battery, "SUP_DIR", self.project / "sup"):
                tree = battery.make_exam_copy("kant", self.root)
                (tree / "shaft/__init__.py").write_text("exam's own edit\n")
                self.assertEqual((self.root / "shaft/__init__.py").read_text(), "")

    def test_a_new_working_plugin_is_checked_and_committed_from_the_settlement_copy(self):
        """#86: adding a plugin during a run gates its manifest and code, including files absent at launch."""
        shutil.copy(Path(__file__).resolve().parents[2] / "template/shaft/plugin_runner.py",
                    self.root / "shaft/plugin_runner.py")
        before = runner._snapshot(self.root)
        folder = self.root / "plugins/new"
        folder.mkdir()
        (folder / "plugin.json").write_text(json.dumps(
            {"name": "new", "description": "test", "parameters": {"type": "object"}}))
        source = "def run(**args):\n    return 'new working plugin'\n"
        (folder / "main.py").write_text(source)
        checked = []

        def check(tree, home, env_root=None, data_root=None):
            result = subprocess.run([sys.executable, "-E", "-s", "-m", "shaft.plugin_runner", "--check"],
                                    cwd=tree, capture_output=True, text=True)
            checked.append(result.stdout)
            return result.returncode == 0, result.stdout + result.stderr

        with self.settings(), mock.patch.object(gate, "run_tests", side_effect=check):
            self.assertTrue(runner._settle("kant", self.root, "live", before, []))
        self.assertEqual(checked, ["ok   new\n"])
        self.assertEqual(git(self.root, "show", "HEAD:plugins/new/main.py"), source)

    def test_cleanup_does_not_chmod_a_link_in_test_scratch(self):
        """#86: removing the read-only tree never follows a link a test left in .tmp."""
        outside = self.project / "outside"
        outside.mkdir(mode=0o755)
        with self.settings(), protected.copy(gate.snapshot(self.root), runner.SUP_DIR / "protected/kant", "kant") as tree:
            (tree / ".tmp/link").symlink_to(outside, target_is_directory=True)
        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
        self.assertFalse(tree.exists())

    def test_a_new_plugin_is_runnable_via_broker_run_and_gated_at_settlement(self):
        """#86: sandbox integration, a new working plugin remains runnable before the gate commits it."""
        shutil.copy(Path(__file__).resolve().parents[2] / "template/shaft/plugin_runner.py",
                    self.root / "shaft/plugin_runner.py")
        git(self.root, "add", "shaft")
        git(self.root, "commit", "-qm", "plugin runner")
        before = runner._snapshot(self.root)
        with self.settings(), protected.copy(before["code"], runner.SUP_DIR / "protected/kant", "kant") as tree:
            folder = self.root / "plugins/new"
            folder.mkdir()
            manifest = {"name": "new", "description": "test", "parameters": {"type": "object"}}
            (folder / "plugin.json").write_text(json.dumps(manifest))
            (folder / "main.py").write_text("def run(**args):\n    return 'new working plugin'\n")
            self.assertFalse((tree / "plugins/new/main.py").exists())
            b = broker.Broker("kant", self.project, {}, runner.SUP_DIR / "broker.log")
            b.egress = mock.Mock(port=0)
            with mock.patch.object(b, "sandbox_env", return_value={}), \
                    mock.patch.object(b, "mail_env", return_value=""), \
                    mock.patch.object(b, "calendar_env", return_value=""):
                out = b._run("python3 -E -s -m shaft.plugin_runner new")
            self.assertTrue(out.startswith("exit 0"), out)
            self.assertIn("new working plugin", out)

            def check(copy, home, env_root=None, data_root=None):
                prof = copy / ".tmp/check.sb"
                prof.write_text(sandbox.profile(copy, home, []))
                result = subprocess.run(sandbox.wrap(prof, [sys.executable, "-E", "-s", "-m",
                                        "shaft.plugin_runner", "--check"]), cwd=copy, capture_output=True, text=True)
                return result.returncode == 0, result.stdout + result.stderr

            with mock.patch.object(gate, "run_tests", side_effect=check):
                self.assertTrue(runner._settle("kant", self.root, "live", before, []))
            self.assertEqual(git(self.root, "show", "HEAD:plugins/new/main.py"),
                             "def run(**args):\n    return 'new working plugin'\n")

    def test_sandbox_cannot_rewrite_chmod_or_replace_launched_code(self):
        """#86: sandbox integration proves the read-only boundary even for the supervisor's Unix uid."""
        with self.settings(), protected.copy(gate.snapshot(self.root), runner.SUP_DIR / "protected/kant", "kant") as tree:
            prof = self.project / "protected.sb"
            prof.write_text(sandbox.profile(self.root, core.HOME, []) + sandbox.protected_code(tree, core.HOME))
            script = ("import os, pathlib, sys\np = pathlib.Path(sys.argv[1])\n"
                      "assert p.read_bytes() == b''\n"
                      "def through_link():\n    q = pathlib.Path('.tmp/alias')\n"
                      "    os.link(p, q)\n    q.chmod(0o644)\n    q.write_text('bad')\n"
                      "for operation in (lambda: p.write_text('bad'), lambda: p.chmod(0o644), "
                      "lambda: p.unlink(), lambda: p.parent.rename(p.parent.with_name('moved')), through_link):\n"
                      "    try:\n        operation()\n    except PermissionError:\n        pass\n"
                      "    else:\n        raise AssertionError('code was writable')\n")
            r = subprocess.run(sandbox.wrap(prof, [sys.executable, "-I", "-c", script,
                               str(tree / "shaft/__init__.py")]), cwd=self.root, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_gate_tests_find_the_instance_data_beside_their_protected_code(self):
        """Regression, 2026-10-29: #86 ran the gate's tests in a copy that held only the code, so a test finding the
        instance's notes from its own file (wren's Path(__file__).parent.parent / "notes/ledger/…") failed with
        FileNotFoundError, and every code change wren made was reverted whatever it was. Real gate, real sandbox: the
        tests read and write the instance's data as before #86, and still write neither copy of the code."""
        shutil.copy(Path(__file__).resolve().parents[2] / "template/shaft/plugin_runner.py",
                    self.root / "shaft/plugin_runner.py")
        git(self.root, "add", "shaft")
        git(self.root, "commit", "-qm", "plugin runner")
        before = runner._snapshot(self.root)
        (self.root / "notes/ledger").mkdir(parents=True)
        (self.root / "notes/ledger/key").write_text("treasury")
        (self.root / ".uv").mkdir()   # an empty .venv would choose a missing interpreter (pyenv.python)
        (self.root / "tests").mkdir()
        (self.root / "tests/test_data.py").write_text(
            "import pathlib, tempfile, unittest\n"
            "ROOT = pathlib.Path(__file__).resolve().parent.parent\n"
            "class T(unittest.TestCase):\n"
            "    def test_data(self):\n"
            "        self.assertEqual((ROOT / 'notes/ledger/key').read_text(), 'treasury')\n"
            "        self.assertEqual(pathlib.Path('memory/journal.md').read_text(), '# kant — journal\\n')\n"
            "        (ROOT / 'notes/written').write_text('by the gate')\n"
            "        with tempfile.NamedTemporaryFile() as f:\n"   # the copy's .tmp, its scratch space
            "            f.write(b'scratch')\n"
            "    def test_code_stays_read_only(self):\n"
            f"        for p in (ROOT / 'shaft/__init__.py', pathlib.Path({str(self.root / 'shaft/__init__.py')!r}),\n"
            f"                  pathlib.Path({str(self.root / '.uv/lib.py')!r})):\n"
            "            with self.assertRaises(PermissionError):\n"
            "                p.write_text('bad')\n")
        with self.settings(), mock.patch.object(gate, "_supervisor_tests", return_value=(True, "", "")):
            kept = runner._settle("kant", self.root, "live", before, [])
            said = [c.args[0] for c in runner.log.call_args_list]
            self.assertEqual(list((runner.SUP_DIR / "protected/kant").iterdir()), [])
        self.assertTrue(kept)
        self.assertTrue(any("code change passed tests" in s for s in said), said)
        self.assertEqual((self.root / "notes/written").read_text(), "by the gate")
        self.assertEqual((self.root / "shaft/__init__.py").read_text(), "")

    def test_launched_code_finds_the_instance_data_from_its_own_file(self):
        """Regression, 2026-10-29: the same #86 gap at launch — approved code that finds the instance's notes from its
        own file (wren's treasury key) looked in the protected copy, which had no notes."""
        (self.root / "notes/ledger").mkdir(parents=True)
        (self.root / "notes/ledger/key").write_text("treasury")
        (self.root / "shaft/__main__.py").write_text(
            "from pathlib import Path\nprint((Path(__file__).resolve().parent.parent / 'notes/ledger/key').read_text())\n")
        git(self.root, "add", "shaft")
        git(self.root, "commit", "-qm", "reads its key")
        b, output = self.fake_broker(), []
        with self.launch_settings(b), mock.patch.object(runner, "_settle", return_value=True), \
                mock.patch.object(sandbox, "wrap", side_effect=lambda profile, cmd: cmd):
            self.assertEqual(runner._run("kant", self.root, "live", [], False, output=output), 0)
        self.assertEqual(output[0].strip(), "treasury")

    def test_only_data_is_linked_never_code_config_or_scratch(self):
        """The links into the instance carry its data only: a code path the snapshot lacks (a tests/ holding nothing
        but __pycache__) is absent, never a link to the unprotected working code; the gate's config.env and .tmp are
        the copy's own."""
        (self.root / "tests/__pycache__").mkdir(parents=True)
        (self.root / "config.env").write_text("LIVE=1\n")
        code = gate.snapshot(self.root)
        self.assertNotIn("tests", {p.split("/")[0] for p in code})
        tree = protected.make(code, self.project / "protected", "kant", b"JUDGED=1\n", instance=self.root)
        try:
            self.assertFalse((tree / "tests").exists() or (tree / "tests").is_symlink())
            for name in ("shaft", "plugins", ".tmp", "config.env"):
                self.assertFalse((tree / name).is_symlink(), name)
            self.assertEqual((tree / "config.env").read_text(), "JUDGED=1\n")
            self.assertEqual((tree / "memory").resolve(), self.root / "memory")
            self.assertEqual((tree / "memory/journal.md").read_text(), "# kant — journal\n")
        finally:
            protected.remove(tree)
        self.assertTrue((self.root / "memory/journal.md").exists(), "removing the copy followed a link")

    def test_nothing_linked_can_supply_an_import(self):
        """Codex review of the fix: the copy leads the import path (the launch puts it first, the gate runs -m unittest
        in it), so a linked unittest.py would have replaced the gate's own runner and a linked json.py the launched
        code's json, from bytes never checked. Module files and packages are not linked; a plain folder is."""
        for name in ("unittest.py", "json.pyc", "fast.cpython-312-darwin.so", "x.pth", "Upper.PY"):
            (self.root / name).write_text("raise SystemExit(0)\n")
        (self.root / "pkg").mkdir()
        (self.root / "pkg/__init__.py").write_text("")
        tree = protected.make(gate.snapshot(self.root), self.project / "protected", "kant", instance=self.root)
        try:
            for name in ("unittest.py", "json.pyc", "fast.cpython-312-darwin.so", "x.pth", "Upper.PY", "pkg"):
                self.assertFalse(os.path.lexists(tree / name), name)
            self.assertTrue((tree / "memory").is_symlink())
            r = subprocess.run([sys.executable, "-E", "-s", "-m", "unittest", "--help"], cwd=tree,
                               capture_output=True, text=True)
            self.assertIn("discover", r.stdout)
        finally:
            protected.remove(tree)
